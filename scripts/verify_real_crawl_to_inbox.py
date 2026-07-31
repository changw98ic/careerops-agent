"""Run and verify the real broad public crawl-to-inbox production path.

This is an operational acceptance command, not a fixture-driven smoke test.
It registers at least 100 independently verified public ATS boards, creates
paused Temporal schedules for every source, executes one durable production
workflow per source with bounded concurrency, verifies database receipts, and
then activates the durable hourly schedules. If this command disconnects,
Temporal keeps the per-source workflows alive; rerunning the command attaches
to their deterministic workflow IDs instead of restarting the whole batch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from temporalio.client import Client

from careerops.application.crawl_activation import TemporalScheduleActivator
from careerops.application.crawl_plan_service import (
    CrawlPlanPreferences,
    CrawlPlanService,
    CrawlSourceService,
)
from careerops.application.crawl_readiness import check_crawl_readiness
from careerops.application.profile_service import ProfilePreferences, ProfileService
from careerops.application.public_source_catalog import (
    PUBLIC_JOB_SOURCES,
    PublicJobSource,
    validate_public_source_catalog,
)
from careerops.application.real_crawl_batch import run_source_workflows
from careerops.config import get_settings
from careerops.domain.crawl_plans import (
    CrawlPerRunLimits,
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.domain.profiles import RemoteRules
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.postgres_crawl_repo import (
    PostgresCrawlPermissionRepository,
    PostgresCrawlPlanRepository,
    PostgresCrawlRunRepository,
    PostgresCrawlSourceRepository,
)
from careerops.infrastructure.database.postgres_job_repo import PostgresJobReadRepository
from careerops.infrastructure.database.postgres_matching_repo import (
    PostgresMatchingReadRepository,
)
from careerops.infrastructure.database.postgres_profile_repo import (
    PostgresProfileRepository,
)
from careerops.infrastructure.database.postgres_tier2_budget import (
    PostgresTier2Budget,
)
from careerops.infrastructure.database.schema import (
    companies,
    crawl_source_attempts,
    filter_decisions,
    job_posting_assignments,
    job_posting_versions,
    match_results,
)
from careerops.infrastructure.temporal.schedule_manager import ScheduleManager
from careerops.workflows.s5_contracts import CrawlRunWorkflowResult

_CANDIDATE_ID = uuid5(NAMESPACE_URL, "careerops:real-broad-crawl-candidate")
_SOURCE_INTERVAL = timedelta(hours=1)
_MIN_REAL_SOURCES = 100
_MIN_REAL_POSTINGS = 1_000
_MAX_POSTINGS_PER_SOURCE = 5_000


def _normalized_company_name(source: PublicJobSource) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", source.company_name.lower()).strip("-")
    return value or source.source_identifier


def _ensure_company(
    job_repo: PostgresJobReadRepository,
    engine: sa.Engine,
    source: PublicJobSource,
) -> UUID:
    normalized = _normalized_company_name(source)
    company_id = uuid5(NAMESPACE_URL, f"careerops:company:{normalized}")
    hostname = urlsplit(source.base_url).hostname or ""
    job_repo.add_company(
        company_id,
        source.company_name,
        normalized,
        official_domains=[hostname],
        terms_status=CrawlPolicyStatus.ALLOWED.value,
    )
    with engine.begin() as conn:
        return conn.execute(
            sa.select(companies.c.id).where(
                companies.c.normalized_name == normalized
            )
        ).scalar_one()


def _ensure_source(
    source_service: CrawlSourceService,
    source_repo: PostgresCrawlSourceRepository,
    candidate_id: UUID,
    company_id: UUID,
    catalog_source: PublicJobSource,
    *,
    now: datetime,
) -> CrawlSource:
    source_type = CrawlSourceType(catalog_source.source_type)
    existing = source_repo.get_by_identity(
        candidate_id,
        company_id,
        source_type,
        catalog_source.source_identifier,
    )
    metadata = {
        "interval_seconds": int(_SOURCE_INTERVAL.total_seconds()),
        "catalog_validated_at": "2026-07-31",
        "access": "public-read-only",
    }
    if existing is not None:
        return source_repo.save(
            dataclasses.replace(
                existing,
                base_url=catalog_source.base_url,
                state=CrawlSourceState.ACTIVE,
                enabled=True,
                trust_status=CrawlPolicyStatus.ALLOWED,
                terms_status=CrawlPolicyStatus.ALLOWED,
                robots_status=CrawlPolicyStatus.ALLOWED,
                last_run_metadata=metadata,
                updated_at=now,
            )
        )

    registered = source_service.register(
        candidate_id,
        company_id=company_id,
        source_type=catalog_source.source_type,
        source_identifier=catalog_source.source_identifier,
        base_url=catalog_source.base_url,
        enabled=True,
        trust_status=CrawlPolicyStatus.ALLOWED,
        terms_status=CrawlPolicyStatus.ALLOWED,
        robots_status=CrawlPolicyStatus.ALLOWED,
        now=now,
    )
    return source_repo.update_state(
        candidate_id,
        registered.id,
        last_run_metadata=metadata,
        now=now,
    )


async def _reconcile_schedules(
    client: Client,
    sources: list[CrawlSource],
    candidate_id: UUID,
    *,
    task_queue: str,
    paused: bool,
) -> int:
    manager = ScheduleManager(client)
    activator = TemporalScheduleActivator(manager, task_queue=task_queue)
    for source in sources:
        await activator.activate_source_schedule(
            source.id,
            _SOURCE_INTERVAL,
            paused=paused,
            owner_id=candidate_id,
        )

    descriptors = [
        await manager.describe(f"crawl:{source.id}") for source in sources
    ]
    if any(descriptor is None for descriptor in descriptors):
        raise RuntimeError("one or more crawl schedules were not persisted")
    desired_paused = paused
    if any(descriptor.paused is not desired_paused for descriptor in descriptors if descriptor):
        raise RuntimeError("one or more crawl schedules have the wrong paused state")
    return len(descriptors)


def _database_receipt(
    engine: sa.Engine,
    *,
    candidate_id: UUID,
    run_ids: tuple[UUID, ...],
    profile_version_id: UUID,
) -> dict[str, object]:
    scoped_jobs = (
        sa.select(
            job_posting_versions.c.job_posting_id,
            job_posting_assignments.c.canonical_job_id,
        )
        .join(
            job_posting_assignments,
            job_posting_assignments.c.job_posting_id
            == job_posting_versions.c.job_posting_id,
        )
        .where(job_posting_versions.c.crawl_run_id.in_(run_ids))
        .cte("scoped_jobs")
    )

    with engine.begin() as conn:
        counts = conn.execute(
            sa.select(
                sa.func.count(sa.distinct(scoped_jobs.c.job_posting_id)).label(
                    "postings"
                ),
                sa.func.count(sa.distinct(scoped_jobs.c.canonical_job_id)).label(
                    "canonical_jobs"
                ),
                sa.func.count(sa.distinct(match_results.c.canonical_job_id))
                .filter(match_results.c.candidate_id == candidate_id)
                .label("matched_jobs"),
                sa.func.count(sa.distinct(filter_decisions.c.canonical_job_id))
                .filter(
                    filter_decisions.c.candidate_id == candidate_id,
                    filter_decisions.c.profile_version_id == profile_version_id,
                )
                .label("inbox_jobs"),
            )
            .select_from(scoped_jobs)
            .outerjoin(
                match_results,
                match_results.c.canonical_job_id
                == scoped_jobs.c.canonical_job_id,
            )
            .outerjoin(
                filter_decisions,
                filter_decisions.c.canonical_job_id
                == scoped_jobs.c.canonical_job_id,
            )
        ).mappings().one()

        attempts = conn.execute(
            sa.select(
                crawl_source_attempts.c.id,
                crawl_source_attempts.c.source_id,
                crawl_source_attempts.c.outcome,
                crawl_source_attempts.c.executor_mode,
                crawl_source_attempts.c.action_count,
            )
            .where(crawl_source_attempts.c.crawl_run_id.in_(run_ids))
            .order_by(crawl_source_attempts.c.source_id)
        ).mappings().all()

    return {
        "postings": int(counts["postings"]),
        "canonical_jobs": int(counts["canonical_jobs"]),
        "matched_jobs": int(counts["matched_jobs"]),
        "inbox_jobs": int(counts["inbox_jobs"]),
        "attempts": [
            {
                "id": str(attempt["id"]),
                "source_id": str(attempt["source_id"]),
                "outcome": str(attempt["outcome"]),
                "executor": str(attempt["executor_mode"]),
                "action_count": int(attempt["action_count"]),
            }
            for attempt in attempts
        ],
        "outcomes": dict(
            Counter(str(attempt["outcome"]) for attempt in attempts)
        ),
    }


async def main() -> None:
    validate_public_source_catalog()
    settings = get_settings()
    engine = create_database_engine(settings)
    now = datetime.now(tz=UTC)

    matching_repo = PostgresMatchingReadRepository(engine)
    matching_repo.add_candidate(_CANDIDATE_ID, "REAL-BROAD-CRAWL-CANDIDATE")

    profile_repo = PostgresProfileRepository(engine)
    profile = profile_repo.get_active_for(_CANDIDATE_ID)
    if profile is None:
        profile = ProfileService(profile_repo).create_version(
            _CANDIDATE_ID,
            ProfilePreferences(remote_rules=RemoteRules(remote_allowed=True)),
            activate=True,
            now=now,
        )

    job_repo = PostgresJobReadRepository(engine)
    source_repo = PostgresCrawlSourceRepository(engine)
    source_service = CrawlSourceService(source_repo)
    sources = [
        _ensure_source(
            source_service,
            source_repo,
            _CANDIDATE_ID,
            _ensure_company(job_repo, engine, catalog_source),
            catalog_source,
            now=now,
        )
        for catalog_source in PUBLIC_JOB_SOURCES
    ]
    if len(sources) < _MIN_REAL_SOURCES:
        raise RuntimeError(
            f"only {len(sources)} real sources registered; "
            f"need at least {_MIN_REAL_SOURCES}"
        )

    plan_repo = PostgresCrawlPlanRepository(engine)
    plan = CrawlPlanService(plan_repo).create_version(
        _CANDIDATE_ID,
        CrawlPlanPreferences(
            sources=tuple(source.id for source in sources),
            schedule_interval_seconds=int(_SOURCE_INTERVAL.total_seconds()),
            schedule_timezone="UTC",
            per_run_limits=CrawlPerRunLimits(
                max_postings_per_source=_MAX_POSTINGS_PER_SOURCE,
                max_sources=200,
                timeout_seconds=3_600,
            ),
        ),
        activate=True,
        now=now,
    )

    permission_repo = PostgresCrawlPermissionRepository(engine)
    budget = PostgresTier2Budget(engine)
    readiness = check_crawl_readiness(
        engine,
        budget_max_concurrent_slots=budget.max_concurrent_slots,
        budget_daily_action_budget=budget.daily_action_budget,
        permission_repository=permission_repo,
    )
    if not readiness.ready:
        raise RuntimeError(
            "crawl readiness gate failed: " + "; ".join(readiness.failures)
        )

    temporal_client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    paused_schedule_count = await _reconcile_schedules(
        temporal_client,
        sources,
        _CANDIDATE_ID,
        task_queue=settings.temporal_task_queue,
        paused=True,
    )

    run_repo = PostgresCrawlRunRepository(engine)
    completed_by_source: dict[UUID, CrawlRunWorkflowResult] = {}
    for prior_run in run_repo.list_for_owner(_CANDIDATE_ID, limit=500):
        if prior_run.state.value != "succeeded" or len(prior_run.source_set) != 1:
            continue
        completed_by_source.setdefault(
            prior_run.source_set[0],
            CrawlRunWorkflowResult(
                run_id=str(prior_run.id),
                state=prior_run.state.value,
                counters={
                    "discovered": prior_run.counters.discovered,
                    "updated": prior_run.counters.updated,
                    "closed": prior_run.counters.closed,
                    "failed": prior_run.counters.failed,
                },
                error_category=prior_run.error_category,
            ),
        )

    remaining_sources = [
        source for source in sources if source.id not in completed_by_source
    ]
    new_results = await run_source_workflows(
        temporal_client,
        remaining_sources,
        _CANDIDATE_ID,
        task_queue=settings.temporal_task_queue,
    )
    new_by_source = {
        source.id: result
        for source, result in zip(remaining_sources, new_results, strict=True)
    }
    results = [
        completed_by_source.get(source.id) or new_by_source[source.id]
        for source in sources
    ]
    run_ids = tuple(UUID(result.run_id) for result in results)

    database = _database_receipt(
        engine,
        candidate_id=_CANDIDATE_ID,
        run_ids=run_ids,
        profile_version_id=profile.id,
    )
    attempts = cast("list[dict[str, object]]", database["attempts"])
    if len(attempts) != len(sources):
        raise RuntimeError(
            f"expected {len(sources)} source attempts, got {len(attempts)}"
        )
    postings_count = cast("int", database["postings"])
    if postings_count < _MIN_REAL_POSTINGS:
        raise RuntimeError(
            f"only {database['postings']} postings reached the database; "
            f"need at least {_MIN_REAL_POSTINGS}"
        )
    if database["matched_jobs"] != database["canonical_jobs"]:
        raise RuntimeError("not every canonical job has a persisted match")
    if database["inbox_jobs"] != database["canonical_jobs"]:
        raise RuntimeError("not every canonical job has an inbox decision")

    active_schedule_count = await _reconcile_schedules(
        temporal_client,
        sources,
        _CANDIDATE_ID,
        task_queue=settings.temporal_task_queue,
        paused=False,
    )

    receipt = {
        "candidate_id": str(_CANDIDATE_ID),
        "plan_version_id": str(plan.id),
        "crawl_run_ids": [str(run_id) for run_id in run_ids],
        "run_state": "succeeded",
        "source_count": len(sources),
        "schedule_count": active_schedule_count,
        "schedules_initially_paused": paused_schedule_count,
        "schedule_interval_seconds": int(_SOURCE_INTERVAL.total_seconds()),
        "max_postings_per_source": _MAX_POSTINGS_PER_SOURCE,
        "counters": {
            "discovered": sum(result.counters.get("discovered", 0) for result in results),
            "updated": sum(result.counters.get("updated", 0) for result in results),
            "failed": sum(result.counters.get("failed", 0) for result in results),
        },
        "database": database,
    }
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
