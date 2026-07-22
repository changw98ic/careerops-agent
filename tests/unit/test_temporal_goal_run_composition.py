from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from careerops.application.goal_run import (
    GoalRunPhase as AppGoalRunPhase,
)
from careerops.application.goal_run import (
    GoalRunRecord,
)
from careerops.application.goal_run import (
    GoalRunStatus as AppGoalRunStatus,
)
from careerops.application.goal_run_composition import canonical_json_hash
from careerops.infrastructure.database.goal_run_composition import (
    GoalRunCandidateQuery,
    GoalRunDiscoveredJobCandidate,
    GoalRunDispatchGmailCommand,
    GoalRunGmailCompositionRecord,
    GoalRunGmailCompositionRepositoryError,
    GoalRunGmailInspection,
    GoalRunInspectGmailQuery,
    GoalRunPrepareGmailCommand,
    GoalRunRecordGmailMatchCommand,
)
from careerops.infrastructure.temporal.goal_run_repository import (
    PostgresGoalRunActivityRepository,
)
from careerops.workflows.goal_run_contracts import (
    GoalRunDomainCommand,
    GoalRunStepOutcome,
)

OWNER_ID = UUID("10000000-0000-0000-0000-000000000001")
GOAL_RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
REGISTRY_ID = UUID("30000000-0000-0000-0000-000000000001")
SOURCE_ROW_ID = UUID("40000000-0000-0000-0000-000000000001")
CRAWLER_RUN_ID = UUID("50000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("60000000-0000-0000-0000-000000000001")
LOW_CANONICAL_ID = UUID("70000000-0000-0000-0000-000000000001")
HIGH_CANONICAL_ID = UUID("70000000-0000-0000-0000-000000000002")
LOW_POSTING_ID = UUID("80000000-0000-0000-0000-000000000001")
HIGH_POSTING_ID = UUID("80000000-0000-0000-0000-000000000002")
LOW_VERSION_ID = UUID("90000000-0000-0000-0000-000000000001")
HIGH_VERSION_ID = UUID("90000000-0000-0000-0000-000000000002")
COMPOSITION_ID = UUID("a0000000-0000-0000-0000-000000000001")
ACTION_INTENT_ID = UUID("a1000000-0000-0000-0000-000000000001")
PAYLOAD_VERSION_ID = UUID("a2000000-0000-0000-0000-000000000001")
APPROVAL_REQUEST_ID = UUID("a3000000-0000-0000-0000-000000000001")
OUTBOX_EVENT_ID = UUID("a4000000-0000-0000-0000-000000000001")
RESERVATION_ID = UUID("a5000000-0000-0000-0000-000000000001")
DB_ACTION_PAYLOAD_HASH = "d" * 64
RECEIPT_PAYLOAD_HASH = "e" * 64


@dataclass(slots=True)
class _Harness:
    record: GoalRunRecord
    candidates: Sequence[GoalRunDiscoveredJobCandidate] = field(default_factory=tuple)
    match_record: GoalRunGmailCompositionRecord = field(
        default_factory=lambda: _composition_record()
    )
    prepare_record: GoalRunGmailCompositionRecord = field(
        default_factory=lambda: _composition_record(
            state="draft_prepared",
            receipt_state="created",
            payload_hash=DB_ACTION_PAYLOAD_HASH,
            action_intent_id=ACTION_INTENT_ID,
            payload_version_id=PAYLOAD_VERSION_ID,
            approval_request_id=APPROVAL_REQUEST_ID,
        )
    )
    dispatch_record: GoalRunGmailCompositionRecord = field(
        default_factory=lambda: _composition_record(
            state="dispatch_enqueued",
            receipt_state="created",
            payload_hash=DB_ACTION_PAYLOAD_HASH,
            action_intent_id=ACTION_INTENT_ID,
            payload_version_id=PAYLOAD_VERSION_ID,
            approval_request_id=APPROVAL_REQUEST_ID,
            outbox_event_id=OUTBOX_EVENT_ID,
            reservation_id=RESERVATION_ID,
        )
    )
    inspection: GoalRunGmailInspection | None = None
    status_queries: list[object] = field(default_factory=list)
    candidate_queries: list[GoalRunCandidateQuery] = field(default_factory=list)
    match_commands: list[GoalRunRecordGmailMatchCommand] = field(default_factory=list)
    prepare_commands: list[GoalRunPrepareGmailCommand] = field(default_factory=list)
    dispatch_commands: list[GoalRunDispatchGmailCommand] = field(default_factory=list)
    inspect_queries: list[GoalRunInspectGmailQuery] = field(default_factory=list)


class _FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_exc_info: object) -> None:
        return None


