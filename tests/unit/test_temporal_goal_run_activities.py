from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from careerops.application.goal_run_crawler_discovery import (
    GoalRunCrawlerDiscoveryState,
    GoalRunReviewedCrawlerResult,
)
from careerops.infrastructure.temporal.activities import (
    _PUBLIC_ATS_MANIFEST,
    DisabledGoalRunControlPlaneRepository,
    GoalRunSourceRegistryActivities,
    _read_registered_artifact,
    _resolve_registered_artifact,
)
from careerops.infrastructure.temporal.goal_run_repository import PostgresGoalRunActivityRepository
from careerops.workflows.goal_run_contracts import (
    GoalRunCheckpointCommand,
    GoalRunDiscoveryCommand,
    GoalRunDiscoveryState,
    GoalRunDomainCommand,
    GoalRunEnsureCommand,
    GoalRunLoadCommand,
    GoalRunPhase,
    GoalRunReviewRequestCommand,
    GoalRunStatus,
)

REGISTRY_ID = uuid4()
SOURCE_ROW_ID = uuid4()
RUN_ID = uuid4()
LEASE_TOKEN = uuid4()
OWNER_USER_ID = uuid4()
REQUEST_ID = uuid4()
RESULT_ID = uuid4()
OUTBOX_EVENT_ID = uuid4()
REVIEWED_PLAN_SHA256 = "a" * 64
SOURCE_SHA256 = "b" * 64
COMMAND_SHA256 = "c" * 64


class _FakeResult:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> dict[str, object]:
        return self._row


class _FakeConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self._row)


class _FakeTransaction:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def __enter__(self) -> _FakeConnection:
        return _FakeConnection(self._row)

    def __exit__(self, *_exc_info: object) -> None:
        return None


class _FakeEngine:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._row)


def test_default_goal_run_activity_uses_postgres_control_plane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = _FakeEngine({})
    monkeypatch.setattr(
        "careerops.infrastructure.temporal.activities.create_database_engine",
        lambda _settings: engine,
    )

    activity = GoalRunSourceRegistryActivities(workspace_root=tmp_path)

    assert isinstance(
        cast(Any, activity)._control_plane_repository,
        PostgresGoalRunActivityRepository,
    )


def test_run_discovery_accepts_producer_shaped_manifest_path(tmp_path: Path) -> None:
    output_dir = "datasets/raw/ats"
    manifest_sha256 = _write_producer_shaped_artifacts(tmp_path, output_dir)
    reviewed = _ready_reviewed_result(output_dir)
    _write_reviewed_receipt(tmp_path, output_dir, reviewed)
    activity = _activity(tmp_path, output_dir)

    result = asyncio.run(
        activity.run_discovery(
            GoalRunDiscoveryCommand(
                goal_run_id=RUN_ID,
                owner_user_id=OWNER_USER_ID,
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                source_row_id=SOURCE_ROW_ID,
                run_id=RUN_ID,
                worker_id="worker-1",
                lease_token=LEASE_TOKEN,
            )
        )
    )

    assert result.output_manifest_sha256 == manifest_sha256
    assert result.state is GoalRunDiscoveryState.READY
    assert result.result == "REVIEWED_CRAWLER_DISCOVERY_READY"
    assert result.crawler_execution_request_id == REQUEST_ID


def test_run_discovery_never_accepts_local_artifacts_without_reviewed_result(
    tmp_path: Path,
) -> None:
    output_dir = "datasets/raw/ats"
    _write_producer_shaped_artifacts(tmp_path, output_dir)
    activity = _activity(
        tmp_path,
        output_dir,
        reviewed=GoalRunReviewedCrawlerResult(
            state=GoalRunCrawlerDiscoveryState.WAITING_REVIEW,
            request_id=REQUEST_ID,
        ),
    )

    result = asyncio.run(activity.run_discovery(_discovery_command()))

    assert result.state is GoalRunDiscoveryState.WAITING_REVIEW
    assert result.output_manifest_sha256 is None
    assert result.crawler_execution_result_id is None


