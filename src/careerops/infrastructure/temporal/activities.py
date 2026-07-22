from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from errno import ELOOP, ENOTDIR
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from temporalio import activity

from careerops.application.goal_run_crawler_discovery import (
    GoalRunCrawlerDiscoveryState,
    GoalRunReviewedCrawlerResult,
    GoalRunReviewedCrawlerResultQuery,
)
from careerops.config import Settings
from careerops.infrastructure.database.crawler_source_registry import (
    PostgresCrawlerSourceRegistryRepository,
)
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.goal_run_crawler_discovery import (
    PostgresGoalRunCrawlerDiscoveryRepository,
)
from careerops.infrastructure.temporal.goal_run_repository import (
    PostgresGoalRunActivityRepository,
)
from careerops.workflows import goal_run_contracts as goal_contracts
from careerops.workflows.goal_run_contracts import (
    GoalRunCheckpointCommand,
    GoalRunCheckpointResult,
    GoalRunClaimCommand,
    GoalRunClaimResult,
    GoalRunCompleteCommand,
    GoalRunDiscoveryCommand,
    GoalRunDiscoveryResult,
    GoalRunDiscoveryState,
    GoalRunDomainCommand,
    GoalRunEnsureCommand,
    GoalRunFailCommand,
    GoalRunIngestCommand,
    GoalRunIngestResult,
    GoalRunLoadCommand,
    GoalRunReviewRequestCommand,
    GoalRunReviewRequestResult,
    GoalRunSnapshot,
    GoalRunStepOutcome,
    GoalRunStepResult,
)
from careerops.workflows.smoke_contracts import (
    SMOKE_COMPLETED_ACTIVITY,
    SMOKE_STARTED_ACTIVITY,
    SmokeActivityReceipt,
    SmokeCompletionCommand,
    SmokeStartCommand,
)

GOAL_RUN_CLAIM_ACTIVITY = goal_contracts.GOAL_RUN_CLAIM_ACTIVITY
GOAL_RUN_DISCOVERY_ACTIVITY = goal_contracts.GOAL_RUN_DISCOVERY_ACTIVITY
GOAL_RUN_COMPLETE_SOURCE_ACTIVITY = goal_contracts.GOAL_RUN_COMPLETE_SOURCE_ACTIVITY
GOAL_RUN_FAIL_SOURCE_ACTIVITY = goal_contracts.GOAL_RUN_FAIL_SOURCE_ACTIVITY
GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY = goal_contracts.GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY
GOAL_RUN_MATCH_JOBS_ACTIVITY = goal_contracts.GOAL_RUN_MATCH_ACTIVITY
GOAL_RUN_PREPARE_DRAFTS_ACTIVITY = goal_contracts.GOAL_RUN_PREPARE_DRAFTS_ACTIVITY
GOAL_RUN_REQUEST_REVIEW_ACTIVITY = goal_contracts.GOAL_RUN_REQUEST_REVIEW_ACTIVITY
GOAL_RUN_DISPATCH_ACTIVITY = goal_contracts.GOAL_RUN_DISPATCH_ACTIVITY
GOAL_RUN_RECONCILE_ACTIVITY = goal_contracts.GOAL_RUN_RECONCILE_ACTIVITY
GOAL_RUN_ENSURE_RUN_ACTIVITY = goal_contracts.GOAL_RUN_ENSURE_ACTIVITY
GOAL_RUN_LOAD_RUN_ACTIVITY = goal_contracts.GOAL_RUN_LOAD_ACTIVITY
GOAL_RUN_CHECKPOINT_ACTIVITY = goal_contracts.GOAL_RUN_CHECKPOINT_ACTIVITY