class _FakeEngine:
    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()


class _FakeGoalRunRepository:
    harness: _Harness

    def __init__(self, _connection: object) -> None:
        pass

    def status(self, query: object) -> GoalRunRecord:
        self.harness.status_queries.append(query)
        return self.harness.record


class _FakeCompositionRepository:
    harness: _Harness

    def __init__(self, _connection: object) -> None:
        pass

    def list_candidates(
        self,
        query: GoalRunCandidateQuery,
    ) -> tuple[GoalRunDiscoveredJobCandidate, ...]:
        self.harness.candidate_queries.append(query)
        return tuple(self.harness.candidates)

    def record_match(
        self,
        command: GoalRunRecordGmailMatchCommand,
    ) -> GoalRunGmailCompositionRecord:
        self.harness.match_commands.append(command)
        return self.harness.match_record

    def prepare_gmail(self, command: GoalRunPrepareGmailCommand) -> GoalRunGmailCompositionRecord:
        self.harness.prepare_commands.append(command)
        return self.harness.prepare_record

    def dispatch_gmail(
        self,
        command: GoalRunDispatchGmailCommand,
    ) -> GoalRunGmailCompositionRecord:
        self.harness.dispatch_commands.append(command)
        return self.harness.dispatch_record

    def inspect_gmail(self, query: GoalRunInspectGmailQuery) -> GoalRunGmailInspection:
        self.harness.inspect_queries.append(query)
        assert self.harness.inspection is not None
        return self.harness.inspection


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    value = _Harness(record=_goal_run_record(context=_context()), candidates=_candidates())
    _FakeGoalRunRepository.harness = value
    _FakeCompositionRepository.harness = value
    monkeypatch.setattr(
        "careerops.infrastructure.temporal.goal_run_repository.PostgresGoalRunRepository",
        _FakeGoalRunRepository,
    )
    monkeypatch.setattr(
        "careerops.infrastructure.temporal.goal_run_repository.PostgresGoalRunCompositionRepository",
        _FakeCompositionRepository,
    )
    yield value


def test_match_jobs_selects_best_candidate_from_nested_goal_context(
    harness: _Harness,
) -> None:
    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(_command())

    assert result.outcome is GoalRunStepOutcome.SUCCEEDED
    assert result.reason_code == "GMAIL_MATCH_RECORDED"
    assert len(harness.match_commands) == 1
    recorded = harness.match_commands[0]
    assert recorded.canonical_job_id == HIGH_CANONICAL_ID
    assert recorded.job_posting_id == HIGH_POSTING_ID
    assert recorded.job_posting_version_id == HIGH_VERSION_ID
    assert recorded.match_score == 1.0
    assert recorded.match_reasons == {
        "matched_keywords": ["agents", "distributed systems", "python"],
        "reason_codes": ["MEETS_MIN_SCORE", "ALL_INCLUDE_KEYWORDS_MATCHED"],
    }
    assert result.output_sha256 == recorded.match_snapshot_sha256
    assert result.details["canonical_job_id"] == str(HIGH_CANONICAL_ID)
    assert result.details["receipt_state"] == "created"
    assert harness.candidate_queries == [
        GoalRunCandidateQuery(
            actor_id=OWNER_ID,
            goal_run_id=GOAL_RUN_ID,
            source_row_id=SOURCE_ROW_ID,
            crawler_run_id=CRAWLER_RUN_ID,
            limit=25,
        )
    ]


