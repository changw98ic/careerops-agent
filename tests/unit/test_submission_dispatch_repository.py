from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection

from careerops.application.application_adapters import (
    BrowserIsolationMode,
    FormFieldSpec,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationFixture,
)
from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    SyntheticReleaseQualification,
)
from careerops.application.submission_dispatch import (
    DispatchAuthority,
    DispatchDecision,
    DispatchDecisionState,
    QualifiedSyntheticDispatchRequest,
)
from careerops.infrastructure.database import submission_dispatch
from careerops.infrastructure.database.submission_dispatch import (
    PostgresSyntheticDispatchReservationStore,
    SyntheticDispatchReservationError,
    acquire_grant_reservation_lock_statement,
    compile_query_for_test,
    insert_cap_reservation_statement,
    mark_action_intent_eligible_after_reservation_statement,
    select_grant_for_reservation_statement,
    select_reservation_by_key_statement,
)
from careerops.policy.autopilot import AutopilotOutcome

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def request() -> QualifiedSyntheticDispatchRequest:
    intent_id = uuid4()
    synthetic_payload = SandboxApplicationPayload(
        action_intent_id=intent_id,
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "resume_sha256": "b" * 64,
            }
        ),
        source_draft_payload_hash="a" * 64,
        approved_material_hashes=("b" * 64,),
    )
    return QualifiedSyntheticDispatchRequest(
        authority=DispatchAuthority(
            campaign_id=uuid4(),
            grant_version_id=uuid4(),
            authorization_id=uuid4(),
            action_intent_id=intent_id,
            payload_version_id=uuid4(),
            policy_decision_id=uuid4(),
            action_kind="submit_application",
            channel="synthetic:greenhouse-sandbox",
            release_version="release-v1",
            payload_hash=synthetic_payload.payload_hash,
            target_host="sandbox.greenhouse.test",
            company_key="example-inc",
            policy_outcome=AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        ),
        fixture=SyntheticApplicationFixture(
            fixture_id="fixture-1",
            adapter_id="greenhouse-sandbox",
            allowed_host="sandbox.greenhouse.test",
            site_policy_text="Automated submissions are allowed for this synthetic sandbox.",
            allowed_fields=(
                FormFieldSpec("first_name"),
                FormFieldSpec("last_name"),
                FormFieldSpec("resume_sha256"),
            ),
        ),
        session=SandboxBrowserSession(
            session_id=uuid4(),
            action_intent_id=intent_id,
            isolation_mode=BrowserIsolationMode.PER_INTENT_CONTEXT,
            host="sandbox.greenhouse.test",
        ),
        payload=synthetic_payload,
        release_qualification=SyntheticReleaseQualification(
            adapter_id="greenhouse-sandbox",
            fixture_id="fixture-1",
            release_version="release-v1",
            stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
            evidence_hash="c" * 64,
            expires_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )


def decision() -> DispatchDecision:
    return DispatchDecision(
        DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH,
        ("SYNTHETIC_DISPATCH_QUALIFIED_FOR_RESERVATION",),
        reservation_key="b" * 64,
        outbox_event_key="synthetic-dispatch:" + "b" * 64,
        reconciliation_key="c" * 64,
    )


class TransactionalConnection:
    def in_transaction(self) -> bool:
        return True


def test_reservation_grant_precheck_stays_read_only_for_api_role() -> None:
    item = request()
    sql = compile_query_for_test(
        select_grant_for_reservation_statement(
            campaign_id=item.authority.campaign_id,
            grant_version_id=item.authority.grant_version_id,
        )
    )

    assert "FROM careerops.autopilot_grant_versions" in sql
    assert str(item.authority.campaign_id) in sql
    assert str(item.authority.grant_version_id) in sql
    assert "FOR UPDATE" not in sql


def test_reservation_uses_a_per_grant_transaction_lock_without_update_privilege() -> None:
    grant_version_id = uuid4()
    sql = compile_query_for_test(acquire_grant_reservation_lock_statement(grant_version_id))

    assert "pg_catalog.pg_advisory_xact_lock" in sql
    assert "pg_catalog.hashtextextended" in sql
    assert f"careerops:autopilot-cap-reservation:{grant_version_id}" in sql


def test_reservation_insert_is_identity_bound_and_database_timestamped() -> None:
    item = request()
    sql = compile_query_for_test(
        insert_cap_reservation_statement(
            reservation_id=uuid4(),
            decision=decision(),
            request=item,
        )
    )

    assert "INSERT INTO careerops.autopilot_cap_reservations" in sql
    assert "ON CONFLICT" not in sql
    assert "RETURNING careerops.autopilot_cap_reservations.id" in sql
    assert str(item.authority.authorization_id) in sql
    assert str(item.authority.action_intent_id) in sql
    assert str(item.authority.payload_version_id) in sql
    assert item.payload.payload_hash in sql
    assert "sandbox.greenhouse.test" in sql
    assert "synthetic:greenhouse-sandbox" in sql
    assert "release-v1" in sql
    assert "greenhouse-sandbox" in sql
    assert "fixture-1" in sql
    assert item.release_qualification.evidence_hash in sql
    assert "release_evidence_hash" in sql
    assert "release_evidence_expires_at" in sql
    assert "reservation_date" not in sql
    assert "reserved_at" not in sql


def test_existing_reservation_lookup_is_read_only_for_api_role() -> None:
    sql = compile_query_for_test(select_reservation_by_key_statement("b" * 64))

    assert "WHERE careerops.autopilot_cap_reservations.reservation_key" in sql
    assert "FOR UPDATE" not in sql
    assert "reconciliation_key" in sql
    assert "channel" in sql
    assert "release_evidence_hash" in sql
    assert "release_evidence_expires_at" in sql


def test_action_intent_is_marked_eligible_only_for_safe_current_payload_states() -> None:
    item = request()
    sql = compile_query_for_test(
        mark_action_intent_eligible_after_reservation_statement(
            action_intent_id=item.authority.action_intent_id,
            payload_version_id=item.authority.payload_version_id,
        )
    )

    assert "UPDATE careerops.action_intents" in sql
    assert "current_payload_version_id" in sql
    assert "status='eligible'" in sql.replace(" ", "")
    assert "current_payload_version_id=" in sql.replace(" ", "")
    assert str(item.authority.action_intent_id) in sql
    assert str(item.authority.payload_version_id) in sql
    assert "careerops.action_intents.status IN ('proposed', 'awaiting_approval', 'eligible')" in sql
    assert "careerops.action_intents.current_payload_version_id IS NULL" in sql
    assert "processing" not in sql
    assert "confirmed" not in sql
    assert "reconciliation_required" not in sql


def test_nonreservable_decision_cannot_build_reservation_statement() -> None:
    stopped = DispatchDecision(
        DispatchDecisionState.STOPPED,
        ("AMBIGUOUS_PROVIDER_STATE",),
    )

    with pytest.raises(SyntheticDispatchReservationError, match="reservation key"):
        insert_cap_reservation_statement(
            reservation_id=uuid4(),
            decision=stopped,
            request=request(),
        )


def test_reservation_audit_records_submission_envelope_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class RecordingAuditWriter:
        def __init__(self, connection: Connection) -> None:
            del connection

        def append(self, event: object) -> None:
            captured.append(event)

    monkeypatch.setattr(submission_dispatch, "PostgresAuditWriter", RecordingAuditWriter)
    item = request()
    item_decision = decision()
    store = PostgresSyntheticDispatchReservationStore(cast("Connection", TransactionalConnection()))

    store._append_reservation_audit(
        reservation_id=UUID("00000000-0000-0000-0000-000000000901"),
        event_id=UUID("00000000-0000-0000-0000-000000000902"),
        decision=item_decision,
        request=item,
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.event_data["mode"] == "synthetic_sandbox"  # type: ignore[union-attr]
    assert event.event_data["provider_state"] == "synthetic_worker_pending"  # type: ignore[union-attr]
    assert event.event_data["worker_state"] == "pending"  # type: ignore[union-attr]
    assert event.event_data["provider_execution"] == "not_started"  # type: ignore[union-attr]
    assert event.event_data["source_draft_payload_hash"] == "a" * 64  # type: ignore[union-attr]
    assert event.event_data["approved_material_hashes"] == ["b" * 64]  # type: ignore[union-attr]
    assert event.event_data["release_evidence_hash"] == "c" * 64  # type: ignore[union-attr]
    assert event.event_data["release_evidence_expires_at"] == (  # type: ignore[union-attr]
        item.release_qualification.expires_at.isoformat()
    )
