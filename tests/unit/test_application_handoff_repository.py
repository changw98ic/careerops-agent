from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.sql import ClauseElement

from careerops.application.application_handoff import (
    ManualApplicationHandoff,
    ManualApplicationHandoffBuilder,
    ManualApplicationHandoffRef,
    ManualSubmissionAttestation,
)
from careerops.application.application_prep import (
    ApprovedMaterialRef,
    CandidateMatchProfile,
    EvidenceFirstApplicationPreparer,
    EvidenceRef,
    PublicJobDiscovery,
    materialize_json_mapping,
)
from careerops.infrastructure.database import application_handoff
from careerops.infrastructure.database.application_handoff import (
    PostgresManualApplicationHandoffStore,
    compile_query_for_test,
    insert_manual_handoff_intent_statement,
    insert_manual_submission_event_statement,
    select_manual_handoff_by_intent_statement,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000301")
CANONICAL_JOB_ID = UUID("00000000-0000-0000-0000-000000000201")
JOB_POSTING_ID = UUID("00000000-0000-0000-0000-000000000202")


class FakeResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class FakeConnection:
    def __init__(
        self,
        *,
        scalar_results: list[UUID | None] | None = None,
        selected_rows: list[dict[str, object] | None] | None = None,
        in_transaction: bool = True,
    ) -> None:
        self._scalar_results = list(scalar_results or [])
        self._selected_rows = list(selected_rows or [])
        self._in_transaction = in_transaction
        self.statements: list[ClauseElement] = []

    def in_transaction(self) -> bool:
        return self._in_transaction

    def scalar(self, statement: sa.sql.Executable) -> UUID | None:
        self.statements.append(cast("ClauseElement", statement))
        if not self._scalar_results:
            raise AssertionError("unexpected scalar query")
        return self._scalar_results.pop(0)

    def execute(self, statement: sa.sql.Executable) -> FakeResult:
        self.statements.append(cast("ClauseElement", statement))
        if str(statement).lstrip().startswith("SELECT"):
            if not self._selected_rows:
                raise AssertionError("unexpected select query")
            return FakeResult(self._selected_rows.pop(0))
        return FakeResult(None)


def handoff() -> ManualApplicationHandoff:
    draft = EvidenceFirstApplicationPreparer().prepare(
        discovery=PublicJobDiscovery(
            canonical_job_id=CANONICAL_JOB_ID,
            job_posting_id=JOB_POSTING_ID,
            company_name="Example AI",
            title="Agent Engineer",
            canonical_url="https://jobs.example.test/123",
            source_type="official_careers",
            required_keywords=("python", "agents"),
            evidence=(
                EvidenceRef(
                    evidence_id=UUID("00000000-0000-0000-0000-000000000101"),
                    content_hash="a" * 64,
                    span_hash="b" * 64,
                    source_url="https://jobs.example.test/123",
                    provider_id=None,
                    sanitized_span="Python agents role.",
                ),
            ),
        ),
        profile=CandidateMatchProfile(
            candidate_id=CANDIDATE_ID,
            desired_keywords=("python", "agents"),
        ),
        materials=(
            ApprovedMaterialRef(
                material_id=UUID("00000000-0000-0000-0000-000000000401"),
                material_kind="resume",
                sha256="c" * 64,
                label_zh="简历",
            ),
        ),
        now=NOW,
    )
    return ManualApplicationHandoffBuilder().prepare(
        draft=draft,
        candidate_id=CANDIDATE_ID,
        now=NOW,
    )


def existing_handoff_row(
    item: ManualApplicationHandoff,
    *,
    intent_id: UUID,
    payload_version_id: UUID,
) -> dict[str, object]:
    return {
        "intent_id": intent_id,
        "action_kind": "create_manual_application_handoff",
        "resource_type": "canonical_job",
        "resource_id": item.canonical_job_id,
        "idempotency_key": item.idempotency_key,
        "payload_version_id": payload_version_id,
        "target": materialize_json_mapping(item.target),
        "payload": materialize_json_mapping(item.payload),
        "attachment_refs": [materialize_json_mapping(value) for value in item.attachment_refs],
        "payload_hash": item.payload_hash,
    }


def test_store_requires_an_explicit_transaction() -> None:
    connection = cast("Connection", FakeConnection(in_transaction=False))
    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresManualApplicationHandoffStore(connection)


def test_store_persists_internal_handoff_without_outbox_or_provider_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = handoff()
    intent_id = uuid4()
    connection = FakeConnection(scalar_results=[intent_id])
    store = PostgresManualApplicationHandoffStore(cast("Connection", connection))
    audit_calls: list[dict[str, object]] = []

    def record_handoff_audit(**kwargs: object) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        store,
        "_append_handoff_prepared_audit",
        record_handoff_audit,
    )

    reference = store.save_handoff(
        item,
        created_by="user:owner",
        trace_id="manual-handoff-prepare",
    )

    assert reference.action_intent_id == intent_id
    assert reference.payload_hash == item.payload_hash
    assert len(connection.statements) == 4
    rendered = "\n".join(str(statement) for statement in connection.statements)
    assert "INSERT INTO careerops.action_intents" in rendered
    assert "INSERT INTO careerops.action_payload_versions" in rendered
    assert "UPDATE careerops.action_intents" in rendered
    assert "INSERT INTO careerops.application_events" in rendered
    assert "outbox" not in rendered
    assert "provider_receipts" not in rendered
    assert audit_calls[0]["reference"] == reference