_BLOCKED_MISSING_CONFIGURATION = "BLOCKED_MISSING_CONFIGURATION"
_BLOCKED_MISSING_CONTROL_PLANE = "BLOCKED_MISSING_CONTROL_PLANE_REPOSITORY"
_DISABLED_PENDING_G014_COMPOSITION = "DISABLED_PENDING_G014_COMPOSITION"
_OPEN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_OPEN_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class GoalRunControlPlaneRepository(Protocol):
    """Durable GoalRun control-plane adapter owned by the application layer."""

    def ensure_run(self, command: GoalRunEnsureCommand) -> GoalRunSnapshot: ...

    def load_run(self, command: GoalRunLoadCommand) -> GoalRunSnapshot: ...

    def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunCheckpointResult: ...

    def request_review(
        self,
        command: GoalRunReviewRequestCommand,
    ) -> GoalRunReviewRequestResult: ...

    def match_jobs(self, command: GoalRunDomainCommand) -> GoalRunStepResult: ...

    def prepare_drafts(self, command: GoalRunDomainCommand) -> GoalRunStepResult: ...

    def inspect_dispatch(self, command: GoalRunDomainCommand) -> GoalRunStepResult: ...

    def inspect_reconciliation(self, command: GoalRunDomainCommand) -> GoalRunStepResult: ...


