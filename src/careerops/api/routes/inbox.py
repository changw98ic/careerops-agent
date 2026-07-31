"""Inbox API routes (Section 6, tasks 6.7-6.8).

Per-candidate routes under /api/v1/candidates/{candidate_id}/inbox for the job
inbox projection:
- GET  .../inbox                       — list inbox items (recommended/excluded/all)
- GET  .../inbox/{job_id}              — job detail with evidence + filter reasons
- POST .../inbox/{job_id}/favorite     — favorite a job (idempotent)
- POST .../inbox/{job_id}/ignore       — ignore a job (idempotent)
- POST .../inbox/{job_id}/snooze       — snooze until a future time
- GET  .../inbox/{job_id}/excluded-reasons — blocking reasons with evidence refs

Iron rules honored:
- Additive API (Iron Rule 8): new routes, no existing routes broken.
- Path-supplied ownership (Iron Rule 6): candidate_id comes from the path.
- Dependency-not-ready (Iron Rule 6): missing repo -> 503.
- Idempotent (Iron Rule 3): favorite/ignore are idempotent.
- Default-deny (Iron Rule 7): CRAWL_PLAN_MANAGEMENT gate on every route.
- Reversible (Iron Rule 4): recording user decisions does NOT delete source history.
"""

# Repos and services fetched via require_repository / _get_inbox_repo are typed
# as ``object``; attribute access is safe but opaque to pyright.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from careerops.api.capability_dependency import (
    require_capability,
    require_repository,
)
from careerops.api.errors import DependencyNotReadyError
from careerops.application.applications import (
    ApplicationCreateRequest,
    ApplicationServiceError,
    StateTransitionRequest,
)
from careerops.domain.applications import ApplicationEventSource, ApplicationState
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1/candidates/{candidate_id}/inbox",
    tags=["inbox"],
    # CRAWL_PLAN_MANAGEMENT gate on every route (Iron Rule 7).  The inbox
    # projection depends on crawl-plan provenance; the capability is released
    # by default so this does not block normal usage.
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)


def _record_inbox_trace(
    request: Request,
    *,
    candidate_id: UUID,
    canonical_job_id: UUID,
    decision: str,
    occurred_at: datetime,
) -> None:
    """Record inbox decisions without making observability a workflow dependency."""

    trace = getattr(getattr(request.app, "state", None), "career_loop_trace", None)
    if trace is None:
        return
    trace.record_inbox_decision(decision=decision)
    if decision != "favorite":
        return
    try:
        detail = _get_inbox_repo(request).get_job_detail_with_evidence(
            candidate_id, canonical_job_id
        )
        first_seen = detail.get("first_seen_at") if detail else None
        if isinstance(first_seen, str):
            captured_at = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if captured_at.tzinfo is not None:
                trace.record_trusted_shortlist(
                    first_seen_at=captured_at,
                    shortlisted_at=occurred_at,
                )
    except Exception:
        # Product instrumentation is best effort; a telemetry/read projection
        # failure must never change the inbox decision result.
        return


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SnoozeRequest(BaseModel):
    """Body for the snooze action. ``snoozed_until`` must be a future time."""

    snoozed_until: datetime


class FavoriteIgnoreResponse(BaseModel):
    """Response for favorite / ignore actions."""

    application_id: str
    state: str
    canonical_job_id: str


class SnoozeResponse(BaseModel):
    """Response for the snooze action."""

    canonical_job_id: str
    snoozed_until: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_inbox_repo(request: Request) -> object:
    """Pull the PostgresInboxRepository from app.state or raise 503."""
    repo = getattr(request.app.state, "inbox_repository", None)
    if repo is None:
        raise DependencyNotReadyError("inbox repository not available")
    return repo


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_inbox(
    request: Request,
    candidate_id: UUID,
    tab: str = Query("recommended", pattern="^(recommended|excluded|all)$"),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    q: str | None = None,
) -> dict[str, object]:
    """List inbox items for the authenticated candidate.

    ``tab=recommended`` shows jobs that passed all hard filters.
    ``tab=excluded`` shows jobs with blocking reasons.
    ``tab=all`` shows every evaluated job regardless of verdict.

    Active snooze records (``snoozed_until > now``) are excluded from all
    tabs so the user's deferred items stay out of the way.
    """
    repo = _get_inbox_repo(request)
    return repo.get_inbox_items(
        candidate_id,
        tab=tab,
        cursor=cursor,
        limit=limit,
        q=q,
    )


@router.get("/{job_id}")
def get_job_detail(
    job_id: str,
    request: Request,
    candidate_id: UUID,
) -> dict[str, object]:
    """Get job detail with evidence links, filter decision, requirement
    matches, application state, and snooze state."""
    repo = _get_inbox_repo(request)

    detail = repo.get_job_detail_with_evidence(candidate_id, UUID(job_id))
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return detail


@router.get("/{job_id}/excluded-reasons")
def get_excluded_reasons(
    job_id: str,
    request: Request,
    candidate_id: UUID,
) -> dict[str, object]:
    """Return blocking reasons with evidence refs for an excluded job.

    Returns 404 when the job has no filter decision or is not excluded for
    the candidate's active profile.
    """
    repo = _get_inbox_repo(request)

    reasons = repo.get_excluded_reasons(candidate_id, UUID(job_id))
    if reasons is None:
        raise HTTPException(status_code=404, detail="No excluded decision found for this job")
    return reasons


