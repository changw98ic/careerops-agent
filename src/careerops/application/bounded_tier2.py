"""Bounded Tier 2 orchestrator (real-autonomous-career-loop Phase 7.2-7.5, 7.7).

Routes confirmed public and permission-gated sources through the Tier 2
browser agent with full enforcement of per-source execution limits,
stop/cooldown signals, and the canonical ingest path.

**Routing (7.2 / 7.3)**:
- ``DYNAMIC_OR_UNSUPPORTED`` sources with positive job-source evidence:
  Tier 2 public crawl (no authenticated session).
- ``AUTH_REQUIRED`` sources with ``GRANTED`` permission + valid session:
  Tier 2 authenticated crawl.  (Phase 6.6 session reference is mocked
  until 6.6 is implemented; a missing session triggers a stop.)

**Per-source limits (7.4)**:
- At most 30 browser actions per source run.
- At most 5 minutes wall-clock time per source run.
- Stop after 3 consecutive result pages with no new canonical posting
  identity.

**Stop signals (7.5)**:
- CAPTCHA / account-risk challenge encountered.
- Permission revoked, expired, or denied mid-run.
- Session expired mid-run.
- Policy denial from the crawl-policy layer.
These signals produce a cooldown outcome via the attempt recorder; they
are NEVER treated as permission or bypass signals.

**Ingest path (7.7)**:
Every valid Tier 2 record flows through the existing ``ingest_posting``
on ``RealCrawlActivitySink`` with crawl-run and plan-version provenance.
There is no parallel ingest implementation.

Iron Rules honored:
- 3 (fail-closed: any stop signal stops the run, never bypasses).
- 2 (server-side ownership: every method takes ``owner_id``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.crawl_agent import CrawlAgentRunResult
from careerops.application.tier2_budget import Tier2Lease
from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlPermissionRepository,
    CrawlPermissionState,
)
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink

__all__ = [
    "BoundedTier2Orchestrator",
    "PermissionBasedSessionChecker",
    "RepositoryPermissionChecker",
    "Tier2RoutingDecision",
    "Tier2RunConfig",
    "Tier2RunResult",
    "Tier2StopReason",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-source execution limits (spec 7.4)
# ---------------------------------------------------------------------------

_MAX_ACTIONS_PER_SOURCE = 30
_MAX_DURATION_S = 300.0  # 5 minutes
_MAX_CONSECUTIVE_EMPTY_PAGES = 3

# Tier 2 cooldown durations for different stop reasons.
_COOLDOWN_DURATIONS: dict[CrawlAttemptOutcome, str] = {
    CrawlAttemptOutcome.TRANSIENT_FAILURE: "30m",
    CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED: "2h",
    CrawlAttemptOutcome.AUTH_REQUIRED: "12h",
}


class Tier2StopReason(StrEnum):
    """Why a Tier 2 source run was stopped."""

    ACTION_LIMIT = "action_limit"
    TIME_LIMIT = "time_limit"
    DUPLICATE_STOP = "duplicate_stop"
    CAPTCHA = "captcha"
    ACCOUNT_RISK = "account_risk"
    PERMISSION_REVOKED = "permission_revoked"
    SESSION_EXPIRED = "session_expired"
    POLICY_DENIED = "policy_denied"
    NO_POSTINGS = "no_postings"
    COMPLETED = "completed"


class Tier2RoutingDecision(StrEnum):
    """Whether a source should enter Tier 2 and how."""

    SKIP_PUBLIC = "skip_public"  # Public Tier 2 (no auth session)
    SKIP_AUTHENTICATED = "skip_authenticated"  # Authenticated Tier 2
    DENY = "deny"  # Not eligible for Tier 2


@dataclass(frozen=True, slots=True)
class Tier2RunConfig:
    """Configuration for a single Tier 2 source run."""

    source_id: str
    base_url: str
    owner_id: UUID
    crawl_run_id: UUID | None = None
    plan_version_id: UUID | None = None
    authenticated: bool = False
    max_actions: int = _MAX_ACTIONS_PER_SOURCE
    max_duration_s: float = _MAX_DURATION_S
    max_consecutive_empty: int = _MAX_CONSECUTIVE_EMPTY_PAGES
    # Forwarded to ``crawl_bounded`` → ``crawl`` → ``_match_skill`` so a Tier 2
    # skill playbook (e.g. workday) matches by source_type on the only legal
    # Tier 2 entry point, not just by URL. ``""`` preserves the prior
    # URL-only behaviour when no type is known.
    source_type: str = ""


@dataclass(slots=True)
class Tier2RunResult:
    """Outcome of a bounded Tier 2 source run."""

    source_id: str
    stop_reason: Tier2StopReason
    action_count: int = 0
    postings_found: int = 0
    postings_new: int = 0
    postings_updated: int = 0
    consecutive_empty_pages: int = 0
    duration_s: float = 0.0
    error: str = ""
    canonical_job_ids: set[UUID] = field(default_factory=lambda: set[UUID]())


# ---------------------------------------------------------------------------
# Protocols for external dependencies
# ---------------------------------------------------------------------------


class SessionChecker(Protocol):
    """Resolve a currently valid source-scoped authenticated session."""

    def get_session_ref(self, owner_id: UUID, source_id: str) -> str | None: ...


class PermissionChecker(Protocol):
    """Checks whether a source's crawl permission is currently granted."""

    def is_permission_granted(self, owner_id: UUID, source_id: UUID) -> bool: ...


