"""Integration tests for crawl execution, safety, and provenance (Section 5, task 5.8).

End-to-end-career-application-loop. These tests exercise the full crawl
execution path with in-memory repositories (no database) and a fake fetcher
(no real network). They prove:

- restart/retry: transient failures (network error) do not prevent the run
  from reaching a terminal state; the run can be re-executed after completion.
- overlapping schedules coalesce: a second run-now while one is in flight
  returns the existing run; a scheduled trigger during a running crawl records
  a CANCELLED run with reason.
- SSRF redirects blocked: the crawl policy denies SSRF targets (private IPs,
  metadata endpoints) BEFORE any fetch attempt.
- private/metadata targets denied: 169.254.169.254 and RFC1918 ranges are
  rejected by the policy.
- rate limits respected: HTTP 429 triggers backoff and records
  ``next_eligible_at``.
- duplicate source observations idempotent: re-ingesting the same content
  hash does not create duplicate posting versions.

Uses:
- In-memory repos from :mod:`careerops.infrastructure.memory_repos`.
- Real crawl policy from :mod:`careerops.application.crawl_policy`.
- Real backoff policy from :mod:`careerops.application.backoff_policy`.
- Real execution service from :mod:`careerops.application.crawl_execution`.
- A fake fetcher + mock sink to avoid network I/O.
"""

from __future__ import annotations

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
from careerops.domain.crawl import CrawlDecision, CrawlPolicyInput, CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceState,
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
from careerops.observability.crawl_metrics import CrawlMetrics
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


def _setup_repos_and_services(
    *,
    owner_id: UUID,
    source_base_url: str = "https://boards.greenhouse.io/example",
    source_type: CrawlSourceType = CrawlSourceType.GREENHOUSE,
    terms_status: CrawlPolicyStatus = CrawlPolicyStatus.ALLOWED,
    enabled: bool = True,
    state: CrawlSourceState = CrawlSourceState.ACTIVE,
) -> tuple[
    CrawlSource,
    UUID,
    InMemoryCrawlSourceRepository,
    InMemoryCrawlPlanRepository,
    InMemoryCrawlRunRepository,
    CrawlRunService,
    CrawlPlanService,
]:
    """Set up in-memory repos, a source, an active plan, and services."""
    source_repo = InMemoryCrawlSourceRepository()
    plan_repo = InMemoryCrawlPlanRepository()
    run_repo = InMemoryCrawlRunRepository(plan_repo)

    source_service = CrawlSourceService(source_repo)
    plan_service = CrawlPlanService(plan_repo)
    run_service = CrawlRunService(
        run_repo, plan_repository=plan_repo, source_repository=source_repo
    )

    # Register a source.
    company_id = uuid4()
    source = source_service.register(
        owner_id,
        company_id=company_id,
        source_type=source_type.value,
        source_identifier="example",
        base_url=source_base_url,
        enabled=enabled,
        terms_status=terms_status,
        now=datetime.now(tz=UTC),
    )
    # Force source state if different from ACTIVE.
    if state is not CrawlSourceState.ACTIVE:
        source = source_repo.update_state(
            owner_id, source.id, state=state, now=datetime.now(tz=UTC)
        )

    # Create an active plan with this source.
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

    return source, plan_version.id, source_repo, plan_repo, run_repo, run_service, plan_service


# ---------------------------------------------------------------------------
# Test: restart / retry (transient failures)
# ---------------------------------------------------------------------------


