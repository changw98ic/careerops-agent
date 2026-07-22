from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


class GoalRunCrawlerDiscoveryState(StrEnum):
    READY = "ready"
    WAITING_REVIEW = "waiting_review"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class GoalRunCrawlerDiscoveryRepositoryError(RuntimeError):
    """Sanitized reviewed-crawler discovery persistence failure."""

    def __init__(self, reason_code: str, *, sqlstate: str | None = None) -> None:
        _validate_identifier(reason_code, "goal run crawler discovery reason_code")
        if sqlstate is not None and not _SQLSTATE.fullmatch(sqlstate):
            raise ValueError("goal run crawler discovery sqlstate must be a SQLSTATE code")
        self.reason_code = reason_code
        self.sqlstate = sqlstate
        message = reason_code if sqlstate is None else f"{reason_code}:{sqlstate}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoalRunReviewedCrawlerResultQuery:
    actor_id: UUID
    goal_run_id: UUID
    registry_id: UUID
    source_id: str

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id, "goal run crawler discovery source_id")


@dataclass(frozen=True, slots=True)
class GoalRunReviewedCrawlerResult:
    state: GoalRunCrawlerDiscoveryState
    registry_id: UUID | None = None
    source_row_id: UUID | None = None
    source_id: str | None = None
    request_id: UUID | None = None
    result_id: UUID | None = None
    outbox_event_id: UUID | None = None
    reviewed_plan_sha256: str | None = None
    source_sha256: str | None = None
    command_sha256: str | None = None
    output_dir: str | None = None
    adapter: str | None = None
    completed_at: datetime | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.source_id is not None:
            _validate_source_id(self.source_id, "goal run crawler discovery source_id")
        for value, label in (
            (self.reviewed_plan_sha256, "reviewed_plan_sha256"),
            (self.source_sha256, "source_sha256"),
            (self.command_sha256, "command_sha256"),
        ):
            if value is not None:
                _validate_sha256(value, f"goal run crawler discovery {label}")
        if self.output_dir is not None:
            _validate_safe_relative_path(self.output_dir, "goal run crawler discovery output_dir")
        if self.adapter is not None:
            _validate_identifier(self.adapter, "goal run crawler discovery adapter")
        if self.completed_at is not None:
            _validate_aware_datetime(self.completed_at, "goal run crawler discovery completed_at")
        if self.error_code is not None:
            _validate_identifier(self.error_code, "goal run crawler discovery error_code")

        if self.state is GoalRunCrawlerDiscoveryState.READY:
            _require_present(self.registry_id, "ready crawler discovery registry_id")
            _require_present(self.source_row_id, "ready crawler discovery source_row_id")
            _require_present(self.source_id, "ready crawler discovery source_id")
            _require_present(self.request_id, "ready crawler discovery request_id")
            _require_present(self.result_id, "ready crawler discovery result_id")
            _require_present(self.outbox_event_id, "ready crawler discovery outbox_event_id")
            _require_present(
                self.reviewed_plan_sha256,
                "ready crawler discovery reviewed_plan_sha256",
            )
            _require_present(self.source_sha256, "ready crawler discovery source_sha256")
            _require_present(self.command_sha256, "ready crawler discovery command_sha256")
            _require_present(self.output_dir, "ready crawler discovery output_dir")
            _require_present(self.adapter, "ready crawler discovery adapter")
            _require_present(self.completed_at, "ready crawler discovery completed_at")
        elif self.state in (
            GoalRunCrawlerDiscoveryState.FAILED,
            GoalRunCrawlerDiscoveryState.RECONCILIATION_REQUIRED,
        ):
            _require_present(self.error_code, f"{self.state.value} crawler discovery error_code")


class GoalRunCrawlerDiscoveryRepository(Protocol):
    def reviewed_result(
        self,
        query: GoalRunReviewedCrawlerResultQuery,
    ) -> GoalRunReviewedCrawlerResult: ...


def goal_run_reviewed_crawler_result_from_mapping(
    value: Mapping[str, object],
) -> GoalRunReviewedCrawlerResult:
    return GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState(cast("str", value["state"])),
        registry_id=_optional_uuid(value.get("registry_id"), "registry_id"),
        source_row_id=_optional_uuid(value.get("source_row_id"), "source_row_id"),
        source_id=cast("str | None", value.get("source_id")),
        request_id=_optional_uuid(value.get("request_id"), "request_id"),
        result_id=_optional_uuid(value.get("result_id"), "result_id"),
        outbox_event_id=_optional_uuid(value.get("outbox_event_id"), "outbox_event_id"),
        reviewed_plan_sha256=cast("str | None", value.get("reviewed_plan_sha256")),
        source_sha256=cast("str | None", value.get("source_sha256")),
        command_sha256=cast("str | None", value.get("command_sha256")),
        output_dir=cast("str | None", value.get("output_dir")),
        adapter=cast("str | None", value.get("adapter")),
        completed_at=_optional_datetime(value.get("completed_at"), "completed_at"),
        error_code=cast("str | None", value.get("error_code")),
    )


def _uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise ValueError(f"{label} must be a UUID")


def _optional_uuid(value: object, label: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, label)


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError(f"{label} must be a datetime")
    _validate_aware_datetime(parsed, label)
    return parsed


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_source_id(value: str, label: str) -> None:
    if not _SOURCE_ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded crawler source identifier")


def _validate_sha256(value: str, label: str) -> None:
    if not _SHA256_HEX.fullmatch(value):
        raise ValueError(f"{label} must be a sha256 hex digest")


def _validate_safe_relative_path(value: str, label: str) -> None:
    if (
        not value
        or _SAFE_RELATIVE_PATH.fullmatch(value) is None
        or value.startswith("/")
        or ".." in value.split("/")
    ):
        raise ValueError(f"{label} must be a safe relative path")


def _validate_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _require_present(value: object | None, label: str) -> None:
    if value is None:
        raise ValueError(f"{label} is required")


__all__ = [
    "GoalRunCrawlerDiscoveryRepository",
    "GoalRunCrawlerDiscoveryRepositoryError",
    "GoalRunCrawlerDiscoveryState",
    "GoalRunReviewedCrawlerResult",
    "GoalRunReviewedCrawlerResultQuery",
    "goal_run_reviewed_crawler_result_from_mapping",
]