class PolicyEvaluator(Protocol):
    """Evaluates crawl policy for a source."""

    def evaluate(self, owner_id: UUID, source_id: str) -> CrawlDecision: ...


class CaptchaDetector(Protocol):
    """Detects CAPTCHA or account-risk signals in page content."""

    def has_captcha_signal(self, body_prefix: str) -> bool: ...


class Tier2BudgetCoordinator(Protocol):
    def acquire(self, source_id: str) -> Tier2Lease | None: ...

    def release(self, lease: Tier2Lease) -> None: ...

    def consume(self, count: int = 1) -> int: ...

    @property
    def daily_remaining(self) -> int: ...


class Tier2Agent(Protocol):
    def crawl_bounded(
        self,
        source_url: str,
        *,
        max_actions: int,
        max_duration_s: float,
        max_consecutive_empty: int,
        session_ref: str | None,
        consume_action: Callable[[], bool] | None,
        source_type: str = "",
    ) -> CrawlAgentRunResult: ...


# ---------------------------------------------------------------------------
# Default implementations
# ---------------------------------------------------------------------------


class _MockSessionChecker:
    """Fail-closed session checker: always returns False.

    Phase 6.6 will replace this with a real session-reference check.
    """

    def get_session_ref(self, owner_id: UUID, source_id: str) -> str | None:
        del owner_id, source_id
        return None


class PermissionBasedSessionChecker:
    """Checks session validity via the crawl permission grant state.

    A source's "session" is considered valid when it has a GRANTED permission
    that has not expired AND carries an opaque session_ref (Phase 6.6).
    The session_ref requirement ensures that authorization (permission grant)
    is not conflated with authentication (a real logged-in session).
    """

    def __init__(
        self,
        permission_repo: CrawlPermissionRepository,
    ) -> None:
        self._repo = permission_repo

    def get_session_ref(self, owner_id: UUID, source_id: str) -> str | None:
        from datetime import UTC, datetime

        try:
            source_uuid = UUID(source_id)
        except ValueError:
            return None
        perms = self._repo.list_for_source(owner_id, source_uuid, limit=10)
        now = datetime.now(tz=UTC)
        for p in perms:
            if (
                p.state is CrawlPermissionState.GRANTED
                and (p.expires_at is None or p.expires_at > now)
                and p.session_ref == f"careerops-login-{source_uuid}"
            ):
                return p.session_ref
        return None


class RepositoryPermissionChecker:
    """Checks granted, unexpired permission state in the durable repository."""

    def __init__(self, permission_repo: CrawlPermissionRepository) -> None:
        self._repo = permission_repo

    def is_permission_granted(self, owner_id: UUID, source_id: UUID) -> bool:
        now = datetime.now(tz=UTC)
        return any(
            permission.state is CrawlPermissionState.GRANTED
            and (permission.expires_at is None or permission.expires_at > now)
            for permission in self._repo.list_for_source(owner_id, source_id, limit=10)
        )


