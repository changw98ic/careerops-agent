"""Canonical seed sources for the persisted ``job_sources`` registry.

Replaces the deleted ``application/crawl_sources.py`` static in-memory registry
(real-autonomous-career-loop Phase 4.3). The seven default sources are captured
here in the *persisted registry's* shape and written through the canonical
:class:`CrawlSourceRepository` (``PostgresCrawlSourceRepository`` in production),
so there is exactly one source of truth — the ``job_sources`` table — and no
duplicate in-memory ``CrawlSource`` model.

Design notes:
- Each seed is a major Chinese-tech career page that needs browser rendering, so
  it maps to ``source_type=OFFICIAL`` (the meta-type whose concrete list adapter
  is resolved at fetch time) and ``executor_mode=EGO`` (the Tier 2 path).
- ``api_substr`` (a job-list API hint) is preserved in ``last_run_metadata``
  rather than as a dedicated column, since the persisted registry has no such
  column; the agent auto-discovers the API when the hint is absent.
- Seeded sources start ``PENDING_REVIEW`` / ``enabled=False``: they are not
  crawled until a user reviews them and the Phase 8 readiness gate lifts the
  paused crawl schedules.

The actual DB seeding is invoked by the discovery/intake path (Phase 5); this
module only owns the seed data and the mapping so it can be unit-tested without
a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
    CrawlSourceType,
)

# Deterministic namespace for deriving stable seed source ids from their URL.
# Stable ids make re-seeding idempotent (the repo upserts on id).
_SEED_NAMESPACE = UUID("a3f5c1b2-1d2e-4f3a-9b8c-0d1e2f3a4b5c")


@dataclass(frozen=True, slots=True)
class SeedSource:
    """One seed career page, in persisted-registry shape.

    ``api_substr`` is an optional hint for the job-list API URL; it is stored in
    ``last_run_metadata`` (no dedicated column exists on ``job_sources``).
    """

    name: str
    base_url: str
    api_substr: str = ""


# The seven default sources previously hardcoded in the deleted static registry.
# Preserved verbatim; ``api_substr`` carried forward as a Tier 2 hint.
DEFAULT_SEED_SOURCES: tuple[SeedSource, ...] = (
    SeedSource(
        name="aliyun",
        base_url="https://careers.aliyun.com/off-campus/position-list?lang=zh",
        api_substr="position/search",
    ),
    SeedSource(name="tencent", base_url="https://careers.tencent.com/search.html"),
    SeedSource(name="bytedance", base_url="https://jobs.bytedance.com/exposed_positions"),
    SeedSource(name="baidu", base_url="https://talent.baidu.com/jobs/social-list"),
    SeedSource(name="meituan", base_url="https://zhaopin.meituan.com/web/position/list"),
    SeedSource(name="jd", base_url="https://zhaopin.jd.com/web/job/job_list"),
    SeedSource(name="netease", base_url="https://hr.163.com/job/list"),
)


def seed_to_crawl_source(
    seed: SeedSource,
    *,
    owner_id: UUID,
    company_id: UUID,
) -> CrawlSource:
    """Map a :class:`SeedSource` to a persisted-registry :class:`CrawlSource`.

    ``id`` is derived deterministically from the URL (via :func:`uuid5`) so
    re-seeding is idempotent. Browser-rendered career pages map to
    ``OFFICIAL`` / ``EGO`` and start ``PENDING_REVIEW`` / disabled, consistent
    with the crawl readiness gate (Phase 8) that keeps new sources paused until
    reviewed.
    """
    source_id = uuid5(_SEED_NAMESPACE, seed.base_url)
    last_run_metadata: dict[str, object] = {}
    if seed.api_substr:
        # No dedicated column on job_sources; carry the hint in metadata.
        last_run_metadata["api_substr"] = seed.api_substr
    return CrawlSource(
        id=source_id,
        owner_id=owner_id,
        company_id=company_id,
        source_type=CrawlSourceType.OFFICIAL,
        source_identifier=seed.name,
        base_url=seed.base_url,
        executor_mode=CrawlExecutorMode.EGO,
        state=CrawlSourceState.PENDING_REVIEW,
        trust_status=CrawlPolicyStatus.UNKNOWN,
        terms_status=CrawlPolicyStatus.UNKNOWN,
        robots_status=CrawlPolicyStatus.UNKNOWN,
        adapter_version="",
        enabled=False,
        last_run_metadata=last_run_metadata,
    )


def seed_default_sources(
    repository: CrawlSourceRepository,
    *,
    owner_id: UUID,
    company_id_by_name: dict[str, UUID],
) -> list[CrawlSource]:
    """Idempotently seed :data:`DEFAULT_SEED_SOURCES` into the registry.

    ``company_id_by_name`` maps each seed ``name`` to its company id; sources
    without a company mapping are skipped (a source requires a company). Returns
    the persisted sources in seed order.
    """
    persisted: list[CrawlSource] = []
    for seed in DEFAULT_SEED_SOURCES:
        company_id = company_id_by_name.get(seed.name)
        if company_id is None:
            continue
        source = seed_to_crawl_source(seed, owner_id=owner_id, company_id=company_id)
        persisted.append(repository.save(source))
    return persisted
