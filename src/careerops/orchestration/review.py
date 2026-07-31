"""POST ``/api/v1/review/{approval_id}`` — the human-review fallback endpoint.

real-autonomous-career-loop Phase 3: the per-message ``review_gate`` interrupt
is gone, replaced by the autonomous A/B approval loop. This router is now the
HUMAN FALLBACK for the rare case A/B did not converge (or the model was
unavailable): the graph leaves such an approval PENDING and ends, and a human
decides it here. It is mounted solely in non-PRODUCTION (v1 demo/test);
``api/app.py`` declines to mount it in PRODUCTION.

Trust model (auth-rm Task 10): the console login is gone, so this endpoint
trusts the loopback reviewer. There is no session, CSRF, or origin gate; the
approver actor is the fixed string ``local-reviewer``, and its decisions are
recorded in the audit chain as USER-initiated (``actor_type=USER``,
``actor_id="local-reviewer"``). The remaining guard is the per-actor
Redis-backed rate limit (over budget -> 429). Ownership is still enforced by
the kernel at decision time: ``record.requested_for`` (written server-side by
``review_gate`` from ``state["requested_for"]``) must equal the approval's
``requested_for``, and it is NOT client-controllable (the request body carries
no ``requested_for`` field).

The endpoint reverse-resolves the approval to its ``intent_id`` via
``ReviewMappingStore``, claims the decision (first writer wins), and applies
the human decision directly to the ``SideEffectKernel``
(``decide_approval``). On ``approve`` it then executes the send through the
kernel (the graph is NOT re-invoked — re-running would replay crawl + A/B
needlessly). A repeat of the SAME decision returns the cached result (200); a
CONFLICTING decision on an already-completed approval returns 409. Only the
autonomous A/B path records AGENT.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from careerops.application.audit import AuditActorType
from careerops.application.side_effect_kernel import (
    ApprovalInvalidError,
    SideEffectKernel,
)
from careerops.domain.side_effects import ApprovalDecision
from careerops.infrastructure.rate_limit import (
    RateLimitAction,
    RateLimiter,
    hash_subject,
)
from careerops.orchestration.mapping_store import (
    MappingConflictError,
    MappingNotFoundError,
    ReviewMappingStore,
)

__all__ = [
    "ReviewEditedDraft",
    "ReviewRequest",
    "install_review_endpoint",
]

_REVIEW_RATE_LIMIT_ACTION = RateLimitAction.REVIEW

# Fixed actor for the loopback reviewer (the console login is gone; there is
# no session principal). Review audit records use actor_type=USER with this
# actor id; only the autonomous A/B path records AGENT.
LOCAL_REVIEWER_ACTOR = "local-reviewer"


# ---------------------------------------------------------------------------
# Request schema — constrained, never an arbitrary dict
# ---------------------------------------------------------------------------


class ReviewEditedDraft(BaseModel):
    """One reviewer edit: subject/body only (no recipient / target change).

    Recipient / target changes require a fresh proposal and must re-enter policy
    (plan v0.4 §2.5); they are therefore not representable here.
    """

    id: str = Field(min_length=1, max_length=128)
    subject: str = Field(default="", max_length=512)
    body: str = Field(default="", max_length=8192)


class ReviewRequest(BaseModel):
    """Reviewer decision. ``edited_drafts`` is legal only for ``edit``."""

    action: Literal["approve", "reject", "edit"]
    edited_drafts: list[ReviewEditedDraft] | None = None

    @model_validator(mode="after")
    def _enforce_action_edit_consistency(self) -> ReviewRequest:
        if self.action == "edit":
            if not self.edited_drafts:
                raise ValueError("edit requires a non-empty edited_drafts list")
        elif self.edited_drafts:
            raise ValueError("approve/reject must not carry edited_drafts")
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(status_code: int, message: str, **extra: object) -> JSONResponse:
    payload: dict[str, object] = {"error": message}
    payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload)


def _approval_action_for(kernel: SideEffectKernel, record: object) -> str | None:
    """Map the recorded terminal decision of ``record.approval_id`` to an action.

    Returns ``"approve"`` / ``"reject"`` / ``None`` (PENDING / EXPIRED / not
    found). ``edit`` is represented as ``reject`` in the kernel (the old approval
    is rejected before re-proposing), so a recorded REJECTED decision matches both
    ``reject`` and ``edit`` requests.
    """
    record_ = cast("Any", record)
    replay = kernel.replay(record_.intent_id)
    for approval in replay.approvals:
        if approval.id == record_.approval_id:
            if approval.decision is ApprovalDecision.APPROVED:
                return "approve"
            if approval.decision is ApprovalDecision.REJECTED:
                return "reject"
            return None
    return None


def _is_duplicate(existing: str | None, requested: str) -> bool:
    """A repeat request is a duplicate only if it matches the recorded decision.

    ``edit`` and ``reject`` both leave the old approval REJECTED, so either
    request against a REJECTED record is a duplicate (the reviewer already moved
    the approval out of PENDING). Any other combination is a conflict.
    """
    if existing == "approve":
        return requested == "approve"
    if existing == "reject":
        return requested in ("reject", "edit")
    return False


def _kernel_receipt(kernel: SideEffectKernel, intent_id: UUID) -> dict[str, object] | None:
    """Return the latest provider receipt for ``intent_id`` from the kernel, if any."""
    replay = kernel.replay(intent_id)
    if not replay.receipts:
        return None
    latest = replay.receipts[-1]
    return {
        "provider": latest.provider,
        "provider_resource_id": latest.provider_resource_id,
        "reconciliation_key": latest.reconciliation_key,
    }


# ---------------------------------------------------------------------------
# Router installation
# ---------------------------------------------------------------------------


def install_review_endpoint(
    app: FastAPI,
    *,
    rate_limiter: RateLimiter,
    review_mapping: ReviewMappingStore,
    career_graph: object,
    side_effect_kernel: object,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    """Mount the review decision router on ``app``.

    The caller (``api/app.py``) is responsible for the PRODUCTION fail-closed
    guard: this function must only be invoked in non-PRODUCTION with a compiled
    graph. ``career_graph`` / ``side_effect_kernel`` are ``object`` because the
    runtime exposes them through a broadly-typed attribute; they are cast here
    to their concrete types at the single use site.
    """
    clock = now_provider or (lambda: datetime.now(tz=UTC))
    kernel = cast("SideEffectKernel", side_effect_kernel)
    router = APIRouter(prefix="/api/v1", tags=["review"])

    @router.post("/review/{approval_id}")
    async def submit_review(  # pyright: ignore[reportUnusedFunction]
        approval_id: UUID,
        body: ReviewRequest,
    ) -> JSONResponse:
        now = clock()

        # 1. Rate limit — keyed on the fixed loopback reviewer actor.
        subject_hash = hash_subject(LOCAL_REVIEWER_ACTOR)
        if not rate_limiter.check(_REVIEW_RATE_LIMIT_ACTION, subject_hash, now=now):
            return _json(429, "review rate limit exceeded")

        # 2. Reverse-resolve the approval to its thread / intent.
        try:
            record = review_mapping.get_by_approval(approval_id)
        except MappingNotFoundError:
            return _json(404, "approval not found")

        # 3. Claim the resume. A completed approval (first writer already won)
        #    is answered from the cached kernel state: same decision -> 200,
        #    conflicting decision -> 409. The graph is NOT re-invoked.
        try:
            review_mapping.claim_resume(approval_id)
        except MappingConflictError:
            existing = _approval_action_for(kernel, record)
            if _is_duplicate(existing, body.action):
                return _json(
                    200,
                    "decision already recorded",
                    status="already_decided",
                    decision=existing,
                    approval_id=str(record.approval_id),
                    intent_id=str(record.intent_id),
                )
            return _json(
                409,
                "conflicting decision already recorded",
                status="conflict",
                existing_decision=existing,
                approval_id=str(record.approval_id),
                intent_id=str(record.intent_id),
            )
        except MappingNotFoundError:
            return _json(404, "approval not found")

        # 4. Apply the human decision directly to the kernel. The autonomous A/B
        #    loop already ran at review_gate; this is the human fallback for an
        #    approval the loop left PENDING (non-convergence / model unavailable).
        #    The graph is NOT re-invoked — re-running would replay crawl + A/B
        #    needlessly. ``edit`` is a transport-level concern expressed as a
        #    reject here (the current approval is invalidated; the drafts remain
        #    visible in graph state for the human to handle manually). The kernel
        #    re-validates the approval owner binding (``requested_for``) at
        #    decision time.
        record_ = cast("Any", record)
        kernel_action = "reject" if body.action == "edit" else body.action
        try:
            kernel.decide_approval(
                approval_id,
                action=kernel_action,
                requested_for=record_.requested_for,
                now=now,
                # Loopback reviewer: fixed local actor, USER audit type. Only
                # the autonomous A/B path records AGENT (task 3.7).
                actor_type=AuditActorType.USER,
                actor_id=LOCAL_REVIEWER_ACTOR,
            )
        except ApprovalInvalidError as exc:
            return _json(409, f"approval no longer decidable: {exc}")

        # 5. On approve, execute the send through the kernel and capture the
        #    receipt. On reject/edit, no send occurs.
        receipt: dict[str, object] | None = None
        if body.action == "approve":
            try:
                outcome = kernel.execute(cast("UUID", record_.intent_id), now=now)
            except ApprovalInvalidError as exc:
                return _json(409, f"approval no longer decidable: {exc}")
            receipt = _kernel_receipt(kernel, cast("UUID", record_.intent_id))
            if receipt is not None:
                receipt["final_state"] = outcome.status.value

        # 6. Mark complete and build the response.
        review_mapping.complete_resume(approval_id, completed_at=now.isoformat())
        return _success_response(record, body.action, receipt)

    def _success_response(
        record: object,
        action: str,
        receipt: dict[str, object] | None,
    ) -> JSONResponse:
        record_ = cast("Any", record)
        base = {
            "approval_id": str(record_.approval_id),
            "intent_id": str(record_.intent_id),
        }
        if action == "approve":
            return JSONResponse(
                status_code=200,
                content={
                    "status": "decided",
                    "decision": "approved",
                    **base,
                    "receipt": receipt,
                },
            )
        if action == "edit":
            return JSONResponse(
                status_code=200,
                content={
                    "status": "edit_recorded",
                    "decision": "rejected",
                    **base,
                },
            )
        return JSONResponse(
            status_code=200,
            content={"status": "decided", "decision": "rejected", **base},
        )

    app.include_router(router)
