"""PostgreSQL-backed repositories for crawl sources, plan versions, and runs.

end-to-end-career-application-loop, Section 4 (crawl-plan-management spec,
tasks 4.1-4.4).

Three concrete implementations of the Protocols in
:mod:`careerops.domain.crawl_plans`:

- :class:`PostgresCrawlSourceRepository` — CRUD on the existing ``job_sources``
  table (extended additively by migration 0015 with trust / terms / robots /
  adapter_version / enabled / last-run columns). The table has no owner column
  (single-user runtime); the resolved ``owner_id`` is stamped onto the model on
  read so the API/service layer sees a uniform ownership field across sources,
  plans and runs.

- :class:`PostgresCrawlPlanRepository` — immutable copy-on-write plan versions
  on ``crawl_plan_versions``. Exactly one version is active per owner; the
  activate path performs the deactivate-prior + activate-new pair inside a
  single transaction so the partial unique index
  ``ix_crawl_plan_versions_owner_active`` is never transiently violated
  (mirrors :class:`careerops.infrastructure.database.postgres_profile_repo`
  `.PostgresProfileRepository`).

- :class:`PostgresCrawlRunRepository` — run records on ``crawl_runs``, scoped
  transitively via the plan version's owner (ownership is a JOIN through
  ``plan_version_id``). ``run_identity`` uniqueness is the idempotency contract
  (Iron Rule 4); a duplicate insert raises :class:`ConflictError`.

Server-side candidate ownership (Iron Rule 2): every method takes an
``owner_id`` parameter that the caller resolves from the authenticated console
user (``console_users.candidate_id``). The repositories scope every read/write
by that id.

This module is intentionally NOT wired into ``RuntimeResources`` here — that
wiring arrives with the Section-4 service/API layer in a later stage.
"""

# The jsonb <-> domain helpers below read untyped JSONB columns (sources,
# themes, remote_rules, ...) out of ``sa.RowMapping`` and reshape them.
# SQLAlchemy types RowMapping values as ``Any``; suppress the unknown-family
# reports the same way ``postgres_profile_repo.py`` does.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from careerops.api.errors import ConflictError, NotFoundError
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import (
    CRAWL_RUN_COUNTER_KEYS,
    CrawlExecutorMode,
    CrawlPerRunLimits,
    CrawlPlanVersion,
    CrawlPolicyStatus,
    CrawlRun,
    CrawlRunCounters,
    CrawlSource,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.domain.profiles import (
    CompensationPreference,
    LocationKind,
    LocationPreference,
    RemoteRules,
)
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlPermissionState,
    CrawlSourceAttempt,
    CrawlSourcePermission,
    is_permission_transition_allowed,
)
from careerops.infrastructure.database.schema import (
    crawl_plan_versions,
    crawl_runs,
    crawl_source_attempts,
    crawl_source_permissions,
    job_sources,
)

__all__ = [
    "PostgresCrawlAttemptRepository",
    "PostgresCrawlPermissionRepository",
    "PostgresCrawlPlanRepository",
    "PostgresCrawlRunRepository",
    "PostgresCrawlSourceRepository",
]


# ---------------------------------------------------------------------------
# Shared jsonb <-> domain mapping for the structured preference types reused
# from Section 2/3 (LocationPreference / RemoteRules / CompensationPreference).
# Duplicated from postgres_profile_repo.py (which keeps its own private copies)
# so Section 2 stays untouched; the mapping logic is identical because the
# domain types are shared. If the two ever diverge, the shared-domain invariant
# has already broken and the duplication surfaces it.
# ---------------------------------------------------------------------------


