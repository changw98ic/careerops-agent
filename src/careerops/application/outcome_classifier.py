"""Source attempt outcome classifier (real-autonomous-career-loop Phase 5.6).

Maps a ``CrawlSourceResult`` (postings tuple, status_code, body_prefix,
expected_fields_missing) plus policy decision and backoff reason onto the
canonical 7-way ``CrawlAttemptOutcome``.

Classification rules (from the task spec):
1. ``len(postings) > 0`` → ``POSTINGS_FOUND``
2. ``200`` + no auth/captcha signal + expected fields present → ``VERIFIED_EMPTY``
3. ``403`` OR captcha/login-wall body signal → ``AUTH_REQUIRED``
   (BUT: requires explicit login evidence — not just 403 alone when there
   are no positive job-content signals; see :func:`_has_login_evidence`)
4. ``4xx`` (other) / parse drift / JS-rendered → ``DYNAMIC_OR_UNSUPPORTED``
5. Transport error / ``5xx`` / timeout → ``TRANSIENT_FAILURE``
6. Policy decision != ALLOW → ``POLICY_DENIED``
7. No adapter + no job content → ``NOT_JOB_SOURCE``

Iron constraint (task 5.6): empty results, HTTP 403, CAPTCHA, model
judgement alone, and temporary network failures MUST NOT collapse into
``AUTH_REQUIRED`` without explicit login evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import CrawlAttemptOutcome
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult

__all__ = [
    "ClassificationInput",
    "classify_outcome",
]


# CAPTCHA / login-wall signal substrings (lowercased).
# Mirrors :data:`careerops.application.backoff_policy._CAPTCHA_SIGNALS` but
# kept separate to avoid a circular import and to allow the classifier to
# distinguish "explicit login evidence" from generic CAPTCHA.
_LOGIN_EVIDENCE_SIGNALS: tuple[str, ...] = (
    "login required",
    "sign in to continue",
    "authentication required",
    "please log in",
    "you must be logged in",
    "access denied",
)

_CAPTCHA_SIGNALS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "please verify you are human",
)


@dataclass(frozen=True, slots=True)
class ClassificationInput:
    """All signals the classifier needs to produce a ``CrawlAttemptOutcome``.

    The caller populates these from the crawl result + policy + backoff.
    """

    result: CrawlSourceResult
    policy_decision: CrawlDecision = CrawlDecision.ALLOW
    has_adapter: bool = True
    transport_error: bool = False
    timed_out: bool = False


def _has_captcha_signal(body_prefix: str) -> bool:
    """Check body prefix for CAPTCHA signals."""
    snippet = body_prefix[:4096].lower()
    return any(signal in snippet for signal in _CAPTCHA_SIGNALS)


def _has_login_evidence(body_prefix: str) -> bool:
    """Check body prefix for explicit login-wall evidence.

    Task 6.1 constraint: login requirements are detected ONLY from explicit
    evidence — login redirect, login wall over job content, explicit login
    message, or authentication-required job API response.
    """
    snippet = body_prefix[:4096].lower()
    return any(signal in snippet for signal in _LOGIN_EVIDENCE_SIGNALS)


def classify_outcome(inp: ClassificationInput) -> CrawlAttemptOutcome:
    """Classify a source attempt into one of the seven canonical outcomes.

    Priority order (highest first — each rule is checked in sequence):
    1. Policy denied → ``POLICY_DENIED`` (terminal stop)
    2. Transport error / timeout → ``TRANSIENT_FAILURE``
    3. Postings found → ``POSTINGS_FOUND``
    4. 403 + explicit login evidence → ``AUTH_REQUIRED``
    5. CAPTCHA signal + explicit login evidence → ``AUTH_REQUIRED``
    6. No adapter + no job content → ``NOT_JOB_SOURCE``
    7. 200 + no captcha + expected fields present → ``VERIFIED_EMPTY``
    8. 4xx / parse drift / JS-rendered → ``DYNAMIC_OR_UNSUPPORTED``
    9. 5xx → ``TRANSIENT_FAILURE``
    10. Fallback → ``NOT_JOB_SOURCE``

    The constraint from the spec is enforced: empty results, 403 alone,
    CAPTCHA alone, or model judgement alone CANNOT become ``AUTH_REQUIRED``
    without explicit login evidence.
    """
    result = inp.result

    # 1. Policy denied — terminal stop, highest priority.
    if inp.policy_decision is not CrawlDecision.ALLOW:
        return CrawlAttemptOutcome.POLICY_DENIED

    # 2. Transport error / timeout — transient, retryable.
    if inp.transport_error or inp.timed_out:
        return CrawlAttemptOutcome.TRANSIENT_FAILURE

    # 3. Postings found — success, regardless of other signals.
    if len(result.postings) > 0:
        return CrawlAttemptOutcome.POSTINGS_FOUND

    # From here on, postings == 0.

    # 4. 403 + explicit login evidence → AUTH_REQUIRED.
    #    But NOT 403 alone — 403 without login evidence is dynamic/unsupported.
    if result.status_code == 403:
        if _has_login_evidence(result.body_prefix):
            return CrawlAttemptOutcome.AUTH_REQUIRED
        # 403 without login evidence → treat as blocked/unsupported.
        return CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # 5. CAPTCHA + explicit login evidence → AUTH_REQUIRED.
    #    CAPTCHA alone is NOT enough (spec: "captcha signal alone cannot
    #    become AUTH_REQUIRED").
    if _has_captcha_signal(result.body_prefix):
        if _has_login_evidence(result.body_prefix):
            return CrawlAttemptOutcome.AUTH_REQUIRED
        return CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # 6. No adapter + no job content → NOT_JOB_SOURCE.
    if not inp.has_adapter:
        return CrawlAttemptOutcome.NOT_JOB_SOURCE

    # 7. 200 + no captcha + expected fields present → VERIFIED_EMPTY.
    #    This is the "clean 200 with a proper adapter that found no jobs" case.
    if result.status_code == 200 and not result.expected_fields_missing:
        return CrawlAttemptOutcome.VERIFIED_EMPTY

    # 8. 4xx (other than 403 handled above) / parse drift → DYNAMIC_OR_UNSUPPORTED.
    if 400 <= result.status_code < 500:
        return CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # Parse drift: expected fields missing (adapter could not extract structure).
    if result.expected_fields_missing:
        return CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # 9. 5xx → TRANSIENT_FAILURE.
    if 500 <= result.status_code < 600:
        return CrawlAttemptOutcome.TRANSIENT_FAILURE

    # 10. Fallback — status 0 (no response) or unknown → NOT_JOB_SOURCE.
    return CrawlAttemptOutcome.NOT_JOB_SOURCE
