"""M5A side-effect policy: trusted-facts-only decision making.

The policy engine consults ONLY trusted business state (``trusted_facts``) and
evidence references (``evidence_refs``). It NEVER consults the model's
self-assertion of safety (``untrusted_claims``); that field is recorded solely
to evidence that it was ignored.

Default-deny invariants:
- Unknown action kinds deny.
- External writes deny unless the capability is explicitly released.
- External writes with a released capability require approval and evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from careerops.policy.rules import (
    EXTERNAL_WRITE_ACTIONS,
    INTERNAL_ACTIONS,
    READ_ACTIONS,
)

M5A_RULESET_VERSION = "m5a.side-effect.v1"


class SideEffectPolicyOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class SideEffectPolicyInput:
    """Policy input restricted to trusted business state and evidence refs.

    ``untrusted_claims`` is accepted only so the kernel can record that it was
    ignored; the decider never reads it.
    """

    action_kind: str
    authenticated: bool
    trusted_facts: dict[str, object] = field(default_factory=lambda: {})
    evidence_refs: tuple[str, ...] = ()
    untrusted_claims: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class SideEffectPolicyDecision:
    outcome: SideEffectPolicyOutcome
    reason_codes: tuple[str, ...]
    ruleset_version: str = M5A_RULESET_VERSION


class SideEffectPolicyDecider:
    """Decide side-effect eligibility from trusted facts only."""

    def decide(self, policy_input: SideEffectPolicyInput) -> SideEffectPolicyDecision:
        if not policy_input.authenticated:
            return SideEffectPolicyDecision(
                SideEffectPolicyOutcome.DENY, ("AUTHENTICATION_REQUIRED",)
            )

        action_kind = policy_input.action_kind
        if action_kind in READ_ACTIONS:
            return SideEffectPolicyDecision(SideEffectPolicyOutcome.ALLOW, ("AUTHENTICATED_READ",))
        if action_kind in INTERNAL_ACTIONS:
            return SideEffectPolicyDecision(SideEffectPolicyOutcome.ALLOW, ("INTERNAL_ONLY",))

        if action_kind in EXTERNAL_WRITE_ACTIONS:
            return self._decide_external_write(policy_input)

        return SideEffectPolicyDecision(SideEffectPolicyOutcome.DENY, ("UNKNOWN_ACTION",))

    def _decide_external_write(
        self, policy_input: SideEffectPolicyInput
    ) -> SideEffectPolicyDecision:
        capability_released = bool(policy_input.trusted_facts.get("capability_released", False))
        if not capability_released:
            return SideEffectPolicyDecision(
                SideEffectPolicyOutcome.DENY, ("CAPABILITY_NOT_RELEASED",)
            )

        target_allowlisted = bool(policy_input.trusted_facts.get("target_allowlisted", False))
        if not target_allowlisted:
            return SideEffectPolicyDecision(
                SideEffectPolicyOutcome.DENY, ("TARGET_NOT_ALLOWLISTED",)
            )

        if not policy_input.evidence_refs:
            return SideEffectPolicyDecision(SideEffectPolicyOutcome.DENY, ("EVIDENCE_REQUIRED",))

        return SideEffectPolicyDecision(
            SideEffectPolicyOutcome.REQUIRE_APPROVAL,
            ("EXTERNAL_WRITE_REQUIRES_APPROVAL",),
        )