class _BodyPrefixCaptchaDetector:
    """Detects CAPTCHA signals in the body prefix."""

    _CAPTCHA_SIGNALS = (
        "captcha",
        "recaptcha",
        "hcaptcha",
        "cf-turnstile",
        "please verify you are human",
    )
    _ACCOUNT_RISK_SIGNALS = (
        "account suspended",
        "too many requests",
        "rate limit",
        "blocked",
        "unusual activity",
    )

    def has_captcha_signal(self, body_prefix: str) -> bool:
        snippet = body_prefix[:4096].lower()
        return any(s in snippet for s in self._CAPTCHA_SIGNALS)

    def has_account_risk_signal(self, body_prefix: str) -> bool:
        snippet = body_prefix[:4096].lower()
        return any(s in snippet for s in self._ACCOUNT_RISK_SIGNALS)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class BoundedTier2Orchestrator:
    """Executes bounded Tier 2 source runs with full limit enforcement.

    This is the canonical Tier 2 execution path (Phase 7).  It:
    1. Checks routing eligibility (7.2 / 7.3).
    2. Acquires a concurrent slot and daily action budget (7.1).
    3. Executes the crawl via the injected ``CrawlAgent`` + ``LLMJobExtractor``.
    4. Enforces per-source limits (7.4) and stop signals (7.5).
    5. Ingests every valid record through ``ingest_posting`` (7.7).
    6. Records the attempt outcome with the appropriate cooldown.

    Args:
        sink: the canonical ``RealCrawlActivitySink`` (Phase 4 factory output).
        budget: the ``Tier2Budget`` coordinator (Phase 7.1).
        permission_checker: checks if a source's permission is granted.
        policy_evaluator: evaluates crawl policy for a source.
        session_checker: checks if an authenticated session is valid
            (defaults to mock until Phase 6.6).
        captcha_detector: detects CAPTCHA/account-risk signals.
        agent: the Tier 2 browser agent (duck-typed as ``.crawl(url)``).
        extractor: the LLM job extractor (duck-typed as ``.extract(html)``).
    """

    def __init__(
        self,
        sink: RealCrawlActivitySink,
        budget: Tier2BudgetCoordinator,
        *,
        permission_checker: PermissionChecker | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        session_checker: SessionChecker | None = None,
        captcha_detector: CaptchaDetector | None = None,
        agent: Tier2Agent | None = None,
        extractor: object | None = None,
        permission_repo: CrawlPermissionRepository | None = None,
        owner_id: UUID | None = None,
    ) -> None:
        self._sink = sink
        self._budget = budget
        self._permission_checker = permission_checker
        self._policy = policy_evaluator
        if session_checker is not None:
            self._session = session_checker
        elif permission_repo is not None:
            self._session = PermissionBasedSessionChecker(permission_repo)
        else:
            self._session = _MockSessionChecker()
        self._captcha = captcha_detector or _BodyPrefixCaptchaDetector()
        self._agent = agent
        self._extractor = extractor

    # ------------------------------------------------------------------
    # Routing (7.2 / 7.3)
    # ------------------------------------------------------------------

    def should_enter_tier2(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        tier1_outcome: CrawlAttemptOutcome,
        has_adapter: bool = False,
        recipe_fallback: str = "llm_skill",
    ) -> Tier2RoutingDecision:
        """Decide if a source should enter Tier 2 and how.

        7.2: ``DYNAMIC_OR_UNSUPPORTED`` with positive job-source evidence
        enters public Tier 2 (no auth session).
        7.3: ``AUTH_REQUIRED`` enters authenticated Tier 2 only when
        permission is granted AND the source-scoped session is valid.

        Evidence gate (beads rule: "Tier 1 empty results must not be directly
        upgraded; there must be job-source evidence"). For
        ``DYNAMIC_OR_UNSUPPORTED``, escalation to public Tier 2 is granted only
        when:

        * ``has_adapter`` is ``True`` — a Tier 1 recipe actually ran against
          this source (direct lookup, OFFICIAL probe, or ``detect()`` URL
          routing). This is positive evidence the URL is a job source whose
          structured parse drifted, not an unknown URL that happened to 200.
        * ``recipe_fallback != "none"`` — the recipe did not explicitly opt out
          of escalation. A recipe declaring ``fallback: none`` pins the source
          to Tier 1-only even when it ran.

        An unknown HTTP source (``has_adapter=False``) therefore never enters
        Tier 2 from an empty Tier 1 result. ``DENY`` is not terminal: author a
        recipe (or let ``detect()`` match) and the source gains evidence.

        Returns ``DENY`` if the source is not eligible.
        """
        if tier1_outcome == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED:
            if has_adapter and recipe_fallback != "none":
                return Tier2RoutingDecision.SKIP_PUBLIC
            return Tier2RoutingDecision.DENY

        if tier1_outcome == CrawlAttemptOutcome.AUTH_REQUIRED:
            if self._permission_checker is None:
                return Tier2RoutingDecision.DENY
            if not self._permission_checker.is_permission_granted(owner_id, source_id):
                return Tier2RoutingDecision.DENY
            if self._session.get_session_ref(owner_id, str(source_id)) is None:
                return Tier2RoutingDecision.DENY
            return Tier2RoutingDecision.SKIP_AUTHENTICATED

        # All other outcomes (POSTINGS_FOUND, VERIFIED_EMPTY, NOT_JOB_SOURCE,
        # TRANSIENT_FAILURE, POLICY_DENIED) -> not eligible.
        return Tier2RoutingDecision.DENY

    # ------------------------------------------------------------------
    # Execution (7.4 / 7.5 / 7.7)
    # ------------------------------------------------------------------

    async def run_source(
        self,
        config: Tier2RunConfig,
    ) -> Tier2RunResult:
        """Execute a bounded Tier 2 source run.

        Acquires a concurrent slot and daily action budget, runs the
        browser agent with per-source limits, ingests valid records, and
        releases the slot.  Returns the run result with stop reason.
        """
        # Pre-flight: policy check (7.5)
        if self._policy is not None:
            decision = self._policy.evaluate(config.owner_id, config.source_id)
            if decision is not CrawlDecision.ALLOW:
                return Tier2RunResult(
                    source_id=config.source_id,
                    stop_reason=Tier2StopReason.POLICY_DENIED,
                    error=f"policy decision: {decision.value}",
                )

        session_ref: str | None = None
        if config.authenticated:
            source_uuid = UUID(config.source_id)
            if (
                self._permission_checker is None
                or not self._permission_checker.is_permission_granted(config.owner_id, source_uuid)
            ):
                return Tier2RunResult(
                    source_id=config.source_id,
                    stop_reason=Tier2StopReason.PERMISSION_REVOKED,
                    error="source-specific crawl permission is not granted",
                )
            session_ref = self._session.get_session_ref(config.owner_id, config.source_id)
            if session_ref is None:
                return Tier2RunResult(
                    source_id=config.source_id,
                    stop_reason=Tier2StopReason.SESSION_EXPIRED,
                    error="source-scoped authenticated session is absent or expired",
                )

        if self._budget.daily_remaining <= 0:
            return Tier2RunResult(
                source_id=config.source_id,
                stop_reason=Tier2StopReason.ACTION_LIMIT,
                error="daily browser-action budget exhausted",
            )

        # Pre-flight: acquire concurrent slot (7.1)
        lease = self._budget.acquire(config.source_id)
        if lease is None:
            return Tier2RunResult(
                source_id=config.source_id,
                stop_reason=Tier2StopReason.NO_POSTINGS,
                error="no concurrent Tier 2 slot available",
            )

        try:
            return await self._execute(config, lease, session_ref=session_ref)
        finally:
            self._budget.release(lease)

    async def _execute(
        self,
        config: Tier2RunConfig,
        lease: Tier2Lease,
        *,
        session_ref: str | None,
    ) -> Tier2RunResult:
        """Inner execution with limit enforcement."""
        start = time.monotonic()
        action_count = 0
        consecutive_empty = 0
        postings_found = 0
        postings_new = 0
        postings_updated = 0
        stop_reason = Tier2StopReason.NO_POSTINGS
        error = ""
        canonical_job_ids: set[UUID] = set()

        # Run the browser agent.
        raw_records: list[RawJobRecord] = []
        try:
            if self._agent is None:
                raise RuntimeError("Tier 2 browser agent is not configured")
            run_result = self._agent.crawl_bounded(
                config.base_url,
                max_actions=config.max_actions,
                max_duration_s=config.max_duration_s,
                max_consecutive_empty=config.max_consecutive_empty,
                session_ref=session_ref,
                consume_action=lambda: self._budget.consume(1) == 1,
                source_type=config.source_type,
            )
            raw_records = list(run_result.records)
            action_count = run_result.action_count
            consecutive_empty = run_result.consecutive_empty_pages
            stop_reason = {
                "action_limit": Tier2StopReason.ACTION_LIMIT,
                "time_limit": Tier2StopReason.TIME_LIMIT,
                "duplicate_stop": Tier2StopReason.DUPLICATE_STOP,
                "session_expired": Tier2StopReason.SESSION_EXPIRED,
            }.get(run_result.stop_reason, Tier2StopReason.NO_POSTINGS)
        except Exception as exc:
            error = str(exc)
            # Check if the error indicates CAPTCHA or account risk.
            if self._captcha.has_captcha_signal(error):
                stop_reason = Tier2StopReason.CAPTCHA
            elif isinstance(
                self._captcha, _BodyPrefixCaptchaDetector
            ) and self._captcha.has_account_risk_signal(error):
                stop_reason = Tier2StopReason.ACCOUNT_RISK
            else:
                stop_reason = Tier2StopReason.NO_POSTINGS
            return Tier2RunResult(
                source_id=config.source_id,
                stop_reason=stop_reason,
                action_count=action_count,
                duration_s=time.monotonic() - start,
                error=error,
            )

        elapsed = time.monotonic() - start

        # Defensive post-checks complement the agent's per-operation checks.
        if stop_reason is Tier2StopReason.ACTION_LIMIT or action_count >= config.max_actions:
            stop_reason = Tier2StopReason.ACTION_LIMIT
        elif stop_reason is Tier2StopReason.TIME_LIMIT or elapsed >= config.max_duration_s:
            stop_reason = Tier2StopReason.TIME_LIMIT
        elif (
            stop_reason is Tier2StopReason.DUPLICATE_STOP
            or consecutive_empty >= config.max_consecutive_empty
        ):
            stop_reason = Tier2StopReason.DUPLICATE_STOP

        # Stop signal check: re-validate permission (7.5 — may have been
        # revoked mid-run).
        if self._permission_checker is not None and config.authenticated:
            if not self._permission_checker.is_permission_granted(
                config.owner_id,
                UUID(config.source_id) if len(config.source_id) == 36 else UUID(int=0),
            ):
                stop_reason = Tier2StopReason.PERMISSION_REVOKED
                error = "permission revoked during crawl"
            elif self._session.get_session_ref(config.owner_id, config.source_id) is None:
                stop_reason = Tier2StopReason.SESSION_EXPIRED
                error = "authenticated session expired during crawl"

        # Ingest valid records through the canonical path (7.7).
        # Skip ingest only if a hard stop signal was raised (7.5: CAPTCHA,
        # account-risk, permission revocation, session expiry, policy denial).
        # Action/time limits stop the crawl but still allow ingest of records
        # already collected.
        _HARD_STOP_SIGNALS = {
            Tier2StopReason.CAPTCHA,
            Tier2StopReason.ACCOUNT_RISK,
            Tier2StopReason.PERMISSION_REVOKED,
            Tier2StopReason.SESSION_EXPIRED,
            Tier2StopReason.POLICY_DENIED,
        }
        if raw_records and stop_reason not in _HARD_STOP_SIGNALS:
            try:
                from careerops.infrastructure.temporal.m1_crawl_sink import (
                    ingest_crawled_records,
                )

                counters = await ingest_crawled_records(
                    self._sink,
                    raw_records,
                    source_id=config.source_id,
                    source_url=config.base_url,
                    crawl_run_id=config.crawl_run_id,
                    plan_version_id=config.plan_version_id,
                )
                postings_found = counters.discovered + counters.updated
                postings_new = counters.discovered
                postings_updated = counters.updated
                canonical_job_ids = counters.canonical_job_ids
                if postings_found > 0:
                    stop_reason = Tier2StopReason.COMPLETED
            except Exception as exc:
                error = f"ingest failed: {exc}"
                stop_reason = Tier2StopReason.NO_POSTINGS

        return Tier2RunResult(
            source_id=config.source_id,
            stop_reason=stop_reason,
            action_count=action_count,
            postings_found=postings_found,
            postings_new=postings_new,
            postings_updated=postings_updated,
            consecutive_empty_pages=consecutive_empty,
            duration_s=time.monotonic() - start,
            error=error,
            canonical_job_ids=canonical_job_ids,
        )

    # ------------------------------------------------------------------
    # Outcome mapping (for attempt recorder integration)
    # ------------------------------------------------------------------

    @staticmethod
    def outcome_for_stop_reason(
        stop_reason: Tier2StopReason,
        postings_found: int,
    ) -> CrawlAttemptOutcome:
        """Map a Tier 2 stop reason to a ``CrawlAttemptOutcome``.

        Used by the attempt recorder to persist the canonical outcome.
        """
        if postings_found > 0:
            return CrawlAttemptOutcome.POSTINGS_FOUND
        mapping = {
            Tier2StopReason.ACTION_LIMIT: CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
            Tier2StopReason.TIME_LIMIT: CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
            Tier2StopReason.DUPLICATE_STOP: CrawlAttemptOutcome.VERIFIED_EMPTY,
            Tier2StopReason.CAPTCHA: CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
            Tier2StopReason.ACCOUNT_RISK: CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
            Tier2StopReason.PERMISSION_REVOKED: CrawlAttemptOutcome.AUTH_REQUIRED,
            Tier2StopReason.SESSION_EXPIRED: CrawlAttemptOutcome.AUTH_REQUIRED,
            Tier2StopReason.POLICY_DENIED: CrawlAttemptOutcome.POLICY_DENIED,
            Tier2StopReason.NO_POSTINGS: CrawlAttemptOutcome.VERIFIED_EMPTY,
            Tier2StopReason.COMPLETED: CrawlAttemptOutcome.VERIFIED_EMPTY,
        }
        return mapping.get(stop_reason, CrawlAttemptOutcome.TRANSIENT_FAILURE)
