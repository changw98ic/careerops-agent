from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from pydantic import JsonValue

type JsonObject = Mapping[str, JsonValue]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_MAX_TEXT_LENGTH = 4_000


class GoalRunStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    BLOCKED = "blocked"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class GoalRunPhase(StrEnum):
    INITIALIZING = "initializing"
    SELECTING_SOURCE = "selecting_source"
    CLAIMING_SOURCE = "claiming_source"
    DISCOVERY = "discovery"
    COMPLETE_SOURCE = "complete_source"
    CANONICAL_INGEST = "canonical_ingest"
    MATCHING = "matching"
    DRAFT_PREPARATION = "draft_preparation"
    REVIEW = "review"
    DISPATCH = "dispatch"
    RECONCILIATION = "reconciliation"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class GoalRunCheckpointOutcome(StrEnum):
    PROGRESS = "progress"
    WAITING_REVIEW = "waiting_review"
    BLOCKED = "blocked"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FINALIZE = "finalize"


class GoalRunReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class GoalRunRepositoryError(RuntimeError):
    """Sanitized persistence failure; never exposes database messages or SQL text."""

    def __init__(self, reason_code: str, *, sqlstate: str | None = None) -> None:
        _validate_identifier(reason_code, "goal run repository reason_code")
        if sqlstate is not None and not _SQLSTATE.fullmatch(sqlstate):
            raise ValueError("goal run repository sqlstate must be a SQLSTATE code")
        self.reason_code = reason_code
        self.sqlstate = sqlstate
        message = reason_code if sqlstate is None else f"{reason_code}:{sqlstate}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoalRunCreateCommand:
    actor_id: UUID
    idempotency_key: str
    goal_kind: str
    goal: str
    context: JsonObject
    registry_id: UUID | None
    source_id: str | None
    max_records: int
    temporal_workflow_id: str | None
    trace_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.idempotency_key, "goal run idempotency_key")
        _validate_identifier(self.goal_kind, "goal run goal_kind")
        _validate_text(self.goal, "goal run goal")
        _validate_json_object(self.context, "goal run context")
        _validate_positive_int(self.max_records, "goal run max_records")
        if self.source_id is not None:
            _validate_identifier(self.source_id, "goal run source_id")
        if self.temporal_workflow_id is not None:
            _validate_identifier(self.temporal_workflow_id, "goal run temporal_workflow_id")
        _validate_identifier(self.trace_id, "goal run trace_id")


@dataclass(frozen=True, slots=True)
class GoalRunCreateLookupQuery:
    actor_id: UUID
    idempotency_key: str

    def __post_init__(self) -> None:
        _validate_identifier(self.idempotency_key, "goal run create lookup idempotency_key")


@dataclass(frozen=True, slots=True)
class GoalRunStatusQuery:
    actor_id: UUID
    goal_run_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunListOwnerQuery:
    actor_id: UUID
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 500:
            raise ValueError("goal run owner list limit must be between 1 and 500")
        if self.cursor is not None:
            _validate_identifier(self.cursor, "goal run owner list cursor")


@dataclass(frozen=True, slots=True)
class GoalRunVersionedCommand:
    actor_id: UUID
    goal_run_id: UUID
    expected_version: int
    fencing_token: UUID
    idempotency_key: str
    trace_id: str

    def __post_init__(self) -> None:
        _validate_positive_int(self.expected_version, "goal run expected_version")
        _validate_identifier(self.idempotency_key, "goal run idempotency_key")
        _validate_identifier(self.trace_id, "goal run trace_id")


