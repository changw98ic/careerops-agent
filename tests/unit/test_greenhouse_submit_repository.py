from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import RowMapping

from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
)
from careerops.infrastructure.database.greenhouse_submit import (
    _dispatch_from_row,
    claim_greenhouse_submit_outbox_events_statement,
    get_greenhouse_submit_account_statement,
    get_greenhouse_submit_reconciliation_case_statement,
    list_greenhouse_submit_accounts_statement,
    list_greenhouse_submit_reconciliation_cases_statement,
    mark_greenhouse_submit_outbox_published_statement,
    record_greenhouse_submit_accepted_unverified_statement,
    record_greenhouse_submit_ambiguity_statement,
    record_greenhouse_submit_prepost_failure_statement,
    record_greenhouse_submit_rejected_statement,
    release_greenhouse_submit_outbox_event_statement,
    review_greenhouse_submit_reconciliation_evidence_statement,
    submit_greenhouse_submit_reconciliation_evidence_statement,
)
from careerops.infrastructure.greenhouse.client import (
    GreenhouseBrokerJournalState,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
)

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000111")


def test_dispatch_row_reconstructs_secret_free_domain_objects() -> None:
    schema = _schema()
    payload = _payload(schema)
    event_id = uuid4()
    intent_id = uuid4()
    payload_id = uuid4()
    account_id = uuid4()

    row = {
        "event_id": event_id,
        "event_key": "greenhouse-submit:reservation-1",
        "account_id": account_id,
        "opaque_broker_handle": "broker://greenhouse/example/profile-v1",
        "account_subject": "owner@example.com",
        "owner_user_id": OWNER_ID,
        "employer_id": "example-ai",
        "credential_profile_version": 1,
        "credential_fingerprint_sha256": "a" * 64,
        "credential_profile_status": "active",
        "credential_profile_expires_at": NOW + timedelta(days=1),
        "raw_response_sha256": schema.raw_response_sha256,
        "schema_json": _schema_payload(),
        "payload_json": _payload_document(schema, payload),
        "attachment_refs_json": [item.canonical() for item in payload.attachment_refs],
        "action_intent_id": intent_id,
        "payload_version_id": payload_id,
        "reservation_key": "reservation-1",
        "reconciliation_key": "reconcile-1",
        "prepare_state": "ready",
        "reason_code": None,
    }

    dispatch = _dispatch_from_row(cast("RowMapping", row))

    assert dispatch.event_id == event_id
    assert dispatch.action_intent_id == intent_id
    assert dispatch.payload_version_id == payload_id
    assert dispatch.account_id == account_id
    assert dispatch.schema.schema_hash == schema.schema_hash
    assert dispatch.payload.payload_hash == payload.payload_hash
    assert dispatch.credential_handle.opaque_handle == "broker://greenhouse/example/profile-v1"
    assert dispatch.credential_handle.board_token == "example"
    assert "api" not in repr(dispatch.credential_handle).lower()


def test_dispatch_row_rejects_payload_hash_drift() -> None:
    schema = _schema()
    payload = _payload(schema)
    row = _dispatch_row(schema, payload)
    payload_json = cast("dict[str, object]", row["payload_json"])
    row["payload_json"] = payload_json | {"payload_hash": "0" * 64}

    with pytest.raises(RuntimeError, match="payload hash drift"):
        _dispatch_from_row(cast("RowMapping", row))


def test_greenhouse_outbox_store_statements_are_channel_specific() -> None:
    event_id = uuid4()
    lease_token = uuid4()

    claim = claim_greenhouse_submit_outbox_events_statement(
        owner="greenhouse-sender-1", lease_seconds=60, limit=10
    )
    published = mark_greenhouse_submit_outbox_published_statement(
        event_id=event_id, owner="greenhouse-sender-1", lease_token=lease_token
    )
    released = release_greenhouse_submit_outbox_event_statement(
        event_id=event_id,
        owner="greenhouse-sender-1",
        lease_token=lease_token,
        retry_at=NOW + timedelta(minutes=5),
        error_code="GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE",
        terminal=False,
    )

    assert "claim_greenhouse_submit_outbox_events" in claim.text
    assert "mark_greenhouse_submit_outbox_published" in published.text
    assert "release_greenhouse_submit_outbox_event" in released.text
    assert "careerops.claim_outbox" not in claim.text


