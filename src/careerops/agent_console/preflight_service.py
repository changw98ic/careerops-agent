"""Capability resolution and preflight for the Agent Console.

Provides the service layer for capability queries and preflight checks.
A preflight validates that an agent operation can proceed: the capability
is enabled, the context is fresh, and budget/consent are in order.

api-contract.md section 3 / 3.1 freezes the Capability and Preflight shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from careerops.agent_console.context_service import ContextService
from careerops.agent_console.contracts import (
    BudgetInfo,
    Capability,
    CapabilityState,
    ConsentScope,
    ModelOperation,
    Preflight,
    PreflightRequest,
)
from careerops.observability import current_trace_id

__all__ = ["PreflightRecord", "PreflightService"]

_PREFLIGHT_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class PreflightRecord:
    """Internal representation of a persisted preflight."""

    preflight_id: UUID
    operation: ModelOperation
    context_id: UUID
    candidate_id: UUID
    decision: Literal["ready", "blocked"]
    capability_state: CapabilityState
    reason_code: str | None
    field_set_hash: str | None
    redaction_version: str | None
    allowed_fields: tuple[str, ...]
    budget: BudgetInfo
    consent_scope: ConsentScope
    consent_id: UUID | None
    consent_issued: bool
    provider_policy_version: str | None
    created_at: datetime
    expires_at: datetime | None
    trace_id: str = ""


class PreflightService:
    """Service for capability resolution and preflight checks.

    v1 is an in-memory stub.  A production implementation would consult
    the capability resolver, consent store, and budget tracker.
    """

    def __init__(
        self,
        *,
        capability_resolver: Any | None = None,
        context_service: ContextService | None = None,
    ) -> None:
        self._capability_resolver = capability_resolver
        self._context_service = context_service or ContextService()
        self._preflights: dict[UUID, PreflightRecord] = {}

    def resolve_capability(
        self,
        candidate_id: UUID,
        operation: ModelOperation,
        *,
        now: datetime | None = None,
    ) -> Capability:
        """Resolve the capability state for an operation.

        Returns the ``Capability`` contract shape.  In v1 the capability
        resolver is consulted if available; otherwise the default is
        ``enabled``.
        """
        del candidate_id  # capability is global, not per-candidate
        trace = current_trace_id()

        if self._capability_resolver is not None:
            # Consult the real resolver if wired.
            try:
                decision = self._capability_resolver.decide(operation)  # type: ignore[union-attr]
                if hasattr(decision, "released") and not decision.released:
                    return Capability(
                        operation=operation,
                        state=CapabilityState.DISABLED_BY_POLICY,
                        reason_code="MODEL_PROVIDER_DISABLED",
                        may_start=False,
                        requires_preflight=True,
                        retryable=False,
                        trace_id=trace,
                    )
            except Exception:
                return Capability(
                    operation=operation,
                    state=CapabilityState.DEPENDENCY_NOT_READY,
                    reason_code="RESOLVER_UNAVAILABLE",
                    may_start=False,
                    requires_preflight=True,
                    retryable=True,
                    trace_id=trace,
                )

        return Capability(
            operation=operation,
            state=CapabilityState.ENABLED,
            reason_code=None,
            may_start=True,
            requires_preflight=True,
            retryable=False,
            trace_id=trace,
        )

    def create_preflight(
        self,
        candidate_id: UUID,
        body: PreflightRequest,
        *,
        now: datetime | None = None,
    ) -> Preflight:
        """Create a preflight check for an operation against a context.

        Validates that the capability is enabled and the context is fresh.
        Returns the ``Preflight`` contract shape.
        """
        occurred_at = now or datetime.now(UTC)
        trace = current_trace_id()
        preflight_id = uuid4()
        expires_at = occurred_at + _PREFLIGHT_TTL

        # Resolve capability.
        capability = self.resolve_capability(candidate_id, body.operation, now=now)

        # Verify the context exists and is owned.
        try:
            self._context_service.get(candidate_id, UUID(body.context_id), now=now)
        except Exception:
            return Preflight(
                preflight_id=str(preflight_id),
                operation=body.operation,
                context_id=body.context_id,
                decision="blocked",
                capability_state=CapabilityState.STALE,
                reason_code="CONTEXT_STALE",
                field_set_hash=None,
                redaction_version=None,
                allowed_fields=[],
                budget=BudgetInfo(
                    run_calls_remaining=0,
                    candidate_calls_remaining=0,
                    input_tokens_remaining=0,
                    output_tokens_remaining=0,
                ),
                consent_scope=ConsentScope(
                    candidate_id="omitted",
                    source_digests=[],
                    expires_at=None,
                ),
                consent_id=None,
                consent_issued=False,
                provider_policy_version=None,
                expires_at=expires_at,
                trace_id=trace,
            )

        # If capability is not enabled, return blocked.
        if capability.state is not CapabilityState.ENABLED:
            return Preflight(
                preflight_id=str(preflight_id),
                operation=body.operation,
                context_id=body.context_id,
                decision="blocked",
                capability_state=capability.state,
                reason_code=capability.reason_code,
                field_set_hash=None,
                redaction_version=None,
                allowed_fields=[],
                budget=BudgetInfo(
                    run_calls_remaining=0,
                    candidate_calls_remaining=0,
                    input_tokens_remaining=0,
                    output_tokens_remaining=0,
                ),
                consent_scope=ConsentScope(
                    candidate_id="omitted",
                    source_digests=[],
                    expires_at=None,
                ),
                consent_id=None,
                consent_issued=False,
                provider_policy_version=None,
                expires_at=expires_at,
                trace_id=trace,
            )

        # Capability is enabled and context is fresh.
        consent_id = uuid4()
        record = PreflightRecord(
            preflight_id=preflight_id,
            operation=body.operation,
            context_id=UUID(body.context_id),
            candidate_id=candidate_id,
            decision="ready",
            capability_state=CapabilityState.ENABLED,
            reason_code=None,
            field_set_hash=None,
            redaction_version="egress-redaction-v1",
            allowed_fields=(),
            budget=BudgetInfo(
                run_calls_remaining=10,
                candidate_calls_remaining=100,
                input_tokens_remaining=1_000_000,
                output_tokens_remaining=500_000,
            ),
            consent_scope=ConsentScope(
                candidate_id="omitted",
                source_digests=["sha256"],
                expires_at=expires_at,
            ),
            consent_id=consent_id,
            consent_issued=True,
            provider_policy_version=None,
            created_at=occurred_at,
            expires_at=expires_at,
        )
        self._preflights[preflight_id] = record
        return self._to_contract(record)

    def get_preflight(
        self,
        candidate_id: UUID,
        preflight_id: UUID,
    ) -> PreflightRecord | None:
        """Look up a preflight by ID.  Returns None if missing or unowned."""
        record = self._preflights.get(preflight_id)
        if record is None or record.candidate_id != candidate_id:
            return None
        return record

    @staticmethod
    def _to_contract(record: PreflightRecord) -> Preflight:
        return Preflight(
            preflight_id=str(record.preflight_id),
            operation=record.operation,
            context_id=str(record.context_id),
            decision=record.decision,
            capability_state=record.capability_state,
            reason_code=record.reason_code,
            field_set_hash=record.field_set_hash,
            redaction_version=record.redaction_version,
            allowed_fields=list(record.allowed_fields),
            budget=record.budget,
            consent_scope=record.consent_scope,
            consent_id=str(record.consent_id) if record.consent_id else None,
            consent_issued=record.consent_issued,
            provider_policy_version=record.provider_policy_version,
            expires_at=record.expires_at,
            trace_id=record.trace_id if hasattr(record, "trace_id") else current_trace_id(),
        )
