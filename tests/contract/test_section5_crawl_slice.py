"""Contract test: Section 5 read-only crawl slice gate (task 5.10).

Proves the vertical slice: user profile -> managed crawl plan ->
provenance-backed crawl run -> new/updated/closed jobs visible in the
inbox, without duplicate records.

This is the gate for Section 6: it proves the data layer (Sections 2-5)
supports a working read-only discovery slice.

Uses in-memory repos + fake fetcher (no real network, no database).
The provenance-tracking ingest layer simulates the idempotent dedup
logic of ``m1_crawl_sink.ingest_posting`` and records
``crawl_run_id`` / ``plan_version_id`` on every ingested version.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.application.crawl_execution import CrawlExecutionService
from careerops.application.crawl_plan_service import (
    CrawlPlanPreferences,
    CrawlPlanService,
    CrawlRunService,
    CrawlSourceService,
)
from careerops.application.crawl_policy import evaluate_crawl_policy
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceType,
)
from careerops.infrastructure.memory_repos import (
    InMemoryCrawlPlanRepository,
    InMemoryCrawlRunRepository,
    InMemoryCrawlSourceRepository,
)
from careerops.infrastructure.temporal.m1_crawl_sink import (
    CrawlSourceResult,
    RealCrawlActivitySink,
)
from careerops.workflows.m1_contracts import CrawledPostingRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_fetcher(url: str) -> FetchedResponse:
    """Fake fetcher that returns a successful response for any URL."""
    return FetchedResponse(
        status_code=200,
        final_url=url,
        fetched_at=datetime.now(tz=UTC),
        response_hash="a" * 64,
        body='<html><script type="application/ld+json">'
        '{"@type":"JobPosting","title":"Engineer","url":"https://example.com/j/1"}'
        "</script></html>",
    )


def _content_hash(structured_data: dict[str, str]) -> str:
    """Deterministic SHA-256 hex digest, mirroring m1_crawl_sink._content_hash."""
    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _make_posting(
    source_id: str,
    external_id: str = "ext-1",
    title: str = "Engineer",
    location: str = "Remote",
) -> CrawledPostingRecord:
    return CrawledPostingRecord(
        source_id=source_id,
        external_id=external_id,
        canonical_url=f"https://example.com/jobs/{external_id}",
        source_url="https://boards.greenhouse.io/example",
        structured_data={"title": title, "location": location},
        parser_version="greenhouse-v1",
        fetched_at=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Provenance-tracking ingest simulation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class IngestedVersionRecord:
    """A single ingested version with provenance fields."""

    version_id: UUID
    posting_id: UUID
    source_id: UUID
    external_id: str
    content_hash: str
    structured_data: dict[str, str]
    crawl_run_id: UUID | None
    plan_version_id: UUID | None
    captured_at: datetime


class ProvenanceTrackingIngest:
    """In-memory ingest simulation that tracks provenance.

    Mirrors the idempotent dedup logic of
    ``RealCrawlActivitySink.ingest_posting``:

    * Posting: unique on ``(source_id, external_id)``.
    * Version: unique on ``(posting_id, content_hash)``.
    * ``last_seen_at`` is touched on every visit to an existing posting.
    * ``crawl_run_id`` and ``plan_version_id`` are recorded on each
      new version row.
    """

    def __init__(self) -> None:
        # (source_id, external_id) -> posting_id
        self._postings: dict[tuple[UUID, str], UUID] = {}
        # (posting_id, content_hash) -> record
        self._versions: dict[tuple[UUID, str], IngestedVersionRecord] = {}
        # posting_id -> last_seen_at
        self._last_seen: dict[UUID, datetime] = {}
        # Ordered list of all version records.
        self.all_versions: list[IngestedVersionRecord] = []

    async def ingest_posting(
        self,
        record: CrawledPostingRecord,
        *,
        crawl_run_id: UUID | None = None,
        plan_version_id: UUID | None = None,
    ) -> dict[str, bool]:
        source_id = UUID(record.source_id)
        content_hash = _content_hash(record.structured_data)
        fetched_at = (
            datetime.fromisoformat(record.fetched_at) if record.fetched_at else datetime.now(tz=UTC)
        )
        posting_key = (source_id, record.external_id)

        # --- Upsert posting (ON CONFLICT DO NOTHING) ---
        is_new_posting = posting_key not in self._postings
        if is_new_posting:
            posting_id = uuid4()
            self._postings[posting_key] = posting_id
        else:
            posting_id = self._postings[posting_key]

        # --- Upsert version (ON CONFLICT DO NOTHING) ---
        version_key = (posting_id, content_hash)
        is_new_version = version_key not in self._versions
        if is_new_version:
            vr = IngestedVersionRecord(
                version_id=uuid4(),
                posting_id=posting_id,
                source_id=source_id,
                external_id=record.external_id,
                content_hash=content_hash,
                structured_data=dict(record.structured_data),
                crawl_run_id=crawl_run_id,
                plan_version_id=plan_version_id,
                captured_at=fetched_at,
            )
            self._versions[version_key] = vr
            self.all_versions.append(vr)

        # Touch last_seen_at on every visit.
        self._last_seen[posting_id] = fetched_at

        return {"is_new_posting": is_new_posting, "is_new_version": is_new_version}

    # -- Query helpers --

    def get_last_seen(self, source_id: UUID, external_id: str) -> datetime | None:
        pid = self._postings.get((source_id, external_id))
        return self._last_seen.get(pid) if pid else None

    def get_versions_for(self, source_id: UUID, external_id: str) -> list[IngestedVersionRecord]:
        pid = self._postings.get((source_id, external_id))
        if pid is None:
            return []
        return [v for v in self.all_versions if v.posting_id == pid]

    def posting_count(self) -> int:
        return len(self._postings)

    def version_count(self) -> int:
        return len(self._versions)


# ---------------------------------------------------------------------------
# Fixture: repos + services + source + active plan
# ---------------------------------------------------------------------------


def _setup(
    *,
    owner_id: UUID,
    source_base_url: str = "https://boards.greenhouse.io/example",
    source_type: CrawlSourceType = CrawlSourceType.GREENHOUSE,
    terms_status: CrawlPolicyStatus = CrawlPolicyStatus.ALLOWED,
) -> tuple[
    CrawlSource,  # source
    UUID,  # plan_version_id
    InMemoryCrawlSourceRepository,
    InMemoryCrawlPlanRepository,
    InMemoryCrawlRunRepository,
    CrawlRunService,
]:
    source_repo = InMemoryCrawlSourceRepository()
    plan_repo = InMemoryCrawlPlanRepository()
    run_repo = InMemoryCrawlRunRepository(plan_repo)

    source_service = CrawlSourceService(source_repo)
    plan_service = CrawlPlanService(plan_repo)
    run_service = CrawlRunService(
        run_repo, plan_repository=plan_repo, source_repository=source_repo
    )

    company_id = uuid4()
    source = source_service.register(
        owner_id,
        company_id=company_id,
        source_type=source_type.value,
        source_identifier="example",
        base_url=source_base_url,
        enabled=True,
        terms_status=terms_status,
        now=datetime.now(tz=UTC),
    )

    plan_version = plan_service.create_version(
        owner_id,
        CrawlPlanPreferences(
            sources=(source.id,),
            schedule_interval_seconds=3600,
            schedule_timezone="UTC",
        ),
        activate=True,
        now=datetime.now(tz=UTC),
    )

    return source, plan_version.id, source_repo, plan_repo, run_repo, run_service


def _build_service(
    *,
    source_repo: InMemoryCrawlSourceRepository,
    plan_repo: InMemoryCrawlPlanRepository,
    run_repo: InMemoryCrawlRunRepository,
    ingest: ProvenanceTrackingIngest,
    postings_to_return: tuple[CrawledPostingRecord, ...],
) -> CrawlExecutionService:
    """Wire a CrawlExecutionService with a controlled sink."""
    sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

    async def controlled_crawl(request: object) -> CrawlSourceResult:
        return CrawlSourceResult(
            postings=postings_to_return,
            status_code=200,
            body_prefix="<html>ok</html>",
        )

    sink.crawl_source_with_signals = controlled_crawl  # type: ignore[method-assign]
    sink.ingest_posting = ingest.ingest_posting  # type: ignore[method-assign]

    return CrawlExecutionService(
        run_repository=run_repo,
        plan_repository=plan_repo,
        source_repository=source_repo,
        sink=sink,
        policy_fn=evaluate_crawl_policy,
    )


# ---------------------------------------------------------------------------
# (1) Plan + manual run -> execution ingests postings with provenance
# ---------------------------------------------------------------------------


class TestProvenanceIngest:
    """Creating a plan + manual run -> execution ingests postings with
    provenance (crawl_run_id + plan_version_id on each version)."""

    @pytest.mark.asyncio
    async def test_run_ingests_postings_with_provenance(self) -> None:
        """A PENDING run executed through CrawlExecutionService produces
        new postings whose versions carry the correct crawl_run_id and
        plan_version_id."""
        owner_id = uuid4()
        source, plan_id, source_repo, plan_repo, run_repo, run_service = _setup(owner_id=owner_id)

        posting_a = _make_posting(str(source.id), "ext-a", "Engineer")
        posting_b = _make_posting(str(source.id), "ext-b", "Designer")
        ingest = ProvenanceTrackingIngest()

        service = _build_service(
            source_repo=source_repo,
            plan_repo=plan_repo,
            run_repo=run_repo,
            ingest=ingest,
            postings_to_return=(posting_a, posting_b),
        )

        # Create PENDING run and execute.
        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run.state is CrawlRunState.PENDING

        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result.state is CrawlRunState.SUCCEEDED
        assert result.counters.discovered == 2
        assert result.counters.updated == 0
        assert result.counters.failed == 0

        # Verify provenance on both versions.
        assert ingest.posting_count() == 2
        assert ingest.version_count() == 2

        for ext_id in ("ext-a", "ext-b"):
            versions = ingest.get_versions_for(source.id, ext_id)
            assert len(versions) == 1, f"expected 1 version for {ext_id}"
            v = versions[0]
            assert v.crawl_run_id == run.id, f"{ext_id}: crawl_run_id mismatch"
            assert v.plan_version_id == plan_id, f"{ext_id}: plan_version_id mismatch"


# ---------------------------------------------------------------------------
# (2) Re-running the same run_identity does NOT create duplicates
# ---------------------------------------------------------------------------


class TestIdempotentReRun:
    """Re-running the same run_identity (via run_now idempotency) does NOT
    create duplicate postings or versions."""

    @pytest.mark.asyncio
    async def test_same_content_no_duplicates(self) -> None:
        """Two executions with the same source content produce exactly 2
        postings and 2 versions total. The second run returns
        is_new_posting=False, is_new_version=False."""
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service = _setup(owner_id=owner_id)

        posting = _make_posting(str(source.id), "ext-idem", "Engineer")
        ingest = ProvenanceTrackingIngest()

        service = _build_service(
            source_repo=source_repo,
            plan_repo=plan_repo,
            run_repo=run_repo,
            ingest=ingest,
            postings_to_return=(posting,),
        )

        # First execution.
        run1 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result1 = await service.execute(owner_id, run1.id, now=datetime.now(tz=UTC))
        assert result1.state is CrawlRunState.SUCCEEDED
        assert result1.counters.discovered == 1
        assert result1.counters.updated == 0

        # Second execution (new run, same content).
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run2.id != run1.id, "new run after terminal state"
        result2 = await service.execute(owner_id, run2.id, now=datetime.now(tz=UTC))
        assert result2.state is CrawlRunState.SUCCEEDED
        assert result2.counters.discovered == 0
        assert result2.counters.updated == 0

        # Exactly 1 posting, 1 version.
        assert ingest.posting_count() == 1
        assert ingest.version_count() == 1

        # The single version is bound to the FIRST run's provenance.
        versions = ingest.get_versions_for(source.id, "ext-idem")
        assert len(versions) == 1
        assert versions[0].crawl_run_id == run1.id


# ---------------------------------------------------------------------------
# (3) Jobs queryable with provenance fields populated
# ---------------------------------------------------------------------------


class TestQueryableProvenance:
    """The resulting ingested versions are queryable with provenance
    fields populated (crawl_run_id + plan_version_id)."""

    @pytest.mark.asyncio
    async def test_all_versions_carry_provenance(self) -> None:
        """Every version produced by a crawl run carries non-None
        crawl_run_id and plan_version_id matching the run and plan."""
        owner_id = uuid4()
        source, plan_id, source_repo, plan_repo, run_repo, run_service = _setup(owner_id=owner_id)

        postings = tuple(_make_posting(str(source.id), f"ext-q-{i}", f"Role {i}") for i in range(5))
        ingest = ProvenanceTrackingIngest()

        service = _build_service(
            source_repo=source_repo,
            plan_repo=plan_repo,
            run_repo=run_repo,
            ingest=ingest,
            postings_to_return=postings,
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result.state is CrawlRunState.SUCCEEDED
        assert result.counters.discovered == 5

        # Every version must have provenance.
        for vr in ingest.all_versions:
            assert vr.crawl_run_id is not None, f"missing crawl_run_id on {vr.external_id}"
            assert vr.plan_version_id is not None, f"missing plan_version_id on {vr.external_id}"
            assert vr.crawl_run_id == run.id
            assert vr.plan_version_id == plan_id


# ---------------------------------------------------------------------------
# (4) Updated content -> new version, no duplicate posting
# ---------------------------------------------------------------------------


class TestUpdatedContentNewVersion:
    """Updated content for the same (source_id, external_id) creates a
    new version (content_hash changed) but does NOT create a duplicate
    posting."""

    @pytest.mark.asyncio
    async def test_content_change_creates_new_version_only(self) -> None:
        """Run 1 ingests posting with title 'Engineer'. Run 2 ingests the
        same external_id with title 'Senior Engineer'. Result: 1 posting,
        2 versions. Second version carries run2's provenance."""
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service = _setup(owner_id=owner_id)

        posting_v1 = _make_posting(str(source.id), "ext-upd", "Engineer")
        posting_v2 = _make_posting(str(source.id), "ext-upd", "Senior Engineer")

        # Verify content hashes differ.
        assert _content_hash(posting_v1.structured_data) != _content_hash(
            posting_v2.structured_data
        )

        ingest = ProvenanceTrackingIngest()
        call_count = 0

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def varying_crawl(request: object) -> CrawlSourceResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return CrawlSourceResult(
                    postings=(posting_v1,), status_code=200, body_prefix="<html>ok</html>"
                )
            return CrawlSourceResult(
                postings=(posting_v2,), status_code=200, body_prefix="<html>ok</html>"
            )

        sink.crawl_source_with_signals = varying_crawl  # type: ignore[method-assign]
        sink.ingest_posting = ingest.ingest_posting  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        # Run 1: new posting + new version.
        run1 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result1 = await service.execute(owner_id, run1.id, now=datetime.now(tz=UTC))
        assert result1.counters.discovered == 1
        assert result1.counters.updated == 0

        # Run 2: same posting, different content -> updated.
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result2 = await service.execute(owner_id, run2.id, now=datetime.now(tz=UTC))
        assert result2.counters.discovered == 0
        assert result2.counters.updated == 1

        # Exactly 1 posting, 2 versions.
        assert ingest.posting_count() == 1
        assert ingest.version_count() == 2

        # Both versions share the same posting_id.
        v1_list = ingest.get_versions_for(source.id, "ext-upd")
        assert len(v1_list) == 2
        assert v1_list[0].posting_id == v1_list[1].posting_id, (
            "same external_id must map to same posting"
        )

        # Version provenance matches respective runs.
        assert v1_list[0].crawl_run_id == run1.id
        assert v1_list[0].content_hash == _content_hash(posting_v1.structured_data)
        assert v1_list[1].crawl_run_id == run2.id
        assert v1_list[1].content_hash == _content_hash(posting_v2.structured_data)


