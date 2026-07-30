"""Unit tests for Phase 5.4-5.8: source discovery, queue, classifier, retry.

Tests:
- 5.4: Source discovery normalizer + dedup into ``job_sources`` registry.
- 5.5: Tier 1 source queue from registry, one attempt per source per run.
- 5.6: Outcome classifier — 7-way classification with explicit-login-evidence
       constraint (empty/403/captcha alone CANNOT become AUTH_REQUIRED).
- 5.7: Bounded retry / cooldown — next_eligible_at calculation, POLICY_DENIED
       terminal stop.
- 5.8: 100-source queue fixture + mixed outcomes, one attempt per source,
       no duplicates, durable outcomes.

Uses in-memory fakes for repositories (no database required).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.outcome_classifier import ClassificationInput, classify_outcome
from careerops.application.source_discovery import (
    DiscoveredSource,
    discover_and_register,
    normalize_source_identifier,
)
from careerops.application.source_queue import SourceQueueService
from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlAttemptRepository,
    CrawlSourceAttempt,
    TERMINAL_STOP_OUTCOMES,
)
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult
from careerops.workflows.m1_contracts import CrawledPostingRecord


# ---------------------------------------------------------------------------
# In-memory repository fakes
# ---------------------------------------------------------------------------


class InMemorySourceRepo:
    """In-memory ``CrawlSourceRepository`` for unit tests."""

    def __init__(self) -> None:
        self._sources: dict[UUID, CrawlSource] = {}

    def save(self, source: CrawlSource) -> CrawlSource:
        self._sources[source.id] = source
        return source

    def get_by_id(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        src = self._sources.get(source_id)
        if src is None:
            from careerops.api.errors import NotFoundError

            raise NotFoundError("source not found")
        return src

    def list_for(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        return list(self._sources.values())[:limit]

    def get_by_identity(
        self,
        owner_id: UUID,
        company_id: UUID,
        source_type: CrawlSourceType,
        source_identifier: str,
    ) -> CrawlSource | None:
        for src in self._sources.values():
            if (
                src.company_id == company_id
                and src.source_type == source_type
                and src.source_identifier == source_identifier
            ):
                return src
        return None

    def upsert_by_identity(self, source: CrawlSource) -> CrawlSource:
        existing = self.get_by_identity(
            source.owner_id,
            source.company_id,
            source.source_type,
            source.source_identifier,
        )
        if existing is not None:
            # Refresh mutable discovery fields; preserve lifecycle state.
            refreshed = CrawlSource(
                id=existing.id,
                owner_id=existing.owner_id,
                company_id=existing.company_id,
                source_type=existing.source_type,
                source_identifier=existing.source_identifier,
                base_url=source.base_url,
                executor_mode=source.executor_mode,
                state=existing.state,
                trust_status=existing.trust_status,
                terms_status=existing.terms_status,
                robots_status=existing.robots_status,
                adapter_version=source.adapter_version,
                enabled=existing.enabled,
                verified_at=existing.verified_at,
                last_discovery_at=source.last_discovery_at,
                last_run_at=existing.last_run_at,
                last_run_metadata=source.last_run_metadata,
                created_at=existing.created_at,
                updated_at=existing.updated_at,
            )
            self._sources[existing.id] = refreshed
            return refreshed
        self._sources[source.id] = source
        return source

    def update_state(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        state: CrawlSourceState | None = None,
        enabled: bool | None = None,
        now: datetime | None = None,
    ) -> CrawlSource:
        src = self.get_by_id(owner_id, source_id)
        updated = CrawlSource(
            id=src.id,
            owner_id=src.owner_id,
            company_id=src.company_id,
            source_type=src.source_type,
            source_identifier=src.source_identifier,
            base_url=src.base_url,
            executor_mode=src.executor_mode,
            state=state or src.state,
            trust_status=src.trust_status,
            terms_status=src.terms_status,
            robots_status=src.robots_status,
            adapter_version=src.adapter_version,
            enabled=enabled if enabled is not None else src.enabled,
            verified_at=src.verified_at,
            last_discovery_at=src.last_discovery_at,
            last_run_at=src.last_run_at,
            last_run_metadata=src.last_run_metadata,
            created_at=src.created_at,
            updated_at=now or src.updated_at,
        )
        self._sources[source_id] = updated
        return updated

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        self._sources.pop(source_id, None)


class InMemoryAttemptRepo:
    """In-memory ``CrawlAttemptRepository`` for unit tests."""

    def __init__(self) -> None:
        self._attempts: dict[UUID, CrawlSourceAttempt] = {}
        # Per-source attempt_no tracking.
        self._attempt_nos: dict[UUID, int] = {}

    def record(self, owner_id: UUID, attempt: CrawlSourceAttempt) -> CrawlSourceAttempt:
        self._attempts[attempt.id] = attempt
        self._attempt_nos[attempt.source_id] = max(
            self._attempt_nos.get(attempt.source_id, 0), attempt.attempt_no
        )
        return attempt

    def get_by_id(self, owner_id: UUID, attempt_id: UUID) -> CrawlSourceAttempt:
        att = self._attempts.get(attempt_id)
        if att is None:
            from careerops.api.errors import NotFoundError

            raise NotFoundError("attempt not found")
        return att

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourceAttempt]:
        attempts = [a for a in self._attempts.values() if a.source_id == source_id]
        attempts.sort(key=lambda a: a.attempt_no, reverse=True)
        return attempts[:limit]

    def latest_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourceAttempt | None:
        attempts = self.list_for_source(owner_id, source_id, limit=1)
        return attempts[0] if attempts else None

    def next_attempt_no(self, owner_id: UUID, source_id: UUID) -> int:
        return self._attempt_nos.get(source_id, 0) + 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OWNER_ID = UUID("11111111-1111-1111-1111-111111111111")
_COMPANY_ID = UUID("22222222-2222-2222-2222-222222222222")


def _make_source(
    *,
    source_id: UUID | None = None,
    company_id: UUID | None = None,
    source_type: CrawlSourceType = CrawlSourceType.GREENHOUSE,
    source_identifier: str = "example",
    base_url: str = "https://boards.greenhouse.io/example",
    enabled: bool = True,
    state: CrawlSourceState = CrawlSourceState.ACTIVE,
    executor_mode: CrawlExecutorMode = CrawlExecutorMode.HTTP,
) -> CrawlSource:
    return CrawlSource(
        id=source_id or uuid4(),
        owner_id=_OWNER_ID,
        company_id=company_id or _COMPANY_ID,
        source_type=source_type,
        source_identifier=source_identifier,
        base_url=base_url,
        executor_mode=executor_mode,
        state=state,
        trust_status=CrawlPolicyStatus.ALLOWED,
        terms_status=CrawlPolicyStatus.ALLOWED,
        robots_status=CrawlPolicyStatus.ALLOWED,
        enabled=enabled,
    )


def _make_posting(external_id: str = "job-1") -> CrawledPostingRecord:
    return CrawledPostingRecord(
        source_id=str(uuid4()),
        external_id=external_id,
        canonical_url="https://example.com/jobs/1",
        source_url="https://example.com/careers",
        structured_data={"title": "Engineer", "location": "Remote"},
        parser_version="test",
        fetched_at=datetime.now(tz=UTC).isoformat(),
    )


def _empty_result(
    *,
    status_code: int = 200,
    body_prefix: str = "",
    expected_fields_missing: tuple[str, ...] = (),
) -> CrawlSourceResult:
    return CrawlSourceResult(
        postings=(),
        status_code=status_code,
        body_prefix=body_prefix,
        expected_fields_missing=expected_fields_missing,
    )


def _result_with_postings(
    count: int = 1,
    *,
    status_code: int = 200,
) -> CrawlSourceResult:
    return CrawlSourceResult(
        postings=tuple(_make_posting(f"job-{i}") for i in range(count)),
        status_code=status_code,
    )


# ===========================================================================
# 5.4 — Source discovery
# ===========================================================================


class TestNormalizeSourceIdentifier:
    def test_strips_query_and_fragment(self) -> None:
        result = normalize_source_identifier(
            "https://acme.com/careers?lang=en#section"
        )
        assert result == "acme.com/careers"

    def test_strips_trailing_slash(self) -> None:
        result = normalize_source_identifier("https://acme.com/careers/")
        assert result == "acme.com/careers"

    def test_lowercases_hostname(self) -> None:
        result = normalize_source_identifier("https://ACME.COM/jobs")
        assert result == "acme.com/jobs"

    def test_different_queries_same_identifier(self) -> None:
        a = normalize_source_identifier("https://acme.com/careers?lang=en")
        b = normalize_source_identifier("https://acme.com/careers?lang=zh")
        assert a == b

    def test_different_paths_different_identifier(self) -> None:
        a = normalize_source_identifier("https://acme.com/careers")
        b = normalize_source_identifier("https://acme.com/jobs")
        assert a != b


class TestDiscoverAndRegister:
    def test_registers_new_source(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://boards.greenhouse.io/acme",
            source_type="greenhouse",
        )
        source = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        assert source.source_type == CrawlSourceType.GREENHOUSE
        assert source.state == CrawlSourceState.PENDING_REVIEW
        assert source.enabled is False
        assert source.executor_mode == CrawlExecutorMode.HTTP
        assert source.last_discovery_at is not None

    def test_dedup_on_same_identity(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://boards.greenhouse.io/acme",
            source_type="greenhouse",
        )
        s1 = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        # Second discovery with different base_url path but same identity.
        discovered2 = DiscoveredSource(
            base_url="https://boards.greenhouse.io/acme?ref=home",
            source_type="greenhouse",
        )
        s2 = discover_and_register(
            repo, discovered2, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        # Same underlying row (id preserved).
        assert s1.id == s2.id
        # Mutable fields refreshed.
        assert s2.last_discovery_at is not None

    def test_preserves_lifecycle_state_on_re_discovery(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://boards.greenhouse.io/acme",
            source_type="greenhouse",
        )
        s1 = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        # Simulate user activating the source.
        repo.update_state(_OWNER_ID, s1.id, state=CrawlSourceState.ACTIVE, enabled=True)
        # Re-discover.
        s2 = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        # State preserved — discovery does NOT clobber lifecycle.
        assert s2.state == CrawlSourceState.ACTIVE
        assert s2.enabled is True

    def test_rejects_unsupported_source_type(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://example.com/jobs",
            source_type="unsupported_type",
        )
        with pytest.raises(ValueError, match="unsupported source type"):
            discover_and_register(
                repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
            )

    def test_official_type_defaults_to_ego_executor(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://careers.tencent.com/search.html",
            source_type="official",
        )
        source = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        assert source.executor_mode == CrawlExecutorMode.EGO

    def test_custom_source_identifier(self) -> None:
        repo = InMemorySourceRepo()
        discovered = DiscoveredSource(
            base_url="https://boards.greenhouse.io/acme",
            source_type="greenhouse",
            source_identifier="custom-id",
        )
        source = discover_and_register(
            repo, discovered, owner_id=_OWNER_ID, company_id=_COMPANY_ID
        )
        assert source.source_identifier == "custom-id"


# ===========================================================================
# 5.5 — Source queue
# ===========================================================================


class TestSourceQueue:
    def _setup_queue(
        self, sources: list[CrawlSource]
    ) -> tuple[SourceQueueService, InMemorySourceRepo, InMemoryAttemptRepo]:
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)
        return service, source_repo, attempt_repo

    def test_select_sources_returns_enabled_active_only(self) -> None:
        s1 = _make_source(enabled=True, state=CrawlSourceState.ACTIVE)
        s2 = _make_source(enabled=False, state=CrawlSourceState.ACTIVE)
        s3 = _make_source(enabled=True, state=CrawlSourceState.PAUSED)
        s4 = _make_source(enabled=True, state=CrawlSourceState.ACTIVE)
        service, _, _ = self._setup_queue([s1, s2, s3, s4])
        selected = service.select_sources(_OWNER_ID)
        ids = {s.id for s in selected}
        assert s1.id in ids
        assert s4.id in ids
        assert s2.id not in ids  # disabled
        assert s3.id not in ids  # paused

    def test_select_sources_starving_first(self) -> None:
        now = datetime.now(tz=UTC)
        s_never = _make_source()  # last_run_at = None
        s_old = dataclasses.replace(
            _make_source(), last_run_at=now - timedelta(hours=5)
        )
        s_recent = dataclasses.replace(
            _make_source(), last_run_at=now - timedelta(minutes=5)
        )
        service, _, _ = self._setup_queue([s_recent, s_old, s_never])
        selected = service.select_sources(_OWNER_ID)
        assert selected[0].id == s_never.id
        assert selected[1].id == s_old.id
        assert selected[2].id == s_recent.id

    def test_one_attempt_per_source(self) -> None:
        source = _make_source()
        service, _, attempt_repo = self._setup_queue([source])
        r1 = service.record_attempt(
            _OWNER_ID, source.id, _result_with_postings(1)
        )
        r2 = service.record_attempt(
            _OWNER_ID, source.id, _empty_result()
        )
        assert r1.attempt_no == 1
        assert r2.attempt_no == 2
        assert r1.source_id == r2.source_id
        # Both are persisted.
        assert len(attempt_repo.list_for_source(_OWNER_ID, source.id)) == 2

    def test_attempt_no_monotonic_across_calls(self) -> None:
        source = _make_source()
        service, _, _ = self._setup_queue([source])
        nos = []
        for _ in range(10):
            att = service.record_attempt(
                _OWNER_ID, source.id, _empty_result()
            )
            nos.append(att.attempt_no)
        assert nos == list(range(1, 11))


# ===========================================================================
# 5.6 — Outcome classifier
# ===========================================================================


class TestOutcomeClassifier:
    def test_postings_found(self) -> None:
        inp = ClassificationInput(result=_result_with_postings(3))
        assert classify_outcome(inp) == CrawlAttemptOutcome.POSTINGS_FOUND

    def test_verified_empty_200_no_signals(self) -> None:
        inp = ClassificationInput(result=_empty_result(status_code=200))
        assert classify_outcome(inp) == CrawlAttemptOutcome.VERIFIED_EMPTY

    def test_403_with_login_evidence_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(
                status_code=403,
                body_prefix="Please log in to continue",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_403_without_login_evidence_is_dynamic(self) -> None:
        """403 alone — no login wall text — is NOT auth_required."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=403,
                body_prefix="Forbidden",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_captcha_with_login_evidence_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200,
                body_prefix="captcha detected. Sign in to continue.",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_captcha_without_login_evidence_is_dynamic(self) -> None:
        """CAPTCHA alone — no login wall text — is NOT auth_required."""
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200,
                body_prefix="Please verify you are human with captcha",
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_no_adapter_is_not_job_source(self) -> None:
        inp = ClassificationInput(result=_empty_result(), has_adapter=False)
        assert classify_outcome(inp) == CrawlAttemptOutcome.NOT_JOB_SOURCE

    def test_transport_error_is_transient(self) -> None:
        inp = ClassificationInput(result=_empty_result(), transport_error=True)
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_timed_out_is_transient(self) -> None:
        inp = ClassificationInput(result=_empty_result(), timed_out=True)
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_policy_denied_is_terminal(self) -> None:
        inp = ClassificationInput(
            result=_result_with_postings(5),
            policy_decision=CrawlDecision.DENY_BLOCKED,
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.POLICY_DENIED

    def test_5xx_is_transient(self) -> None:
        inp = ClassificationInput(result=_empty_result(status_code=503))
        assert classify_outcome(inp) == CrawlAttemptOutcome.TRANSIENT_FAILURE

    def test_4xx_other_is_dynamic(self) -> None:
        inp = ClassificationInput(result=_empty_result(status_code=404))
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_parse_drift_is_dynamic(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(
                status_code=200,
                expected_fields_missing=("title",),
            )
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_status_0_is_not_job_source(self) -> None:
        inp = ClassificationInput(result=_empty_result(status_code=0))
        assert classify_outcome(inp) == CrawlAttemptOutcome.NOT_JOB_SOURCE

    def test_postings_found_overrides_403(self) -> None:
        """Postings found takes priority over 403."""
        inp = ClassificationInput(
            result=_result_with_postings(2, status_code=403),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.POSTINGS_FOUND

    def test_policy_denied_overrides_postings(self) -> None:
        """Policy denied takes priority even with postings."""
        inp = ClassificationInput(
            result=_result_with_postings(5),
            policy_decision=CrawlDecision.DENY_BLOCKED,
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.POLICY_DENIED


# ===========================================================================
# 5.7 — Bounded retry / cooldown
# ===========================================================================


class TestBoundedRetry:
    def _setup(self) -> tuple[SourceQueueService, InMemorySourceRepo, InMemoryAttemptRepo]:
        source_repo = InMemorySourceRepo()
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)
        return service, source_repo, attempt_repo

    def test_record_attempt_sets_next_eligible_at(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        now = datetime.now(tz=UTC)
        att = service.record_attempt(
            _OWNER_ID, source.id, _empty_result(status_code=503), now=now
        )
        assert att.next_eligible_at is not None
        assert att.next_eligible_at > now

    def test_policy_denied_no_next_eligible(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        att = service.record_attempt(
            _OWNER_ID,
            source.id,
            _empty_result(),
            policy_decision=CrawlDecision.DENY_BLOCKED,
        )
        assert att.outcome == CrawlAttemptOutcome.POLICY_DENIED
        assert att.next_eligible_at is None

    def test_source_not_eligible_during_cooldown(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        now = datetime.now(tz=UTC)
        # Record a transient failure.
        service.record_attempt(
            _OWNER_ID, source.id, _empty_result(status_code=503), now=now
        )
        # Still in cooldown — not eligible.
        assert service.is_source_eligible(_OWNER_ID, source.id, now=now) is False

    def test_source_eligible_after_cooldown(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        now = datetime.now(tz=UTC)
        # Record a transient failure.
        service.record_attempt(
            _OWNER_ID, source.id, _empty_result(status_code=503), now=now
        )
        # After 1 hour, should be eligible again (default transient cooldown = 30min).
        later = now + timedelta(hours=1)
        assert service.is_source_eligible(_OWNER_ID, source.id, now=later) is True

    def test_policy_denied_stays_ineligible(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        now = datetime.now(tz=UTC)
        service.record_attempt(
            _OWNER_ID,
            source.id,
            _empty_result(),
            policy_decision=CrawlDecision.DENY_BLOCKED,
            now=now,
        )
        # Even after a long time, POLICY_DENIED stays ineligible.
        later = now + timedelta(days=30)
        assert service.is_source_eligible(_OWNER_ID, source.id, now=later) is False

    def test_postings_found_short_cooldown(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source()
        repo.save(source)
        now = datetime.now(tz=UTC)
        att = service.record_attempt(
            _OWNER_ID, source.id, _result_with_postings(3), now=now
        )
        assert att.outcome == CrawlAttemptOutcome.POSTINGS_FOUND
        assert att.next_eligible_at is not None
        cooldown = att.next_eligible_at - now
        assert cooldown <= timedelta(minutes=10)

    def test_disabled_source_not_eligible(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source(enabled=False)
        repo.save(source)
        assert service.is_source_eligible(_OWNER_ID, source.id) is False

    def test_paused_source_not_eligible(self) -> None:
        service, repo, _ = self._setup()
        source = _make_source(state=CrawlSourceState.PAUSED)
        repo.save(source)
        assert service.is_source_eligible(_OWNER_ID, source.id) is False


# ===========================================================================
# 5.8 — 100-source queue fixture + mixed outcomes
# ===========================================================================


# The seven outcome types.
_OUTCOME_SCENARIOS: list[tuple[str, CrawlDecision, CrawlSourceResult, bool, bool, bool, CrawlAttemptOutcome]] = [
    # (label, policy, result, has_adapter, transport_err, timeout, expected_outcome)
    (
        "postings_found",
        CrawlDecision.ALLOW,
        _result_with_postings(3),
        True, False, False,
        CrawlAttemptOutcome.POSTINGS_FOUND,
    ),
    (
        "verified_empty",
        CrawlDecision.ALLOW,
        _empty_result(status_code=200),
        True, False, False,
        CrawlAttemptOutcome.VERIFIED_EMPTY,
    ),
    (
        "auth_required",
        CrawlDecision.ALLOW,
        _empty_result(status_code=403, body_prefix="Sign in to continue"),
        True, False, False,
        CrawlAttemptOutcome.AUTH_REQUIRED,
    ),
    (
        "dynamic_or_unsupported",
        CrawlDecision.ALLOW,
        _empty_result(status_code=404),
        True, False, False,
        CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
    ),
    (
        "transient_failure",
        CrawlDecision.ALLOW,
        _empty_result(status_code=503),
        True, False, False,
        CrawlAttemptOutcome.TRANSIENT_FAILURE,
    ),
    (
        "policy_denied",
        CrawlDecision.DENY_BLOCKED,
        _empty_result(),
        True, False, False,
        CrawlAttemptOutcome.POLICY_DENIED,
    ),
    (
        "not_job_source",
        CrawlDecision.ALLOW,
        _empty_result(status_code=0),
        False, False, False,
        CrawlAttemptOutcome.NOT_JOB_SOURCE,
    ),
]


class Test100SourceQueue:
    """5.8: 100-source queue fixture with mixed outcomes.

    Verifies:
    - Every source receives exactly one attempt per run.
    - Every attempt has a durable outcome matching the classifier.
    - No duplicate attempts (attempt_no monotonicity).
    - POLICY_DENIED sources have no next_eligible_at.
    - Non-terminal sources have next_eligible_at set.
    """

    def _build_100_sources(self) -> list[CrawlSource]:
        """Create 100 sources cycling through the 7 outcome scenarios."""
        sources: list[CrawlSource] = []
        for i in range(100):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            label = _OUTCOME_SCENARIOS[scenario_idx][0]
            sources.append(
                _make_source(
                    source_identifier=f"source-{i}-{label}",
                    base_url=f"https://example{i}.com/careers",
                )
            )
        return sources

    def test_100_sources_one_attempt_each(self) -> None:
        """Each of 100 sources gets exactly one attempt in a single run."""
        sources = self._build_100_sources()
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)
        results: list[CrawlSourceAttempt] = []

        for i, source in enumerate(sources):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            _, policy, result, has_adapter, transport_err, timeout, _ = (
                _OUTCOME_SCENARIOS[scenario_idx]
            )
            att = service.record_attempt(
                _OWNER_ID,
                source.id,
                result,
                policy_decision=policy,
                has_adapter=has_adapter,
                transport_error=transport_err,
                timed_out=timeout,
                now=now,
            )
            results.append(att)

        # Exactly 100 attempts.
        assert len(results) == 100

        # Every attempt_no == 1 (first attempt for each source).
        for att in results:
            assert att.attempt_no == 1

        # Every source_id is unique.
        source_ids = {att.source_id for att in results}
        assert len(source_ids) == 100

    def test_100_sources_outcomes_match_classifier(self) -> None:
        """Every attempt's outcome matches what the classifier would produce."""
        sources = self._build_100_sources()
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)

        for i, source in enumerate(sources):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            label, policy, result, has_adapter, transport_err, timeout, expected = (
                _OUTCOME_SCENARIOS[scenario_idx]
            )
            att = service.record_attempt(
                _OWNER_ID,
                source.id,
                result,
                policy_decision=policy,
                has_adapter=has_adapter,
                transport_error=transport_err,
                timed_out=timeout,
                now=now,
            )
            assert att.outcome == expected, (
                f"source-{i} ({label}): expected {expected.value}, "
                f"got {att.outcome.value}"
            )

    def test_100_sources_no_duplicate_attempts(self) -> None:
        """No two attempts share the same (source_id, attempt_no)."""
        sources = self._build_100_sources()
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)

        # Record 2 attempts per source.
        for i, source in enumerate(sources):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            _, policy, result, has_adapter, transport_err, timeout, _ = (
                _OUTCOME_SCENARIOS[scenario_idx]
            )
            service.record_attempt(
                _OWNER_ID, source.id, result,
                policy_decision=policy,
                has_adapter=has_adapter,
                transport_error=transport_err,
                timed_out=timeout,
                now=now,
            )
            service.record_attempt(
                _OWNER_ID, source.id, _empty_result(status_code=200),
                now=now,
            )

        # Verify no duplicates: for each source, attempt_nos are [1, 2].
        for source in sources:
            attempts = attempt_repo.list_for_source(_OWNER_ID, source.id)
            nos = sorted(a.attempt_no for a in attempts)
            assert nos == [1, 2], f"source {source.id}: expected [1, 2], got {nos}"

    def test_100_sources_cooldown_compliance(self) -> None:
        """Non-terminal outcomes have next_eligible_at; terminal ones don't."""
        sources = self._build_100_sources()
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)

        for i, source in enumerate(sources):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            _, policy, result, has_adapter, transport_err, timeout, expected = (
                _OUTCOME_SCENARIOS[scenario_idx]
            )
            att = service.record_attempt(
                _OWNER_ID,
                source.id,
                result,
                policy_decision=policy,
                has_adapter=has_adapter,
                transport_error=transport_err,
                timed_out=timeout,
                now=now,
            )
            if expected in TERMINAL_STOP_OUTCOMES:
                assert att.next_eligible_at is None, (
                    f"source-{i} ({expected.value}): terminal should have no cooldown"
                )
            else:
                assert att.next_eligible_at is not None, (
                    f"source-{i} ({expected.value}): non-terminal should have cooldown"
                )
                assert att.next_eligible_at > now

    def test_100_sources_evidence_summary_populated(self) -> None:
        """Every attempt has a non-empty evidence_summary."""
        sources = self._build_100_sources()
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)

        for i, source in enumerate(sources):
            scenario_idx = i % len(_OUTCOME_SCENARIOS)
            _, policy, result, has_adapter, transport_err, timeout, _ = (
                _OUTCOME_SCENARIOS[scenario_idx]
            )
            att = service.record_attempt(
                _OWNER_ID,
                source.id,
                result,
                policy_decision=policy,
                has_adapter=has_adapter,
                transport_error=transport_err,
                timed_out=timeout,
                now=now,
            )
            assert att.evidence_summary, f"source-{i}: evidence_summary is empty"
            assert "status_code" in att.evidence_summary
            assert "postings_count" in att.evidence_summary
            assert "policy_decision" in att.evidence_summary

    def test_select_sources_excludes_policy_denied_on_rerun(self) -> None:
        """After POLICY_DENIED, source is not eligible on next run."""
        sources = [_make_source() for _ in range(10)]
        source_repo = InMemorySourceRepo()
        for s in sources:
            source_repo.save(s)
        attempt_repo = InMemoryAttemptRepo()
        service = SourceQueueService(source_repo, attempt_repo)

        now = datetime.now(tz=UTC)

        # Record attempts: first 5 get POSTINGS_FOUND, last 5 get POLICY_DENIED.
        for i, source in enumerate(sources):
            if i < 5:
                service.record_attempt(
                    _OWNER_ID, source.id, _result_with_postings(1), now=now
                )
            else:
                service.record_attempt(
                    _OWNER_ID,
                    source.id,
                    _empty_result(),
                    policy_decision=CrawlDecision.DENY_BLOCKED,
                    now=now,
                )

        # Check eligibility after cooldown.
        later = now + timedelta(hours=1)
        eligible_count = sum(
            1
            for s in sources
            if service.is_source_eligible(_OWNER_ID, s.id, now=later)
        )
        # Only the 5 POSTINGS_FOUND sources are eligible (short cooldown).
        # The 5 POLICY_DENIED are terminal stops.
        assert eligible_count == 5


class TestEligibilityEdgeCases:
    """Additional edge cases for source eligibility."""

    def test_never_attempted_source_is_eligible(self) -> None:
        repo = InMemorySourceRepo()
        attempt_repo = InMemoryAttemptRepo()
        source = _make_source()
        repo.save(source)
        service = SourceQueueService(repo, attempt_repo)
        assert service.is_source_eligible(_OWNER_ID, source.id) is True

    def test_multiple_runs_increment_attempt_no(self) -> None:
        repo = InMemorySourceRepo()
        attempt_repo = InMemoryAttemptRepo()
        source = _make_source()
        repo.save(source)
        service = SourceQueueService(repo, attempt_repo)

        now = datetime.now(tz=UTC)
        for run_no in range(1, 6):
            att = service.record_attempt(
                _OWNER_ID, source.id, _empty_result(), now=now
            )
            assert att.attempt_no == run_no

    def test_all_seven_outcomes_represented(self) -> None:
        """Verify the test fixture covers all 7 outcomes."""
        outcomes = {scenario[6] for scenario in _OUTCOME_SCENARIOS}
        expected = set(CrawlAttemptOutcome)
        assert outcomes == expected, (
            f"Missing outcomes: {expected - outcomes}"
        )