def test_outcome_statements_persist_evidence_without_generic_reclaim() -> None:
    event_id = uuid4()
    lease_token = uuid4()
    evidence = _evidence()
    rejected_evidence = _evidence(outcome=GreenhouseSubmissionOutcome.REJECTED)
    ambiguous_evidence = _evidence(outcome=GreenhouseSubmissionOutcome.AMBIGUOUS)

    accepted = record_greenhouse_submit_accepted_unverified_statement(
        event_id=event_id,
        owner="greenhouse-sender-1",
        lease_token=lease_token,
        reconciliation_key=evidence.reconciliation_key,
        provider_timestamp=NOW,
        receipt_id=uuid4(),
        http_status=202,
        broker_request_sha256=evidence.broker_request_sha256,
        broker_response_sha256=evidence.broker_response_sha256,
        provider_request_sha256=evidence.provider_request_sha256 or "",
        provider_response_sha256=evidence.provider_response_sha256 or "",
        observed_raw_response_sha256=evidence.observed_schema_raw_response_sha256 or "",
        observed_normalized_schema_sha256=evidence.observed_schema_hash or "",
        journal_receipt_sha256=evidence.journal_receipt_hash or "",
        journal_state=GreenhouseBrokerJournalState.RESPONSE_OBSERVED.value,
        journal_sequence=1,
        evidence_sha256=evidence.evidence_hash,
    )
    rejected = record_greenhouse_submit_rejected_statement(
        event_id=event_id,
        owner="greenhouse-sender-1",
        lease_token=lease_token,
        error_code="GREENHOUSE_PROVIDER_VALIDATION_REJECTED",
        reason_codes=("GREENHOUSE_PROVIDER_VALIDATION_REJECTED",),
        evidence=rejected_evidence,
    )
    ambiguous = record_greenhouse_submit_ambiguity_statement(
        event_id=event_id,
        owner="greenhouse-sender-1",
        lease_token=lease_token,
        error_code="GREENHOUSE_BROKER_BOUNDARY_AMBIGUOUS",
        evidence=ambiguous_evidence,
    )
    prepost = record_greenhouse_submit_prepost_failure_statement(
        event_id=event_id,
        owner="greenhouse-sender-1",
        lease_token=lease_token,
        error_code="GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE",
    )

    assert "greenhouse_submit_record_accepted_unverified" in accepted.text
    assert "greenhouse_submit_record_rejected" in rejected.text
    assert "greenhouse_submit_record_ambiguity" in ambiguous.text
    assert "greenhouse_submit_record_prepost_failure" in prepost.text
    assert "claim_greenhouse_submit_reconciliation_jobs" not in ambiguous.text


def test_api_statements_use_owner_scoped_redacted_functions() -> None:
    event_id = uuid4()
    owner_id = uuid4()
    account_id = uuid4()
    evidence_id = uuid4()

    statements = (
        list_greenhouse_submit_accounts_statement(owner_user_id=owner_id),
        get_greenhouse_submit_account_statement(owner_user_id=owner_id, account_id=account_id),
        list_greenhouse_submit_reconciliation_cases_statement(owner_user_id=owner_id),
        get_greenhouse_submit_reconciliation_case_statement(
            owner_user_id=owner_id, event_id=event_id
        ),
        submit_greenhouse_submit_reconciliation_evidence_statement(
            owner_user_id=owner_id,
            event_id=event_id,
            evidence_source="recruiting_webhook",
            evidence_sha256="e" * 64,
            observed_status="accepted_unverified",
            observed_at=NOW,
            reason_code="GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
            idempotency_key="evidence-1",
            trace_id="trace-evidence-1",
        ),
        review_greenhouse_submit_reconciliation_evidence_statement(
            owner_user_id=owner_id,
            event_id=event_id,
            evidence_review_id=evidence_id,
            evidence_sha256="e" * 64,
            reviewed_evidence_source="recruiting_webhook",
            reviewed_observed_status="accepted_unverified",
            decision="confirmed",
            reviewed_employer_authorization_evidence_sha256="f" * 64,
            reviewed_by_user_id=owner_id,
            review_snapshot_sha256="1" * 64,
            reason="reviewed employer-side system",
            idempotency_key="review-1",
            trace_id="trace-review-1",
        ),
    )

    combined = "\n".join(statement.text for statement in statements)
    assert "FROM careerops.greenhouse_submit_accounts" not in combined
    assert "opaque_broker_handle" not in combined
    assert "submit_greenhouse_submit_reconciliation_evidence" in combined
    assert "review_greenhouse_submit_reconciliation_evidence" in combined


@pytest.mark.parametrize("evidence_source", ["gmail", "greenhouse_provider_get", "webhook"])
def test_reconciliation_evidence_rejects_non_employer_sources(evidence_source: str) -> None:
    with pytest.raises(ValueError, match="employer-side"):
        submit_greenhouse_submit_reconciliation_evidence_statement(
            owner_user_id=uuid4(),
            event_id=uuid4(),
            evidence_source=evidence_source,
            evidence_sha256="e" * 64,
            observed_status="accepted_unverified",
            observed_at=NOW,
            reason_code="GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
            idempotency_key="evidence-1",
            trace_id="trace-evidence-1",
        )