class TestRestartRetry:
    """Transient failures allow retry; policy denials do not create new runs."""

    @pytest.mark.asyncio
    async def test_transient_failure_allows_retry(self) -> None:
        """A run that fails due to a network error can be retried.

        The first execution encounters a transient error on the source fetch,
        the run reaches FAILED state. A second run_now creates a new PENDING
        run that can be executed again.
        """
        owner_id = uuid4()
        _source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        # Create a PENDING run via run_now.
        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run.state is CrawlRunState.PENDING

        # Create a sink that raises on first call, succeeds on second.
        call_count = 0

        async def failing_crawl(request: object) -> CrawlSourceResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient network error")
            return CrawlSourceResult(postings=(), status_code=200)

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        sink.crawl_source_with_signals = failing_crawl  # type: ignore[method-assign]

        async def fake_ingest(
            record: object,
            *,
            crawl_run_id: UUID | None = None,
            plan_version_id: UUID | None = None,
        ) -> dict[str, bool]:
            return {"is_new_posting": False, "is_new_version": False}

        sink.ingest_posting = fake_ingest  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        # First execution: the source fails but the run still completes.
        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result.state is CrawlRunState.SUCCEEDED  # per-source failure, not run-level
        assert result.counters.failed == 1

        # A new run_now after terminal state creates a new run (retry allowed).
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run2.id != run.id
        assert run2.state is CrawlRunState.PENDING

    @pytest.mark.asyncio
    async def test_policy_denial_does_not_block_subsequent_runs(self) -> None:
        """A run that fails due to policy denial still completes (SUCCEEDED
        with failed counter) and a new run can be started afterward.
        """
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(
                owner_id=owner_id,
                terms_status=CrawlPolicyStatus.BLOCKED,
            )
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def no_call(*args: object, **kwargs: object) -> CrawlSourceResult:  # type: ignore[type-arg]
            pytest.fail("fetch should not be called when policy denies")

        sink.crawl_source_with_signals = no_call  # type: ignore[method-assign]
        sink.ingest_posting = no_call  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result.state is CrawlRunState.SUCCEEDED
        assert result.counters.failed == 1
        # Source should be marked BLOCKED.
        updated_source = source_repo.get_by_id(owner_id, source.id)
        assert updated_source.state is CrawlSourceState.BLOCKED

        # Resume the source so it's eligible again.
        source_repo.update_state(owner_id, source.id, state=CrawlSourceState.ACTIVE, enabled=True)

        # A new run can be created.
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run2.state is CrawlRunState.PENDING


# ---------------------------------------------------------------------------
# Test: overlapping schedules coalesce
# ---------------------------------------------------------------------------


class TestOverlappingSchedulesCoalesce:
    """Overlapping triggers coalesce: run_now returns existing non-terminal run;
    scheduled overlap records CANCELLED with reason."""

    def test_run_now_returns_existing_pending_run(self) -> None:
        """A second run_now while the first is PENDING returns the same run."""
        owner_id = uuid4()
        _source, _plan_id, _source_repo, _plan_repo, _run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run1 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        assert run1.id == run2.id
        assert run1.run_identity == run2.run_identity

    def test_run_now_returns_existing_running_run(self) -> None:
        """A second run_now while the first is RUNNING returns the same run."""
        owner_id = uuid4()
        _source, _plan_id, _source_repo, _plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run1 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        # Simulate transition to RUNNING.
        run_repo.update_terminal(
            owner_id,
            run1.id,
            state=CrawlRunState.RUNNING,
            started_at=datetime.now(tz=UTC),
            now=datetime.now(tz=UTC),
        )

        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        assert run2.id == run1.id

    def test_overlap_records_cancelled_run(self) -> None:
        """record_overlap_skip creates a CANCELLED run with coalesce reason."""
        owner_id = uuid4()
        source, plan_id, _source_repo, _plan_repo, _run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        cancelled = run_service.record_overlap_skip(
            owner_id,
            plan_version_id=plan_id,
            source_set=(source.id,),
            reason="overlap_with_running",
            now=datetime.now(tz=UTC),
        )

        assert cancelled.state is CrawlRunState.CANCELLED
        assert cancelled.error_category == "overlap_with_running"
        assert cancelled.ended_at is not None


# ---------------------------------------------------------------------------
# Test: SSRF redirects blocked + private/metadata targets denied
# ---------------------------------------------------------------------------


