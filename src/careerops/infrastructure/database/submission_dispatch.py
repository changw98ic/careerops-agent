from __future__ import annotations

import re
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.dml import Insert

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.application.outbox import OutboxEventType, PendingOutboxEvent
from careerops.application.submission_dispatch import (
    DispatchDecision,
    DispatchDecisionState,
    QualifiedSyntheticDispatchRequest,
)
from careerops.infrastructure.database.audit import PostgresAuditWriter
from careerops.infrastructure.database.outbox import PostgresOutboxRepository
from careerops.infrastructure.database.schema import (
    action_intents,
    autopilot_cap_reservations,
    autopilot_grant_versions,
)

_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")


class SyntheticDispatchReservationError(RuntimeError):
    pass


class PostgresSyntheticDispatchReservationStore:
    """Reserve one cap slot and one internal synthetic workflow event atomically.

    A retry locks the grant version, reads the existing reservation before any new insert, and
    reuses the matching outbox event. The reservation trigger independently rechecks immutable
    authorization, scope, material, cap, revocation, and database-time constraints. No provider
    write, browser, credential, or worker execution happens in this store.
    """

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("synthetic dispatch reservation requires an explicit transaction")
        self._connection = connection

    def reserve_and_enqueue(
        self,
        decision: DispatchDecision,
        request: QualifiedSyntheticDispatchRequest,
    ) -> UUID:
        if decision.state is not DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH:
            raise SyntheticDispatchReservationError("dispatch decision is not reservable")
        if (
            decision.reservation_key is None
            or decision.outbox_event_key is None
            or decision.reconciliation_key is None
        ):
            raise SyntheticDispatchReservationError("dispatch decision is missing idempotency keys")
        if not _EVENT_KEY.fullmatch(decision.outbox_event_key):
            raise SyntheticDispatchReservationError("outbox event key is not a bounded identifier")

        self._connection.execute(
            acquire_grant_reservation_lock_statement(request.authority.grant_version_id)
        )
        grant_id = self._connection.scalar(
            select_grant_for_reservation_statement(
                campaign_id=request.authority.campaign_id,
                grant_version_id=request.authority.grant_version_id,
            )
        )
        if not isinstance(grant_id, UUID):
            raise SyntheticDispatchReservationError("dispatch grant is missing")

        existing = self._find_existing_reservation(decision.reservation_key)
        created = existing is None
        if existing is None:
            reservation_id = uuid4()
            inserted_id = self._connection.scalar(
                insert_cap_reservation_statement(
                    reservation_id=reservation_id,
                    decision=decision,
                    request=request,
                )
            )
            if not isinstance(inserted_id, UUID):
                raise RuntimeError("cap reservation insert did not return an id")
            reservation_id = inserted_id
        else:
            _assert_matching_reservation(existing, decision=decision, request=request)
            reservation_id = cast("UUID", existing["id"])

        available_at = self._database_now()
        event_id = PostgresOutboxRepository(self._connection).enqueue_once(
            PendingOutboxEvent(
                event_id=uuid4(),
                event_key=decision.outbox_event_key,
                action_intent_id=request.authority.action_intent_id,
                payload_version_id=request.authority.payload_version_id,
                event_type=OutboxEventType.WORKFLOW_SIGNAL,
                available_at=available_at,
            )
        )
        if created:
            updated = self._connection.execute(
                mark_action_intent_eligible_after_reservation_statement(
                    action_intent_id=request.authority.action_intent_id,
                    payload_version_id=request.authority.payload_version_id,
                )
            )
            if updated.rowcount != 1:
                raise SyntheticDispatchReservationError(
                    "qualified reservation could not mark action intent eligible"
                )
            self._append_reservation_audit(
                reservation_id=reservation_id,
                event_id=event_id,
                decision=decision,
                request=request,
            )
        return reservation_id

    def _find_existing_reservation(self, reservation_key: str) -> RowMapping | None:
        return (
            self._connection.execute(select_reservation_by_key_statement(reservation_key))
            .mappings()
            .one_or_none()
        )

    def _database_now(self) -> datetime:
        now = self._connection.scalar(sa.select(sa.func.current_timestamp()))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("database did not return a timezone-aware current timestamp")
        return now

    def _append_reservation_audit(
        self,
        *,
        reservation_id: UUID,
        event_id: UUID,
        decision: DispatchDecision,
        request: QualifiedSyntheticDispatchRequest,
    ) -> None:
        reconciliation_key = decision.reconciliation_key
        reservation_key = decision.reservation_key
        if reconciliation_key is None or reservation_key is None:
            raise SyntheticDispatchReservationError(
                "synthetic reservation audit requires idempotency keys"
            )
        PostgresAuditWriter(self._connection).append(
            AuditEventDraft(
                event_type="synthetic_dispatch_reserved",
                actor_type=AuditActorType.POLICY,
                actor_id=str(request.authority.policy_decision_id),
                resource_type="action_intent",
                resource_id=request.authority.action_intent_id,
                trace_id=reservation_key,
                event_data={
                    "mode": "synthetic_sandbox",
                    "provider_state": "synthetic_worker_pending",
                    "worker_state": "pending",
                    "provider_execution": "not_started",
                    "reservation_id": str(reservation_id),
                    "campaign_id": str(request.authority.campaign_id),
                    "grant_version_id": str(request.authority.grant_version_id),
                    "authorization_id": str(request.authority.authorization_id),
                    "payload_version_id": str(request.authority.payload_version_id),
                    "payload_hash": request.authority.payload_hash,
                    "source_draft_payload_hash": request.payload.source_draft_payload_hash,
                    "approved_material_hashes": list(request.payload.approved_material_hashes),
                    "channel": request.authority.channel,
                    "release_version": request.authority.release_version,
                    "target_host": request.authority.target_host,
                    "adapter_id": request.fixture.adapter_id,
                    "fixture_id": request.fixture.fixture_id,
                    "release_evidence_hash": request.release_qualification.evidence_hash,
                    "release_evidence_expires_at": (
                        request.release_qualification.expires_at.isoformat()
                    ),
                    "outbox_event_id": str(event_id),
                    "reconciliation_key": reconciliation_key,
                },
            )
        )


