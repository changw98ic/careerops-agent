"""Gmail read-sync API routes (Section 11, tasks 11.7 + 11.10).

Additive routes under /api/v1 that expose the dedicated recruiting-account
connection state, on-demand sync, sync history, ingested threads/messages,
the unresolved thread-association queue, and user link confirmation. The
provider is NEVER called from the request path — ``sync-now`` records a
durable sync run that a worker later drives; the route returns ``pending``.

Routes:
- GET   /api/v1/mail/account                       — connection status (11.7)
- POST  /api/v1/mail/account/revoke                — revoke the account (11.2)
- POST  /api/v1/mail/sync-now                      — record a sync run (11.3)
- GET   /api/v1/mail/sync-history                  — cursor-paginated runs (11.10)
- GET   /api/v1/mail/threads                       — thread summaries (11.7)
- GET   /api/v1/mail/threads/{thread_id}/messages  — cursor-paginated messages
- GET   /api/v1/mail/unresolved-links              — association queue (11.6)
- POST  /api/v1/mail/unresolved-links/{link_id}/confirm — user resolution (11.6)

Iron rules honored:
- Additive (Iron Rule 8): new paths only; no existing route touched.
- Server-side ownership (Iron Rule 2 + 6): candidate resolved via
  ``require_candidate_id``; not-owned -> 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service -> 503.
- Default-deny (Iron Rule 7): every route is gated on the GMAIL_READ
  capability, which stays DENIED at the contract layer until a separate
  qualification change releases it. Real Gmail OAuth is NOT enabled here
  (task 17.6); the path is built but the capability gate denies by default.
- Model review-only (Iron Rule 2): the request body carries only the user's
  explicit decision; the server never lets model output pick a link.
- Bounded responses (Iron Rule 8 / task 11.10): cursor pagination with a hard
  ``limit`` ceiling and ``Cache-Control: no-store`` on every response.
"""

# Services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.mail_sync_service import (
    AccountNotConnectedError,
    AccountNotOwnedError,
    LinkAlreadyResolvedError,
    LinkNotFoundError,
    MailSyncService,
    ReadScopeError,
    ThreadNotFoundError,
)
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1/mail",
    tags=["mail-sync"],
    # Iron Rule 7: GMAIL_READ stays DENIED at the contract layer. Every
    # Section-11 route is default-denied until a separate qualification change
    # releases the capability. The denied response itself is the "sync
    # unavailable" signal the frontend renders.
    dependencies=[Depends(require_capability(CapabilityKind.GMAIL_READ))],
)

# Hard ceiling on page size (task 11.10 — bounded response payloads).
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 50
_NO_STORE = {"cache_control": "no-store"}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AccountStatusResponse(BaseModel):
    connected: bool
    connection_state: str
    sync_available: bool
    email_address: str = ""
    account_id: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    history_id: str = ""
    watch_expiration: str | None = None
    last_sync_at: str | None = None
    last_error_code: str = ""
    policy_version: str = ""


class RevokeResponse(BaseModel):
    account_id: str
    connection_state: str
    revoked: bool


class SyncNowResponse(BaseModel):
    run_id: str
    status: str
    direction: str


class ConfirmLinkRequest(BaseModel):
    """User's resolution of an unresolved thread link (task 11.6).

    ``application_id`` is the user's pick; omit / null to leave the thread
    unlinked (a valid resolution).
    """

    application_id: str | None = None


class ConfirmLinkResponse(BaseModel):
    link_id: str
    confirmed_application_id: str | None
    decided_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(request: Request) -> MailSyncService:
    return require_repository(request, "mail_sync_service")  # type: ignore[return-value]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, (AccountNotOwnedError, ThreadNotFoundError, LinkNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AccountNotConnectedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, LinkAlreadyResolvedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReadScopeError):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/account")
