"""Source-specific backoff and stop rules (Section 5, task 5.7).

Detects HTTP 403 (blocked), 429 (rate limit), CAPTCHA / login-wall signals,
explicit terms blocks, parser drift (structured_data missing expected fields),
and repeated failures. When a stop condition is met the policy records
``next_eligible_at`` on the source or run so the scheduler skips the domain
until the backoff expires.

Design:
- ``BackoffReason`` enum — the safe, non-sensitive label persisted into
  ``error_category`` / ``last_run_metadata`` (never raw payloads or PII).
- ``BackoffState`` — per-domain mutable state tracking consecutive failures
  and the computed ``next_eligible_at``.
- ``BackoffPolicy`` — stateless evaluator that inspects a
  ``SourceFetchOutcome`` and returns a ``BackoffDecision``.

Fail-closed (Iron Rule 3): unknown or unavailable policy dependencies
(e.g. a terms-DB lookup that raises) produce ``BackoffReason.DEPENDENCY_UNAVAILABLE``
and the source is stopped. The caller does NOT silently proceed.

The policy does NOT import or depend on external services (DNS resolver, terms
DB) — it receives pre-evaluated signals. If a caller cannot evaluate a
dependency, it passes ``dependency_available=False`` and the policy fails closed.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from enum import StrEnum

__all__ = [
    "BackoffDecision",
    "BackoffPolicy",
    "BackoffReason",
    "BackoffState",
    "SourceFetchOutcome",
]


class BackoffReason(StrEnum):
    """Safe, non-sensitive label for backoff / stop decisions.

    Persisted into ``error_category`` or ``last_run_metadata`` — never raw
    error payloads or PII (crawl-plan-management spec: "Operational failures
    SHALL be actionable without exposing credentials or raw sensitive content").
    """

    BLOCKED_403 = "blocked_403"
    RATE_LIMITED_429 = "rate_limited_429"
    CAPTCHA_OR_LOGIN_WALL = "captcha_or_login_wall"
    TERMS_BLOCKED = "terms_blocked"
    PARSE_DRIFT = "parse_drift"
    REPEATED_FAILURE = "repeated_failure"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


# CAPTCHA / login-wall heuristics: substrings that appear in response bodies
# or headers when a source requires human interaction.  Checked case-insensitive
# against the first 4 KiB of the response body.
_CAPTCHA_SIGNALS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "please verify you are human",
    "access denied",
    "login required",
    "sign in to continue",
    "authentication required",
)


@dataclasses.dataclass(frozen=True, slots=True)
class SourceFetchOutcome:
    """Signals the caller extracted from one source fetch attempt.

    The caller populates these from the HTTP response + adapter parse result.
    If a dependency (terms DB, DNS resolver) was unavailable, set
    ``dependency_available=False`` — the policy fails closed.
    """

    domain: str
    status_code: int = 0
    body_prefix: str = ""
    terms_status: str = "unknown"
    jobs_found: int = 0
    expected_fields_missing: tuple[str, ...] = ()
    consecutive_failures: int = 0
    dependency_available: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class BackoffDecision:
    """Result of a backoff policy evaluation.

    ``should_stop=True`` means the source MUST NOT be retried in this run.
    ``next_eligible_at`` is the earliest time the scheduler may touch this
    domain again; ``None`` means "no backoff" (the source may be retried on
    the next scheduled run).
    """

    should_stop: bool
    reason: BackoffReason | None = None
    next_eligible_at: datetime | None = None


@dataclasses.dataclass(slots=True)
class BackoffState:
    """Mutable per-domain backoff state.

    Updated by :meth:`BackoffPolicy.record_outcome`. The caller persists
    ``next_eligible_at`` on the source or run record.
    """

    consecutive_failures: int = 0
    next_eligible_at: datetime | None = None


# Backoff durations by reason.
_BACKOFF_DURATIONS: dict[BackoffReason, timedelta] = {
    BackoffReason.BLOCKED_403: timedelta(hours=24),
    BackoffReason.RATE_LIMITED_429: timedelta(minutes=30),
    BackoffReason.CAPTCHA_OR_LOGIN_WALL: timedelta(hours=12),
    BackoffReason.TERMS_BLOCKED: timedelta(days=7),
    BackoffReason.PARSE_DRIFT: timedelta(hours=4),
    BackoffReason.REPEATED_FAILURE: timedelta(hours=1),
    BackoffReason.DEPENDENCY_UNAVAILABLE: timedelta(minutes=15),
}

# After this many consecutive failures, stop the source for the current run.
_MAX_CONSECUTIVE_FAILURES = 3

# Minimum number of expected fields that must be present to avoid parse drift.
_PARSE_DRIFT_MIN_FIELDS = 1


class BackoffPolicy:
    """Stateless evaluator for source-specific backoff and stop rules.

    Inspects a :class:`SourceFetchOutcome` and returns a
    :class:`BackoffDecision`. The caller owns per-domain
    :class:`BackoffState` and calls :meth:`record_outcome` after each fetch.
    """

    def evaluate(
        self,
        outcome: SourceFetchOutcome,
        *,
        now: datetime | None = None,
    ) -> BackoffDecision:
        """Evaluate one fetch outcome and return a stop/backoff decision.

        Checks (in priority order):
        1. Dependency unavailable -> fail-closed.
        2. 403 -> blocked.
        3. Terms status "blocked" -> terms blocked.
        4. 429 -> rate limited.
        5. CAPTCHA / login-wall signals in body.
        6. Parser drift (expected fields missing and zero jobs).
        7. Repeated consecutive failures.
        """
        ts = now or datetime.now(tz=UTC)

        # 1. Dependency unavailable -> fail-closed.
        if not outcome.dependency_available:
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.DEPENDENCY_UNAVAILABLE,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.DEPENDENCY_UNAVAILABLE],
            )

        # 2. HTTP 403 — server explicitly blocked us.
        if outcome.status_code == 403:
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.BLOCKED_403,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.BLOCKED_403],
            )

        # 3. Terms status "blocked" — the terms layer flagged the domain.
        if outcome.terms_status == "blocked":
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.TERMS_BLOCKED,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.TERMS_BLOCKED],
            )

        # 4. HTTP 429 — rate limited.
        if outcome.status_code == 429:
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.RATE_LIMITED_429,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.RATE_LIMITED_429],
            )

        # 5. CAPTCHA / login-wall signals in body prefix.
        if _has_captcha_signal(outcome.body_prefix):
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.CAPTCHA_OR_LOGIN_WALL,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.CAPTCHA_OR_LOGIN_WALL],
            )

        # 6. Parser drift: expected fields missing AND zero jobs found.
        if (
            len(outcome.expected_fields_missing) >= _PARSE_DRIFT_MIN_FIELDS
            and outcome.jobs_found == 0
        ):
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.PARSE_DRIFT,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.PARSE_DRIFT],
            )

        # 7. Repeated consecutive failures.
        if outcome.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            return BackoffDecision(
                should_stop=True,
                reason=BackoffReason.REPEATED_FAILURE,
                next_eligible_at=ts + _BACKOFF_DURATIONS[BackoffReason.REPEATED_FAILURE],
            )

        # All clear — no backoff.
        return BackoffDecision(should_stop=False)

    def record_outcome(
        self,
        state: BackoffState,
        decision: BackoffDecision,
        *,
        success: bool,
    ) -> BackoffState:
        """Update per-domain state after a fetch attempt.

        On success the consecutive failure counter resets and any pending
        ``next_eligible_at`` is cleared. On failure the counter increments and
        the decision's ``next_eligible_at`` is recorded if present.
        """
        if success:
            return BackoffState(consecutive_failures=0, next_eligible_at=None)
        new_count = state.consecutive_failures + 1
        return BackoffState(
            consecutive_failures=new_count,
            next_eligible_at=decision.next_eligible_at or state.next_eligible_at,
        )


def _has_captcha_signal(body_prefix: str) -> bool:
    """Check the first 4 KiB of a response body for CAPTCHA / login-wall signals."""
    snippet = body_prefix[:4096].lower()
    return any(signal in snippet for signal in _CAPTCHA_SIGNALS)
