"""PostgreSQL persistence for human-only real-application handoffs.

This adapter stores immutable handoff packets and user attestations. It intentionally has no
outbox, provider, browser, credential, or side-effect-worker dependency: the final application
submission remains a user action outside CareerOps.
"""

from __future__ import annotations

import re
from typing import cast
from uuid import UUID, uuid4, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.base import Executable

from careerops.application.application_handoff import (
    ManualApplicationHandoff,
    ManualApplicationHandoffRef,
    ManualSubmissionAttestation,
    ManualSubmissionRecord,
)
from careerops.application.application_prep import materialize_json_mapping
from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.infrastructure.database.audit import PostgresAuditWriter
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    application_events,
)

_ACTOR = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_TRACE = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_ACTION_KIND = "create_manual_application_handoff"
_EVENT_NAMESPACE = UUID("3ef971a8-1c33-4b0a-9c3e-9a5a69b890eb")


class ManualApplicationHandoffPersistenceError(RuntimeError):
    pass


class PostgresManualApplicationHandoffStore:
    """Persist an evidence-bound handoff and append a user-only submission attestation.

    The caller must hold one transaction. A stable idempotency key converges retries of handoff
    creation. An attestation uses a deterministic application-event id, so retrying the exact
    same user record cannot create a duplicate event. Neither operation creates an outbox event or
    marks a provider-side success.
    """

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError(
                "manual application handoff persistence requires an explicit transaction"
            )
        self._connection = connection

    def save_handoff(
        self,
        handoff: ManualApplicationHandoff,
        *,
        created_by: str,
        trace_id: str,
    ) -> ManualApplicationHandoffRef:
        _validate_actor(created_by, "created_by")
        _validate_trace(trace_id)
        intent_id = uuid4()
        inserted_id = self._connection.scalar(
            insert_manual_handoff_intent_statement(
                intent_id=intent_id,
                handoff=handoff,
                created_by=created_by,
            )
        )
        if isinstance(inserted_id, UUID):
            payload_version_id = uuid4()
            self._connection.execute(
                insert_manual_handoff_payload_statement(
                    payload_version_id=payload_version_id,
                    intent_id=inserted_id,
                    handoff=handoff,
                )
            )
            self._connection.execute(
                bind_manual_handoff_payload_statement(
                    intent_id=inserted_id,
                    payload_version_id=payload_version_id,
                )
            )
            reference = _handoff_ref(
                action_intent_id=inserted_id,
                payload_version_id=payload_version_id,
                handoff=handoff,
            )
            self._insert_handoff_prepared_event(reference=reference, handoff=handoff)
            self._append_handoff_prepared_audit(
                reference=reference,
                handoff=handoff,
                created_by=created_by,
                trace_id=trace_id,
            )
            return reference

        existing = (
            self._connection.execute(
                select_manual_handoff_by_key_statement(handoff.idempotency_key)
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise ManualApplicationHandoffPersistenceError(
                "handoff idempotency conflict did not yield an existing handoff"
            )
        _assert_matching_handoff(existing, handoff)
        return _handoff_ref(
            action_intent_id=cast("UUID", existing["intent_id"]),
            payload_version_id=cast("UUID", existing["payload_version_id"]),
            handoff=handoff,
        )

    def record_manual_submission(
        self,
        reference: ManualApplicationHandoffRef,
        attestation: ManualSubmissionAttestation,
        *,
        recorded_by: str,
        trace_id: str,
    ) -> ManualSubmissionRecord:
        _validate_actor(recorded_by, "recorded_by")
        _validate_trace(trace_id)
        existing_handoff = (
            self._connection.execute(
                select_manual_handoff_by_intent_statement(reference.action_intent_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing_handoff is None:
            raise ManualApplicationHandoffPersistenceError("manual handoff is missing")
        _assert_matching_handoff_reference(existing_handoff, reference)
        event_id = _manual_submission_event_id(reference, attestation)
        inserted_id = self._connection.scalar(
            insert_manual_submission_event_statement(
                event_id=event_id,
                reference=reference,
                attestation=attestation,
            )
        )
        if isinstance(inserted_id, UUID):
            self._append_manual_submission_audit(
                reference=reference,
                attestation=attestation,
                recorded_by=recorded_by,
                trace_id=trace_id,
            )
            return ManualSubmissionRecord(application_event_id=inserted_id, newly_created=True)

        existing_event = (
            self._connection.execute(select_application_event_statement(event_id))
            .mappings()
            .one_or_none()
        )
        if existing_event is None:
            raise ManualApplicationHandoffPersistenceError(
                "manual submission idempotency conflict did not yield an existing event"
            )
        _assert_matching_manual_submission(existing_event, reference, attestation)
        return ManualSubmissionRecord(application_event_id=event_id, newly_created=False)

    def _insert_handoff_prepared_event(
        self,
        *,
        reference: ManualApplicationHandoffRef,
        handoff: ManualApplicationHandoff,
    ) -> None:
        self._connection.execute(
            sa.insert(application_events).values(
                id=uuid4(),
                candidate_id=reference.candidate_id,
                canonical_job_id=reference.canonical_job_id,
                job_posting_id=reference.job_posting_id,
                event_type="manual_handoff_prepared",
                source_type="careerops",
                source_id=str(reference.action_intent_id),
                occurred_at=handoff.prepared_at,
                event_data={
                    "mode": "human_final_submission",
                    "provider_execution": "disabled",
                    "payload_hash": reference.payload_hash,
                    "source_draft_payload_hash": handoff.source_draft_payload_hash,
                    "submission_url": handoff.target_url,
                },
            )
        )

    def _append_handoff_prepared_audit(
        self,
        *,
        reference: ManualApplicationHandoffRef,
        handoff: ManualApplicationHandoff,
        created_by: str,
        trace_id: str,
    ) -> None:
        PostgresAuditWriter(self._connection).append(
            AuditEventDraft(
                event_type="manual_application_handoff_prepared",
                actor_type=AuditActorType.USER,
                actor_id=created_by,
                resource_type="action_intent",
                resource_id=reference.action_intent_id,
                trace_id=trace_id,
                event_data={
                    "mode": "human_final_submission",
                    "provider_execution": "disabled",
                    "payload_version_id": str(reference.payload_version_id),
                    "payload_hash": reference.payload_hash,
                    "source_draft_payload_hash": handoff.source_draft_payload_hash,
                    "submission_url": handoff.target_url,
                },
            )
        )

    def _append_manual_submission_audit(
        self,
        *,
        reference: ManualApplicationHandoffRef,
        attestation: ManualSubmissionAttestation,
        recorded_by: str,
        trace_id: str,
    ) -> None:
        PostgresAuditWriter(self._connection).append(
            AuditEventDraft(
                event_type="manual_application_submission_recorded",
                actor_type=AuditActorType.USER,
                actor_id=recorded_by,
                resource_type="action_intent",
                resource_id=reference.action_intent_id,
                trace_id=trace_id,
                event_data={
                    "mode": "human_final_submission",
                    "provider_receipt_verified": False,
                    "attestation_only": True,
                    "payload_version_id": str(reference.payload_version_id),
                    "payload_hash": reference.payload_hash,
                    "receipt_reference": attestation.receipt_reference,
                    "submitted_at": attestation.submitted_at.isoformat(),
                },
            )
        )


def insert_manual_handoff_intent_statement(
    *,
    intent_id: UUID,
    handoff: ManualApplicationHandoff,
    created_by: str,
) -> Executable:
    return (
        postgresql.insert(action_intents)
        .values(
            id=intent_id,
            action_kind=_ACTION_KIND,
            resource_type="canonical_job",
            resource_id=handoff.canonical_job_id,
            idempotency_key=handoff.idempotency_key,
            status="proposed",
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=[action_intents.c.idempotency_key])
        .returning(action_intents.c.id)
    )


def insert_manual_handoff_payload_statement(
    *,
    payload_version_id: UUID,
    intent_id: UUID,
    handoff: ManualApplicationHandoff,
) -> sa.Insert:
    return sa.insert(action_payload_versions).values(
        id=payload_version_id,
        action_intent_id=intent_id,
        version=1,
        target=materialize_json_mapping(handoff.target),
        payload=materialize_json_mapping(handoff.payload),
        attachment_refs=[materialize_json_mapping(item) for item in handoff.attachment_refs],
        payload_hash=handoff.payload_hash,
    )


def bind_manual_handoff_payload_statement(
    *,
    intent_id: UUID,
    payload_version_id: UUID,
) -> sa.Update:
    return (
        sa.update(action_intents)
        .where(action_intents.c.id == intent_id)
        .values(current_payload_version_id=payload_version_id)
    )


def select_manual_handoff_by_key_statement(idempotency_key: str) -> sa.Select[tuple[object, ...]]:
    return _select_manual_handoff().where(action_intents.c.idempotency_key == idempotency_key)


def select_manual_handoff_by_intent_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return _select_manual_handoff().where(action_intents.c.id == intent_id)


def _select_manual_handoff() -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            action_intents.c.id.label("intent_id"),
            action_intents.c.action_kind,
            action_intents.c.resource_type,
            action_intents.c.resource_id,
            action_intents.c.idempotency_key,
            action_payload_versions.c.id.label("payload_version_id"),
            action_payload_versions.c.target,
            action_payload_versions.c.payload,
            action_payload_versions.c.attachment_refs,
            action_payload_versions.c.payload_hash,
        )
        .select_from(
            action_intents.join(
                action_payload_versions,
                sa.and_(
                    action_payload_versions.c.action_intent_id == action_intents.c.id,
                    action_payload_versions.c.id == action_intents.c.current_payload_version_id,
                ),
            )
        )
        .where(action_intents.c.action_kind == _ACTION_KIND)
        .with_for_update(of=action_intents)
    )


def insert_manual_submission_event_statement(
    *,
    event_id: UUID,
    reference: ManualApplicationHandoffRef,
    attestation: ManualSubmissionAttestation,
) -> Executable:
    return (
        postgresql.insert(application_events)
        .values(
            id=event_id,
            candidate_id=reference.candidate_id,
            canonical_job_id=reference.canonical_job_id,
            job_posting_id=reference.job_posting_id,
            event_type="manual_submission_recorded",
            source_type="user_attestation",
            source_id=str(reference.action_intent_id),
            occurred_at=attestation.submitted_at,
            event_data={
                "mode": "human_final_submission",
                "provider_receipt_verified": False,
                "payload_hash": reference.payload_hash,
                "receipt_reference": attestation.receipt_reference,
            },
        )
        .on_conflict_do_nothing(index_elements=[application_events.c.id])
        .returning(application_events.c.id)
    )


def select_application_event_statement(event_id: UUID) -> sa.Select[tuple[object, ...]]:
    return sa.select(
        application_events.c.id,
        application_events.c.candidate_id,
        application_events.c.canonical_job_id,
        application_events.c.job_posting_id,
        application_events.c.event_type,
        application_events.c.source_type,
        application_events.c.source_id,
        application_events.c.occurred_at,
        application_events.c.event_data,
    ).where(application_events.c.id == event_id)


def _handoff_ref(
    *,
    action_intent_id: UUID,
    payload_version_id: UUID,
    handoff: ManualApplicationHandoff,
) -> ManualApplicationHandoffRef:
    return ManualApplicationHandoffRef(
        action_intent_id=action_intent_id,
        payload_version_id=payload_version_id,
        payload_hash=handoff.payload_hash,
        candidate_id=handoff.candidate_id,
        canonical_job_id=handoff.canonical_job_id,
        job_posting_id=handoff.job_posting_id,
    )


def _manual_submission_event_id(
    reference: ManualApplicationHandoffRef,
    attestation: ManualSubmissionAttestation,
) -> UUID:
    return uuid5(
        _EVENT_NAMESPACE,
        ":".join(
            (
                str(reference.action_intent_id),
                str(reference.payload_version_id),
                reference.payload_hash,
                attestation.receipt_reference,
                attestation.submitted_at.isoformat(),
            )
        ),
    )


def _assert_matching_handoff(existing: RowMapping, handoff: ManualApplicationHandoff) -> None:
    expected = {
        "action_kind": _ACTION_KIND,
        "resource_type": "canonical_job",
        "resource_id": handoff.canonical_job_id,
        "idempotency_key": handoff.idempotency_key,
        "target": materialize_json_mapping(handoff.target),
        "payload": materialize_json_mapping(handoff.payload),
        "attachment_refs": [materialize_json_mapping(item) for item in handoff.attachment_refs],
        "payload_hash": handoff.payload_hash,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise ManualApplicationHandoffPersistenceError(
            "handoff idempotency key is already bound to a different immutable handoff"
        )


def _assert_matching_handoff_reference(
    existing: RowMapping,
    reference: ManualApplicationHandoffRef,
) -> None:
    target_raw = existing["target"]
    if not isinstance(target_raw, dict):
        raise ManualApplicationHandoffPersistenceError("stored manual handoff target is invalid")
    target = cast("dict[str, object]", target_raw)
    expected = {
        "intent_id": reference.action_intent_id,
        "payload_version_id": reference.payload_version_id,
        "payload_hash": reference.payload_hash,
        "resource_id": reference.canonical_job_id,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise ManualApplicationHandoffPersistenceError(
            "manual handoff reference is stale or mismatched"
        )
    if (
        target.get("candidate_id") != str(reference.candidate_id)
        or target.get("canonical_job_id") != str(reference.canonical_job_id)
        or target.get("job_posting_id") != str(reference.job_posting_id)
    ):
        raise ManualApplicationHandoffPersistenceError(
            "manual handoff target identity is mismatched"
        )


def _assert_matching_manual_submission(
    existing: RowMapping,
    reference: ManualApplicationHandoffRef,
    attestation: ManualSubmissionAttestation,
) -> None:
    expected_event_data = {
        "mode": "human_final_submission",
        "provider_receipt_verified": False,
        "payload_hash": reference.payload_hash,
        "receipt_reference": attestation.receipt_reference,
    }
    expected = {
        "candidate_id": reference.candidate_id,
        "canonical_job_id": reference.canonical_job_id,
        "job_posting_id": reference.job_posting_id,
        "event_type": "manual_submission_recorded",
        "source_type": "user_attestation",
        "source_id": str(reference.action_intent_id),
        "occurred_at": attestation.submitted_at,
        "event_data": expected_event_data,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise ManualApplicationHandoffPersistenceError(
            "manual submission event id is already bound to a different attestation"
        )


def _validate_actor(value: str, label: str) -> None:
    if not _ACTOR.fullmatch(value):
        raise ValueError(f"{label} must be a bounded actor identifier")


def _validate_trace(value: str) -> None:
    if not _TRACE.fullmatch(value):
        raise ValueError("trace_id must be a bounded identifier")


def compile_query_for_test(statement: sa.ClauseElement | Executable) -> str:
    """Render PostgreSQL SQL deterministically without opening a database."""

    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "ManualApplicationHandoffPersistenceError",
    "PostgresManualApplicationHandoffStore",
    "bind_manual_handoff_payload_statement",
    "compile_query_for_test",
    "insert_manual_handoff_intent_statement",
    "insert_manual_handoff_payload_statement",
    "insert_manual_submission_event_statement",
    "select_application_event_statement",
    "select_manual_handoff_by_intent_statement",
    "select_manual_handoff_by_key_statement",
]