def test_match_jobs_records_match_only_without_preparing_or_dispatching(
    harness: _Harness,
) -> None:
    PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(_command())

    assert len(harness.match_commands) == 1
    assert harness.prepare_commands == []
    assert harness.dispatch_commands == []
    assert harness.inspect_queries == []


def test_prepare_drafts_builds_exact_gmail_envelope_and_review_digest(
    harness: _Harness,
) -> None:
    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).prepare_drafts(_command())

    assert result.outcome is GoalRunStepOutcome.SUCCEEDED
    assert result.reason_code == "GMAIL_DRAFT_PREPARED"
    assert len(harness.match_commands) == 1
    assert len(harness.prepare_commands) == 1
    prepared = harness.prepare_commands[0]
    assert prepared.payload_hash is None
    assert prepared.target["target_host"] == "gmail.googleapis.com"
    assert prepared.target["channel"] == "gmail:send"
    assert prepared.target["recipient_sha256"]
    assert prepared.target["subject_sha256"]
    assert prepared.target["body_sha256"]
    assert prepared.payload["sender"] == "candidate@example.com"
    assert prepared.payload["recipient"] == "recruiting@example.com"
    assert prepared.payload["subject"] == "Application for Agent Platform Engineer"
    assert prepared.payload["payload_hash"]
    assert prepared.attachment_refs[0]["filename"] == "resume.pdf"

    assert result.details["payload_hash"] == DB_ACTION_PAYLOAD_HASH
    assert result.details["action_intent_id"] == str(ACTION_INTENT_ID)
    assert result.details["payload_version_id"] == str(PAYLOAD_VERSION_ID)
    assert result.details["approval_request_id"] == str(APPROVAL_REQUEST_ID)
    exact_payload = cast(dict[str, object], result.details["exact_payload"])
    assert exact_payload["gmail_message_payload_hash"] == prepared.payload["payload_hash"]
    assert exact_payload["action_payload_hash"] == DB_ACTION_PAYLOAD_HASH
    assert result.output_sha256 == canonical_json_hash(result.details)


def test_dispatch_result_never_claims_provider_io(harness: _Harness) -> None:
    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).inspect_dispatch(
        _command()
    )

    assert result.outcome is GoalRunStepOutcome.SUCCEEDED
    assert result.reason_code == "GMAIL_DISPATCH_ENQUEUED"
    assert result.output_sha256 == DB_ACTION_PAYLOAD_HASH
    assert result.details == {
        "composition_id": str(COMPOSITION_ID),
        "action_intent_id": str(ACTION_INTENT_ID),
        "payload_version_id": str(PAYLOAD_VERSION_ID),
        "payload_hash": DB_ACTION_PAYLOAD_HASH,
        "outbox_event_id": str(OUTBOX_EVENT_ID),
        "reservation_id": str(RESERVATION_ID),
        "receipt_state": "created",
        "provider_io_performed": False,
    }
    assert harness.dispatch_commands == [
        GoalRunDispatchGmailCommand(actor_id=OWNER_ID, goal_run_id=GOAL_RUN_ID)
    ]


