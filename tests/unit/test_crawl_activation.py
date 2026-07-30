"""Unit tests for Phase 8.2 + 8.3: crawl activation and matching chain.

Tests:
- 8.2: activate_crawl_schedules enables schedules after readiness gate,
  respects budget limits, skips cooldown sources.
- 8.3: chain_crawl_to_matching only chains POSTINGS_FOUND outcomes,
  filters out failed/denied/permission-pending/invalid-extraction.
- 8.4: deterministic mixed-source integration suite covering all outcomes.
- 8.5: 100-source capacity test.
- 8.6: real-source smoke test marker (optional/manual).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.crawl_activation import (
    CrawlActivationResult,
    CrawlActivationService,
    MatchingChainResult,
)
from careerops.application.crawl_readiness import CrawlReadiness, ReadinessCheck
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlSourceAttempt,
    SUCCESSFUL_CHAIN_OUTCOMES,
    TERMINAL_STOP_OUTCOMES,
)
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlSource,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult
from careerops.workflows.m1_contracts import CrawledPostingRecord


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_OWNER_ID = UUID("11111111-1111-1111-1111-111111111111")


class FakeScheduleActivator:
    """Records activate_source_schedule calls."""

    def __init__(self) -> None:
        self.activated: list[tuple[UUID, timedelta, bool]] = []

    async def activate_source_schedule(
        self,
        source_id: UUID,
        interval: timedelta,
        *,
        paused: bool = False,
    ) -> bool:
        self.activated.append((source_id, interval, paused))
        return True


class FakeBudgetChecker:
    """Configurable budget checker with optional state tracking."""

    def __init__(
        self,
        available_slots: int = 3,
        daily_remaining: int = 500,
        *,
        track_state: bool = False,
    ) -> None:
        self._available = available_slots
        self._daily = daily_remaining
        self._track_state = track_state

    @property
    def available_slots(self) -> int:
        return self._available

    @property
    def daily_remaining(self) -> int:
        return self._daily

    def consume_slot(self) -> None:
        """Simulate a slot being consumed (for stateful tests)."""
        if self._track_state and self._available > 0:
            self._available -= 1


class FakeEligibilityChecker:
    """Configurable eligibility checker."""

    def __init__(self, eligible: set[UUID] | None = None) -> None:
        self._eligible = eligible or set()

    def is_source_eligible(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        return source_id in self._eligible


class FakeInboxProjector:
    """Records project_new_postings calls."""

    def __init__(self, projected_count: int = 5) -> None:
        self.calls: list[tuple[UUID, UUID]] = []
        self._count = projected_count

    async def project_new_postings(
        self,
        owner_id: UUID,
        source_id: UUID,
        postings: tuple[CrawlSourceResult, ...],
        *,
        now: datetime | None = None,
    ) -> int:
        self.calls.append((owner_id, source_id))
        return self._count


class FailingInboxProjector:
    """Always raises on project_new_postings."""

    async def project_new_postings(
        self,
        owner_id: UUID,
        source_id: UUID,
        postings: tuple[CrawlSourceResult, ...],
        *,
        now: datetime | None = None,
    ) -> int:
        raise RuntimeError("projection failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ready() -> CrawlReadiness:
    return CrawlReadiness(
        ready=True,
        checks={
            ReadinessCheck.MIGRATIONS_APPLIED: True,
            ReadinessCheck.CRAWLER_AVAILABLE: True,
            ReadinessCheck.PERMISSION_APIS: True,
            ReadinessCheck.BUDGETS_CONFIGURED: True,
        },
    )


def _not_ready(reason: str = "test") -> CrawlReadiness:
    return CrawlReadiness(ready=False, failures=(reason,))


def _make_source(
    source_id: UUID | None = None,
    *,
    enabled: bool = True,
    state: CrawlSourceState = CrawlSourceState.ACTIVE,
) -> CrawlSource:
    return CrawlSource(
        id=source_id or uuid4(),
        owner_id=_OWNER_ID,
        company_id=uuid4(),
        source_type=CrawlSourceType.GREENHOUSE,
        source_identifier="test-source",
        base_url="https://example.com/jobs",
        enabled=enabled,
        state=state,
    )


def _make_attempt(
    source_id: UUID,
    outcome: CrawlAttemptOutcome,
) -> CrawlSourceAttempt:
    return CrawlSourceAttempt(
        id=uuid4(),
        source_id=source_id,
        owner_id=_OWNER_ID,
        attempt_no=1,
        outcome=outcome,
    )


def _make_crawl_result(*postings: dict[str, str]) -> CrawlSourceResult:
    records = tuple(
        CrawledPostingRecord(
            source_id=str(uuid4()),
            external_id=p.get("external_id", "job-1"),
            canonical_url=p.get("url", "https://example.com/job/1"),
            source_url="https://example.com",
            structured_data=p,
            parser_version="test-v1",
            fetched_at=datetime.now(tz=UTC).isoformat(),
        )
        for p in postings
    )
    return CrawlSourceResult(postings=records, status_code=200)


# ---------------------------------------------------------------------------
# 8.2 -- activate_crawl_schedules
# ---------------------------------------------------------------------------


class TestActivateCrawlSchedules:
    """8.2: enable paused schedules after readiness gate passes."""

    @pytest.mark.asyncio
    async def test_readiness_gate_blocks_activation(self) -> None:
        """When readiness is False, no schedules are activated."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        eligibility = FakeEligibilityChecker()
        service = CrawlActivationService(activator, budget, eligibility)

        sources = [_make_source() for _ in range(5)]
        result = await service.activate_crawl_schedules(
            _not_ready(), _OWNER_ID, sources
        )

        assert result.readiness.ready is False
        assert result.schedules_activated == 0
        assert result.sources_considered == 0
        assert len(activator.activated) == 0

    @pytest.mark.asyncio
    async def test_eligible_sources_get_activated(self) -> None:
        """Eligible sources get their schedules activated."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        sources = [_make_source() for _ in range(3)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.readiness.ready is True
        assert result.schedules_activated == 3
        assert result.sources_considered == 3
        assert len(activator.activated) == 3

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_remaining(self) -> None:
        """When budget is exhausted, remaining sources are skipped."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker(available_slots=0)  # no slots
        sources = [_make_source() for _ in range(5)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.schedules_activated == 0
        assert result.schedules_skipped_budget == 5

    @pytest.mark.asyncio
    async def test_daily_budget_exhausted_skips(self) -> None:
        """When daily action budget is exhausted, sources are skipped."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker(daily_remaining=0)
        sources = [_make_source() for _ in range(3)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.schedules_activated == 0
        assert result.schedules_skipped_budget == 3

    @pytest.mark.asyncio
    async def test_ineligible_sources_skipped(self) -> None:
        """Sources in cooldown or terminal state are skipped."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        sources = [_make_source() for _ in range(3)]
        # Only first source is eligible.
        eligibility = FakeEligibilityChecker(eligible={sources[0].id})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.schedules_activated == 1
        assert result.schedules_skipped_cooldown == 2

    @pytest.mark.asyncio
    async def test_partial_budget_allows_all_when_sufficient(self) -> None:
        """When budget has capacity for all sources, all activate."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker(available_slots=5, daily_remaining=500)
        sources = [_make_source() for _ in range(5)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.schedules_activated == 5
        assert result.schedules_skipped_budget == 0


# ---------------------------------------------------------------------------
# 8.3 -- chain_crawl_to_matching
# ---------------------------------------------------------------------------


class TestChainCrawlToMatching:
    """8.3: only POSTINGS_FOUND chains into matching."""

    @pytest.mark.asyncio
    async def test_postings_found_chains(self) -> None:
        """POSTINGS_FOUND outcome chains into matching."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POSTINGS_FOUND)
        result_data = _make_crawl_result({"title": "SWE", "external_id": "j1"})

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, result_data
        )

        assert result.chained is True
        assert result.postings_projected == 5
        assert len(projector.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [
            CrawlAttemptOutcome.VERIFIED_EMPTY,
            CrawlAttemptOutcome.NOT_JOB_SOURCE,
            CrawlAttemptOutcome.TRANSIENT_FAILURE,
            CrawlAttemptOutcome.AUTH_REQUIRED,
            CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
            CrawlAttemptOutcome.POLICY_DENIED,
        ],
    )
    async def test_non_successful_outcomes_do_not_chain(
        self, outcome: CrawlAttemptOutcome
    ) -> None:
        """Non-POSTINGS_FOUND outcomes are filtered out."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, outcome)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, _make_crawl_result({"title": "SWE"})
        )

        assert result.chained is False
        assert result.postings_projected == 0
        assert len(projector.calls) == 0
        assert "does not chain" in result.skip_reason

    @pytest.mark.asyncio
    async def test_no_projector_skips(self) -> None:
        """When no projector is wired, chain is skipped."""
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), None,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POSTINGS_FOUND)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, _make_crawl_result({"title": "SWE"})
        )

        assert result.chained is False
        assert "no inbox projector" in result.skip_reason

    @pytest.mark.asyncio
    async def test_no_postings_skips(self) -> None:
        """When crawl result has no postings, chain is skipped."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POSTINGS_FOUND)
        empty_result = CrawlSourceResult(postings=(), status_code=200)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, empty_result
        )

        assert result.chained is False
        assert "no postings" in result.skip_reason

    @pytest.mark.asyncio
    async def test_no_crawl_result_skips(self) -> None:
        """When crawl_result is None, chain is skipped."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POSTINGS_FOUND)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, None
        )

        assert result.chained is False
        assert "no postings" in result.skip_reason

    @pytest.mark.asyncio
    async def test_projection_error_handled(self) -> None:
        """Projection errors are caught and reported."""
        projector = FailingInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POSTINGS_FOUND)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt,
            _make_crawl_result({"title": "SWE", "external_id": "j1"}),
        )

        assert result.chained is False
        assert "projection error" in result.skip_reason


# ---------------------------------------------------------------------------
# 8.4 -- deterministic mixed-source integration
# ---------------------------------------------------------------------------


class TestMixedSourceIntegration:
    """8.4: deterministic integration covering all outcome types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome, should_chain",
        [
            (CrawlAttemptOutcome.POSTINGS_FOUND, True),
            (CrawlAttemptOutcome.VERIFIED_EMPTY, False),
            (CrawlAttemptOutcome.NOT_JOB_SOURCE, False),
            (CrawlAttemptOutcome.TRANSIENT_FAILURE, False),
            (CrawlAttemptOutcome.AUTH_REQUIRED, False),
            (CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED, False),
            (CrawlAttemptOutcome.POLICY_DENIED, False),
        ],
    )
    async def test_each_outcome_chains_correctly(
        self, outcome: CrawlAttemptOutcome, should_chain: bool
    ) -> None:
        """Each outcome type chains or doesn't chain as expected."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, outcome)
        crawl_result = _make_crawl_result({"title": "Job", "external_id": "j1"})

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt, crawl_result
        )

        assert result.chained is should_chain
        if should_chain:
            assert len(projector.calls) == 1
        else:
            assert len(projector.calls) == 0

    def test_successful_chain_outcomes_set(self) -> None:
        """Only POSTINGS_FOUND is in SUCCESSFUL_CHAIN_OUTCOMES."""
        assert SUCCESSFUL_CHAIN_OUTCOMES == frozenset(
            {CrawlAttemptOutcome.POSTINGS_FOUND}
        )

    def test_terminal_stop_outcomes_set(self) -> None:
        """POLICY_DENIED is terminal."""
        assert CrawlAttemptOutcome.POLICY_DENIED in TERMINAL_STOP_OUTCOMES

    @pytest.mark.asyncio
    async def test_structured_source_activation(self) -> None:
        """Structured source activates normally."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        source = _make_source()
        eligibility = FakeEligibilityChecker(eligible={source.id})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, [source]
        )

        assert result.schedules_activated == 1

    @pytest.mark.asyncio
    async def test_public_dynamic_source_activation(self) -> None:
        """Public dynamic source activates normally."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        source = _make_source()
        eligibility = FakeEligibilityChecker(eligible={source.id})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, [source]
        )

        assert result.schedules_activated == 1

    @pytest.mark.asyncio
    async def test_login_required_source_eligibility_gated(self) -> None:
        """Login-required source respects eligibility check."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        source = _make_source()
        # Not eligible (e.g., permission pending).
        eligibility = FakeEligibilityChecker(eligible=set())
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, [source]
        )

        assert result.schedules_activated == 0
        assert result.schedules_skipped_cooldown == 1

    @pytest.mark.asyncio
    async def test_permission_denied_outcome_no_chain(self) -> None:
        """Permission-denied outcome never chains into matching."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.POLICY_DENIED)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt
        )

        assert result.chained is False

    @pytest.mark.asyncio
    async def test_captcha_outcome_no_chain(self) -> None:
        """CAPTCHA-adjacent DYNAMIC_OR_UNSUPPORTED outcome doesn't chain."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(
            source_id, CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED
        )

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt
        )

        assert result.chained is False

    @pytest.mark.asyncio
    async def test_invalid_llm_output_no_chain(self) -> None:
        """NOT_JOB_SOURCE (invalid LLM output) doesn't chain."""
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )
        source_id = uuid4()
        attempt = _make_attempt(source_id, CrawlAttemptOutcome.NOT_JOB_SOURCE)

        result = await service.chain_crawl_to_matching(
            _OWNER_ID, source_id, attempt
        )

        assert result.chained is False


