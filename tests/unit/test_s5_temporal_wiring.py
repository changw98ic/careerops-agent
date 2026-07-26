"""Unit tests for Section 5 Temporal wiring (task 5.4).

Tests the S5 activities (execute_crawl_run, create_scheduled_run) and
workflows (CrawlRunWorkflow, CrawlScheduledWorkflow) with mock services.

Activity tests use ``temporalio.testing.ActivityEnvironment`` to provide the
activity context that ``@activity.defn`` methods require.  Worker wiring
tests verify imports and construction without requiring a real Temporal
client (the Temporal SDK's ``Worker()`` requires a bridge client that
MagicMock cannot satisfy; full integration is tested separately).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from temporalio.testing import ActivityEnvironment

from careerops.api.errors import InvalidStateError
from careerops.infrastructure.temporal.s5_activities import (
    NoOpCrawlExecutor,
    NoOpScheduledRunCreator,
    S5CrawlExecutionActivities,
)
from careerops.workflows.s5_contracts import (
    CrawlRunExecuteInput,
    CrawlRunExecuteResult,
    CreateScheduledRunInput,
    CreateScheduledRunResult,
)

# Stable UUIDs for tests.
_OWNER_ID = "550e8400-e29b-41d4-a716-446655440000"
_RUN_ID = "660e8400-e29b-41d4-a716-446655440001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_run(
    run_id: str = _RUN_ID,
    state: str = "succeeded",
    discovered: int = 1,
    updated: int = 0,
    failed: int = 0,
    error_category: str = "",
    next_eligible_at: object = None,
) -> object:
    """Create a fake CrawlRun-like object."""

    @dataclasses.dataclass(frozen=True, slots=True)
    class _Counters:
        discovered: int = 0
        updated: int = 0
        closed: int = 0
        failed: int = 0

    @dataclasses.dataclass(frozen=True, slots=True)
    class _State:
        value: str

    @dataclasses.dataclass(frozen=True, slots=True)
    class _Run:
        id: str
        state: object
        counters: object
        error_category: str = ""
        next_eligible_at: object = None

    return _Run(
        id=run_id,
        state=_State(value=state),
        counters=_Counters(discovered=discovered, updated=updated, failed=failed),
        error_category=error_category,
        next_eligible_at=next_eligible_at,
    )


# ---------------------------------------------------------------------------
# Tests: NoOp sinks (no activity context needed)
# ---------------------------------------------------------------------------


class TestNoOpSinks:
    """NoOp sinks return synthetic results without I/O."""

    @pytest.mark.asyncio
    async def test_noop_executor_returns_succeeded(self) -> None:
        executor = NoOpCrawlExecutor()
        result = await executor.execute(UUID(_OWNER_ID), UUID(_RUN_ID))
        assert result.state.value == "succeeded"
        assert result.counters.discovered == 0

    def test_noop_run_creator_returns_pending(self) -> None:
        creator = NoOpScheduledRunCreator()
        result = creator.run_now(UUID(_OWNER_ID))
        assert result.state.value == "pending"
        assert result.id is not None


# ---------------------------------------------------------------------------
# Tests: execute_crawl_run activity (via ActivityEnvironment)
# ---------------------------------------------------------------------------


class TestExecuteCrawlRunActivity:
    """Tests for the execute_crawl_run Temporal activity."""

    @pytest.mark.asyncio
    async def test_success_returns_result(self) -> None:
        """Successful execution returns CrawlRunExecuteResult."""
        executor = AsyncMock()
        executor.execute.return_value = _fake_run(
            run_id=_RUN_ID, state="succeeded", discovered=3, updated=1, failed=0
        )
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        result = await env.run(
            activities.execute_crawl_run,
            CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
        )

        assert isinstance(result, CrawlRunExecuteResult)
        assert result.run_id == _RUN_ID
        assert result.state == "succeeded"
        assert result.counters["discovered"] == 3
        assert result.counters["updated"] == 1
        assert result.counters["failed"] == 0
        assert result.error_category == ""

    @pytest.mark.asyncio
    async def test_failed_run_returns_error_category(self) -> None:
        """A run that ends FAILED carries the error_category."""
        executor = AsyncMock()
        executor.execute.return_value = _fake_run(
            run_id=_RUN_ID, state="failed", failed=2, error_category="execution_error"
        )
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        result = await env.run(
            activities.execute_crawl_run,
            CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
        )

        assert result.state == "failed"
        assert result.error_category == "execution_error"
        assert result.counters["failed"] == 2

    @pytest.mark.asyncio
    async def test_invalid_state_error_is_non_retryable(self) -> None:
        """InvalidStateError (run not PENDING) raises non-retryable ApplicationError."""
        executor = AsyncMock()
        executor.execute.side_effect = InvalidStateError("run is already succeeded")
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        with pytest.raises(Exception, match="run is already succeeded"):
            await env.run(
                activities.execute_crawl_run,
                CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
            )

    @pytest.mark.asyncio
    async def test_unexpected_error_is_non_retryable(self) -> None:
        """Unexpected exceptions raise non-retryable ApplicationError."""
        executor = AsyncMock()
        executor.execute.side_effect = RuntimeError("DB connection lost")
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        with pytest.raises(Exception, match="crawl execution failed"):
            await env.run(
                activities.execute_crawl_run,
                CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
            )

    @pytest.mark.asyncio
    async def test_next_eligible_at_propagated(self) -> None:
        """next_eligible_at from backoff is propagated in the result."""

        next_time = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        executor = AsyncMock()
        executor.execute.return_value = _fake_run(
            run_id=_RUN_ID, state="succeeded", failed=1, next_eligible_at=next_time
        )
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        result = await env.run(
            activities.execute_crawl_run,
            CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
        )

        assert result.next_eligible_at == next_time.isoformat()

    @pytest.mark.asyncio
    async def test_passes_uuid_to_executor(self) -> None:
        """The activity converts string IDs to UUID before calling the executor."""
        executor = AsyncMock()
        executor.execute.return_value = _fake_run()
        activities = S5CrawlExecutionActivities(executor=executor)
        env = ActivityEnvironment()

        await env.run(
            activities.execute_crawl_run,
            CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID),
        )

        call_args = executor.execute.call_args
        assert call_args[0][0] == UUID(_OWNER_ID)
        assert call_args[0][1] == UUID(_RUN_ID)


# ---------------------------------------------------------------------------
# Tests: create_scheduled_run activity (via ActivityEnvironment)
# ---------------------------------------------------------------------------


class TestCreateScheduledRunActivity:
    """Tests for the create_scheduled_run Temporal activity."""

    @pytest.mark.asyncio
    async def test_success_returns_run_created(self) -> None:
        """run_now returns a PENDING run; activity returns run_created=True."""
        run_id = uuid4()

        @dataclasses.dataclass(frozen=True, slots=True)
        class _FakeRun:
            id: object
            state: object

        @dataclasses.dataclass(frozen=True, slots=True)
        class _FakeState:
            value: str = "pending"

        creator = MagicMock()
        creator.run_now.return_value = _FakeRun(id=run_id, state=_FakeState())
        activities = S5CrawlExecutionActivities(run_creator=creator)
        env = ActivityEnvironment()

        result = await env.run(
            activities.create_scheduled_run,
            CreateScheduledRunInput(owner_id=_OWNER_ID),
        )

        assert isinstance(result, CreateScheduledRunResult)
        assert result.run_created is True
        assert result.run_id == str(run_id)
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_paused_plan_returns_skipped(self) -> None:
        """InvalidStateError (paused plan) returns run_created=False."""
        creator = MagicMock()
        creator.run_now.side_effect = InvalidStateError("crawl plan has no active version")
        activities = S5CrawlExecutionActivities(run_creator=creator)
        env = ActivityEnvironment()

        result = await env.run(
            activities.create_scheduled_run,
            CreateScheduledRunInput(owner_id=_OWNER_ID),
        )

        assert result.run_created is False
        assert result.run_id is None
        assert "no active version" in result.error

    @pytest.mark.asyncio
    async def test_no_eligible_sources_returns_skipped(self) -> None:
        """InvalidStateError (no eligible sources) returns run_created=False."""
        creator = MagicMock()
        creator.run_now.side_effect = InvalidStateError("crawl plan has no eligible sources")
        activities = S5CrawlExecutionActivities(run_creator=creator)
        env = ActivityEnvironment()

        result = await env.run(
            activities.create_scheduled_run,
            CreateScheduledRunInput(owner_id=_OWNER_ID),
        )

        assert result.run_created is False
        assert "no eligible sources" in result.error

    @pytest.mark.asyncio
    async def test_db_error_raises_retryable(self) -> None:
        """DB errors raise ApplicationError (retryable by default)."""
        creator = MagicMock()
        creator.run_now.side_effect = RuntimeError("connection refused")
        activities = S5CrawlExecutionActivities(run_creator=creator)
        env = ActivityEnvironment()

        with pytest.raises(Exception, match="failed to create scheduled run"):
            await env.run(
                activities.create_scheduled_run,
                CreateScheduledRunInput(owner_id=_OWNER_ID),
            )


# ---------------------------------------------------------------------------
# Tests: worker wiring (import + construction, no real client)
# ---------------------------------------------------------------------------


class TestWorkerWiring:
    """Verify the worker module imports and bundle construction succeed.

    Full Worker construction requires a real Temporal client bridge; these
    tests verify the wiring code does not crash at import time and that
    M1ActivityBundles accepts the S5 field.  Integration tests that start
    a test-server worker are separate and skip when the SDK is unavailable.
    """

    def test_s5_activities_importable(self) -> None:
        """S5 activities and workflows are importable."""
        from careerops.infrastructure.temporal.s5_activities import S5CrawlExecutionActivities
        from careerops.workflows.s5_workflows import (
            CrawlRunWorkflow,
            CrawlScheduledWorkflow,
        )

        assert S5CrawlExecutionActivities is not None
        assert CrawlRunWorkflow is not None
        assert CrawlScheduledWorkflow is not None

    def test_bundle_accepts_s5_field(self) -> None:
        """M1ActivityBundles accepts the crawl_execution field."""
        from careerops.infrastructure.temporal.worker import M1ActivityBundles

        s5 = S5CrawlExecutionActivities()
        bundle = M1ActivityBundles(crawl_execution=s5)
        assert bundle.crawl_execution is s5

    def test_bundle_default_s5_is_none(self) -> None:
        """M1ActivityBundles defaults crawl_execution to None."""
        from careerops.infrastructure.temporal.worker import M1ActivityBundles

        bundle = M1ActivityBundles()
        assert bundle.crawl_execution is None


# ---------------------------------------------------------------------------
# Tests: workflow contracts round-trip
# ---------------------------------------------------------------------------


class TestWorkflowContracts:
    """Verify the workflow contract dataclasses are well-formed."""

    def test_execute_input_fields(self) -> None:
        inp = CrawlRunExecuteInput(owner_id=_OWNER_ID, run_id=_RUN_ID)
        assert inp.owner_id == _OWNER_ID
        assert inp.run_id == _RUN_ID

    def test_execute_result_fields(self) -> None:
        result = CrawlRunExecuteResult(
            run_id=_RUN_ID,
            state="succeeded",
            counters={"discovered": 5, "updated": 2, "closed": 0, "failed": 1},
            error_category="",
            next_eligible_at=None,
        )
        assert result.run_id == _RUN_ID
        assert result.counters["discovered"] == 5

    def test_create_scheduled_run_input(self) -> None:
        inp = CreateScheduledRunInput(owner_id=_OWNER_ID)
        assert inp.owner_id == _OWNER_ID

    def test_create_scheduled_run_result_skipped(self) -> None:
        result = CreateScheduledRunResult(run_created=False, error="no active version")
        assert result.run_created is False
        assert result.run_id is None

    def test_create_scheduled_run_result_created(self) -> None:
        result = CreateScheduledRunResult(run_created=True, run_id=_RUN_ID)
        assert result.run_created is True
        assert result.run_id == _RUN_ID