@router.post("/{job_id}/favorite")
def favorite_job(
    job_id: str,
    request: Request,
    candidate_id: UUID,
) -> FavoriteIgnoreResponse:
    """Favorite a job (idempotent).

    Creates or reuses an application record in FAVORITED state.  If the
    application already exists and is favorited, returns the existing state
    without modification.  Recording this user decision never deletes source
    history (canonical job + postings are preserved).
    """
    app_service = require_repository(request, "application_service")
    now = datetime.now(tz=UTC)
    canonical_job_id = UUID(job_id)

    # Try to find existing application first (idempotent)
    existing = app_service.find_by_candidate_and_job(candidate_id, canonical_job_id)
    if existing is not None:
        # Already exists -- if not favorited, transition
        if existing.state != ApplicationState.FAVORITED:
            try:
                updated = app_service.transition_state(
                    StateTransitionRequest(
                        application_id=existing.id,
                        to_state=ApplicationState.FAVORITED,
                        source=ApplicationEventSource.USER,
                        actor_id=str(candidate_id),
                    ),
                    now,
                )
                _record_inbox_trace(
                    request,
                    candidate_id=candidate_id,
                    canonical_job_id=canonical_job_id,
                    decision="favorite",
                    occurred_at=now,
                )
                return FavoriteIgnoreResponse(
                    application_id=str(updated.id),
                    state=updated.state.value,
                    canonical_job_id=str(updated.canonical_job_id),
                )
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        # Already favorited -- idempotent return
        return FavoriteIgnoreResponse(
            application_id=str(existing.id),
            state=existing.state.value,
            canonical_job_id=str(existing.canonical_job_id),
        )

    # Create new application
    try:
        application = app_service.create_application(
            ApplicationCreateRequest(
                candidate_id=candidate_id,
                canonical_job_id=canonical_job_id,
            ),
            now,
        )
    except ApplicationServiceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _record_inbox_trace(
        request,
        candidate_id=candidate_id,
        canonical_job_id=canonical_job_id,
        decision="favorite",
        occurred_at=now,
    )
    return FavoriteIgnoreResponse(
        application_id=str(application.id),
        state=application.state.value,
        canonical_job_id=str(application.canonical_job_id),
    )


@router.post("/{job_id}/ignore")
def ignore_job(
    job_id: str,
    request: Request,
    candidate_id: UUID,
) -> FavoriteIgnoreResponse:
    """Ignore a job (idempotent).

    Records the decision and removes the job from the active recommendation
    queue.  The canonical job + postings are never deleted -- source history
    is preserved.
    """
    app_service = require_repository(request, "application_service")
    now = datetime.now(tz=UTC)
    canonical_job_id = UUID(job_id)

    # Try to find existing application first (idempotent)
    existing = app_service.find_by_candidate_and_job(candidate_id, canonical_job_id)
    if existing is not None:
        if existing.state != ApplicationState.IGNORED:
            try:
                updated = app_service.transition_state(
                    StateTransitionRequest(
                        application_id=existing.id,
                        to_state=ApplicationState.IGNORED,
                        source=ApplicationEventSource.USER,
                        actor_id=str(candidate_id),
                    ),
                    now,
                )
                _record_inbox_trace(
                    request,
                    candidate_id=candidate_id,
                    canonical_job_id=canonical_job_id,
                    decision="ignore",
                    occurred_at=now,
                )
                return FavoriteIgnoreResponse(
                    application_id=str(updated.id),
                    state=updated.state.value,
                    canonical_job_id=str(updated.canonical_job_id),
                )
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        # Already ignored -- idempotent return
        return FavoriteIgnoreResponse(
            application_id=str(existing.id),
            state=existing.state.value,
            canonical_job_id=str(existing.canonical_job_id),
        )

    # Create new application then transition to IGNORED
    try:
        application = app_service.create_application(
            ApplicationCreateRequest(
                candidate_id=candidate_id,
                canonical_job_id=canonical_job_id,
            ),
            now,
        )
        application = app_service.transition_state(
            StateTransitionRequest(
                application_id=application.id,
                to_state=ApplicationState.IGNORED,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
            ),
            now,
        )
    except ApplicationServiceError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    _record_inbox_trace(
        request,
        candidate_id=candidate_id,
        canonical_job_id=canonical_job_id,
        decision="ignore",
        occurred_at=now,
    )
    return FavoriteIgnoreResponse(
        application_id=str(application.id),
        state=application.state.value,
        canonical_job_id=str(application.canonical_job_id),
    )


@router.post("/{job_id}/snooze")
def snooze_job(
    job_id: str,
    body: SnoozeRequest,
    request: Request,
    candidate_id: UUID,
) -> SnoozeResponse:
    """Snooze a job until a future time (idempotent).

    Snooze hides the job from all inbox tabs until ``snoozed_until`` passes.
    This is NOT an application state change -- it is a separate user decision
    that does not touch the applications table.  Repeating the call with the
    same ``(candidate_id, canonical_job_id)`` updates the snooze time
    (idempotent).  Recording a snooze never deletes source history.
    """
    repo = _get_inbox_repo(request)
    now = datetime.now(tz=UTC)

    # Validate: snoozed_until must be in the future
    snoozed_until = body.snoozed_until
    if snoozed_until.tzinfo is None:
        snoozed_until = snoozed_until.replace(tzinfo=UTC)
    if snoozed_until <= now:
        raise HTTPException(status_code=422, detail="snoozed_until must be a future time")

    canonical_job_id = UUID(job_id)

    # Verify the job exists
    job_repo = require_repository(request, "job_read_repository")
    job_detail = job_repo.get_job_detail(job_id)
    if job_detail is None:
        raise HTTPException(status_code=404, detail="Job not found")

    repo.upsert_snooze(candidate_id, canonical_job_id, snoozed_until)
    _record_inbox_trace(
        request,
        candidate_id=candidate_id,
        canonical_job_id=canonical_job_id,
        decision="snooze",
        occurred_at=now,
    )

    return SnoozeResponse(
        canonical_job_id=str(canonical_job_id),
        snoozed_until=snoozed_until.isoformat(),
    )