def get_account_status(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> AccountStatusResponse:
    """Return the dedicated recruiting-account connection status (11.1/11.7).

    Returns ``connected=false`` when no account is connected for the
    candidate, so the frontend can render the connect affordance / stale-data
    state without distinguishing "none" from "denied" (the capability gate
    already denied this route unless GMAIL_READ is released).
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    summary = service.get_account_status(candidate_id=candidate_id)
    if summary is None:
        return AccountStatusResponse(
            connected=False,
            connection_state="disconnected",
            sync_available=False,
        )
    return AccountStatusResponse(
        connected=summary.connection_state == "connected",
        connection_state=summary.connection_state.value,
        sync_available=summary.sync_available,
        email_address=summary.email_address,
        account_id=str(summary.account_id),
        granted_scopes=list(summary.granted_scopes),
        history_id=summary.history_id,
        watch_expiration=(
            summary.watch_expiration.isoformat() if summary.watch_expiration else None
        ),
        last_sync_at=summary.last_sync_at.isoformat() if summary.last_sync_at else None,
        last_error_code=summary.last_error_code,
        policy_version=summary.policy_version,
    )


@router.post("/account/revoke")
def revoke_account(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> RevokeResponse:
    """Revoke the candidate's recruiting account (task 11.2).

    Stops future sync + provider use and invalidates pending provider actions
    for that account. Idempotent. Requires an existing connected account.
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    summary = service.get_account_status(candidate_id=candidate_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="no mail account connected")
    try:
        revoked = service.revoke_account(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            now=_now(),
        )
    except AccountNotOwnedError as exc:
        raise _translate(exc) from exc
    return RevokeResponse(
        account_id=str(revoked.account_id),
        connection_state=revoked.connection_state.value,
        revoked=revoked.connection_state.value == "revoked",
    )


@router.post("/sync-now")
def sync_now(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    direction: str = Query("incremental", pattern="^(full|incremental|backfill)$"),
) -> SyncNowResponse:
    """Record a durable sync run (task 11.3). Returns ``pending`` immediately.

    The provider is NOT called here — an isolated worker later drives the run.
    A repeat request while a run is pending/running returns that run
    (idempotent). Requires the account to be CONNECTED.
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    summary = service.get_account_status(candidate_id=candidate_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="no mail account connected")
    try:
        from careerops.domain.mail_sync import SyncDirection

        run = service.request_sync(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            now=_now(),
            direction=SyncDirection(direction),
        )
    except (AccountNotConnectedError, AccountNotOwnedError) as exc:
        raise _translate(exc) from exc
    return SyncNowResponse(
        run_id=str(run.id),
        status=run.status.value,
        direction=run.direction.value,
    )


@router.get("/sync-history")
def list_sync_history(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    cursor: str | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, object]:
    """Cursor-paginated sync-run history (task 11.10)."""
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    summary = service.get_account_status(candidate_id=candidate_id)
    if summary is None:
        return {"items": [], "next_cursor": None, "has_more": False}
    try:
        return service.list_sync_runs(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            cursor=cursor,
            limit=_bounded_limit(limit),
        )
    except AccountNotOwnedError as exc:
        raise _translate(exc) from exc


@router.get("/threads")
def list_threads(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    cursor: str | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, object]:
    """Cursor-paginated thread summaries for the candidate's account."""
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    summary = service.get_account_status(candidate_id=candidate_id)
    if summary is None:
        return {"items": [], "next_cursor": None, "has_more": False}
    try:
        return service.list_threads(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            cursor=cursor,
            limit=_bounded_limit(limit),
        )
    except AccountNotOwnedError as exc:
        raise _translate(exc) from exc


@router.get("/threads/{thread_id}/messages")
def list_thread_messages(
    thread_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    cursor: str | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, object]:
    """Cursor-paginated messages within a thread (task 11.10)."""
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    try:
        return service.list_messages(
            candidate_id=candidate_id,
            thread_id=UUID(thread_id),
            cursor=cursor,
            limit=_bounded_limit(limit),
        )
    except (ThreadNotFoundError, AccountNotOwnedError) as exc:
        raise _translate(exc) from exc


@router.get("/unresolved-links")
def list_unresolved_links(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    cursor: str | None = None,
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> dict[str, object]:
    """The unresolved thread-association queue (task 11.6)."""
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    return service.list_unresolved_links(
        candidate_id=candidate_id,
        cursor=cursor,
        limit=_bounded_limit(limit),
    )


@router.post("/unresolved-links/{link_id}/confirm")
def confirm_link(
    link_id: str,
    body: ConfirmLinkRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ConfirmLinkResponse:
    """Record the user's resolution of an unresolved thread link (task 11.6).

    The user picks one application (or none). The decision is append-only and
    idempotent — a repeat on the same link returns the recorded result.
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    confirmed = UUID(body.application_id) if body.application_id else None
    try:
        decision = service.confirm_link(
            candidate_id=candidate_id,
            link_id=UUID(link_id),
            confirmed_application_id=confirmed,
            now=_now(),
        )
    except (LinkNotFoundError, LinkAlreadyResolvedError) as exc:
        raise _translate(exc) from exc
    return ConfirmLinkResponse(
        link_id=str(decision.link_id),
        confirmed_application_id=(
            str(decision.confirmed_application_id) if decision.confirmed_application_id else None
        ),
        decided_at=decision.decided_at.isoformat(),
    )
