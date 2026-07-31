"""Agent Console API routes.

Implements the endpoints frozen by
``openspec/changes/agent-first-console-experience/docs/agent-console/api-contract.md``
sections 3 and 3.1.

Routes are per-candidate (``/api/v1/candidates/{candidate_id}/...``); the
``candidate_id`` path parameter is used for ownership scoping.

Every mutation requires:
- candidate-scoped auth (via ``require_api_auth`` at the router mount),
- CSRF + Origin validation (via ``_validate_mutation_origin``),
- ``Idempotency-Key`` header.

Every response containing candidate content sets
``Cache-Control: no-store`` and ``Pragma: no-cache``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import JsonValue

from careerops.agent_console.action_projection import (
    ActionProjectionBuilder,
)
from careerops.agent_console.capability_service import PreflightService
from careerops.agent_console.context_service import ContextService
from careerops.agent_console.contracts import (
    AcceptAction,
    ActionOutcome,
    ActionPage,
    ActionReceipt,
    ActionState,
    Capability,
    CompleteAction,
    Context,
    CreateContext,
    DismissAction,
    ModelOperation,
    NotificationPolicy,
    Preflight,
    PreflightRequest,
    SnoozeAction,
)
from careerops.api.errors import (
    CareerOpsHTTPException,
    CSRFRejectedError,
    NotFoundError,
)
from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.observability import current_trace_id
from careerops.web.security import OriginHostValidator, RequestOriginRejected

router = APIRouter(prefix="/api/v1/candidates/{candidate_id}", tags=["agent-console"])
_log = logging.getLogger("careerops.api.agent_console")

# ---------------------------------------------------------------------------
# Idempotency key validation
# ---------------------------------------------------------------------------

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")


def _validate_idempotency_key(key: str) -> str:
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise _InvalidRequest("Idempotency-Key header is invalid")
    return key


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


class _InvalidRequest(CareerOpsHTTPException):
    error_code_str = "INVALID_REQUEST"
    message_default = "请求格式不正确"

    @classmethod
    def _status_code(cls) -> int:
        return 422

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class _StaleConflict(CareerOpsHTTPException):
    error_code_str = "STALE"
    message_default = "资源状态已变更"

    @classmethod
    def _status_code(cls) -> int:
        return 409

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


class _PrerequisiteBlocked(CareerOpsHTTPException):
    error_code_str = "PREREQUISITE_BLOCKED"
    message_default = "前置条件未满足"

    @classmethod
    def _status_code(cls) -> int:
        return 422

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


def _validate_host(request: Request) -> None:
    settings = getattr(request.app.state, "web_settings", None)
    if settings is None:
        return
    try:
        OriginHostValidator(settings).validate_host(request)
    except RequestOriginRejected:
        raise CSRFRejectedError() from None


def _validate_mutation_origin(request: Request) -> None:
    settings = getattr(request.app.state, "web_settings", None)
    if settings is None:
        return
    try:
        OriginHostValidator(settings).validate_mutation(request)
    except RequestOriginRejected:
        raise CSRFRejectedError() from None


def _get_action_projection(request: Request) -> ActionProjectionBuilder:
    builder = getattr(request.app.state, "action_projection", None)
    if builder is None:
        # Wire a default stub if not explicitly set.
        builder = ActionProjectionBuilder()
    return builder


def _get_context_service(request: Request) -> ContextService:
    svc = getattr(request.app.state, "context_service", None)
    if svc is None:
        svc = ContextService()
        request.app.state.context_service = svc
    return svc


def _get_preflight_service(request: Request) -> PreflightService:
    svc = getattr(request.app.state, "preflight_service", None)
    if svc is None:
        from careerops.agent_console.capability_service import (
            CapabilityResolver,
            EgressGuard,
            ProviderPolicy,
        )

        # Wire the CapabilityResolver when Settings are available.
        capability_svc_resolver = getattr(request.app.state, "capability_svc_resolver", None)
        if capability_svc_resolver is None:
            settings = getattr(request.app.state, "web_settings", None)
            orch_resolver = getattr(request.app.state, "capability_resolver", None)
            if settings is not None:
                capability_svc_resolver = CapabilityResolver(
                    settings,
                    capability_resolver=orch_resolver,
                )
                request.app.state.capability_svc_resolver = capability_svc_resolver

        # Wire the egress guard.
        egress_guard = getattr(request.app.state, "egress_guard", None)
        if egress_guard is None:
            egress_guard = EgressGuard(ProviderPolicy())
            request.app.state.egress_guard = egress_guard

        svc = PreflightService(
            capability_resolver=capability_svc_resolver,
            context_service=_get_context_service(request),
            egress_guard=egress_guard,
        )
        request.app.state.preflight_service = svc
    return svc


def _append_audit(
    request: Request,
    *,
    event_type: str,
    resource_type: str,
    resource_id: UUID,
    actor_id: str,
    event_data: dict[str, JsonValue] | None = None,
) -> UUID:
    """Append an audit event. Returns the audit event_id."""
    engine = getattr(request.app.state, "readiness_probe", None)
    audit_engine = getattr(engine, "database", None) if engine is not None else None
    event_id = uuid4()
    draft = AuditEventDraft(
        event_type=event_type,
        actor_type=AuditActorType.USER,
        resource_type=resource_type,
        resource_id=resource_id,
        trace_id=current_trace_id(),
        actor_id=actor_id,
        event_data=event_data or {},
        event_id=event_id,
    )
    if audit_engine is not None:
        try:
            from careerops.infrastructure.database.audit import PostgresAuditWriterEngine

            PostgresAuditWriterEngine(audit_engine).append(draft)
        except Exception as exc:
            # Audit failure does not block the response; the mutation
            # already succeeded.  Log-only in v1.
            _log.warning("agent-console audit append failed: %s", type(exc).__name__)
    return event_id


def _compute_body_hash(body: object) -> str:
    """Compute a SHA-256 hash of a request body for idempotency binding."""
    raw = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Idempotency store (in-memory, per-process)
# ---------------------------------------------------------------------------

_idempotency_store: dict[str, tuple[str, dict[str, object]]] = {}


def _check_idempotency(key: str, body_hash: str) -> dict[str, object] | None:
    """Check the idempotency store.  Returns the cached response if replayed
    with the same hash, raises 409 if replayed with a different hash."""
    existing = _idempotency_store.get(key)
    if existing is None:
        return None
    stored_hash, stored_response = existing
    if stored_hash != body_hash:
        raise _IdempotencyConflict()
    return stored_response


def _store_idempotency(key: str, body_hash: str, response: dict[str, object]) -> None:
    _idempotency_store[key] = (body_hash, response)


class _IdempotencyConflict(CareerOpsHTTPException):
    error_code_str = "IDEMPOTENCY_CONFLICT"
    message_default = "幂等键已被使用且请求内容不同"

    @classmethod
    def _status_code(cls) -> int:
        return 409

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message=message or self.message_default, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/agent-console/actions")
async def list_actions(
    request: Request,
    response: Response,
    candidate_id: UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=3, ge=1, le=3),
) -> ActionPage:
    """GET .../agent-console/actions -- action queue (hard cap limit=3)."""
    _validate_host(request)
    # Enforce hard cap of 3 per contract.
    effective_limit = min(limit, 3)
    del effective_limit  # builder enforces internally

    builder = _get_action_projection(request)
    projection = builder.build(candidate_id)

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return ActionPage(
        queue_version=projection.queue_version,
        generated_at=projection.generated_at,
        user_goal_scope="job_search",
        notification_policy=NotificationPolicy(surface="in_app", quiet_hours=False),
        items=list(projection.items),
        next_cursor=None,
        trace_id=projection.trace_id,
    )


@router.post("/agent-console/actions/{action_key}/accept")
async def accept_action(
    action_key: str,
    body: AcceptAction,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ActionReceipt:
    """POST .../agent-console/actions/{action_key}/accept."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return ActionReceipt.model_validate(cached)

    builder = _get_action_projection(request)
    action = builder.find_action(candidate_id, action_key)
    if action is None:
        raise NotFoundError("action not found")
    if action.state is not ActionState.PROPOSED:
        raise _StaleConflict("action is not in proposed state")

    resource_id = uuid4()
    audit_id = _append_audit(
        request,
        event_type="agent_action_accepted",
        resource_type="agent_action",
        resource_id=resource_id,
        actor_id=str(candidate_id),
        event_data={"action_key": action_key},
    )

    receipt = ActionReceipt(
        trace_id=current_trace_id(),
        receipt_id=str(uuid4()),
        idempotency_key=idempotency_key,
        resource_type="agent_action",
        resource_id=str(resource_id),
        action_key=action_key,
        state=ActionState.ACCEPTED.value,
        queue_version=body.expected_queue_version,
        outcome=ActionOutcome(route=action.target_route, run_id=None),
        audit_event_id=str(audit_id),
        idempotency_replayed=False,
        accepted_at=datetime.now(UTC),
    )

    _store_idempotency(idempotency_key, body_hash, receipt.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return receipt


@router.post("/agent-console/actions/{action_key}/snooze")
async def snooze_action(
    action_key: str,
    body: SnoozeAction,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ActionReceipt:
    """POST .../agent-console/actions/{action_key}/snooze."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return ActionReceipt.model_validate(cached)

    builder = _get_action_projection(request)
    action = builder.find_action(candidate_id, action_key)
    if action is None:
        raise NotFoundError("action not found")
    if action.state is not ActionState.PROPOSED:
        raise _StaleConflict("action is not in proposed state")

    resource_id = uuid4()
    audit_id = _append_audit(
        request,
        event_type="agent_action_snoozed",
        resource_type="agent_action",
        resource_id=resource_id,
        actor_id=str(candidate_id),
        event_data={"action_key": action_key, "until": body.until.isoformat()},
    )

    receipt = ActionReceipt(
        trace_id=current_trace_id(),
        receipt_id=str(uuid4()),
        idempotency_key=idempotency_key,
        resource_type="agent_action",
        resource_id=str(resource_id),
        action_key=action_key,
        state=ActionState.SNOOZED.value,
        queue_version=body.expected_queue_version,
        outcome=ActionOutcome(route=None, run_id=None),
        audit_event_id=str(audit_id),
        idempotency_replayed=False,
        accepted_at=datetime.now(UTC),
    )

    _store_idempotency(idempotency_key, body_hash, receipt.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return receipt


@router.post("/agent-console/actions/{action_key}/dismiss")
async def dismiss_action(
    action_key: str,
    body: DismissAction,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ActionReceipt:
    """POST .../agent-console/actions/{action_key}/dismiss."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return ActionReceipt.model_validate(cached)

    builder = _get_action_projection(request)
    action = builder.find_action(candidate_id, action_key)
    if action is None:
        raise NotFoundError("action not found")
    if action.state is not ActionState.PROPOSED:
        raise _StaleConflict("action is not in proposed state")

    resource_id = uuid4()
    audit_id = _append_audit(
        request,
        event_type="agent_action_dismissed",
        resource_type="agent_action",
        resource_id=resource_id,
        actor_id=str(candidate_id),
        event_data={"action_key": action_key, "reason_code": body.reason_code},
    )

    receipt = ActionReceipt(
        trace_id=current_trace_id(),
        receipt_id=str(uuid4()),
        idempotency_key=idempotency_key,
        resource_type="agent_action",
        resource_id=str(resource_id),
        action_key=action_key,
        state=ActionState.DISMISSED.value,
        queue_version=body.expected_queue_version,
        outcome=ActionOutcome(route=None, run_id=None),
        audit_event_id=str(audit_id),
        idempotency_replayed=False,
        accepted_at=datetime.now(UTC),
    )

    _store_idempotency(idempotency_key, body_hash, receipt.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return receipt


@router.post("/agent-console/actions/{action_key}/complete")
async def complete_action(
    action_key: str,
    body: CompleteAction,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ActionReceipt:
    """POST .../agent-console/actions/{action_key}/complete."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return ActionReceipt.model_validate(cached)

    builder = _get_action_projection(request)
    action = builder.find_action(candidate_id, action_key)
    if action is None:
        raise NotFoundError("action not found")
    if action.state is not ActionState.ACCEPTED:
        raise _PrerequisiteBlocked("action must be accepted before completion")

    resource_id = uuid4()
    audit_id = _append_audit(
        request,
        event_type="agent_action_completed",
        resource_type="agent_action",
        resource_id=resource_id,
        actor_id=str(candidate_id),
        event_data={"action_key": action_key},
    )

    receipt = ActionReceipt(
        trace_id=current_trace_id(),
        receipt_id=str(uuid4()),
        idempotency_key=idempotency_key,
        resource_type="agent_action",
        resource_id=str(resource_id),
        action_key=action_key,
        state=ActionState.COMPLETED.value,
        queue_version=body.expected_queue_version,
        outcome=ActionOutcome(route=None, run_id=None),
        audit_event_id=str(audit_id),
        idempotency_replayed=False,
        accepted_at=datetime.now(UTC),
    )

    _store_idempotency(idempotency_key, body_hash, receipt.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return receipt


@router.post("/agent-console/contexts", status_code=201)
async def create_context(
    body: CreateContext,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> Context:
    """POST .../agent-console/contexts -- create context (201)."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return Context.model_validate(cached)

    ctx_svc = _get_context_service(request)
    context = ctx_svc.create(candidate_id, body)

    _append_audit(
        request,
        event_type="agent_context_created",
        resource_type="agent_context",
        resource_id=UUID(context.context_id),
        actor_id=str(candidate_id),
        event_data={"operation": body.operation.value},
    )

    _store_idempotency(idempotency_key, body_hash, context.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return context


@router.get("/agent-console/contexts/{context_id}")
async def get_context(
    context_id: UUID,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> Context:
    """GET .../agent-console/contexts/{context_id} -- read context."""
    _validate_host(request)
    ctx_svc = _get_context_service(request)
    context = ctx_svc.get(candidate_id, context_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return context


@router.get("/capabilities/agent")
async def get_agent_capability(
    request: Request,
    response: Response,
    candidate_id: UUID,
    operation: Annotated[ModelOperation, Query(...)],
) -> Capability:
    """GET .../capabilities/agent?operation= -- capability resolution."""
    _validate_host(request)
    pflt_svc = _get_preflight_service(request)
    capability = pflt_svc.resolve_capability(candidate_id, operation)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return capability


@router.post("/agent-console/preflight")
async def create_preflight(
    body: PreflightRequest,
    request: Request,
    response: Response,
    candidate_id: UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> Preflight:
    """POST .../agent-console/preflight -- model preflight."""
    _validate_mutation_origin(request)
    _validate_idempotency_key(idempotency_key)
    body_hash = _compute_body_hash(body)
    cached = _check_idempotency(idempotency_key, body_hash)
    if cached is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return Preflight.model_validate(cached)

    pflt_svc = _get_preflight_service(request)
    preflight = pflt_svc.create_preflight(candidate_id, body)

    _append_audit(
        request,
        event_type="agent_preflight_created",
        resource_type="agent_preflight",
        resource_id=UUID(preflight.preflight_id),
        actor_id=str(candidate_id),
        event_data={"operation": body.operation.value, "decision": preflight.decision},
    )

    _store_idempotency(idempotency_key, body_hash, preflight.model_dump(mode="json"))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return preflight