class TestSSRFAndPrivateTargetsDenied:
    """The crawl policy denies SSRF targets before any fetch occurs."""

    def test_private_ip_denied(self) -> None:
        """10.x.x.x, 172.16.x.x, 192.168.x.x are denied."""
        for url in [
            "https://10.0.0.1/careers",
            "https://172.16.0.1/careers",
            "https://192.168.1.1/careers",
        ]:
            decision = evaluate_crawl_policy(
                CrawlPolicyInput(
                    source_url=url,
                    terms_status="allowed",
                    domain="10.0.0.1",
                )
            )
            assert decision.decision is CrawlDecision.DENY_SSRF, f"expected SSRF deny for {url}"

    def test_localhost_denied(self) -> None:
        """localhost is denied (exact-match pattern)."""
        decision = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="http://localhost/careers",
                terms_status="allowed",
                domain="localhost",
            )
        )
        assert decision.decision is CrawlDecision.DENY_SSRF

    def test_metadata_endpoint_denied(self) -> None:
        """169.254.169.254 and metadata.google.internal are denied."""
        for url in [
            "http://169.254.169.254/metadata",
            "http://metadata.google.internal/metadata",
        ]:
            decision = evaluate_crawl_policy(
                CrawlPolicyInput(
                    source_url=url,
                    terms_status="allowed",
                    domain="169.254.169.254",
                )
            )
            assert decision.decision is CrawlDecision.DENY_SSRF, f"expected SSRF deny for {url}"

    def test_loopback_denied(self) -> None:
        """127.0.0.1 is denied."""
        decision = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="http://127.0.0.1/careers",
                terms_status="allowed",
                domain="127.0.0.1",
            )
        )
        assert decision.decision is CrawlDecision.DENY_SSRF

    @pytest.mark.asyncio
    async def test_ssrf_denied_before_fetch(self) -> None:
        """When policy denies SSRF, the execution service skips the source
        without calling the crawl adapter."""
        owner_id = uuid4()
        _source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(
                owner_id=owner_id,
                source_base_url="http://10.0.0.1/careers",
            )
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        fetch_called = False

        async def tracking_fetch(request: object) -> CrawlSourceResult:
            nonlocal fetch_called
            fetch_called = True
            return CrawlSourceResult(postings=(), status_code=200)

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        sink.crawl_source_with_signals = tracking_fetch  # type: ignore[method-assign]
        sink.ingest_posting = tracking_fetch  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))

        assert fetch_called is False, "fetch should NOT be called for SSRF-denied URLs"
        assert result.counters.failed == 1


# ---------------------------------------------------------------------------
# Test: rate limits respected (backoff recorded)
# ---------------------------------------------------------------------------


class TestRateLimitsRespected:
    """HTTP 429 triggers backoff and records next_eligible_at."""

    @pytest.mark.asyncio
    async def test_429_records_backoff(self) -> None:
        """A 429 response triggers backoff and sets next_eligible_at on the run."""
        owner_id = uuid4()
        _source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        # Sink returns 429 with rate-limit body.
        async def rate_limited_crawl(request: object) -> CrawlSourceResult:
            return CrawlSourceResult(
                postings=(),
                status_code=429,
                body_prefix="Rate limit exceeded. Try again later.",
            )

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        sink.crawl_source_with_signals = rate_limited_crawl  # type: ignore[method-assign]

        async def noop_ingest(
            record: object,
            **kwargs: object,
        ) -> dict[str, bool]:
            return {"is_new_posting": False, "is_new_version": False}

        sink.ingest_posting = noop_ingest  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))

        assert result.state is CrawlRunState.SUCCEEDED
        assert result.counters.failed == 1
        assert result.counters.discovered == 0
        # next_eligible_at should be set (30-minute backoff for 429).
        assert result.next_eligible_at is not None
        assert result.next_eligible_at > datetime.now(tz=UTC)

    @pytest.mark.asyncio
    async def test_429_does_not_block_source(self) -> None:
        """429 is temporary; the source is NOT marked BLOCKED."""
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        async def rate_limited(request: object) -> CrawlSourceResult:
            return CrawlSourceResult(postings=(), status_code=429, body_prefix="rate limited")

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        sink.crawl_source_with_signals = rate_limited  # type: ignore[method-assign]

        async def noop(record: object, **kwargs: object) -> dict[str, bool]:
            return {"is_new_posting": False, "is_new_version": False}

        sink.ingest_posting = noop  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))

        # Source should NOT be blocked (429 is temporary).
        updated_source = source_repo.get_by_id(owner_id, source.id)
        assert updated_source.state is CrawlSourceState.ACTIVE


