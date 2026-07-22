from __future__ import annotations

import runpy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from careerops.application.submission_dispatch import (
    SyntheticProviderState,
    SyntheticSubmissionReceipt,
)
from careerops.infrastructure.database.submission_outbox import (
    _dispatch_from_row,
    prepare_synthetic_submission_outbox_event_statement,
    record_synthetic_submission_outbox_ambiguity_statement,
    record_synthetic_submission_outbox_receipt_statement,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0008_add_synthetic_submission_execution.py"
)
MIGRATION_MODULE = runpy.run_path(str(MIGRATION_PATH))


def test_prepare_statement_calls_narrow_security_definer_function() -> None:
    statement = prepare_synthetic_submission_outbox_event_statement(
        event_id=uuid4(),
        owner="outbox-worker-1",
        lease_token=uuid4(),
    )

    assert statement.text == (
        "SELECT * FROM careerops.prepare_synthetic_submission_outbox_event("
        ":event_id, :lease_owner, :lease_token)"
    )
    assert set(statement._bindparams) == {"event_id", "lease_owner", "lease_token"}


def test_record_statement_calls_receipt_function_with_bound_receipt_payload() -> None:
    receipt = SyntheticSubmissionReceipt(
        provider="synthetic",
        provider_resource_id="synthetic-receipt-1",
        reconciliation_key="reconcile-1",
        provider_state=SyntheticProviderState.CONFIRMED,
        received_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    statement = record_synthetic_submission_outbox_receipt_statement(
        event_id=uuid4(),
        owner="outbox-worker-1",
        lease_token=uuid4(),
        receipt=receipt,
        receipt_id=uuid4(),
    )

    assert statement.text == (
        "SELECT careerops.record_synthetic_submission_outbox_receipt("
        ":event_id, :lease_owner, :lease_token, :provider, :provider_resource_id, "
        ":reconciliation_key, :provider_state, :received_at, :receipt_id)"
    )
    assert statement._bindparams["provider"].value == "synthetic"
    assert statement._bindparams["provider_state"].value == "confirmed"
    assert statement._bindparams["reconciliation_key"].value == "reconcile-1"


def test_record_ambiguity_statement_does_not_fabricate_a_receipt() -> None:
    statement = record_synthetic_submission_outbox_ambiguity_statement(
        event_id=uuid4(),
        owner="outbox-worker-1",
        lease_token=uuid4(),
        error_code="SYNTHETIC_PROVIDER_EXCEPTION",
    )

    assert statement.text == (
        "SELECT careerops.record_synthetic_submission_outbox_ambiguity("
        ":event_id, :lease_owner, :lease_token, :error_code)"
    )
    assert "provider_resource_id" not in statement.text
    assert statement._bindparams["error_code"].value == "SYNTHETIC_PROVIDER_EXCEPTION"


def test_dispatch_row_maps_confirmed_receipt_for_replay_without_new_attempt() -> None:
    event_id = uuid4()
    intent_id = uuid4()
    payload_id = uuid4()
    received_at = datetime(2026, 7, 20, 9, 1, tzinfo=UTC)
    row = {
        "event_id": event_id,
        "event_key": "synthetic-dispatch:reservation-1",
        "action_intent_id": intent_id,
        "payload_version_id": payload_id,
        "reservation_key": "reservation-1",
        "reconciliation_key": "reconcile-1",
        "existing_provider": "synthetic",
        "existing_provider_resource_id": "synthetic-receipt-1",
        "existing_provider_state": "confirmed",
        "existing_received_at": received_at,
        "prepare_state": "already_confirmed",
        "reason_code": None,
    }

    dispatch = _dispatch_from_row(row)  # type: ignore[arg-type]

    assert dispatch.event_id == event_id
    assert dispatch.action_intent_id == intent_id
    assert dispatch.payload_version_id == payload_id
    assert dispatch.reservation_key == "reservation-1"
    assert dispatch.confirmed_receipt is not None
    assert dispatch.confirmed_receipt.provider_state is SyntheticProviderState.CONFIRMED
    assert dispatch.confirmed_receipt.received_at == received_at


def test_migration_contract_hardens_execution_boundary() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert MIGRATION_MODULE["revision"] == "0008"
    assert MIGRATION_MODULE["down_revision"] == "0007"
    assert MIGRATION_MODULE["APPEND_ONLY_TABLES"] == ("autopilot_kill_switch_events",)
    assert "scope_type IN ('global', 'campaign', 'provider')" in source
    assert "idempotency_key" in source
    assert "release_evidence_hash IS NULL" in source
    assert "NEW.release_evidence_hash IS NULL" in source
    assert "NEW.release_evidence_expires_at <= CURRENT_TIMESTAMP" in source
    assert "(release_evidence_hash IS NULL) = (release_evidence_expires_at IS NULL)" in source
    assert "ORDER BY sequence DESC" in source
    assert "receipt.reconciliation_key = v_reservation.reconciliation_key" in source
    assert "v_receipt.provider::text" in source
    assert "v_receipt.final_state::text" in source
    assert "pg_advisory_xact_lock_shared(" in source
    assert "trg_autopilot_kill_switch_events_serialize_insert" in source
    assert (
        "GRANT INSERT (id, scope_type, campaign_id, provider, active, reason, actor_user_id"
        in source
    )
    assert "SET status = 'eligible'" in source
    assert "current_payload_version_id = NEW.payload_version_id" in source
    assert "v_intent_status IS DISTINCT FROM 'eligible'" in source
    assert "'stopped'::text" in source
    assert "INSERT INTO careerops.side_effect_attempts" in source
    assert "INSERT INTO careerops.provider_receipts" in source
    assert "record_synthetic_submission_outbox_ambiguity" in source
    assert "final_state,\n                    provider_timestamp" in source
    assert "'ambiguous'" in source
    assert "careerops.append_audit_event" in source
    assert "synthetic_submission_reconciliation_required" in source
    assert "GRANT EXECUTE ON FUNCTION careerops.prepare_synthetic_submission_outbox_event" in (
        "\n".join(MIGRATION_MODULE["_OUTBOX_GRANTS"])
    )
    assert "GRANT EXECUTE ON FUNCTION careerops.record_synthetic_submission_outbox_receipt" in (
        "\n".join(MIGRATION_MODULE["_OUTBOX_GRANTS"])
    )
    assert "GRANT INSERT ON careerops.provider_receipts TO careerops_outbox" not in source
    assert "GRANT INSERT ON careerops.side_effect_attempts TO careerops_outbox" not in source
