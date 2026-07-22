from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal, NoReturn, Protocol, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.engine import Connection, Engine
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from careerops.api.goal_runs import (
    CreateGoalRunResponse,
    GoalRunCandidateProfileSnapshot,
    GoalRunCommandResponse,
    GoalRunConflict,
    GoalRunMatchConfig,
    GoalRunMaterialRef,
    GoalRunNotFound,
    GoalRunReviewActionLabel,
    GoalRunReviewedGmailDispatchConfig,
    GoalRunReviewProjection,
    GoalRunStatusResponse,
    GoalRunSummary,
    GoalRunUnavailable,
    ListGoalRunsResponse,
)
from careerops.application.candidate_profile import (
    ApprovedCandidateProfileQuery,
    CandidateProfileDecision,
    CandidateProfileRepositoryError,
    CandidateProfileSnapshotRecord,
)
from careerops.application.goal_run import (
    GoalRunCancelCommand,
    GoalRunCreateCommand,
    GoalRunCreateLookupQuery,
    GoalRunListOwnerQuery,
    GoalRunListPage,
    GoalRunRecord,
    GoalRunRepositoryError,
    GoalRunResumeCommand,
    GoalRunReviewDecisionCommand,
    GoalRunStatusQuery,
)
from careerops.application.goal_run import (
    GoalRunReviewDecision as ApplicationGoalRunReviewDecision,
)
from careerops.config import Settings
from careerops.infrastructure.database.candidate_profile import (
    PostgresCandidateProfileRepository,
)
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.goal_run import PostgresGoalRunRepository
from careerops.workflows.goal_run_contracts import (
    GoalRunCancelSignal,
    GoalRunInput,
    GoalRunMode,
    GoalRunResumeSignal,
    GoalRunReviewSignal,
)

_DEFAULT_GOAL_KIND = "job-search"
_DEFAULT_GOAL = "Run bounded career goal automation with authenticated operator review."
_DEFAULT_MAX_RECORDS = 1000
_CONFLICT_REASON_CODES = frozenset(
    {
        "GOAL_RUN_IDEMPOTENCY_CONFLICT",
        "GOAL_RUN_REJECTED",
        "GOAL_RUN_VERSION_CONFLICT",
        "GOAL_RUN_STATE_CONFLICT",
    }
)
_NOT_FOUND_REASON_CODES = frozenset({"GOAL_RUN_NOT_FOUND", "GOAL_RUN_FORBIDDEN"})


class TemporalClientPort(Protocol):
    async def start_workflow(
        self,
        workflow: str,
        arg: object,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: WorkflowIDReusePolicy,
        id_conflict_policy: WorkflowIDConflictPolicy,
    ) -> object: ...

    def get_workflow_handle(self, workflow_id: str) -> TemporalWorkflowHandlePort: ...


class TemporalWorkflowHandlePort(Protocol):
    async def signal(self, signal: str, arg: object) -> object: ...


class GoalRunRepositoryPort(Protocol):
    def find_create(self, query: GoalRunCreateLookupQuery) -> object | None: ...

    def create(self, command: GoalRunCreateCommand) -> object: ...

    def status(self, query: GoalRunStatusQuery) -> GoalRunRecord: ...

    def list_owner(self, query: GoalRunListOwnerQuery) -> GoalRunListPage: ...

    def record_review_decision(self, command: GoalRunReviewDecisionCommand) -> object: ...

    def resume(self, command: GoalRunResumeCommand) -> object: ...

    def cancel(self, command: GoalRunCancelCommand) -> object: ...