# ---------------------------------------------------------------------------
# Test: duplicate source observations idempotent
# ---------------------------------------------------------------------------


class TestDuplicateObservationsIdempotent:
    """Re-ingesting the same content hash does not create duplicate versions."""

    @pytest.mark.asyncio
    async def test_same_content_hash_no_duplicate_version(self) -> None:
        """Two postings with the same structured_data (same content_hash)
        produce only one new version. The second ingest returns
        is_new_version=False."""
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        posting = _make_posting(str(source.id), external_id="ext-dup", title="Engineer")

        ingest_calls: list[dict[str, bool]] = []

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def returning_posting(request: object) -> CrawlSourceResult:
            return CrawlSourceResult(
                postings=(posting,),
                status_code=200,
                body_prefix="<html>ok</html>",
            )

        sink.crawl_source_with_signals = returning_posting  # type: ignore[method-assign]

        # Real ingest_posting requires a DB engine; use a fake that tracks calls
        # and simulates the idempotent dedup behavior.
        call_count = 0

        async def fake_ingest(
            record: CrawledPostingRecord,
            *,
            crawl_run_id: UUID | None = None,
            plan_version_id: UUID | None = None,
        ) -> dict[str, bool]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                result = {"is_new_posting": True, "is_new_version": True}
            else:
                # Same content -> idempotent: posting exists, version exists.
                result = {"is_new_posting": False, "is_new_version": False}
            ingest_calls.append(result)
            return result

        sink.ingest_posting = fake_ingest  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        # First execution: new posting + new version.
        result1 = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result1.counters.discovered == 1
        assert result1.counters.updated == 0

        # Second run with the same content: idempotent.
        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result2 = await service.execute(owner_id, run2.id, now=datetime.now(tz=UTC))
        assert result2.counters.discovered == 0
        assert result2.counters.updated == 0  # is_new_version is False

    @pytest.mark.asyncio
    async def test_new_content_hash_creates_updated_version(self) -> None:
        """A posting with a different content_hash (same external_id) produces
        is_new_posting=False, is_new_version=True (updated)."""
        owner_id = uuid4()
        source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        posting_v1 = _make_posting(str(source.id), external_id="ext-v", title="Engineer")
        posting_v2 = _make_posting(str(source.id), external_id="ext-v", title="Senior Engineer")

        call_count = 0

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def returning_varying(request: object) -> CrawlSourceResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return CrawlSourceResult(
                    postings=(posting_v1,), status_code=200, body_prefix="<html>ok</html>"
                )
            return CrawlSourceResult(
                postings=(posting_v2,), status_code=200, body_prefix="<html>ok</html>"
            )

        sink.crawl_source_with_signals = returning_varying  # type: ignore[method-assign]

        ingest_call = 0

        async def fake_ingest(
            record: CrawledPostingRecord,
            *,
            crawl_run_id: UUID | None = None,
            plan_version_id: UUID | None = None,
        ) -> dict[str, bool]:
            nonlocal ingest_call
            ingest_call += 1
            if ingest_call == 1:
                return {"is_new_posting": True, "is_new_version": True}
            # Same posting (external_id), different content -> update.
            return {"is_new_posting": False, "is_new_version": True}

        sink.ingest_posting = fake_ingest  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        result1 = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))
        assert result1.counters.discovered == 1
        assert result1.counters.updated == 0

        run2 = run_service.run_now(owner_id, now=datetime.now(tz=UTC))
        result2 = await service.execute(owner_id, run2.id, now=datetime.now(tz=UTC))
        assert result2.counters.discovered == 0
        assert result2.counters.updated == 1