@pytest.mark.parametrize(
    ("inspection_kwargs", "expected_outcome", "expected_reason"),
    (
        (
            {"outbox_status": "pending"},
            GoalRunStepOutcome.PENDING_EXTERNAL,
            "GMAIL_SEND_RECEIPT_PENDING",
        ),
        (
            {"outbox_status": "leased"},
            GoalRunStepOutcome.PENDING_EXTERNAL,
            "GMAIL_SEND_RECEIPT_PENDING",
        ),
        (
            {"outbox_status": "published"},
            GoalRunStepOutcome.PENDING_EXTERNAL,
            "GMAIL_SEND_RECEIPT_PENDING",
        ),
        (
            {"outbox_status": "published", "provider_state": "sent_confirmed"},
            GoalRunStepOutcome.SUCCEEDED,
            "GMAIL_SEND_CONFIRMED",
        ),
        (
            {"outbox_status": "published", "provider_state": "ambiguous"},
            GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            "GMAIL_SEND_AMBIGUOUS",
        ),
        (
            {
                "outbox_status": "published",
                "provider_state": "ambiguous",
                "reconciliation_status": "queued",
            },
            GoalRunStepOutcome.PENDING_EXTERNAL,
            "GMAIL_SEND_RECEIPT_PENDING",
        ),
        (
            {"outbox_status": "failed"},
            GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            "GMAIL_SEND_RECONCILIATION_REQUIRED",
        ),
        (
            {"outbox_status": "published", "reconciliation_status": "resolved"},
            GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            "GMAIL_SEND_RESOLVED_WITHOUT_RECEIPT",
        ),
    ),
)
def test_reconciliation_maps_external_states(
    harness: _Harness,
    inspection_kwargs: dict[str, str],
    expected_outcome: GoalRunStepOutcome,
    expected_reason: str,
) -> None:
    inspection = _inspection(**inspection_kwargs)
    harness.inspection = inspection

    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).inspect_reconciliation(
        _command()
    )

    assert result.outcome is expected_outcome
    assert result.reason_code == expected_reason
    assert result.output_sha256 == RECEIPT_PAYLOAD_HASH
    assert result.details["provider_state"] == inspection.provider_state
    assert result.details["reconciliation_status"] == inspection.reconciliation_status
    assert harness.inspect_queries == [
        GoalRunInspectGmailQuery(actor_id=OWNER_ID, goal_run_id=GOAL_RUN_ID)
    ]


def test_missing_config_fails_closed_before_candidate_query(harness: _Harness) -> None:
    harness.record = _goal_run_record(context={})

    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(_command())

    assert result.outcome is GoalRunStepOutcome.BLOCKED_CONFIGURATION
    assert result.reason_code == "BLOCKED_MISSING_CONFIGURATION"
    assert harness.candidate_queries == []
    assert harness.match_commands == []


def test_no_candidates_fail_closed_without_recording_match(harness: _Harness) -> None:
    harness.candidates = ()

    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).prepare_drafts(_command())

    assert result.outcome is GoalRunStepOutcome.BLOCKED_CONFIGURATION
    assert result.reason_code == "BLOCKED_NO_DISCOVERED_JOBS"
    assert len(harness.candidate_queries) == 1
    assert harness.match_commands == []
    assert harness.prepare_commands == []


def test_preapplication_prepare_blocks_match_snapshot_drift_without_gmail_side_effects(
    harness: _Harness,
) -> None:
    harness.record = _goal_run_record(context=_preapplication_context())

    matched = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(
        _command()
    )
    assert matched.outcome is GoalRunStepOutcome.SUCCEEDED
    assert matched.output_sha256 is not None

    # Keep the same selected canonical job but change one scoring dimension between
    # MATCHING and artifact preparation. The immutable match digest must bind both steps.
    harness.candidates = tuple(
        replace(
            candidate,
            description="Build Python agents.",
            keywords=("python", "agents"),
        )
        if candidate.canonical_job_id == HIGH_CANONICAL_ID
        else candidate
        for candidate in harness.candidates
    )
    result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).prepare_drafts(
        replace(
            _command(),
            expected_match_snapshot_sha256=matched.output_sha256,
        )
    )

    assert result.outcome is GoalRunStepOutcome.BLOCKED_CONFIGURATION
    assert result.reason_code == "BLOCKED_MATCH_SNAPSHOT_DRIFT"
    assert harness.match_commands == []
    assert harness.prepare_commands == []
    assert harness.dispatch_commands == []
    assert harness.inspect_queries == []


