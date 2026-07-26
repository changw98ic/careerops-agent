"""Unit tests for CrawlExecutionService (Section 5, tasks 5.1-5.3, 5.5-5.7).

Tests the crawl execution flow: policy evaluation before fetch, provenance
on ingest_posting (crawl_run_id + plan_version_id), counter accumulation,
terminal state transitions, error handling, backoff/stop rules, and
fail-closed behavior on unknown dependencies.

Uses mock repositories and sink to isolate the service logic from I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from careerops.api.errors import InvalidStateError
from careerops.application.backoff_policy import (
    BackoffDecision,
    BackoffPolicy,
    BackoffReason,
    BackoffState,
    SourceFetchOutcome,
)
from careerops.application.crawl_execution import CrawlExecutionService
from careerops.domain.crawl import (
    CrawlDecision,
    CrawlPolicyDecision,
    CrawlPolicyInput,
    CrawlRunState,
)
from careerops.domain.crawl_plans import (
    CrawlPlanVersion,
    CrawlPolicyStatus,
    CrawlRun,
    CrawlSource,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult


def _make_source(
    *,
    source_id: UUID | None = None,
    owner_id: UUID | None = None,
    company_id: UUID | None = None,
    enabled: bool = True,
    state: CrawlSourceState = CrawlSourceState.ACTIVE,
    terms_status: CrawlPolicyStatus = CrawlPolicyStatus.ALLOWED,
    base_url: str = "https://boards.greenhouse.io/example",
    source_type: CrawlSourceType = CrawlSourceType.GREENHOUSE,
) -> CrawlSource:
    return CrawlSource(
        id=source_id or uuid4(),
        owner_id=owner_id or uuid4(),
        company_id=company_id or uuid4(),
        source_type=source_type,
        source_identifier="example",
        base_url=base_url,
        state=state,
        trust_status=CrawlPolicyStatus.ALLOWED,
        terms_status=terms_status,
        robots_status=CrawlPolicyStatus.ALLOWED,
        enabled=enabled,
    )


def _make_plan(
    *,
    plan_id: UUID | None = None,
    owner_id: UUID | None = None,
    sources: tuple[UUID, ...] = (),
) -> CrawlPlanVersion:
    return CrawlPlanVersion(
        id=plan_id or uuid4(),
        owner_id=owner_id or uuid4(),
        version=1,
        is_active=True,
        sources=sources,
        interval_seconds=3600,
        timezone="UTC",
    )


def _make_run(
    *,
    run_id: UUID | None = None,
    plan_version_id: UUID | None = None,
    state: CrawlRunState = CrawlRunState.PENDING,
) -> CrawlRun:
    return CrawlRun(
        id=run_id or uuid4(),
        plan_version_id=plan_version_id or uuid4(),
        run_identity=f"manual:{plan_version_id or uuid4()}:test:1",
        source_set=(),
        state=state,
    )


def _make_empty_signals_result() -> CrawlSourceResult:
    """Return a CrawlSourceResult with no postings and no signals."""
    return CrawlSourceResult(postings=(), status_code=200)


def _make_posting_result(
    source_id: str, external_id: str = "ext-1", title: str = "Engineer"
) -> CrawlSourceResult:
    """Return a CrawlSourceResult with one posting."""
    from careerops.workflows.m1_contracts import CrawledPostingRecord

    posting = CrawledPostingRecord(
        source_id=source_id,
        external_id=external_id,
        canonical_url=f"https://example.com/jobs/{external_id}",
        source_url="https://boards.greenhouse.io/example",
        structured_data={"title": title, "location": "Remote"},
        parser_version="greenhouse-v1",
        fetched_at=datetime.now(tz=UTC).isoformat(),
    )
    return CrawlSourceResult(
        postings=(posting,),
        status_code=200,
        body_prefix="<html>ok</html>",
    )


class TestCrawlExecutionService:
    """Tests for CrawlExecutionService.execute."""

    def _make_service(
        self,
        *,
        policy_fn: Callable[[CrawlPolicyInput], CrawlPolicyDecision] | None = None,
        backoff_policy: BackoffPolicy | None = None,
    ) -> tuple[CrawlExecutionService, MagicMock, MagicMock, MagicMock, MagicMock]:
        """Create a service with mock repos and sink."""
        run_repo = MagicMock()
        plan_repo = MagicMock()
        source_repo = MagicMock()
        sink = MagicMock()
        sink.crawl_source = AsyncMock(return_value=[])
        sink.crawl_source_with_signals = AsyncMock(return_value=_make_empty_signals_result())
        sink.ingest_posting = AsyncMock(
            return_value={"is_new_posting": True, "is_new_version": True}
        )
        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=policy_fn,
            backoff_policy=backoff_policy,
        )
        return service, run_repo, plan_repo, source_repo, sink

    @pytest.mark.asyncio
    async def test_rejects_non_pending_run(self) -> None:
        """execute raises InvalidStateError for a non-PENDING run."""
        service, run_repo, _plan_repo, _source_repo, _sink = self._make_service()
        run = _make_run(state=CrawlRunState.SUCCEEDED)
        run_repo.get_by_id.return_value = run

        with pytest.raises(InvalidStateError, match="only PENDING"):
            await service.execute(uuid4(), run.id)

    @pytest.mark.asyncio
    async def test_transitions_to_running_then_succeeded(self) -> None:
        """execute transitions PENDING -> RUNNING -> SUCCEEDED."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, _sink = self._make_service()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        # update_terminal returns progressively updated run records.
        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        assert run_repo.update_terminal.call_count == 2
        first_call = run_repo.update_terminal.call_args_list[0]
        assert first_call[1]["state"] == CrawlRunState.RUNNING
        second_call = run_repo.update_terminal.call_args_list[1]
        assert second_call[1]["state"] == CrawlRunState.SUCCEEDED

    @pytest.mark.asyncio
    async def test_policy_denied_skips_source(self) -> None:
        """Policy denial skips the source and increments the failed counter."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id, terms_status=CrawlPolicyStatus.BLOCKED)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        deny_fn = MagicMock(
            return_value=CrawlPolicyDecision(
                decision=CrawlDecision.DENY_BLOCKED,
                reason="terms blocked",
            )
        )

        service, run_repo, plan_repo, source_repo, sink = self._make_service(policy_fn=deny_fn)
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # crawl_source_with_signals was NOT called (policy denied before fetch).
        sink.crawl_source_with_signals.assert_not_called()
        # The terminal call should have failed=1 in counters.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert counters.discovered == 0

    @pytest.mark.asyncio
    async def test_provenance_passed_to_ingest_posting(self) -> None:
        """crawl_run_id + plan_version_id are passed to ingest_posting."""
        owner_id = uuid4()
        plan_id = uuid4()
        run_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(run_id=run_id, plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source
        sink.crawl_source_with_signals.return_value = _make_posting_result(str(source.id))
        sink.ingest_posting.return_value = {"is_new_posting": True, "is_new_version": True}

        running_run = _make_run(run_id=run_id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run_id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run_id)

        # Verify provenance was passed to ingest_posting.
        sink.ingest_posting.assert_called_once()
        call_kwargs = sink.ingest_posting.call_args[1]
        assert call_kwargs["crawl_run_id"] == run_id
        assert call_kwargs["plan_version_id"] == plan_id

    @pytest.mark.asyncio
    async def test_counters_accumulate_from_ingest_results(self) -> None:
        """Counters are accumulated from is_new_posting / is_new_version flags."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        # Two postings from the sink.
        from careerops.workflows.m1_contracts import CrawledPostingRecord

        posting1 = CrawledPostingRecord(
            source_id=str(source.id),
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            source_url="https://boards.greenhouse.io/example",
            structured_data={"title": "Engineer", "location": "Remote"},
            parser_version="greenhouse-v1",
        )
        posting2 = CrawledPostingRecord(
            source_id=str(source.id),
            external_id="ext-2",
            canonical_url="https://example.com/jobs/2",
            source_url="https://boards.greenhouse.io/example",
            structured_data={"title": "PM", "location": "NYC"},
            parser_version="greenhouse-v1",
        )

        sink.crawl_source_with_signals.return_value = CrawlSourceResult(
            postings=(posting1, posting2),
            status_code=200,
        )
        # First posting is new, second is an update (existing posting, new version).
        sink.ingest_posting.side_effect = [
            {"is_new_posting": True, "is_new_version": True},
            {"is_new_posting": False, "is_new_version": True},
        ]

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.discovered == 1
        assert counters.updated == 1
        assert counters.failed == 0

    @pytest.mark.asyncio
    async def test_skips_disabled_source(self) -> None:
        """Disabled sources are skipped without calling policy or fetch."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id, enabled=False)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        sink.crawl_source_with_signals.assert_not_called()
        sink.ingest_posting.assert_not_called()

    @pytest.mark.asyncio
    async def test_crawl_source_failure_increments_failed(self) -> None:
        """Exception from crawl_source increments the failed counter.

        Per-source failures are caught and counted; the run still succeeds
        because the exception did not propagate past the source loop. The
        error_category is set locally but not passed to the terminal update
        because the run is SUCCEEDED, not FAILED.
        """
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source
        sink.crawl_source_with_signals.side_effect = RuntimeError("network error")

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # Per-source failure: the run still succeeds with failed=1.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert counters.discovered == 0
        # error_category is only set on the FAILED path; the run succeeded.
        assert terminal_call[1]["state"] == CrawlRunState.SUCCEEDED

    @pytest.mark.asyncio
    async def test_policy_callback_is_invoked(self) -> None:
        """The policy callback is invoked with a CrawlPolicyInput."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        policy_fn = MagicMock(
            return_value=CrawlPolicyDecision(
                decision=CrawlDecision.ALLOW,
                reason="ok",
            )
        )

        service, run_repo, plan_repo, source_repo, _sink = self._make_service(policy_fn=policy_fn)
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        policy_fn.assert_called_once()
        policy_input = policy_fn.call_args[0][0]
        assert policy_input.source_url == source.base_url
        assert policy_input.terms_status == source.terms_status.value
        assert policy_input.domain == "boards.greenhouse.io"


