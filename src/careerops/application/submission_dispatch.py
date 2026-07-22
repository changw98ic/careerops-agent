from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.application.application_adapters import (
    AdapterDryRunOutcome,
    AdapterQualificationStatus,
    ApplicationAdapterContract,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationAdapter,
    SyntheticApplicationFixture,
)
from careerops.application.release_qualification import (
    ReleaseGateOutcome,
    SyntheticReleaseGate,
    SyntheticReleaseQualification,
)
from careerops.policy.autopilot import AutopilotOutcome

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_EVENT_PREFIX = "synthetic-dispatch"
_SYNTHETIC_PROVIDER = "synthetic"


class DispatchDecisionState(StrEnum):
    RESERVE_SYNTHETIC_DISPATCH = "reserve_synthetic_dispatch"
    STOPPED = "stopped"


class SyntheticProviderState(StrEnum):
    NOT_STARTED = "not_started"
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"


class ReconciliationState(StrEnum):
    CONFIRMED = "confirmed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class DispatchAuthority:
    campaign_id: UUID
    grant_version_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    policy_decision_id: UUID
    action_kind: str
    channel: str
    release_version: str
    payload_hash: str
    target_host: str
    company_key: str
    policy_outcome: AutopilotOutcome
    authorized_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_identifier(self.action_kind, "action_kind")
        _validate_identifier(self.channel, "channel")
        _validate_identifier(self.release_version, "release_version")
        _validate_hash(self.payload_hash, "payload_hash")
        _validate_host(self.target_host, "target_host")
        _validate_identifier(self.company_key, "company_key")
        _validate_aware(self.authorized_at, "authorized_at")
        _validate_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.authorized_at:
            raise ValueError("dispatch authority must expire after authorization")


@dataclass(frozen=True, slots=True)
class QualifiedSyntheticDispatchRequest:
    """All inputs required for a sandbox-only dispatch reservation.

    The planner runs the synthetic adapter itself instead of trusting a caller-provided
    qualification or dry-run result. Nothing in this request can reach a browser or a
    provider; the host must be a synthetic ``.test`` host and the database repeats the
    authority checks before it reserves capacity.
    """

    authority: DispatchAuthority
    fixture: SyntheticApplicationFixture
    session: SandboxBrowserSession
    payload: SandboxApplicationPayload
    release_qualification: SyntheticReleaseQualification
    now: datetime
    global_kill_switch_active: bool = False
    campaign_kill_switch_active: bool = False
    provider_kill_switch_active: bool = False
    generic_approval_present: bool = False
    model_output_approval_present: bool = False
    provider_state: SyntheticProviderState = SyntheticProviderState.NOT_STARTED

    def __post_init__(self) -> None:
        _validate_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    state: DispatchDecisionState
    reason_codes: tuple[str, ...]
    reservation_key: str | None = None
    outbox_event_key: str | None = None
    reconciliation_key: str | None = None

    @property
    def can_reserve(self) -> bool:
        return self.state is DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH


@dataclass(frozen=True, slots=True)
class DispatchReservationResult:
    decision: DispatchDecision
    reservation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SyntheticSubmissionReceipt:
    provider: str
    provider_resource_id: str
    reconciliation_key: str
    provider_state: SyntheticProviderState
    received_at: datetime

    def __post_init__(self) -> None:
        if self.provider != _SYNTHETIC_PROVIDER:
            raise ValueError("synthetic submission receipts must use the synthetic provider")
        _validate_identifier(self.provider, "provider")
        _validate_identifier(self.provider_resource_id, "provider_resource_id")
        _validate_identifier(self.reconciliation_key, "reconciliation_key")
        _validate_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    state: ReconciliationState
    reason_code: str


class SyntheticDispatchReservationStore(Protocol):
    def reserve_and_enqueue(
        self,
        decision: DispatchDecision,
        request: QualifiedSyntheticDispatchRequest,
    ) -> UUID: ...


