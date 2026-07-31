"""Domain models for user-managed crawl sources, versioned plans, and runs.

End-to-end-career-application-loop, Section 4 (crawl-plan-management spec,
tasks 4.1-4.4).

Three concepts back the "采集计划" workspace (design Decision 2):

- :class:`CrawlSource` — a registered ATS / official company careers source
  (Greenhouse / Lever / Ashby / official) plus its trust, terms and robots
  policy status. Sources DECLARE SCOPE ONLY; they cannot bypass the SSRF /
  rate-limit / terms / robots policy in :mod:`careerops.application.crawl_policy`
  (Iron Rule 6). Source registration reuses the existing adapter registry in
  :mod:`careerops.infrastructure.temporal.m1_crawl_sink`; unsupported types are
  refused by the service layer via :data:`SUPPORTED_SOURCE_TYPES`.

- :class:`CrawlPlanVersion` — an immutable copy-on-write snapshot of the user's
  crawl preferences (source set, themes, keyword rules, role families, location
  / remote / seniority / compensation filters, content scope, schedule and
  per-run limits). Editing an active plan creates a NEW version and preserves
  the old one for provenance; the new version applies only to future runs
  (design Decision 2). Exactly one version is active per owner at a time
  (DB-enforced by a partial unique index on ``crawl_plan_versions``).

- :class:`CrawlRun` — a single execution bound to one plan-version snapshot,
  source set and run identity (crawl-plan-management spec). Replay-safe by
  ``run_identity`` uniqueness: a worker retry that reuses the same identity
  MUST NOT create duplicate postings or duplicate content versions. Section 4
  only records the run identity + terminal counters; the actual ingest
  idempotency lives in
  :class:`careerops.infrastructure.temporal.m1_crawl_sink.RealCrawlActivitySink`
  (Section 5).

Server-side ownership (Iron Rule 2): every repository method takes an
``owner_id`` parameter that the caller resolves server-side from the
``candidates`` table (the console-login user model was removed; there is no
``console_users`` lookup anymore). The repositories scope every read/write
by that id; a client-supplied owner substitute is never honored.

The lifecycle enums :class:`CrawlPlanState` and :class:`CrawlRunState` already
live in :mod:`careerops.domain.crawl` (Phase 0 contract freeze) and are
imported here, NOT redefined.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from careerops.domain.crawl import CrawlRunState
from careerops.domain.profiles import (
    CompensationPreference,
    LocationPreference,
    RemoteRules,
)

__all__ = [
    "CRAWL_RUN_COUNTER_KEYS",
    "SUPPORTED_SOURCE_TYPES",
    "CrawlExecutorMode",
    "CrawlPerRunLimits",
    "CrawlPlanRepository",
    "CrawlPlanVersion",
    "CrawlPolicyStatus",
    "CrawlRun",
    "CrawlRunCounters",
    "CrawlRunRepository",
    "CrawlSource",
    "CrawlSourceRepository",
    "CrawlSourceState",
    "CrawlSourceType",
    "CrawlTrustLevel",
    "is_supported_source_type",
]


# ---------------------------------------------------------------------------
# Source-type + status closures (validated against the adapter registry by the
# service layer; the domain only freezes the value set here).
# ---------------------------------------------------------------------------


class CrawlSourceType(StrEnum):
    """Registered crawl source types.

    These map to the existing adapter registry keys in
    :mod:`careerops.infrastructure.temporal.m1_crawl_sink`. ``OFFICIAL`` is the
    meta-type for an official company careers page; its concrete list adapter
    (json_ld / sitemap / static_html) is resolved at fetch time by Section 5's
    execution layer. The service layer rejects any source type not in this enum
    with ``unsupported_source`` until an adapter + policy contract exists
    (crawl-plan-management spec: "Unsupported source types SHALL remain disabled
    until an adapter and policy contract exist").
    """

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    OFFICIAL = "official"


class CrawlExecutorMode(StrEnum):
    """The explicitly selected network executor for one registered source."""

    HTTP = "http"
    EGO = "ego"


SUPPORTED_SOURCE_TYPES: frozenset[CrawlSourceType] = frozenset(CrawlSourceType)


def is_supported_source_type(value: str) -> bool:
    """Return True if ``value`` is a registered crawl source type.

    The service layer calls this when validating a registration request before
    it constructs a :class:`CrawlSource`. The adapter-registry-level check
    (does the literal adapter object exist for this type) is deferred to the
    execution layer; this closure only freezes the domain value set.
    """
    try:
        CrawlSourceType(value)
    except ValueError:
        return False
    return True


class CrawlPolicyStatus(StrEnum):
    """Closure of trust / terms / robots policy status values.

    Mirrors the ``companies.terms_status`` CHECK values (unknown / allowed /
    blocked). A source declares these; the crawl policy layer in
    :mod:`careerops.application.crawl_policy` stays authoritative and fails
    closed on ``blocked`` (Iron Rule 6).
    """

    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    BLOCKED = "blocked"


# Alias retained for readability at call sites that mean "trust level" vs
# "terms status" vs "robots status" — they share the value space today.
CrawlTrustLevel = CrawlPolicyStatus


class CrawlSourceState(StrEnum):
    """Lifecycle state of a registered source.

    Mirrors the existing ``job_sources.state`` CHECK values (migration 0001).
    ``ACTIVE`` sources may participate in runs; ``PAUSED`` is the user toggle
    that retains prior postings/evidence/run history but schedules no new runs;
    ``PENDING_REVIEW`` is the initial state for an unverified source;
    ``BLOCKED`` covers policy-blocked sources.
    """

    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Structured value types (schedule, limits, counters)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlSchedule:
    """Schedule declaration for a plan version.

    ``interval_seconds`` is the minimum spacing between runs. ``timezone`` is
    an IANA zone name; the service layer validates it via ``zoneinfo`` and
    rejects unparseable / non-IANA values with ``invalid_schedule`` (task 4.6).
    Bounded intervals are enforced there too — the domain carries the values;
    the service owns the safety checks (Iron Rule 5).
    """

    interval_seconds: int
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class CrawlPerRunLimits:
    """Per-run bounds. ``None`` means "use the system default" (service-resolved)."""

    max_postings_per_source: int | None = None
    max_sources: int | None = None
    timeout_seconds: int | None = None


# The counter keys persisted in ``crawl_runs.counters`` jsonb. The repo layer
# round-trips these names through :class:`CrawlRunCounters`; keeping them in a
# single tuple avoids drift between the migration's CHECK shape and the repo's
# serialization.
CRAWL_RUN_COUNTER_KEYS: tuple[str, ...] = ("discovered", "updated", "closed", "failed")


@dataclass(frozen=True, slots=True)
class CrawlRunCounters:
    """Terminal result counters for a crawl run.

    Section 4 only RECORDS these; the actual posting/version ingest that
    produces them is delegated to the m1_crawl_sink in Section 5. ``failed``
    counts source/parser failures that did not stop the whole run.
    """

    discovered: int = 0
    updated: int = 0
    closed: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# CrawlSource
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlSource:
    """A registered job source owned by the single user.

    ``owner_id`` is the server-resolved candidate id (Iron Rule 2). The
    existing ``job_sources`` table has no owner column because the runtime is
    single-user; the postgres repo stamps the resolved owner onto the model on
    read so the service / API layer sees a uniform ownership field across
    sources, plans and runs. ``enabled`` is the user on/off toggle; ``state``
    is the operational lifecycle. ``last_run_metadata`` is treated as immutable
    even though it is stored as a dict (same pattern as
    :class:`careerops.adapters.job_sources.RawJobRecord.raw_data`).
    """

    id: UUID
    owner_id: UUID
    company_id: UUID
    source_type: CrawlSourceType
    source_identifier: str
    base_url: str
    executor_mode: CrawlExecutorMode = CrawlExecutorMode.HTTP
    state: CrawlSourceState = CrawlSourceState.PENDING_REVIEW
    trust_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN
    terms_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN
    robots_status: CrawlPolicyStatus = CrawlPolicyStatus.UNKNOWN
    adapter_version: str = ""
    enabled: bool = False
    verified_at: datetime | None = None
    last_discovery_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_metadata: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# CrawlPlanVersion
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlPlanVersion:
    """An immutable, copy-on-write crawl plan version.

    ``version`` is per-owner monotonically increasing; ``is_active`` is true
    for at most one version per owner (DB-enforced by a partial unique index on
    ``crawl_plan_versions``). ``sources`` is the selected source id set;
    ``themes`` / ``role_families`` / ``seniority`` are free-form tag tuples;
    ``include_keywords`` / ``exclude_keywords`` are keyword rules; ``locations``
    / ``remote_rules`` / ``compensation`` reuse the profile structured types
    (Section 2/3) so the same validation / filtering code path applies.
    ``content_scope`` is a stable label (e.g. ``"listings_only"``). Schedule
    and limits are the two structured groups the run executor consumes.

    The plan version is the snapshot a run binds to (Iron Rule 4); editing the
    active plan creates a new version rather than mutating this one.
    """

    id: UUID
    owner_id: UUID
    version: int
    is_active: bool = False
    sources: tuple[UUID, ...] = ()
    themes: tuple[str, ...] = ()
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    role_families: tuple[str, ...] = ()
    locations: tuple[LocationPreference, ...] = ()
    remote_rules: RemoteRules = field(default_factory=RemoteRules)
    seniority: tuple[str, ...] = ()
    compensation: CompensationPreference = field(default_factory=CompensationPreference)
    content_scope: str = ""
    interval_seconds: int = 0
    timezone: str = "UTC"
    per_run_limits: CrawlPerRunLimits = field(default_factory=CrawlPerRunLimits)
    rules_version: str = ""
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# CrawlRun
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlRun:
    """A single crawl run execution bound to a plan-version snapshot.

    ``run_identity`` is the idempotency key: a worker retry that reuses it (and
    the same plan-version + source set) MUST NOT create duplicate postings or
    versions (Iron Rule 4). The DB enforces ``run_identity`` uniqueness. ``state``
    transitions are governed by :data:`careerops.domain.crawl.ALLOWED_TRANSITIONS`;
    Section 4 records PENDING on creation and terminal counters on completion.
    ``next_eligible_at`` records the source-specific backoff time the policy
    layer requires before the next run may touch the same source.
    """

    id: UUID
    plan_version_id: UUID
    run_identity: str
    source_set: tuple[UUID, ...] = ()
    state: CrawlRunState = CrawlRunState.PENDING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    limits: CrawlPerRunLimits = field(default_factory=CrawlPerRunLimits)
    counters: CrawlRunCounters = field(default_factory=CrawlRunCounters)
    error_category: str = ""
    next_eligible_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Repository Protocols
#
# Every method takes the server-resolved ``owner_id`` (Iron Rule 2). The
# concrete postgres + memory implementations live in
# :mod:`careerops.infrastructure.database.postgres_crawl_repo` and
# :mod:`careerops.infrastructure.memory_repos`.
# ---------------------------------------------------------------------------


@runtime_checkable
class CrawlSourceRepository(Protocol):
    """Source CRUD scoped by the server-resolved owner."""

    def get_by_id(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        """Return the source or raise :class:`NotFoundError`.

        ``owner_id`` is the server-resolved candidate. In the single-user
        runtime every source belongs to that candidate; the param is the
        server-side resolution point so a future multi-user schema change only
        adds a WHERE clause.
        """
        ...

    def list_for(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        """Return the owner's sources, newest first."""
        ...

    def save(self, source: CrawlSource) -> CrawlSource:
        """Insert or update (idempotent on ``id``). Returns the persisted row."""
        ...

    def update_state(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        state: CrawlSourceState | None = None,
        enabled: bool | None = None,
        trust_status: CrawlPolicyStatus | None = None,
        terms_status: CrawlPolicyStatus | None = None,
        robots_status: CrawlPolicyStatus | None = None,
        adapter_version: str | None = None,
        last_run_at: datetime | None = None,
        last_run_metadata: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> CrawlSource:
        """Patch the lifecycle / policy / last-run fields. Raises
        :class:`NotFoundError` if the source does not exist."""
        ...

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        """Delete the source (retention/retirement is the service's job)."""
        ...

    def get_by_identity(
        self,
        owner_id: UUID,
        company_id: UUID,
        source_type: CrawlSourceType,
        source_identifier: str,
    ) -> CrawlSource | None:
        """Return the source matching the business unique key
        ``(company_id, source_type, source_identifier)``, or None.

        Phase 5.4 discovery dedup: lets the normalizer find the canonical row
        for a discovered source without knowing its id.
        """
        ...

    def upsert_by_identity(self, source: CrawlSource) -> CrawlSource:
        """Insert or update keyed by the business unique key so two discovery
        events for the same source collapse to ONE row (Phase 5.4). On conflict
        the existing row's id wins; mutable discovery fields are refreshed.

        Distinct from :meth:`save` (idempotent on ``id``), which is used by the
        seed path where ids are deterministic.
        """
        ...


@runtime_checkable
class CrawlPlanRepository(Protocol):
    """Versioned plan store. Immutable versions; one active per owner."""

    def create_version(self, plan: CrawlPlanVersion) -> CrawlPlanVersion:
        """Persist a new immutable version. If ``plan.is_active`` is true the
        prior active version (if any) is deactivated in the same transaction."""
        ...

    def activate(
        self, owner_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> CrawlPlanVersion:
        """Activate one version and deactivate the prior active version.

        Performed in a single transaction so the partial unique index
        ``ix_crawl_plan_versions_owner_active`` is never transiently violated.
        Raises :class:`NotFoundError` if the version id is not owned by
        ``owner_id``.
        """
        ...

    def get_active_for(self, owner_id: UUID) -> CrawlPlanVersion | None:
        """Return the active plan version for the owner, or None."""
        ...

    def get_by_id(self, owner_id: UUID, version_id: UUID) -> CrawlPlanVersion:
        """Return the version, scoped by owner. Raises :class:`NotFoundError`
        if the version does not exist or belongs to a different owner."""
        ...

    def list_versions(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlPlanVersion]:
        """Return the owner's plan versions, newest version first."""
        ...

    def deactivate_all(self, owner_id: UUID, *, now: datetime | None = None) -> None:
        """Flip the owner's active plan (if any) to inactive — used by the
        ARCHIVE / pause service path before a head-state change."""
        ...


@runtime_checkable
class CrawlRunRepository(Protocol):
    """Run records scoped transitively via the plan version's owner."""

    def create(self, owner_id: UUID, run: CrawlRun) -> CrawlRun:
        """Insert a new PENDING run.

        Validates that ``run.plan_version_id`` belongs to ``owner_id`` (Iron
        Rule 2 transitive ownership). Raises :class:`ConflictError` if
        ``run.run_identity`` already exists (caller should use
        :meth:`get_by_identity` to recover the existing record — replay-safe
        idempotency, Iron Rule 4).
        """
        ...

    def get_by_id(self, owner_id: UUID, run_id: UUID) -> CrawlRun:
        """Return the run, scoped transitively via its plan version's owner.

        Raises :class:`NotFoundError` if the run does not exist or its plan
        version belongs to a different owner.
        """
        ...

    def get_by_identity(self, owner_id: UUID, run_identity: str) -> CrawlRun | None:
        """Return the run matching ``run_identity`` for the owner, or None.

        Idempotency read: a worker retry calls this before inserting to decide
        whether to reuse the existing run record.
        """
        ...

    def list_for_owner(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlRun]:
        """Return the owner's runs across all plan versions, newest first."""
        ...

    def list_for_plan(
        self, owner_id: UUID, plan_version_id: UUID, *, limit: int = 50
    ) -> list[CrawlRun]:
        """Return runs bound to the given plan version, newest first."""
        ...

    def count_for_plan(self, owner_id: UUID, plan_version_id: UUID) -> int:
        """Return the total number of owner-scoped runs for a plan version."""
        ...

    def update_terminal(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        state: CrawlRunState,
        counters: CrawlRunCounters | None = None,
        error_category: str | None = None,
        next_eligible_at: datetime | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CrawlRun:
        """Record a terminal (or RUNNING) state + counters on a run.

        The caller validates the transition against
        :data:`careerops.domain.crawl.ALLOWED_TRANSITIONS`; the repo only
        persists the supplied fields. Raises :class:`NotFoundError` if the run
        does not exist or is not owned by ``owner_id``.
        """
        ...
