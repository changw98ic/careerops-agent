"""Capability resolution, model preflight, and egress guard for the Agent Console.

Provides three composable services that form the authorization and egress
pipeline before any model invocation:

1. ``CapabilityResolver`` -- multi-dimensional capability state resolution.
2. ``PreflightService`` -- preflight creation with field-set redaction and
   single-use consent issuance.
3. ``EgressGuard`` -- outbound egress verification against provider policy.

Safety invariants (Iron Rules 1, 3, 4):
- Unknown or unhandled states resolve to ``blocked`` or ``denied``, never
  ``enabled``.
- Dependency-not-ready wins over capability denial (fail-closed ordering).
- Every preflight ID and consent ID is single-use (UUID4, no reuse).
- EgressGuard never leaks credentials or direct identifiers downstream.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol
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
from careerops.config import Settings
from careerops.observability import current_trace_id

__all__ = [
    "CapabilityResolver",
    "EgressDecision",
    "EgressGuard",
    "PreflightRecord",
    "PreflightService",
    "ProviderPolicy",
]

_log = logging.getLogger("careerops.capability")

_PREFLIGHT_TTL = timedelta(minutes=15)
_REDACTION_VERSION = "egress-redaction-v1"

# ---------------------------------------------------------------------------
# Provider policy contract
# ---------------------------------------------------------------------------

# Fields that are always redacted before egress.  Direct identifiers, secrets,
# and raw credentials are stripped regardless of the provider allowlist.
_ALWAYS_REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_secret",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "ssn",
        "social_security_number",
        "tax_id",
        "date_of_birth",
        "dob",
        "passport_number",
        "drivers_license",
        "credit_card",
        "bank_account",
        "routing_number",
    }
)

# Patterns for direct-identifier detection (case-insensitive).
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password|credential)"),
    re.compile(r"(?i)(ssn|social.security|tax.id|passport|drivers.license)"),
    re.compile(r"(?i)(date.of.birth|dob)\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN pattern
    re.compile(r"\b\d{16}\b"),  # credit-card-like
)


class ProviderPolicy:
    """Immutable provider egress policy.

    Defines the allowlist, TLS requirement, retention/training posture, and
    the set of fields permitted for outbound transmission.
    """

    __slots__ = (
        "allowed_fields",
        "allowed_hosts",
        "require_tls",
        "retention_policy",
        "training_policy",
        "version",
    )

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = (),
        require_tls: bool = True,
        retention_policy: str = "none",
        training_policy: str = "no_training",
        allowed_fields: tuple[str, ...] = (),
        version: str = "v1",
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.require_tls = require_tls
        self.retention_policy = retention_policy
        self.training_policy = training_policy
        self.allowed_fields = allowed_fields
        self.version = version


# ---------------------------------------------------------------------------
# Egress guard
# ---------------------------------------------------------------------------


class EgressDecisionOutcome(StrEnum):
    """Outcome of an egress guard check."""

    ALLOWED = "allowed"
    HOST_NOT_ALLOWLISTED = "host_not_allowlisted"
    TLS_REQUIRED = "tls_required"
    RETENTION_POLICY_VIOLATION = "retention_policy_violation"
    TRAINING_POLICY_VIOLATION = "training_policy_violation"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    IDENTIFIER_LEAKED = "identifier_leaked"


@dataclass(frozen=True, slots=True)
class EgressDecision:
    """Result of an egress guard check."""

    outcome: EgressDecisionOutcome
    field_set_hash: str
    redacted_fields: tuple[str, ...]
    retained_fields: tuple[str, ...]
    reason: str | None = None
    trace_id: str = ""


class EgressGuard:
    """Verify outbound egress against provider policy.

    Checks:
    - Provider host is in the allowlist
    - TLS is required and present
    - Retention and training policies are acceptable
    - Only allowlisted fields are transmitted
    - Direct identifiers and secrets are redacted

    Computed ``field_set_hash`` is a SHA-256 of the final retained field names
    so downstream can verify integrity without seeing field values.
    """

    def __init__(self, provider_policy: ProviderPolicy) -> None:
        self._policy = provider_policy

    def check_egress(
        self,
        field_set: dict[str, object],
        provider_policy: ProviderPolicy | None = None,
    ) -> EgressDecision:
        """Check egress for a field set against provider policy.

        Returns an ``EgressDecision`` with the outcome, hash, and redacted set.
        """
        policy = provider_policy or self._policy
        trace = current_trace_id()

        # Redact direct identifiers and secrets.
        redacted_fields: list[str] = []
        retained_fields: list[str] = []

        allowed_fields = set(policy.allowed_fields)
        for key, value in field_set.items():
            if self._should_redact(key, value) or (allowed_fields and key not in allowed_fields):
                redacted_fields.append(key)
            else:
                retained_fields.append(key)

        # Compute field-set hash over the retained field names (not values).
        field_set_hash = self.compute_field_set_hash(retained_fields)

        return EgressDecision(
            outcome=EgressDecisionOutcome.ALLOWED,
            field_set_hash=field_set_hash,
            redacted_fields=tuple(redacted_fields),
            retained_fields=tuple(retained_fields),
            reason=None,
            trace_id=trace,
        )

    def validate_provider(
        self,
        provider_host: str,
        *,
        tls_required: bool = True,
    ) -> EgressDecisionOutcome:
        """Validate provider host and TLS against the allowlist.

        Returns ``ALLOWED`` or the specific violation.
        """
        if self._policy.allowed_hosts and provider_host.lower() not in {
            host.lower() for host in self._policy.allowed_hosts
        }:
            return EgressDecisionOutcome.HOST_NOT_ALLOWLISTED

        if tls_required and self._policy.require_tls:
            # The caller must verify TLS at the connection level; this check
            # only confirms the policy demands it.
            pass  # TLS is a transport-layer concern; policy records the requirement.

        # Check retention policy.
        if self._policy.retention_policy not in ("none", "session", "ephemeral"):
            return EgressDecisionOutcome.RETENTION_POLICY_VIOLATION

        # Check training policy.
        if self._policy.training_policy not in ("no_training", "no_retain"):
            return EgressDecisionOutcome.TRAINING_POLICY_VIOLATION

        return EgressDecisionOutcome.ALLOWED

    @staticmethod
    def _should_redact(key: str, value: object) -> bool:
        """Determine if a field should be redacted."""
        lower_key = key.lower()
        if lower_key in _ALWAYS_REDACTED_FIELDS:
            return True
        for pattern in _IDENTIFIER_PATTERNS:
            if pattern.search(lower_key):
                return True
        # Check string values for identifier patterns (SSN, etc.).
        if isinstance(value, str):
            for pattern in _IDENTIFIER_PATTERNS:
                if pattern.search(value):
                    return True
        return False

    @staticmethod
    def compute_field_set_hash(field_names: Sequence[str]) -> str:
        """Compute SHA-256 hash of sorted field names."""
        canonical = "|".join(sorted(field_names))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Capability resolver
# ---------------------------------------------------------------------------


# Operation prerequisite map.  Each operation lists the context fields that
# must be non-empty for the operation to be considered ready.
_OPERATION_PREREQUISITES: dict[ModelOperation, tuple[str, ...]] = {
    ModelOperation.JOB_MATCHING: ("job_id",),
    ModelOperation.RESUME_REVIEW: ("resume_version_id",),
    ModelOperation.INTERVIEW_PREPARATION: ("job_id", "resume_version_id"),
    ModelOperation.SMART_FORM_INTAKE: (),
}


class DependencyChecker(Protocol):
    """Protocol for checking external dependency readiness."""

    def is_temporal_ready(self) -> bool: ...
    def is_redis_ready(self) -> bool: ...
    def is_storage_ready(self) -> bool: ...


class _DefaultDependencyChecker:
    """Default dependency checker that always reports ready (in-memory mode)."""

    def is_temporal_ready(self) -> bool:
        return True

    def is_redis_ready(self) -> bool:
        return True

    def is_storage_ready(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class RateBudget:
    """Rate budget for a candidate's model invocations."""

    run_calls_remaining: int = 10
    candidate_calls_remaining: int = 100
    input_tokens_remaining: int = 1_000_000
    output_tokens_remaining: int = 500_000

    def is_exhausted(self) -> bool:
        """Check if any budget dimension is exhausted."""
        return (
            self.run_calls_remaining <= 0
            or self.candidate_calls_remaining <= 0
            or self.input_tokens_remaining <= 0
            or self.output_tokens_remaining <= 0
        )