def test_store_reuses_only_an_exact_existing_handoff() -> None:
    item = handoff()
    existing_intent_id = uuid4()
    existing_payload_version_id = uuid4()
    connection = FakeConnection(
        scalar_results=[None],
        selected_rows=[
            existing_handoff_row(
                item,
                intent_id=existing_intent_id,
                payload_version_id=existing_payload_version_id,
            )
        ],
    )

    reference = PostgresManualApplicationHandoffStore(cast("Connection", connection)).save_handoff(
        item,
        created_by="user:owner",
        trace_id="manual-handoff-retry",
    )

    assert reference.action_intent_id == existing_intent_id
    assert reference.payload_version_id == existing_payload_version_id
    assert len(connection.statements) == 2
    rendered = "\n".join(str(statement) for statement in connection.statements)
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in rendered
    assert "FOR UPDATE" in rendered
    assert "INSERT INTO careerops.application_events" not in rendered


def test_store_rejects_an_idempotency_conflict_bound_to_a_different_handoff() -> None:
    item = handoff()
    existing = existing_handoff_row(
        item,
        intent_id=uuid4(),
        payload_version_id=uuid4(),
    )
    existing["payload_hash"] = "f" * 64
    connection = FakeConnection(scalar_results=[None], selected_rows=[existing])

    with pytest.raises(
        application_handoff.ManualApplicationHandoffPersistenceError,
        match="different immutable handoff",
    ):
        PostgresManualApplicationHandoffStore(cast("Connection", connection)).save_handoff(
            item,
            created_by="user:owner",
            trace_id="manual-handoff-conflict",
        )


def test_manual_submission_record_is_idempotent_and_is_not_a_provider_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = handoff()
    reference = ManualApplicationHandoffRef(
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        payload_hash=item.payload_hash,
        candidate_id=item.candidate_id,
        canonical_job_id=item.canonical_job_id,
        job_posting_id=item.job_posting_id,
    )
    attestation = ManualSubmissionAttestation(
        receipt_reference="candidate-provided-reference-123",
        submitted_at=NOW,
    )
    event_id = uuid4()
    connection = FakeConnection(
        scalar_results=[event_id],
        selected_rows=[
            existing_handoff_row(
                item,
                intent_id=reference.action_intent_id,
                payload_version_id=reference.payload_version_id,
            )
        ],
    )
    store = PostgresManualApplicationHandoffStore(cast("Connection", connection))
    audit_calls: list[dict[str, object]] = []

    def record_submission_audit(**kwargs: object) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        store,
        "_append_manual_submission_audit",
        record_submission_audit,
    )

    result = store.record_manual_submission(
        reference,
        attestation,
        recorded_by="user:owner",
        trace_id="manual-handoff-confirm",
    )

    assert result.application_event_id == event_id
    assert result.newly_created is True
    rendered = "\n".join(str(statement) for statement in connection.statements)
    assert "ON CONFLICT (id) DO NOTHING" in rendered
    event_parameters = (
        cast("ClauseElement", connection.statements[-1])
        .compile(dialect=postgresql.dialect())
        .params
    )
    assert event_parameters is not None
    assert event_parameters["source_type"] == "user_attestation"
    assert event_parameters["event_data"]["provider_receipt_verified"] is False
    assert "outbox" not in rendered
    assert audit_calls[0]["attestation"] == attestation


