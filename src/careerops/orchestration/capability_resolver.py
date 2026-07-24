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
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

from careerops.config import RuntimeEnvironment, Settings
from careerops.orchestration.kernel_adapter import Capability
from careerops.orchestration.state import DraftDTO

__all__ = ["V1_EVIDENCE_REFS", "SettingsCapabilityResolver"]

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
