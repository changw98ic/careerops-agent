"""``review_gate`` node + capability resolver (plan v0.4 §2.5, §2.6, §2.7).

The per-message human-approval gate has been replaced (real-autonomous-career-loop
design D2) by a dual-model OA approval loop. The ``review_gate`` node now:

1. Builds an idempotent ``ProposalInput`` whose idempotency key embeds
   ``review_revision`` and the canonical payload hash, so the same draft set
   always re-uses the same intent.
2. Runs the autonomous A/B approval loop (``ABApprovalLoop``) over each draft:
   drafter A refines the template skeleton into a sendable email; reviewer B
   reviews A's draft along an orthogonal axis. B approves -> the draft may send;
   B rejects -> A revises; at most 5 rounds, else escalate.
3. Calls ``kernel.propose`` (idempotent on the key) and
   ``kernel.get_or_create_pending_approval`` (atomic find-or-create under the
   kernel RLock). Both are safe to repeat on graph re-runs.
4. Persists ``thread_id <-> approval_id <-> intent_id`` via
   ``ReviewMappingStore.put_if_absent``.
5. If EVERY draft's A/B loop approved: calls ``kernel.decide_approval(approve,
   actor_type=AGENT)`` (task 3.7: agent-initiated sends are recorded as AGENT)
   and routes to ``send`` — no human click, no interrupt.
6. If ANY draft escalated (non-convergence, model unavailable, provider error):
   leaves the approval PENDING and routes to END, so the human-review fallback
   (``POST /api/v1/review/{approval_id}``) can still decide it. The send does
   NOT proceed autonomously (default-deny on uncertainty).

``untrusted_claims`` is ALWAYS ``{}``. The capability resolver provides trusted
facts only; it is forbidden to substitute ``True`` literals. ``parse_review_decision``
is retained for the human-review fallback endpoint that resumes an escalated
approval.
"""

# langgraph ships without bundled pyright stubs; suppress the missing-stub
# warning only (Unknown-family errors are still reported because the rest of
# this file is fully typed).
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Optional, Protocol, cast
from uuid import UUID

from langchain_core.runnables.config import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command

from careerops.application.audit import AuditActorType
from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.model_gateway.base import StructuredModelClient
from careerops.orchestration.mapping_store import (
    ReviewMappingStore,
)
from careerops.orchestration.state import (
    CareerOpsState,
    DraftDTO,
    EditedDraftItem,
    EditedDraftPayload,
)

__all__ = [
    "Capability",
    "CapabilityResolver",
    "ReviewDecision",
    "ReviewDecisionError",
    "drafts_payload_hash",
    "parse_review_decision",
    "review_gate",
    "review_gate_ab",
    "review_idempotency_key",
]


# ---------------------------------------------------------------------------
# Capability resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capability:
    """Trusted-facts bundle returned by ``CapabilityResolver``.

    The policy (``policy/side_effect_policy.py``) demands three things before
    an external write can leave REQUIRE_APPROVAL: ``capability_released`` /
    ``target_allowlisted`` / non-empty ``evidence_refs``. The resolver produces
    these from trusted business state; the node never substitutes literals.
    """

    resource_id: UUID
    target: dict[str, object]
    trusted_facts: dict[str, object]
    evidence_refs: tuple[str, ...]
    authenticated: bool


class CapabilityResolver(Protocol):
    """Build a capability bundle for a batch of drafts.

    v1 implementation: ``Stage1DemoCapabilityResolver`` (in this module) reads
    ``capability_released`` from settings, treats the recipient as allowlisted
    only when the contact was extracted with ``publicly_listed=True``, and uses
    a v1 placeholder evidence tuple. Real evidence/qualification ships via the
    ADR 0006 pilot; v1 deliberately keeps the bar at "non-empty evidence_refs
    so policy fails closed when the resolver is not wired".
    """

    def for_send_batch(self, drafts: tuple[DraftDTO, ...]) -> Capability: ...


# ---------------------------------------------------------------------------
# Idempotency / hashing
# ---------------------------------------------------------------------------


