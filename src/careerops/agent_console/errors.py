"""Agent-console error codes (api-contract.md section 2).

Canonical, stable error codes for the Agent-First Console Experience.
These are a separate namespace from the existing ``careerops.api.contracts.ErrorCode``
and are not mixed into the existing error envelope.
"""

from __future__ import annotations

from enum import StrEnum


class AgentConsoleErrorCode(StrEnum):
    """Canonical error codes for the Agent Console API contract.

    Each code maps to a fixed HTTP status through the matching exception
    class in this package.  The ``message`` value is safe for a candidate
    and never contains SQL, provider output, URLs, credentials, raw source
    text, or another candidate's existence.
    """

    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_FOUND = "NOT_FOUND"
    POLICY_DENIED = "POLICY_DENIED"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    PREREQUISITE_BLOCKED = "PREREQUISITE_BLOCKED"
    CONTEXT_STALE = "CONTEXT_STALE"
    PREFLIGHT_REQUIRED = "PREFLIGHT_REQUIRED"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    STALE = "STALE"
    NOT_RETRYABLE = "NOT_RETRYABLE"
    STOP_NOT_ALLOWED = "STOP_NOT_ALLOWED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CSRF_DENIED = "CSRF_DENIED"
    ORIGIN_DENIED = "ORIGIN_DENIED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CURSOR_INVALID = "CURSOR_INVALID"
    AUDIT_FAILED = "AUDIT_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_SAFE_FAILURE = "INTERNAL_SAFE_FAILURE"