class SyntheticSubmissionDispatchService:
    """Production composition for synthetic planning plus reservation.

    The service deliberately composes only the planner and reservation store. It performs no
    provider writes, browser IO, credential access, or worker execution.
    """

    def __init__(
        self,
        reservation_store: SyntheticDispatchReservationStore,
        planner: SyntheticSubmissionDispatchPlanner | None = None,
    ) -> None:
        self._reservation_store = reservation_store
        self._planner = planner or SyntheticSubmissionDispatchPlanner()

    def dispatch(self, request: QualifiedSyntheticDispatchRequest) -> DispatchReservationResult:
        decision = self._planner.plan(request)
        if not decision.can_reserve:
            return DispatchReservationResult(decision=decision)
        reservation_id = self._reservation_store.reserve_and_enqueue(decision, request)
        return DispatchReservationResult(decision=decision, reservation_id=reservation_id)


class SyntheticSubmissionDispatchPlanner:
    """Plan only a release-qualified, synthetic ``.test`` dispatch reservation.

    This module deliberately produces internal reservation/outbox work only. It does not import
    browser drivers, credentials, or provider SDKs. Generic human approval and model output are
    explicitly non-authoritative for autonomous dispatch.
    """

    def __init__(
        self,
        adapter: ApplicationAdapterContract | None = None,
        release_gate: SyntheticReleaseGate | None = None,
    ) -> None:
        self._adapter = adapter or SyntheticApplicationAdapter()
        self._release_gate = release_gate or SyntheticReleaseGate()

    def plan(self, request: QualifiedSyntheticDispatchRequest) -> DispatchDecision:
        authority = request.authority
        qualification = self._adapter.qualify_fixture(request.fixture)
        dry_run = self._adapter.dry_run(
            fixture=request.fixture,
            session=request.session,
            payload=request.payload,
        )
        release = self._release_gate.decide(
            request.release_qualification,
            adapter_id=request.fixture.adapter_id,
            fixture_id=request.fixture.fixture_id,
            target_host=authority.target_host,
            release_version=authority.release_version,
            now=request.now,
        )
        reasons: list[str] = []
        if authority.policy_outcome is not AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION:
            reasons.append("AUTOPILOT_POLICY_OUTCOME_REQUIRED")
        if authority.action_kind != "submit_application":
            reasons.append("ACTION_NOT_AUTOPILOT_ELIGIBLE")
        if not _is_synthetic_host(authority.target_host):
            reasons.append("NON_SYNTHETIC_TARGET_HOST")
        if request.fixture.allowed_host != authority.target_host:
            reasons.append("FIXTURE_TARGET_HOST_MISMATCH")
        if request.payload.target_host != authority.target_host:
            reasons.append("PAYLOAD_TARGET_HOST_MISMATCH")
        if request.payload.channel != authority.channel:
            reasons.append("PAYLOAD_CHANNEL_MISMATCH")
        if request.payload.payload_hash != authority.payload_hash:
            reasons.append("PAYLOAD_HASH_MISMATCH")
        if request.session.action_intent_id != authority.action_intent_id:
            reasons.append("SESSION_INTENT_MISMATCH")
        if request.payload.action_intent_id != authority.action_intent_id:
            reasons.append("PAYLOAD_INTENT_MISMATCH")
        if authority.channel != f"synthetic:{request.fixture.adapter_id}":
            reasons.append("SYNTHETIC_CHANNEL_MISMATCH")
        if release.outcome is not ReleaseGateOutcome.ALLOW_SYNTHETIC_RESERVATION:
            reasons.append(release.reason_code)
        if request.generic_approval_present:
            reasons.append("GENERIC_APPROVAL_NOT_EXECUTION_AUTHORITY")
        if request.model_output_approval_present:
            reasons.append("MODEL_OUTPUT_NOT_EXECUTION_AUTHORITY")
        if request.global_kill_switch_active:
            reasons.append("GLOBAL_KILL_SWITCH_ACTIVE")
        if request.campaign_kill_switch_active:
            reasons.append("CAMPAIGN_KILL_SWITCH_ACTIVE")
        if request.provider_kill_switch_active:
            reasons.append("PROVIDER_KILL_SWITCH_ACTIVE")
        if request.now >= authority.expires_at:
            reasons.append("DISPATCH_AUTHORITY_EXPIRED")
        if request.now < authority.authorized_at:
            reasons.append("DISPATCH_PRECEDES_AUTHORIZATION")
        if qualification.status is not AdapterQualificationStatus.QUALIFIED_SANDBOX:
            reasons.append("ADAPTER_NOT_RELEASE_QUALIFIED")
        if qualification.hard_stop_categories:
            reasons.append("QUALIFICATION_HARD_STOP_PRESENT")
        if dry_run.outcome is not AdapterDryRunOutcome.READY_FOR_REVIEW:
            reasons.append("SYNTHETIC_DRY_RUN_NOT_READY")
        if dry_run.would_submit_real_provider:
            reasons.append("REAL_PROVIDER_SUBMIT_CAPABILITY_PRESENT")
        if dry_run.hard_stop_categories:
            reasons.append("DRY_RUN_HARD_STOP_PRESENT")
        if dry_run.synthetic_confirmation_selector is None:
            reasons.append("SYNTHETIC_CONFIRMATION_REQUIRED")
        if request.provider_state is SyntheticProviderState.AMBIGUOUS:
            reasons.append("AMBIGUOUS_PROVIDER_STATE")
        if request.provider_state is SyntheticProviderState.CONFIRMED:
            reasons.append("ALREADY_CONFIRMED_PROVIDER_STATE")

        if reasons:
            return DispatchDecision(DispatchDecisionState.STOPPED, tuple(dict.fromkeys(reasons)))

        reservation_key = _stable_key(
            "reservation",
            authority.campaign_id,
            authority.grant_version_id,
            authority.authorization_id,
            authority.action_intent_id,
            authority.payload_version_id,
            authority.payload_hash,
            authority.channel,
            request.fixture.adapter_id,
            request.fixture.fixture_id,
            request.release_qualification.evidence_hash,
            request.release_qualification.expires_at,
        )
        reconciliation_key = _stable_key(
            "reconcile",
            authority.campaign_id,
            authority.grant_version_id,
            authority.action_intent_id,
            authority.payload_hash,
            authority.channel,
            request.fixture.adapter_id,
        )
        return DispatchDecision(
            DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH,
            ("SYNTHETIC_DISPATCH_QUALIFIED_FOR_RESERVATION",),
            reservation_key=reservation_key,
            outbox_event_key=f"{_EVENT_PREFIX}:{reservation_key}",
            reconciliation_key=reconciliation_key,
        )