def drafts_payload_hash(drafts: tuple[DraftDTO, ...]) -> str:
    """Stable SHA-256 over the draft batch.

    The hash covers ``id`` / ``recipient`` / ``subject`` / ``body`` for every
    draft, in order. Any edit to subject/body, or any recipient change, produces
    a new hash; combined with ``review_revision`` this becomes the propose
    idempotency key.
    """
    payload = [
        {
            "id": d.get("id", ""),
            "recipient": d.get("recipient", ""),
            "subject": d.get("subject", ""),
            "body": d.get("body", ""),
        }
        for d in drafts
    ]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_idempotency_key(
    *, requested_for: str, review_revision: int, drafts: tuple[DraftDTO, ...]
) -> str:
    """Compose the propose idempotency key.

    Format: ``langgraph.review_gate:{requested_for}:{revision}:{drafts_hash}``.
    Embedding the revision means an edit loop (revision += 1) automatically
    creates a new intent; embedding the drafts hash means any payload byte
    change creates a new intent.
    """
    return f"langgraph.review_gate:{requested_for}:r{review_revision}:{drafts_payload_hash(drafts)}"


# ---------------------------------------------------------------------------
# Review decision parsing
# ---------------------------------------------------------------------------


ReviewAction = Literal["approve", "reject", "edit"]


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Internal representation of a reviewer decision.

    ``approval_id`` is parsed back to ``UUID`` and compared against the
    approval that the gate is currently interrupted on. ``edited_drafts`` is
    non-empty only for the ``edit`` action.
    """

    action: ReviewAction
    approval_id: UUID
    edited_payload: EditedDraftPayload | None


class ReviewDecisionError(ValueError):
    """Raised when a resume payload does not satisfy the constrained schema."""


def parse_review_decision(raw: object, *, expected_approval_id: UUID) -> ReviewDecision:
    """Parse a JSON-safe resume payload into a validated ``ReviewDecision``.

    Constraints enforced here are the transport-layer safety net (plan v0.4
    §2.5). The review endpoint applies the same constraints server-side; this
    in-graph check defends in depth against a misbehaving caller.

    - ``action`` must be one of ``approve`` / ``reject`` / ``edit``.
    - ``approval_id`` must be a valid UUID and match the interrupted approval.
    - ``approve`` / ``reject`` must NOT carry ``edited_drafts``.
    - ``edit`` must carry ``edited_drafts`` with each item having only
      ``id`` / ``subject`` / ``body`` (no recipient / target).
    """
    if not isinstance(raw, dict):
        raise ReviewDecisionError("resume payload must be a JSON object")
    payload: dict[str, object] = cast(dict[str, object], raw)
    action_raw = payload.get("action")
    if not isinstance(action_raw, str) or action_raw not in (
        "approve",
        "reject",
        "edit",
    ):
        raise ReviewDecisionError("action must be one of approve/reject/edit")
    # isinstance check above narrows action_raw to ``str`` and the membership
    # check narrows it further to one of the three ReviewAction literals.
    action: ReviewAction = action_raw
    approval_raw = payload.get("approval_id")
    if not isinstance(approval_raw, str):
        raise ReviewDecisionError("approval_id must be a string")
    try:
        approval_id = UUID(approval_raw)
    except (ValueError, TypeError) as exc:
        raise ReviewDecisionError("approval_id is not a valid UUID") from exc
    if approval_id != expected_approval_id:
        raise ReviewDecisionError("approval_id does not match the interrupted approval")
    edited_payload: EditedDraftPayload | None = None
    if action == "edit":
        items_raw = payload.get("edited_drafts")
        if not isinstance(items_raw, list) or not items_raw:
            raise ReviewDecisionError("edit requires non-empty edited_drafts")
        items: list[EditedDraftItem] = []
        raw_items: list[object] = cast("list[object]", items_raw)
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ReviewDecisionError("each edited_drafts item must be an object")
            item: dict[str, object] = cast(dict[str, object], raw_item)
            bad = set(item.keys()) - {"id", "subject", "body"}
            if bad:
                raise ReviewDecisionError(f"edited_drafts item has forbidden keys: {sorted(bad)}")
            if not isinstance(item.get("id"), str):
                raise ReviewDecisionError("edited_drafts item.id must be a string")
            if not isinstance(item.get("subject", ""), str):
                raise ReviewDecisionError("edited_drafts item.subject must be a string")
            if not isinstance(item.get("body", ""), str):
                raise ReviewDecisionError("edited_drafts item.body must be a string")
            items.append(
                EditedDraftItem(
                    id=item["id"],  # type: ignore[typeddict-item]
                    subject=item.get("subject", ""),  # type: ignore[typeddict-item]
                    body=item.get("body", ""),  # type: ignore[typeddict-item]
                )
            )
        edited_payload = EditedDraftPayload(drafts=tuple(items))
    else:
        if "edited_drafts" in payload:
            raise ReviewDecisionError("approve/reject must not carry edited_drafts")
    return ReviewDecision(action=action, approval_id=approval_id, edited_payload=edited_payload)


# ---------------------------------------------------------------------------
# review_gate node
# ---------------------------------------------------------------------------


def _draft_to_payload_entry(draft: DraftDTO) -> dict[str, object]:
    return {
        "id": draft.get("id", ""),
        "job_external_id": draft.get("job_external_id", ""),
        "recipient": draft.get("recipient", ""),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "revision": draft.get("revision", 0),
    }


def _draft_to_context(draft: DraftDTO, state: CareerOpsState) -> "DraftContext":
    """Build the trusted ``DraftContext`` A grounds its draft on.

    ``recipient`` / ``job_external_id`` come from the draft (trusted, extracted
    by upstream nodes). Job title / company / resume summary are read from
    trusted graph state. There is no inbound excerpt for the application-send
    path (the reply path would populate ``inbound_excerpt``).
    """
    from careerops.application.approval_loop import DraftContext
    job_title = ""
    company = ""
    jobs = state.get("filtered_jobs") or state.get("raw_job_records") or ()
    target_id = draft.get("job_external_id", "")
    for job in jobs:
        if job.get("external_id", "") == target_id:
            job_title = str(job.get("title", ""))
            raw = job.get("raw_data")
            if isinstance(raw, dict):
                company = str(raw.get("company", "") or raw.get("company_name", ""))
            break
    return DraftContext(
        recipient=draft.get("recipient", ""),
        job_title=job_title,
        company=company,
        resume_summary=str(state.get("resume_text", "") or ""),
        inbound_excerpt="",
        intent="application",
    )


def review_gate_ab(
    state: CareerOpsState,
    drafts: tuple[DraftDTO, ...],
    *,
    model_client: StructuredModelClient,
    capability_resolver: CapabilityResolver,
    trace_id: str = "",
) -> tuple[tuple[DraftDTO, ...], tuple["ApprovalLoopResult", ...], bool]:
    """Run the autonomous A/B approval loop over each draft.

    Returns ``(final_drafts, results, all_approved)``:

    - ``final_drafts``: the draft tuple with each draft's subject/body replaced
      by A's final output (the template skeleton when A could not produce one).
    - ``results``: one ``ApprovalLoopResult`` per draft, in order.
    - ``all_approved``: True only when every draft's loop approved.

    The loop itself is default-deny: any uncertainty escalates that draft
    rather than auto-sending (see ``approval_loop.ABApprovalLoop``).
    """
    from careerops.application.approval_loop import (
        ABApprovalLoop,
        ApprovalLoopResult,
        DraftResult,
    )

    loop = ABApprovalLoop(client=model_client, capability_resolver=capability_resolver)
    final: list[DraftDTO] = []
    results: list[ApprovalLoopResult] = []
    all_approved = True
    for draft in drafts:
        skeleton = DraftResult(
            subject=draft.get("subject", ""),
            body=draft.get("body", ""),
        )
        context = _draft_to_context(draft, state)
        result = loop.run(skeleton, context, trace_id=trace_id)
        results.append(result)
        if result.outcome != "approved":
            all_approved = False
        merged = dict(draft)
        merged["subject"] = result.subject
        merged["body"] = result.body
        merged["revision"] = int(draft.get("revision", 0)) + result.rounds
        final.append(DraftDTO(merged))  # type: ignore[typeddict-item]
    return tuple(final), tuple(results), all_approved


def review_gate(
    state: CareerOpsState,
    *,
    kernel: SideEffectKernel,
    review_mapping: ReviewMappingStore,
    capability_resolver: CapabilityResolver,
    model_client: StructuredModelClient | None = None,
    config: Optional[RunnableConfig] = None,  # noqa: UP045  -- LangGraph matches the literal string "Optional[RunnableConfig]"
    now: Any | None = None,
    end_node: str = END,
    send_node: str = "send",
    draft_node: str = "draft",
) -> Command[Any]:
    """LangGraph review_gate node, driven by the autonomous A/B approval loop.

    Inputs are passed as keyword args via ``functools.partial`` (or any
    closure-equivalent) when the graph is built in ``orchestration.graph``.
    The node returns ``Command(goto=...)`` so the conditional edge never needs
    a separate routing table.

    The per-message human ``interrupt`` has been removed (design D2). Instead
    the A/B loop runs inline: when every draft is approved by reviewer B, the
    node records an AGENT-initiated approval and routes to ``send``; when any
    draft escalates, the approval is left PENDING and the node routes to END so
    the human-review fallback can still decide it.

    ``model_client`` is the same MiMo client used elsewhere (two independent
    calls with distinct prompts play A and B). ``now`` is injected for
    deterministic tests. ``end_node`` / ``send_node`` / ``draft_node`` are
    injectable so a graph built with non-default node names still routes.
    """
    from datetime import UTC, datetime

    from careerops.application.approval_loop import AB_ACTOR_ID

    requested_for = state.get("requested_for", "")
    if not requested_for:
        raise ReviewDecisionError("state.requested_for must be set before review_gate")
    drafts = state.get("drafts") or ()
    if not drafts:
        raise ReviewDecisionError("review_gate invoked with empty drafts")

    actual_now: datetime = now if isinstance(now, datetime) else datetime.now(tz=UTC)
    revision = int(state.get("review_revision", 0))
    thread_id = _thread_id_from_config(config)
    trace_id = f"review_gate:{thread_id}:r{revision}"

    # Run the autonomous A/B approval loop. When the model is not configured
    # (e.g. tests with DisabledModelAdapter, or MODEL_PROVIDER=disabled), every
    # draft escalates and the flow falls back to human review — it never
    # auto-sends on uncertainty.
    if model_client is None:
        # No model client wired: behave as fully escalated (default-deny).
        final_drafts = drafts
        all_approved = False
    else:
        final_drafts, _results, all_approved = review_gate_ab(
            state,
            drafts,
            model_client=model_client,
            capability_resolver=capability_resolver,
            trace_id=trace_id,
        )

    capability = capability_resolver.for_send_batch(final_drafts)

    payload: dict[str, object] = {
        "drafts": [_draft_to_payload_entry(d) for d in final_drafts],
        "revision": revision,
    }
    proposal = ProposalInput(
        action_kind="send_email",
        resource_type="email_thread",
        resource_id=capability.resource_id,
        idempotency_key=review_idempotency_key(
            requested_for=requested_for, review_revision=revision, drafts=final_drafts
        ),
        created_by="langgraph.review_gate",
        target=dict(capability.target),
        payload=payload,
        trusted_facts=dict(capability.trusted_facts),
        evidence_refs=tuple(capability.evidence_refs),
        # ADR 0006 invariant: the model's self-asserted safety is NEVER an input
        # to the policy. untrusted_claims is captured only to record that it was
        # ignored.
        untrusted_claims={},
        authenticated=capability.authenticated,
    )
    result = kernel.propose(proposal, now=actual_now)
    approval = kernel.get_or_create_pending_approval(
        result.intent.id, requested_for=requested_for, now=actual_now
    )

    review_mapping.put_if_absent(
        _record_factory(
            thread_id=thread_id,
            approval_id=approval.id,
            intent_id=result.intent.id,
            requested_for=requested_for,
        )
    )

    if all_approved:
        # A/B convergence: record an AGENT-initiated approval (task 3.7) and
        # route straight to the send chain — no human click, no interrupt.
        kernel.decide_approval(
            approval.id,
            action="approve",
            requested_for=requested_for,
            now=actual_now,
            actor_type=AuditActorType.AGENT,
            actor_id=AB_ACTOR_ID,
            decision_rule_reference="autonomous_ab_approval_loop",
        )
        return Command(
            goto=send_node,
            update={
                "drafts": final_drafts,
                "approved_draft_ids": tuple(d.get("id", "") for d in final_drafts),
                "pending_approval_id": str(approval.id),
                "pending_intent_id": str(result.intent.id),
                "ab_outcome": "approved",
            },
        )

    # Escalation (non-convergence / model unavailable / provider error): leave
    # the approval PENDING and route to END. The human-review fallback
    # (POST /api/v1/review/{approval_id}) can still decide it; the send does
    # NOT proceed autonomously (default-deny on uncertainty).
    return Command(
        goto=end_node,
        update={
            "drafts": final_drafts,
            "pending_approval_id": str(approval.id),
            "pending_intent_id": str(result.intent.id),
            "ab_outcome": "escalated",
        },
    )


def _thread_id_from_config(config: Optional[RunnableConfig]) -> str:  # noqa: UP045
    if config is None:
        raise ReviewDecisionError("review_gate requires LangGraph config")
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ReviewDecisionError("config.configurable.thread_id must be a non-empty str")
    return thread_id


def _record_factory(
    *,
    thread_id: str,
    approval_id: UUID,
    intent_id: UUID,
    requested_for: str,
):
    """Build a MappingRecord. Wrapped in a function so we can lazy-import."""
    from careerops.orchestration.mapping_store import MappingRecord

    return MappingRecord(
        thread_id=thread_id,
        approval_id=approval_id,
        intent_id=intent_id,
        requested_for=requested_for,
        completed_at=None,
    )