# ---------------------------------------------------------------------------
# (5) Closed/unseen postings: last_seen_at not updated
# ---------------------------------------------------------------------------


class TestClosedUnseenPostings:
    """Postings not seen in a subsequent run retain their original
    last_seen_at (detectable for stale/close logic)."""

    @pytest.mark.asyncio
    async def test_unseen_posting_retains_last_seen_at(self) -> None:
        """Run 1 sees postings A, B, C. Run 2 sees only A, B.
        C's last_seen_at remains from run 1; A and B's last_seen_at
        is updated to run 2's timestamp.

        Postings are created with distinct fetched_at values per run so
        that last_seen_at differs between the two runs.
        """
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service = _setup(owner_id=owner_id)

        t1 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 7, 26, 11, 0, 0, tzinfo=UTC)

        def _make_posting_at(ext_id: str, title: str, fetched_at: datetime) -> CrawledPostingRecord:
            return CrawledPostingRecord(
                source_id=str(source.id),
                external_id=ext_id,
                canonical_url=f"https://example.com/jobs/{ext_id}",
                source_url="https://boards.greenhouse.io/example",
                structured_data={"title": title, "location": "Remote"},
                parser_version="greenhouse-v1",
                fetched_at=fetched_at.isoformat(),
            )

        ingest = ProvenanceTrackingIngest()
        call_count = 0

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def selective_crawl(request: object) -> CrawlSourceResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Run 1: all three postings at t1.
                return CrawlSourceResult(
                    postings=(
                        _make_posting_at("ext-a", "Engineer", t1),
                        _make_posting_at("ext-b", "Designer", t1),
                        _make_posting_at("ext-c", "Manager", t1),
                    ),
                    status_code=200,
                    body_prefix="<html>ok</html>",
                )
            # Run 2: only A and B at t2 (C disappeared).
            return CrawlSourceResult(
                postings=(
                    _make_posting_at("ext-a", "Engineer", t2),
                    _make_posting_at("ext-b", "Designer", t2),
                ),
                status_code=200,
                body_prefix="<html>ok</html>",
            )

        sink.crawl_source_with_signals = selective_crawl  # type: ignore[method-assign]
        sink.ingest_posting = ingest.ingest_posting  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        # Run 1 at t1.
        run1 = run_service.run_now(owner_id, now=t1)
        await service.execute(owner_id, run1.id, now=t1)

        # Record last_seen_at for all three after run 1.
        seen_a_r1 = ingest.get_last_seen(source.id, "ext-a")
        seen_b_r1 = ingest.get_last_seen(source.id, "ext-b")
        seen_c_r1 = ingest.get_last_seen(source.id, "ext-c")
        assert seen_a_r1 is not None
        assert seen_b_r1 is not None
        assert seen_c_r1 is not None

        # Run 2 at t2.
        run2 = run_service.run_now(owner_id, now=t2)
        await service.execute(owner_id, run2.id, now=t2)

        # A and B last_seen_at updated to t2.
        seen_a_r2 = ingest.get_last_seen(source.id, "ext-a")
        seen_b_r2 = ingest.get_last_seen(source.id, "ext-b")
        assert seen_a_r2 is not None and seen_a_r2 > seen_a_r1
        assert seen_b_r2 is not None and seen_b_r2 > seen_b_r1

        # C last_seen_at unchanged (still from run 1 at t1).
        seen_c_r2 = ingest.get_last_seen(source.id, "ext-c")
        assert seen_c_r2 == seen_c_r1, (
            "unseen posting must retain its last_seen_at from the prior run"
        )