def test_run_discovery_rejects_receipt_not_bound_to_reviewed_request(tmp_path: Path) -> None:
    output_dir = "datasets/raw/ats"
    _write_producer_shaped_artifacts(tmp_path, output_dir)
    reviewed = _ready_reviewed_result(output_dir)
    _write_reviewed_receipt(tmp_path, output_dir, reviewed, request_id=str(uuid4()))
    activity = _activity(tmp_path, output_dir, reviewed=reviewed)

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(activity.run_discovery(_discovery_command()))


def test_registered_artifact_rejects_symlinked_datasets_root(tmp_path: Path) -> None:
    target = tmp_path / "outside-datasets"
    target.mkdir()
    (tmp_path / "datasets").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="datasets root must not be a symbolic link"):
        _resolve_registered_artifact(
            tmp_path,
            "datasets/raw/ats",
            "discovered_public_ats_jobs.jsonl",
        )


def test_registered_artifact_rejects_manifest_path_escape(tmp_path: Path) -> None:
    (tmp_path / "datasets/raw/ats").mkdir(parents=True)
    outside_artifact = tmp_path / "outside.jsonl"
    outside_artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="crawler datasets root"):
        _resolve_registered_artifact(
            tmp_path,
            "datasets/raw/ats",
            str(outside_artifact),
        )


def test_registered_artifact_accepts_workspace_relative_manifest_path(tmp_path: Path) -> None:
    artifact = tmp_path / "datasets/raw/ats/discovered_public_ats_jobs.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")

    assert (
        _resolve_registered_artifact(
            tmp_path,
            "datasets/raw/ats",
            "datasets/raw/ats/discovered_public_ats_jobs.jsonl",
        )
        == artifact
    )


def test_registered_artifact_rejects_symlink_in_artifact_path(tmp_path: Path) -> None:
    real_output = tmp_path / "datasets/raw/real"
    real_output.mkdir(parents=True)
    linked_output = tmp_path / "datasets/raw/ats"
    linked_output.symlink_to(real_output, target_is_directory=True)
    (real_output / "discovered_public_ats_jobs.jsonl").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="symbolic links"):
        _resolve_registered_artifact(
            tmp_path,
            "datasets/raw/ats",
            "discovered_public_ats_jobs.jsonl",
        )


def test_registered_artifact_fd_reader_rejects_symlinked_file(tmp_path: Path) -> None:
    output = tmp_path / "datasets/raw/ats"
    output.mkdir(parents=True)
    target = tmp_path / "datasets/raw/real.jsonl"
    target.write_text("{}", encoding="utf-8")
    (output / "discovered_public_ats_jobs.jsonl").symlink_to(target)

    with pytest.raises(ValueError, match="symbolic links"):
        _read_registered_artifact(
            tmp_path,
            "datasets/raw/ats",
            "discovered_public_ats_jobs.jsonl",
            max_bytes=1024,
            artifact="public ATS jsonl",
        )


def test_match_jobs_blocks_when_configuration_is_missing(tmp_path: Path) -> None:
    activity = _activity(tmp_path, "datasets/raw/ats")
    command = _domain_command()

    result = asyncio.run(activity.match_jobs(command))

    assert _reason_code(result) == "BLOCKED_MISSING_CONFIGURATION"


def test_dispatch_and_reconcile_are_disabled_pending_later_goal_lanes(tmp_path: Path) -> None:
    activity = _activity(tmp_path, "datasets/raw/ats")
    command = _domain_command()

    dispatch = asyncio.run(activity.dispatch_goal(command))
    reconcile = asyncio.run(activity.reconcile_goal(command))

    assert _reason_code(dispatch) == "DISABLED_PENDING_G014_COMPOSITION"
    assert _reason_code(reconcile) == "DISABLED_PENDING_G014_COMPOSITION"