class SyntheticReconciler:
    def decide(self, receipt: SyntheticSubmissionReceipt) -> ReconciliationDecision:
        if receipt.provider_state is SyntheticProviderState.CONFIRMED:
            return ReconciliationDecision(
                ReconciliationState.CONFIRMED,
                "SYNTHETIC_RECEIPT_CONFIRMED",
            )
        return ReconciliationDecision(
            ReconciliationState.RECONCILIATION_REQUIRED,
            "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS",
        )


def _stable_key(label: str, *parts: object) -> str:
    encoded = json.dumps(
        {"label": label, "parts": [str(part) for part in parts]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_synthetic_host(value: str) -> bool:
    return value.lower().endswith(".test")


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_host(value: str, label: str) -> None:
    if not _HOST.fullmatch(value):
        raise ValueError(f"{label} must be a bounded host")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "DispatchAuthority",
    "DispatchDecision",
    "DispatchDecisionState",
    "DispatchReservationResult",
    "QualifiedSyntheticDispatchRequest",
    "ReconciliationDecision",
    "ReconciliationState",
    "SyntheticDispatchReservationStore",
    "SyntheticProviderState",
    "SyntheticReconciler",
    "SyntheticSubmissionDispatchPlanner",
    "SyntheticSubmissionDispatchService",
    "SyntheticSubmissionReceipt",
]
