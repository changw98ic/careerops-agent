"""Fail-closed release qualification for rollout control planes.

The production runtime has no qualified real-provider write release. This module contains two
layers:

* the existing narrow synthetic ``.test`` gate, kept for the current dispatch path; and
* a generic immutable release-qualification domain that future adapters can persist and audit
  before any real provider is allowed to progress beyond shadow/review-required rollout.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")


class AutopilotReleaseStage(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    REVIEW_REQUIRED = "review_required"
    SYNTHETIC_SANDBOX = "synthetic_sandbox"
    LIMITED_AUTOPILOT = "limited_autopilot"
    EXPANDED_AUTOPILOT = "expanded_autopilot"


class ReleaseGateOutcome(StrEnum):
    ALLOW_SYNTHETIC_RESERVATION = "allow_synthetic_reservation"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class ReleaseQualificationStatus(StrEnum):
    DRAFT = "draft"
    EVALUATING = "evaluating"
    PENDING_INDEPENDENT_REVIEW = "pending_independent_review"
    QUALIFIED = "qualified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ReleaseQualificationActorRole(StrEnum):
    RUNNER = "runner"
    REVIEWER = "reviewer"
    OPERATOR = "operator"


class RolloutExpectedDecision(StrEnum):
    ALLOW_WRITE = "allow_write"
    REQUIRE_REVIEW = "require_review"
    BLOCK = "block"


class RolloutActualDecision(StrEnum):
    ALLOW_WRITE = "allow_write"
    REQUIRE_REVIEW = "require_review"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class ReleaseQualificationRequestBinding:
    """Exact release identity that a runtime request must match before it can proceed."""

    capability: str
    action_kind: str
    mode: AutopilotReleaseStage
    adapter_id: str
    provider: str
    provider_release: str
    implementation_hash: str
    config_hash: str
    policy_hash: str
    dataset_hash: str
    git_commit: str
    image_digest: str
    migration_version: str
    oauth_scope_hash: str
    credential_profile_hash: str
    network_policy_hash: str
    reconcile_strategy_hash: str
    hard_stop_version: str
    sensitive_policy_version: str
    kill_switch_version: str
    fixture_manifest_hash: str
    fault_manifest_hash: str
    holdout_manifest_hash: str
    live_sample_manifest_hash: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.capability, "capability"),
            (self.action_kind, "action_kind"),
            (self.adapter_id, "adapter_id"),
            (self.provider, "provider"),
            (self.provider_release, "provider_release"),
            (self.git_commit, "git_commit"),
            (self.image_digest, "image_digest"),
            (self.migration_version, "migration_version"),
            (self.hard_stop_version, "hard_stop_version"),
            (self.sensitive_policy_version, "sensitive_policy_version"),
            (self.kill_switch_version, "kill_switch_version"),
        ):
            _validate_identifier(value, label)
        for value, label in (
            (self.implementation_hash, "implementation_hash"),
            (self.config_hash, "config_hash"),
            (self.policy_hash, "policy_hash"),
            (self.dataset_hash, "dataset_hash"),
            (self.oauth_scope_hash, "oauth_scope_hash"),
            (self.credential_profile_hash, "credential_profile_hash"),
            (self.network_policy_hash, "network_policy_hash"),
            (self.reconcile_strategy_hash, "reconcile_strategy_hash"),
            (self.fixture_manifest_hash, "fixture_manifest_hash"),
            (self.fault_manifest_hash, "fault_manifest_hash"),
            (self.holdout_manifest_hash, "holdout_manifest_hash"),
            (self.live_sample_manifest_hash, "live_sample_manifest_hash"),
        ):
            _validate_hash(value, label)


@dataclass(frozen=True, slots=True)
class ReleaseQualificationBinding(ReleaseQualificationRequestBinding):
    qualification_id: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        ReleaseQualificationRequestBinding.__post_init__(self)
        _validate_identifier(self.qualification_id, "qualification_id")
        _validate_aware(self.created_at, "created_at")
        _validate_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("release qualification must expire after creation")

    @property
    def request_binding(self) -> ReleaseQualificationRequestBinding:
        return ReleaseQualificationRequestBinding(
            capability=self.capability,
            action_kind=self.action_kind,
            mode=self.mode,
            adapter_id=self.adapter_id,
            provider=self.provider,
            provider_release=self.provider_release,
            implementation_hash=self.implementation_hash,
            config_hash=self.config_hash,
            policy_hash=self.policy_hash,
            dataset_hash=self.dataset_hash,
            git_commit=self.git_commit,
            image_digest=self.image_digest,
            migration_version=self.migration_version,
            oauth_scope_hash=self.oauth_scope_hash,
            credential_profile_hash=self.credential_profile_hash,
            network_policy_hash=self.network_policy_hash,
            reconcile_strategy_hash=self.reconcile_strategy_hash,
            hard_stop_version=self.hard_stop_version,
            sensitive_policy_version=self.sensitive_policy_version,
            kill_switch_version=self.kill_switch_version,
            fixture_manifest_hash=self.fixture_manifest_hash,
            fault_manifest_hash=self.fault_manifest_hash,
            holdout_manifest_hash=self.holdout_manifest_hash,
            live_sample_manifest_hash=self.live_sample_manifest_hash,
        )


@dataclass(frozen=True, slots=True)
class RolloutObservation:
    observation_id: str
    stage: AutopilotReleaseStage
    expected_decision: RolloutExpectedDecision
    actual_decision: RolloutActualDecision
    human_override_required: bool = False
    hard_stop_present: bool = False
    autonomous_provider_write_attempted: bool = False

    def __post_init__(self) -> None:
        _validate_identifier(self.observation_id, "observation_id")


@dataclass(frozen=True, slots=True)
class RolloutMeasurementSummary:
    total_observations: int
    shadow_observations: int
    review_required_observations: int
    predicted_positive: int
    true_positive: int
    false_positive: int
    false_negative: int
    human_overrides_required: int
    autonomous_provider_write_attempts: int
    decision_precision: float
    no_autonomous_writes: bool

    def __post_init__(self) -> None:
        counts = (
            self.total_observations,
            self.shadow_observations,
            self.review_required_observations,
            self.predicted_positive,
            self.true_positive,
            self.false_positive,
            self.false_negative,
            self.human_overrides_required,
            self.autonomous_provider_write_attempts,
        )
        if any(value < 0 for value in counts):
            raise ValueError("rollout measurement counts must not be negative")
        if self.shadow_observations + self.review_required_observations > self.total_observations:
            raise ValueError("rollout stage counts exceed total observations")
        if self.true_positive + self.false_positive != self.predicted_positive:
            raise ValueError("rollout predicted-positive counts are inconsistent")
        if not 0 <= self.decision_precision <= 1:
            raise ValueError("rollout decision precision must be between zero and one")
        if self.no_autonomous_writes is not (self.autonomous_provider_write_attempts == 0):
            raise ValueError("rollout no-write flag does not match autonomous write count")


@dataclass(frozen=True, slots=True)
class ReleaseQualificationEvidence:
    evidence_id: str
    qualification_id: str
    runner_actor: str
    run_started_at: datetime
    run_completed_at: datetime
    artifact_hash: str
    metrics_hash: str
    measurement_summary: RolloutMeasurementSummary

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_id, "evidence_id"),
            (self.qualification_id, "qualification_id"),
            (self.runner_actor, "runner_actor"),
        ):
            _validate_identifier(value, label)
        _validate_aware(self.run_started_at, "run_started_at")
        _validate_aware(self.run_completed_at, "run_completed_at")
        if self.run_completed_at < self.run_started_at:
            raise ValueError("evidence run cannot complete before it starts")
        _validate_hash(self.artifact_hash, "artifact_hash")
        _validate_hash(self.metrics_hash, "metrics_hash")
        if self.measurement_summary.total_observations < 1:
            raise ValueError("release qualification evidence requires measured observations")


@dataclass(frozen=True, slots=True)
class ReleaseQualification:
    binding: ReleaseQualificationBinding
    status: ReleaseQualificationStatus
    created_by: str
    status_updated_at: datetime

    def __post_init__(self) -> None:
        _validate_identifier(self.created_by, "created_by")
        _validate_aware(self.status_updated_at, "status_updated_at")
        if self.status_updated_at < self.binding.created_at:
            raise ValueError("status_updated_at cannot predate qualification creation")

    @classmethod
    def draft(
        cls,
        binding: ReleaseQualificationBinding,
        *,
        created_by: str,
    ) -> Self:
        return cls(
            binding=binding,
            status=ReleaseQualificationStatus.DRAFT,
            created_by=created_by,
            status_updated_at=binding.created_at,
        )


@dataclass(frozen=True, slots=True)
class ReleaseQualificationDecision:
    decision_id: str
    qualification_id: str
    from_status: ReleaseQualificationStatus
    to_status: ReleaseQualificationStatus
    actor_id: str
    actor_role: ReleaseQualificationActorRole
    decided_at: datetime
    reason_code: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.decision_id, "decision_id"),
            (self.qualification_id, "qualification_id"),
            (self.actor_id, "actor_id"),
            (self.reason_code, "reason_code"),
        ):
            _validate_identifier(value, label)
        _validate_aware(self.decided_at, "decided_at")
        for evidence_id in self.evidence_ids:
            _validate_identifier(evidence_id, "evidence_ids")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("decision evidence_ids must be unique")
        if self.evidence_ids != tuple(sorted(self.evidence_ids)):
            raise ValueError("decision evidence_ids must use canonical sorted order")


@dataclass(frozen=True, slots=True)
class QualifiedReleaseGateDecision:
    outcome: ReleaseGateOutcome
    reason_code: str


@dataclass(frozen=True, slots=True)
class SyntheticReleaseQualification:
    adapter_id: str
    fixture_id: str
    release_version: str
    stage: AutopilotReleaseStage
    evidence_hash: str
    expires_at: datetime
    declares_real_provider_write: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.adapter_id, "adapter_id"),
            (self.fixture_id, "fixture_id"),
            (self.release_version, "release_version"),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{label} must be a bounded identifier")
        if not _HASH.fullmatch(self.evidence_hash):
            raise ValueError("evidence_hash must be a lowercase sha256 hex digest")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("qualification expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReleaseGateDecision:
    outcome: ReleaseGateOutcome
    reason_code: str


class SyntheticReleaseGate:
    """Allow only a non-provider, `.test`-host synthetic reservation."""

    def decide(
        self,
        qualification: SyntheticReleaseQualification,
        *,
        adapter_id: str,
        fixture_id: str,
        target_host: str,
        release_version: str,
        now: datetime,
    ) -> ReleaseGateDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            return ReleaseGateDecision(ReleaseGateOutcome.BLOCKED, "INVALID_RELEASE_TIME")
        if now >= qualification.expires_at:
            return ReleaseGateDecision(ReleaseGateOutcome.BLOCKED, "RELEASE_QUALIFICATION_EXPIRED")
        if qualification.declares_real_provider_write:
            return ReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "REAL_PROVIDER_WRITE_CAPABILITY_FORBIDDEN",
            )
        if qualification.adapter_id != adapter_id or qualification.fixture_id != fixture_id:
            return ReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED, "QUALIFICATION_IDENTITY_MISMATCH"
            )
        if qualification.release_version != release_version:
            return ReleaseGateDecision(ReleaseGateOutcome.BLOCKED, "STALE_RELEASE_QUALIFICATION")
        if not target_host.lower().endswith(".test"):
            return ReleaseGateDecision(ReleaseGateOutcome.BLOCKED, "NON_SYNTHETIC_TARGET_HOST")
        if qualification.stage is AutopilotReleaseStage.SHADOW:
            return ReleaseGateDecision(ReleaseGateOutcome.REVIEW_REQUIRED, "SHADOW_STAGE_ONLY")
        if qualification.stage is AutopilotReleaseStage.REVIEW_REQUIRED:
            return ReleaseGateDecision(
                ReleaseGateOutcome.REVIEW_REQUIRED,
                "RELEASE_STAGE_REQUIRES_REVIEW",
            )
        if qualification.stage is not AutopilotReleaseStage.SYNTHETIC_SANDBOX:
            return ReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "RELEASE_STAGE_NOT_ENABLED_FOR_THIS_RUNTIME",
            )
        return ReleaseGateDecision(
            ReleaseGateOutcome.ALLOW_SYNTHETIC_RESERVATION,
            "SYNTHETIC_RELEASE_QUALIFIED",
        )


class ReleaseQualificationLifecycle:
    """Validate append-only release qualification status transitions.

    Actor IDs are process/agent identities, not console-user IDs. The release sequence is:

    * runner/implementer opens evaluation from draft;
    * independent reviewer sends measured evidence to review; and
    * human operator makes the final or revocation decision.
    """

    _ALLOWED: Mapping[ReleaseQualificationStatus, frozenset[ReleaseQualificationStatus]] = (
        MappingProxyType(
            {
                ReleaseQualificationStatus.DRAFT: frozenset(
                    {ReleaseQualificationStatus.EVALUATING}
                ),
                ReleaseQualificationStatus.EVALUATING: frozenset(
                    {ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW}
                ),
                ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW: frozenset(
                    {
                        ReleaseQualificationStatus.QUALIFIED,
                        ReleaseQualificationStatus.REJECTED,
                    }
                ),
                ReleaseQualificationStatus.QUALIFIED: frozenset(
                    {
                        ReleaseQualificationStatus.EXPIRED,
                        ReleaseQualificationStatus.REVOKED,
                    }
                ),
            }
        )
    )

    def apply(
        self,
        qualification: ReleaseQualification,
        decision: ReleaseQualificationDecision,
        *,
        evidence: Sequence[ReleaseQualificationEvidence] = (),
        previous_decisions: Sequence[ReleaseQualificationDecision] = (),
    ) -> ReleaseQualification:
        if decision.qualification_id != qualification.binding.qualification_id:
            raise ValueError("decision qualification_id does not match qualification")
        if decision.from_status is not qualification.status:
            raise ValueError("decision from_status does not match qualification status")
        if decision.to_status not in self._ALLOWED.get(qualification.status, frozenset()):
            raise ValueError("release qualification status transition is not allowed")
        if decision.decided_at < qualification.status_updated_at:
            raise ValueError("decision cannot predate current qualification status")

        evidence_by_id = {item.evidence_id: item for item in evidence}
        try:
            selected_evidence = tuple(evidence_by_id[item] for item in decision.evidence_ids)
        except KeyError as error:
            raise ValueError("decision references missing evidence") from error
        for item in selected_evidence:
            if item.qualification_id != qualification.binding.qualification_id:
                raise ValueError("evidence qualification_id does not match qualification")

        if decision.to_status is ReleaseQualificationStatus.EVALUATING:
            if decision.actor_role is not ReleaseQualificationActorRole.RUNNER:
                raise ValueError("only a runner can open release evaluation")
            if decision.evidence_ids:
                raise ValueError("draft evaluation decision must not bind evidence")
        elif decision.to_status is ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW:
            if decision.actor_role is not ReleaseQualificationActorRole.REVIEWER:
                raise ValueError("only an independent reviewer can send evidence to review")
            if not selected_evidence:
                raise ValueError("independent review requires release evidence")
            if decision.evidence_ids != tuple(sorted(evidence_by_id)):
                raise ValueError("independent review must bind the complete evidence set")
            _ensure_independent_from_runners(decision.actor_id, selected_evidence)
        elif decision.to_status in {
            ReleaseQualificationStatus.QUALIFIED,
            ReleaseQualificationStatus.REJECTED,
        }:
            if decision.actor_role is not ReleaseQualificationActorRole.OPERATOR:
                raise ValueError("only an operator can make final release decisions")
            if not selected_evidence:
                raise ValueError("final release decision requires evidence")
            _ensure_independent_from_runners(decision.actor_id, selected_evidence)
            reviewer_decisions = tuple(
                item
                for item in previous_decisions
                if item.qualification_id == qualification.binding.qualification_id
                and item.to_status is ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW
                and item.actor_role is ReleaseQualificationActorRole.REVIEWER
            )
            if not reviewer_decisions:
                raise ValueError("final release decision requires an independent reviewer")
            latest_review = max(
                reviewer_decisions,
                key=lambda item: (item.decided_at, item.decision_id),
            )
            if decision.actor_id == latest_review.actor_id:
                raise ValueError("final release operator must differ from reviewer")
            if decision.evidence_ids != latest_review.evidence_ids:
                raise ValueError("final decision must use the independently reviewed evidence set")
            if decision.evidence_ids != tuple(sorted(evidence_by_id)):
                raise ValueError("final decision must bind the complete evidence set")
            if decision.to_status is ReleaseQualificationStatus.QUALIFIED:
                _ensure_evidence_passes_release_boundary(selected_evidence)
        elif decision.to_status in {
            ReleaseQualificationStatus.EXPIRED,
            ReleaseQualificationStatus.REVOKED,
        }:
            if decision.actor_role is not ReleaseQualificationActorRole.OPERATOR:
                raise ValueError("only an operator can expire or revoke release qualification")

        return ReleaseQualification(
            binding=qualification.binding,
            status=decision.to_status,
            created_by=qualification.created_by,
            status_updated_at=decision.decided_at,
        )


class QualifiedReleaseGate:
    """Default-deny evaluator for fully qualified rollout bindings."""

    def decide(
        self,
        qualification: ReleaseQualification,
        *,
        requested_binding: ReleaseQualificationRequestBinding,
        now: datetime,
    ) -> QualifiedReleaseGateDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            return QualifiedReleaseGateDecision(ReleaseGateOutcome.BLOCKED, "INVALID_RELEASE_TIME")
        if now >= qualification.binding.expires_at:
            return QualifiedReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "RELEASE_QUALIFICATION_EXPIRED",
            )
        if requested_binding != qualification.binding.request_binding:
            return QualifiedReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "RELEASE_BINDING_MISMATCH",
            )
        if qualification.status is not ReleaseQualificationStatus.QUALIFIED:
            if qualification.binding.mode is AutopilotReleaseStage.SHADOW:
                return QualifiedReleaseGateDecision(
                    ReleaseGateOutcome.REVIEW_REQUIRED,
                    "SHADOW_STAGE_ONLY",
                )
            if qualification.binding.mode is AutopilotReleaseStage.REVIEW_REQUIRED:
                return QualifiedReleaseGateDecision(
                    ReleaseGateOutcome.REVIEW_REQUIRED,
                    "RELEASE_STAGE_REQUIRES_REVIEW",
                )
            return QualifiedReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "RELEASE_QUALIFICATION_NOT_QUALIFIED",
            )
        if qualification.binding.mode in {
            AutopilotReleaseStage.SHADOW,
            AutopilotReleaseStage.REVIEW_REQUIRED,
        }:
            return QualifiedReleaseGateDecision(
                ReleaseGateOutcome.REVIEW_REQUIRED,
                "RELEASE_STAGE_REQUIRES_REVIEW",
            )
        if qualification.binding.mode is not AutopilotReleaseStage.SYNTHETIC_SANDBOX:
            return QualifiedReleaseGateDecision(
                ReleaseGateOutcome.BLOCKED,
                "RELEASE_STAGE_NOT_ENABLED_FOR_THIS_RUNTIME",
            )
        return QualifiedReleaseGateDecision(
            ReleaseGateOutcome.ALLOW_SYNTHETIC_RESERVATION,
            "SYNTHETIC_RELEASE_QUALIFIED",
        )


def summarize_rollout_observations(
    observations: Sequence[RolloutObservation],
) -> RolloutMeasurementSummary:
    total = len(observations)
    predicted_positive = 0
    true_positive = 0
    false_positive = 0
    false_negative = 0
    human_overrides_required = 0
    autonomous_writes = 0
    shadow = 0
    review_required = 0

    for item in observations:
        expected_positive = item.expected_decision is not RolloutExpectedDecision.ALLOW_WRITE
        actual_positive = item.actual_decision is not RolloutActualDecision.ALLOW_WRITE
        if item.stage is AutopilotReleaseStage.SHADOW:
            shadow += 1
        if item.stage is AutopilotReleaseStage.REVIEW_REQUIRED:
            review_required += 1
        if actual_positive:
            predicted_positive += 1
        if expected_positive and actual_positive:
            true_positive += 1
        if not expected_positive and actual_positive:
            false_positive += 1
        if expected_positive and not actual_positive:
            false_negative += 1
        if item.human_override_required:
            human_overrides_required += 1
        if item.autonomous_provider_write_attempted or (
            item.stage in {AutopilotReleaseStage.SHADOW, AutopilotReleaseStage.REVIEW_REQUIRED}
            and item.actual_decision is RolloutActualDecision.ALLOW_WRITE
        ):
            autonomous_writes += 1

    precision = 1.0 if predicted_positive == 0 else true_positive / predicted_positive
    return RolloutMeasurementSummary(
        total_observations=total,
        shadow_observations=shadow,
        review_required_observations=review_required,
        predicted_positive=predicted_positive,
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        human_overrides_required=human_overrides_required,
        autonomous_provider_write_attempts=autonomous_writes,
        decision_precision=precision,
        no_autonomous_writes=autonomous_writes == 0,
    )


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_hash(value: str, label: str) -> None:
    if not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _ensure_independent_from_runners(
    actor_id: str,
    evidence: Sequence[ReleaseQualificationEvidence],
) -> None:
    if actor_id in {item.runner_actor for item in evidence}:
        raise ValueError("release decision actor must be independent from evidence runners")


def _ensure_evidence_passes_release_boundary(
    evidence: Sequence[ReleaseQualificationEvidence],
) -> None:
    for item in evidence:
        summary = item.measurement_summary
        if not summary.no_autonomous_writes or summary.false_negative:
            raise ValueError("qualified release evidence contains a fail-open observation")


__all__ = [
    "AutopilotReleaseStage",
    "QualifiedReleaseGate",
    "QualifiedReleaseGateDecision",
    "ReleaseGateDecision",
    "ReleaseGateOutcome",
    "ReleaseQualification",
    "ReleaseQualificationActorRole",
    "ReleaseQualificationBinding",
    "ReleaseQualificationDecision",
    "ReleaseQualificationEvidence",
    "ReleaseQualificationLifecycle",
    "ReleaseQualificationRequestBinding",
    "ReleaseQualificationStatus",
    "RolloutActualDecision",
    "RolloutExpectedDecision",
    "RolloutMeasurementSummary",
    "RolloutObservation",
    "SyntheticReleaseGate",
    "SyntheticReleaseQualification",
    "summarize_rollout_observations",
]
