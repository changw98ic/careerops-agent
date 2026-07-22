"""Pure, fail-closed policy evaluation for a future bounded-autopilot capability.

This module deliberately has no dependency on the M0 ``PolicyEngine``, database, outbox,
browser, credentials, or provider worker. Callers must construct its inputs from immutable,
server-side state; request bodies, model output, and page content are not policy facts.
``ALLOW_AUTOPILOT_SUBMISSION`` is not interchangeable with the existing generic policy
``allow`` outcome and cannot cause an external effect on its own. ``REQUIRE_APPROVAL`` creates
a human-review candidate only; it cannot be promoted to autonomous execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

AUTOPILOT_RULE_VERSION = "autopilot-grant.v1"
AUTOPILOT_SUBMISSION_ACTION_KINDS = frozenset({"submit_application"})


class AutopilotGrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class SiteAutomationPolicy(StrEnum):
    ALLOWED = "allowed"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"


class AutopilotOutcome(StrEnum):
    ALLOW_AUTOPILOT_SUBMISSION = "allow_autopilot_submission"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def _empty_hard_stop_categories() -> frozenset[str]:
    return frozenset()


@dataclass(frozen=True, slots=True)
class AutopilotGrant:
    """Immutable campaign scope. Updating it requires a new version outside this module."""

    id: UUID
    actor_id: UUID
    campaign_id: UUID
    version: int
    action_kinds: frozenset[str]
    channels: frozenset[str]
    target_hosts: frozenset[str]
    approved_material_hashes: frozenset[str]
    max_submissions: int
    max_submissions_per_day: int | None
    expires_at: datetime
    policy_version: str
    release_qualification_version: str
    status: AutopilotGrantStatus = AutopilotGrantStatus.ACTIVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_kinds", frozenset(self.action_kinds))
        object.__setattr__(self, "channels", frozenset(self.channels))
        object.__setattr__(self, "target_hosts", frozenset(self.target_hosts))
        object.__setattr__(
            self, "approved_material_hashes", frozenset(self.approved_material_hashes)
        )


@dataclass(frozen=True, slots=True)
class AutopilotSubmissionFacts:
    """Trusted, immutable facts for one proposed application submission."""

    authenticated: bool
    actor_id: UUID
    campaign_id: UUID
    action_kind: str
    channel: str
    target_host: str
    payload_hash: str
    policy_payload_hash: str
    material_hashes: frozenset[str]
    target_matches_campaign: bool
    evidence_complete: bool
    site_policy: SiteAutomationPolicy
    adapter_release_qualified: bool
    credentials_active: bool
    policy_version: str
    release_qualification_version: str
    confirmed_submission_count: int
    confirmed_submission_count_today: int
    duplicate_or_ambiguous: bool = False
    hard_stop_categories: frozenset[str] = field(default_factory=_empty_hard_stop_categories)
    global_kill_switch_active: bool = False
    campaign_kill_switch_active: bool = False
    provider_kill_switch_active: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "material_hashes", frozenset(self.material_hashes))
        object.__setattr__(self, "hard_stop_categories", frozenset(self.hard_stop_categories))


@dataclass(frozen=True, slots=True)
class AutopilotDecision:
    outcome: AutopilotOutcome
    reason_code: str
    rule_version: str = AUTOPILOT_RULE_VERSION


class AutopilotPolicyEngine:
    """Evaluate a campaign grant without reserving capacity or executing a provider call."""

    def decide(
        self,
        grant: AutopilotGrant,
        facts: AutopilotSubmissionFacts,
        *,
        now: datetime,
    ) -> AutopilotDecision:
        if not facts.authenticated:
            return self._deny("AUTHENTICATION_REQUIRED")
        if not _is_timezone_aware(now) or not _is_timezone_aware(grant.expires_at):
            return self._deny("INVALID_DECISION_TIME")
        if not _has_valid_grant_configuration(grant):
            return self._deny("INVALID_GRANT_CONFIGURATION")
        if grant.status is not AutopilotGrantStatus.ACTIVE:
            return self._deny("GRANT_NOT_ACTIVE")
        if now >= grant.expires_at:
            return self._deny("GRANT_EXPIRED")
        if facts.actor_id != grant.actor_id:
            return self._deny("GRANT_ACTOR_MISMATCH")
        if facts.campaign_id != grant.campaign_id:
            return self._deny("GRANT_CAMPAIGN_MISMATCH")
        if facts.action_kind not in AUTOPILOT_SUBMISSION_ACTION_KINDS:
            return self._deny("ACTION_NOT_AUTOPILOT_ELIGIBLE")
        if facts.action_kind not in grant.action_kinds:
            return self._deny("ACTION_NOT_GRANTED")
        if facts.channel not in grant.channels:
            return self._deny("CHANNEL_NOT_GRANTED")
        if facts.target_host not in grant.target_hosts:
            return self._deny("TARGET_HOST_NOT_GRANTED")
        if facts.policy_version != grant.policy_version:
            return self._deny("STALE_POLICY_VERSION")
        if facts.release_qualification_version != grant.release_qualification_version:
            return self._deny("STALE_RELEASE_QUALIFICATION")
        if facts.global_kill_switch_active:
            return self._deny("GLOBAL_KILL_SWITCH_ACTIVE")
        if facts.campaign_kill_switch_active:
            return self._deny("CAMPAIGN_KILL_SWITCH_ACTIVE")
        if facts.provider_kill_switch_active:
            return self._deny("PROVIDER_KILL_SWITCH_ACTIVE")
        if not facts.adapter_release_qualified:
            return self._deny("ADAPTER_NOT_RELEASE_QUALIFIED")
        if not facts.credentials_active:
            return self._deny("CREDENTIALS_UNAVAILABLE")
        if not _is_sha256_hex(facts.payload_hash) or not _is_sha256_hex(facts.policy_payload_hash):
            return self._deny("INVALID_PAYLOAD_HASH")
        if facts.payload_hash != facts.policy_payload_hash:
            return self._deny("PAYLOAD_HASH_MISMATCH")
        if facts.confirmed_submission_count < 0 or facts.confirmed_submission_count_today < 0:
            return self._deny("INVALID_SUBMISSION_COUNTS")
        if facts.confirmed_submission_count >= grant.max_submissions:
            return self._deny("GRANT_CAP_EXHAUSTED")
        if (
            grant.max_submissions_per_day is not None
            and facts.confirmed_submission_count_today >= grant.max_submissions_per_day
        ):
            return self._deny("DAILY_CAP_EXHAUSTED")
        if facts.site_policy is SiteAutomationPolicy.PROHIBITED:
            return self._review("SITE_AUTOMATION_PROHIBITED")
        if facts.duplicate_or_ambiguous:
            return self._review("DUPLICATE_OR_AMBIGUOUS_SUBMISSION")
        if facts.hard_stop_categories:
            return self._review("HARD_STOP_REQUIRES_REVIEW")
        if facts.site_policy is not SiteAutomationPolicy.ALLOWED:
            return self._review("SITE_POLICY_UNKNOWN")
        if not facts.target_matches_campaign:
            return self._review("TARGET_OUTSIDE_CAMPAIGN")
        if not facts.evidence_complete:
            return self._review("EVIDENCE_INCOMPLETE")
        if not facts.material_hashes:
            return self._review("MATERIAL_EVIDENCE_MISSING")
        if not facts.material_hashes.issubset(grant.approved_material_hashes):
            return self._review("UNAPPROVED_CAMPAIGN_MATERIAL")

        return AutopilotDecision(
            AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
            "AUTOPILOT_GRANT_MATCHED",
        )

    @staticmethod
    def _deny(reason_code: str) -> AutopilotDecision:
        return AutopilotDecision(AutopilotOutcome.DENY, reason_code)

    @staticmethod
    def _review(reason_code: str) -> AutopilotDecision:
        return AutopilotDecision(AutopilotOutcome.REQUIRE_APPROVAL, reason_code)


def _has_valid_grant_configuration(grant: AutopilotGrant) -> bool:
    return (
        grant.version > 0
        and bool(grant.action_kinds)
        and bool(grant.channels)
        and bool(grant.target_hosts)
        and bool(grant.approved_material_hashes)
        and all(grant.action_kinds)
        and all(grant.channels)
        and all(grant.target_hosts)
        and all(_is_sha256_hex(value) for value in grant.approved_material_hashes)
        and grant.max_submissions > 0
        and (grant.max_submissions_per_day is None or grant.max_submissions_per_day > 0)
        and bool(grant.policy_version)
        and bool(grant.release_qualification_version)
    )


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(
        "0" <= character <= "9" or "a" <= character <= "f" for character in value
    )