def test_control_plane_activities_delegate_to_injected_repository(tmp_path: Path) -> None:
    activity = _activity(tmp_path, "datasets/raw/ats")
    repository = _RecordingControlPlaneRepository()
    mutable_activity = cast(Any, activity)
    mutable_activity._control_plane_repository = repository

    assert asyncio.run(activity.ensure_run(_ensure_command())) == {"operation": "ensure_run"}
    assert asyncio.run(activity.load_run(_load_command())) == {"operation": "load_run"}
    assert asyncio.run(activity.checkpoint(_checkpoint_command())) == {"operation": "checkpoint"}
    assert asyncio.run(activity.request_review(_review_command())) == {
        "operation": "request_review"
    }
    assert repository.calls == [
        "ensure_run",
        "load_run",
        "checkpoint",
        "request_review",
    ]


def test_postgres_activity_repository_maps_status_to_workflow_snapshot() -> None:
    repository = PostgresGoalRunActivityRepository(cast(Any, _GoalRunEngine(_goal_run_record())))
    command = GoalRunLoadCommand(goal_run_id=RUN_ID, owner_user_id=OWNER_USER_ID)

    snapshot = repository.load_run(command)

    assert snapshot.goal_run_id == RUN_ID
    assert snapshot.owner_user_id == OWNER_USER_ID
    assert snapshot.version == 2
    assert snapshot.source_id == "public-ats"


def test_postgres_activity_repository_rejects_temporal_workflow_mismatch() -> None:
    record = _goal_run_record()
    record["temporal_workflow_id"] = "goal-run-other"
    repository = PostgresGoalRunActivityRepository(cast(Any, _GoalRunEngine(record)))

    with pytest.raises(ValueError, match="temporal workflow mismatch"):
        repository.ensure_run(_ensure_command())


def test_postgres_activity_repository_accepts_dynamic_source_and_rejects_pinned_mismatch() -> None:
    dynamic_record = _goal_run_record()
    dynamic_record["source_id"] = None
    dynamic_repository = PostgresGoalRunActivityRepository(
        cast(Any, _GoalRunEngine(dynamic_record))
    )

    dynamic_snapshot = dynamic_repository.ensure_run(_ensure_command())

    assert dynamic_snapshot.source_id is None

    pinned_record = _goal_run_record()
    pinned_record["source_id"] = "different-source"
    pinned_repository = PostgresGoalRunActivityRepository(cast(Any, _GoalRunEngine(pinned_record)))
    with pytest.raises(ValueError, match="source mismatch"):
        pinned_repository.ensure_run(_ensure_command())


def test_postgres_activity_repository_derives_deterministic_review_item_id() -> None:
    repository = PostgresGoalRunActivityRepository(cast(Any, _GoalRunEngine(_goal_run_record())))
    first = repository.request_review(_review_command())
    replay = repository.request_review(_review_command())
    changed_idempotency = repository.request_review(
        GoalRunReviewRequestCommand(
            goal_run_id=RUN_ID,
            owner_user_id=OWNER_USER_ID,
            expected_version=1,
            fencing_token=LEASE_TOKEN,
            review_kind="application_drafts",
            review_payload={"review_item_id": str(RUN_ID)},
            snapshot_sha256="a" * 64,
            idempotency_key="goal-run:review:other",
            trace_id="goal-run:test",
        )
    )

    assert first.review_item_id == replay.review_item_id
    assert first.review_item_id != RUN_ID
    assert changed_idempotency.review_item_id != first.review_item_id


def test_postgres_activity_repository_missing_configuration_stops_before_composition() -> None:
    engine = _GoalRunEngine(_goal_run_record())
    repository = PostgresGoalRunActivityRepository(cast(Any, engine))
    command = _domain_command()

    match = repository.match_jobs(command)

    assert match.reason_code == "BLOCKED_MISSING_CONFIGURATION"
    assert len(engine.connection.statements) == 1
    assert "goal_run_status" in str(engine.connection.statements[0])
    assert "gmail_send" not in str(engine.connection.statements[0])