def test_nonretryable_repository_errors_fail_closed(harness: _Harness) -> None:
    class _FailingCompositionRepository(_FakeCompositionRepository):
        def list_candidates(
            self,
            query: GoalRunCandidateQuery,
        ) -> tuple[GoalRunDiscoveredJobCandidate, ...]:
            raise GoalRunGmailCompositionRepositoryError("GOAL_RUN_GMAIL_COMPOSITION_NOT_FOUND")

    _FailingCompositionRepository.harness = harness
    import careerops.infrastructure.temporal.goal_run_repository as module

    old = module.PostgresGoalRunCompositionRepository
    module.PostgresGoalRunCompositionRepository = _FailingCompositionRepository
    try:
        result = PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(_command())
    finally:
        module.PostgresGoalRunCompositionRepository = old

    assert result.outcome is GoalRunStepOutcome.BLOCKED_CONFIGURATION
    assert result.reason_code == "GOAL_RUN_GMAIL_COMPOSITION_NOT_FOUND"


def test_retryable_repository_errors_are_rethrown(harness: _Harness) -> None:
    class _FailingCompositionRepository(_FakeCompositionRepository):
        def list_candidates(
            self,
            query: GoalRunCandidateQuery,
        ) -> tuple[GoalRunDiscoveredJobCandidate, ...]:
            raise GoalRunGmailCompositionRepositoryError(
                "GOAL_RUN_GMAIL_COMPOSITION_RETRY_DEADLOCK"
            )

    _FailingCompositionRepository.harness = harness
    import careerops.infrastructure.temporal.goal_run_repository as module

    old = module.PostgresGoalRunCompositionRepository
    module.PostgresGoalRunCompositionRepository = _FailingCompositionRepository
    try:
        with pytest.raises(GoalRunGmailCompositionRepositoryError):
            PostgresGoalRunActivityRepository(cast(Any, _FakeEngine())).match_jobs(_command())
    finally:
        module.PostgresGoalRunCompositionRepository = old


def _command() -> GoalRunDomainCommand:
    return GoalRunDomainCommand(
        goal_run_id=GOAL_RUN_ID,
        owner_user_id=OWNER_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
        max_records=25,
    )


def _goal_run_record(*, context: dict[str, object]) -> GoalRunRecord:
    return GoalRunRecord(
        goal_run_id=GOAL_RUN_ID,
        actor_id=OWNER_ID,
        goal_kind="job-search",
        goal="Find reviewed jobs.",
        status=AppGoalRunStatus.RUNNING,
        phase=AppGoalRunPhase.MATCHING,
        version=1,
        fencing_token=uuid4(),
        context=cast(Any, context),
        checkpoint={},
        idempotency_key="goal-create",
        trace_id="trace-goal",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        updated_at=datetime(2026, 7, 20, tzinfo=UTC),
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        max_records=25,
        temporal_workflow_id="goal-workflow",
    )


def _context() -> dict[str, object]:
    return {
        "match_config": {
            "include_keywords": ["Python", "Agents", "distributed systems"],
            "exclude_keywords": ["intern", "unpaid"],
            "min_score": 0.67,
        },
        "gmail_dispatch": {
            "candidate_id": "00000000-0000-0000-0000-00000000c001",
            "sender": "candidate@example.com",
            "recipient": "recruiting@example.com",
            "subject": "Application for Agent Platform Engineer",
            "text_body": "Hello team, I am applying for the reviewed role.",
            "attachment_refs": [
                {
                    "object_key": "resume:approved:v1",
                    "filename": "resume.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": "a" * 64,
                }
            ],
        },
    }


def _preapplication_context() -> dict[str, object]:
    return {
        "mode": "pre_application_only",
        "candidate_profile": {
            "candidate_id": "00000000-0000-0000-0000-00000000c001",
            "profile_version": "candidate-profile:test:v1",
            "source_sha256": "a" * 64,
            "headline": "Agent platform engineer",
            "desired_titles": ["Agent Platform Engineer"],
            "skills": ["python", "agents", "distributed systems"],
            "locations": ["remote"],
            "remote_preference": "acceptable",
            "years_experience": 6,
            "excluded_terms": ["intern"],
            "resume": None,
        },
        "match_config": {
            "include_keywords": ["python", "agents"],
            "exclude_keywords": ["unpaid"],
            "min_score": 0.1,
            "max_applications": 1,
        },
    }