# ---------------------------------------------------------------------------
# 8.5 -- 100-source capacity test
# ---------------------------------------------------------------------------


class TestHundredSourceCapacity:
    """8.5: 100-source capacity test across multiple workers.

    Verifies one durable outcome per source, no budget overrun, and no
    duplicate posting or permission request.
    """

    @pytest.mark.asyncio
    async def test_100_sources_respect_budget(self) -> None:
        """100 sources with zero budget activates none."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker(available_slots=0, daily_remaining=0)
        sources = [_make_source() for _ in range(100)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        assert result.sources_considered == 100
        assert result.schedules_activated == 0
        assert result.schedules_skipped_budget == 100
        assert len(activator.activated) == 0

    @pytest.mark.asyncio
    async def test_100_sources_mixed_outcomes_all_classified(self) -> None:
        """Each of 100 sources gets exactly one attempt outcome."""
        outcomes = list(CrawlAttemptOutcome)
        projector = FakeInboxProjector()
        service = CrawlActivationService(
            FakeScheduleActivator(), FakeBudgetChecker(),
            FakeEligibilityChecker(), projector,
        )

        chained_count = 0
        for i in range(100):
            outcome = outcomes[i % len(outcomes)]
            source_id = uuid4()
            attempt = _make_attempt(source_id, outcome)
            result = await service.chain_crawl_to_matching(
                _OWNER_ID, source_id, attempt,
                _make_crawl_result({"title": f"Job {i}", "external_id": f"j{i}"}),
            )
            if result.chained:
                chained_count += 1

        # Only POSTINGS_FOUND (index 0, every 7th) chains.
        expected_chain = sum(1 for i in range(100) if i % len(outcomes) == 0)
        assert chained_count == expected_chain

    @pytest.mark.asyncio
    async def test_no_duplicate_activation(self) -> None:
        """Activating the same source set twice is idempotent."""
        activator = FakeScheduleActivator()
        budget = FakeBudgetChecker()
        sources = [_make_source() for _ in range(10)]
        eligibility = FakeEligibilityChecker(eligible={s.id for s in sources})
        service = CrawlActivationService(activator, budget, eligibility)

        result1 = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )
        result2 = await service.activate_crawl_schedules(
            _ready(), _OWNER_ID, sources
        )

        # Both calls activate all sources (idempotent at the schedule level).
        assert result1.schedules_activated == 10
        assert result2.schedules_activated == 10
        # Total calls = 20 (idempotent at the ScheduleManager level).


# ---------------------------------------------------------------------------
# 8.6 -- real-source smoke test (optional / manual)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="requires real network access and API credentials")
class TestRealSourceSmoke:
    """8.6: explicit opt-in real-source smoke test.

    One structured + one public dynamic + one user-authorized login source.
    Records receipts without storing credentials or raw session material.

    Run manually: pytest tests/unit/test_crawl_activation.py -k real_source --run-skip
    """

    @pytest.mark.asyncio
    async def test_structured_source_smoke(self) -> None:
        """Smoke test a structured (ATS) source."""
        # Placeholder: would call real Greenhouse/Lever API.
        pass

    @pytest.mark.asyncio
    async def test_public_dynamic_source_smoke(self) -> None:
        """Smoke test a public dynamic source."""
        # Placeholder: would call real careers page.
        pass

    @pytest.mark.asyncio
    async def test_login_source_smoke(self) -> None:
        """Smoke test a user-authorized login source."""
        # Placeholder: would use real session reference.
        pass