class DisabledGoalRunControlPlaneRepository:
    """Fail-closed placeholder until the application repository is composed in."""

    def ensure_run(self, command: GoalRunEnsureCommand) -> GoalRunSnapshot:
        raise RuntimeError(_BLOCKED_MISSING_CONTROL_PLANE)

    def load_run(self, command: GoalRunLoadCommand) -> GoalRunSnapshot:
        raise RuntimeError(_BLOCKED_MISSING_CONTROL_PLANE)

    def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunCheckpointResult:
        raise RuntimeError(_BLOCKED_MISSING_CONTROL_PLANE)

    def request_review(
        self,
        command: GoalRunReviewRequestCommand,
    ) -> GoalRunReviewRequestResult:
        raise RuntimeError(_BLOCKED_MISSING_CONTROL_PLANE)

    def match_jobs(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return _blocked_step(command, reason_code=_BLOCKED_MISSING_CONFIGURATION)

    def prepare_drafts(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return _blocked_step(command, reason_code=_BLOCKED_MISSING_CONFIGURATION)

    def inspect_dispatch(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return _blocked_step(command, reason_code=_DISABLED_PENDING_G014_COMPOSITION)

    def inspect_reconciliation(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return _blocked_step(command, reason_code=_DISABLED_PENDING_G014_COMPOSITION)


def _blocked_step(command: GoalRunDomainCommand, *, reason_code: str) -> GoalRunStepResult:
    return GoalRunStepResult(
        outcome=GoalRunStepOutcome.BLOCKED_CONFIGURATION,
        reason_code=reason_code,
        details={
            "goal_run_id": str(command.goal_run_id),
            "registry_id": str(command.registry_id),
            "source_id": command.source_id,
            "source_row_id": str(command.source_row_id),
            "crawler_run_id": str(command.crawler_run_id),
        },
    )


class SmokeActivitySink(Protocol):
    """Activity-side adapter; implementations may perform external I/O."""

    async def record_started(self, command: SmokeStartCommand) -> str: ...

    async def record_completed(self, command: SmokeCompletionCommand) -> str: ...


class NoOpSmokeActivitySink:
    """M0 sink that emits stable receipts without external side effects."""

    async def record_started(self, command: SmokeStartCommand) -> str:
        return f"started:{command.operation_id}"

    async def record_completed(self, command: SmokeCompletionCommand) -> str:
        return f"completed:{command.idempotency_key}"


class SmokeActivities:
    def __init__(self, sink: SmokeActivitySink | None = None) -> None:
        self._sink = sink or NoOpSmokeActivitySink()

    @activity.defn(name=SMOKE_STARTED_ACTIVITY)
    async def record_started(self, command: SmokeStartCommand) -> SmokeActivityReceipt:
        activity.logger.info("recording recoverable smoke start")
        receipt_id = await self._sink.record_started(command)
        return SmokeActivityReceipt(receipt_id=receipt_id)

    @activity.defn(name=SMOKE_COMPLETED_ACTIVITY)
    async def record_completed(self, command: SmokeCompletionCommand) -> SmokeActivityReceipt:
        activity.logger.info("recording recoverable smoke completion")
        receipt_id = await self._sink.record_completed(command)
        return SmokeActivityReceipt(receipt_id=receipt_id)


class GoalRunSourceRegistryActivities:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        workspace_root: str | Path | None = None,
        control_plane_repository: GoalRunControlPlaneRepository | None = None,
        reviewed_crawler_result_provider: (
            Callable[[GoalRunReviewedCrawlerResultQuery], GoalRunReviewedCrawlerResult] | None
        ) = None,
    ) -> None:
        self._settings = settings or Settings()
        self._workspace_root = Path(workspace_root or self._settings.crawler_workspace_root)
        self._database_engine = create_database_engine(self._settings)
        self._provider = None
        self._reviewed_crawler_result_provider = reviewed_crawler_result_provider
        self._control_plane_repository = (
            control_plane_repository or PostgresGoalRunActivityRepository(self._database_engine)
        )

    @activity.defn(name=GOAL_RUN_ENSURE_RUN_ACTIVITY)
    async def ensure_run(self, command: GoalRunEnsureCommand) -> GoalRunSnapshot:
        return self._control_plane_repository.ensure_run(command)

    @activity.defn(name=GOAL_RUN_LOAD_RUN_ACTIVITY)
    async def load_run(self, command: GoalRunLoadCommand) -> GoalRunSnapshot:
        return self._control_plane_repository.load_run(command)

    @activity.defn(name=GOAL_RUN_CHECKPOINT_ACTIVITY)
    async def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunCheckpointResult:
        return self._control_plane_repository.checkpoint(command)

    @activity.defn(name=GOAL_RUN_REQUEST_REVIEW_ACTIVITY)
    async def request_review(
        self,
        command: GoalRunReviewRequestCommand,
    ) -> GoalRunReviewRequestResult:
        return self._control_plane_repository.request_review(command)

    @activity.defn(name=GOAL_RUN_CLAIM_ACTIVITY)
    async def claim_source(self, command: GoalRunClaimCommand) -> GoalRunClaimResult:
        worker_id = f"goal-run:{command.goal_run_id}"
        lease_seconds = 300
        with self._database_engine.begin() as connection:
            repository = PostgresCrawlerSourceRegistryRepository(connection)
            if command.source_id is None:
                row = repository.claim_due_source(
                    registry_id=command.registry_id,
                    worker_id=worker_id,
                    lease_for=timedelta(seconds=lease_seconds),
                )
            else:
                row = repository.claim_source(
                    command.registry_id,
                    command.source_id,
                    worker_id=worker_id,
                    lease_for=timedelta(seconds=lease_seconds),
                )
            if row is None:
                raise ValueError("no due source is currently claimable")
            return GoalRunClaimResult(
                registry_id=command.registry_id,
                run_id=cast(UUID, row["run_id"]),
                source_row_id=cast(UUID, row["source_row_id"]),
                source_id=cast(str, row["source_id"]),
                lease_token=cast(UUID, row["lease_token"]),
                lease_owner=cast(str, row["lease_owner"]),
                lease_expires_at=cast(datetime, row["lease_expires_at"]),
            )

    @activity.defn(name=GOAL_RUN_DISCOVERY_ACTIVITY)
    async def run_discovery(self, command: GoalRunDiscoveryCommand) -> GoalRunDiscoveryResult:
        reviewed = self._reviewed_crawler_result(
            GoalRunReviewedCrawlerResultQuery(
                actor_id=command.owner_user_id,
                goal_run_id=command.goal_run_id,
                registry_id=command.registry_id,
                source_id=command.source_id,
            )
        )
        if reviewed.state is not GoalRunCrawlerDiscoveryState.READY:
            return GoalRunDiscoveryResult(
                source_row_id=command.source_row_id,
                run_id=command.run_id,
                state=GoalRunDiscoveryState(reviewed.state.value),
                output_manifest_sha256=None,
                cursor=None,
                result=reviewed.error_code or "REVIEWED_CRAWLER_EXECUTION_PENDING",
                crawler_execution_request_id=reviewed.request_id,
                crawler_execution_result_id=reviewed.result_id,
                crawler_execution_outbox_event_id=reviewed.outbox_event_id,
            )

        if (
            reviewed.registry_id != command.registry_id
            or reviewed.source_row_id != command.source_row_id
            or reviewed.source_id != command.source_id
        ):
            raise ValueError("reviewed crawler result does not match the active source claim")
        if reviewed.adapter != "recruitment.public_ats_feed":
            return GoalRunDiscoveryResult(
                source_row_id=command.source_row_id,
                run_id=command.run_id,
                state=GoalRunDiscoveryState.FAILED,
                output_manifest_sha256=None,
                cursor=None,
                result="UNSUPPORTED_SOURCE_ADAPTER",
                crawler_execution_request_id=reviewed.request_id,
                crawler_execution_result_id=reviewed.result_id,
                crawler_execution_outbox_event_id=reviewed.outbox_event_id,
            )
        output_dir = reviewed.output_dir
        if output_dir is None:
            raise ValueError("reviewed crawler result is missing output directory evidence")
        receipt_bytes = _read_registered_artifact(
            self._workspace_root,
            output_dir,
            _CRAWLER_EXECUTION_RECEIPT,
            max_bytes=_MAX_RECEIPT_BYTES,
            artifact="reviewed crawler execution receipt",
        )
        _validate_reviewed_crawler_receipt(receipt_bytes, reviewed)

        manifest_bytes = _read_registered_artifact(
            self._workspace_root,
            output_dir,
            _PUBLIC_ATS_MANIFEST,
            max_bytes=_MAX_MANIFEST_BYTES,
            artifact="public ATS manifest",
        )
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        discovered_jobs = _public_ats_discovered_jobs_artifact(manifest_bytes)

        jobs_bytes = _read_registered_artifact(
            self._workspace_root,
            output_dir,
            discovered_jobs.path,
            max_bytes=_MAX_JSONL_BYTES,
            artifact="public ATS jsonl",
        )
        if len(jobs_bytes) != discovered_jobs.bytes:
            raise ValueError("public ATS JSONL byte count does not match manifest")
        if hashlib.sha256(jobs_bytes).hexdigest() != discovered_jobs.sha256:
            raise ValueError("public ATS JSONL digest does not match manifest")

        return GoalRunDiscoveryResult(
            source_row_id=command.source_row_id,
            run_id=command.run_id,
            state=GoalRunDiscoveryState.READY,
            output_manifest_sha256=manifest_sha256,
            cursor=None,
            result="REVIEWED_CRAWLER_DISCOVERY_READY",
            crawler_execution_request_id=reviewed.request_id,
            crawler_execution_result_id=reviewed.result_id,
            crawler_execution_outbox_event_id=reviewed.outbox_event_id,
        )

    @activity.defn(name=GOAL_RUN_COMPLETE_SOURCE_ACTIVITY)
    async def complete_source(self, command: GoalRunCompleteCommand) -> str:
        with self._database_engine.begin() as connection:
            repository = PostgresCrawlerSourceRegistryRepository(connection)
            repository.complete_source(
                source_row_id=command.source_row_id,
                run_id=command.run_id,
                worker_id=command.worker_id,
                lease_token=command.lease_token,
                output_manifest_sha256=command.output_manifest_sha256,
                cursor=command.cursor,
                result=command.result,
            )
        return "ok"

    @activity.defn(name=GOAL_RUN_FAIL_SOURCE_ACTIVITY)
    async def fail_source(self, command: GoalRunFailCommand) -> str:
        with self._database_engine.begin() as connection:
            repository = PostgresCrawlerSourceRegistryRepository(connection)
            repository.fail_source(
                source_row_id=command.source_row_id,
                run_id=command.run_id,
                worker_id=command.worker_id,
                lease_token=command.lease_token,
                error=command.error_code,
            )
        return "ok"

    @activity.defn(name=GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY)
    async def ingest_public_ats(self, command: GoalRunIngestCommand) -> GoalRunIngestResult:
        provider = self._goal_provider()
        response = await provider.ingest_public_ats(
            registry_id=command.registry_id,
            source_id=command.source_id,
            run_id=command.run_id,
            source_row_id=command.source_row_id,
            actor_id=command.owner_user_id,
            max_records=command.max_records,
        )
        return GoalRunIngestResult(
            observed_records=response.observed_records,
            inserted_versions=response.inserted_versions,
            reused_versions=response.reused_versions,
        )

    @activity.defn(name=GOAL_RUN_MATCH_JOBS_ACTIVITY)
    async def match_jobs(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return self._control_plane_repository.match_jobs(command)

    @activity.defn(name=GOAL_RUN_PREPARE_DRAFTS_ACTIVITY)
    async def prepare_drafts(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return self._control_plane_repository.prepare_drafts(command)

    @activity.defn(name=GOAL_RUN_DISPATCH_ACTIVITY)
    async def dispatch_goal(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return self._control_plane_repository.inspect_dispatch(command)

    @activity.defn(name=GOAL_RUN_RECONCILE_ACTIVITY)
    async def reconcile_goal(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        return self._control_plane_repository.inspect_reconciliation(command)

    def _reviewed_crawler_result(
        self,
        query: GoalRunReviewedCrawlerResultQuery,
    ) -> GoalRunReviewedCrawlerResult:
        if self._reviewed_crawler_result_provider is not None:
            return self._reviewed_crawler_result_provider(query)
        with self._database_engine.begin() as connection:
            return PostgresGoalRunCrawlerDiscoveryRepository(connection).reviewed_result(query)

    def _goal_provider(self):
        if self._provider is None:
            from careerops.api.source_registry import RuntimeSourceRegistryOperatorProvider

            self._provider = RuntimeSourceRegistryOperatorProvider(
                self._database_engine,
                self._settings,
            )
        return self._provider


@dataclass(frozen=True, slots=True)
class _ManifestArtifactEvidence:
    path: str
    sha256: str
    bytes: int


_PUBLIC_ATS_MANIFEST = "public_ats_job_feed_manifest.json"
_CRAWLER_EXECUTION_RECEIPT = "configured_crawl_receipt.json"
_MAX_RECEIPT_BYTES = 65_536
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_JSONL_BYTES = 64 * 1_048_576
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTERED_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")


def _validate_reviewed_crawler_receipt(
    receipt_bytes: bytes,
    reviewed: GoalRunReviewedCrawlerResult,
) -> None:
    try:
        decoded = json.loads(receipt_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("reviewed crawler execution receipt is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("reviewed crawler execution receipt must be a JSON object")
    receipt = cast(dict[object, object], decoded)
    expected = {
        "adapter": reviewed.adapter,
        "command_sha256": reviewed.command_sha256,
        "execution_request_id": str(reviewed.request_id),
        "reviewed_plan_sha256": reviewed.reviewed_plan_sha256,
        "source_id": reviewed.source_id,
        "source_sha256": reviewed.source_sha256,
        "status": "completed",
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("crawler receipt does not match the reviewed execution result")
    if receipt.get("returncode") != 0:
        raise ValueError("reviewed crawler receipt is not successful")
    raw_completed_at = receipt.get("completed_at")
    if not isinstance(raw_completed_at, str):
        raise ValueError("reviewed crawler receipt completion time is missing")
    try:
        completed_at = datetime.fromisoformat(raw_completed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("reviewed crawler receipt completion time is invalid") from error
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("reviewed crawler receipt completion time must be timezone-aware")
    if reviewed.completed_at is None or completed_at > reviewed.completed_at:
        raise ValueError("crawler receipt completion time exceeds its durable result")


def _resolve_registered_artifact(root: Path, output_dir: str, filename: str) -> Path:
    workspace_root = root.resolve()
    raw_datasets_root = workspace_root / "datasets"
    if raw_datasets_root.is_symlink():
        raise ValueError("crawler datasets root must not be a symbolic link")
    try:
        datasets_root = raw_datasets_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("crawler datasets root is unavailable") from error
    if not datasets_root.is_relative_to(workspace_root):
        raise ValueError("crawler datasets root escapes the workspace")

    if (
        not output_dir
        or _REGISTERED_RELATIVE_PATH.fullmatch(output_dir) is None
        or Path(output_dir).is_absolute()
        or ".." in Path(output_dir).parts
    ):
        raise ValueError("registered output_dir is not a safe relative path")

    artifact_path = Path(filename)
    if artifact_path.is_absolute():
        unresolved = artifact_path
    else:
        if ".." in artifact_path.parts:
            raise ValueError("public ATS artifact path is not safe")
        output_path = Path(output_dir)
        if artifact_path.parts[: len(output_path.parts)] == output_path.parts:
            unresolved = workspace_root / artifact_path
        else:
            unresolved = workspace_root / output_dir / artifact_path

    try:
        unresolved.relative_to(datasets_root)
    except ValueError as error:
        raise ValueError(
            "public ATS artifact must stay inside the crawler datasets root"
        ) from error

    current = Path(unresolved.anchor)
    for part in unresolved.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("public ATS artifacts must not resolve through symbolic links")

    try:
        resolved = unresolved.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"required public ATS artifact is unavailable: {filename}") from error
    if not resolved.is_relative_to(workspace_root):
        raise ValueError("public ATS artifact escapes the crawler workspace")
    if not resolved.is_relative_to(datasets_root):
        raise ValueError("public ATS artifact escapes the crawler datasets root")
    if not resolved.is_file():
        raise ValueError(f"required public ATS artifact is not a file: {filename}")
    return resolved


def _read_registered_artifact(
    root: Path,
    output_dir: str,
    filename: str,
    *,
    max_bytes: int,
    artifact: str,
) -> bytes:
    _resolve_registered_artifact(root, output_dir, filename)
    workspace_root = root.resolve()
    output_path = Path(output_dir)
    if not _safe_registered_output_path(output_path):
        raise ValueError("registered output_dir is not a safe relative path")
    if not output_path.parts or output_path.parts[0] != "datasets":
        raise ValueError("registered output_dir must stay inside the crawler datasets root")

    artifact_parts = _registered_artifact_parts(
        workspace_root=workspace_root,
        output_path=output_path,
        filename=filename,
    )
    try:
        root_fd = _open_directory_path(workspace_root, description="crawler workspace root")
    except OSError as exc:
        raise ValueError("crawler workspace root is unavailable") from exc

    owned_fds: list[int] = []
    try:
        parent_fd = root_fd
        for part in artifact_parts[:-1]:
            next_fd = _open_child_directory(parent_fd, part)
            owned_fds.append(next_fd)
            parent_fd = next_fd
        file_fd = _open_child_file(parent_fd, artifact_parts[-1])
        try:
            return _read_bounded_fd(file_fd, max_bytes=max_bytes, artifact=artifact)
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno in {ELOOP, ENOTDIR}:
            raise ValueError(
                "public ATS artifacts must not resolve through symbolic links"
            ) from exc
        raise ValueError(f"{artifact} could not be read") from exc
    finally:
        for fd in reversed(owned_fds):
            os.close(fd)
        os.close(root_fd)


def _safe_registered_output_path(output_path: Path) -> bool:
    return (
        bool(output_path.parts)
        and _REGISTERED_RELATIVE_PATH.fullmatch(output_path.as_posix()) is not None
        and not output_path.is_absolute()
        and ".." not in output_path.parts
        and all(part not in {"", "."} for part in output_path.parts)
    )


def _registered_artifact_parts(
    *,
    workspace_root: Path,
    output_path: Path,
    filename: str,
) -> tuple[str, ...]:
    raw_artifact_path = Path(filename)
    if raw_artifact_path.is_absolute():
        resolved = raw_artifact_path.resolve(strict=False)
        datasets_root = workspace_root / "datasets"
        try:
            relative = resolved.relative_to(datasets_root)
        except ValueError as exc:
            raise ValueError("public ATS artifact escapes the crawler datasets root") from exc
        artifact_path = Path("datasets") / relative
    else:
        if ".." in raw_artifact_path.parts or any(
            part in {"", "."} for part in raw_artifact_path.parts
        ):
            raise ValueError("public ATS artifact path is not safe")
        if raw_artifact_path.parts[: len(output_path.parts)] == output_path.parts:
            artifact_path = raw_artifact_path
        else:
            artifact_path = output_path / raw_artifact_path
    if (
        not artifact_path.parts
        or artifact_path.parts[0] != "datasets"
        or ".." in artifact_path.parts
    ):
        raise ValueError("public ATS artifact must stay inside the crawler datasets root")
    return artifact_path.parts


def _open_directory_path(path: Path, *, description: str) -> int:
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW | _OPEN_CLOEXEC,
    )
    if not stat_module.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{description} is not a directory")
    return fd


def _open_child_directory(parent_fd: int, name: str) -> int:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _OPEN_NOFOLLOW | _OPEN_CLOEXEC,
        dir_fd=parent_fd,
    )
    if not stat_module.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("public ATS artifact path component is not a directory")
    return fd


def _open_child_file(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | _OPEN_NOFOLLOW | _OPEN_CLOEXEC, dir_fd=parent_fd)
    if not stat_module.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("required public ATS artifact is not a file")
    return fd


def _read_bounded_fd(fd: int, *, max_bytes: int, artifact: str) -> bytes:
    payload = b""
    while len(payload) <= max_bytes:
        chunk = os.read(fd, min(1_048_576, max_bytes + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    if len(payload) > max_bytes:
        raise ValueError(f"{artifact} exceeds size limit")
    return payload


def _public_ats_discovered_jobs_artifact(manifest_bytes: bytes) -> _ManifestArtifactEvidence:
    try:
        decoded = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("public ATS manifest is invalid JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("public ATS manifest must be a JSON object")
    manifest = cast(dict[object, object], decoded)
    if manifest.get("kind") != "public_ats_job_feed_manifest":
        raise ValueError("public ATS manifest kind is unsupported")
    version = manifest.get("version")
    if type(version) is not int or version != 1:
        raise ValueError("public ATS manifest version is unsupported")

    artifact_files = manifest.get("artifact_files")
    if not isinstance(artifact_files, dict):
        raise ValueError("public ATS manifest is missing artifact_files")
    discovered_jobs = cast(dict[object, object], artifact_files).get("discovered_jobs")
    if not isinstance(discovered_jobs, dict):
        raise ValueError("public ATS manifest missing discovered_jobs metadata")
    evidence = cast(dict[object, object], discovered_jobs)

    discovered_path = evidence.get("path")
    if not isinstance(discovered_path, str) or not 1 <= len(discovered_path) <= 500:
        raise ValueError("public ATS manifest discovered_jobs path is unsupported")

    artifact_bytes = evidence.get("bytes")
    if type(artifact_bytes) is not int or artifact_bytes < 0 or artifact_bytes > _MAX_JSONL_BYTES:
        raise ValueError("public ATS manifest has invalid discovered_jobs byte count")

    digest = evidence.get("artifact_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("public ATS manifest has invalid discovered_jobs digest")
    return _ManifestArtifactEvidence(path=discovered_path, sha256=digest, bytes=artifact_bytes)


__all__ = [
    "GOAL_RUN_CHECKPOINT_ACTIVITY",
    "GOAL_RUN_CLAIM_ACTIVITY",
    "GOAL_RUN_COMPLETE_SOURCE_ACTIVITY",
    "GOAL_RUN_DISCOVERY_ACTIVITY",
    "GOAL_RUN_DISPATCH_ACTIVITY",
    "GOAL_RUN_ENSURE_RUN_ACTIVITY",
    "GOAL_RUN_FAIL_SOURCE_ACTIVITY",
    "GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY",
    "GOAL_RUN_LOAD_RUN_ACTIVITY",
    "GOAL_RUN_MATCH_JOBS_ACTIVITY",
    "GOAL_RUN_PREPARE_DRAFTS_ACTIVITY",
    "GOAL_RUN_RECONCILE_ACTIVITY",
    "GOAL_RUN_REQUEST_REVIEW_ACTIVITY",
    "DisabledGoalRunControlPlaneRepository",
    "GoalRunControlPlaneRepository",
    "GoalRunSourceRegistryActivities",
    "NoOpSmokeActivitySink",
    "SmokeActivities",
    "SmokeActivitySink",
]
