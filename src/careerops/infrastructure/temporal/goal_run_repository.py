from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy.engine import Engine

from careerops.application.goal_run import (
    GoalRunCheckpointCommand as AppGoalRunCheckpointCommand,
)
from careerops.application.goal_run import (
    GoalRunCheckpointOutcome as AppGoalRunCheckpointOutcome,
)
from careerops.application.goal_run import (
    GoalRunPhase as AppGoalRunPhase,
)
from careerops.application.goal_run import (
    GoalRunRecord,
    GoalRunStatusQuery,
)
from careerops.application.goal_run import (
    GoalRunReviewDecision as AppGoalRunReviewDecision,
)
from careerops.application.goal_run import (
    GoalRunReviewRequestCommand as AppGoalRunReviewRequestCommand,
)
from careerops.application.goal_run import (
    GoalRunStatus as AppGoalRunStatus,
)
from careerops.application.goal_run_composition import (
    GOAL_RUN_COMPOSITION_VERSION,
    GOAL_RUN_REVIEW_KIND,
    ComposedGoalRunApplication,
    GoalRunApplicationComposer,
    GoalRunCompositionConfig,
    canonical_json_hash,
    parse_goal_run_composition_context,
    rank_goal_run_applications,
)
from careerops.application.preapplication import (
    PREAPPLICATION_EXECUTION_MODE,
    PreApplicationComposition,
    PreApplicationConfigurationError,
    compose_preapplication_review,
)
from careerops.infrastructure.database.goal_run import PostgresGoalRunRepository
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
    PostgresGoalRunCompositionRepository,
)
from careerops.workflows.goal_run_contracts import (
    GoalRunCheckpointCommand,
    GoalRunCheckpointResult,
    GoalRunDomainCommand,
    GoalRunEnsureCommand,
    GoalRunLoadCommand,
    GoalRunMode,
    GoalRunReviewDecision,
    GoalRunReviewRequestCommand,
    GoalRunReviewRequestResult,
    GoalRunSnapshot,
    GoalRunStepOutcome,
    GoalRunStepResult,
)
from careerops.workflows.goal_run_contracts import (
    GoalRunPhase as WorkflowGoalRunPhase,
)
from careerops.workflows.goal_run_contracts import (
    GoalRunStatus as WorkflowGoalRunStatus,
)

_REVIEW_ITEM_NAMESPACE = UUID("2f004459-cb5c-4e82-a23c-239617391405")
_GMAIL_SEND_CHANNEL = "gmail:send"
_GMAIL_TARGET_HOST = "gmail.googleapis.com"
_RETRYABLE_COMPOSITION_REASONS = frozenset(
    {
        "GOAL_RUN_GMAIL_COMPOSITION_RETRY_SERIALIZATION",
        "GOAL_RUN_GMAIL_COMPOSITION_RETRY_DEADLOCK",
        "GOAL_RUN_GMAIL_COMPOSITION_DATABASE_ERROR",
    }
)