def select_grant_for_reservation_statement(
    *,
    campaign_id: UUID,
    grant_version_id: UUID,
) -> sa.Select[tuple[UUID]]:
    # This is only an early existence check. The SECURITY DEFINER reservation
    # trigger owns the authoritative FOR UPDATE lock, cap check, and revocation
    # check. Keeping the application query read-only preserves least privilege
    # for careerops_api while the trigger still serializes new reservations.
    return sa.select(autopilot_grant_versions.c.id).where(
        autopilot_grant_versions.c.campaign_id == campaign_id,
        autopilot_grant_versions.c.id == grant_version_id,
    )


def acquire_grant_reservation_lock_statement(grant_version_id: UUID) -> Executable:
    """Serialize reservation create/replay without granting row UPDATE authority."""

    return sa.text(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:lock_key, 0))"
    ).bindparams(lock_key=f"careerops:autopilot-cap-reservation:{grant_version_id}")


def select_reservation_by_key_statement(reservation_key: str) -> sa.Select[tuple[object, ...]]:
    return sa.select(
        autopilot_cap_reservations.c.id,
        autopilot_cap_reservations.c.campaign_id,
        autopilot_cap_reservations.c.grant_version_id,
        autopilot_cap_reservations.c.authorization_id,
        autopilot_cap_reservations.c.action_intent_id,
        autopilot_cap_reservations.c.payload_version_id,
        autopilot_cap_reservations.c.payload_hash,
        autopilot_cap_reservations.c.target_host,
        autopilot_cap_reservations.c.channel,
        autopilot_cap_reservations.c.release_version,
        autopilot_cap_reservations.c.company_key,
        autopilot_cap_reservations.c.adapter_id,
        autopilot_cap_reservations.c.fixture_id,
        autopilot_cap_reservations.c.reconciliation_key,
        autopilot_cap_reservations.c.release_evidence_hash,
        autopilot_cap_reservations.c.release_evidence_expires_at,
    ).where(autopilot_cap_reservations.c.reservation_key == reservation_key)