@dataclass(frozen=True, slots=True)
class GoalRunCheckpointCommand(GoalRunVersionedCommand):
    phase: GoalRunPhase
    status: GoalRunStatus
    outcome: GoalRunCheckpointOutcome
    checkpoint: JsonObject
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        GoalRunVersionedCommand.__post_init__(self)
        _validate_json_object(self.checkpoint, "goal run checkpoint")
        if self.last_error_code is not None:
            _validate_identifier(self.last_error_code, "goal run last_error_code")
        is_finalize = self.outcome is GoalRunCheckpointOutcome.FINALIZE
        if self.status in _TERMINAL_STATUSES and not is_finalize:
            raise ValueError("terminal checkpoint status requires finalize outcome")
        if self.status not in _TERMINAL_STATUSES and is_finalize:
            raise ValueError("finalize outcome requires terminal checkpoint status")


@dataclass(frozen=True, slots=True)
class GoalRunReviewRequestCommand(GoalRunVersionedCommand):
    review_item_id: UUID
    review_snapshot_sha256: str
    review_kind: str
    review_payload: JsonObject

    def __post_init__(self) -> None:
        GoalRunVersionedCommand.__post_init__(self)
        _validate_sha256(self.review_snapshot_sha256, "goal run review_snapshot_sha256")
        _validate_identifier(self.review_kind, "goal run review_kind")
        _validate_json_object(self.review_payload, "goal run review_payload")


@dataclass(frozen=True, slots=True)
class GoalRunReviewDecisionCommand(GoalRunVersionedCommand):
    review_item_id: UUID
    review_snapshot_sha256: str
    decision: GoalRunReviewDecision
    reason: str

    def __post_init__(self) -> None:
        GoalRunVersionedCommand.__post_init__(self)
        _validate_sha256(self.review_snapshot_sha256, "goal run review_snapshot_sha256")
        _validate_text(self.reason, "goal run review decision reason")


@dataclass(frozen=True, slots=True)
class GoalRunCancelCommand(GoalRunVersionedCommand):
    reason: str

    def __post_init__(self) -> None:
        GoalRunVersionedCommand.__post_init__(self)
        _validate_text(self.reason, "goal run cancel reason")


@dataclass(frozen=True, slots=True)
class GoalRunResumeCommand(GoalRunVersionedCommand):
    pass


@dataclass(frozen=True, slots=True)
class GoalRunRecord:
    goal_run_id: UUID
    actor_id: UUID
    goal_kind: str
    goal: str
    status: GoalRunStatus
    phase: GoalRunPhase
    version: int
    fencing_token: UUID
    context: JsonObject
    checkpoint: JsonObject
    idempotency_key: str
    trace_id: str
    created_at: datetime
    updated_at: datetime
    registry_id: UUID | None = None
    source_id: str | None = None
    max_records: int = 1
    temporal_workflow_id: str | None = None
    review_item_id: UUID | None = None
    review_snapshot_sha256: str | None = None
    review_decision: GoalRunReviewDecision | None = None
    review_kind: str | None = None
    review_payload: JsonObject | None = None
    last_error_code: str | None = None
    cancelled_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.goal_kind, "goal run goal_kind")
        _validate_text(self.goal, "goal run goal")
        _validate_positive_int(self.version, "goal run version")
        _validate_json_object(self.context, "goal run context")
        _validate_json_object(self.checkpoint, "goal run checkpoint")
        _validate_identifier(self.idempotency_key, "goal run idempotency_key")
        _validate_identifier(self.trace_id, "goal run trace_id")
        _validate_aware_datetime(self.created_at, "goal run created_at")
        _validate_aware_datetime(self.updated_at, "goal run updated_at")
        _validate_positive_int(self.max_records, "goal run max_records")
        if self.source_id is not None:
            _validate_identifier(self.source_id, "goal run source_id")
        if self.temporal_workflow_id is not None:
            _validate_identifier(self.temporal_workflow_id, "goal run temporal_workflow_id")
        if self.review_snapshot_sha256 is not None:
            _validate_sha256(self.review_snapshot_sha256, "goal run review_snapshot_sha256")
        if self.review_kind is not None:
            _validate_identifier(self.review_kind, "goal run review_kind")
        if self.review_payload is not None:
            _validate_json_object(self.review_payload, "goal run review_payload")
        if self.last_error_code is not None:
            _validate_identifier(self.last_error_code, "goal run last_error_code")
        if self.cancelled_at is not None:
            _validate_aware_datetime(self.cancelled_at, "goal run cancelled_at")
        if self.completed_at is not None:
            _validate_aware_datetime(self.completed_at, "goal run completed_at")