def _schema_payload() -> dict[str, object]:
    return {
        "id": 123456,
        "internal_job_id": 987654,
        "title": "Senior Agent Engineer",
        "company_name": "Example AI",
        "updated_at": "2026-07-21T08:00:00Z",
        "application_deadline": None,
        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123456",
        "questions": [
            _question("First name", True, "first_name"),
            _question("Last name", True, "last_name"),
            _question("Email", True, "email"),
            _question("Resume", True, "resume", kind="input_file"),
        ],
    }


def _question(label: str, required: bool, name: str, *, kind: str = "input_text") -> dict[str, Any]:
    return {"label": label, "required": required, "fields": [{"name": name, "type": kind}]}


def _schema() -> GreenhouseJobSchemaSnapshot:
    raw = _canonical_json_bytes(_schema_payload())
    return GreenhouseJobSchemaSnapshot.from_json_bytes(
        target=GreenhouseTarget(board_token="example", job_id=123456),
        raw_response=raw,
    )


def _payload(schema: GreenhouseJobSchemaSnapshot) -> GreenhouseSubmissionPayload:
    return GreenhouseSubmissionPayload(
        target=schema.target,
        schema_hash=schema.schema_hash,
        fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        source_draft_payload_hash="c" * 64,
        approved_material_hashes=("a" * 64,),
        attachment_refs=(
            GreenhouseAttachmentRef(
                field_name="resume",
                object_key="materials/resume.pdf",
                filename="resume.pdf",
                content_type="application/pdf",
                size_bytes=1024,
                sha256="b" * 64,
            ),
        ),
    )


def _payload_document(
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> dict[str, object]:
    return {
        "target": {
            "board_token": payload.target.board_token,
            "job_id": payload.target.job_id,
            "schema_sha256": schema.schema_hash,
        },
        "fields": dict(payload.fields),
        "source_draft_payload_hash": payload.source_draft_payload_hash,
        "approved_material_hashes": list(payload.approved_material_hashes),
        "schema_hash": schema.schema_hash,
        "payload_hash": payload.payload_hash,
        "material_hash": payload.material_hash,
        "submission_identity_sha256": payload.submission_identity,
    }


def _dispatch_row(
    schema: GreenhouseJobSchemaSnapshot,
    payload: GreenhouseSubmissionPayload,
) -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "event_key": "greenhouse-submit:reservation-1",
        "account_id": uuid4(),
        "opaque_broker_handle": "broker://greenhouse/example/profile-v1",
        "account_subject": "owner@example.com",
        "owner_user_id": OWNER_ID,
        "employer_id": "example-ai",
        "credential_profile_version": 1,
        "credential_fingerprint_sha256": "a" * 64,
        "credential_profile_status": "active",
        "credential_profile_expires_at": NOW + timedelta(days=1),
        "raw_response_sha256": schema.raw_response_sha256,
        "schema_json": _schema_payload(),
        "payload_json": _payload_document(schema, payload),
        "attachment_refs_json": [item.canonical() for item in payload.attachment_refs],
        "action_intent_id": uuid4(),
        "payload_version_id": uuid4(),
        "reservation_key": "reservation-1",
        "reconciliation_key": "reconcile-1",
        "prepare_state": "ready",
        "reason_code": None,
    }


def _evidence(
    *,
    outcome: GreenhouseSubmissionOutcome = GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED,
) -> GreenhouseSubmissionEvidence:
    schema = _schema()
    payload = _payload(schema)
    reason_code = {
        GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED: "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
        GreenhouseSubmissionOutcome.REJECTED: "GREENHOUSE_PROVIDER_VALIDATION_REJECTED",
        GreenhouseSubmissionOutcome.AMBIGUOUS: "GREENHOUSE_BROKER_BOUNDARY_AMBIGUOUS",
    }[outcome]
    status_code = 422 if outcome is GreenhouseSubmissionOutcome.REJECTED else 202
    return GreenhouseSubmissionEvidence(
        outcome=outcome,
        reason_code=reason_code,
        submission_identity=payload.submission_identity,
        reconciliation_key="reconcile-1",
        broker_request_sha256="1" * 64,
        broker_response_sha256="2" * 64,
        status_code=status_code,
        journal_receipt_hash="3" * 64,
        journal_state=GreenhouseBrokerJournalState.RESPONSE_OBSERVED,
        journal_sequence=1,
        expected_schema_hash=schema.schema_hash,
        payload_hash=payload.payload_hash,
        material_hash=payload.material_hash,
        provider_request_sha256="4" * 64,
        provider_response_sha256="5" * 64,
        observed_schema_hash=schema.schema_hash,
        observed_schema_raw_response_sha256=schema.raw_response_sha256,
    )


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
