"""POST ``/api/v1/review/{approval_id}`` — the HITL review decision endpoint.

Plan v0.4 §2.5 / §2.7 / §3 Stage 3. This router is the ONLY HTTP entry point
that resumes the LangGraph review gate. It is mounted solely in non-PRODUCTION
(v1 demo/test); ``api/app.py`` declines to mount it in PRODUCTION and
``RuntimeResources`` does not even build the in-memory graph there, so there is
no silent degradation to in-memory state.

Security chain (every gate fails closed):

1. **Auth** — the ``careerops_session`` cookie is authenticated via
   ``ConsoleAuthService.authenticate``. No session / bad session -> 401.
2. **CSRF** — the reviewer must supply the matching CSRF token in the
   ``X-CSRF-Token`` header (double-submit). The CSRF cookie is HttpOnly, so the
   token is delivered to trusted console JS through the web flow; reading the
   cookie value here would defeat the check. Mismatch -> 403.
3. **Same-origin** — ``OriginHostValidator.validate_mutation`` rejects any
   request whose ``Host`` / ``Origin`` is not on the allowlist. Mismatch -> 403.
4. **Rate limit** — per-user (``hash_subject(user_id)``) Redis-backed fixed
   window via ``RedisAuthRateLimiter``. Over budget -> 429.
5. **Owner check** — ``record.requested_for`` (written server-side by
   ``review_gate`` from ``state["requested_for"]``) must equal the authenticated
   user. This is NOT client-controllable: the request body carries no
   ``requested_for`` field. Mismatch -> 403.

After the gates, the endpoint reverse-resolves the approval to its
``thread_id`` / ``intent_id`` via ``ReviewMappingStore``, claims the resume
(first writer wins), and invokes ``graph.invoke(Command(resume=...))``. A repeat
of the SAME decision returns the cached result (200); a CONFLICTING decision on
an already-completed approval returns 409.

ADR 0006 invariants hold: the endpoint never widens the model gate, never passes
tools, and the decision payload it sends is the constrained
``ReviewRequest`` (action + optional edited subject/body only) — never an
arbitrary dict.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel, Field, model_validator

from careerops.application.side_effect_kernel import (
    ApprovalInvalidError,
    SideEffectKernel,
)
from careerops.auth.contracts import (
    AuthAction,
    AuthenticatedPrincipal,
    AuthError,
    AuthRateLimiter,
    CsrfRejected,
)
from careerops.auth.crypto import hash_subject
from careerops.domain.side_effects import ApprovalDecision
from careerops.orchestration.kernel_adapter import ReviewDecisionError
from careerops.orchestration.mapping_store import (
    MappingConflictError,
    MappingNotFoundError,
    ReviewMappingStore,
)
from careerops.web.security import (
    ConsoleWebSettings,
    OriginHostValidator,
    RequestOriginRejected,
)

__all__ = [
    "ReviewAuthService",
    "ReviewEditedDraft",
    "ReviewRequest",
    "install_review_endpoint",
]

_REVIEW_RATE_LIMIT_ACTION = AuthAction.REVIEW


class ReviewAuthService(Protocol):
    """Minimal auth surface the review endpoint needs.

    ``ConsoleAuthService`` satisfies this structurally; tests inject a stub. Only
    ``authenticate`` and ``validate_csrf`` are used — the endpoint never logs in
    or rotates sessions.
    """

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal: ...

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None: ...


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


def _decision_payload(approval_id: UUID, request: ReviewRequest) -> dict[str, object]:
    """Build the JSON-safe resume payload consumed by ``parse_review_decision``."""
    payload: dict[str, object] = {"action": request.action, "approval_id": str(approval_id)}
    if request.action == "edit" and request.edited_drafts:
        payload["edited_drafts"] = [
            {"id": item.id, "subject": item.subject, "body": item.body}
            for item in request.edited_drafts
        ]
    return payload


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


def _interrupt_value(graph: Any, config: dict[str, object]) -> dict[str, object] | None:
    """Return the current review_gate interrupt payload, if any.

    After an ``edit`` resume the graph re-interrupts on a NEW approval; the
    endpoint surfaces the new approval_id / intent_id so the client knows what to
    review next. Returns ``None`` when the graph is at END (approve/reject).
    """
    state = graph.get_state(config)
    if not state.next:
        return None
    tasks = getattr(state, "tasks", ()) or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", ()) or ()
        for interrupt in interrupts:
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict):
                return cast("dict[str, object]", value)
    return None


def _latest_receipt(values: dict[str, object]) -> dict[str, object] | None:
    receipts = values.get("send_receipts")
    if not isinstance(receipts, (tuple, list)) or not receipts:
        return None
    latest = receipts[-1]
    if isinstance(latest, dict):
        return cast("dict[str, object]", latest)
    return None


# ---------------------------------------------------------------------------
# Router installation
# ---------------------------------------------------------------------------


def install_review_endpoint(
    app: FastAPI,
    *,
    auth_service: ReviewAuthService,
    rate_limiter: AuthRateLimiter,
    review_mapping: ReviewMappingStore,
    career_graph: object,
    side_effect_kernel: object,
    web_settings: ConsoleWebSettings,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    """Mount the review decision router on ``app``.

    The caller (``api/app.py``) is responsible for the PRODUCTION fail-closed
    guard: this function must only be invoked in non-PRODUCTION with a compiled
    graph. ``career_graph`` / ``side_effect_kernel`` are ``object`` because the
    runtime exposes them through a broadly-typed attribute; they are cast here to
    their concrete types at the single use site.
    """
    clock = now_provider or (lambda: datetime.now(tz=UTC))
    validator = OriginHostValidator(web_settings)
    graph = cast("Any", career_graph)
    kernel = cast("SideEffectKernel", side_effect_kernel)
    router = APIRouter(prefix="/api/v1", tags=["review"])

    @router.post("/review/{approval_id}")
    async def submit_review(  # pyright: ignore[reportUnusedFunction]
        approval_id: UUID,
        body: ReviewRequest,
        request: Request,
    ) -> JSONResponse:
        now = clock()

        # 1. Auth — session cookie.
        session_token = request.cookies.get(web_settings.session_cookie_name, "")
        try:
            principal = auth_service.authenticate(session_token, now=now)
        except (AuthError, ValueError):
            return _json(401, "authentication required")

        # 2. CSRF — double-submit header. The CSRF cookie is HttpOnly; reading
        #    its value here would auto-pass any same-site forged request.
        csrf_token = request.headers.get("x-csrf-token", "")
        try:
            auth_service.validate_csrf(principal, csrf_token)
        except (CsrfRejected, AuthError, ValueError):
            return _json(403, "csrf token rejected")

        # 3. Same-origin (Host + Origin allowlists).
        try:
            validator.validate_mutation(request)
        except RequestOriginRejected:
            return _json(403, "request origin is not allowed")

        # 4. Per-user rate limit.
        subject_hash = hash_subject(str(principal.user_id))
        if not rate_limiter.check(_REVIEW_RATE_LIMIT_ACTION, subject_hash, now=now):
            return _json(429, "review rate limit exceeded")

        # 5. Reverse-resolve the approval to its thread / intent.
        try:
            record = review_mapping.get_by_approval(approval_id)
        except MappingNotFoundError:
            return _json(404, "approval not found")

        # 6. Owner check — server-side identity, never client-overridable.
        if record.requested_for != str(principal.user_id):
            return _json(403, "approval belongs to a different user")

        # 7. Claim the resume. A completed approval (first writer already won)
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

        # 8. Resume the graph with the constrained decision.
        config: dict[str, object] = {"configurable": {"thread_id": record.thread_id}}
        try:
            graph.invoke(Command(resume=_decision_payload(approval_id, body)), config)
        except ReviewDecisionError as exc:
            return _json(409, f"review decision rejected: {exc}")
        except ApprovalInvalidError as exc:
            return _json(409, f"approval no longer decidable: {exc}")

        # 9. Mark complete and build the response from the resulting graph state.
        review_mapping.complete_resume(approval_id, completed_at=now.isoformat())
        return _success_response(graph, config, record, body.action)

    def _success_response(
        graph: Any,
        config: dict[str, object],
        record: object,
        action: str,
    ) -> JSONResponse:
        record_ = cast("Any", record)
        base = {
            "approval_id": str(record_.approval_id),
            "intent_id": str(record_.intent_id),
        }
        if action == "approve":
            values = cast("dict[str, object]", graph.get_state(config).values)
            receipt = _latest_receipt(values)
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
            interrupt = _interrupt_value(graph, config)
            new_approval = str(interrupt["approval_id"]) if interrupt else None
            new_intent = str(interrupt["intent_id"]) if interrupt else None
            return JSONResponse(
                status_code=200,
                content={
                    "status": "edit_pending",
                    "decision": "rejected",
                    **base,
                    "new_approval_id": new_approval,
                    "new_intent_id": new_intent,
                },
            )
        return JSONResponse(
            status_code=200,
            content={"status": "decided", "decision": "rejected", **base},
        )

    app.include_router(router)