@dataclass(frozen=True, slots=True)
class GoalRunMutationResult:
    record: GoalRunRecord
    idempotency_key: str
    newly_created: bool

    @property
    def goal_run_id(self) -> UUID:
        return self.record.goal_run_id

    @property
    def fencing_token(self) -> UUID:
        return self.record.fencing_token

    def __post_init__(self) -> None:
        _validate_identifier(self.idempotency_key, "goal run mutation idempotency_key")


@dataclass(frozen=True, slots=True)
class GoalRunListPage:
    records: tuple[GoalRunRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if self.next_cursor is not None:
            _validate_identifier(self.next_cursor, "goal run owner list next_cursor")


class GoalRunRepository(Protocol):
    def find_create(self, query: GoalRunCreateLookupQuery) -> GoalRunMutationResult | None: ...

    def create(self, command: GoalRunCreateCommand) -> GoalRunMutationResult: ...

    def status(self, query: GoalRunStatusQuery) -> GoalRunRecord: ...

    def list_owner(self, query: GoalRunListOwnerQuery) -> GoalRunListPage: ...

    def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunMutationResult: ...

    def request_review(self, command: GoalRunReviewRequestCommand) -> GoalRunMutationResult: ...

    def record_review_decision(
        self,
        command: GoalRunReviewDecisionCommand,
    ) -> GoalRunMutationResult: ...

    def cancel(self, command: GoalRunCancelCommand) -> GoalRunMutationResult: ...

    def resume(self, command: GoalRunResumeCommand) -> GoalRunMutationResult: ...


def goal_run_record_from_mapping(value: Mapping[str, object]) -> GoalRunRecord:
    return GoalRunRecord(
        goal_run_id=_uuid(value["goal_run_id"], "goal_run_id"),
        actor_id=_uuid(value["actor_id"], "actor_id"),
        goal_kind=cast("str", value["goal_kind"]),
        goal=cast("str", value["goal"]),
        status=GoalRunStatus(cast("str", value["status"])),
        phase=GoalRunPhase(cast("str", value["phase"])),
        version=int(cast("int", value["version"])),
        fencing_token=_uuid(value["fencing_token"], "fencing_token"),
        context=_json_object(value.get("context") or {}, "context"),
        checkpoint=_json_object(value.get("checkpoint") or {}, "checkpoint"),
        idempotency_key=cast("str", value["idempotency_key"]),
        trace_id=cast("str", value["trace_id"]),
        created_at=_datetime(value["created_at"], "created_at"),
        updated_at=_datetime(value["updated_at"], "updated_at"),
        registry_id=_optional_uuid(value.get("registry_id"), "registry_id"),
        source_id=cast("str | None", value.get("source_id")),
        max_records=int(cast("int", value.get("max_records") or 1)),
        temporal_workflow_id=cast("str | None", value.get("temporal_workflow_id")),
        review_item_id=_optional_uuid(value.get("review_item_id"), "review_item_id"),
        review_snapshot_sha256=cast("str | None", value.get("review_snapshot_sha256")),
        review_decision=_optional_review_decision(value.get("review_decision")),
        review_kind=cast("str | None", value.get("review_kind")),
        review_payload=_optional_json_object(value.get("review_payload"), "review_payload"),
        last_error_code=cast("str | None", value.get("last_error_code")),
        cancelled_at=_optional_datetime(value.get("cancelled_at"), "cancelled_at"),
        completed_at=_optional_datetime(value.get("completed_at"), "completed_at"),
    )


def goal_run_mutation_result_from_mapping(value: Mapping[str, object]) -> GoalRunMutationResult:
    record_value = value.get("record", value)
    if not isinstance(record_value, Mapping):
        raise ValueError("goal run mutation result record must be an object")
    return GoalRunMutationResult(
        record=goal_run_record_from_mapping(cast("Mapping[str, object]", record_value)),
        idempotency_key=cast(
            "str",
            value.get("idempotency_key") or record_value["idempotency_key"],
        ),
        newly_created=bool(value.get("newly_created", False)),
    )


def goal_run_list_page_from_mapping(value: Mapping[str, object]) -> GoalRunListPage:
    raw_records = value.get("records", ())
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, str | bytes):
        raise ValueError("goal run owner list records must be a sequence")
    record_values = cast("Sequence[object]", raw_records)
    records = tuple(
        goal_run_record_from_mapping(cast("Mapping[str, object]", item))
        for item in record_values
        if isinstance(item, Mapping)
    )
    if len(records) != len(record_values):
        raise ValueError("goal run owner list records must contain objects")
    return GoalRunListPage(
        records=records,
        next_cursor=cast("str | None", value.get("next_cursor")),
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


def _datetime(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError(f"{label} must be a datetime")
    _validate_aware_datetime(parsed, label)
    return parsed


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _datetime(value, label)


def _optional_review_decision(value: object) -> GoalRunReviewDecision | None:
    if value is None:
        return None
    return GoalRunReviewDecision(cast("str", value))


def _optional_json_object(value: object, label: str) -> JsonObject | None:
    if value is None:
        return None
    return _json_object(value, label)


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    source = cast("Mapping[object, object]", value)
    copied: dict[str, JsonValue] = {}
    for key, item in source.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} must use non-empty string keys")
        _validate_json_value(item, label)
        copied[key] = cast("JsonValue", item)
    return copied


def _validate_json_object(value: Mapping[str, object], label: str) -> None:
    source = cast("Mapping[object, object]", value)
    for key, item in source.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} must use non-empty string keys")
        _validate_json_value(item, label)