def _candidates() -> tuple[GoalRunDiscoveredJobCandidate, ...]:
    return (
        _candidate(
            canonical_job_id=LOW_CANONICAL_ID,
            job_posting_id=LOW_POSTING_ID,
            job_posting_version_id=LOW_VERSION_ID,
            discovered_job_id="low",
            title="Python Engineer",
            description="Python platform role.",
            keywords=("python",),
        ),
        _candidate(
            canonical_job_id=HIGH_CANONICAL_ID,
            job_posting_id=HIGH_POSTING_ID,
            job_posting_version_id=HIGH_VERSION_ID,
            discovered_job_id="high",
            title="Senior Agent Platform Engineer",
            description="Build Python agents and distributed systems.",
            keywords=("python", "agents", "distributed systems"),
        ),
        _candidate(
            canonical_job_id=HIGH_CANONICAL_ID,
            job_posting_id=uuid4(),
            job_posting_version_id=uuid4(),
            discovered_job_id="duplicate-high",
            title="Duplicate Senior Agent Platform Engineer",
            description="Duplicate should be ignored after canonical de-dupe.",
            keywords=("python", "agents", "distributed systems"),
        ),
    )


def _candidate(
    *,
    canonical_job_id: UUID,
    job_posting_id: UUID,
    job_posting_version_id: UUID,
    discovered_job_id: str,
    title: str,
    description: str,
    keywords: tuple[str, ...],
) -> GoalRunDiscoveredJobCandidate:
    return GoalRunDiscoveredJobCandidate(
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
        crawler_run_event_id=EVENT_ID,
        canonical_job_id=canonical_job_id,
        job_posting_id=job_posting_id,
        job_posting_version_id=job_posting_version_id,
        source_id="public-ats",
        discovered_job_id=discovered_job_id,
        company_name="Example AI",
        title=title,
        canonical_url=f"https://jobs.example.com/{discovered_job_id}",
        description=description,
        keywords=keywords,
        evidence_sha256="b" * 64,
        structured_data={},
    )


def _composition_record(
    *,
    state: str = "matched",
    receipt_state: str | None = "created",
    payload_hash: str | None = None,
    action_intent_id: UUID | None = None,
    payload_version_id: UUID | None = None,
    approval_request_id: UUID | None = None,
    outbox_event_id: UUID | None = None,
    reservation_id: UUID | None = None,
) -> GoalRunGmailCompositionRecord:
    return GoalRunGmailCompositionRecord(
        composition_id=COMPOSITION_ID,
        goal_run_id=GOAL_RUN_ID,
        actor_user_id=OWNER_ID,
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
        crawler_run_event_id=EVENT_ID,
        job_posting_id=HIGH_POSTING_ID,
        job_posting_version_id=HIGH_VERSION_ID,
        canonical_job_id=HIGH_CANONICAL_ID,
        state=state,
        receipt_state=receipt_state,
        payload_hash=payload_hash,
        action_intent_id=action_intent_id,
        payload_version_id=payload_version_id,
        approval_request_id=approval_request_id,
        outbox_event_id=outbox_event_id,
        reservation_id=reservation_id,
    )


def _inspection(
    *,
    outbox_status: str,
    provider_state: str | None = None,
    reconciliation_status: str | None = None,
) -> GoalRunGmailInspection:
    return GoalRunGmailInspection(
        goal_run_id=GOAL_RUN_ID,
        state="dispatch_enqueued",
        action_intent_id=ACTION_INTENT_ID,
        payload_version_id=PAYLOAD_VERSION_ID,
        payload_hash=RECEIPT_PAYLOAD_HASH,
        outbox_event_id=OUTBOX_EVENT_ID,
        outbox_status=outbox_status,
        provider_state=provider_state,
        reconciliation_status=reconciliation_status,
        reconciliation_last_error_code="AMBIGUOUS" if provider_state == "ambiguous" else None,
    )