class TestBackoffPolicyIntegration:
    """Tests for backoff/stop rules integration (task 5.7)."""

    def _make_service_with_backoff(
        self,
        *,
        policy_fn: Callable[[CrawlPolicyInput], CrawlPolicyDecision] | None = None,
    ) -> tuple[CrawlExecutionService, MagicMock, MagicMock, MagicMock, MagicMock]:
        """Create a service with real BackoffPolicy."""
        run_repo = MagicMock()
        plan_repo = MagicMock()
        source_repo = MagicMock()
        sink = MagicMock()
        sink.crawl_source_with_signals = AsyncMock(return_value=_make_empty_signals_result())
        sink.ingest_posting = AsyncMock(
            return_value={"is_new_posting": True, "is_new_version": True}
        )
        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=policy_fn,
            backoff_policy=BackoffPolicy(),
        )
        return service, run_repo, plan_repo, source_repo, sink

    @pytest.mark.asyncio
    async def test_403_triggers_backoff_and_blocks_source(self) -> None:
        """HTTP 403 triggers backoff and marks source as BLOCKED."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service_with_backoff()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        sink.crawl_source_with_signals.return_value = CrawlSourceResult(
            postings=(),
            status_code=403,
            body_prefix="Access Denied",
        )

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # Source should be marked BLOCKED.
        source_repo.update_state.assert_called_once()
        call_kwargs = source_repo.update_state.call_args
        assert call_kwargs[1]["state"] == CrawlSourceState.BLOCKED

        # Terminal call should have failed=1 and next_eligible_at set.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert terminal_call[1]["next_eligible_at"] is not None

    @pytest.mark.asyncio
    async def test_429_triggers_backoff(self) -> None:
        """HTTP 429 triggers backoff without blocking the source."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service_with_backoff()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        sink.crawl_source_with_signals.return_value = CrawlSourceResult(
            postings=(),
            status_code=429,
            body_prefix="Rate limited",
        )

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # Source should NOT be blocked (429 is temporary).
        source_repo.update_state.assert_not_called()

        # Terminal call should have failed=1 and next_eligible_at set.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert terminal_call[1]["next_eligible_at"] is not None

    @pytest.mark.asyncio
    async def test_captcha_triggers_backoff(self) -> None:
        """CAPTCHA signal in body triggers backoff."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service_with_backoff()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        sink.crawl_source_with_signals.return_value = CrawlSourceResult(
            postings=(),
            status_code=200,
            body_prefix="<html>Please verify you are human with this captcha</html>",
        )

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert terminal_call[1]["next_eligible_at"] is not None

    @pytest.mark.asyncio
    async def test_parse_drift_triggers_backoff(self) -> None:
        """Parser drift (expected fields missing, zero jobs) triggers backoff."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service_with_backoff()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        sink.crawl_source_with_signals.return_value = CrawlSourceResult(
            postings=(),
            status_code=200,
            body_prefix="<html>no jobs here</html>",
            expected_fields_missing=("title",),
        )

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert terminal_call[1]["next_eligible_at"] is not None

    @pytest.mark.asyncio
    async def test_terms_blocked_marks_source_blocked(self) -> None:
        """Policy terms_blocked decision marks source as BLOCKED."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id, terms_status=CrawlPolicyStatus.BLOCKED)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        deny_fn = MagicMock(
            return_value=CrawlPolicyDecision(
                decision=CrawlDecision.DENY_BLOCKED,
                reason="terms blocked",
            )
        )

        service, run_repo, plan_repo, source_repo, _sink = self._make_service_with_backoff(
            policy_fn=deny_fn
        )
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # Source should be marked BLOCKED.
        source_repo.update_state.assert_called_once()
        call_kwargs = source_repo.update_state.call_args
        assert call_kwargs[1]["state"] == CrawlSourceState.BLOCKED

    @pytest.mark.asyncio
    async def test_fetch_success_resets_backoff_state(self) -> None:
        """A successful fetch after failures resets the backoff state."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        service, run_repo, plan_repo, source_repo, sink = self._make_service_with_backoff()
        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        # Return a successful result with postings.
        sink.crawl_source_with_signals.return_value = _make_posting_result(str(source.id))

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.discovered == 1
        assert counters.failed == 0