def _validate_json_value(value: object, label: str) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    if isinstance(value, Mapping):
        _validate_json_object(cast("Mapping[str, object]", value), label)
        return
    if isinstance(value, list | tuple):
        sequence = cast("list[object] | tuple[object, ...]", value)
        for item in sequence:
            _validate_json_value(item, label)
        return
    raise ValueError(f"{label} must be JSON serializable")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded machine identifier")


def _validate_sha256(value: str, label: str) -> None:
    if not _SHA256_HEX.fullmatch(value):
        raise ValueError(f"{label} must be a sha256 hex digest")


def _validate_text(value: str, label: str) -> None:
    if not value.strip() or len(value) > _MAX_TEXT_LENGTH:
        raise ValueError(f"{label} must be non-empty and bounded")


def _validate_positive_int(value: int, label: str) -> None:
    if value < 1:
        raise ValueError(f"{label} must be positive")


def _validate_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


_TERMINAL_STATUSES = frozenset(
    {
        GoalRunStatus.COMPLETED,
        GoalRunStatus.FAILED,
        GoalRunStatus.CANCELLED,
        GoalRunStatus.REJECTED,
    }
)


__all__ = [
    "GoalRunCancelCommand",
    "GoalRunCheckpointCommand",
    "GoalRunCheckpointOutcome",
    "GoalRunCreateCommand",
    "GoalRunCreateLookupQuery",
    "GoalRunListOwnerQuery",
    "GoalRunListPage",
    "GoalRunMutationResult",
    "GoalRunPhase",
    "GoalRunRecord",
    "GoalRunRepository",
    "GoalRunRepositoryError",
    "GoalRunResumeCommand",
    "GoalRunReviewDecision",
    "GoalRunReviewDecisionCommand",
    "GoalRunReviewRequestCommand",
    "GoalRunStatus",
    "GoalRunStatusQuery",
    "JsonObject",
    "goal_run_list_page_from_mapping",
    "goal_run_mutation_result_from_mapping",
    "goal_run_record_from_mapping",
]
