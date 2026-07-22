from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import JsonValue

GOAL_RUN_ENSURE_ACTIVITY = "goal.ensure_run"
GOAL_RUN_LOAD_ACTIVITY = "goal.load_run"
GOAL_RUN_CHECKPOINT_ACTIVITY = "goal.checkpoint"
GOAL_RUN_REQUEST_REVIEW_ACTIVITY = "goal.request_review"
GOAL_RUN_CLAIM_ACTIVITY = "goal.claim_source"
GOAL_RUN_DISCOVERY_ACTIVITY = "goal.run_discovery"
GOAL_RUN_COMPLETE_SOURCE_ACTIVITY = "goal.complete_source"
GOAL_RUN_FAIL_SOURCE_ACTIVITY = "goal.fail_source"
GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY = "goal.ingest_public_ats"
GOAL_RUN_MATCH_ACTIVITY = "goal.match_jobs"
GOAL_RUN_PREPARE_DRAFTS_ACTIVITY = "goal.prepare_drafts"
GOAL_RUN_DISPATCH_ACTIVITY = "goal.dispatch_goal"
GOAL_RUN_RECONCILE_ACTIVITY = "goal.reconcile_goal"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


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


class GoalRunStepOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    WAITING_REVIEW = "waiting_review"
    PENDING_EXTERNAL = "pending_external"
    BLOCKED_CONFIGURATION = "blocked_configuration"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class GoalRunDiscoveryState(StrEnum):
    READY = "ready"
    WAITING_REVIEW = "waiting_review"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class GoalRunReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class GoalRunMode(StrEnum):
    """Immutable execution boundary for a GoalRun.

    Missing mode fields in older Temporal payloads intentionally deserialize as Gmail
    dispatch so replay can never reinterpret an existing outbound-capable run as a new
    local-only workflow.
    """

    GMAIL_DISPATCH = "gmail_dispatch"
    PRE_APPLICATION_ONLY = "pre_application_only"


@dataclass(frozen=True, slots=True)
class GoalRunProgress:
    source_row_id: UUID | None = None
    crawler_run_id: UUID | None = None
    source_id: str | None = None
    output_manifest_sha256: str | None = None
    crawler_execution_request_id: UUID | None = None
    crawler_execution_result_id: UUID | None = None
    crawler_execution_outbox_event_id: UUID | None = None
    observed_records: int = 0
    inserted_versions: int = 0
    reused_versions: int = 0
    match_snapshot_sha256: str | None = None
    review_item_id: UUID | None = None
    review_snapshot_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class GoalRunInput:
    """Internal workflow input.

    The authenticated operator API constructs this object. None of its runtime identity,
    fencing, continuation, or worker fields are accepted from the public request body.
    """

    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    fencing_token: UUID
    source_id: str | None = None
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH
    max_records: int = 1000
    expected_version: int = 0
    next_phase: GoalRunPhase = GoalRunPhase.INITIALIZING
    continuation_count: int = 0
    resume_attempts: int = 0
    progress: GoalRunProgress = field(default_factory=GoalRunProgress)

    def __post_init__(self) -> None:
        if self.source_id is not None and not _IDENTIFIER.fullmatch(self.source_id):
            raise ValueError("source_id must be a bounded identifier")
        if not 1 <= self.max_records <= 10_000:
            raise ValueError("max_records must be between 1 and 10000")
        if self.expected_version < 0:
            raise ValueError("expected_version must not be negative")
        if not 0 <= self.continuation_count <= 100:
            raise ValueError("continuation_count must be between 0 and 100")
        if not 0 <= self.resume_attempts <= 3:
            raise ValueError("resume_attempts must be between 0 and 3")


@dataclass(frozen=True, slots=True)
class GoalRunEnsureCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    source_id: str | None
    max_records: int
    fencing_token: UUID
    temporal_workflow_id: str
    idempotency_key: str
    trace_id: str
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH


@dataclass(frozen=True, slots=True)
class GoalRunLoadCommand:
    goal_run_id: UUID
    owner_user_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunSnapshot:
    goal_run_id: UUID
    owner_user_id: UUID
    version: int
    fencing_token: UUID
    phase: GoalRunPhase
    status: GoalRunStatus
    source_id: str | None = None
    pending_review_item_id: UUID | None = None
    pending_review_snapshot_sha256: str | None = None
    review_decision: GoalRunReviewDecision | None = None
    last_error_code: str | None = None
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH


@dataclass(frozen=True, slots=True)
class GoalRunCheckpointCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    expected_version: int
    fencing_token: UUID
    phase: GoalRunPhase
    status: GoalRunStatus
    outcome: str
    checkpoint: dict[str, JsonValue]
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class GoalRunCheckpointResult:
    goal_run_id: UUID
    version: int
    phase: GoalRunPhase
    status: GoalRunStatus
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class GoalRunReviewRequestCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    expected_version: int
    fencing_token: UUID
    review_kind: str
    review_payload: dict[str, JsonValue]
    snapshot_sha256: str
    idempotency_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class GoalRunReviewRequestResult:
    goal_run_id: UUID
    review_item_id: UUID
    snapshot_sha256: str
    version: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class GoalRunClaimCommand:
    goal_run_id: UUID
    registry_id: UUID
    source_id: str | None


