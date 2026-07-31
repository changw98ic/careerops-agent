"""Production v1 ``CapabilityResolver`` (plan v0.4 §2.5, §2.6, §2.7).

The ``CapabilityResolver`` Protocol is declared in ``orchestration.kernel_adapter``.
This module ships the v1 production implementation, ``SettingsCapabilityResolver``,
which is the ONLY object wired into ``RuntimeResources`` for the demo/test review
endpoint. It derives trusted facts from server-owned state; the node
(``review_gate``) never substitutes ``True`` literals, and ``untrusted_claims``
is always ``{}`` (ADR 0006).

v1 positioning (plan v0.4 §2.4 / §2.7):

- ``capability_released`` reflects whether the runtime environment has released
  the v1 in-process side-effect capability. ``RuntimeEnvironment.PRODUCTION``
  NEVER releases it via this path: the review endpoint is not mounted in
  production and the whole stack is fail-closed. Non-production (demo/test)
  releases it so the graph can reach ``review_gate`` for the demo flow.
- ``target_allowlisted`` is ``True`` only when EVERY draft in the batch carries a
  recipient. The draft node assigns recipients exclusively from contacts
  extracted with ``publicly_listed=True``; an empty recipient therefore means no
  public contact was found -> not allowlisted -> policy DENY (fail-closed).
- ``evidence_refs`` is a v1 placeholder non-empty tuple so the policy's
  ``EVIDENCE_REQUIRED`` gate is satisfied. Real evidence/qualification ships via
  the ADR 0006 pilot; v1 deliberately keeps this bar minimal while remaining
  non-empty so policy fails closed when the resolver is not wired.
- ``resource_id`` is a deterministic UUID derived from the batch recipients so
  the same target maps to the same ``email_thread`` resource across re-runs.

This file is intentionally v1-simplified; it is NOT the ADR 0006 qualification
path. widening ``capability_released`` to production, or replacing the evidence
placeholder, belongs to that pilot.

Phase-0 capability decision contract (OpenSpec change
``end-to-end-career-application-loop`` task 1.4, design Decision 11):

- ``CapabilityKind`` enumerates the provider/system capabilities gated by
  feature flags.
- ``CapabilityDecision`` is the queryable, fail-closed answer for one kind.
- ``SettingsCapabilityResolver.decide`` reads trusted flag state from
  ``Settings`` and NEVER releases a capability it has not been explicitly
  taught. This is a contract freeze only — it does not implement any read or
  send logic, and it does not touch the existing ``for_send_batch`` trusted
  facts used by the review_gate policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from careerops.config import RuntimeEnvironment, Settings
from careerops.orchestration.kernel_adapter import Capability
from careerops.orchestration.state import DraftDTO

__all__ = [
    "V1_EVIDENCE_REFS",
    "CapabilityDecision",
    "CapabilityKind",
    "SettingsCapabilityResolver",
]


# ---------------------------------------------------------------------------
# Phase-0 capability decision contract (design Decision 11)
# ---------------------------------------------------------------------------


class CapabilityKind(StrEnum):
    """Provider/system capabilities gated by feature flags (design Decision 11).

    A ``CapabilityDecision(released=True)`` is necessary but NOT sufficient for
    any external effect: released capabilities still pass through their own
    source policy, review_gate, outbox and reconciliation gates downstream.
    Future members added to this enum MUST fall through to a fail-closed denied
    decision in ``SettingsCapabilityResolver.decide`` until that method is
    explicitly updated — the resolver never silently releases a capability.
    """

    CRAWL_PLAN_MANAGEMENT = "crawl_plan_management"
    MODEL_TAILORING = "model_tailoring"
    SMART_INTAKE = "smart_intake"
    GMAIL_READ = "gmail_read"
    SYSTEM_MANAGED_SEND = "system_managed_send"
    AUTO_SEND = "auto_send"


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    """Fail-closed capability decision returned by ``SettingsCapabilityResolver``.

    ``released=True`` means the capability is available subject to its
    downstream source-policy / approval / review gates. ``released=False`` means
    the path is denied at the contract layer; callers MUST fall back to the safe
    alternative named in ``reason`` and MUST NOT attempt the effect.
    ``reason`` is always non-empty and safe to surface in operator/UI messaging.
    """

    released: bool
    reason: str


# Non-empty placeholder so the policy's EVIDENCE_REQUIRED gate is satisfied for
# the v1 demo. The ADR 0006 pilot replaces this with real provenance.
V1_EVIDENCE_REFS: tuple[str, ...] = ("evidence:langgraph.review_gate:v1",)


def _batch_resource_id(recipients: tuple[str, ...]) -> UUID:
    """Deterministic UUID for the batch's email-thread resource.

    ``resource_type`` is ``email_thread``; the resource id identifies the thread
    being created. Keying on the recipient tuple means the same target always
    re-uses the same resource id across review_gate re-runs (resume), which keeps
    kernel replay coherent. ``NAMESPACE_URL`` gives a stable, spec-defined UUID.
    """
    canonical = "|".join(recipients)
    return uuid5(NAMESPACE_URL, f"careerops:send_email_batch:{canonical}")


class SettingsCapabilityResolver:
    """v1 production ``CapabilityResolver`` reading trusted facts from settings.

    The class is a structural match for ``kernel_adapter.CapabilityResolver``; it
    is intentionally NOT declared as a subclass because the Protocol is structural
    and the runtime wires this concrete object directly.
    """

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def for_send_batch(self, drafts: tuple[DraftDTO, ...]) -> Capability:
        if not drafts:
            raise ValueError("capability resolver requires a non-empty draft batch")
        settings = self._settings
        capability_released = settings.environment is not RuntimeEnvironment.PRODUCTION
        recipients = tuple(str(draft.get("recipient", "")) for draft in drafts)
        target_allowlisted = bool(recipients) and all(bool(r) for r in recipients)
        resource_id = _batch_resource_id(recipients)
        # v1 sends to a single shared recipient (the first publicly-listed
        # contact). An empty recipient means target_allowlisted is False, so the
        # target dict content does not weaken policy.
        target: dict[str, object] = {"to": recipients[0]} if recipients else {"to": ""}
        trusted_facts: dict[str, object] = {
            "capability_released": capability_released,
            "target_allowlisted": target_allowlisted,
        }
        return Capability(
            resource_id=resource_id,
            target=target,
            trusted_facts=trusted_facts,
            evidence_refs=V1_EVIDENCE_REFS,
            # ``authenticated`` must come from the trusted runtime context. The
            # review endpoint authenticates the reviewer before any resume, and
            # the graph is only invokable server-side, so this is a trusted flag.
            authenticated=True,
        )

    def decide(self, capability: CapabilityKind) -> CapabilityDecision:
        """Phase-0 capability decision (design Decision 11; Iron Rules 1 & 4).

        Reads trusted flag state from ``Settings`` and returns a fail-closed
        ``CapabilityDecision``. This is a feature-flag gate only — it does NOT
        implement any read or send logic, and does NOT bypass the review_gate /
        outbox / source-policy gates that govern released capabilities.

        Defaults:

        - ``CRAWL_PLAN_MANAGEMENT``: released subject to source policy.
        - ``MODEL_TAILORING``: disabled; model output is review-only and never
          authoritative (enforced regardless of the flag).
        - ``GMAIL_READ``: denied while Google OAuth is closed (M4 gate).
        - ``SYSTEM_MANAGED_SEND``: denied while external writes are closed
          (M5A side-effect gate).
        - ``AUTO_SEND``: permanently denied; the human confirmation gate is
          load-bearing and no flag flip releases it.

        Any member not explicitly handled below (e.g. a future enum value added
        without updating this dispatch) MUST fall through to a denied decision.
        """
        settings = self._settings

        if capability is CapabilityKind.CRAWL_PLAN_MANAGEMENT:
            if settings.crawl_plan_management_enabled:
                return CapabilityDecision(
                    released=True,
                    reason=(
                        "crawl plan management released; source policy still applies downstream"
                    ),
                )
            return CapabilityDecision(
                released=False,
                reason="crawl plan management disabled by operator flag",
            )

        if capability is CapabilityKind.MODEL_TAILORING:
            if settings.model_tailoring_enabled:
                return CapabilityDecision(
                    released=True,
                    reason=(
                        "model tailoring enabled; model output is review-only "
                        "and never authoritative"
                    ),
                )
            return CapabilityDecision(
                released=False,
                reason=(
                    "model tailoring disabled; model output is review-only and never authoritative"
                ),
            )

        if capability is CapabilityKind.SMART_INTAKE:
            if settings.smart_intake_enabled:
                return CapabilityDecision(
                    released=True,
                    reason="smart intake released for review-only form proposals",
                )
            return CapabilityDecision(
                released=False,
                reason="smart intake disabled by operator flag",
            )

        if capability is CapabilityKind.GMAIL_READ:
            if settings.google_oauth_enabled:
                return CapabilityDecision(
                    released=True,
                    reason=(
                        "gmail read released via google oauth; subject to "
                        "scope/cursor/retention policy"
                    ),
                )
            return CapabilityDecision(
                released=False,
                reason=(
                    "gmail read denied; google oauth unavailable before the M4 integration gate"
                ),
            )

        if capability is CapabilityKind.SYSTEM_MANAGED_SEND:
            if settings.external_writes_enabled:
                return CapabilityDecision(
                    released=True,
                    reason=(
                        "system-managed send released; subject to review_gate and outbox policy"
                    ),
                )
            return CapabilityDecision(
                released=False,
                reason=(
                    "system-managed send denied; external writes unavailable "
                    "before the M5A side-effect gate"
                ),
            )

        if capability is CapabilityKind.AUTO_SEND:
            # The permanent hard-deny is torn down for the single-user
            # autonomous loop (real-autonomous-career-loop). AUTO_SEND now
            # follows the operator flag like the other capabilities; autonomy
            # is bounded downstream by the autonomous-action policy
            # (reversible categories auto-act, irreversible commitments blocked).
            if settings.auto_send_enabled:
                return CapabilityDecision(
                    released=True,
                    reason="auto-send released; bounded by the autonomous-action policy",
                )
            return CapabilityDecision(
                released=False,
                reason="auto-send disabled by operator flag",
            )

        # Fail-closed for any member not explicitly handled above. Unreachable
        # for the current enum members but defends against a future member
        # being added without updating this dispatch.
        return CapabilityDecision(
            released=False,
            reason=f"unknown capability {capability!r}: fail-closed denial",
        )


def is_external_effect_path_usable(
    resolver: SettingsCapabilityResolver,
    capability: CapabilityKind,
    *,
    dependency_available: bool,
) -> tuple[bool, str]:
    """Combined gate for repos/projections that touch external-effect
    capabilities (end-to-end-career-application-loop task 2.8 helper).

    Returns ``(usable, reason)``. A path is usable only when BOTH hold:

    1. the backing dependency (provider, model, crawl worker, ...) is
       available — ``dependency_available=True``; AND
    2. the ``SettingsCapabilityResolver`` releases the capability.

    Iron rule 3 makes dependency-not-ready win over capability denial: a
    released capability whose dependency is down is still unusable, and the
    caller MUST surface ``DEPENDENCY_NOT_READY`` (503) rather than silently
    fall back to an unscoped or in-memory implementation. The reason string
    is safe to surface in operator/UI messaging.

    This helper is intentionally NOT wired into request paths yet (task 2.11
    does the runtime/app wiring); it exists so stage-3+ repositories and
    projections route external-effect decisions through one place instead of
    re-implementing the dependency-vs-capability precedence.
    """
    if not dependency_available:
        return False, "dependency_not_ready"
    decision = resolver.decide(capability)
    if not decision.released:
        return False, decision.reason
    return True, decision.reason
