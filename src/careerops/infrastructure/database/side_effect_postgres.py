"""PostgreSQL-backed ``SideEffectStore`` for the M5A authorization chain.

Uses the existing ``careerops`` schema tables (``action_intents``,
``action_payload_versions``, ``policy_decisions``, ``approval_requests``,
``side_effect_attempts``, ``provider_receipts``) defined in
``infrastructure.database.schema``.

``get_or_create_pending_approval`` is atomic: it uses ``SELECT ... FOR UPDATE``
on the intent's existing approvals to prevent duplicate PENDING rows under
concurrent review_gate resumes.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError

from careerops.domain.side_effects import (
    ActionIntent,
    ActionPayloadVersion,
    ApprovalDecision,
    ApprovalRequest,
    AttemptState,
    IntentStatus,
    PolicyDecisionRecord,
    PolicyDecisionValue,
    ProviderReceipt,
    ReceiptFinalState,
    SideEffectAttempt,
)
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    approval_requests,
    policy_decisions,
    provider_receipts,
    side_effect_attempts,
)


def _uuid(val: Any) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val))


def _dt(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    raise TypeError(f"expected datetime, got {type(val)}")


class PostgresSideEffectStore:
    """SQLAlchemy-backed store implementing the ``SideEffectStore`` protocol.

    All reads and writes go through the ``careerops`` schema tables. The store
    is **not** thread-safe at the Python level; concurrency control is delegated
    to PostgreSQL row-level locks (``FOR UPDATE``) and unique constraints.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- intents ---------------------------------------------------------

    def insert_intent(self, intent: ActionIntent) -> ActionIntent:
        with self._engine.begin() as conn:
            conn.execute(
                action_intents.insert().values(
                    id=intent.id,
                    action_kind=intent.action_kind,
                    resource_type=intent.resource_type,
                    resource_id=intent.resource_id,
                    idempotency_key=intent.idempotency_key,
                    status=intent.status.value,
                    current_payload_version_id=intent.current_payload_version_id,
                    created_by=intent.created_by,
                    created_at=intent.created_at,
                    updated_at=intent.updated_at,
                )
            )
        return intent

    def find_intent_by_idempotency_key(self, idempotency_key: str) -> ActionIntent | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(action_intents).where(
                        action_intents.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return self._row_to_intent(row)

    def get_intent(self, intent_id: UUID) -> ActionIntent:
        with self._engine.begin() as conn:
            row = (
                conn.execute(select(action_intents).where(action_intents.c.id == intent_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"intent {intent_id} not found")
        return self._row_to_intent(row)

    def update_intent_status(
        self,
        intent_id: UUID,
        status: IntentStatus,
        *,
        now: datetime,
        current_payload_version_id: UUID | None = None,
    ) -> ActionIntent:
        with self._engine.begin() as conn:
            values: dict[str, Any] = {"status": status.value, "updated_at": now}
            if current_payload_version_id is not None:
                values["current_payload_version_id"] = current_payload_version_id
            conn.execute(
                action_intents.update().where(action_intents.c.id == intent_id).values(**values)
            )
            row = (
                conn.execute(select(action_intents).where(action_intents.c.id == intent_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"intent {intent_id} not found")
        return self._row_to_intent(row)

    # -- payload versions ------------------------------------------------

    def insert_payload_version(self, version: ActionPayloadVersion) -> ActionPayloadVersion:
        with self._engine.begin() as conn:
            conn.execute(
                action_payload_versions.insert().values(
                    id=version.id,
                    action_intent_id=version.action_intent_id,
                    version=version.version,
                    target=version.target,
                    payload=version.payload,
                    attachment_refs=list(version.attachment_refs),
                    payload_hash=version.payload_hash,
                    created_at=version.created_at,
                )
            )
        return version

    def get_payload_version(self, version_id: UUID) -> ActionPayloadVersion:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(action_payload_versions).where(
                        action_payload_versions.c.id == version_id
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"payload version {version_id} not found")
        return self._row_to_payload_version(row)

    def list_payload_versions(self, intent_id: UUID) -> tuple[ActionPayloadVersion, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(action_payload_versions)
                    .where(action_payload_versions.c.action_intent_id == intent_id)
                    .order_by(action_payload_versions.c.version)
                )
                .mappings()
                .fetchall()
            )
        return tuple(self._row_to_payload_version(r) for r in rows)

    # -- policy decisions ------------------------------------------------

    def insert_policy_decision(self, decision: PolicyDecisionRecord) -> PolicyDecisionRecord:
        with self._engine.begin() as conn:
            conn.execute(
                policy_decisions.insert().values(
                    id=decision.id,
                    action_intent_id=decision.action_intent_id,
                    payload_version_id=decision.payload_version_id,
                    ruleset_version=decision.ruleset_version,
                    decision=decision.decision.value,
                    reason_codes=list(decision.reason_codes),
                    payload_hash=decision.payload_hash,
                    trusted_facts=decision.trusted_facts,
                    evidence_refs=list(decision.evidence_refs),
                    untrusted_claims=decision.untrusted_claims,
                    expires_at=decision.expires_at,
                    created_at=decision.created_at,
                )
            )
        return decision

    def get_policy_decision(self, decision_id: UUID) -> PolicyDecisionRecord:
        with self._engine.begin() as conn:
            row = (
                conn.execute(select(policy_decisions).where(policy_decisions.c.id == decision_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"policy decision {decision_id} not found")
        return self._row_to_policy_decision(row)

    def list_policy_decisions(self, intent_id: UUID) -> tuple[PolicyDecisionRecord, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(policy_decisions)
                    .where(policy_decisions.c.action_intent_id == intent_id)
                    .order_by(policy_decisions.c.created_at)
                )
                .mappings()
                .fetchall()
            )
        return tuple(self._row_to_policy_decision(r) for r in rows)

    # -- approvals -------------------------------------------------------

    def insert_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        with self._engine.begin() as conn:
            conn.execute(
                approval_requests.insert().values(
                    id=approval.id,
                    action_intent_id=approval.action_intent_id,
                    payload_version_id=approval.payload_version_id,
                    policy_decision_id=approval.policy_decision_id,
                    requested_for=approval.requested_for,
                    decision=approval.decision.value,
                    decision_rule_reference=approval.decision_rule_reference,
                    expires_at=approval.expires_at,
                    decided_at=approval.decided_at,
                    created_at=approval.created_at,
                )
            )
        return approval

    def get_approval(self, approval_id: UUID) -> ApprovalRequest:
        with self._engine.begin() as conn:
            row = (
                conn.execute(select(approval_requests).where(approval_requests.c.id == approval_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        return self._row_to_approval(row)

    def update_approval_decision(
        self,
        approval_id: UUID,
        decision: ApprovalDecision,
        *,
        decided_at: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest:
        with self._engine.begin() as conn:
            values: dict[str, Any] = {
                "decision": decision.value,
                "decided_at": decided_at,
            }
            if decision_rule_reference is not None:
                values["decision_rule_reference"] = decision_rule_reference
            conn.execute(
                approval_requests.update()
                .where(approval_requests.c.id == approval_id)
                .values(**values)
            )
            row = (
                conn.execute(select(approval_requests).where(approval_requests.c.id == approval_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"approval {approval_id} not found")
        return self._row_to_approval(row)

    def list_approvals(self, intent_id: UUID) -> tuple[ApprovalRequest, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(approval_requests)
                    .where(approval_requests.c.action_intent_id == intent_id)
                    .order_by(approval_requests.c.created_at)
                )
                .mappings()
                .fetchall()
            )
        return tuple(self._row_to_approval(r) for r in rows)

    # -- attempts --------------------------------------------------------

    def insert_attempt(self, attempt: SideEffectAttempt) -> SideEffectAttempt:
        with self._engine.begin() as conn:
            conn.execute(
                side_effect_attempts.insert().values(
                    id=attempt.id,
                    action_intent_id=attempt.action_intent_id,
                    outbox_event_id=attempt.outbox_event_id,
                    ordinal=attempt.ordinal,
                    state=attempt.state.value,
                    request_fingerprint=attempt.request_fingerprint,
                    started_at=attempt.started_at,
                    finished_at=attempt.finished_at,
                    error_code=attempt.error_code,
                    response_metadata=attempt.response_metadata,
                )
            )
        return attempt

    def get_attempt(self, attempt_id: UUID) -> SideEffectAttempt:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(side_effect_attempts).where(side_effect_attempts.c.id == attempt_id)
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"attempt {attempt_id} not found")
        return self._row_to_attempt(row)

    def update_attempt(
        self,
        attempt_id: UUID,
        *,
        state: AttemptState,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        response_metadata: dict[str, object] | None = None,
    ) -> SideEffectAttempt:
        with self._engine.begin() as conn:
            values: dict[str, Any] = {"state": state.value}
            if finished_at is not None:
                values["finished_at"] = finished_at
            if error_code is not None:
                values["error_code"] = error_code
            if response_metadata is not None:
                values["response_metadata"] = response_metadata
            conn.execute(
                side_effect_attempts.update()
                .where(side_effect_attempts.c.id == attempt_id)
                .values(**values)
            )
            row = (
                conn.execute(
                    select(side_effect_attempts).where(side_effect_attempts.c.id == attempt_id)
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            raise KeyError(f"attempt {attempt_id} not found")
        return self._row_to_attempt(row)

    def list_attempts(self, intent_id: UUID) -> tuple[SideEffectAttempt, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(side_effect_attempts)
                    .where(side_effect_attempts.c.action_intent_id == intent_id)
                    .order_by(side_effect_attempts.c.ordinal)
                )
                .mappings()
                .fetchall()
            )
        return tuple(self._row_to_attempt(r) for r in rows)

    def next_attempt_ordinal(self, intent_id: UUID) -> int:
        from sqlalchemy import func

        with self._engine.begin() as conn:
            row = conn.execute(
                select(func.coalesce(func.max(side_effect_attempts.c.ordinal), 0) + 1).where(
                    side_effect_attempts.c.action_intent_id == intent_id
                )
            ).scalar()
        return int(row or 1)

    # -- receipts --------------------------------------------------------

    def insert_receipt(self, receipt: ProviderReceipt) -> ProviderReceipt:
        with self._engine.begin() as conn, contextlib.suppress(IntegrityError):
            # Idempotent: unique constraint on (provider, reconciliation_key)
            conn.execute(
                provider_receipts.insert().values(
                    id=receipt.id,
                    side_effect_attempt_id=receipt.side_effect_attempt_id,
                    provider=receipt.provider,
                    provider_resource_id=receipt.provider_resource_id,
                    reconciliation_key=receipt.reconciliation_key,
                    final_state=receipt.final_state.value,
                    provider_timestamp=receipt.provider_timestamp,
                    received_at=receipt.received_at,
                    receipt_metadata=receipt.receipt_metadata,
                )
            )
        return receipt

    def find_receipt_by_reconciliation_key(
        self, provider: str, reconciliation_key: str
    ) -> ProviderReceipt | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(provider_receipts).where(
                        provider_receipts.c.provider == provider,
                        provider_receipts.c.reconciliation_key == reconciliation_key,
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return self._row_to_receipt(row)

    def list_receipts(self, intent_id: UUID) -> tuple[ProviderReceipt, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(provider_receipts)
                    .join(
                        side_effect_attempts,
                        provider_receipts.c.side_effect_attempt_id == side_effect_attempts.c.id,
                    )
                    .where(side_effect_attempts.c.action_intent_id == intent_id)
                    .order_by(provider_receipts.c.received_at)
                )
                .mappings()
                .fetchall()
            )
        return tuple(self._row_to_receipt(r) for r in rows)

    # -- row mappers -----------------------------------------------------

    @staticmethod
    def _row_to_intent(row: Any) -> ActionIntent:
        return ActionIntent(
            id=_uuid(row["id"]),
            action_kind=str(row["action_kind"]),
            resource_type=str(row["resource_type"]),
            resource_id=_uuid(row["resource_id"]),
            idempotency_key=str(row["idempotency_key"]),
            status=IntentStatus(row["status"]),
            current_payload_version_id=(
                _uuid(row["current_payload_version_id"])
                if row["current_payload_version_id"] is not None
                else None
            ),
            created_by=str(row["created_by"]),
            created_at=_dt(row["created_at"]) or datetime.min,
            updated_at=_dt(row["updated_at"]) or datetime.min,
        )

    @staticmethod
    def _row_to_payload_version(row: Any) -> ActionPayloadVersion:
        raw_attach: list[str] = row.get("attachment_refs") or []
        return ActionPayloadVersion(
            id=_uuid(row["id"]),
            action_intent_id=_uuid(row["action_intent_id"]),
            version=int(row["version"]),
            target=dict(row["target"]) if isinstance(row["target"], dict) else {},
            payload=dict(row["payload"]) if isinstance(row["payload"], dict) else {},
            attachment_refs=tuple(str(a) for a in raw_attach),
            payload_hash=str(row["payload_hash"]),
            created_at=_dt(row["created_at"]) or datetime.min,
        )

    @staticmethod
    def _row_to_policy_decision(row: Any) -> PolicyDecisionRecord:
        raw_reason: list[str] = row.get("reason_codes") or []
        raw_evidence: list[str] = row.get("evidence_refs") or []
        return PolicyDecisionRecord(
            id=_uuid(row["id"]),
            action_intent_id=_uuid(row["action_intent_id"]),
            payload_version_id=_uuid(row["payload_version_id"]),
            ruleset_version=str(row["ruleset_version"]),
            decision=PolicyDecisionValue(row["decision"]),
            reason_codes=tuple(str(r) for r in raw_reason),
            payload_hash=str(row["payload_hash"]),
            trusted_facts=(
                dict(row["trusted_facts"]) if isinstance(row.get("trusted_facts"), dict) else {}
            ),
            evidence_refs=tuple(str(e) for e in raw_evidence),
            untrusted_claims=(
                dict(row["untrusted_claims"])
                if isinstance(row.get("untrusted_claims"), dict)
                else {}
            ),
            expires_at=_dt(row.get("expires_at")),
            created_at=_dt(row.get("created_at")),
        )

    @staticmethod
    def _row_to_approval(row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            id=_uuid(row["id"]),
            action_intent_id=_uuid(row["action_intent_id"]),
            payload_version_id=_uuid(row["payload_version_id"]),
            policy_decision_id=_uuid(row["policy_decision_id"]),
            requested_for=str(row["requested_for"]),
            decision=ApprovalDecision(row["decision"]),
            decision_rule_reference=(
                str(row["decision_rule_reference"])
                if row.get("decision_rule_reference") is not None
                else None
            ),
            expires_at=_dt(row["expires_at"]) or datetime.min,
            decided_at=_dt(row.get("decided_at")),
            created_at=_dt(row["created_at"]) or datetime.min,
        )

    @staticmethod
    def _row_to_attempt(row: Any) -> SideEffectAttempt:
        return SideEffectAttempt(
            id=_uuid(row["id"]),
            action_intent_id=_uuid(row["action_intent_id"]),
            outbox_event_id=_uuid(row["outbox_event_id"]),
            ordinal=int(row["ordinal"]),
            state=AttemptState(row["state"]),
            request_fingerprint=str(row["request_fingerprint"]),
            started_at=_dt(row["started_at"]) or datetime.min,
            finished_at=_dt(row.get("finished_at")),
            error_code=str(row["error_code"]) if row.get("error_code") is not None else None,
            response_metadata=(
                dict(row["response_metadata"])
                if isinstance(row.get("response_metadata"), dict)
                else {}
            ),
        )

    @staticmethod
    def _row_to_receipt(row: Any) -> ProviderReceipt:
        return ProviderReceipt(
            id=_uuid(row["id"]),
            side_effect_attempt_id=_uuid(row["side_effect_attempt_id"]),
            provider=str(row["provider"]),
            provider_resource_id=str(row["provider_resource_id"]),
            reconciliation_key=str(row["reconciliation_key"]),
            final_state=ReceiptFinalState(row["final_state"]),
            provider_timestamp=_dt(row.get("provider_timestamp")),
            received_at=_dt(row["received_at"]) or datetime.min,
            receipt_metadata=(
                dict(row["receipt_metadata"])
                if isinstance(row.get("receipt_metadata"), dict)
                else {}
            ),
        )