def _activity(
    root: Path,
    output_dir: str,
    *,
    reviewed: GoalRunReviewedCrawlerResult | None = None,
) -> GoalRunSourceRegistryActivities:
    activity = GoalRunSourceRegistryActivities.__new__(GoalRunSourceRegistryActivities)
    mutable_activity = cast(Any, activity)
    mutable_activity._workspace_root = root
    mutable_activity._database_engine = _FakeEngine(
        {
            "adapter": "recruitment.public_ats_feed",
            "cursor": "cursor-1",
            "output_dir": output_dir,
            "registry_id": REGISTRY_ID,
            "source_id": "public-ats",
        }
    )
    mutable_activity._provider = None
    mutable_activity._reviewed_crawler_result_provider = lambda _query: (
        reviewed or _ready_reviewed_result(output_dir)
    )
    mutable_activity._control_plane_repository = DisabledGoalRunControlPlaneRepository()
    return activity


def _write_producer_shaped_artifacts(root: Path, output_dir: str) -> str:
    artifact_dir = root / output_dir
    artifact_dir.mkdir(parents=True)
    jsonl_path = artifact_dir / "discovered_public_ats_jobs.jsonl"
    jsonl = b'{"canonical_url":"https://jobs.example.com/1"}\n'
    jsonl_path.write_bytes(jsonl)
    manifest = (
        json.dumps(
            {
                "artifact_files": {
                    "discovered_jobs": {
                        "artifact_sha256": hashlib.sha256(jsonl).hexdigest(),
                        "bytes": len(jsonl),
                        "path": str(jsonl_path),
                    },
                    "manifest": str(artifact_dir / _PUBLIC_ATS_MANIFEST),
                },
                "generated_at": datetime.now(UTC).isoformat(),
                "kind": "public_ats_job_feed_manifest",
                "version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    (artifact_dir / _PUBLIC_ATS_MANIFEST).write_bytes(manifest)
    return hashlib.sha256(manifest).hexdigest()


def _ready_reviewed_result(output_dir: str) -> GoalRunReviewedCrawlerResult:
    return GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState.READY,
        registry_id=REGISTRY_ID,
        source_row_id=SOURCE_ROW_ID,
        source_id="public-ats",
        request_id=REQUEST_ID,
        result_id=RESULT_ID,
        outbox_event_id=OUTBOX_EVENT_ID,
        reviewed_plan_sha256=REVIEWED_PLAN_SHA256,
        source_sha256=SOURCE_SHA256,
        command_sha256=COMMAND_SHA256,
        output_dir=output_dir,
        adapter="recruitment.public_ats_feed",
        completed_at=datetime.now(UTC),
    )


def _write_reviewed_receipt(
    root: Path,
    output_dir: str,
    reviewed: GoalRunReviewedCrawlerResult,
    *,
    request_id: str | None = None,
) -> None:
    receipt = {
        "adapter": reviewed.adapter,
        "command_sha256": reviewed.command_sha256,
        "completed_at": reviewed.completed_at.isoformat()
        if reviewed.completed_at is not None
        else datetime.now(UTC).isoformat(),
        "execution_request_id": request_id or str(reviewed.request_id),
        "returncode": 0,
        "reviewed_plan_sha256": reviewed.reviewed_plan_sha256,
        "source_id": reviewed.source_id,
        "source_sha256": reviewed.source_sha256,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "completed",
    }
    (root / output_dir / "configured_crawl_receipt.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )


def _discovery_command() -> GoalRunDiscoveryCommand:
    return GoalRunDiscoveryCommand(
        goal_run_id=RUN_ID,
        owner_user_id=OWNER_USER_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        source_row_id=SOURCE_ROW_ID,
        run_id=RUN_ID,
        worker_id="worker-1",
        lease_token=LEASE_TOKEN,
    )


def _reason_code(result: object) -> object:
    if isinstance(result, str):
        decoded = json.loads(result)
        assert isinstance(decoded, dict)
        details = decoded["details"]
        assert isinstance(details, dict)
        return details["reason_code"]
    result_with_attrs = cast(Any, result)
    if hasattr(result_with_attrs, "reason_code"):
        return result_with_attrs.reason_code
    details = result_with_attrs.details
    return details["reason_code"]


def _domain_command() -> GoalRunDomainCommand:
    return GoalRunDomainCommand(
        goal_run_id=RUN_ID,
        owner_user_id=OWNER_USER_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=RUN_ID,
        max_records=100,
    )


def _ensure_command() -> GoalRunEnsureCommand:
    return GoalRunEnsureCommand(
        goal_run_id=RUN_ID,
        owner_user_id=OWNER_USER_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        max_records=100,
        fencing_token=LEASE_TOKEN,
        temporal_workflow_id="goal-run-test",
        idempotency_key="goal-run:create:test",
        trace_id="goal-run:test",
    )


def _load_command() -> GoalRunLoadCommand:
    return GoalRunLoadCommand(goal_run_id=RUN_ID, owner_user_id=OWNER_USER_ID)


def _checkpoint_command() -> GoalRunCheckpointCommand:
    return GoalRunCheckpointCommand(
        goal_run_id=RUN_ID,
        owner_user_id=OWNER_USER_ID,
        expected_version=1,
        fencing_token=LEASE_TOKEN,
        phase=GoalRunPhase.MATCHING,
        status=GoalRunStatus.RUNNING,
        outcome="started",
        checkpoint={},
        idempotency_key="goal-run:checkpoint:test",
        trace_id="goal-run:test",
    )


def _review_command() -> GoalRunReviewRequestCommand:
    return GoalRunReviewRequestCommand(
        goal_run_id=RUN_ID,
        owner_user_id=OWNER_USER_ID,
        expected_version=1,
        fencing_token=LEASE_TOKEN,
        review_kind="application_drafts",
        review_payload={
            "review_item_id": str(RUN_ID),
        },
        snapshot_sha256="a" * 64,
        idempotency_key="goal-run:review:test",
        trace_id="goal-run:test",
    )


class _RecordingControlPlaneRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_run(self, command: object) -> object:
        return self._record("ensure_run", command)

    def load_run(self, command: object) -> object:
        return self._record("load_run", command)

    def checkpoint(self, command: object) -> object:
        return self._record("checkpoint", command)

    def request_review(self, command: object) -> object:
        return self._record("request_review", command)

    def match_jobs(self, command: object) -> object:
        return self._record("match_jobs", command)

    def prepare_drafts(self, command: object) -> object:
        return self._record("prepare_drafts", command)

    def inspect_dispatch(self, command: object) -> object:
        return self._record("inspect_dispatch", command)

    def inspect_reconciliation(self, command: object) -> object:
        return self._record("inspect_reconciliation", command)

    def _record(self, operation: str, command: object) -> object:
        assert cast(Any, command).goal_run_id == RUN_ID
        self.calls.append(operation)
        return {"operation": operation}


class _GoalRunScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _GoalRunConnection:
    def __init__(self, value: object) -> None:
        self.value = value
        self.statements: list[object] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: object) -> _GoalRunScalarResult:
        self.statements.append(statement)
        return _GoalRunScalarResult(self.value)


class _GoalRunTransaction:
    def __init__(self, connection: _GoalRunConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _GoalRunConnection:
        return self._connection

    def __exit__(self, *_exc_info: object) -> None:
        return None


class _GoalRunEngine:
    def __init__(self, value: object) -> None:
        self.connection = _GoalRunConnection(value)

    def begin(self) -> _GoalRunTransaction:
        return _GoalRunTransaction(self.connection)


def _goal_run_record() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "goal_run_id": RUN_ID,
        "actor_id": OWNER_USER_ID,
        "goal_kind": "job-search",
        "goal": "Find reviewable roles.",
        "status": "running",
        "phase": "matching",
        "version": 2,
        "fencing_token": LEASE_TOKEN,
        "context": {},
        "checkpoint": {},
        "registry_id": REGISTRY_ID,
        "source_id": "public-ats",
        "max_records": 100,
        "temporal_workflow_id": "goal-run-test",
        "review_item_id": None,
        "review_snapshot_sha256": None,
        "review_decision": None,
        "review_state": "none",
        "review_kind": None,
        "review_payload": None,
        "last_error_code": None,
        "idempotency_key": "goal-run:create:test",
        "trace_id": "goal-run:test",
        "created_at": now,
        "updated_at": now,
    }