def insert_cap_reservation_statement(
    *,
    reservation_id: UUID,
    decision: DispatchDecision,
    request: QualifiedSyntheticDispatchRequest,
) -> Insert:
    if decision.reservation_key is None or decision.reconciliation_key is None:
        raise SyntheticDispatchReservationError("dispatch decision is missing reservation key")
    authority = request.authority
    values: dict[str, object] = {
        "id": reservation_id,
        "campaign_id": authority.campaign_id,
        "grant_version_id": authority.grant_version_id,
        "authorization_id": authority.authorization_id,
        "action_intent_id": authority.action_intent_id,
        "payload_version_id": authority.payload_version_id,
        "payload_hash": authority.payload_hash,
        "target_host": authority.target_host,
        "channel": authority.channel,
        "release_version": authority.release_version,
        "company_key": authority.company_key,
        "adapter_id": request.fixture.adapter_id,
        "fixture_id": request.fixture.fixture_id,
        "reservation_key": decision.reservation_key,
        "reconciliation_key": decision.reconciliation_key,
        "release_evidence_hash": request.release_qualification.evidence_hash,
        "release_evidence_expires_at": request.release_qualification.expires_at,
    }
    return (
        sa.insert(autopilot_cap_reservations)
        .values(**values)
        .returning(autopilot_cap_reservations.c.id)
    )


def mark_action_intent_eligible_after_reservation_statement(
    *,
    action_intent_id: UUID,
    payload_version_id: UUID,
) -> Executable:
    return (
        sa.update(action_intents)
        .where(
            action_intents.c.id == action_intent_id,
            action_intents.c.status.in_(("proposed", "awaiting_approval", "eligible")),
            sa.or_(
                action_intents.c.current_payload_version_id.is_(None),
                action_intents.c.current_payload_version_id == payload_version_id,
            ),
        )
        .values(
            current_payload_version_id=payload_version_id,
            status="eligible",
            updated_at=sa.func.now(),
        )
    )


def _assert_matching_reservation(
    existing: RowMapping,
    *,
    decision: DispatchDecision,
    request: QualifiedSyntheticDispatchRequest,
) -> None:
    authority = request.authority
    expected: dict[str, object] = {
        "campaign_id": authority.campaign_id,
        "grant_version_id": authority.grant_version_id,
        "authorization_id": authority.authorization_id,
        "action_intent_id": authority.action_intent_id,
        "payload_version_id": authority.payload_version_id,
        "payload_hash": authority.payload_hash,
        "target_host": authority.target_host,
        "channel": authority.channel,
        "release_version": authority.release_version,
        "company_key": authority.company_key,
        "adapter_id": request.fixture.adapter_id,
        "fixture_id": request.fixture.fixture_id,
        "reconciliation_key": decision.reconciliation_key,
        "release_evidence_hash": request.release_qualification.evidence_hash,
        "release_evidence_expires_at": request.release_qualification.expires_at,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise SyntheticDispatchReservationError(
            "reservation key is already bound to a different dispatch identity"
        )


def compile_query_for_test(statement: sa.ClauseElement | Executable) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "PostgresSyntheticDispatchReservationStore",
    "SyntheticDispatchReservationError",
    "acquire_grant_reservation_lock_statement",
    "compile_query_for_test",
    "insert_cap_reservation_statement",
    "mark_action_intent_eligible_after_reservation_statement",
    "select_grant_for_reservation_statement",
    "select_reservation_by_key_statement",
]
