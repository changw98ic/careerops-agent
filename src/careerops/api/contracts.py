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


class ErrorBody(StrictContract):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str
    details: JsonValue = None
    trace_id: str


class ErrorResponse(StrictContract):
    error: ErrorBody
