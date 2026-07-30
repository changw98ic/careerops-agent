"""Seed-source migration contract (real-autonomous-career-loop Phase 4.3).

Pins the migration of the deleted static ``DEFAULT_SOURCES`` registry into the
canonical persisted-registry shape:

- :data:`DEFAULT_SEED_SOURCES` preserves the seven default sources verbatim.
- :func:`seed_to_crawl_source` maps each into a persisted :class:`CrawlSource`
  with ``source_type=OFFICIAL`` / ``executor_mode=EGO`` (browser-rendered career
  pages) and ``state=PENDING_REVIEW`` / disabled (the readiness gate keeps new
  sources paused until reviewed).
- ``api_substr`` hints are carried in ``last_run_metadata`` (no dedicated column
  exists on ``job_sources``).
- Source ids are derived deterministically from the URL, so re-seeding is
  idempotent.
- :func:`seed_default_sources` writes through the repository, skipping sources
  without a company mapping.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from careerops.application.crawl_seed import (
    DEFAULT_SEED_SOURCES,
    seed_default_sources,
    seed_to_crawl_source,
)
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlSource,
    CrawlSourceType,
    CrawlSourceState,
)

_EXPECTED_NAMES = {"aliyun", "tencent", "bytedance", "baidu", "meituan", "jd", "netease"}


class _RecordingRepo:
    """Fake ``CrawlSourceRepository`` that records saved sources."""

    def __init__(self) -> None:
        self.saved: list[CrawlSource] = []

    def save(self, source: CrawlSource) -> CrawlSource:
        self.saved.append(source)
        return source


class TestSeedSources:
    def test_seven_default_sources_preserved(self) -> None:
        assert len(DEFAULT_SEED_SOURCES) == 7
        assert {s.name for s in DEFAULT_SEED_SOURCES} == _EXPECTED_NAMES

    def test_aliyun_carries_api_substr_hint(self) -> None:
        aliyun = next(s for s in DEFAULT_SEED_SOURCES if s.name == "aliyun")
        assert aliyun.api_substr == "position/search"
        # Sources without a known hint default to empty (auto-discovery).
        tencent = next(s for s in DEFAULT_SEED_SOURCES if s.name == "tencent")
        assert tencent.api_substr == ""

    def test_seed_to_crawl_source_maps_to_persisted_shape(self) -> None:
        owner_id = uuid4()
        company_id = uuid4()
        seed = DEFAULT_SEED_SOURCES[0]

        source = seed_to_crawl_source(seed, owner_id=owner_id, company_id=company_id)

        assert source.owner_id == owner_id
        assert source.company_id == company_id
        assert source.source_type is CrawlSourceType.OFFICIAL
        assert source.executor_mode is CrawlExecutorMode.EGO
        assert source.state is CrawlSourceState.PENDING_REVIEW
        assert source.enabled is False  # paused until reviewed
        assert source.source_identifier == seed.name
        assert source.base_url == seed.base_url
        # api_substr carried in last_run_metadata (no dedicated column).
        assert source.last_run_metadata.get("api_substr") == seed.api_substr

    def test_source_ids_are_deterministic_per_url(self) -> None:
        owner_id = uuid4()
        company_id = uuid4()
        seed = DEFAULT_SEED_SOURCES[1]

        a = seed_to_crawl_source(seed, owner_id=owner_id, company_id=company_id)
        b = seed_to_crawl_source(seed, owner_id=owner_id, company_id=company_id)

        assert a.id == b.id  # re-seeding is idempotent
        assert isinstance(a.id, UUID)

    def test_no_api_substr_leaves_metadata_empty(self) -> None:
        owner_id = uuid4()
        company_id = uuid4()
        # tencent has no api_substr hint.
        seed = next(s for s in DEFAULT_SEED_SOURCES if s.name == "tencent")

        source = seed_to_crawl_source(seed, owner_id=owner_id, company_id=company_id)

        assert source.last_run_metadata == {}

    def test_seed_default_sources_writes_mapped_sources_only(self) -> None:
        owner_id = uuid4()
        repo = _RecordingRepo()
        # Only two sources have a company mapping; the rest are skipped.
        company_map = {
            "aliyun": uuid4(),
            "tencent": uuid4(),
        }

        persisted = seed_default_sources(
            repo, owner_id=owner_id, company_id_by_name=company_map
        )

        assert len(persisted) == 2
        assert {p.source_identifier for p in persisted} == {"aliyun", "tencent"}
        assert len(repo.saved) == 2