def _locations_to_domain(raw: Any) -> tuple[LocationPreference, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[LocationPreference] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind_value = str(entry.get("kind", "preferred"))
        try:
            kind = LocationKind(kind_value)
        except ValueError:
            kind = LocationKind.PREFERRED
        radius = entry.get("radius_km")
        items.append(
            LocationPreference(
                name=str(entry.get("name", "")),
                kind=kind,
                radius_km=int(radius) if isinstance(radius, int) else None,
            )
        )
    return tuple(items)


def _remote_rules_to_domain(raw: Any) -> RemoteRules:
    if not isinstance(raw, dict):
        return RemoteRules()
    return RemoteRules(
        remote_allowed=bool(raw.get("remote_allowed", False)),
        hybrid_allowed=bool(raw.get("hybrid_allowed", False)),
        onsite_required=bool(raw.get("onsite_required", False)),
        timezone=str(raw.get("timezone", "")),
    )


def _compensation_to_domain(raw: Any) -> CompensationPreference:
    if not isinstance(raw, dict):
        return CompensationPreference()
    amount_min = raw.get("amount_min")
    amount_max = raw.get("amount_max")
    period_value = raw.get("period", "")
    return CompensationPreference(
        currency=str(raw.get("currency", "")),
        amount_min=int(amount_min) if isinstance(amount_min, (int, float)) else None,
        amount_max=int(amount_max) if isinstance(amount_max, (int, float)) else None,
        period=str(period_value) if period_value else "",
        equity=bool(raw.get("equity", False)),
    )


def _locations_to_json(locations: tuple[LocationPreference, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": loc.name,
            "kind": loc.kind.value,
            "radius_km": loc.radius_km,
        }
        for loc in locations
    ]


def _remote_rules_to_json(rules: RemoteRules) -> dict[str, object]:
    return {
        "remote_allowed": rules.remote_allowed,
        "hybrid_allowed": rules.hybrid_allowed,
        "onsite_required": rules.onsite_required,
        "timezone": rules.timezone,
    }


def _compensation_to_json(comp: CompensationPreference) -> dict[str, object]:
    return {
        "currency": comp.currency,
        "amount_min": comp.amount_min,
        "amount_max": comp.amount_max,
        "period": getattr(comp.period, "value", str(comp.period)),
        "equity": comp.equity,
    }


def _per_run_limits_to_domain(raw: Any) -> CrawlPerRunLimits:
    if not isinstance(raw, dict):
        return CrawlPerRunLimits()

    def _opt_int(key: str) -> int | None:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        return None

    return CrawlPerRunLimits(
        max_postings_per_source=_opt_int("max_postings_per_source"),
        max_sources=_opt_int("max_sources"),
        timeout_seconds=_opt_int("timeout_seconds"),
    )


def _per_run_limits_to_json(limits: CrawlPerRunLimits) -> dict[str, object]:
    value: dict[str, object] = {}
    if limits.max_postings_per_source is not None:
        value["max_postings_per_source"] = limits.max_postings_per_source
    if limits.max_sources is not None:
        value["max_sources"] = limits.max_sources
    if limits.timeout_seconds is not None:
        value["timeout_seconds"] = limits.timeout_seconds
    return value


def _counters_to_domain(raw: Any) -> CrawlRunCounters:
    if not isinstance(raw, dict):
        return CrawlRunCounters()

    def _int(key: str) -> int:
        value = raw.get(key, 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return CrawlRunCounters(
        discovered=_int("discovered"),
        updated=_int("updated"),
        closed=_int("closed"),
        failed=_int("failed"),
    )


def _counters_to_json(counters: CrawlRunCounters) -> dict[str, object]:
    return {key: int(getattr(counters, key)) for key in CRAWL_RUN_COUNTER_KEYS}


def _uuids_to_domain(raw: Any) -> tuple[UUID, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[UUID] = []
    for entry in raw:
        if isinstance(entry, str):
            try:
                items.append(UUID(entry))
            except ValueError:
                continue
        elif isinstance(entry, UUID):
            items.append(entry)
    return tuple(items)


def _uuids_to_json(ids: tuple[UUID, ...]) -> list[str]:
    return [str(item) for item in ids]


def _strings_to_domain(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _strings_to_json(items: tuple[str, ...]) -> list[str]:
    return list(items)


def _policy_status(value: str) -> CrawlPolicyStatus:
    try:
        return CrawlPolicyStatus(value)
    except ValueError:
        return CrawlPolicyStatus.UNKNOWN


def _source_state(value: str) -> CrawlSourceState:
    try:
        return CrawlSourceState(value)
    except ValueError:
        return CrawlSourceState.PENDING_REVIEW


def _source_type(value: str) -> CrawlSourceType:
    try:
        return CrawlSourceType(value)
    except ValueError:
        # The DB CHECK allows any string up to 32 chars in source_type (legacy
        # M1 column); an unknown value round-trips as the closest known type.
        # The service layer rejects unsupported types at registration time, so
        # this path only fires for legacy rows.
        return CrawlSourceType.OFFICIAL


def _executor_mode(value: str) -> CrawlExecutorMode:
    try:
        return CrawlExecutorMode(value)
    except ValueError:
        return CrawlExecutorMode.HTTP


# ---------------------------------------------------------------------------
# PostgresCrawlSourceRepository
# ---------------------------------------------------------------------------


def _row_to_source(row: sa.RowMapping, owner_id: UUID) -> CrawlSource:
    """Map a ``job_sources`` row onto :class:`CrawlSource`.

    ``owner_id`` is the server-resolved candidate (the table has no owner
    column in the single-user runtime); it is stamped onto the model so the
    service / API layer sees a uniform ownership field.
    """
    last_run_metadata = row["last_run_metadata"]
    return CrawlSource(
        id=row["id"],
        owner_id=owner_id,
        company_id=row["company_id"],
        source_type=_source_type(str(row["source_type"])),
        source_identifier=str(row["source_identifier"]),
        base_url=str(row["base_url"]),
        executor_mode=_executor_mode(str(row.get("executor_mode", "http"))),
        state=_source_state(str(row["state"])),
        trust_status=_policy_status(str(row["trust_status"])),
        terms_status=_policy_status(str(row["terms_status"])),
        robots_status=_policy_status(str(row["robots_status"])),
        adapter_version=str(row["adapter_version"]),
        enabled=bool(row["enabled"]),
        verified_at=row["verified_at"],
        last_discovery_at=row["last_discovery_at"],
        last_run_at=row["last_run_at"],
        last_run_metadata=dict(last_run_metadata) if isinstance(last_run_metadata, dict) else {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresCrawlSourceRepository:
    """Crawl-source CRUD on ``job_sources`` (extended by migration 0015)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_by_id(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        with self._engine.begin() as conn:
            row = (
                conn.execute(sa.select(job_sources).where(job_sources.c.id == source_id))
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError("crawl source not found")
        return _row_to_source(row, owner_id)

    def list_for(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        stmt = (
            sa.select(job_sources)
            .order_by(job_sources.c.created_at.desc(), job_sources.c.id.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_source(row, owner_id) for row in rows]

    def save(self, source: CrawlSource) -> CrawlSource:
        """Insert or update (idempotent on ``id`` via ON CONFLICT DO UPDATE).

        The owner_id field is NOT persisted (the table has no owner column);
        it is re-stamped on the re-read.
        """
        values = {
            "id": source.id,
            "company_id": source.company_id,
            "source_type": source.source_type.value,
            "source_identifier": source.source_identifier,
            "base_url": source.base_url,
            "executor_mode": source.executor_mode.value,
            "state": source.state.value,
            "trust_status": source.trust_status.value,
            "terms_status": source.terms_status.value,
            "robots_status": source.robots_status.value,
            "adapter_version": source.adapter_version,
            "enabled": source.enabled,
            "verified_at": source.verified_at,
            "last_discovery_at": source.last_discovery_at,
            "last_run_at": source.last_run_at,
            "last_run_metadata": dict(source.last_run_metadata),
        }
        with self._engine.begin() as conn:
            upsert = (
                pg_insert(job_sources)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[job_sources.c.id],
                    set_={
                        "company_id": source.company_id,
                        "source_type": source.source_type.value,
                        "source_identifier": source.source_identifier,
                        "base_url": source.base_url,
                        "executor_mode": source.executor_mode.value,
                        "state": source.state.value,
                        "trust_status": source.trust_status.value,
                        "terms_status": source.terms_status.value,
                        "robots_status": source.robots_status.value,
                        "adapter_version": source.adapter_version,
                        "enabled": source.enabled,
                        "verified_at": source.verified_at,
                        "last_discovery_at": source.last_discovery_at,
                        "last_run_at": source.last_run_at,
                        "last_run_metadata": dict(source.last_run_metadata),
                        "updated_at": sa.func.now(),
                    },
                )
            )
            conn.execute(upsert)
        return self.get_by_id(source.owner_id, source.id)

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
        patches: dict[str, Any] = {"updated_at": (now or sa.func.now())}
        if state is not None:
            patches["state"] = state.value
        if enabled is not None:
            patches["enabled"] = enabled
        if trust_status is not None:
            patches["trust_status"] = trust_status.value
        if terms_status is not None:
            patches["terms_status"] = terms_status.value
        if robots_status is not None:
            patches["robots_status"] = robots_status.value
        if adapter_version is not None:
            patches["adapter_version"] = adapter_version
        if last_run_at is not None:
            patches["last_run_at"] = last_run_at
        if last_run_metadata is not None:
            patches["last_run_metadata"] = dict(last_run_metadata)

        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(job_sources).where(job_sources.c.id == source_id).values(**patches)
            )
            if result.rowcount == 0:
                raise NotFoundError("crawl source not found")
        return self.get_by_id(owner_id, source_id)

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(sa.delete(job_sources).where(job_sources.c.id == source_id))
            if result.rowcount == 0:
                raise NotFoundError("crawl source not found")

    # -- discovery dedup (Phase 5.4) ---------------------------------------

    def get_by_identity(
        self,
        owner_id: UUID,
        company_id: UUID,
        source_type: CrawlSourceType,
        source_identifier: str,
    ) -> CrawlSource | None:
        """Return the source matching the business unique key
        ``(company_id, source_type, source_identifier)``, or None.

        Used by the discovery normalizer and :meth:`upsert_by_identity` to find
        the canonical row for a discovered source without knowing its id.
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(job_sources).where(
                        sa.and_(
                            job_sources.c.company_id == company_id,
                            job_sources.c.source_type == source_type.value,
                            job_sources.c.source_identifier == source_identifier,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _row_to_source(row, owner_id) if row else None

    def upsert_by_identity(self, source: CrawlSource) -> CrawlSource:
        """Insert or update keyed by the business unique key
        ``(company_id, source_type, source_identifier)``.

        Closes the gap in :meth:`save` (which upserts on ``id`` only): two
        discovery events for the same source collapse to ONE row. On conflict
        the EXISTING row's id wins (the incoming id is ignored), and mutable
        discovery fields (base_url, executor_mode, adapter_version,
        last_discovery_at) are refreshed. The canonical row is then re-read by
        identity and returned.
        """
        values = {
            "id": source.id,
            "company_id": source.company_id,
            "source_type": source.source_type.value,
            "source_identifier": source.source_identifier,
            "base_url": source.base_url,
            "executor_mode": source.executor_mode.value,
            "state": source.state.value,
            "trust_status": source.trust_status.value,
            "terms_status": source.terms_status.value,
            "robots_status": source.robots_status.value,
            "adapter_version": source.adapter_version,
            "enabled": source.enabled,
            "verified_at": source.verified_at,
            "last_discovery_at": source.last_discovery_at,
            "last_run_at": source.last_run_at,
            "last_run_metadata": dict(source.last_run_metadata),
        }
        with self._engine.begin() as conn:
            upsert = (
                pg_insert(job_sources)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_job_sources_company_type_identifier",
                    set_={
                        # Refresh mutable discovery fields; do NOT clobber
                        # lifecycle state / policy status / enabled / last_run_*
                        # that the user or a prior run may have set.
                        "base_url": source.base_url,
                        "executor_mode": source.executor_mode.value,
                        "adapter_version": source.adapter_version,
                        "last_discovery_at": source.last_discovery_at,
                        "last_run_metadata": dict(source.last_run_metadata),
                        "updated_at": sa.func.now(),
                    },
                )
            )
            conn.execute(upsert)
        existing = self.get_by_identity(
            source.owner_id,
            source.company_id,
            source.source_type,
            source.source_identifier,
        )
        # The conflict target guarantees a row exists after the upsert.
        assert existing is not None  # noqa: S101 - post-upsert invariant
        return existing


# ---------------------------------------------------------------------------
# PostgresCrawlPlanRepository
# ---------------------------------------------------------------------------


def _row_to_plan(row: sa.RowMapping) -> CrawlPlanVersion:
    return CrawlPlanVersion(
        id=row["id"],
        owner_id=row["owner_id"],
        version=row["version"],
        is_active=bool(row["is_active"]),
        sources=_uuids_to_domain(row["sources"]),
        themes=_strings_to_domain(row["themes"]),
        include_keywords=_strings_to_domain(row["include_keywords"]),
        exclude_keywords=_strings_to_domain(row["exclude_keywords"]),
        role_families=_strings_to_domain(row["role_families"]),
        locations=_locations_to_domain(row["locations"]),
        remote_rules=_remote_rules_to_domain(row["remote_rules"]),
        seniority=_strings_to_domain(row["seniority"]),
        compensation=_compensation_to_domain(row["compensation"]),
        content_scope=str(row["content_scope"]),
        interval_seconds=int(row["interval_seconds"]),
        timezone=str(row["timezone"]),
        per_run_limits=_per_run_limits_to_domain(row["per_run_limits"]),
        rules_version=str(row["rules_version"]),
        created_at=row["created_at"],
    )


class PostgresCrawlPlanRepository:
    """PostgreSQL-backed crawl-plan-version store.

    Every method opens its own transaction via ``engine.begin()``. The
    ``activate`` path performs the deactivate-prior + activate-new pair inside
    a single transaction so the partial unique index
    ``ix_crawl_plan_versions_owner_active`` is never transiently violated.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- reads --------------------------------------------------------------

    def get_active_for(self, owner_id: UUID) -> CrawlPlanVersion | None:
        stmt = sa.select(crawl_plan_versions).where(
            sa.and_(
                crawl_plan_versions.c.owner_id == owner_id,
                crawl_plan_versions.c.is_active.is_(True),
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_plan(row) if row else None

    def get_by_id(self, owner_id: UUID, version_id: UUID) -> CrawlPlanVersion:
        stmt = sa.select(crawl_plan_versions).where(
            sa.and_(
                crawl_plan_versions.c.id == version_id,
                crawl_plan_versions.c.owner_id == owner_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFoundError("crawl plan version not found for owner")
        return _row_to_plan(row)

    def list_versions(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlPlanVersion]:
        stmt = (
            sa.select(crawl_plan_versions)
            .where(crawl_plan_versions.c.owner_id == owner_id)
            .order_by(crawl_plan_versions.c.version.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_plan(row) for row in rows]

    # -- writes -------------------------------------------------------------

    def create_version(self, plan: CrawlPlanVersion) -> CrawlPlanVersion:
        """Persist a new immutable plan version.

        Stored inactive unless ``plan.is_active`` is already true, in which
        case :meth:`activate` is used to flip it atomically with the prior
        active version's deactivation.
        """
        values = {
            "id": plan.id,
            "owner_id": plan.owner_id,
            "version": plan.version,
            "is_active": False,  # never store active directly; activate() flips it
            "sources": _uuids_to_json(plan.sources),
            "themes": _strings_to_json(plan.themes),
            "include_keywords": _strings_to_json(plan.include_keywords),
            "exclude_keywords": _strings_to_json(plan.exclude_keywords),
            "role_families": _strings_to_json(plan.role_families),
            "locations": _locations_to_json(plan.locations),
            "remote_rules": _remote_rules_to_json(plan.remote_rules),
            "seniority": _strings_to_json(plan.seniority),
            "compensation": _compensation_to_json(plan.compensation),
            "content_scope": plan.content_scope,
            "interval_seconds": plan.interval_seconds,
            "timezone": plan.timezone,
            "per_run_limits": _per_run_limits_to_json(plan.per_run_limits),
            "rules_version": plan.rules_version,
        }
        with self._engine.begin() as conn:
            conn.execute(pg_insert(crawl_plan_versions).values(**values))
            if plan.is_active:
                self._activate_in_connection(conn, plan.owner_id, plan.id)

        return self.get_by_id(plan.owner_id, plan.id)

    def activate(
        self, owner_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> CrawlPlanVersion:
        del now  # the DB sets updated_at via server_default on UPDATE; the
        # partial unique index guarantees one active per owner regardless of
        # wall clock, so no explicit timestamp is needed here.
        with self._engine.begin() as conn:
            target = (
                conn.execute(
                    sa.select(crawl_plan_versions).where(
                        sa.and_(
                            crawl_plan_versions.c.id == version_id,
                            crawl_plan_versions.c.owner_id == owner_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                raise NotFoundError("crawl plan version not found for owner")
            self._activate_in_connection(conn, owner_id, version_id)

        return self.get_by_id(owner_id, version_id)

    def deactivate_all(self, owner_id: UUID, *, now: datetime | None = None) -> None:
        del now
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(crawl_plan_versions)
                .where(
                    sa.and_(
                        crawl_plan_versions.c.owner_id == owner_id,
                        crawl_plan_versions.c.is_active.is_(True),
                    )
                )
                .values(is_active=False)
            )

    @staticmethod
    def _activate_in_connection(conn: sa.Connection, owner_id: UUID, version_id: UUID) -> None:
        """Deactivate the current active version, then flip the target active.

        Ordering matters: the partial unique index
        ``ix_crawl_plan_versions_owner_active`` allows at most one active row
        per owner, so the prior active row MUST be cleared before the new one
        is set within the same statement sequence. Mirrors
        :meth:`PostgresProfileRepository._activate_in_connection`.
        """
        conn.execute(
            sa.update(crawl_plan_versions)
            .where(
                sa.and_(
                    crawl_plan_versions.c.owner_id == owner_id,
                    crawl_plan_versions.c.is_active.is_(True),
                    crawl_plan_versions.c.id != version_id,
                )
            )
            .values(is_active=False)
        )
        conn.execute(
            sa.update(crawl_plan_versions)
            .where(crawl_plan_versions.c.id == version_id)
            .values(is_active=True)
        )


# ---------------------------------------------------------------------------
# PostgresCrawlRunRepository
#
# Ownership is transitive via the plan version. Every read joins
# ``crawl_runs`` -> ``crawl_plan_versions`` on ``plan_version_id`` and filters
# by ``owner_id`` so a run whose plan version belongs to a different owner is
# never returned (Iron Rule 2).
# ---------------------------------------------------------------------------


def _row_to_run(row: sa.RowMapping) -> CrawlRun:
    return CrawlRun(
        id=row["id"],
        plan_version_id=row["plan_version_id"],
        run_identity=str(row["run_identity"]),
        source_set=_uuids_to_domain(row["source_set"]),
        state=CrawlRunState(str(row["state"])),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        limits=_per_run_limits_to_domain(row["limits"]),
        counters=_counters_to_domain(row["counters"]),
        error_category=str(row["error_category"]),
        next_eligible_at=row["next_eligible_at"],
        created_at=row["created_at"],
    )


def _owner_scoped_run_select(owner_id: UUID) -> sa.Select[Any]:
    """``SELECT crawl_runs.* JOIN crawl_plan_versions`` filtered by owner.

    The return annotation is intentionally broad (the row maps the full
    ``crawl_runs`` column set via ``.mappings()`` at the call site, which
    re-types each value); the explicit type argument keeps pyright from
    flagging the generic ``Select`` as partially unknown.
    """
    return (
        sa.select(crawl_runs)
        .join(
            crawl_plan_versions,
            crawl_runs.c.plan_version_id == crawl_plan_versions.c.id,
        )
        .where(crawl_plan_versions.c.owner_id == owner_id)
    )


class PostgresCrawlRunRepository:
    """PostgreSQL-backed crawl-run store, scoped transitively by owner."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, owner_id: UUID, run: CrawlRun) -> CrawlRun:
        """Insert a new run after validating transitive ownership.

        Raises :class:`NotFoundError` if ``run.plan_version_id`` does not
        belong to ``owner_id``, and :class:`ConflictError` if
        ``run.run_identity`` already exists (Iron Rule 4 idempotency).
        """
        values = {
            "id": run.id,
            "plan_version_id": run.plan_version_id,
            "run_identity": run.run_identity,
            "source_set": _uuids_to_json(run.source_set),
            "state": run.state.value,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "limits": _per_run_limits_to_json(run.limits),
            "counters": _counters_to_json(run.counters),
            "error_category": run.error_category,
            "next_eligible_at": run.next_eligible_at,
        }
        with self._engine.begin() as conn:
            # Iron Rule 2: verify the plan version belongs to the owner before
            # inserting. A run bound to another owner's plan version is an
            # ownership-substitution attempt.
            owned = conn.execute(
                sa.select(crawl_plan_versions.c.id).where(
                    sa.and_(
                        crawl_plan_versions.c.id == run.plan_version_id,
                        crawl_plan_versions.c.owner_id == owner_id,
                    )
                )
            ).first()
            if owned is None:
                raise NotFoundError("crawl plan version not found for owner")
            try:
                conn.execute(pg_insert(crawl_runs).values(**values))
            except IntegrityError as err:
                # run_identity uniqueness is the idempotency contract; translate
                # to ConflictError so the caller can recover via get_by_identity.
                raise ConflictError("crawl run identity already exists") from err
        return self.get_by_id(owner_id, run.id)

    def get_by_id(self, owner_id: UUID, run_id: UUID) -> CrawlRun:
        stmt = _owner_scoped_run_select(owner_id).where(crawl_runs.c.id == run_id)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFoundError("crawl run not found for owner")
        return _row_to_run(row)

    def get_by_identity(self, owner_id: UUID, run_identity: str) -> CrawlRun | None:
        stmt = _owner_scoped_run_select(owner_id).where(crawl_runs.c.run_identity == run_identity)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_run(row) if row else None

    def list_for_owner(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlRun]:
        stmt = (
            _owner_scoped_run_select(owner_id)
            .order_by(crawl_runs.c.created_at.desc(), crawl_runs.c.id.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_run(row) for row in rows]

    def list_for_plan(
        self, owner_id: UUID, plan_version_id: UUID, *, limit: int = 50
    ) -> list[CrawlRun]:
        stmt = (
            _owner_scoped_run_select(owner_id)
            .where(crawl_runs.c.plan_version_id == plan_version_id)
            .order_by(crawl_runs.c.created_at.desc(), crawl_runs.c.id.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_run(row) for row in rows]

    def count_for_plan(self, owner_id: UUID, plan_version_id: UUID) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(crawl_runs)
            .join(
                crawl_plan_versions,
                crawl_runs.c.plan_version_id == crawl_plan_versions.c.id,
            )
            .where(
                sa.and_(
                    crawl_plan_versions.c.owner_id == owner_id,
                    crawl_runs.c.plan_version_id == plan_version_id,
                )
            )
        )
        with self._engine.begin() as conn:
            return int(conn.execute(stmt).scalar_one())

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
        del now  # no updated_at column on crawl_runs; created_at is immutable.
        patches: dict[str, Any] = {"state": state.value}
        if counters is not None:
            patches["counters"] = _counters_to_json(counters)
        if error_category is not None:
            patches["error_category"] = error_category
        if next_eligible_at is not None:
            patches["next_eligible_at"] = next_eligible_at
        if started_at is not None:
            patches["started_at"] = started_at
        if ended_at is not None:
            patches["ended_at"] = ended_at

        with self._engine.begin() as conn:
            # Update only if the run is owner-scoped (join enforces Iron Rule 2).
            result = conn.execute(
                sa.update(crawl_runs)
                .where(
                    sa.and_(
                        crawl_runs.c.id == run_id,
                        crawl_runs.c.plan_version_id.in_(
                            sa.select(crawl_plan_versions.c.id).where(
                                crawl_plan_versions.c.owner_id == owner_id
                            )
                        ),
                    )
                )
                .values(**patches)
            )
            if result.rowcount == 0:
                raise NotFoundError("crawl run not found for owner")
        return self.get_by_id(owner_id, run_id)


# ---------------------------------------------------------------------------
# PostgresCrawlAttemptRepository (Phase 5.1/5.3)
# ---------------------------------------------------------------------------


def _attempt_outcome(value: str) -> CrawlAttemptOutcome:
    try:
        return CrawlAttemptOutcome(value)
    except ValueError:
        # Unknown legacy value round-trips as the closest safe non-terminal
        # outcome; the service layer never writes an invalid value (CHECK
        # enforced). This path only fires for corrupted rows.
        return CrawlAttemptOutcome.TRANSIENT_FAILURE


def _row_to_attempt(row: sa.RowMapping, owner_id: UUID) -> CrawlSourceAttempt:
    """Map a ``crawl_source_attempts`` row onto :class:`CrawlSourceAttempt`.

    ``owner_id`` is stamped on read (transitive via job_sources.company_id in
    the single-user runtime; the table has no owner column).
    """
    evidence = row["evidence_summary"]
    return CrawlSourceAttempt(
        id=row["id"],
        source_id=row["source_id"],
        owner_id=owner_id,
        attempt_no=int(row["attempt_no"]),
        outcome=_attempt_outcome(str(row["outcome"])),
        crawl_run_id=row["crawl_run_id"],
        executor_mode=_executor_mode(str(row.get("executor_mode", "http"))),
        action_count=int(row.get("action_count", 0)),
        evidence_summary=dict(evidence) if isinstance(evidence, dict) else {},
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        next_eligible_at=row["next_eligible_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresCrawlAttemptRepository:
    """Append-only per-source attempt-outcome store on ``crawl_source_attempts``.

    Every method takes the server-resolved ``owner_id`` (stamped on read); the
    table has no owner column in the single-user runtime. ``attempt_no`` is the
    monotonic per-source sequence — callers obtain it via
    :meth:`next_attempt_no` so the ``uq_crawl_source_attempts_source_attempt_no``
    unique key is not violated under retry.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def next_attempt_no(self, owner_id: UUID, source_id: UUID) -> int:
        stmt = sa.select(sa.func.coalesce(sa.func.max(crawl_source_attempts.c.attempt_no), 0)).where(
            crawl_source_attempts.c.source_id == source_id
        )
        with self._engine.begin() as conn:
            current = conn.execute(stmt).scalar() or 0
        return int(current) + 1

    def record(self, owner_id: UUID, attempt: CrawlSourceAttempt) -> CrawlSourceAttempt:
        values = {
            "id": attempt.id,
            "source_id": attempt.source_id,
            "crawl_run_id": attempt.crawl_run_id,
            "attempt_no": attempt.attempt_no,
            "outcome": attempt.outcome.value,
            "executor_mode": attempt.executor_mode.value,
            "action_count": attempt.action_count,
            "evidence_summary": dict(attempt.evidence_summary),
            "started_at": attempt.started_at,
            "finished_at": attempt.finished_at,
            "next_eligible_at": attempt.next_eligible_at,
        }
        with self._engine.begin() as conn:
            conn.execute(pg_insert(crawl_source_attempts).values(**values))
        return self.get_by_id(owner_id, attempt.id)

    def get_by_id(self, owner_id: UUID, attempt_id: UUID) -> CrawlSourceAttempt:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(crawl_source_attempts).where(
                        crawl_source_attempts.c.id == attempt_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError("crawl source attempt not found")
        return _row_to_attempt(row, owner_id)

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourceAttempt]:
        stmt = (
            sa.select(crawl_source_attempts)
            .where(crawl_source_attempts.c.source_id == source_id)
            .order_by(crawl_source_attempts.c.attempt_no.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_attempt(row, owner_id) for row in rows]

    def latest_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourceAttempt | None:
        stmt = (
            sa.select(crawl_source_attempts)
            .where(crawl_source_attempts.c.source_id == source_id)
            .order_by(crawl_source_attempts.c.attempt_no.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_attempt(row, owner_id) if row else None


# ---------------------------------------------------------------------------
# PostgresCrawlPermissionRepository (Phase 5.2/5.3)
# ---------------------------------------------------------------------------


def _permission_state(value: str) -> CrawlPermissionState:
    try:
        return CrawlPermissionState(value)
    except ValueError:
        return CrawlPermissionState.PENDING


def _row_to_permission(row: sa.RowMapping, owner_id: UUID) -> CrawlSourcePermission:
    terms = row["disclosed_terms"]
    return CrawlSourcePermission(
        id=row["id"],
        source_id=row["source_id"],
        owner_id=owner_id,
        state=_permission_state(str(row["state"])),
        domain_scope=str(row.get("domain_scope", "")),
        disclosed_terms=dict(terms) if isinstance(terms, dict) else {},
        requested_at=row["requested_at"],
        granted_at=row["granted_at"],
        denied_at=row["denied_at"],
        revoked_at=row["revoked_at"],
        expired_at=row["expired_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# The decision timestamp to stamp for each target state on transition. PENDING
# has no entry here because transitions TO pending are not legal (terminal
# states admit no further moves); re-request is a new row.
_DECISION_TIMESTAMP_COLUMN: dict[CrawlPermissionState, str] = {
    CrawlPermissionState.GRANTED: "granted_at",
    CrawlPermissionState.DENIED: "denied_at",
    CrawlPermissionState.REVOKED: "revoked_at",
    CrawlPermissionState.EXPIRED: "expired_at",
}


class PostgresCrawlPermissionRepository:
    """Per-source crawl-permission store on ``crawl_source_permissions``.

    Stores the consent lifecycle ONLY — no password / raw session material
    (tasks 5.2, 6.6, 10.5). The ``transition`` method enforces the state machine
    in :data:`careerops.domain.crawl_attempts.ALLOWED_PERMISSION_TRANSITIONS` and
    stamps the matching decision timestamp.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(
        self, owner_id: UUID, permission: CrawlSourcePermission
    ) -> CrawlSourcePermission:
        values = {
            "id": permission.id,
            "source_id": permission.source_id,
            "domain_scope": permission.domain_scope,
            "state": permission.state.value,
            "disclosed_terms": dict(permission.disclosed_terms),
            "requested_at": permission.requested_at,
            "granted_at": permission.granted_at,
            "denied_at": permission.denied_at,
            "revoked_at": permission.revoked_at,
            "expired_at": permission.expired_at,
            "expires_at": permission.expires_at,
        }
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(crawl_source_permissions)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[crawl_source_permissions.c.id],
                    set_={
                        "domain_scope": permission.domain_scope,
                        "state": permission.state.value,
                        "disclosed_terms": dict(permission.disclosed_terms),
                        "requested_at": permission.requested_at,
                        "granted_at": permission.granted_at,
                        "denied_at": permission.denied_at,
                        "revoked_at": permission.revoked_at,
                        "expired_at": permission.expired_at,
                        "expires_at": permission.expires_at,
                        "updated_at": sa.func.now(),
                    },
                )
            )
        return self.get_by_id(owner_id, permission.id)

    def get_by_id(self, owner_id: UUID, permission_id: UUID) -> CrawlSourcePermission:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(crawl_source_permissions).where(
                        crawl_source_permissions.c.id == permission_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFoundError("crawl source permission not found")
        return _row_to_permission(row, owner_id)

    def get_unresolved_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourcePermission | None:
        stmt = (
            sa.select(crawl_source_permissions)
            .where(
                sa.and_(
                    crawl_source_permissions.c.source_id == source_id,
                    crawl_source_permissions.c.state == CrawlPermissionState.PENDING.value,
                )
            )
            .order_by(crawl_source_permissions.c.created_at.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_permission(row, owner_id) if row else None

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourcePermission]:
        stmt = (
            sa.select(crawl_source_permissions)
            .where(crawl_source_permissions.c.source_id == source_id)
            .order_by(crawl_source_permissions.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_permission(row, owner_id) for row in rows]

    def transition(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        to: CrawlPermissionState,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        current = self.get_by_id(owner_id, permission_id)
        if not is_permission_transition_allowed(current.state, to):
            raise ValueError(
                f"illegal crawl permission transition: {current.state.value} -> {to.value}"
            )
        stamp_column = _DECISION_TIMESTAMP_COLUMN.get(to)
        patches: dict[str, Any] = {
            "state": to.value,
            "updated_at": now or sa.func.now(),
        }
        if stamp_column is not None:
            patches[stamp_column] = now or sa.func.now()
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(crawl_source_permissions)
                .where(crawl_source_permissions.c.id == permission_id)
                .values(**patches)
            )
            if result.rowcount == 0:
                raise NotFoundError("crawl source permission not found")
        return self.get_by_id(owner_id, permission_id)


# Re-export the domain exceptions so callers can import everything from one place.
__all__ += ["ConflictError", "NotFoundError"]
