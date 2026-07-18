from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from careerops.policy.rules import (
    EXTERNAL_WRITE_ACTIONS,
    INTERNAL_ACTIONS,
    READ_ACTIONS,
    RULE_VERSION,
)


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ActionProposal:
    action_kind: str
    authenticated: bool
    trusted_facts: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    untrusted_claims: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason_code: str
    rule_version: str = RULE_VERSION


class PolicyEngine:
    """M0 rules: authenticated reads/internal drafts only; every external write denies."""

    def decide(self, proposal: ActionProposal) -> PolicyDecision:
        if not proposal.authenticated:
            return PolicyDecision(PolicyOutcome.DENY, "AUTHENTICATION_REQUIRED")
        if proposal.action_kind in READ_ACTIONS:
            return PolicyDecision(PolicyOutcome.ALLOW, "AUTHENTICATED_READ")
        if proposal.action_kind in INTERNAL_ACTIONS:
            return PolicyDecision(PolicyOutcome.ALLOW, "INTERNAL_ONLY")
        if proposal.action_kind in EXTERNAL_WRITE_ACTIONS:
            return PolicyDecision(PolicyOutcome.DENY, "CAPABILITY_NOT_RELEASED")
        return PolicyDecision(PolicyOutcome.DENY, "UNKNOWN_ACTION")
