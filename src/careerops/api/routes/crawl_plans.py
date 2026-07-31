"""Crawl-plan API routes (Section 4, task 4.7).

Additive routes under ``/api/v1/crawl-plans`` backed by :class:`CrawlPlanService`
and :class:`CrawlRunService` (run-now). Same Iron-rule posture as the sources
router: server-side candidate ownership, ``require_repository`` (503) on
missing wiring, and the ``CRAWL_PLAN_MANAGEMENT`` capability gate (released by
default). The run-now route returns the queued run record after requesting its
idempotent Temporal workflow; it never waits for crawl completion.

Plan versions are immutable copy-on-write (design Decision 2): editing the
active plan creates a new version (POST ``/``), preserving the prior one for
provenance. Pause deactivates the active version; resume re-activates the
latest version. Next-run is derived (``now + interval`` in the plan's IANA
timezone) and returned for display — it is not persisted.

Readiness state aggregates sidecar health, plan state, and source eligibility
into a single endpoint so the console can render a consolidated CTA.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.api.errors import InvalidStateError
from careerops.application.crawl_plan_service import (
    MAX_SCHEDULE_INTERVAL_SECONDS,
    MIN_SCHEDULE_INTERVAL_SECONDS,
    CrawlPlanPreferences,
    CrawlPlanService,
    CrawlRunService,
    CrawlSourceService,
)
from careerops.domain.crawl_plans import CrawlPerRunLimits, CrawlSourceState
from careerops.domain.profiles import (
    CompensationPreference,
    LocationKind,
    LocationPreference,
    RemoteRules,
)
from careerops.orchestration.capability_resolver import CapabilityKind
from careerops.workflows.s5_contracts import CrawlRunWorkflowInput
from careerops.workflows.s5_workflows import CrawlRunWorkflow

try:
    from temporalio.exceptions import WorkflowAlreadyStartedError
except ImportError:  # pragma: no cover - temporalio is a runtime dependency
    WorkflowAlreadyStartedError = type("WorkflowAlreadyStartedError", (Exception,), {})


_log = logging.getLogger("careerops.api.crawl_plans")

router = APIRouter(
    prefix="/api/v1/crawl-plans",
    tags=["crawl-plans"],
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class LocationInput(BaseModel):
    name: str
    kind: str = "preferred"
    radius_km: int | None = None


class RemoteRulesInput(BaseModel):
    remote_allowed: bool = False
    hybrid_allowed: bool = False
    onsite_required: bool = False
    timezone: str = ""


class CompensationInput(BaseModel):
    currency: str = ""
    amount_min: int | None = None
    amount_max: int | None = None
    period: str = ""
    equity: bool = False


class PerRunLimitsInput(BaseModel):
    max_postings_per_source: int | None = None
    max_sources: int | None = None
    timeout_seconds: int | None = None


class CrawlPlanWriteRequest(BaseModel):
    """Body for create. Carries NO candidate_id (server-resolved)."""

    sources: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    locations: list[LocationInput] = Field(default_factory=list)
    remote_rules: RemoteRulesInput = Field(default_factory=RemoteRulesInput)
    seniority: list[str] = Field(default_factory=list)
    compensation: CompensationInput = Field(default_factory=CompensationInput)
    content_scope: str = ""
    interval_seconds: int = MIN_SCHEDULE_INTERVAL_SECONDS
    timezone: str = "UTC"
    per_run_limits: PerRunLimitsInput = Field(default_factory=PerRunLimitsInput)
    activate: bool = True


class LocationOut(BaseModel):
    name: str
    kind: str = "preferred"
    radius_km: int | None = None


class RemoteRulesOut(BaseModel):
    remote_allowed: bool = False
    hybrid_allowed: bool = False
    onsite_required: bool = False
    timezone: str = ""


class CompensationOut(BaseModel):
    currency: str = ""
    amount_min: int | None = None
    amount_max: int | None = None
    period: str = ""
    equity: bool = False


class PerRunLimitsOut(BaseModel):
    max_postings_per_source: int | None = None
    max_sources: int | None = None
    timeout_seconds: int | None = None


class CrawlPlanVersionResponse(BaseModel):
    id: str
    version: int
    is_active: bool
    sources: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    locations: list[LocationOut] = Field(default_factory=list)
    remote_rules: RemoteRulesOut = Field(default_factory=RemoteRulesOut)
    seniority: list[str] = Field(default_factory=list)
    compensation: CompensationOut = Field(default_factory=CompensationOut)
    content_scope: str = ""
    interval_seconds: int = 0
    timezone: str = "UTC"
    per_run_limits: PerRunLimitsOut = Field(default_factory=PerRunLimitsOut)
    rules_version: str = ""
    created_at: str | None = None


class CrawlPlanVersionListResponse(BaseModel):
    items: list[CrawlPlanVersionResponse] = Field(default_factory=list)
    total: int = 0


class CrawlPlanHeadResponse(BaseModel):
    """Active-plan head view: the active version plus the derived next-run."""

    active: CrawlPlanVersionResponse | None = None
    next_run_at: str | None = None
    state: str  # active | paused | draft


class RunResponse(BaseModel):
    id: str
    plan_version_id: str
    run_identity: str
    state: str
    source_set: list[str] = Field(default_factory=list)
    error_category: str = ""
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    next_eligible_at: str | None = None


# ---------------------------------------------------------------------------
# Readiness / empty-state / scope preview schemas
# ---------------------------------------------------------------------------


class SidecarReadinessOut(BaseModel):
    """Sidecar health sub-object in the readiness response."""

    ready: bool = False
    service: str = ""
    protocol_version: str = ""
    image_digest: str = ""
    policy_bundle_digest: str = ""
    egress_proxy_mode: str = ""
    key_id: str = ""
    reason_code: str | None = None
    checked_at: str | None = None


class SourceReadinessOut(BaseModel):
    """Per-source eligibility summary."""

    source_id: str
    source_type: str
    base_url: str
    enabled: bool = False
    state: str = ""
    executor_mode: str = "http"
    eligible: bool = False
    reason: str | None = None


class ReadinessStateResponse(BaseModel):
    """Aggregated readiness: sidecar + plan + source eligibility.

    The console uses this to render the consolidated CTA: when
    ``ready=false``, ``missing`` lists what the user must configure before
    a run can start.
    """

    ready: bool = False
    plan_state: str  # active | paused | draft | none
    sidecar: SidecarReadinessOut = Field(default_factory=SidecarReadinessOut)
    sources: list[SourceReadinessOut] = Field(default_factory=list)
    eligible_source_count: int = 0
    total_source_count: int = 0
    missing: list[str] = Field(default_factory=list)
    trace_id: str = ""


class EmptyStateCTAResponse(BaseModel):
    """Empty-state CTA when the candidate has no plan versions.

    Guides the console to render an onboarding action instead of a plan
    dashboard.
    """

    has_plan: bool = False
    has_sources: bool = False
    cta_kind: str  # create_plan | add_sources | activate_plan
    cta_title: str = ""
    cta_description: str = ""
    next_route: str = ""
    trace_id: str = ""


class SourceAllowlistItem(BaseModel):
    """One source in the scope preview allowlist."""

    source_id: str
    source_type: str
    base_url: str
    executor_mode: str = "http"
    trust_status: str = "unknown"
    terms_status: str = "unknown"
    robots_status: str = "unknown"
    enabled: bool = False
    state: str = ""


class SchedulePreviewOut(BaseModel):
    """Derived schedule preview for a plan version."""

    interval_seconds: int = 0
    timezone: str = "UTC"
    next_run_at: str | None = None
    human_readable: str = ""


class PolicyPreviewOut(BaseModel):
    """Policy/scope summary for a plan version."""

    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    content_scope: str = ""


class ScopePreviewResponse(BaseModel):
    """Full scope preview: allowlist, policy, schedule, and readiness.

    Returned for a plan version (or the active version when no id is given)
    so the console can render a pre-run summary.
    """

    plan_version_id: str | None = None
    version: int = 0
    is_active: bool = False
    allowlist: list[SourceAllowlistItem] = Field(default_factory=list)
    policy: PolicyPreviewOut = Field(default_factory=PolicyPreviewOut)
    schedule: SchedulePreviewOut = Field(default_factory=SchedulePreviewOut)
    eligible_source_count: int = 0
    total_source_count: int = 0
    ready: bool = False
    missing: list[str] = Field(default_factory=list)
    trace_id: str = ""


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _plan_service(request: Request) -> CrawlPlanService:
    return require_repository(request, "crawl_plan_service")  # type: ignore[return-value]


def _run_service(request: Request) -> CrawlRunService:
    return require_repository(request, "crawl_run_service")  # type: ignore[return-value]


def _source_repo(request: Request) -> CrawlSourceService:
    return cast(CrawlSourceService, require_repository(request, "crawl_source_service"))


# ---------------------------------------------------------------------------
# Routes — head + version history
# ---------------------------------------------------------------------------


@router.get("")
def get_active_plan(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanHeadResponse:
    """Return the active-plan head view: the active version (or ``null`` when
    paused / no versions), the derived ``next_run_at`` (``null`` when paused),
    and the derived head ``state`` (``active`` / ``paused`` / ``draft``)."""
    return _head_response(candidate_id, service)


@router.get("/versions")
def list_plan_versions(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
    limit: int = Query(default=50, ge=1, le=200),
) -> CrawlPlanVersionListResponse:
    versions = service.list_versions(candidate_id, limit=limit)
    return CrawlPlanVersionListResponse(
        items=[_to_response(v) for v in versions], total=len(versions)
    )


@router.get("/versions/{version_id}")
def get_plan_version(
    version_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanVersionResponse:
    return _to_response(service.get_version(candidate_id, version_id))


# ---------------------------------------------------------------------------
# Routes — version lifecycle
# ---------------------------------------------------------------------------


@router.post("/versions", status_code=201)
async def create_plan_version(
    request: Request,
    body: CrawlPlanWriteRequest,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanVersionResponse:
    """Validate, persist, and (by default) activate a new plan version.

    Copy-on-write: the prior active version is preserved and deactivated in
    the same transaction; the new version applies only to future runs (design
    Decision 2). An invalid schedule raises ``INVALID_STATE`` (409) BEFORE any
    persistence and never displaces the prior active version.
    """
    _validate_interval_bounds(body.interval_seconds)
    preferences = _to_preferences(body)
    version = service.create_version(candidate_id, preferences, activate=body.activate)
    if body.activate:
        await _reconcile_crawl_schedules(
            request, candidate_id, version, paused=False
        )
    return _to_response(version)


@router.post("/versions/{version_id}/activate")
async def activate_plan_version(
    request: Request,
    version_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanVersionResponse:
    """Activate an existing version (deactivates the prior active one). The
    schedule is re-validated against current bounds so a version created under
    an older rules-version cannot become active with an invalid schedule."""
    version = service.activate_version(candidate_id, version_id)
    await _reconcile_crawl_schedules(
        request, candidate_id, version, paused=False
    )
    return _to_response(version)


@router.post("/pause")
async def pause_plan(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanHeadResponse:
    """Idempotent: deactivate the active version so the scheduler fires no new
    runs. Prior versions / postings / evidence / run history are preserved."""
    version = service.pause(candidate_id)
    if version is not None:
        await _reconcile_crawl_schedules(
            request, candidate_id, version, paused=True
        )
    return _head_response(candidate_id, service)


@router.post("/resume")
async def resume_plan(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
) -> CrawlPlanHeadResponse:
    """Idempotent: re-activate the latest version. Raises ``INVALID_STATE`` if
    there is no version to resume or the schedule has become invalid."""
    version = service.resume(candidate_id)
    await _reconcile_crawl_schedules(
        request, candidate_id, version, paused=False
    )
    return _head_response(candidate_id, service)


# ---------------------------------------------------------------------------
# Routes — run-now (queue a Temporal workflow without waiting)
# ---------------------------------------------------------------------------


@router.post("/run-now")
async def run_plan_now(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    run_service: Annotated[CrawlRunService, Depends(_run_service)],
) -> RunResponse:
    """Create (or return the existing PENDING) run for the active plan.

    Iron Rule 4 idempotency: a repeat while the prior run is PENDING/RUNNING
    returns the same run record. Requires an ACTIVE plan with at least one
    eligible (enabled + ACTIVE) source; otherwise raises ``INVALID_STATE``.
    The run is recorded as PENDING and the corresponding workflow is started
    with a stable workflow ID. If the Temporal client/worker is unavailable,
    the run remains visibly PENDING so a later request can retry the start;
    this endpoint never reports a crawl as completed merely because enqueueing
    was requested.
    """
    run = run_service.run_now(candidate_id)
    await _start_run_workflow(request, candidate_id, run.id)
    return _run_to_response(run)


# ---------------------------------------------------------------------------
# Routes — readiness + empty-state CTA + scope preview
# ---------------------------------------------------------------------------


@router.get("/readiness")
def get_readiness_state(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
    request: Request,
) -> ReadinessStateResponse:
    """Return aggregated readiness: sidecar health, plan state, and source
    eligibility.

    The console uses this to render a consolidated CTA. When ``ready=false``,
    ``missing`` lists what the user must configure before a run can start.
    """
    trace = _trace_id_or_default(request)
    active = service.get_active(candidate_id)
    versions = service.list_versions(candidate_id, limit=1)

    # Derive plan state.
    if active is not None:
        plan_state = "active"
    elif versions:
        plan_state = "paused"
    else:
        plan_state = "draft"

    # Sidecar readiness.
    sidecar_out = _resolve_sidecar_readiness(request)

    # Source eligibility.
    source_repo = _source_repo(request)
    sources_out: list[SourceReadinessOut] = []
    eligible_count = 0
    try:
        if hasattr(source_repo, "list_sources"):
            raw_sources = source_repo.list_sources(candidate_id, limit=200)
            for src in raw_sources:
                eligible = (
                    src.enabled and src.state == CrawlSourceState.ACTIVE and sidecar_out.ready
                    if src.executor_mode == "ego"
                    else src.enabled and src.state == CrawlSourceState.ACTIVE
                )
                reason = None
                if not src.enabled:
                    reason = "source_disabled"
                elif src.state != CrawlSourceState.ACTIVE:
                    reason = f"source_state={src.state}"
                elif src.executor_mode == "ego" and not sidecar_out.ready:
                    reason = "sidecar_not_ready"

                sources_out.append(
                    SourceReadinessOut(
                        source_id=str(src.id),
                        source_type=src.source_type,
                        base_url=src.base_url,
                        enabled=src.enabled,
                        state=src.state,
                        executor_mode=src.executor_mode,
                        eligible=eligible,
                        reason=reason,
                    )
                )
                if eligible:
                    eligible_count += 1
    except Exception:
        _log.debug("source listing unavailable for readiness check", exc_info=True)

    # Build missing list.
    missing: list[str] = []
    if plan_state == "draft":
        missing.append("create_plan")
    elif plan_state == "paused":
        missing.append("resume_plan")
    if not sources_out:
        missing.append("add_sources")
    elif eligible_count == 0:
        missing.append("enable_sources")
    if not sidecar_out.ready and any(s.executor_mode == "ego" for s in sources_out):
        missing.append("sidecar_not_ready")

    ready = (
        plan_state == "active"
        and eligible_count > 0
        and (sidecar_out.ready or not any(s.executor_mode == "ego" for s in sources_out))
    )

    return ReadinessStateResponse(
        ready=ready,
        plan_state=plan_state,
        sidecar=sidecar_out,
        sources=sources_out,
        eligible_source_count=eligible_count,
        total_source_count=len(sources_out),
        missing=missing,
        trace_id=trace,
    )


@router.get("/empty-state-cta")
def get_empty_state_cta(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
    request: Request,
) -> EmptyStateCTAResponse:
    """Return the empty-state CTA when the candidate has no plan versions.

    Guides the console to render an onboarding action instead of a plan
    dashboard.
    """
    trace = _trace_id_or_default(request)
    versions = service.list_versions(candidate_id, limit=1)
    active = service.get_active(candidate_id)

    # Check if sources exist.
    source_repo = _source_repo(request)
    has_sources = False
    try:
        if hasattr(source_repo, "list_sources"):
            raw_sources = source_repo.list_sources(candidate_id, limit=1)
            has_sources = len(raw_sources) > 0
    except Exception as exc:
        _log.warning("crawl source availability lookup failed: %s", type(exc).__name__)

    if not versions:
        return EmptyStateCTAResponse(
            has_plan=False,
            has_sources=has_sources,
            cta_kind="create_plan",
            cta_title="创建采集计划",
            cta_description=("配置您的职位来源、关键词和筛选条件\N{FULLWIDTH COMMA}开始自动采集。"),
            next_route="/crawl-plans/versions",
            trace_id=trace,
        )
    elif active is None:
        return EmptyStateCTAResponse(
            has_plan=True,
            has_sources=has_sources,
            cta_kind="activate_plan",
            cta_title="激活采集计划",
            cta_description="您有一个已暂停的计划。激活后即可开始定时采集。",
            next_route="/crawl-plans/resume",
            trace_id=trace,
        )
    elif not has_sources:
        return EmptyStateCTAResponse(
            has_plan=True,
            has_sources=False,
            cta_kind="add_sources",
            cta_title="添加职位来源",
            cta_description="添加 Greenhouse、Lever 或公司官网作为采集来源。",
            next_route="/crawl-sources",
            trace_id=trace,
        )
    else:
        # Should not normally be reached (this endpoint is for empty states).
        return EmptyStateCTAResponse(
            has_plan=True,
            has_sources=True,
            cta_kind="create_plan",
            cta_title="",
            cta_description="",
            next_route="",
            trace_id=trace,
        )


@router.get("/scope-preview")
def get_scope_preview(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlPlanService, Depends(_plan_service)],
    request: Request,
    version_id: UUID | None = None,
) -> ScopePreviewResponse:
    """Return the scope preview for a plan version (or the active/latest
    version when no ``version_id`` is given).

    Shows the source allowlist, policy summary, schedule preview, and
    readiness state so the console can render a pre-run summary.
    """
    trace = _trace_id_or_default(request)
    missing: list[str] = []

    # Resolve the target version.
    if version_id is not None:
        version = service.get_version(candidate_id, version_id)
    else:
        active = service.get_active(candidate_id)
        if active is not None:
            version = active
        else:
            latest = service.latest_version(candidate_id)
            if latest is None:
                return ScopePreviewResponse(
                    ready=False,
                    missing=["create_plan"],
                    trace_id=trace,
                )
            version = latest

    # Build allowlist from the version's source set.
    source_repo = _source_repo(request)
    allowlist: list[SourceAllowlistItem] = []
    eligible_count = 0
    try:
        for source_id in version.sources:
            if hasattr(source_repo, "get_source"):
                src = source_repo.get_source(candidate_id, source_id)
                eligible = src.enabled and src.state == CrawlSourceState.ACTIVE
                allowlist.append(
                    SourceAllowlistItem(
                        source_id=str(src.id),
                        source_type=src.source_type,
                        base_url=src.base_url,
                        executor_mode=src.executor_mode,
                        trust_status=src.trust_status,
                        terms_status=src.terms_status,
                        robots_status=src.robots_status,
                        enabled=src.enabled,
                        state=src.state,
                    )
                )
                if eligible:
                    eligible_count += 1
    except Exception:
        _log.debug("source resolution failed during scope preview", exc_info=True)

    # Policy preview.
    policy = PolicyPreviewOut(
        include_keywords=list(version.include_keywords),
        exclude_keywords=list(version.exclude_keywords),
        themes=list(version.themes),
        role_families=list(version.role_families),
        seniority=list(version.seniority),
        content_scope=version.content_scope,
    )

    # Schedule preview.
    next_run = service.next_run_at(candidate_id)
    human = _human_readable_interval(version.interval_seconds)
    schedule = SchedulePreviewOut(
        interval_seconds=version.interval_seconds,
        timezone=version.timezone,
        next_run_at=_iso(next_run),
        human_readable=human,
    )

    # Missing items.
    if not allowlist:
        missing.append("add_sources")
    elif eligible_count == 0:
        missing.append("enable_sources")

    # Check sidecar readiness for ego sources.
    sidecar_out = _resolve_sidecar_readiness(request)
    has_ego = any(s.executor_mode == "ego" for s in allowlist)
    if has_ego and not sidecar_out.ready:
        missing.append("sidecar_not_ready")

    ready = version.is_active and eligible_count > 0 and (sidecar_out.ready or not has_ego)

    return ScopePreviewResponse(
        plan_version_id=str(version.id),
        version=version.version,
        is_active=version.is_active,
        allowlist=allowlist,
        policy=policy,
        schedule=schedule,
        eligible_source_count=eligible_count,
        total_source_count=len(allowlist),
        ready=ready,
        missing=missing,
        trace_id=trace,
    )


async def _start_run_workflow(request: Request, owner_id: UUID, run_id: UUID) -> None:
    """Best-effort enqueue of the durable crawl workflow.

    ``RuntimeResources`` exposes the real Temporal client lazily. Lightweight
    contract probes used by unit/API tests may not provide that method; in that
    case the run remains queued rather than falling back to a fake executor.
    A repeated request uses the same workflow ID and is therefore naturally
    idempotent when Temporal reports that it already exists.
    """
    probe = getattr(request.app.state, "readiness_probe", None)
    get_client = getattr(probe, "get_temporal_client", None)
    if not callable(get_client):
        _log.warning("crawl workflow not enqueued: Temporal client is not wired")
        return

    try:
        client = await cast("Callable[[], Awaitable[Any]]", get_client)()
        settings = request.app.state.settings
        await client.start_workflow(
            CrawlRunWorkflow.run,
            CrawlRunWorkflowInput(owner_id=str(owner_id), run_id=str(run_id)),
            id=f"careerops.crawl-run:{run_id}",
            task_queue=settings.temporal_task_queue,
        )
    except WorkflowAlreadyStartedError:
        # Double-click/retry: the existing workflow owns this run.
        return
    except Exception as exc:
        # Keep the durable run queued for a later retry. Do not expose provider
        # or network details through the API response.
        _log.warning(
            "crawl workflow enqueue unavailable for run %s: %s",
            run_id,
            type(exc).__name__,
        )


async def _reconcile_crawl_schedules(
    request: Request,
    owner_id: UUID,
    version: object,
    *,
    paused: bool,
) -> None:
    """Create/update the real per-source Temporal schedules for a plan."""
    probe = getattr(request.app.state, "readiness_probe", None)
    get_client = getattr(probe, "get_temporal_client", None)
    if not callable(get_client):
        return
    runtime = cast("Any", probe)

    if not paused:
        from careerops.application.crawl_readiness import check_crawl_readiness

        budget = runtime.tier2_budget
        readiness = check_crawl_readiness(
            runtime.database,
            budget_max_concurrent_slots=budget.max_concurrent_slots,
            budget_daily_action_budget=budget.daily_action_budget,
            permission_repository=runtime.crawl_permission_repo,
        )
        if not readiness.ready:
            raise InvalidStateError(
                "crawl schedules are not ready: " + "; ".join(readiness.failures)
            )

    from careerops.application.crawl_activation import TemporalScheduleActivator
    from careerops.infrastructure.temporal.schedule_manager import ScheduleManager

    client = await cast("Callable[[], Awaitable[Any]]", get_client)()
    activator = TemporalScheduleActivator(
        ScheduleManager(client),
        task_queue=request.app.state.settings.temporal_task_queue,
    )
    source_ids = tuple(getattr(version, "sources", ()))
    plan_interval = max(1, int(getattr(version, "interval_seconds", 3600)))
    for source_id in source_ids:
        source = runtime.crawl_source_repo.get_by_id(owner_id, source_id)
        configured = source.last_run_metadata.get("interval_seconds")
        interval_seconds = (
            int(configured)
            if isinstance(configured, int) and configured > 0
            else plan_interval
        )
        if source.executor_mode.value == "ego":
            interval_seconds = max(interval_seconds, 2 * 60 * 60)
        await activator.activate_source_schedule(
            source_id,
            timedelta(seconds=interval_seconds),
            paused=paused,
            owner_id=owner_id,
        )


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _trace_id_or_default(request: Request) -> str:
    value = getattr(getattr(request, "state", None), "trace_id", None)
    return value if isinstance(value, str) else "unavailable"


def _resolve_sidecar_readiness(request: Request) -> SidecarReadinessOut:
    """Best-effort sidecar readiness probe.

    If the sidecar client is wired in app.state, call ``check_ready`` and
    return the validated response.  If absent or unreachable, return a
    ``ready=false`` stub so the readiness endpoint degrades gracefully.
    """
    client = getattr(getattr(request.app, "state", None), "sidecar_client", None)
    if client is None:
        return SidecarReadinessOut(ready=False, reason_code="SIDECAR_NOT_CONFIGURED")
    try:
        resp = client.check_ready()
        return SidecarReadinessOut(
            ready=resp.ready,
            service=resp.service,
            protocol_version=resp.protocol_version,
            image_digest=resp.image_digest,
            policy_bundle_digest=resp.policy_bundle_digest,
            egress_proxy_mode=resp.egress_proxy_mode,
            key_id=resp.key_id,
            reason_code=resp.reason_code,
            checked_at=resp.checked_at,
        )
    except Exception as exc:
        _log.debug("sidecar readiness probe failed: %s", type(exc).__name__)
        return SidecarReadinessOut(ready=False, reason_code="SIDECAR_UNREACHABLE")


def _human_readable_interval(seconds: int) -> str:
    """Return a human-readable interval string (e.g. '每 4 小时')."""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return f"每 {seconds} 秒"
    if seconds < 3600:
        minutes = seconds // 60
        return f"每 {minutes} 分钟"
    if seconds < 86400:
        hours = seconds // 3600
        return f"每 {hours} 小时"
    days = seconds // 86400
    return f"每 {days} 天"


def _head_response(candidate_id: UUID, service: CrawlPlanService) -> CrawlPlanHeadResponse:
    """Build the active-plan head view: active version (or null), derived
    next-run (or null when paused), and derived head state."""
    active = service.get_active(candidate_id)
    versions = service.list_versions(candidate_id, limit=1)
    if active is not None:
        state = "active"
    elif versions:
        state = "paused"
    else:
        state = "draft"
    return CrawlPlanHeadResponse(
        active=_to_response(active) if active else None,
        next_run_at=_iso(service.next_run_at(candidate_id)),
        state=state,
    )


def _validate_interval_bounds(interval_seconds: int) -> None:
    """Early, route-level bound check so the error path is identical whether
    the value is sent by the JSON body or coerced by a future client."""
    if isinstance(interval_seconds, bool):
        raise _invalid_state("interval_seconds must be an integer")
    if interval_seconds < MIN_SCHEDULE_INTERVAL_SECONDS:
        raise _invalid_state(f"interval_seconds below minimum {MIN_SCHEDULE_INTERVAL_SECONDS}s")
    if interval_seconds > MAX_SCHEDULE_INTERVAL_SECONDS:
        raise _invalid_state(f"interval_seconds above maximum {MAX_SCHEDULE_INTERVAL_SECONDS}s")


def _invalid_state(message: str) -> InvalidStateError:
    return InvalidStateError(message)


def _to_preferences(body: CrawlPlanWriteRequest) -> CrawlPlanPreferences:
    locations = tuple(_location_to_domain(loc) for loc in body.locations)
    remote = RemoteRules(
        remote_allowed=body.remote_rules.remote_allowed,
        hybrid_allowed=body.remote_rules.hybrid_allowed,
        onsite_required=body.remote_rules.onsite_required,
        timezone=body.remote_rules.timezone,
    )
    compensation = CompensationPreference(
        currency=body.compensation.currency,
        amount_min=body.compensation.amount_min,
        amount_max=body.compensation.amount_max,
        period=body.compensation.period,
        equity=body.compensation.equity,
    )
    limits = CrawlPerRunLimits(
        max_postings_per_source=body.per_run_limits.max_postings_per_source,
        max_sources=body.per_run_limits.max_sources,
        timeout_seconds=body.per_run_limits.timeout_seconds,
    )
    return CrawlPlanPreferences(
        sources=tuple(UUID(s) for s in body.sources),
        themes=tuple(body.themes),
        include_keywords=tuple(body.include_keywords),
        exclude_keywords=tuple(body.exclude_keywords),
        role_families=tuple(body.role_families),
        locations=locations,
        remote_rules=remote,
        seniority=tuple(body.seniority),
        compensation=compensation,
        content_scope=body.content_scope,
        schedule_interval_seconds=body.interval_seconds,
        schedule_timezone=body.timezone,
        per_run_limits=limits,
    )


def _location_to_domain(loc: LocationInput) -> LocationPreference:
    try:
        kind = LocationKind(loc.kind)
    except ValueError:
        kind = LocationKind.PREFERRED
    return LocationPreference(name=loc.name, kind=kind, radius_km=loc.radius_km)


def _to_response(version: object) -> CrawlPlanVersionResponse:
    return CrawlPlanVersionResponse(
        id=str(version.id),  # type: ignore[attr-defined]
        version=version.version,  # type: ignore[attr-defined]
        is_active=version.is_active,  # type: ignore[attr-defined]
        sources=[str(s) for s in version.sources],  # type: ignore[attr-defined]
        themes=list(version.themes),  # type: ignore[attr-defined]
        include_keywords=list(version.include_keywords),  # type: ignore[attr-defined]
        exclude_keywords=list(version.exclude_keywords),  # type: ignore[attr-defined]
        role_families=list(version.role_families),  # type: ignore[attr-defined]
        locations=[
            LocationOut(name=loc.name, kind=loc.kind.value, radius_km=loc.radius_km)  # type: ignore[attr-defined]
            for loc in version.locations  # type: ignore[attr-defined]
        ],
        remote_rules=RemoteRulesOut(
            remote_allowed=version.remote_rules.remote_allowed,  # type: ignore[attr-defined]
            hybrid_allowed=version.remote_rules.hybrid_allowed,  # type: ignore[attr-defined]
            onsite_required=version.remote_rules.onsite_required,  # type: ignore[attr-defined]
            timezone=version.remote_rules.timezone,  # type: ignore[attr-defined]
        ),
        compensation=CompensationOut(
            currency=version.compensation.currency,  # type: ignore[attr-defined]
            amount_min=version.compensation.amount_min,  # type: ignore[attr-defined]
            amount_max=version.compensation.amount_max,  # type: ignore[attr-defined]
            period=getattr(version.compensation.period, "value", str(version.compensation.period)),  # type: ignore[attr-defined]
            equity=version.compensation.equity,  # type: ignore[attr-defined]
        ),
        content_scope=version.content_scope,  # type: ignore[attr-defined]
        interval_seconds=version.interval_seconds,  # type: ignore[attr-defined]
        timezone=version.timezone,  # type: ignore[attr-defined]
        per_run_limits=PerRunLimitsOut(
            max_postings_per_source=version.per_run_limits.max_postings_per_source,  # type: ignore[attr-defined]
            max_sources=version.per_run_limits.max_sources,  # type: ignore[attr-defined]
            timeout_seconds=version.per_run_limits.timeout_seconds,  # type: ignore[attr-defined]
        ),
        rules_version=version.rules_version,  # type: ignore[attr-defined]
        created_at=_iso(version.created_at),  # type: ignore[attr-defined]
    )


def _run_to_response(run: object) -> RunResponse:
    return RunResponse(
        id=str(run.id),  # type: ignore[attr-defined]
        plan_version_id=str(run.plan_version_id),  # type: ignore[attr-defined]
        run_identity=run.run_identity,  # type: ignore[attr-defined]
        state=run.state.value,  # type: ignore[attr-defined]
        source_set=[str(s) for s in run.source_set],  # type: ignore[attr-defined]
        error_category=run.error_category,  # type: ignore[attr-defined]
        created_at=_iso(run.created_at),  # type: ignore[attr-defined]
        started_at=_iso(run.started_at),  # type: ignore[attr-defined]
        ended_at=_iso(run.ended_at),  # type: ignore[attr-defined]
        next_eligible_at=_iso(run.next_eligible_at),  # type: ignore[attr-defined]
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
