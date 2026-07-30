"""Source attempt outcome classifier (real-autonomous-career-loop Phase 5.6 + 6.1).

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

Task 6.1 additions: login requirements are detected ONLY from explicit
positive signals — login redirect (``_has_login_redirect_signal``), login
wall over job content on a 200 page (``_has_login_evidence``), explicit
login message, or authentication-required job API response.  Empty results,
generic 403, CAPTCHA alone, and model judgement alone are NOT login evidence.
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
    "has_login_evidence",
    "has_login_redirect_signal",
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

# Login-redirect URL / body substrings (lowercased).  Task 6.1: a 3xx
# redirect whose ``Location`` header or body snippet contains one of these
# AND zero job postings is explicit login-evidence — the source requires
# authentication before it will serve job content.
_LOGIN_REDIRECT_SIGNALS: tuple[str, ...] = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth/login",
    "/account/login",
    "/sso/login",
    "login.microsoftonline",
    "login.live.com",
    "accounts.google.com",
    "auth0.com/authorize",
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


def _has_login_redirect_signal(body_prefix: str) -> bool:
    """Check body prefix for explicit login-redirect URL evidence.

    Task 6.1: a redirect whose ``Location`` header or response body contains
    a login URL (``/login``, ``/signin``, SSO endpoints) is positive evidence
    that the source requires authentication.  The body_prefix for HTTP
    redirects typically carries the ``Location`` header value or a short HTML
    snippet with the redirect URL.
    """
    snippet = body_prefix[:4096].lower()
    return any(signal in snippet for signal in _LOGIN_REDIRECT_SIGNALS)


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

    # 5b. Login redirect (3xx + login URL in Location/body) → AUTH_REQUIRED.
    #     Task 6.1: a redirect to a login URL is explicit positive evidence.
    #     Without the redirect signal, 3xx falls through to DYNAMIC below.
    if 300 <= result.status_code < 400:
        if _has_login_redirect_signal(result.body_prefix):
            return CrawlAttemptOutcome.AUTH_REQUIRED
        return CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    # 6. No adapter + no job content → NOT_JOB_SOURCE.
    if not inp.has_adapter:
        return CrawlAttemptOutcome.NOT_JOB_SOURCE

    # 7a. 200 + login wall evidence in body → AUTH_REQUIRED.
    #     Task 6.1: a 200 page that shows a login wall over job content (e.g.
    #     "sign in to continue", "login required") is explicit evidence.
    if result.status_code == 200 and _has_login_evidence(result.body_prefix):
        return CrawlAttemptOutcome.AUTH_REQUIRED

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


# ---------------------------------------------------------------------------
# Public re-exports so callers (e.g. the permission service) can check
# login evidence without importing private helpers.
# ---------------------------------------------------------------------------
has_login_evidence = _has_login_evidence
has_login_redirect_signal = _has_login_redirect_signal