def test_manual_submission_rejects_a_stale_or_mismatched_handoff_reference() -> None:
    item = handoff()
    reference = ManualApplicationHandoffRef(
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        payload_hash="d" * 64,
        candidate_id=item.candidate_id,
        canonical_job_id=item.canonical_job_id,
        job_posting_id=item.job_posting_id,
    )
    connection = FakeConnection(
        selected_rows=[
            existing_handoff_row(
                item,
                intent_id=reference.action_intent_id,
                payload_version_id=reference.payload_version_id,
            )
        ]
    )

    with pytest.raises(
        application_handoff.ManualApplicationHandoffPersistenceError,
        match="stale or mismatched",
    ):
        PostgresManualApplicationHandoffStore(
            cast("Connection", connection)
        ).record_manual_submission(
            reference,
            ManualSubmissionAttestation(
                receipt_reference="candidate-provided-reference-123",
                submitted_at=NOW,
            ),
            recorded_by="user:owner",
            trace_id="manual-handoff-stale",
        )


def test_manual_submission_retry_reuses_the_same_user_attestation_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = handoff()
    reference = ManualApplicationHandoffRef(
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        payload_hash=item.payload_hash,
        candidate_id=item.candidate_id,
        canonical_job_id=item.canonical_job_id,
        job_posting_id=item.job_posting_id,
    )
    attestation = ManualSubmissionAttestation(
        receipt_reference="candidate-provided-reference-123",
        submitted_at=NOW,
    )
    existing_event = {
        "candidate_id": reference.candidate_id,
        "canonical_job_id": reference.canonical_job_id,
        "job_posting_id": reference.job_posting_id,
        "event_type": "manual_submission_recorded",
        "source_type": "user_attestation",
        "source_id": str(reference.action_intent_id),
        "occurred_at": attestation.submitted_at,
        "event_data": {
            "mode": "human_final_submission",
            "provider_receipt_verified": False,
            "payload_hash": reference.payload_hash,
            "receipt_reference": attestation.receipt_reference,
        },
    }
    connection = FakeConnection(
        scalar_results=[None],
        selected_rows=[
            existing_handoff_row(
                item,
                intent_id=reference.action_intent_id,
                payload_version_id=reference.payload_version_id,
            ),
            existing_event,
        ],
    )
    store = PostgresManualApplicationHandoffStore(cast("Connection", connection))

    def unexpected_audit(**_kwargs: object) -> None:
        raise AssertionError("idempotent retry must not append a second audit event")

    monkeypatch.setattr(store, "_append_manual_submission_audit", unexpected_audit)

    result = store.record_manual_submission(
        reference,
        attestation,
        recorded_by="user:owner",
        trace_id="manual-handoff-retry",
    )

    assert result.newly_created is False
    assert len(connection.statements) == 3


def test_sql_binds_candidate_identity_and_handoff_payload_hash() -> None:
    item = handoff()
    intent_id = uuid4()
    payload_version_id = uuid4()
    reference = ManualApplicationHandoffRef(
        action_intent_id=intent_id,
        payload_version_id=payload_version_id,
        payload_hash=item.payload_hash,
        candidate_id=item.candidate_id,
        canonical_job_id=item.canonical_job_id,
        job_posting_id=item.job_posting_id,
    )
    intent_sql = compile_query_for_test(
        insert_manual_handoff_intent_statement(
            intent_id=intent_id,
            handoff=item,
            created_by="user:owner",
        )
    )
    submission_statement = insert_manual_submission_event_statement(
        event_id=uuid4(),
        reference=reference,
        attestation=ManualSubmissionAttestation(
            receipt_reference="candidate-provided-reference-123",
            submitted_at=NOW,
        ),
    )
    compiled_submission = cast("ClauseElement", submission_statement).compile(
        dialect=postgresql.dialect()
    )
    submission_sql = str(compiled_submission)
    submission_parameters = compiled_submission.params
    assert submission_parameters is not None
    select_sql = compile_query_for_test(select_manual_handoff_by_intent_statement(intent_id))

    assert "create_manual_application_handoff" in intent_sql
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in intent_sql
    assert "ON CONFLICT (id) DO NOTHING" in submission_sql
    assert submission_parameters["candidate_id"] == CANDIDATE_ID
    assert submission_parameters["event_data"]["payload_hash"] == item.payload_hash
    assert submission_parameters["event_data"]["provider_receipt_verified"] is False
    assert "FOR UPDATE OF action_intents" in select_sql


def test_handoff_repository_does_not_import_execution_surfaces() -> None:
    import_lines = [
        line
        for line in inspect.getsource(application_handoff).splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]

    for forbidden in (
        "outbox",
        "provider",
        "browser",
        "credential",
        "playwright",
        "side_effect",
    ):
        assert all(forbidden not in line for line in import_lines)