@dataclass(frozen=True, slots=True)
class GoalRunDiscoveryCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    source_id: str
    source_row_id: UUID
    run_id: UUID
    worker_id: str
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class GoalRunClaimResult:
    registry_id: UUID
    run_id: UUID
    source_row_id: UUID
    source_id: str
    lease_token: UUID
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class GoalRunDiscoveryResult:
    source_row_id: UUID
    run_id: UUID
    state: GoalRunDiscoveryState
    output_manifest_sha256: str | None
    cursor: str | None
    result: str
    crawler_execution_request_id: UUID | None = None
    crawler_execution_result_id: UUID | None = None
    crawler_execution_outbox_event_id: UUID | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.result):
            raise ValueError("discovery result must be a bounded machine identifier")
        if self.output_manifest_sha256 is not None and not _HASH.fullmatch(
            self.output_manifest_sha256
        ):
            raise ValueError("output_manifest_sha256 must be a lowercase sha256 digest")
        if self.state is GoalRunDiscoveryState.READY:
            if (
                self.output_manifest_sha256 is None
                or self.crawler_execution_request_id is None
                or self.crawler_execution_result_id is None
                or self.crawler_execution_outbox_event_id is None
            ):
                raise ValueError("ready discovery requires reviewed result and manifest evidence")
        elif self.output_manifest_sha256 is not None:
            raise ValueError("non-ready discovery must not carry output manifest evidence")


@dataclass(frozen=True, slots=True)
class GoalRunCompleteCommand:
    registry_id: UUID
    source_id: str
    run_id: UUID
    source_row_id: UUID
    worker_id: str
    lease_token: UUID
    output_manifest_sha256: str | None
    cursor: str | None
    result: str


@dataclass(frozen=True, slots=True)
class GoalRunFailCommand:
    registry_id: UUID
    source_id: str
    run_id: UUID
    source_row_id: UUID
    worker_id: str
    lease_token: UUID
    error_code: str


@dataclass(frozen=True, slots=True)
class GoalRunIngestCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    source_id: str
    source_row_id: UUID
    run_id: UUID
    max_records: int


@dataclass(frozen=True, slots=True)
class GoalRunIngestResult:
    observed_records: int
    inserted_versions: int
    reused_versions: int


@dataclass(frozen=True, slots=True)
class GoalRunDomainCommand:
    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    source_id: str
    source_row_id: UUID
    crawler_run_id: UUID
    max_records: int
    expected_match_snapshot_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class GoalRunStepResult:
    outcome: GoalRunStepOutcome
    reason_code: str
    details: dict[str, JsonValue] = field(default_factory=lambda: {})
    output_sha256: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.reason_code):
            raise ValueError("reason_code must be a bounded identifier")
        if self.output_sha256 is not None and not _HASH.fullmatch(self.output_sha256):
            raise ValueError("output_sha256 must be a lowercase sha256 digest")


@dataclass(frozen=True, slots=True)
class GoalRunReviewSignal:
    command_id: UUID
    review_item_id: UUID
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if not _HASH.fullmatch(self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be a lowercase sha256 digest")


@dataclass(frozen=True, slots=True)
class GoalRunResumeSignal:
    command_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunCancelSignal:
    command_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunStatusView:
    goal_run_id: UUID | None
    phase: GoalRunPhase
    status: GoalRunStatus
    version: int
    pending_review_item_id: UUID | None
    last_error_code: str | None
    continuation_count: int
    resume_attempts: int


@dataclass(frozen=True, slots=True)
class GoalRunResult:
    goal_run_id: UUID
    status: GoalRunStatus
    current_phase: GoalRunPhase
    version: int
    source_id: str | None
    source_row_id: UUID | None
    crawler_run_id: UUID | None
    observed_records: int
    inserted_versions: int
    reused_versions: int
    reason_code: str
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH
    completion_kind: str | None = None


__all__ = [
    "GOAL_RUN_CHECKPOINT_ACTIVITY",
    "GOAL_RUN_CLAIM_ACTIVITY",
    "GOAL_RUN_COMPLETE_SOURCE_ACTIVITY",
    "GOAL_RUN_DISCOVERY_ACTIVITY",
    "GOAL_RUN_DISPATCH_ACTIVITY",
    "GOAL_RUN_ENSURE_ACTIVITY",
    "GOAL_RUN_FAIL_SOURCE_ACTIVITY",
    "GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY",
    "GOAL_RUN_LOAD_ACTIVITY",
    "GOAL_RUN_MATCH_ACTIVITY",
    "GOAL_RUN_PREPARE_DRAFTS_ACTIVITY",
    "GOAL_RUN_RECONCILE_ACTIVITY",
    "GOAL_RUN_REQUEST_REVIEW_ACTIVITY",
    "GoalRunCancelSignal",
    "GoalRunCheckpointCommand",
    "GoalRunCheckpointResult",
    "GoalRunClaimCommand",
    "GoalRunClaimResult",
    "GoalRunCompleteCommand",
    "GoalRunDiscoveryCommand",
    "GoalRunDiscoveryResult",
    "GoalRunDiscoveryState",
    "GoalRunDomainCommand",
    "GoalRunEnsureCommand",
    "GoalRunFailCommand",
    "GoalRunIngestCommand",
    "GoalRunIngestResult",
    "GoalRunInput",
    "GoalRunLoadCommand",
    "GoalRunMode",
    "GoalRunPhase",
    "GoalRunProgress",
    "GoalRunResult",
    "GoalRunResumeSignal",
    "GoalRunReviewDecision",
    "GoalRunReviewRequestCommand",
    "GoalRunReviewRequestResult",
    "GoalRunReviewSignal",
    "GoalRunSnapshot",
    "GoalRunStatus",
    "GoalRunStatusView",
    "GoalRunStepOutcome",
    "GoalRunStepResult",
]