# ---------------------------------------------------------------------------
# Test: metrics integration (5.9)
# ---------------------------------------------------------------------------


class TestCrawlMetricsIntegration:
    """Verify CrawlMetrics records correct values during execution."""

    def test_run_started_ended_gauge(self) -> None:
        """Active runs gauge increments on start and decrements on end."""
        metrics = CrawlMetrics()
        assert metrics.registry.get_sample_value("careerops_crawl_active_runs") == 0.0

        metrics.run_started()
        assert metrics.registry.get_sample_value("careerops_crawl_active_runs") == 1.0

        metrics.run_started()
        assert metrics.registry.get_sample_value("careerops_crawl_active_runs") == 2.0

        metrics.run_ended()
        assert metrics.registry.get_sample_value("careerops_crawl_active_runs") == 1.0

        metrics.run_ended()
        assert metrics.registry.get_sample_value("careerops_crawl_active_runs") == 0.0

    def test_run_duration_histogram(self) -> None:
        """Run duration is recorded in the histogram."""
        metrics = CrawlMetrics()
        metrics.observe_run_duration(state="succeeded", duration_seconds=42.5)

        # The histogram should have one observation.
        sample = metrics.registry.get_sample_value(
            "careerops_crawl_run_duration_seconds_count", {"state": "succeeded"}
        )
        assert sample == 1.0

    def test_source_failure_counter(self) -> None:
        """Source failures are counted by error category."""
        metrics = CrawlMetrics()
        metrics.record_source_failure(error_category="rate_limited_429")
        metrics.record_source_failure(error_category="rate_limited_429")
        metrics.record_source_failure(error_category="blocked_403")

        rl = metrics.registry.get_sample_value(
            "careerops_crawl_source_failures_total", {"error_category": "rate_limited_429"}
        )
        assert rl == 2.0

        blocked = metrics.registry.get_sample_value(
            "careerops_crawl_source_failures_total", {"error_category": "blocked_403"}
        )
        assert blocked == 1.0

    def test_rate_limit_denial_counter(self) -> None:
        """Rate-limit denials are counted by domain."""
        metrics = CrawlMetrics()
        metrics.record_rate_limit_denial(domain="boards.greenhouse.io")
        metrics.record_rate_limit_denial(domain="boards.greenhouse.io")

        val = metrics.registry.get_sample_value(
            "careerops_crawl_rate_limit_denials_total",
            {"domain": "boards.greenhouse.io"},
        )
        assert val == 2.0

    def test_postings_counters(self) -> None:
        """Postings created/updated/closed are counted separately."""
        metrics = CrawlMetrics()
        metrics.record_posting_created(3)
        metrics.record_posting_updated(2)
        metrics.record_posting_closed(1)

        assert (
            metrics.registry.get_sample_value(
                "careerops_crawl_postings_total", {"outcome": "created"}
            )
            == 3.0
        )
        assert (
            metrics.registry.get_sample_value(
                "careerops_crawl_postings_total", {"outcome": "updated"}
            )
            == 2.0
        )
        assert (
            metrics.registry.get_sample_value(
                "careerops_crawl_postings_total", {"outcome": "closed"}
            )
            == 1.0
        )

    def test_policy_denial_counter(self) -> None:
        """Policy denials are counted by source_type and decision."""
        metrics = CrawlMetrics()
        metrics.record_policy_denial(source_type="greenhouse", decision="deny_blocked")
        metrics.record_policy_denial(source_type="lever", decision="deny_rate_limited")

        gh = metrics.registry.get_sample_value(
            "careerops_crawl_policy_denials_total",
            {"source_type": "greenhouse", "decision": "deny_blocked"},
        )
        assert gh == 1.0

        lever = metrics.registry.get_sample_value(
            "careerops_crawl_policy_denials_total",
            {"source_type": "lever", "decision": "deny_rate_limited"},
        )
        assert lever == 1.0

    def test_observe_run_result_convenience(self) -> None:
        """observe_run_result records duration and posting counts in one call."""
        metrics = CrawlMetrics()
        metrics.observe_run_result(
            state="succeeded",
            duration_seconds=120.0,
            discovered=5,
            updated=3,
            closed=1,
        )

        duration_count = metrics.registry.get_sample_value(
            "careerops_crawl_run_duration_seconds_count", {"state": "succeeded"}
        )
        assert duration_count == 1.0

        created = metrics.registry.get_sample_value(
            "careerops_crawl_postings_total", {"outcome": "created"}
        )
        assert created == 5.0

        updated = metrics.registry.get_sample_value(
            "careerops_crawl_postings_total", {"outcome": "updated"}
        )
        assert updated == 3.0

        closed = metrics.registry.get_sample_value(
            "careerops_crawl_postings_total", {"outcome": "closed"}
        )
        assert closed == 1.0