class PostgresGoalRunActivityRepository:
    """Temporal-facing adapter over the application GoalRun repository."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_run(self, command: GoalRunEnsureCommand) -> GoalRunSnapshot:
        with self._engine.begin() as connection:
            record = PostgresGoalRunRepository(connection).status(_status_query(command))
        _validate_fencing(record, command)
        _validate_context(record, command)
        return _snapshot(record)

    def load_run(self, command: GoalRunLoadCommand) -> GoalRunSnapshot:
        with self._engine.begin() as connection:
            record = PostgresGoalRunRepository(connection).status(_status_query(command))
        return _snapshot(record)

    def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunCheckpointResult:
        with self._engine.begin() as connection:
            result = PostgresGoalRunRepository(connection).checkpoint(
                AppGoalRunCheckpointCommand(
                    actor_id=command.owner_user_id,
                    goal_run_id=command.goal_run_id,
                    expected_version=command.expected_version,
                    fencing_token=command.fencing_token,
                    phase=AppGoalRunPhase(command.phase.value),
                    status=AppGoalRunStatus(command.status.value),
                    outcome=_checkpoint_outcome(command.status),
                    checkpoint=_checkpoint_payload(command),
                    last_error_code=_optional_text(command.checkpoint.get("error_code")),
                    idempotency_key=command.idempotency_key,
                    trace_id=command.trace_id,
                )
            )
        return GoalRunCheckpointResult(
            goal_run_id=result.record.goal_run_id,
            version=result.record.version,
            phase=_workflow_phase_from_record(result.record),
            status=_workflow_status_from_record(result.record),
            replayed=not result.newly_created,
        )

    def request_review(self, command: GoalRunReviewRequestCommand) -> GoalRunReviewRequestResult:
        review_payload = command.review_payload
        snapshot_sha256 = command.snapshot_sha256
        review_item_id = _review_item_id(command)
        payload_with_digest: dict[str, JsonValue] = dict(review_payload)
        payload_with_digest["snapshot_sha256"] = snapshot_sha256
        payload_with_digest["review_item_id"] = str(review_item_id)
        with self._engine.begin() as connection:
            result = PostgresGoalRunRepository(connection).request_review(
                AppGoalRunReviewRequestCommand(
                    actor_id=command.owner_user_id,
                    goal_run_id=command.goal_run_id,
                    expected_version=command.expected_version,
                    fencing_token=command.fencing_token,
                    review_item_id=review_item_id,
                    review_snapshot_sha256=snapshot_sha256,
                    review_kind=command.review_kind,
                    review_payload=payload_with_digest,
                    idempotency_key=command.idempotency_key,
                    trace_id=command.trace_id,
                )
            )
        return GoalRunReviewRequestResult(
            goal_run_id=result.record.goal_run_id,
            review_item_id=review_item_id,
            snapshot_sha256=snapshot_sha256,
            version=result.record.version,
            replayed=not result.newly_created,
        )

    def match_jobs(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        try:
            with self._engine.begin() as connection:
                record = PostgresGoalRunRepository(connection).status(_domain_status_query(command))
                mode = _goal_run_mode(record)
                if mode is not GoalRunMode.PRE_APPLICATION_ONLY:
                    _validate_composition_configuration_presence(record)
                repository = PostgresGoalRunCompositionRepository(connection)
                candidates = repository.list_candidates(_candidate_query(command))
                if mode is GoalRunMode.PRE_APPLICATION_ONLY:
                    preapplication = compose_preapplication_review(
                        cast("Mapping[str, object]", record.context),
                        candidates,
                    )
                    return GoalRunStepResult(
                        outcome=GoalRunStepOutcome.SUCCEEDED,
                        reason_code="PREAPPLICATION_MATCH_RANKED",
                        output_sha256=preapplication.match_snapshot_sha256,
                        details=_preapplication_match_details(preapplication),
                    )
                composed, candidate = _compose_from_discovery(
                    record,
                    candidates,
                )
                persisted = repository.record_match(
                    GoalRunRecordGmailMatchCommand(
                        actor_id=command.owner_user_id,
                        goal_run_id=command.goal_run_id,
                        source_row_id=command.source_row_id,
                        crawler_run_id=command.crawler_run_id,
                        canonical_job_id=candidate.canonical_job_id,
                        job_posting_id=candidate.job_posting_id,
                        job_posting_version_id=candidate.job_posting_version_id,
                        match_score=composed.selected_match.score,
                        match_reasons={
                            "matched_keywords": list(composed.selected_match.matched_keywords),
                            "reason_codes": list(composed.selected_match.reason_codes),
                        },
                        match_snapshot_sha256=composed.identity.match_snapshot_sha256,
                    )
                )
        except (PreApplicationConfigurationError, _CompositionConfigurationError) as exc:
            return _blocked_step(command, reason_code=exc.reason_code)
        except GoalRunGmailCompositionRepositoryError as exc:
            return _composition_repository_failure(command, exc)
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="GMAIL_MATCH_RECORDED",
            output_sha256=composed.identity.match_snapshot_sha256,
            details=_match_details(composed, candidate, persisted),
        )

    def prepare_drafts(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        try:
            with self._engine.begin() as connection:
                record = PostgresGoalRunRepository(connection).status(_domain_status_query(command))
                mode = _goal_run_mode(record)
                if mode is not GoalRunMode.PRE_APPLICATION_ONLY:
                    _validate_composition_configuration_presence(record)
                repository = PostgresGoalRunCompositionRepository(connection)
                candidates = repository.list_candidates(_candidate_query(command))
                if mode is GoalRunMode.PRE_APPLICATION_ONLY:
                    preapplication = compose_preapplication_review(
                        cast("Mapping[str, object]", record.context),
                        candidates,
                    )
                    _require_match_snapshot(
                        command,
                        preapplication.match_snapshot_sha256,
                    )
                    return GoalRunStepResult(
                        outcome=GoalRunStepOutcome.SUCCEEDED,
                        reason_code="PREAPPLICATION_REVIEW_PREPARED",
                        output_sha256=preapplication.review_snapshot_sha256,
                        details=dict(preapplication.review_payload),
                    )
                composed, candidate = _compose_from_discovery(
                    record,
                    candidates,
                )
                repository.record_match(
                    GoalRunRecordGmailMatchCommand(
                        actor_id=command.owner_user_id,
                        goal_run_id=command.goal_run_id,
                        source_row_id=command.source_row_id,
                        crawler_run_id=command.crawler_run_id,
                        canonical_job_id=candidate.canonical_job_id,
                        job_posting_id=candidate.job_posting_id,
                        job_posting_version_id=candidate.job_posting_version_id,
                        match_score=composed.selected_match.score,
                        match_reasons={
                            "matched_keywords": list(composed.selected_match.matched_keywords),
                            "reason_codes": list(composed.selected_match.reason_codes),
                        },
                        match_snapshot_sha256=composed.identity.match_snapshot_sha256,
                    )
                )
                target, payload, attachment_refs = _gmail_draft_envelope(composed)
                prepared = repository.prepare_gmail(
                    GoalRunPrepareGmailCommand(
                        actor_id=command.owner_user_id,
                        goal_run_id=command.goal_run_id,
                        target=target,
                        payload=payload,
                        attachment_refs=attachment_refs,
                    )
                )
        except (PreApplicationConfigurationError, _CompositionConfigurationError) as exc:
            return _blocked_step(command, reason_code=exc.reason_code)
        except GoalRunGmailCompositionRepositoryError as exc:
            return _composition_repository_failure(command, exc)
        try:
            review_payload = _review_payload(composed, prepared)
        except ValueError:
            return _blocked_step(command, reason_code="BLOCKED_INVALID_DRAFT_IDENTITY")
        review_snapshot_sha256 = canonical_json_hash(review_payload)
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="GMAIL_DRAFT_PREPARED",
            output_sha256=review_snapshot_sha256,
            details=review_payload,
        )

    def inspect_dispatch(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        try:
            with self._engine.begin() as connection:
                goal_run = PostgresGoalRunRepository(connection).status(
                    _domain_status_query(command)
                )
                if _goal_run_mode(goal_run) is GoalRunMode.PRE_APPLICATION_ONLY:
                    return _blocked_step(
                        command,
                        reason_code="BLOCKED_PREAPPLICATION_DISPATCH_FORBIDDEN",
                    )
                record = PostgresGoalRunCompositionRepository(connection).dispatch_gmail(
                    GoalRunDispatchGmailCommand(
                        actor_id=command.owner_user_id,
                        goal_run_id=command.goal_run_id,
                    )
                )
        except _CompositionConfigurationError as exc:
            return _blocked_step(command, reason_code=exc.reason_code)
        except GoalRunGmailCompositionRepositoryError as exc:
            return _composition_repository_failure(command, exc)
        if record.state != "dispatch_enqueued" or record.outbox_event_id is None:
            return _blocked_step(command, reason_code="BLOCKED_GMAIL_DISPATCH_NOT_ENQUEUED")
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="GMAIL_DISPATCH_ENQUEUED",
            output_sha256=record.payload_hash,
            details=_dispatch_details(record),
        )

    def inspect_reconciliation(self, command: GoalRunDomainCommand) -> GoalRunStepResult:
        try:
            with self._engine.begin() as connection:
                goal_run = PostgresGoalRunRepository(connection).status(
                    _domain_status_query(command)
                )
                if _goal_run_mode(goal_run) is GoalRunMode.PRE_APPLICATION_ONLY:
                    return _blocked_step(
                        command,
                        reason_code="BLOCKED_PREAPPLICATION_RECONCILIATION_FORBIDDEN",
                    )
                inspection = PostgresGoalRunCompositionRepository(connection).inspect_gmail(
                    GoalRunInspectGmailQuery(
                        actor_id=command.owner_user_id,
                        goal_run_id=command.goal_run_id,
                    )
                )
        except _CompositionConfigurationError as exc:
            return _blocked_step(command, reason_code=exc.reason_code)
        except GoalRunGmailCompositionRepositoryError as exc:
            return _composition_repository_failure(command, exc)
        return _reconciliation_result(command, inspection)


class _CompositionConfigurationError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _domain_status_query(command: GoalRunDomainCommand) -> GoalRunStatusQuery:
    return GoalRunStatusQuery(
        actor_id=command.owner_user_id,
        goal_run_id=command.goal_run_id,
    )


def _candidate_query(command: GoalRunDomainCommand) -> GoalRunCandidateQuery:
    return GoalRunCandidateQuery(
        actor_id=command.owner_user_id,
        goal_run_id=command.goal_run_id,
        source_row_id=command.source_row_id,
        crawler_run_id=command.crawler_run_id,
        limit=min(command.max_records, 500),
    )


def _compose_from_discovery(
    record: GoalRunRecord,
    raw_candidates: Sequence[GoalRunDiscoveredJobCandidate],
) -> tuple[ComposedGoalRunApplication, GoalRunDiscoveredJobCandidate]:
    context = cast("Mapping[str, object]", record.context)
    match_config = _required_mapping(context.get("match_config"), "match_config")
    gmail_dispatch = _required_mapping(context.get("gmail_dispatch"), "gmail_dispatch")
    candidates = _deduplicated_candidates(raw_candidates)
    if not candidates:
        raise _CompositionConfigurationError("BLOCKED_NO_DISCOVERED_JOBS")

    applications: list[dict[str, object]] = []
    for candidate in candidates:
        applications.append(
            {
                "application_id": str(candidate.canonical_job_id),
                "channel": _GMAIL_SEND_CHANNEL,
                "job": {
                    "source_id": candidate.source_id,
                    "discovered_job_id": candidate.discovered_job_id,
                    "company_name": candidate.company_name,
                    "title": candidate.title,
                    "canonical_url": candidate.canonical_url,
                    "description": candidate.description,
                    "keywords": list(candidate.keywords),
                    "evidence_sha256": candidate.evidence_sha256,
                },
                "recipient_email": gmail_dispatch.get("recipient"),
                "subject": gmail_dispatch.get("subject"),
                "text_body": gmail_dispatch.get("text_body"),
                "attachment_refs": gmail_dispatch.get("attachment_refs"),
            }
        )
    composition_context: dict[str, object] = {
        "composition": {
            "version": GOAL_RUN_COMPOSITION_VERSION,
            "candidate_id": gmail_dispatch.get("candidate_id"),
            "sender_email": gmail_dispatch.get("sender"),
            "include_keywords": match_config.get("include_keywords"),
            "exclude_keywords": match_config.get("exclude_keywords", ()),
            "min_score": match_config.get("min_score"),
            "selected_application_id": applications[0]["application_id"],
            "applications": applications,
        }
    }
    try:
        initial = parse_goal_run_composition_context(composition_context)
        selected = next(
            (
                match
                for match in rank_goal_run_applications(initial)
                if match.selectable and match.score >= initial.min_score
            ),
            None,
        )
        if selected is None:
            raise _CompositionConfigurationError("BLOCKED_NO_MATCHING_JOB")
        config: GoalRunCompositionConfig = replace(
            initial,
            selected_application_id=selected.application_id,
        )
        composed = GoalRunApplicationComposer().compose_config(config)
    except _CompositionConfigurationError:
        raise
    except (TypeError, ValueError):
        raise _CompositionConfigurationError("BLOCKED_INVALID_COMPOSITION_CONFIGURATION") from None

    selected_candidate = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.canonical_job_id) == composed.selected_application.application_id
        ),
        None,
    )
    if selected_candidate is None:
        raise _CompositionConfigurationError("BLOCKED_SELECTED_JOB_PROVENANCE_MISSING")
    return composed, selected_candidate


def _validate_composition_configuration_presence(record: GoalRunRecord) -> None:
    context = cast("Mapping[str, object]", record.context)
    _required_mapping(context.get("match_config"), "match_config")
    _required_mapping(context.get("gmail_dispatch"), "gmail_dispatch")


def _goal_run_mode(record: GoalRunRecord) -> GoalRunMode:
    context = cast("Mapping[str, object]", record.context)
    value = context.get("mode")
    if value is None:
        # Existing records predate the explicit mode marker. Preserve their Gmail
        # semantics even when configuration is incomplete, so resume/replay cannot silently
        # reinterpret an old blocked run as a different kind of goal.
        return GoalRunMode.GMAIL_DISPATCH
    try:
        return GoalRunMode(value)
    except (TypeError, ValueError):
        raise _CompositionConfigurationError("BLOCKED_INVALID_GOAL_RUN_MODE") from None


def _require_match_snapshot(command: GoalRunDomainCommand, actual: str) -> None:
    expected = command.expected_match_snapshot_sha256
    if expected is None:
        raise _CompositionConfigurationError("BLOCKED_MATCH_SNAPSHOT_MISSING")
    if expected != actual:
        raise _CompositionConfigurationError("BLOCKED_MATCH_SNAPSHOT_DRIFT")


def _deduplicated_candidates(
    candidates: Sequence[GoalRunDiscoveredJobCandidate],
) -> tuple[GoalRunDiscoveredJobCandidate, ...]:
    result: list[GoalRunDiscoveredJobCandidate] = []
    seen: set[UUID] = set()
    for candidate in candidates:
        if candidate.canonical_job_id in seen:
            continue
        seen.add(candidate.canonical_job_id)
        result.append(candidate)
    return tuple(result)


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _CompositionConfigurationError("BLOCKED_MISSING_CONFIGURATION")
    mapping = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) for key in mapping):
        raise _CompositionConfigurationError("BLOCKED_INVALID_COMPOSITION_CONFIGURATION")
    return cast("Mapping[str, object]", mapping)


def _gmail_draft_envelope(
    composed: ComposedGoalRunApplication,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], tuple[dict[str, JsonValue], ...]]:
    gmail_payload = composed.gmail_payload
    attachment_refs = tuple(
        cast("dict[str, JsonValue]", dict(ref.canonical())) for ref in gmail_payload.attachment_refs
    )
    target: dict[str, JsonValue] = {
        "target_host": _GMAIL_TARGET_HOST,
        "channel": _GMAIL_SEND_CHANNEL,
        "adapter_id": "gmail",
        "fixture_id": "gmail-send.v1",
        "recipient_sha256": gmail_payload.recipient_sha256,
        "subject_sha256": _sha256_text(gmail_payload.subject),
        "body_sha256": gmail_payload.body_sha256,
        "grant_material_hash": gmail_payload.grant_material_hash,
        "attachment_manifest_sha256": canonical_json_hash(attachment_refs),
        "gmail_target_hash": gmail_payload.target_hash,
    }
    payload = cast(
        "dict[str, JsonValue]",
        _materialize_json(dict(gmail_payload.canonical_without_hash())),
    )
    payload["payload_hash"] = gmail_payload.payload_hash
    payload["message_id_header"] = gmail_payload.message_id_header
    return target, payload, attachment_refs


def _match_details(
    composed: ComposedGoalRunApplication,
    candidate: GoalRunDiscoveredJobCandidate,
    persisted: GoalRunGmailCompositionRecord,
) -> dict[str, JsonValue]:
    return {
        "composition_id": str(persisted.composition_id),
        "canonical_job_id": str(candidate.canonical_job_id),
        "job_posting_id": str(candidate.job_posting_id),
        "job_posting_version_id": str(candidate.job_posting_version_id),
        "match_score": composed.selected_match.score,
        "matched_keywords": list(composed.selected_match.matched_keywords),
        "reason_codes": list(composed.selected_match.reason_codes),
        "match_snapshot_sha256": composed.identity.match_snapshot_sha256,
        "job": _materialize_json(composed.selected_application.job.review_safe()),
        "receipt_state": persisted.receipt_state,
    }


def _preapplication_match_details(
    composed: PreApplicationComposition,
) -> dict[str, JsonValue]:
    job = composed.selected_job
    return {
        "mode": PREAPPLICATION_EXECUTION_MODE,
        "candidate_id": str(composed.profile.candidate_id),
        "profile_version": composed.profile.profile_version,
        "profile_snapshot_sha256": composed.profile_snapshot_sha256,
        "canonical_job_id": str(job.canonical_job_id),
        "job_posting_id": str(job.job_posting_id),
        "job_posting_version_id": str(job.job_posting_version_id),
        "match_score": composed.selected_match.score,
        "match_snapshot_sha256": composed.match_snapshot_sha256,
        "matched_skills": list(composed.selected_match.matched_skills),
        "matched_include_keywords": list(
            composed.selected_match.matched_include_keywords
        ),
        "dimension_scores": dict(composed.selected_match.dimension_scores),
        "reason_codes": list(composed.selected_match.reason_codes),
        "ranked_matches": [item.canonical() for item in composed.ranked_matches],
        "provider_io_performed": False,
    }


def _review_payload(
    composed: ComposedGoalRunApplication,
    prepared: GoalRunGmailCompositionRecord,
) -> dict[str, JsonValue]:
    if (
        prepared.state != "draft_prepared"
        or prepared.action_intent_id is None
        or prepared.payload_version_id is None
        or prepared.approval_request_id is None
        or prepared.payload_hash is None
    ):
        raise ValueError("prepared Gmail composition is missing exact draft identity")
    materialized = _materialize_json(composed.review_payload)
    if not isinstance(materialized, dict):
        raise ValueError("review payload must be an object")
    review_payload = cast("dict[str, JsonValue]", materialized)
    review_payload["review_kind"] = GOAL_RUN_REVIEW_KIND
    review_payload["composition_id"] = str(prepared.composition_id)
    review_payload["action_intent_id"] = str(prepared.action_intent_id)
    review_payload["payload_version_id"] = str(prepared.payload_version_id)
    review_payload["approval_request_id"] = str(prepared.approval_request_id)
    review_payload["payload_hash"] = prepared.payload_hash
    exact = review_payload.get("exact_payload")
    if isinstance(exact, dict):
        exact_payload = cast("dict[str, JsonValue]", exact)
        gmail_hash = exact_payload.get("payload_hash")
        if isinstance(gmail_hash, str):
            exact_payload["gmail_message_payload_hash"] = gmail_hash
        exact_payload["action_payload_hash"] = prepared.payload_hash
    return review_payload


def _dispatch_details(record: GoalRunGmailCompositionRecord) -> dict[str, JsonValue]:
    return {
        "composition_id": str(record.composition_id),
        "action_intent_id": str(record.action_intent_id),
        "payload_version_id": str(record.payload_version_id),
        "payload_hash": record.payload_hash,
        "outbox_event_id": str(record.outbox_event_id),
        "reservation_id": str(record.reservation_id) if record.reservation_id else None,
        "receipt_state": record.receipt_state,
        "provider_io_performed": False,
    }


def _reconciliation_result(
    command: GoalRunDomainCommand,
    inspection: GoalRunGmailInspection,
) -> GoalRunStepResult:
    details: dict[str, JsonValue] = {
        "goal_run_id": str(command.goal_run_id),
        "state": inspection.state,
        "action_intent_id": str(inspection.action_intent_id)
        if inspection.action_intent_id
        else None,
        "payload_version_id": str(inspection.payload_version_id)
        if inspection.payload_version_id
        else None,
        "payload_hash": inspection.payload_hash,
        "outbox_event_id": str(inspection.outbox_event_id) if inspection.outbox_event_id else None,
        "outbox_status": inspection.outbox_status,
        "provider_state": inspection.provider_state,
        "reconciliation_status": inspection.reconciliation_status,
        "reconciliation_last_error_code": inspection.reconciliation_last_error_code,
    }
    if inspection.provider_state == "sent_confirmed":
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="GMAIL_SEND_CONFIRMED",
            output_sha256=inspection.payload_hash,
            details=details,
        )
    if inspection.outbox_status == "failed" or inspection.reconciliation_status == "blocked":
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            reason_code="GMAIL_SEND_RECONCILIATION_REQUIRED",
            output_sha256=inspection.payload_hash,
            details=details,
        )
    if inspection.reconciliation_status == "resolved":
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            reason_code="GMAIL_SEND_RESOLVED_WITHOUT_RECEIPT",
            output_sha256=inspection.payload_hash,
            details=details,
        )
    if inspection.provider_state == "ambiguous" and inspection.reconciliation_status not in {
        "queued",
        "leased",
    }:
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.RECONCILIATION_REQUIRED,
            reason_code="GMAIL_SEND_AMBIGUOUS",
            output_sha256=inspection.payload_hash,
            details=details,
        )
    if inspection.state == "dispatch_enqueued" and inspection.outbox_status in {
        "pending",
        "leased",
        "published",
    }:
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.PENDING_EXTERNAL,
            reason_code="GMAIL_SEND_RECEIPT_PENDING",
            output_sha256=inspection.payload_hash,
            details=details,
        )
    return GoalRunStepResult(
        outcome=GoalRunStepOutcome.RECONCILIATION_REQUIRED,
        reason_code="GMAIL_SEND_STATE_UNCERTAIN",
        output_sha256=inspection.payload_hash,
        details=details,
    )


def _composition_repository_failure(
    command: GoalRunDomainCommand,
    exc: GoalRunGmailCompositionRepositoryError,
) -> GoalRunStepResult:
    if exc.reason_code in _RETRYABLE_COMPOSITION_REASONS:
        raise exc
    return _blocked_step(command, reason_code=exc.reason_code)


def _materialize_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        result: dict[str, JsonValue] = {}
        for key, item in mapping.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _materialize_json(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_materialize_json(item) for item in cast("Sequence[object]", value)]
    if value is None or isinstance(value, str | int | float | bool):
        return cast(JsonValue, value)
    if isinstance(value, UUID):
        return str(value)
    raise ValueError("value is not JSON serializable")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _status_query(command: GoalRunEnsureCommand | GoalRunLoadCommand) -> GoalRunStatusQuery:
    return GoalRunStatusQuery(
        actor_id=command.owner_user_id,
        goal_run_id=command.goal_run_id,
    )


def _snapshot(record: GoalRunRecord) -> GoalRunSnapshot:
    return GoalRunSnapshot(
        goal_run_id=record.goal_run_id,
        owner_user_id=record.actor_id,
        version=record.version,
        fencing_token=record.fencing_token,
        phase=_workflow_phase_from_record(record),
        status=_workflow_status_from_record(record),
        source_id=record.source_id,
        pending_review_item_id=record.review_item_id,
        pending_review_snapshot_sha256=record.review_snapshot_sha256,
        review_decision=_workflow_review_decision(record),
        last_error_code=record.last_error_code,
        mode=_goal_run_mode(record),
    )


def _validate_fencing(record: GoalRunRecord, command: GoalRunEnsureCommand) -> None:
    if command.fencing_token != record.fencing_token:
        raise ValueError("goal run fencing token mismatch")


def _validate_context(record: GoalRunRecord, command: GoalRunEnsureCommand) -> None:
    if _goal_run_mode(record) is not command.mode:
        raise ValueError("goal run mode mismatch")
    if record.registry_id != command.registry_id:
        raise ValueError("goal run registry mismatch")
    if (
        command.source_id is not None
        and record.source_id is not None
        and record.source_id != command.source_id
    ):
        raise ValueError("goal run source mismatch")
    if record.max_records != command.max_records:
        raise ValueError("goal run max_records mismatch")
    if record.temporal_workflow_id != command.temporal_workflow_id:
        raise ValueError("goal run temporal workflow mismatch")


def _checkpoint_payload(command: GoalRunCheckpointCommand) -> dict[str, JsonValue]:
    checkpoint = command.checkpoint
    payload = dict(cast("Mapping[str, JsonValue]", checkpoint))
    if command.outcome:
        payload["outcome"] = command.outcome
    return payload


def _checkpoint_outcome(status: WorkflowGoalRunStatus) -> AppGoalRunCheckpointOutcome:
    if status is WorkflowGoalRunStatus.WAITING_REVIEW:
        return AppGoalRunCheckpointOutcome.WAITING_REVIEW
    if status is WorkflowGoalRunStatus.BLOCKED:
        return AppGoalRunCheckpointOutcome.BLOCKED
    if status is WorkflowGoalRunStatus.RECONCILIATION_REQUIRED:
        return AppGoalRunCheckpointOutcome.RECONCILIATION_REQUIRED
    if status in {
        WorkflowGoalRunStatus.COMPLETED,
        WorkflowGoalRunStatus.FAILED,
        WorkflowGoalRunStatus.CANCELLED,
        WorkflowGoalRunStatus.REJECTED,
    }:
        return AppGoalRunCheckpointOutcome.FINALIZE
    return AppGoalRunCheckpointOutcome.PROGRESS


def _workflow_phase_from_record(record: GoalRunRecord) -> WorkflowGoalRunPhase:
    return WorkflowGoalRunPhase(record.phase.value)


def _workflow_status_from_record(record: GoalRunRecord) -> WorkflowGoalRunStatus:
    return WorkflowGoalRunStatus(record.status.value)


def _workflow_review_decision(record: GoalRunRecord) -> GoalRunReviewDecision | None:
    if record.review_decision is AppGoalRunReviewDecision.APPROVE:
        return GoalRunReviewDecision.APPROVE
    if record.review_decision is AppGoalRunReviewDecision.REJECT:
        return GoalRunReviewDecision.REJECT
    return None


def _review_item_id(command: GoalRunReviewRequestCommand) -> UUID:
    return uuid5(
        _REVIEW_ITEM_NAMESPACE,
        f"{command.goal_run_id}:{command.snapshot_sha256}:{command.idempotency_key}",
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    return value


def _blocked_step(command: GoalRunDomainCommand, *, reason_code: str) -> GoalRunStepResult:
    details: dict[str, JsonValue] = {
        "reason_code": reason_code,
        "goal_run_id": str(command.goal_run_id),
        "registry_id": str(command.registry_id),
        "source_id": command.source_id,
        "source_row_id": str(command.source_row_id),
        "crawler_run_id": str(command.crawler_run_id),
    }
    return GoalRunStepResult(
        outcome=GoalRunStepOutcome.BLOCKED_CONFIGURATION,
        reason_code=reason_code,
        details=details,
    )


__all__ = ["PostgresGoalRunActivityRepository"]