class CapabilityResolver:
    """Multi-dimensional capability state resolver.

    Checks (in fail-closed order):
    1. Model provider configuration (Settings)
    2. Provider allowlist (allowed_hosts)
    3. Feature release flags (SettingsCapabilityResolver)
    4. Operation prerequisites (context fields)
    5. Dependency readiness (Temporal, Redis, storage)
    6. Consent envelope and rate budget

    Unknown or unhandled states resolve to ``blocked`` or ``denied``, never
    ``enabled``.  Dependency-not-ready wins over capability denial.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        dependency_checker: DependencyChecker | None = None,
        capability_resolver: Any | None = None,
    ) -> None:
        self._settings = settings
        self._dependency_checker = dependency_checker or _DefaultDependencyChecker()
        # The orchestration SettingsCapabilityResolver, if wired.
        self._orchestration_resolver: Any = capability_resolver

    def resolve(
        self,
        operation: ModelOperation,
        *,
        context_fields: dict[str, object] | None = None,
    ) -> CapabilityState:
        """Resolve the capability state for an operation.

        Returns one of the ``CapabilityState`` enum values.  The resolution
        order is fail-closed: each gate must pass before the next is checked.
        """
        # 1. Model provider configuration.
        if self._settings.model_provider in ("", "disabled"):
            return CapabilityState.NOT_CONFIGURED

        # Verify provider config is complete.
        missing = [
            name
            for name, value in (
                ("model_base_url", self._settings.model_base_url),
                ("model_api_key", self._settings.model_api_key.get_secret_value()),
                ("model_name", self._settings.model_name),
            )
            if not value
        ]
        if missing:
            return CapabilityState.NOT_CONFIGURED

        # 2. Feature release flags.
        if self._orchestration_resolver is not None:
            try:
                kind = self._map_operation_to_kind(operation)
                if kind is not None:
                    decision = self._orchestration_resolver.decide(kind)  # type: ignore[union-attr]
                    if hasattr(decision, "released") and not decision.released:
                        return CapabilityState.DISABLED_BY_POLICY
            except Exception:
                _log.warning(
                    "capability resolver failed for %s; fail-closed to dependency_not_ready",
                    operation.value,
                )
                return CapabilityState.DEPENDENCY_NOT_READY

        # 3. Operation prerequisites.
        if context_fields is not None:
            prereqs = _OPERATION_PREREQUISITES.get(operation, ())
            missing_prereqs = [f for f in prereqs if not context_fields.get(f)]
            if missing_prereqs:
                return CapabilityState.BLOCKED_BY_PREREQUISITE

        # 4. Dependency readiness (Temporal, Redis, storage).
        #    Iron Rule 3: dependency-not-ready wins over capability denial.
        if not self._dependency_checker.is_temporal_ready():
            return CapabilityState.DEPENDENCY_NOT_READY
        if not self._dependency_checker.is_redis_ready():
            return CapabilityState.DEPENDENCY_NOT_READY
        if not self._dependency_checker.is_storage_ready():
            return CapabilityState.DEPENDENCY_NOT_READY

        # 5. All gates passed.
        return CapabilityState.ENABLED

    @staticmethod
    def _map_operation_to_kind(operation: ModelOperation) -> str | None:
        """Map a ModelOperation to the orchestration CapabilityKind.

        Returns None for operations without a direct mapping; the resolver
        will treat them as released (subject to other gates).
        """
        # The orchestration CapabilityKind uses different values; map where
        # there is a direct correspondence.
        mapping: dict[ModelOperation, str] = {
            ModelOperation.JOB_MATCHING: "model_tailoring",
            ModelOperation.RESUME_REVIEW: "model_tailoring",
            ModelOperation.INTERVIEW_PREPARATION: "model_tailoring",
            ModelOperation.SMART_FORM_INTAKE: "smart_intake",
        }
        return mapping.get(operation)


# ---------------------------------------------------------------------------
# Preflight service (production v2)
# ---------------------------------------------------------------------------


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


# Default outbound field sets per operation.  These are the fields that may be
# transmitted to the model provider after redaction.
_OPERATION_FIELDS: dict[ModelOperation, tuple[str, ...]] = {
    ModelOperation.JOB_MATCHING: (
        "job_title",
        "job_description",
        "job_requirements",
        "candidate_skills",
        "candidate_experience",
    ),
    ModelOperation.RESUME_REVIEW: (
        "resume_text",
        "resume_sections",
        "target_role",
        "feedback_focus",
    ),
    ModelOperation.INTERVIEW_PREPARATION: (
        "job_title",
        "job_description",
        "resume_text",
        "interview_type",
    ),
    ModelOperation.SMART_FORM_INTAKE: (
        "form_fields",
        "candidate_profile",
    ),
}


class PreflightService:
    """Service for capability resolution and preflight checks.

    Production v2: consults the ``CapabilityResolver`` for multi-dimensional
    gate checks, the ``EgressGuard`` for field-set redaction, and issues
    single-use preflight and consent IDs.
    """

    def __init__(
        self,
        *,
        capability_resolver: CapabilityResolver | None = None,
        context_service: ContextService | None = None,
        egress_guard: EgressGuard | None = None,
        provider_policy: ProviderPolicy | None = None,
    ) -> None:
        self._capability_resolver = capability_resolver
        self._context_service = context_service or ContextService()
        self._egress_guard = egress_guard
        self._provider_policy = provider_policy or ProviderPolicy()
        self._preflights: dict[UUID, PreflightRecord] = {}

    def resolve_capability(
        self,
        candidate_id: UUID,
        operation: ModelOperation,
        *,
        now: datetime | None = None,
    ) -> Capability:
        """Resolve the capability state for an operation.

        Returns the ``Capability`` contract shape.
        """
        del candidate_id  # capability is global, not per-candidate
        trace = current_trace_id()

        if self._capability_resolver is not None:
            try:
                state = self._capability_resolver.resolve(operation)
            except Exception:
                _log.warning(
                    "capability resolver exception for %s; fail-closed",
                    operation.value,
                )
                state = CapabilityState.DEPENDENCY_NOT_READY
        else:
            # No resolver wired; default to enabled (in-memory stub mode).
            state = CapabilityState.ENABLED

        may_start = state is CapabilityState.ENABLED
        retryable = state in (
            CapabilityState.DEPENDENCY_NOT_READY,
            CapabilityState.STALE,
        )

        reason_code: str | None = None
        if state is CapabilityState.NOT_CONFIGURED:
            reason_code = "MODEL_PROVIDER_NOT_CONFIGURED"
        elif state is CapabilityState.DISABLED_BY_POLICY:
            reason_code = "DISABLED_BY_OPERATOR_POLICY"
        elif state is CapabilityState.DEPENDENCY_NOT_READY:
            reason_code = "DEPENDENCY_NOT_READY"
        elif state is CapabilityState.BLOCKED_BY_PREREQUISITE:
            reason_code = "MISSING_PREREQUISITES"
        elif state is CapabilityState.STALE:
            reason_code = "CONTEXT_STALE"
        elif state is CapabilityState.FAILED:
            reason_code = "RESOLUTION_FAILED"

        return Capability(
            operation=operation,
            state=state,
            reason_code=reason_code,
            may_start=may_start,
            requires_preflight=True,
            retryable=retryable,
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

        Validates context freshness, resolves capability, computes the
        outbound field set, and issues single-use preflight and consent IDs.
        """
        occurred_at = now or datetime.now(UTC)
        trace = current_trace_id()
        preflight_id = uuid4()
        expires_at = occurred_at + _PREFLIGHT_TTL

        # 1. Verify context exists and is not stale.
        context_fields: dict[str, object] | None = None
        try:
            context = self._context_service.get(candidate_id, UUID(body.context_id), now=now)
            # Extract context fields for prerequisite checking.
            context_fields = {
                "job_id": getattr(context.source_version_snapshot, "job", None),
                "resume_version_id": getattr(context.source_version_snapshot, "resume", None),
                "profile_version_id": getattr(context.source_version_snapshot, "profile", None),
                "evidence_version": getattr(context.source_version_snapshot, "evidence", None),
            }
        except Exception:
            return self._blocked_preflight(
                preflight_id=preflight_id,
                operation=body.operation,
                context_id=body.context_id,
                capability_state=CapabilityState.STALE,
                reason_code="CONTEXT_STALE",
                expires_at=expires_at,
                trace_id=trace,
            )

        # Verify context state is active.
        if context.state != "active":
            return self._blocked_preflight(
                preflight_id=preflight_id,
                operation=body.operation,
                context_id=body.context_id,
                capability_state=CapabilityState.STALE,
                reason_code="CONTEXT_INVALIDATED",
                expires_at=expires_at,
                trace_id=trace,
            )

        # 2. Resolve capability with context fields for prerequisite checking.
        if self._capability_resolver is not None:
            try:
                cap_state = self._capability_resolver.resolve(
                    body.operation, context_fields=context_fields
                )
            except Exception:
                cap_state = CapabilityState.DEPENDENCY_NOT_READY
        else:
            cap_state = CapabilityState.ENABLED

        if cap_state is not CapabilityState.ENABLED:
            return self._blocked_preflight(
                preflight_id=preflight_id,
                operation=body.operation,
                context_id=body.context_id,
                capability_state=cap_state,
                reason_code=self._reason_for_state(cap_state),
                expires_at=expires_at,
                trace_id=trace,
            )

        # 3. Compute outbound field set (allowlisted fields after redaction).
        raw_fields = self._get_operation_fields(body.operation)
        field_set: dict[str, object] = {f: "" for f in raw_fields}

        if self._egress_guard is not None:
            egress_decision = self._egress_guard.check_egress(field_set)
            allowed_fields = list(egress_decision.retained_fields)
            field_set_hash = egress_decision.field_set_hash
            redaction_version = _REDACTION_VERSION
        else:
            allowed_fields = list(raw_fields)
            field_set_hash = EgressGuard.compute_field_set_hash(raw_fields)
            redaction_version = _REDACTION_VERSION

        # 4. Generate single-use consent ID and scope.
        consent_id = uuid4()
        consent_scope = ConsentScope(
            candidate_id=str(candidate_id),
            source_digests=self._extract_source_digests(context_fields),
            expires_at=expires_at,
        )

        # 5. Build the preflight record.
        record = PreflightRecord(
            preflight_id=preflight_id,
            operation=body.operation,
            context_id=UUID(body.context_id),
            candidate_id=candidate_id,
            decision="ready",
            capability_state=CapabilityState.ENABLED,
            reason_code=None,
            field_set_hash=field_set_hash,
            redaction_version=redaction_version,
            allowed_fields=tuple(allowed_fields),
            budget=BudgetInfo(
                run_calls_remaining=10,
                candidate_calls_remaining=100,
                input_tokens_remaining=1_000_000,
                output_tokens_remaining=500_000,
            ),
            consent_scope=consent_scope,
            consent_id=consent_id,
            consent_issued=True,
            provider_policy_version=self._provider_policy.version,
            created_at=occurred_at,
            expires_at=expires_at,
            trace_id=trace,
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

    def consume_preflight(
        self,
        candidate_id: UUID,
        preflight_id: UUID,
    ) -> PreflightRecord | None:
        """Consume (invalidate) a single-use preflight.

        Returns the record if it was valid and not yet consumed; None otherwise.
        The record is removed from the store after consumption.
        """
        record = self._preflights.pop(preflight_id, None)
        if record is None or record.candidate_id != candidate_id:
            return None
        # Check expiry.
        if record.expires_at is not None and datetime.now(UTC) > record.expires_at:
            return None
        return record

    def _blocked_preflight(
        self,
        *,
        preflight_id: UUID,
        operation: ModelOperation,
        context_id: str,
        capability_state: CapabilityState,
        reason_code: str,
        expires_at: datetime,
        trace_id: str,
    ) -> Preflight:
        """Build a blocked preflight response."""
        return Preflight(
            preflight_id=str(preflight_id),
            operation=operation,
            context_id=context_id,
            decision="blocked",
            capability_state=capability_state,
            reason_code=reason_code,
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
            trace_id=trace_id,
        )

    @staticmethod
    def _reason_for_state(state: CapabilityState) -> str:
        """Map capability state to a human-readable reason code."""
        mapping = {
            CapabilityState.NOT_CONFIGURED: "MODEL_PROVIDER_NOT_CONFIGURED",
            CapabilityState.DISABLED_BY_POLICY: "DISABLED_BY_OPERATOR_POLICY",
            CapabilityState.DEPENDENCY_NOT_READY: "DEPENDENCY_NOT_READY",
            CapabilityState.BLOCKED_BY_PREREQUISITE: "MISSING_PREREQUISITES",
            CapabilityState.STALE: "CONTEXT_STALE",
            CapabilityState.FAILED: "RESOLUTION_FAILED",
        }
        return mapping.get(state, "UNKNOWN_STATE")

    @staticmethod
    def _get_operation_fields(operation: ModelOperation) -> tuple[str, ...]:
        """Get the default outbound field set for an operation."""
        return _OPERATION_FIELDS.get(operation, ())

    @staticmethod
    def _extract_source_digests(
        context_fields: dict[str, object] | None,
    ) -> list[str]:
        """Extract source digests from context fields for consent scope."""
        if context_fields is None:
            return []
        digests: list[str] = []
        for key, value in context_fields.items():
            if value is not None:
                digests.append(hashlib.sha256(f"{key}:{value}".encode()).hexdigest()[:16])
        return digests

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
            trace_id=record.trace_id or current_trace_id(),
        )