class TestFailClosed:
    """Tests for fail-closed behavior on unknown dependencies (task 5.3)."""

    @pytest.mark.asyncio
    async def test_policy_raises_fails_closed(self) -> None:
        """If the policy callback raises, the source is skipped (fail-closed)."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        # Policy raises (e.g. DNS resolver down, terms DB unavailable).
        policy_fn = MagicMock(side_effect=RuntimeError("DNS resolver down"))

        run_repo = MagicMock()
        plan_repo = MagicMock()
        source_repo = MagicMock()
        sink = MagicMock()
        sink.crawl_source_with_signals = AsyncMock(return_value=_make_empty_signals_result())
        sink.ingest_posting = AsyncMock(
            return_value={"is_new_posting": True, "is_new_version": True}
        )
        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            policy_fn=policy_fn,
        )

        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # crawl_source_with_signals was NOT called (policy failed closed).
        sink.crawl_source_with_signals.assert_not_called()
        # The terminal call should have failed=1 in counters.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert counters.discovered == 0

    @pytest.mark.asyncio
    async def test_unavailable_dependency_fails_closed(self) -> None:
        """dependency_available=False in the backoff policy fails closed."""
        owner_id = uuid4()
        plan_id = uuid4()
        source = _make_source(owner_id=owner_id)
        plan = _make_plan(plan_id=plan_id, owner_id=owner_id, sources=(source.id,))
        run = _make_run(plan_version_id=plan_id)

        run_repo = MagicMock()
        plan_repo = MagicMock()
        source_repo = MagicMock()
        sink = MagicMock()
        sink.crawl_source_with_signals = AsyncMock(return_value=_make_empty_signals_result())
        sink.ingest_posting = AsyncMock(
            return_value={"is_new_posting": True, "is_new_version": True}
        )

        # Create a backoff policy that always says dependency unavailable.
        backoff = MagicMock(spec=BackoffPolicy)
        backoff.evaluate.return_value = BackoffDecision(
            should_stop=True,
            reason=BackoffReason.DEPENDENCY_UNAVAILABLE,
            next_eligible_at=datetime.now(tz=UTC) + timedelta(minutes=15),
        )
        backoff.record_outcome.return_value = BackoffState(
            consecutive_failures=1,
            next_eligible_at=datetime.now(tz=UTC) + timedelta(minutes=15),
        )

        service = CrawlExecutionService(
            run_repository=run_repo,
            plan_repository=plan_repo,
            source_repository=source_repo,
            sink=sink,
            backoff_policy=backoff,
        )

        run_repo.get_by_id.return_value = run
        plan_repo.get_by_id.return_value = plan
        source_repo.get_by_id.return_value = source

        running_run = _make_run(run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.RUNNING)
        succeeded_run = _make_run(
            run_id=run.id, plan_version_id=plan_id, state=CrawlRunState.SUCCEEDED
        )
        run_repo.update_terminal.side_effect = [running_run, succeeded_run]

        await service.execute(owner_id, run.id)

        # The terminal call should have failed=1 and next_eligible_at set.
        terminal_call = run_repo.update_terminal.call_args_list[1]
        counters = terminal_call[1]["counters"]
        assert counters.failed == 1
        assert terminal_call[1]["next_eligible_at"] is not None


class TestBackoffPolicyUnit:
    """Unit tests for the BackoffPolicy helper itself."""

    def test_403_returns_blocked(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(domain="example.com", status_code=403)
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.BLOCKED_403
        assert decision.next_eligible_at is not None

    def test_429_returns_rate_limited(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(domain="example.com", status_code=429)
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.RATE_LIMITED_429
        assert decision.next_eligible_at is not None

    def test_captcha_signal_detected(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com",
            status_code=200,
            body_prefix="<html>please verify you are human</html>",
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.CAPTCHA_OR_LOGIN_WALL

    def test_recaptcha_detected(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com",
            status_code=200,
            body_prefix='<script src="https://www.google.com/recaptcha/api.js"></script>',
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.CAPTCHA_OR_LOGIN_WALL

    def test_terms_blocked(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(domain="example.com", status_code=200, terms_status="blocked")
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.TERMS_BLOCKED

    def test_parse_drift_no_jobs(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com",
            status_code=200,
            jobs_found=0,
            expected_fields_missing=("title",),
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.PARSE_DRIFT

    def test_parse_drift_with_jobs_not_triggered(self) -> None:
        """Parse drift is not triggered when jobs are found."""
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com",
            status_code=200,
            jobs_found=5,
            expected_fields_missing=("title",),
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is False

    def test_repeated_failures(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com",
            status_code=200,
            consecutive_failures=3,
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.REPEATED_FAILURE

    def test_dependency_unavailable(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(
            domain="example.com", status_code=200, dependency_available=False
        )
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is True
        assert decision.reason == BackoffReason.DEPENDENCY_UNAVAILABLE

    def test_successful_outcome_no_backoff(self) -> None:
        policy = BackoffPolicy()
        outcome = SourceFetchOutcome(domain="example.com", status_code=200, jobs_found=10)
        decision = policy.evaluate(outcome, now=datetime(2025, 1, 1, tzinfo=UTC))
        assert decision.should_stop is False
        assert decision.reason is None
        assert decision.next_eligible_at is None

    def test_record_outcome_success_resets_state(self) -> None:
        policy = BackoffPolicy()
        state = BackoffState(consecutive_failures=5)
        decision = BackoffDecision(should_stop=False)
        new_state = policy.record_outcome(state, decision, success=True)
        assert new_state.consecutive_failures == 0
        assert new_state.next_eligible_at is None

    def test_record_outcome_failure_increments(self) -> None:
        policy = BackoffPolicy()
        state = BackoffState(consecutive_failures=2)
        now = datetime(2025, 1, 1, tzinfo=UTC)
        decision = BackoffDecision(
            should_stop=True,
            reason=BackoffReason.RATE_LIMITED_429,
            next_eligible_at=now + timedelta(minutes=30),
        )
        new_state = policy.record_outcome(state, decision, success=False)
        assert new_state.consecutive_failures == 3
        assert new_state.next_eligible_at == now + timedelta(minutes=30)
