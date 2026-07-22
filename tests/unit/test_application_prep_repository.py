from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.sql import ClauseElement

from careerops.application.application_prep import (
    ApprovedMaterialRef,
    CandidateMatchProfile,
    EvidenceFirstApplicationPreparer,
    EvidenceRef,
    PreparedApplicationDraft,
    PublicJobDiscovery,
    materialize_json_mapping,
)
from careerops.infrastructure.database import application_prep
from careerops.infrastructure.database.application_prep import PostgresApplicationDraftStore

NOW = datetime(2026, 7, 19, 17, 0, tzinfo=UTC)


class FakeConnection:
    def __init__(
        self,
        *,
        in_transaction: bool = True,
        inserted_id: UUID | None = None,
        insert_succeeds: bool = True,
        existing: dict[str, object] | None = None,
    ) -> None:
        self._in_transaction = in_transaction
        self._inserted_id = inserted_id or uuid4() if insert_succeeds else None
        self._existing = existing
        self.statements: list[ClauseElement] = []

    def in_transaction(self) -> bool:
        return self._in_transaction

    def scalar(self, statement: sa.sql.Executable) -> UUID | None:
        self.statements.append(cast("ClauseElement", statement))
        return self._inserted_id

    def execute(self, statement: sa.sql.Executable) -> FakeResult:
        self.statements.append(cast("ClauseElement", statement))
        return FakeResult(self._existing)


class FakeResult:
    def __init__(self, existing: dict[str, object] | None) -> None:
        self._existing = existing

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._existing


def draft() -> PreparedApplicationDraft:
    return EvidenceFirstApplicationPreparer().prepare(
        discovery=PublicJobDiscovery(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000201"),
            job_posting_id=UUID("00000000-0000-0000-0000-000000000202"),
            company_name="Example AI",
            title="Agent Engineer",
            canonical_url="https://jobs.example.com/123",
            source_type="public_ats",
            required_keywords=("python", "agents"),
            evidence=(
                EvidenceRef(
                    evidence_id=UUID("00000000-0000-0000-0000-000000000101"),
                    content_hash="a" * 64,
                    span_hash="b" * 64,
                    source_url="https://jobs.example.com/123",
                    provider_id=None,
                    sanitized_span="Python agents role.",
                ),
            ),
        ),
        profile=CandidateMatchProfile(
            candidate_id=UUID("00000000-0000-0000-0000-000000000301"),
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


def test_postgres_application_draft_store_requires_explicit_transaction() -> None:
    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresApplicationDraftStore(cast("Connection", FakeConnection(in_transaction=False)))


def test_postgres_application_draft_store_persists_internal_intent_without_outbox() -> None:
    connection = FakeConnection()
    store = PostgresApplicationDraftStore(cast("Connection", connection))

    intent_id = store.save_prepared_draft(draft(), created_by="agent:careerops")

    assert intent_id
    assert len(connection.statements) == 3
    rendered = "\n".join(str(statement) for statement in connection.statements)
    assert "INSERT INTO careerops.action_intents" in rendered
    assert "INSERT INTO careerops.action_payload_versions" in rendered
    assert "UPDATE careerops.action_intents" in rendered
    assert "outbox" not in rendered
    compiled_insert = connection.statements[0].compile()
    assert compiled_insert.params is not None
    assert compiled_insert.params["action_kind"] == "create_internal_draft"
    assert compiled_insert.params["status"] == "proposed"


def test_postgres_application_draft_store_reuses_an_exact_existing_draft() -> None:
    item = draft()
    existing_id = UUID("00000000-0000-0000-0000-000000000999")
    connection = FakeConnection(
        insert_succeeds=False,
        existing={
            "intent_id": existing_id,
            "action_kind": item.action_kind,
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "idempotency_key": item.idempotency_key,
            "target": materialize_json_mapping(item.target),
            "payload": materialize_json_mapping(item.payload),
            "attachment_refs": [materialize_json_mapping(value) for value in item.attachment_refs],
            "payload_hash": item.payload_hash,
        },
    )

    reused_id = PostgresApplicationDraftStore(cast("Connection", connection)).save_prepared_draft(
        item,
        created_by="agent:careerops",
    )

    assert reused_id == existing_id
    assert len(connection.statements) == 2
    rendered = "\n".join(str(statement) for statement in connection.statements)
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in rendered
    assert "FOR UPDATE" in rendered
    assert "INSERT INTO careerops.action_payload_versions" not in rendered


def test_postgres_application_draft_store_rejects_external_action_kind() -> None:
    original = draft()
    unsafe = PreparedApplicationDraft(
        prepared_at=original.prepared_at,
        action_kind="submit_application",
        resource_type=original.resource_type,
        resource_id=original.resource_id,
        idempotency_key=original.idempotency_key,
        target=original.target,
        payload=original.payload,
        attachment_refs=original.attachment_refs,
        payload_hash=original.payload_hash,
        match_score=original.match_score,
        reason_codes=original.reason_codes,
    )

    with pytest.raises(ValueError, match="internal"):
        PostgresApplicationDraftStore(cast("Connection", FakeConnection())).save_prepared_draft(
            unsafe,
            created_by="agent:careerops",
        )


def test_postgres_application_draft_store_rejects_a_stale_draft_hash() -> None:
    stale = replace(draft(), payload_hash="d" * 64)

    with pytest.raises(ValueError, match="payload hash"):
        PostgresApplicationDraftStore(cast("Connection", FakeConnection())).save_prepared_draft(
            stale,
            created_by="agent:careerops",
        )


def test_application_prep_repository_does_not_import_execution_surfaces() -> None:
    import_lines = [
        line
        for line in inspect.getsource(application_prep).splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]

    assert all("outbox" not in line for line in import_lines)
    assert all("provider" not in line for line in import_lines)
    assert all("browser" not in line for line in import_lines)
    assert all("side_effect" not in line for line in import_lines)
