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

    UNAUTHORIZED = "UNAUTHORIZED"
    CSRF_REJECTED = "CSRF_REJECTED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    RATE_LIMITED = "RATE_LIMITED"
    BOOTSTRAP_CLOSED = "BOOTSTRAP_CLOSED"
    CANDIDATE_PROFILE_REQUIRED = "CANDIDATE_PROFILE_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DEPENDENCY_NOT_READY = "DEPENDENCY_NOT_READY"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    FORBIDDEN = "FORBIDDEN"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorBody(StrictContract):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: JsonValue = None
    trace_id: str


class ErrorResponse(StrictContract):
    error: ErrorBody