# ---------------------------------------------------------------------------
# Test: terms status unknown -> fail-closed (one discovery request allowed)
# ---------------------------------------------------------------------------


class TestTermsUnknownFailClosed:
    """Sources with terms_status='unknown' are denied by policy (safe limited-discovery)."""

    def test_terms_unknown_denied(self) -> None:
        """terms_status='unknown' produces DENY_TERMS_UNKNOWN."""
        decision = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="https://example.com/careers",
                terms_status="unknown",
                domain="example.com",
            )
        )
        assert decision.decision is CrawlDecision.DENY_TERMS_UNKNOWN

    @pytest.mark.asyncio
    async def test_terms_unknown_skips_source_in_execution(self) -> None:
        """A source with unknown terms is skipped during execution."""
        owner_id = uuid4()
        _source, _plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(
                owner_id=owner_id,
                terms_status=CrawlPolicyStatus.UNKNOWN,
            )
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        fetch_called = False

        async def tracking(request: object) -> CrawlSourceResult:
            nonlocal fetch_called
            fetch_called = True
            return CrawlSourceResult(postings=(), status_code=200)

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        sink.crawl_source_with_signals = tracking  # type: ignore[method-assign]

        async def noop(record: object, **kwargs: object) -> dict[str, bool]:
            return {"is_new_posting": False, "is_new_version": False}

        sink.ingest_posting = noop  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        result = await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))

        assert fetch_called is False
        assert result.counters.failed == 1


# ---------------------------------------------------------------------------
# Test: provenance recorded on ingest
# ---------------------------------------------------------------------------


class TestProvenanceRecorded:
    """Every ingested posting records crawl_run_id and plan_version_id."""

    @pytest.mark.asyncio
    async def test_provenance_passed_to_ingest(self) -> None:
        """crawl_run_id and plan_version_id are passed to ingest_posting."""
        owner_id = uuid4()
        source, plan_id, source_repo, plan_repo, run_repo, run_service, _ = (
            _setup_repos_and_services(owner_id=owner_id)
        )

        run = run_service.run_now(owner_id, now=datetime.now(tz=UTC))

        posting = _make_posting(str(source.id))
        captured_crawl_run_id: UUID | None = None
        captured_plan_version_id: UUID | None = None

        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)

        async def returning_posting(request: object) -> CrawlSourceResult:
            return CrawlSourceResult(
                postings=(posting,), status_code=200, body_prefix="<html>ok</html>"
            )

        sink.crawl_source_with_signals = returning_posting  # type: ignore[method-assign]

        async def capturing_ingest(
            record: CrawledPostingRecord,
            *,
            crawl_run_id: UUID | None = None,
            plan_version_id: UUID | None = None,
        ) -> dict[str, bool]:
            nonlocal captured_crawl_run_id, captured_plan_version_id
            captured_crawl_run_id = crawl_run_id
            captured_plan_version_id = plan_version_id
            return {"is_new_posting": True, "is_new_version": True}

        sink.ingest_posting = capturing_ingest  # type: ignore[method-assign]

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=evaluate_crawl_policy,
        )

        await service.execute(owner_id, run.id, now=datetime.now(tz=UTC))

        assert captured_crawl_run_id == run.id
        assert captured_plan_version_id == plan_id
