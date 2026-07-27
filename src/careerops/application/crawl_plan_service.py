"""Crawl source / plan / run application services (Section 4, tasks 4.4-4.6).

end-to-end-career-application-loop, ``crawl-plan-management`` spec. Three thin
services wrap the Section-4 repositories (Postgres + in-memory twins) and own
the business rules the spec promises:

- :class:`CrawlSourceService` — register a trusted ATS / official source
  (Greenhouse / Lever / Ashby / official via the existing adapter registry),
  CRUD, and idempotent pause/resume. Sources DECLARE SCOPE ONLY; the crawl
  policy in :mod:`careerops.application.crawl_policy` stays authoritative and
  fails closed (Iron Rule 6). Unsupported source types are refused at
  registration (spec: "Unsupported source types SHALL remain disabled until an
  adapter and policy contract exist").

- :class:`CrawlPlanService` — immutable copy-on-write plan versions (design
  Decision 2). Editing the active plan creates a NEW version and preserves the
  old one for provenance; the new version applies only to future runs. Schedule
  validation (bounded interval + IANA timezone) runs before persistence so an
  invalid schedule never displaces the prior active version (Iron Rule 5).

- :class:`CrawlRunService` — manual run-now creates a PENDING run bound to the
  active plan-version snapshot + eligible source set + a stable ``run_identity``
  (Iron Rule 4). Idempotent on the identity: a repeat while the prior run is
  still PENDING/RUNNING returns the existing run; only after a terminal state
  does a new attempt mint a new identity. The overlap helper records a CANCELLED
  run with a reason instead of starting a second crawl (task 4.6).

Section 4 ONLY records run identity + terminal counters; the actual posting /
content-version ingest idempotency lives in
:class:`careerops.infrastructure.temporal.m1_crawl_sink.RealCrawlActivitySink`
(Section 5). Temporal execution is NOT started here.

Server-side ownership (Iron Rule 2): every method takes a server-resolved
``owner_id`` (the authenticated candidate). No method reads an owner from a
request body. A missing repository surfaces as ``DependencyNotReadyError``
(503) at the route via :func:`require_repository`; the services never fall back
to an unscoped or in-memory store.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from careerops.api.errors import ConflictError, InvalidStateError, NotFoundError
from careerops.domain.crawl import ALLOWED_TRANSITIONS, CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPerRunLimits,
    CrawlPlanRepository,
    CrawlPlanVersion,
    CrawlPolicyStatus,
    CrawlRun,
    CrawlRunCounters,
    CrawlRunRepository,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
    CrawlSourceType,
    is_supported_source_type,
)
from careerops.domain.profiles import (
    CompensationPreference,
    LocationPreference,
    RemoteRules,
)

# Protocol re-exports: the domain Protocols (``CrawlSourceRepository`` etc.)
# are the canonical seams the services depend on. They are ``runtime_checkable``
# in the domain module; the service consumes them directly rather than
# re-declaring a parallel Protocol surface.
_SourceRepoProtocol = CrawlSourceRepository
_PlanRepoProtocol = CrawlPlanRepository
_RunRepoProtocol = CrawlRunRepository

__all__ = [
    "CRAWL_PLAN_RULES_VERSION",
    "MAX_PER_RUN_POSTINGS_PER_SOURCE",
    "MAX_PER_RUN_SOURCES",
    "MAX_PER_RUN_TIMEOUT_SECONDS",
    "MAX_SCHEDULE_INTERVAL_SECONDS",
    "MIN_SCHEDULE_INTERVAL_SECONDS",
    "CrawlPlanPreferences",
    "CrawlPlanService",
    "CrawlRunService",
    "CrawlSourceService",
    "compute_next_run_at",
    "validate_per_run_limits",
    "validate_schedule",
]

# Bounded rules version for the plan-version snapshot. A future rules change
# bumps this so a re-activation can detect that a stored version must be
# re-validated against the current schedule bounds.
CRAWL_PLAN_RULES_VERSION = "crawl-plan-v1"

# Bounded schedule interval — Iron Rule 5. The DB CHECK only enforces
# ``interval_seconds >= 0``; the service layer applies the safe operational
# bounds the scheduler can reason about. Below the minimum a plan would hammer
# a single domain (the per-domain rate limit would deny every run anyway);
# above the maximum the plan is effectively dormant and should be re-created
# on demand rather than hold a long-lived schedule slot.
MIN_SCHEDULE_INTERVAL_SECONDS = 300  # 5 minutes
MAX_SCHEDULE_INTERVAL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Per-run limits are user-configurable, but ``None`` means the bounded executor
# default rather than unlimited work. These upper bounds are enforced before a
# plan version is persisted so a malformed API request cannot become an
# unbounded network/ingest job later.
MAX_PER_RUN_POSTINGS_PER_SOURCE = 5_000
MAX_PER_RUN_SOURCES = 200
MAX_PER_RUN_TIMEOUT_SECONDS = 3_600


# ---------------------------------------------------------------------------
# Structured preference input (API-facing). Maps 1:1 onto CrawlPlanVersion
# minus the identity / version / active / timestamp fields the service owns.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class CrawlPlanPreferences:
    """Structured preference input for a new crawl-plan version.

    ``schedule_interval_seconds`` + ``schedule_timezone`` carry the bounded
    schedule (task 4.6); the service validates them via :func:`validate_schedule`
    before persistence. ``per_run_limits`` reuses the domain type so the same
    bounds flow into the run snapshot unchanged.
    """

    sources: tuple[UUID, ...] = ()
    themes: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    role_families: tuple[str, ...] = ()
    locations: tuple[LocationPreference, ...] = ()
    remote_rules: RemoteRules = dataclasses.field(default_factory=RemoteRules)
    seniority: tuple[str, ...] = ()
    compensation: CompensationPreference = dataclasses.field(default_factory=CompensationPreference)
    content_scope: str = ""
    schedule_interval_seconds: int = MIN_SCHEDULE_INTERVAL_SECONDS
    schedule_timezone: str = "UTC"
    per_run_limits: CrawlPerRunLimits = dataclasses.field(default_factory=CrawlPerRunLimits)


# ---------------------------------------------------------------------------
# Schedule validation + next-run computation (task 4.6)
# ---------------------------------------------------------------------------


def validate_schedule(*, interval_seconds: int, timezone: str) -> None:
    """Reject schedules the scheduler cannot evaluate safely (Iron Rule 5).

    Bounded interval — the value must fall inside
    :data:`MIN_SCHEDULE_INTERVAL_SECONDS` ... :data:`MAX_SCHEDULE_INTERVAL_SECONDS`.
    IANA timezone — the value must resolve via :class:`zoneinfo.ZoneInfo`; an
    unknown or non-IANA zone is rejected (a fixed offset is not accepted). Each
    failure raises :class:`InvalidStateError` (Phase 0) and the caller MUST NOT
    persist a version carrying the bad schedule (spec: "Schedule is invalid ...
    the system rejects the plan and does not schedule a run").
    """
    if isinstance(interval_seconds, bool):
        # ``bool`` is a subclass of ``int`` in Python; reject it explicitly so a
        # JSON ``true`` is not silently coerced to ``1`` by downstream layers.
        raise InvalidStateError("schedule interval_seconds must be an integer")
    if interval_seconds < MIN_SCHEDULE_INTERVAL_SECONDS:
        raise InvalidStateError(
            f"schedule interval_seconds below minimum {MIN_SCHEDULE_INTERVAL_SECONDS}s"
        )
    if interval_seconds > MAX_SCHEDULE_INTERVAL_SECONDS:
        raise InvalidStateError(
            f"schedule interval_seconds above maximum {MAX_SCHEDULE_INTERVAL_SECONDS}s"
        )
    if not timezone:
        raise InvalidStateError("schedule timezone is required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise InvalidStateError(f"schedule timezone is not a valid IANA zone: {timezone}") from err


def validate_per_run_limits(limits: CrawlPerRunLimits) -> None:
    """Validate optional per-run budgets before they enter an immutable plan.

    ``None`` selects the execution service's bounded default. Explicit values
    must be positive integers and stay below the operational caps; zero,
    negative values, booleans, and oversized values are rejected fail-closed.
    """

    fields = (
        (
            "max_postings_per_source",
            limits.max_postings_per_source,
            MAX_PER_RUN_POSTINGS_PER_SOURCE,
        ),
        ("max_sources", limits.max_sources, MAX_PER_RUN_SOURCES),
        ("timeout_seconds", limits.timeout_seconds, MAX_PER_RUN_TIMEOUT_SECONDS),
    )
    for name, value, maximum in fields:
        if value is None:
            continue
        if type(value) is not int:
            raise InvalidStateError(f"per_run_limits.{name} must be an integer")
        if value < 1:
            raise InvalidStateError(f"per_run_limits.{name} must be positive")
        if value > maximum:
            raise InvalidStateError(f"per_run_limits.{name} exceeds maximum {maximum}")


def compute_next_run_at(*, interval_seconds: int, timezone: str, now: datetime | None) -> datetime:
    """Return the next run time = ``now + interval`` in the plan's timezone.

    ``now`` defaults to :func:`datetime.now` in UTC. The returned datetime is
    timezone-aware and normalized into the schedule's IANA zone so the UI can
    render it without re-interpreting the offset. ``interval_seconds`` and
    ``timezone`` are assumed already validated by :func:`validate_schedule`.
    """
    base = now or datetime.now(tz=UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return (base + timedelta(seconds=interval_seconds)).astimezone(zone)


# ---------------------------------------------------------------------------
# Repository protocols (Protocol seam so routes depend on the service while
# tests substitute the in-memory repository). Satisfied by the Postgres and
# in-memory implementations in infrastructure/.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CrawlSourceService
# ---------------------------------------------------------------------------


class CrawlSourceService:
    """Register and control trusted job sources (tasks 4.2 / 4.3 / 4.5).

    A source DECLARES SCOPE ONLY: its trust / terms / robots status is recorded
    for display and provenance, but the crawl policy layer
    (:mod:`careerops.application.crawl_policy`) stays authoritative and fails
    closed on ``blocked`` / unknown (Iron Rule 6). Unsupported source types are
    refused here via :data:`SUPPORTED_SOURCE_TYPES` until an adapter + policy
    contract exists (spec: "Unsupported source types SHALL remain disabled").
    """

    def __init__(self, repository: _SourceRepoProtocol) -> None:
        self._repo = repository

    # -- reads --------------------------------------------------------------

    def get_source(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        return self._repo.get_by_id(owner_id, source_id)

    def list_sources(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        return self._repo.list_for(owner_id, limit=limit)

    # -- writes -------------------------------------------------------------

    def register(
        self,
        owner_id: UUID,
        *,
        company_id: UUID,
        source_type: str,
        source_identifier: str,
        base_url: str,
        executor_mode: str = "http",
        enabled: bool = False,
        adapter_version: str = "",
        trust_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN,
        terms_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN,
        robots_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN,
        now: datetime | None = None,
    ) -> CrawlSource:
        """Register a new source. ``enabled`` is the user on/off toggle.

        Raises :class:`InvalidStateError` for an unsupported source type or an
        empty identifier / URL. The operational ``state`` starts ACTIVE — a
        registered source from a supported adapter is operationally live; the
        crawl policy layer (Section 5) is the real safety gate and fails closed
        while trust / terms / robots are UNKNOWN.
        """
        if not is_supported_source_type(source_type):
            raise InvalidStateError(
                f"unsupported crawl source type '{source_type}'; "
                "no adapter and policy contract is registered"
            )
        if not source_identifier:
            raise InvalidStateError("crawl source source_identifier is required")
        if not base_url:
            raise InvalidStateError("crawl source base_url is required")
        try:
            selected_executor = CrawlExecutorMode(executor_mode)
        except ValueError as err:
            raise InvalidStateError("unsupported crawl executor mode") from err
        source = CrawlSource(
            id=uuid4(),
            owner_id=owner_id,
            company_id=company_id,
            source_type=CrawlSourceType(source_type),
            source_identifier=source_identifier,
            base_url=base_url,
            executor_mode=selected_executor,
            state=CrawlSourceState.ACTIVE,
            trust_status=trust_status,
            terms_status=terms_status,
            robots_status=robots_status,
            adapter_version=adapter_version,
            enabled=enabled,
            verified_at=now,
            created_at=now,
            updated_at=now,
        )
        return self._repo.save(source)

    def update(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        base_url: str | None = None,
        source_identifier: str | None = None,
        executor_mode: str | None = None,
        adapter_version: str | None = None,
        trust_status: CrawlPolicyStatus | None = None,
        terms_status: CrawlPolicyStatus | None = None,
        robots_status: CrawlPolicyStatus | None = None,
        state: CrawlSourceState | None = None,
        now: datetime | None = None,
    ) -> CrawlSource:
        """Patch the editable fields. ``enabled`` is owned by pause/resume."""
        existing = self._repo.get_by_id(owner_id, source_id)
        merged_base = base_url if base_url is not None else existing.base_url
        merged_ident = (
            source_identifier if source_identifier is not None else existing.source_identifier
        )
        if not merged_base:
            raise InvalidStateError("crawl source base_url is required")
        if not merged_ident:
            raise InvalidStateError("crawl source source_identifier is required")
        if executor_mode is None:
            selected_executor = existing.executor_mode
        else:
            try:
                selected_executor = CrawlExecutorMode(executor_mode)
            except ValueError as err:
                raise InvalidStateError("unsupported crawl executor mode") from err
        merged = dataclasses.replace(
            existing,
            base_url=merged_base,
            source_identifier=merged_ident,
            executor_mode=selected_executor,
            adapter_version=(
                adapter_version if adapter_version is not None else existing.adapter_version
            ),
            trust_status=trust_status if trust_status is not None else existing.trust_status,
            terms_status=terms_status if terms_status is not None else existing.terms_status,
            robots_status=robots_status if robots_status is not None else existing.robots_status,
            state=state if state is not None else existing.state,
            updated_at=now or datetime.now(tz=UTC),
        )
        return self._repo.save(merged)

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        """Delete the source. Retention of prior postings/evidence is the
        crawler's job (the spec's retirement semantics); the row itself is
        removed here on explicit user action."""
        self._repo.remove(owner_id, source_id)

    # -- pause / resume (task 4.5) -----------------------------------------

    def pause(self, owner_id: UUID, source_id: UUID, *, now: datetime | None = None) -> CrawlSource:
        """Idempotent: flip ``enabled`` off. Retains prior postings, evidence,
        and run history (spec: "prevents new runs ... while retaining prior
        postings, evidence, and run history"). Re-pausing an already-paused
        source is a no-op."""
        return self._repo.update_state(
            owner_id, source_id, enabled=False, state=CrawlSourceState.PAUSED, now=now
        )

    def resume(
        self, owner_id: UUID, source_id: UUID, *, now: datetime | None = None
    ) -> CrawlSource:
        """Idempotent: flip ``enabled`` on and return the source to ACTIVE if
        it was PAUSED. A BLOCKED source is not resumed (the policy layer owns
        that transition); resuming an already-enabled source is a no-op."""
        existing = self._repo.get_by_id(owner_id, source_id)
        if existing.state is CrawlSourceState.BLOCKED:
            raise InvalidStateError("cannot resume a policy-blocked crawl source")
        next_state = (
            CrawlSourceState.ACTIVE if existing.state is CrawlSourceState.PAUSED else existing.state
        )
        return self._repo.update_state(owner_id, source_id, enabled=True, state=next_state, now=now)


# ---------------------------------------------------------------------------
# CrawlPlanService
# ---------------------------------------------------------------------------


class CrawlPlanService:
    """Versioned crawl-plan lifecycle (tasks 4.4 / 4.5 / 4.6).

    Editing the active plan creates a NEW immutable version (copy-on-write,
    design Decision 2); the prior version is preserved for provenance and the
    new version applies only to future runs. The repository performs the
    deactivate-prior + activate-new pair in one transaction so the per-owner
    partial unique index (one active version) is never transiently violated
    (mirrors :class:`careerops.application.profile_service.ProfileService`).

    Plan head-state is DERIVED from the active version (there is no separate
    head-state column — KISS, additive schema): an active version means ACTIVE;
    versions but none active means PAUSED; no versions means DRAFT. ``pause``
    deactivates the active version (no new scheduled runs); ``resume``
    re-activates the latest version.
    """

    def __init__(self, repository: _PlanRepoProtocol) -> None:
        self._repo = repository

    # -- reads --------------------------------------------------------------

    def get_active(self, owner_id: UUID) -> CrawlPlanVersion | None:
        return self._repo.get_active_for(owner_id)

    def get_version(self, owner_id: UUID, version_id: UUID) -> CrawlPlanVersion:
        return self._repo.get_by_id(owner_id, version_id)

    def list_versions(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlPlanVersion]:
        return self._repo.list_versions(owner_id, limit=limit)

    def latest_version(self, owner_id: UUID) -> CrawlPlanVersion | None:
        """Return the highest-numbered version regardless of active state.

        Used by :meth:`resume` (re-activate the latest) and by the UI to show
        the current plan head even while paused. ``None`` when the owner has no
        versions yet.
        """
        versions = self._repo.list_versions(owner_id, limit=50)
        return versions[0] if versions else None

    # -- writes -------------------------------------------------------------

    def create_version(
        self,
        owner_id: UUID,
        preferences: CrawlPlanPreferences,
        *,
        activate: bool = True,
        now: datetime | None = None,
    ) -> CrawlPlanVersion:
        """Validate, persist, and (by default) activate a new plan version.

        Schedule validation runs BEFORE the version is constructed so an
        :class:`InvalidStateError` never displaces the prior active version
        (spec: "Schedule is invalid ... rejects the plan and does not schedule
        a run"). When ``activate`` is true the repository atomically
        deactivates the prior active version and activates this one.
        """
        validate_schedule(
            interval_seconds=preferences.schedule_interval_seconds,
            timezone=preferences.schedule_timezone,
        )
        validate_per_run_limits(preferences.per_run_limits)
        version_number = self._next_version_number(owner_id)
        plan = CrawlPlanVersion(
            id=uuid4(),
            owner_id=owner_id,
            version=version_number,
            is_active=activate,
            sources=preferences.sources,
            themes=preferences.themes,
            include_keywords=preferences.include_keywords,
            exclude_keywords=preferences.exclude_keywords,
            role_families=preferences.role_families,
            locations=preferences.locations,
            remote_rules=preferences.remote_rules,
            seniority=preferences.seniority,
            compensation=preferences.compensation,
            content_scope=preferences.content_scope,
            interval_seconds=preferences.schedule_interval_seconds,
            timezone=preferences.schedule_timezone,
            per_run_limits=preferences.per_run_limits,
            rules_version=CRAWL_PLAN_RULES_VERSION,
            created_at=now,
        )
        return self._repo.create_version(plan)

    def activate_version(
        self, owner_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> CrawlPlanVersion:
        """Activate an existing version (deactivates the prior active one).

        Re-validates the schedule against the current bounds so a version
        created under an older rules-version with a now-invalid schedule cannot
        become active (Iron Rule 5)."""
        version = self._repo.get_by_id(owner_id, version_id)
        validate_schedule(interval_seconds=version.interval_seconds, timezone=version.timezone)
        validate_per_run_limits(version.per_run_limits)
        return self._repo.activate(owner_id, version_id, now=now)

    # -- pause / resume (task 4.5) -----------------------------------------

    def pause(self, owner_id: UUID, *, now: datetime | None = None) -> CrawlPlanVersion | None:
        """Idempotent: deactivate the active version so the scheduler fires no
        new runs. Prior versions, postings, evidence and run history are
        preserved (the rows are untouched; only ``is_active`` flips). Returns
        the formerly-active version (or ``None`` if the plan was already
        paused / had no active version)."""
        active = self._repo.get_active_for(owner_id)
        if active is None:
            return None
        self._repo.deactivate_all(owner_id, now=now)
        return dataclasses.replace(active, is_active=False)

    def resume(self, owner_id: UUID, *, now: datetime | None = None) -> CrawlPlanVersion:
        """Idempotent: re-activate the latest version.

        Raises :class:`InvalidStateError` if the owner has no versions to
        resume. Re-validates the schedule so a paused plan whose schedule
        became invalid (e.g. timezone renamed) cannot resume silently. A plan
        that is already active is a no-op and returns the active version
        unchanged.
        """
        active = self._repo.get_active_for(owner_id)
        if active is not None:
            return active
        latest = self._repo.list_versions(owner_id, limit=1)
        if not latest:
            raise InvalidStateError("no crawl plan version to resume")
        target = latest[0]
        validate_schedule(interval_seconds=target.interval_seconds, timezone=target.timezone)
        validate_per_run_limits(target.per_run_limits)
        return self._repo.activate(owner_id, target.id, now=now)

    # -- schedule (task 4.6) -----------------------------------------------

    def next_run_at(self, owner_id: UUID, *, now: datetime | None = None) -> datetime | None:
        """Return the computed next-run time for the active plan, or ``None``
        if the plan is paused / has no active version. The value is derived
        (``now + interval`` in the plan's IANA timezone); it is NOT persisted
        — the scheduler recomputes it on each tick so a resumed plan does not
        immediately fire a stale, overdue run."""
        active = self._repo.get_active_for(owner_id)
        if active is None:
            return None
        return compute_next_run_at(
            interval_seconds=active.interval_seconds,
            timezone=active.timezone,
            now=now,
        )

    # -- helpers ------------------------------------------------------------

    def _next_version_number(self, owner_id: UUID) -> int:
        history = self._repo.list_versions(owner_id, limit=50)
        if not history:
            return 1
        return max(v.version for v in history) + 1


# ---------------------------------------------------------------------------
# CrawlRunService
# ---------------------------------------------------------------------------


def _source_set_hash(source_ids: tuple[UUID, ...]) -> str:
    """Stable 12-hex digest over the sorted source-id set.

    Sorting makes the hash order-independent so a run's identity is stable
    even if the plan's source tuple is reordered. Used as part of the
    ``run_identity`` so two runs over the same plan + source set collide on
    purpose (and the idempotency read catches the duplicate)."""
    if not source_ids:
        return "empty"
    payload = "|".join(str(sid) for sid in sorted(source_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _is_non_terminal(state: CrawlRunState) -> bool:
    return state in (CrawlRunState.PENDING, CrawlRunState.RUNNING)


class CrawlRunService:
    """Manual run-now + overlap rule + run history (tasks 4.5 / 4.6 / 4.7).

    ``run_now`` binds to the active plan-version snapshot + the eligible source
    set (the plan's sources filtered to ``enabled AND ACTIVE``) + a stable
    ``run_identity`` (Iron Rule 4). Idempotent: a repeat while the prior run is
    PENDING/RUNNING returns the existing run; only after a terminal state does
    a new attempt mint a new identity (so a user can deliberately re-run after
    completion). Temporal execution is Section 5 — here we only record the
    PENDING run identity + terminal counters.
    """

    def __init__(
        self,
        run_repository: _RunRepoProtocol,
        *,
        plan_repository: _PlanRepoProtocol,
        source_repository: _SourceRepoProtocol,
    ) -> None:
        self._runs = run_repository
        self._plans = plan_repository
        self._sources = source_repository

    # -- reads --------------------------------------------------------------

    def get_run(self, owner_id: UUID, run_id: UUID) -> CrawlRun:
        return self._runs.get_by_id(owner_id, run_id)

    def list_runs(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlRun]:
        return self._runs.list_for_owner(owner_id, limit=limit)

    def list_runs_for_plan(
        self, owner_id: UUID, plan_version_id: UUID, *, limit: int = 50
    ) -> list[CrawlRun]:
        # Verify ownership of the plan version before listing its runs so a
        # client-supplied plan_version_id for another owner leaks nothing.
        self._plans.get_by_id(owner_id, plan_version_id)
        return self._runs.list_for_plan(owner_id, plan_version_id, limit=limit)

    def find_non_terminal_run(self, owner_id: UUID, plan_version_id: UUID) -> CrawlRun | None:
        """Return the most recent PENDING/RUNNING run for the plan version, or
        ``None``. This is the overlap detector (task 4.6): the scheduler asks
        this before firing; a non-``None`` answer means the trigger MUST
        coalesce/skip rather than start a second crawl."""
        runs = self._runs.list_for_plan(owner_id, plan_version_id, limit=50)
        for run in runs:
            if _is_non_terminal(run.state):
                return run
        return None

    # -- writes -------------------------------------------------------------

    def run_now(
        self,
        owner_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        """Create (or return the existing) PENDING run for the active plan.

        Iron Rule 4 idempotency: if a PENDING/RUNNING run already exists for
        the active plan version, it is returned unchanged — a repeat click
        while the prior run is in flight MUST NOT start a second crawl or
        duplicate postings. Only when the prior run has reached a terminal
        state does this mint a new identity (next attempt) and create a fresh
        PENDING run.

        Requires an ACTIVE plan (spec: "an immediate run for an active plan");
        a paused plan raises :class:`InvalidStateError`. The eligible source
        set is the plan's sources filtered to ``enabled AND ACTIVE``; an empty
        eligible set raises :class:`InvalidStateError` so the user gets an
        actionable message instead of a no-op run.
        """
        active = self._plans.get_active_for(owner_id)
        if active is None:
            raise InvalidStateError(
                "crawl plan has no active version; resume the plan before running"
            )
        # Overlap/idempotency: a PENDING/RUNNING run for THIS plan version is
        # the existing in-flight attempt — return it (task 4.5 idempotency).
        existing = self.find_non_terminal_run(owner_id, active.id)
        if existing is not None:
            return existing
        source_set = self._resolve_eligible_sources(owner_id, active.sources)
        if not source_set:
            raise InvalidStateError(
                "crawl plan has no eligible sources; enable at least one active source"
            )
        run_identity = self._mint_identity(active.id, source_set, owner_id)
        run = CrawlRun(
            id=uuid4(),
            plan_version_id=active.id,
            run_identity=run_identity,
            source_set=source_set,
            state=CrawlRunState.PENDING,
            limits=active.per_run_limits,
            counters=CrawlRunCounters(),
            created_at=now,
        )
        try:
            return self._runs.create(owner_id, run)
        except ConflictError:
            # Two concurrent requests can both observe no non-terminal run and
            # race on the same deterministic identity. The unique constraint
            # is the arbiter; recover the winner instead of surfacing a false
            # duplicate or creating a second logical run.
            existing = self._runs.get_by_identity(owner_id, run_identity)
            if existing is not None:
                return existing
            raise

    def record_overlap_skip(
        self,
        owner_id: UUID,
        *,
        plan_version_id: UUID,
        source_set: tuple[UUID, ...],
        reason: str = "overlap_with_running",
        now: datetime | None = None,
    ) -> CrawlRun:
        """Record a CANCELLED run for a scheduled trigger that overlapped an
        already-running source/plan (task 4.6 overlap rule).

        Called by the scheduler (Section 5) when a trigger fires while a
        non-terminal run already exists for the same plan/source set. Instead
        of starting a second unbounded crawl, the trigger records a CANCELLED
        run carrying the coalesce/skip reason so the user can see in history
        why the scheduled slot was skipped. The plan version MUST belong to the
        owner (Iron Rule 2 transitive ownership).
        """
        self._plans.get_by_id(owner_id, plan_version_id)
        fired_at = now or datetime.now(tz=UTC)
        run = CrawlRun(
            id=uuid4(),
            plan_version_id=plan_version_id,
            # Include fired-at microseconds so repeat overlap skips never
            # collide on the run_identity unique constraint.
            run_identity=(
                f"overlap:{plan_version_id}:{_source_set_hash(source_set)}:"
                f"{fired_at.strftime('%Y%m%dT%H%M%S%f')}"
            ),
            source_set=source_set,
            state=CrawlRunState.CANCELLED,
            error_category=reason,
            created_at=fired_at,
            ended_at=fired_at,
        )
        return self._runs.create(owner_id, run)

    # -- helpers ------------------------------------------------------------

    def _resolve_eligible_sources(
        self, owner_id: UUID, source_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Return the subset of ``source_ids`` that are ``enabled AND ACTIVE``.

        A paused or blocked source schedules no new runs (spec: "User pauses a
        source ... prevents new runs from starting for that source"). A source
        id that no longer exists is silently dropped (the plan version is a
        snapshot; a deleted source does not break the run).
        """
        eligible: list[UUID] = []
        for source_id in source_ids:
            try:
                source = self._sources.get_by_id(owner_id, source_id)
            except NotFoundError:
                continue
            if source.enabled and source.state is CrawlSourceState.ACTIVE:
                eligible.append(source.id)
        return tuple(eligible)

    def _mint_identity(
        self, plan_version_id: UUID, source_set: tuple[UUID, ...], owner_id: UUID
    ) -> str:
        """Mint a unique ``run_identity`` for a new manual run.

        Identity = ``manual:{plan_version_id}:{source_set_hash}:{attempt}``
        where ``attempt`` is one past the count of runs already bound to this
        plan version. The identity is therefore stable for the lifetime of one
        attempt (worker retries in Section 5 reuse it) and advances only when
        the prior attempt is terminal — at which point a new run is desired and
        a new identity is required (the DB unique constraint would reject a
        reuse).
        """
        count_for_plan = getattr(self._runs, "count_for_plan", None)
        if callable(count_for_plan):
            count_fn = cast("Callable[[UUID, UUID], int]", count_for_plan)
            attempt = count_fn(owner_id, plan_version_id) + 1
        else:
            # Compatibility for older test doubles; never cap the history at
            # the overlap-read page size when minting a unique identity.
            existing = self._runs.list_for_plan(owner_id, plan_version_id, limit=100_000)
            attempt = len(existing) + 1
        return f"manual:{plan_version_id}:{_source_set_hash(source_set)}:{attempt}"


# Re-export the domain transition table so route/service callers can validate
# a terminal-state update without reaching into the domain module separately.
__all__ += ["ALLOWED_TRANSITIONS"]
