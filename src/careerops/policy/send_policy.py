"""M6 send policy: category-aware auto-send decisions.

Extends the M5A side-effect policy with Gmail send category rules:
- Auto-send candidates require the four-layer gate to pass.
- Approval-required categories always need human approval.
- Permanent-deny categories can NEVER auto-send; no rule overrides this.
- No-source contact, low-trust sender, no Application = no auto-send.
- Prompt Injection cannot change recipient, payload, Policy, or tools.

The policy consults ONLY trusted business state. Model self-assertion is
recorded as ignored and never consulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from careerops.domain.send import (
    AUTO_SEND_CANDIDATES,
    PERMANENT_DENY_CATEGORIES,
    SendCategory,
)

M6_RULESET_VERSION = "m6.send-policy.v1"


class SendPolicyOutcome(StrEnum):
    AUTO_SEND_ALLOWED = "auto_send_allowed"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class SendPolicyInput:
    """Policy input for M6 send decisions. All fields are trusted business state."""

    category: SendCategory
    has_source_contact: bool
    sender_trust_level: str  # "high", "medium", "low", "unknown"
    has_application_linkage: bool
    contact_domain_matches: bool
    four_layer_gate_passed: bool
    evidence_refs: tuple[str, ...] = ()
    untrusted_claims: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class SendPolicyDecision:
    outcome: SendPolicyOutcome
    reason_codes: tuple[str, ...]
    ruleset_version: str = M6_RULESET_VERSION


class SendPolicyDecider:
    """Decide send eligibility from trusted facts and category rules only.

    Invariants:
    - Permanent-deny categories ALWAYS deny auto-send regardless of any gate.
    - Approval-required categories ALWAYS require human approval.
    - Auto-send candidates require four-layer gate + eligibility context.
    - Untrusted claims are never consulted.
    """

    def decide(self, policy_input: SendPolicyInput) -> SendPolicyDecision:
        category = policy_input.category

        # Layer 1: Permanent deny - no rule can override
        if category in PERMANENT_DENY_CATEGORIES:
            return SendPolicyDecision(
                SendPolicyOutcome.DENY,
                (f"PERMANENT_DENY_{category.value.upper()}", "NO_OVERRIDE_POSSIBLE"),
            )

        # Layer 2: Approval-required categories
        if category not in AUTO_SEND_CANDIDATES:
            return SendPolicyDecision(
                SendPolicyOutcome.REQUIRE_APPROVAL,
                ("CATEGORY_REQUIRES_HUMAN_APPROVAL",),
            )

        # Layer 3: Eligibility context checks (trusted state only)
        eligibility_reasons = self._check_eligibility(policy_input)
        if eligibility_reasons:
            return SendPolicyDecision(
                SendPolicyOutcome.REQUIRE_APPROVAL,
                eligibility_reasons,
            )

        # Layer 4: Four-layer gate
        if not policy_input.four_layer_gate_passed:
            return SendPolicyDecision(
                SendPolicyOutcome.REQUIRE_APPROVAL,
                ("FOUR_LAYER_GATE_NOT_PASSED",),
            )

        # All checks pass: auto-send allowed
        return SendPolicyDecision(
            SendPolicyOutcome.AUTO_SEND_ALLOWED,
            ("AUTO_SEND_APPROVED",),
        )

    def _check_eligibility(self, policy_input: SendPolicyInput) -> tuple[str, ...]:
        """Check trusted eligibility context. Returns denial reasons if any."""
        reasons: list[str] = []

        if not policy_input.has_source_contact:
            reasons.append("NO_SOURCE_CONTACT")

        if policy_input.sender_trust_level in ("low", "unknown"):
            reasons.append("LOW_TRUST_SENDER")

        if not policy_input.has_application_linkage:
            reasons.append("NO_APPLICATION_LINKAGE")

        if not policy_input.contact_domain_matches:
            reasons.append("CONTACT_DOMAIN_MISMATCH")

        if not policy_input.evidence_refs:
            reasons.append("EVIDENCE_REQUIRED")

        return tuple(reasons)