class RuntimeGoalRunOperatorProvider:
    """Runtime adapter between authenticated GoalRun APIs, Postgres, and Temporal."""

    def __init__(
        self,
        *,
        settings: Settings,
        transaction_factory: Callable[[], AbstractContextManager[Connection]],
        repository_factory: Callable[[Connection], GoalRunRepositoryPort],
        temporal_client_factory: Callable[[], TemporalClientPort | Awaitable[TemporalClientPort]],
    ) -> None:
        self._settings = settings
        self._transaction_factory = transaction_factory
        self._repository_factory = repository_factory
        self._temporal_client_factory = temporal_client_factory
        self._temporal_client: TemporalClientPort | None = None
        self._temporal_lock = asyncio.Lock()

    async def create_goal_run(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        registry_id: UUID,
        source_id: str | None,
        mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH,
        match_config: GoalRunMatchConfig | None = None,
        candidate_id: UUID | None = None,
        candidate_profile_version_id: UUID | None = None,
        gmail_dispatch: GoalRunReviewedGmailDispatchConfig | None = None,
        now: datetime,
    ) -> CreateGoalRunResponse:
        del now
        _validate_create_boundary(
            mode=mode,
            match_config=match_config,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            gmail_dispatch=gmail_dispatch,
        )
        workflow_id = _workflow_id(actor_id, command_id)
        context = _create_context(
            registry_id=registry_id,
            source_id=source_id,
            mode=mode,
            match_config=match_config,
            gmail_dispatch=gmail_dispatch,
        )
        result = await asyncio.to_thread(
            self._create_record,
            GoalRunCreateCommand(
                actor_id=actor_id,
                idempotency_key=_idempotency_key(actor_id, command_id),
                goal_kind=_DEFAULT_GOAL_KIND,
                goal=_DEFAULT_GOAL,
                context=context,
                registry_id=registry_id,
                source_id=source_id,
                max_records=_DEFAULT_MAX_RECORDS,
                temporal_workflow_id=workflow_id,
                trace_id=_trace_id(command_id),
            ),
            mode,
            candidate_id,
            candidate_profile_version_id,
        )
        record = _record_from_mutation(result)
        if _workflow_id_from_record(record) != workflow_id:
            raise GoalRunConflict()
        try:
            await self._start_workflow(record, registry_id=registry_id, source_id=source_id)
        except GoalRunUnavailable:
            raise
        return CreateGoalRunResponse(goal_run=_summary_from_record(record))

    async def list_goal_runs(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGoalRunsResponse:
        records = await asyncio.to_thread(self._list_records, actor_id, limit)
        return ListGoalRunsResponse(
            goal_runs=tuple(_summary_from_record(record) for record in records)
        )

    async def goal_run_status(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
    ) -> GoalRunStatusResponse:
        record = await asyncio.to_thread(self._status_record, actor_id, goal_run_id)
        return GoalRunStatusResponse(goal_run=_summary_from_record(record))

    async def submit_review(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        review_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        now: datetime,
    ) -> GoalRunCommandResponse:
        del now
        record = await asyncio.to_thread(self._status_record, actor_id, goal_run_id)
        result = await asyncio.to_thread(
            self._record_review_decision,
            record,
            review_id,
            snapshot_sha256,
            decision,
            command_id,
        )
        committed = _record_from_mutation(result)
        await self._signal_workflow(
            committed,
            signal="review",
            arg=GoalRunReviewSignal(
                command_id=command_id,
                review_item_id=review_id,
                snapshot_sha256=snapshot_sha256,
            ),
        )
        return GoalRunCommandResponse(
            status="submitted",
            goal_run=_summary_from_record(committed),
        )

    async def resume_goal_run(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        now: datetime,
    ) -> GoalRunCommandResponse:
        del now
        record = await asyncio.to_thread(self._status_record, actor_id, goal_run_id)
        result = await asyncio.to_thread(self._resume_record, record, command_id)
        committed = _record_from_mutation(result)
        await self._signal_workflow(
            committed,
            signal="resume",
            arg=GoalRunResumeSignal(command_id=command_id),
        )
        return GoalRunCommandResponse(
            status="resumed",
            goal_run=_summary_from_record(committed),
        )

    async def cancel_goal_run(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> GoalRunCommandResponse:
        del now
        record = await asyncio.to_thread(self._status_record, actor_id, goal_run_id)
        result = await asyncio.to_thread(
            self._cancel_record,
            record,
            reason or "operator requested cancellation",
            command_id,
        )
        committed = _record_from_mutation(result)
        await self._signal_workflow(
            committed,
            signal="cancel",
            arg=GoalRunCancelSignal(command_id=command_id),
        )
        return GoalRunCommandResponse(
            status="cancelled",
            goal_run=_summary_from_record(committed),
        )

    async def close(self) -> None:
        client = self._temporal_client
        self._temporal_client = None
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    def _create_record(
        self,
        command: GoalRunCreateCommand,
        mode: GoalRunMode,
        candidate_id: UUID | None,
        candidate_profile_version_id: UUID | None,
    ) -> object:
        try:
            with self._transaction_factory() as connection:
                repository = self._repository_factory(connection)
                existing = repository.find_create(
                    GoalRunCreateLookupQuery(
                        actor_id=command.actor_id,
                        idempotency_key=command.idempotency_key,
                    )
                )
                if existing is not None:
                    _validate_existing_create(
                        _record_from_mutation(existing),
                        command=command,
                        mode=mode,
                        candidate_id=candidate_id,
                        candidate_profile_version_id=candidate_profile_version_id,
                    )
                    return existing
                if candidate_id is not None and candidate_profile_version_id is not None:
                    profile_record = PostgresCandidateProfileRepository(connection).get_approved(
                        ApprovedCandidateProfileQuery(
                            actor_id=command.actor_id,
                            candidate_id=candidate_id,
                        )
                    )
                    _validate_candidate_profile_identity(
                        profile_record,
                        actor_id=command.actor_id,
                        candidate_id=candidate_id,
                        profile_version_id=candidate_profile_version_id,
                    )
                    context = dict(command.context)
                    try:
                        context.update(_goal_run_profile_context(profile_record))
                    except (TypeError, ValueError):
                        raise GoalRunConflict(
                            "approved candidate profile cannot be materialized"
                        ) from None
                    command = replace(command, context=context)
                return repository.create(command)
        except CandidateProfileRepositoryError as exc:
            if exc.reason_code == "CANDIDATE_PROFILE_NOT_FOUND":
                raise GoalRunNotFound() from None
            if exc.reason_code in {
                "CANDIDATE_PROFILE_STATE_CONFLICT",
                "CANDIDATE_PROFILE_INPUT_INVALID",
            }:
                raise GoalRunConflict() from None
            raise GoalRunUnavailable("candidate profile database unavailable") from None
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    def _status_record(self, actor_id: UUID, goal_run_id: UUID) -> GoalRunRecord:
        try:
            with self._transaction_factory() as connection:
                return self._repository_factory(connection).status(
                    GoalRunStatusQuery(actor_id=actor_id, goal_run_id=goal_run_id)
                )
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    def _list_records(self, actor_id: UUID, limit: int) -> Sequence[GoalRunRecord]:
        try:
            with self._transaction_factory() as connection:
                repository = self._repository_factory(connection)
                page = repository.list_owner(GoalRunListOwnerQuery(actor_id=actor_id, limit=limit))
                return page.records
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    def _record_review_decision(
        self,
        record: GoalRunRecord,
        review_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        command_id: UUID,
    ) -> object:
        if _review_snapshot_sha256(record) != snapshot_sha256:
            raise GoalRunConflict()
        try:
            with self._transaction_factory() as connection:
                repository = self._repository_factory(connection)
                if (
                    decision == "approve"
                    and _mode_from_context(_context(record)) is GoalRunMode.PRE_APPLICATION_ONLY
                ):
                    _revalidate_preapplication_profile(connection, record)
                return repository.record_review_decision(
                    GoalRunReviewDecisionCommand(
                        actor_id=_owner_user_id(record),
                        goal_run_id=record.goal_run_id,
                        expected_version=record.version,
                        fencing_token=record.fencing_token,
                        review_item_id=review_id,
                        review_snapshot_sha256=snapshot_sha256,
                        decision=ApplicationGoalRunReviewDecision(decision),
                        reason="authenticated operator review",
                        idempotency_key=_idempotency_key(_owner_user_id(record), command_id),
                        trace_id=_trace_id(command_id),
                    )
                )
        except CandidateProfileRepositoryError as exc:
            if exc.reason_code in {
                "CANDIDATE_PROFILE_NOT_FOUND",
                "CANDIDATE_PROFILE_STATE_CONFLICT",
                "CANDIDATE_PROFILE_INPUT_INVALID",
            }:
                raise GoalRunConflict() from None
            raise GoalRunUnavailable("candidate profile database unavailable") from None
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    def _resume_record(self, record: GoalRunRecord, command_id: UUID) -> object:
        try:
            with self._transaction_factory() as connection:
                return self._repository_factory(connection).resume(
                    GoalRunResumeCommand(
                        actor_id=_owner_user_id(record),
                        goal_run_id=record.goal_run_id,
                        expected_version=record.version,
                        fencing_token=record.fencing_token,
                        idempotency_key=_idempotency_key(_owner_user_id(record), command_id),
                        trace_id=_trace_id(command_id),
                    )
                )
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    def _cancel_record(self, record: GoalRunRecord, reason: str, command_id: UUID) -> object:
        try:
            with self._transaction_factory() as connection:
                return self._repository_factory(connection).cancel(
                    GoalRunCancelCommand(
                        actor_id=_owner_user_id(record),
                        goal_run_id=record.goal_run_id,
                        expected_version=record.version,
                        fencing_token=record.fencing_token,
                        reason=reason,
                        idempotency_key=_idempotency_key(_owner_user_id(record), command_id),
                        trace_id=_trace_id(command_id),
                    )
                )
        except GoalRunRepositoryError as exc:
            _raise_goal_run_error(exc)

    async def _start_workflow(
        self,
        record: GoalRunRecord,
        *,
        registry_id: UUID,
        source_id: str | None,
    ) -> None:
        workflow_id = _workflow_id_from_record(record)
        try:
            await (await self._client()).start_workflow(
                "GoalRunWorkflow",
                GoalRunInput(
                    goal_run_id=record.goal_run_id,
                    owner_user_id=_owner_user_id(record),
                    registry_id=_registry_id(record, registry_id),
                    source_id=_source_id(record, source_id),
                    max_records=_max_records(record),
                    fencing_token=record.fencing_token,
                    mode=_mode_from_context(_context(record)),
                    expected_version=record.version,
                ),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        except Exception as exc:
            if exc.__class__.__name__ == "WorkflowAlreadyStartedError":
                return
            raise GoalRunUnavailable("temporal unavailable") from None

    async def _signal_workflow(self, record: GoalRunRecord, *, signal: str, arg: object) -> None:
        try:
            handle = (await self._client()).get_workflow_handle(_workflow_id_from_record(record))
            await handle.signal(signal, arg)
        except Exception as exc:
            if exc.__class__.__name__ in {"WorkflowNotFoundError", "RPCError"}:
                raise GoalRunUnavailable("temporal unavailable") from None
            raise GoalRunUnavailable("temporal unavailable") from None

    async def _client(self) -> TemporalClientPort:
        if self._temporal_client is not None:
            return self._temporal_client
        async with self._temporal_lock:
            if self._temporal_client is None:
                raw_client = self._temporal_client_factory()
                if inspect.isawaitable(raw_client):
                    raw_client = await raw_client
                self._temporal_client = raw_client
        return self._temporal_client


def create_runtime_goal_run_operator_provider(
    settings: Settings,
    *,
    engine: Engine | None = None,
) -> RuntimeGoalRunOperatorProvider:
    resolved_engine = engine or create_database_engine(settings)
    return RuntimeGoalRunOperatorProvider(
        settings=settings,
        transaction_factory=resolved_engine.begin,
        repository_factory=PostgresGoalRunRepository,
        temporal_client_factory=lambda: Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            lazy=True,
        ),
    )


def _summary_from_record(record: GoalRunRecord) -> GoalRunSummary:
    context = _context(record)
    mode = _mode_from_context(context)
    waiting_review = _string_value(record.status) == "waiting_review"
    review_id = (
        _optional_uuid_attr(record, "review_item_id")
        or _optional_uuid_attr(record, "pending_review_item_id")
        if waiting_review
        else None
    )
    return GoalRunSummary(
        goal_run_id=record.goal_run_id,
        owner_user_id=_owner_user_id(record),
        registry_id=_registry_id_from_record(record, context),
        source_id=_source_id_from_record(record, context),
        mode=mode,
        completion_kind=_completion_kind(record, mode),
        status=_api_status(_string_value(record.status)),
        phase=_api_phase(_string_value(record.phase)),
        created_at=record.created_at,
        updated_at=record.updated_at,
        status_message=_status_message(record),
        pending_review_id=review_id,
        pending_review=_review_projection_from_record(record, mode) if waiting_review else None,
        temporal_workflow_id=_optional_str_attr(record, "temporal_workflow_id"),
    )


def _record_from_mutation(value: object) -> GoalRunRecord:
    record = getattr(value, "record", value)
    return cast(GoalRunRecord, record)


def _create_context(
    *,
    registry_id: UUID,
    source_id: str | None,
    mode: GoalRunMode,
    match_config: GoalRunMatchConfig | None,
    gmail_dispatch: GoalRunReviewedGmailDispatchConfig | None,
) -> dict[str, JsonValue]:
    context: dict[str, JsonValue] = {
        "registry_id": str(registry_id),
        "max_records": _DEFAULT_MAX_RECORDS,
        "mode": mode.value,
    }
    if source_id is not None:
        context["source_id"] = source_id
    if match_config is not None:
        context["match_config"] = cast(
            JsonValue,
            match_config.model_dump(mode="json", exclude_none=True),
        )
    if gmail_dispatch is not None:
        context["gmail_dispatch"] = cast(
            JsonValue,
            gmail_dispatch.model_dump(mode="json", exclude_none=True),
        )
    return context


def _validate_create_boundary(
    *,
    mode: GoalRunMode,
    match_config: GoalRunMatchConfig | None,
    candidate_id: UUID | None,
    candidate_profile_version_id: UUID | None,
    gmail_dispatch: GoalRunReviewedGmailDispatchConfig | None,
) -> None:
    if mode is GoalRunMode.PRE_APPLICATION_ONLY:
        if candidate_id is None or candidate_profile_version_id is None:
            raise GoalRunConflict("pre-application mode requires an approved profile reference")
        if match_config is not None or gmail_dispatch is not None:
            raise GoalRunConflict("pre-application inputs must come from the approved profile")
        return
    if candidate_id is not None or candidate_profile_version_id is not None:
        raise GoalRunConflict("Gmail mode cannot use a pre-application profile reference")


def _validate_existing_create(
    record: GoalRunRecord,
    *,
    command: GoalRunCreateCommand,
    mode: GoalRunMode,
    candidate_id: UUID | None,
    candidate_profile_version_id: UUID | None,
) -> None:
    context = _context(record)
    try:
        context_mode = _mode_from_context(context)
        context_registry_id = _uuid_from_mapping(context, "registry_id")
    except (GoalRunUnavailable, TypeError, ValueError):
        raise GoalRunConflict("existing GoalRun create identity is invalid") from None
    if (
        record.actor_id != command.actor_id
        or record.idempotency_key != command.idempotency_key
        or record.goal_kind != command.goal_kind
        or record.goal != command.goal
        or record.trace_id != command.trace_id
        or record.registry_id != command.registry_id
        or record.source_id != command.source_id
        or record.max_records != command.max_records
        or record.temporal_workflow_id != command.temporal_workflow_id
        or context_mode is not mode
        or context_registry_id != command.registry_id
        or context.get("max_records") != command.max_records
        or (context.get("source_id") if "source_id" in context else None)
        != command.source_id
        or ("source_id" in context) != (command.source_id is not None)
    ):
        raise GoalRunConflict("existing GoalRun create parameters do not match")

    if mode is GoalRunMode.GMAIL_DISPATCH:
        if dict(context) != dict(command.context):
            raise GoalRunConflict("existing Gmail GoalRun context does not match")
        return

    if candidate_id is None or candidate_profile_version_id is None:
        raise GoalRunConflict("existing pre-application GoalRun profile identity is missing")
    try:
        context_candidate_id = _uuid_from_mapping(context, "candidate_id")
        context_profile_version_id = _uuid_from_mapping(
            context,
            "candidate_profile_version_id",
        )
        profile_context = _mapping_from_context(context, "candidate_profile")
        profile_candidate_id = _uuid_from_mapping(profile_context, "candidate_id")
    except (TypeError, ValueError):
        raise GoalRunConflict(
            "existing pre-application GoalRun profile identity is invalid"
        ) from None
    if (
        context_candidate_id != candidate_id
        or context_profile_version_id != candidate_profile_version_id
        or profile_candidate_id != candidate_id
        or "gmail_dispatch" in context
        or not isinstance(context.get("match_config"), Mapping)
    ):
        raise GoalRunConflict("existing pre-application GoalRun profile identity does not match")


def _validate_candidate_profile_identity(
    record: CandidateProfileSnapshotRecord,
    *,
    actor_id: UUID,
    candidate_id: UUID,
    profile_version_id: UUID,
) -> None:
    if (
        record.owner_user_id != actor_id
        or record.candidate_id != candidate_id
        or record.profile_version_id != profile_version_id
        or record.decision is not CandidateProfileDecision.APPROVE
    ):
        raise GoalRunConflict("candidate profile identity is not approved")


def _revalidate_preapplication_profile(
    connection: Connection,
    goal_run: GoalRunRecord,
) -> None:
    context = _context(goal_run)
    try:
        candidate_id = _uuid_from_mapping(context, "candidate_id")
        profile_version_id = _uuid_from_mapping(context, "candidate_profile_version_id")
        snapshot_sha256 = _text_from_mapping(context, "candidate_profile_snapshot_sha256")
        material_bundle_sha256 = _text_from_mapping(
            context,
            "candidate_material_bundle_sha256",
        )
    except (TypeError, ValueError):
        raise GoalRunConflict("pre-application profile approval identity is invalid") from None
    profile_record = PostgresCandidateProfileRepository(connection).get_approved(
        ApprovedCandidateProfileQuery(
            actor_id=_owner_user_id(goal_run),
            candidate_id=candidate_id,
        )
    )
    _validate_candidate_profile_identity(
        profile_record,
        actor_id=_owner_user_id(goal_run),
        candidate_id=candidate_id,
        profile_version_id=profile_version_id,
    )
    if (
        profile_record.snapshot_sha256 != snapshot_sha256
        or profile_record.material_bundle_sha256 != material_bundle_sha256
    ):
        raise GoalRunConflict("pre-application profile approval hashes do not match")


def _mapping_from_context(
    mapping: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise TypeError(f"{key} must be a mapping")
    raw = cast("Mapping[object, object]", value)
    if any(not isinstance(item_key, str) for item_key in raw):
        raise TypeError(f"{key} must use text keys")
    return cast("Mapping[str, object]", raw)


def _uuid_from_mapping(mapping: Mapping[str, object], key: str) -> UUID:
    value = mapping.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError(f"{key} must be a UUID")


def _text_from_mapping(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be text")
    return value


def _goal_run_profile_context(
    record: CandidateProfileSnapshotRecord,
) -> dict[str, JsonValue]:
    profile = record.profile
    preferences = record.preferences
    role_titles = _profile_terms(profile, "role_titles")
    target_titles = _profile_terms(preferences, "target_titles")
    desired_titles = target_titles or role_titles
    skills = _bounded_terms(
        (
            *_profile_terms(profile, "skills"),
            *_profile_terms(preferences, "required_skills"),
            *_profile_terms(preferences, "preferred_skills"),
        ),
        100,
    )
    required_skills = _bounded_terms(
        _profile_terms(preferences, "required_skills"),
        50,
    )
    required_keywords = _bounded_terms(
        _profile_terms(preferences, "required_keywords"),
        50,
    )
    include_keywords = _bounded_terms(
        (
            *_profile_terms(preferences, "preferred_keywords"),
            *desired_titles,
        ),
        50,
    )
    excluded_terms = _bounded_terms(
        (
            *_profile_terms(preferences, "excluded_keywords"),
            *_profile_terms(preferences, "excluded_skills"),
            *_profile_terms(preferences, "excluded_companies"),
            *_profile_terms(preferences, "excluded_industries"),
        ),
        50,
    )
    work_modes = _profile_terms(preferences, "work_modes")
    remote_preference = (
        "required"
        if work_modes == ("remote",)
        else "preferred"
        if "remote" in work_modes
        else "onsite"
        if work_modes == ("onsite",)
        else "unspecified"
    )
    resume = _resume_material(record.materials)
    minimum_match_score = _profile_number(
        preferences,
        "minimum_match_score",
        default=0.5,
    )
    years_experience = _profile_optional_number(profile, "years_experience")
    minimum_salary = _profile_optional_int(preferences, "minimum_salary")
    salary_currency = _profile_optional_text(preferences, "salary_currency")
    candidate_profile = GoalRunCandidateProfileSnapshot(
        candidate_id=record.candidate_id,
        profile_version=f"candidate-profile:{record.profile_version}:{record.profile_version_id}",
        source_sha256=record.snapshot_sha256,
        headline=" / ".join(role_titles or desired_titles)[:500],
        desired_titles=desired_titles,
        skills=skills,
        required_skills=required_skills,
        required_keywords=required_keywords,
        locations=_bounded_terms(_profile_terms(preferences, "allowed_locations"), 30),
        excluded_locations=_bounded_terms(
            _profile_terms(preferences, "excluded_locations"), 30
        ),
        allowed_companies=_bounded_terms(
            _profile_terms(preferences, "allowed_companies"), 50
        ),
        allowed_industries=_bounded_terms(
            _profile_terms(preferences, "allowed_industries"), 50
        ),
        remote_preference=remote_preference,
        years_experience=years_experience,
        excluded_terms=excluded_terms,
        work_modes=cast("tuple[Any, ...]", work_modes),
        employment_types=cast(
            "tuple[Any, ...]",
            _profile_terms(preferences, "employment_types"),
        ),
        seniority_levels=cast(
            "tuple[Any, ...]",
            _profile_terms(preferences, "seniority_levels"),
        ),
        work_authorization=cast(
            "Any",
            _profile_text(profile, "work_authorization", default="unknown"),
        ),
        requires_sponsorship=_profile_optional_bool(profile, "requires_sponsorship"),
        sponsorship_allowed=_profile_optional_bool(preferences, "sponsorship_allowed"),
        minimum_salary=minimum_salary,
        salary_currency=salary_currency,
        resume=resume,
    )
    match_config = GoalRunMatchConfig(
        include_keywords=include_keywords,
        exclude_keywords=excluded_terms,
        min_score=minimum_match_score,
    )
    return {
        "candidate_id": str(record.candidate_id),
        "candidate_profile_version_id": str(record.profile_version_id),
        "candidate_profile_snapshot_sha256": record.snapshot_sha256,
        "candidate_material_bundle_sha256": record.material_bundle_sha256,
        "candidate_profile": cast(
            JsonValue,
            candidate_profile.model_dump(mode="json", exclude_none=True),
        ),
        "match_config": cast(
            JsonValue,
            match_config.model_dump(mode="json", exclude_none=True),
        ),
    }


def _resume_material(
    materials: Sequence[Mapping[str, JsonValue]],
) -> GoalRunMaterialRef:
    resume = next((item for item in materials if item.get("kind") == "resume"), None)
    if resume is None:
        raise ValueError("approved profile is missing a resume")
    return GoalRunMaterialRef(
        object_key=_profile_text(resume, "object_key"),
        filename=_profile_text(resume, "filename"),
        content_type=_profile_text(resume, "media_type"),
        size_bytes=_profile_int(resume, "byte_size"),
        sha256=_profile_text(resume, "sha256"),
    )


def _profile_terms(mapping: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = mapping.get(key, ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ValueError(f"{key} must be a sequence")
    result: list[str] = []
    for item in cast("Sequence[object]", raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} contains an invalid term")
        result.append(item.strip())
    return tuple(result)


def _bounded_terms(values: Sequence[str], maximum: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))[:maximum]


def _profile_text(
    mapping: Mapping[str, object],
    key: str,
    *,
    default: str | None = None,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be text")
    return value.strip()


def _profile_optional_text(mapping: Mapping[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    return _profile_text(mapping, key)


def _profile_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _profile_optional_int(mapping: Mapping[str, object], key: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    return _profile_int(mapping, key)


def _profile_number(
    mapping: Mapping[str, object],
    key: str,
    *,
    default: float,
) -> float:
    value = mapping.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    return float(value)


def _profile_optional_number(mapping: Mapping[str, object], key: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    return _profile_number(mapping, key, default=0.0)


def _profile_optional_bool(mapping: Mapping[str, object], key: str) -> bool | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _review_projection_from_record(
    record: GoalRunRecord,
    mode: GoalRunMode,
) -> GoalRunReviewProjection | None:
    review_id = _optional_uuid_attr(record, "review_item_id") or _optional_uuid_attr(
        record, "pending_review_item_id"
    )
    if review_id is None:
        return None
    snapshot_sha256 = getattr(record, "review_snapshot_sha256", None)
    if not isinstance(snapshot_sha256, str) or not snapshot_sha256:
        payload = getattr(record, "review_payload", {})
        if isinstance(payload, Mapping):
            payload_map = cast(Mapping[str, object], payload)
            raw_snapshot = payload_map.get("snapshot_sha256")
            if isinstance(raw_snapshot, str) and raw_snapshot:
                snapshot_sha256 = raw_snapshot
    if not isinstance(snapshot_sha256, str) or not snapshot_sha256:
        return None
    raw_kind = getattr(record, "review_kind", None)
    kind = raw_kind if isinstance(raw_kind, str) and raw_kind else "operator_review"
    raw_payload = getattr(record, "review_payload", {})
    payload = _sanitize_review_payload(
        cast(Mapping[str, object], raw_payload) if isinstance(raw_payload, Mapping) else {}
    )
    return GoalRunReviewProjection(
        review_id=review_id,
        kind=kind,
        snapshot_sha256=snapshot_sha256,
        payload=payload,
        actions=(
            (
                GoalRunReviewActionLabel(
                    action="approve",
                    label_zh="批准申请包（不会发送或投递）",  # noqa: RUF001
                ),
                GoalRunReviewActionLabel(action="reject", label_zh="拒绝"),
            )
            if mode is GoalRunMode.PRE_APPLICATION_ONLY
            else (
                GoalRunReviewActionLabel(
                    action="approve",
                    label_zh="批准进入受控 Gmail 出站链路",
                ),
                GoalRunReviewActionLabel(action="reject", label_zh="拒绝"),
            )
        ),
    )


def _mode_from_context(context: Mapping[str, object]) -> GoalRunMode:
    raw = context.get("mode")
    if raw is None:
        return GoalRunMode.GMAIL_DISPATCH
    try:
        return GoalRunMode(raw)
    except (TypeError, ValueError):
        raise GoalRunUnavailable("goal run has an invalid immutable mode") from None


def _completion_kind(record: GoalRunRecord, mode: GoalRunMode) -> str | None:
    checkpoint = getattr(record, "checkpoint", {})
    if isinstance(checkpoint, Mapping):
        checkpoint_map = cast("Mapping[str, object]", checkpoint)
        value = checkpoint_map.get("completion_kind")
        if isinstance(value, str) and value:
            return value
    if (
        mode is GoalRunMode.PRE_APPLICATION_ONLY
        and _string_value(record.status) == "completed"
        and _string_value(getattr(record, "review_decision", "")) == "approve"
    ):
        return "pre_application_package_approved"
    return None


def _sanitize_review_payload(value: Mapping[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, item in value.items():
        if _is_sensitive_review_key(key):
            continue
        sanitized[key] = _sanitize_review_value(item)
    return sanitized


def _sanitize_review_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _sanitize_review_payload(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return [_sanitize_review_value(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(_sanitize_review_value(item) for item in cast(tuple[object, ...], value))
    return value


def _is_sensitive_review_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        marker in normalized
        for marker in (
            "credential",
            "secret",
            "token",
            "oauth",
            "password",
            "api_key",
            "private_key",
            "access_key",
            "handle",
        )
    )


def _raise_goal_run_error(exc: GoalRunRepositoryError) -> NoReturn:
    if exc.reason_code in _NOT_FOUND_REASON_CODES:
        raise GoalRunNotFound() from None
    if exc.reason_code in _CONFLICT_REASON_CODES or exc.sqlstate in {"23505", "55000"}:
        raise GoalRunConflict() from None
    raise GoalRunUnavailable("goal run database unavailable") from None


def _owner_user_id(record: GoalRunRecord) -> UUID:
    value = getattr(record, "owner_user_id", None)
    if isinstance(value, UUID):
        return value
    return record.actor_id


def _context(record: GoalRunRecord) -> Mapping[str, object]:
    return cast(Mapping[str, object], getattr(record, "context", {}))


def _registry_id(record: GoalRunRecord, fallback: UUID) -> UUID:
    return cast(UUID, getattr(record, "registry_id", fallback))


def _registry_id_from_record(record: GoalRunRecord, context: Mapping[str, object]) -> UUID:
    value = getattr(record, "registry_id", None)
    if isinstance(value, UUID):
        return value
    return _uuid_from_context(context, "registry_id")


def _source_id(record: GoalRunRecord, fallback: str | None) -> str | None:
    return cast(str | None, getattr(record, "source_id", fallback))


def _source_id_from_record(
    record: GoalRunRecord,
    context: Mapping[str, object],
) -> str | None:
    missing = object()
    value = getattr(record, "source_id", missing)
    if isinstance(value, str) or value is None:
        return value
    return _str_from_context(context, "source_id")


def _max_records(record: GoalRunRecord) -> int:
    raw = getattr(record, "max_records", None) or _context(record).get("max_records")
    if isinstance(raw, int) and 1 <= raw <= 10_000:
        return raw
    return _DEFAULT_MAX_RECORDS


def _review_snapshot_sha256(record: GoalRunRecord) -> str:
    value = getattr(record, "review_snapshot_sha256", None)
    if isinstance(value, str) and value:
        return value
    payload = getattr(record, "review_payload", {})
    if isinstance(payload, Mapping):
        payload_map = cast(Mapping[str, object], payload)
        snapshot = payload_map.get("snapshot_sha256")
        if isinstance(snapshot, str) and snapshot:
            return snapshot
    raise GoalRunConflict()


def _status_message(record: GoalRunRecord) -> str | None:
    error_code = getattr(record, "last_error_code", None)
    if isinstance(error_code, str) and error_code:
        return error_code
    return None


def _optional_uuid_attr(record: GoalRunRecord, name: str) -> UUID | None:
    value = getattr(record, name, None)
    return value if isinstance(value, UUID) else None


def _optional_str_attr(record: GoalRunRecord, name: str) -> str | None:
    value = getattr(record, name, None)
    return value if isinstance(value, str) and value else None


def _uuid_from_context(context: Mapping[str, object], key: str) -> UUID:
    value = context.get(key)
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise GoalRunUnavailable("goal run record is missing registry identity")


def _str_from_context(context: Mapping[str, object], key: str) -> str | None:
    value = context.get(key)
    return value if isinstance(value, str) else None


def _string_value(value: object) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise GoalRunUnavailable("goal run record state is invalid")
    return raw


def _api_status(value: str) -> Any:
    aliases = {
        "pending": "starting",
        "queued": "starting",
        "waiting_for_review": "waiting_review",
        "succeeded": "completed",
    }
    return aliases.get(value, value)


def _api_phase(value: str) -> Any:
    aliases = {
        "created": "initializing",
        "source_selection": "selecting_source",
        "ingestion": "canonical_ingest",
        "match_draft": "draft_preparation",
        "review_queue": "review",
        "done": "completed",
    }
    return aliases.get(value, value)


def _workflow_id(actor_id: UUID, command_id: UUID) -> str:
    return f"goal-run:{actor_id}:{command_id}"


def _workflow_id_from_record(record: GoalRunRecord) -> str:
    workflow_id = _optional_str_attr(record, "temporal_workflow_id")
    if workflow_id is None:
        raise GoalRunUnavailable("goal run record is missing temporal workflow identity")
    return workflow_id


def _trace_id(command_id: UUID) -> str:
    return f"goal-run-command:{command_id}"


def _idempotency_key(actor_id: UUID, command_id: UUID) -> str:
    return f"goal-run:{actor_id}:{command_id}"


__all__: Sequence[str] = (
    "RuntimeGoalRunOperatorProvider",
    "TemporalClientPort",
    "TemporalWorkflowHandlePort",
    "create_runtime_goal_run_operator_provider",
)
