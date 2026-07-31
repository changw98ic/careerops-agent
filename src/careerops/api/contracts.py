from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(StrictContract):
    status: Literal["ok"] = "ok"
    service: Literal["careerops"] = "careerops"
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class CheckStatus(StrEnum):
    OK = "ok"
    DISABLED = "disabled"
    NOT_READY = "not_ready"


class ReadinessResponse(StrictContract):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


# ---------------------------------------------------------------------------
# Error codes — canonical set used across all API endpoints
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Canonical error codes for the CareerOps API."""

    RATE_LIMITED = "RATE_LIMITED"
    CANDIDATE_PROFILE_REQUIRED = "CANDIDATE_PROFILE_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    FORBIDDEN = "FORBIDDEN"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Business-lifecycle error codes (end-to-end-career-application-loop, task 1.5).
    # Each code maps to a fixed HTTP status via the matching CareerOpsHTTPException
    # subclass in careerops/api/errors.py.
    INVALID_STATE = "INVALID_STATE"
    STALE_PAYLOAD = "STALE_PAYLOAD"
    UNAVAILABLE_DEPENDENCY = "UNAVAILABLE_DEPENDENCY"
    DENIED_POLICY = "DENIED_POLICY"
    UNRESOLVED_EMAIL_LINK = "UNRESOLVED_EMAIL_LINK"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    SMART_INTAKE_DISABLED = "SMART_INTAKE_DISABLED"
    SMART_PREVIEW_NOT_FOUND = "SMART_PREVIEW_NOT_FOUND"
    SMART_PREVIEW_IN_PROGRESS = "SMART_PREVIEW_IN_PROGRESS"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    STALE_SMART_INTAKE_PREVIEW = "STALE_SMART_INTAKE_PREVIEW"
    SMART_PREVIEW_EXPIRED = "SMART_PREVIEW_EXPIRED"


class ErrorBody(StrictContract):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: JsonValue = None
    trace_id: str


class ErrorResponse(StrictContract):
    error: ErrorBody
