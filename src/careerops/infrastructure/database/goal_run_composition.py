from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NoReturn, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import JsonValue
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.sql import Executable

from careerops.infrastructure.database.schema import (
    canonical_jobs,
    companies,
    crawler_job_ingestion_evidence,
    crawler_source_runs,
    goal_runs,
    job_posting_versions,
    job_postings,
)

type JsonObject = Mapping[str, JsonValue]

_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_MAX_TEXT_LENGTH = 8_000

_SQLSTATE_REASON_CODES = {
    "22023": "GOAL_RUN_GMAIL_COMPOSITION_INVALID_ARGUMENT",
    "23503": "GOAL_RUN_GMAIL_COMPOSITION_NOT_FOUND",
    "23505": "GOAL_RUN_GMAIL_COMPOSITION_CONFLICT",
    "23514": "GOAL_RUN_GMAIL_COMPOSITION_STATE_CONFLICT",
    "40001": "GOAL_RUN_GMAIL_COMPOSITION_RETRY_SERIALIZATION",
    "40P01": "GOAL_RUN_GMAIL_COMPOSITION_RETRY_DEADLOCK",
    "42501": "GOAL_RUN_GMAIL_COMPOSITION_FORBIDDEN",
    "P0001": "GOAL_RUN_GMAIL_COMPOSITION_REJECTED",
}


class GoalRunGmailCompositionRepositoryError(RuntimeError):
    """Sanitized composition persistence failure; never exposes SQL text or DB detail."""

    def __init__(self, reason_code: str, *, sqlstate: str | None = None) -> None:
        _validate_identifier(reason_code, "reason_code")
        if sqlstate is not None and not _SQLSTATE.fullmatch(sqlstate):
            raise ValueError("sqlstate must be a SQLSTATE code")
        self.reason_code = reason_code
        self.sqlstate = sqlstate
        message = reason_code if sqlstate is None else f"{reason_code}:{sqlstate}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoalRunCandidateQuery:
    actor_id: UUID
    goal_run_id: UUID
    source_row_id: UUID
    crawler_run_id: UUID
    limit: int = 100

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 500:
            raise ValueError("limit must be between 1 and 500")


@dataclass(frozen=True, slots=True)
class GoalRunDiscoveredJobCandidate:
    source_row_id: UUID
    crawler_run_id: UUID
    crawler_run_event_id: UUID
    canonical_job_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    source_id: str
    discovered_job_id: str
    company_name: str
    title: str
    canonical_url: str
    description: str
    keywords: tuple[str, ...]
    evidence_sha256: str
    structured_data: JsonObject
    location: str | None = None
    locations: tuple[str, ...] = ()
    department: str | None = None
    apply_url: str | None = None
    employment_type: str | None = None
    work_mode: str | None = None
    remote: bool | str | None = None
    seniority: str | None = None
    salary: JsonValue | None = None
    authorization: JsonValue | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.source_id, "source_id")
        _validate_identifier(self.discovered_job_id, "discovered_job_id")
        _validate_text(self.company_name, "company_name")
        _validate_text(self.title, "title")
        _validate_text(self.canonical_url, "canonical_url")
        _validate_text(self.description, "description")
        _validate_sha256(self.evidence_sha256, "evidence_sha256")
        for keyword in self.keywords:
            _validate_text(keyword, "keyword", max_length=160)
        for label, value in (
            ("location", self.location),
            ("department", self.department),
            ("apply_url", self.apply_url),
            ("employment_type", self.employment_type),
            ("work_mode", self.work_mode),
            ("seniority", self.seniority),
        ):
            if value is not None:
                _validate_text(value, label)
        for location in self.locations:
            _validate_text(location, "locations item", max_length=500)
        _optional_remote(self.remote)
        _validate_optional_json_value(self.salary, "salary")
        _validate_optional_json_value(self.authorization, "authorization")


@dataclass(frozen=True, slots=True)
class GoalRunRecordGmailMatchCommand:
    actor_id: UUID
    goal_run_id: UUID
    source_row_id: UUID
    crawler_run_id: UUID
    canonical_job_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    match_score: float
    match_reasons: JsonObject | Sequence[JsonValue]
    match_snapshot_sha256: str

    def __post_init__(self) -> None:
        if not 0 <= self.match_score <= 1:
            raise ValueError("match_score must be between 0 and 1")
        _validate_sha256(self.match_snapshot_sha256, "match_snapshot_sha256")


@dataclass(frozen=True, slots=True)
class GoalRunPrepareGmailCommand:
    actor_id: UUID
    goal_run_id: UUID
    target: JsonObject
    payload: JsonObject
    attachment_refs: Sequence[JsonObject]
    payload_hash: str | None = None

    def __post_init__(self) -> None:
        if self.payload_hash is not None:
            _validate_sha256(self.payload_hash, "payload_hash")


@dataclass(frozen=True, slots=True)
class GoalRunDispatchGmailCommand:
    actor_id: UUID
    goal_run_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunInspectGmailQuery:
    actor_id: UUID
    goal_run_id: UUID


@dataclass(frozen=True, slots=True)
class GoalRunGmailCompositionRecord:
    composition_id: UUID
    goal_run_id: UUID
    actor_user_id: UUID
    source_row_id: UUID
    crawler_run_id: UUID
    crawler_run_event_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    canonical_job_id: UUID
    state: str
    receipt_state: str | None
    payload_hash: str | None = None
    action_intent_id: UUID | None = None
    payload_version_id: UUID | None = None
    approval_request_id: UUID | None = None
    authorization_id: UUID | None = None
    outbox_event_id: UUID | None = None
    reservation_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.state not in {"matched", "draft_prepared", "dispatch_enqueued"}:
            raise ValueError("composition state is invalid")
        if self.payload_hash is not None:
            _validate_sha256(self.payload_hash, "payload_hash")


@dataclass(frozen=True, slots=True)
class GoalRunGmailInspection:
    goal_run_id: UUID
    state: str
    action_intent_id: UUID | None
    payload_version_id: UUID | None
    payload_hash: str | None
    outbox_event_id: UUID | None
    outbox_status: str | None
    provider_state: str | None
    reconciliation_status: str | None
    reconciliation_last_error_code: str | None
    provider_message_id: str | None = None
    provider_thread_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"matched", "draft_prepared", "dispatch_enqueued"}:
            raise ValueError("inspection state is invalid")
        if self.payload_hash is not None:
            _validate_sha256(self.payload_hash, "payload_hash")


class PostgresGoalRunGmailCompositionRepository:
    """GoalRun Gmail composition boundary.

    This repository is intentionally scoped to canonical-ingestion reads and the G014
    SECURITY DEFINER wrapper functions. It never writes Gmail tables directly and
    never performs provider IO.
    """

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError(
                "goal-run Gmail composition repository requires an explicit transaction"
            )
        self._connection = connection

    def list_candidates(
        self,
        query: GoalRunCandidateQuery,
    ) -> tuple[GoalRunDiscoveredJobCandidate, ...]:
        try:
            rows = (
                self._connection.execute(goal_run_candidate_jobs_statement(query)).mappings().all()
            )
        except DBAPIError as exc:
            _raise_sanitized_database_error(exc)
        except SQLAlchemyError:
            raise GoalRunGmailCompositionRepositoryError(
                "GOAL_RUN_GMAIL_COMPOSITION_DATABASE_ERROR"
            ) from None
        return tuple(goal_run_candidate_from_mapping(row) for row in rows)

    def record_match(
        self,
        command: GoalRunRecordGmailMatchCommand,
    ) -> GoalRunGmailCompositionRecord:
        value = self._execute_json(goal_run_record_gmail_match_statement(command))
        return goal_run_gmail_composition_record_from_mapping(value)

    def prepare_gmail(self, command: GoalRunPrepareGmailCommand) -> GoalRunGmailCompositionRecord:
        value = self._execute_json(goal_run_prepare_gmail_statement(command))
        return goal_run_gmail_composition_record_from_mapping(value)

    def dispatch_gmail(
        self,
        command: GoalRunDispatchGmailCommand,
    ) -> GoalRunGmailCompositionRecord:
        value = self._execute_json(goal_run_dispatch_gmail_statement(command))
        return goal_run_gmail_composition_record_from_mapping(value)

    def inspect_gmail(self, query: GoalRunInspectGmailQuery) -> GoalRunGmailInspection:
        value = self._execute_json(goal_run_inspect_gmail_statement(query))
        return goal_run_gmail_inspection_from_mapping(value)

    def _execute_json(self, statement: sa.Select[tuple[Any]]) -> Mapping[str, object]:
        try:
            value = self._connection.execute(statement).scalar_one()
        except DBAPIError as exc:
            _raise_sanitized_database_error(exc)
        except SQLAlchemyError:
            raise GoalRunGmailCompositionRepositoryError(
                "GOAL_RUN_GMAIL_COMPOSITION_DATABASE_ERROR"
            ) from None
        if not isinstance(value, Mapping):
            raise GoalRunGmailCompositionRepositoryError(
                "GOAL_RUN_GMAIL_COMPOSITION_RESULT_INVALID"
            )
        return cast("Mapping[str, object]", value)


def goal_run_candidate_jobs_statement(query: GoalRunCandidateQuery) -> sa.Select[tuple[Any, ...]]:
    structured = job_posting_versions.c.structured_data
    description = sa.func.left(
        sa.func.coalesce(
            structured.op("->>")("description"),
            structured.op("->>")("body"),
            structured.op("->>")("summary"),
            job_posting_versions.c.source_url,
        ),
        _MAX_TEXT_LENGTH,
    ).label("description")
    keywords = sa.func.coalesce(
        structured.op("->")("keywords"),
        sa.cast(sa.text("'[]'"), postgresql.JSONB),
    ).label("keywords")
    locations = sa.func.coalesce(
        structured.op("->")("locations"),
        structured.op("->")("source_job_locations"),
        sa.cast(sa.text("'[]'"), postgresql.JSONB),
    ).label("locations")
    location = sa.func.coalesce(
        structured.op("->>")("location"),
        structured.op("->>")("source_job_location"),
    ).label("location")
    department = sa.func.coalesce(
        structured.op("->>")("department"),
        structured.op("->>")("source_job_department"),
    ).label("department")
    apply_url = sa.func.coalesce(
        structured.op("->>")("apply_url"),
        structured.op("->>")("source_job_apply_url"),
    ).label("apply_url")
    employment_type = sa.func.coalesce(
        structured.op("->>")("employment_type"),
        structured.op("->>")("source_job_employment_type"),
    ).label("employment_type")
    work_mode = sa.func.coalesce(
        structured.op("->>")("work_mode"),
        structured.op("->>")("source_job_work_mode"),
    ).label("work_mode")
    remote = sa.func.coalesce(
        structured.op("->")("remote"),
        structured.op("->")("source_job_remote"),
    ).label("remote")
    seniority = sa.func.coalesce(
        structured.op("->>")("seniority"),
        structured.op("->>")("source_job_seniority"),
    ).label("seniority")
    salary = sa.func.coalesce(
        structured.op("->")("salary"),
        structured.op("->")("source_job_salary"),
    ).label("salary")
    authorization = sa.func.coalesce(
        structured.op("->")("authorization"),
        structured.op("->")("source_job_authorization"),
    ).label("authorization")
    discovered_job_id = sa.func.coalesce(
        structured.op("->>")("external_id"),
        job_postings.c.external_id,
    ).label("discovered_job_id")
    evidence_sha256 = crawler_job_ingestion_evidence.c.content_hash.label("evidence_sha256")
    ranked = (
        sa.select(
            crawler_job_ingestion_evidence.c.source_row_id,
            crawler_job_ingestion_evidence.c.run_id.label("crawler_run_id"),
            crawler_job_ingestion_evidence.c.run_event_id.label("crawler_run_event_id"),
            crawler_job_ingestion_evidence.c.canonical_job_id,
            crawler_job_ingestion_evidence.c.job_posting_id,
            crawler_job_ingestion_evidence.c.job_posting_version_id,
            crawler_source_runs.c.source_id,
            discovered_job_id,
            companies.c.name.label("company_name"),
            canonical_jobs.c.canonical_title.label("title"),
            job_postings.c.canonical_url,
            description,
            keywords,
            location,
            locations,
            department,
            apply_url,
            employment_type,
            work_mode,
            remote,
            seniority,
            salary,
            authorization,
            evidence_sha256,
            structured.label("structured_data"),
            crawler_job_ingestion_evidence.c.captured_at,
            sa.func.row_number()
            .over(
                partition_by=crawler_job_ingestion_evidence.c.canonical_job_id,
                order_by=(
                    crawler_job_ingestion_evidence.c.captured_at.desc(),
                    job_posting_versions.c.captured_at.desc(),
                    crawler_job_ingestion_evidence.c.job_posting_version_id.desc(),
                ),
            )
            .label("_canonical_rank"),
        )
        .select_from(
            crawler_job_ingestion_evidence.join(
                goal_runs,
                sa.and_(
                    goal_runs.c.id == _uuid_param("p_goal_run_id", query.goal_run_id),
                    goal_runs.c.actor_user_id == _uuid_param("p_actor_id", query.actor_id),
                ),
            )
            .join(
                crawler_source_runs,
                sa.and_(
                    crawler_source_runs.c.event_id == crawler_job_ingestion_evidence.c.run_event_id,
                    crawler_source_runs.c.source_row_id
                    == crawler_job_ingestion_evidence.c.source_row_id,
                    crawler_source_runs.c.run_id == crawler_job_ingestion_evidence.c.run_id,
                    crawler_source_runs.c.event_kind == "succeeded",
                    crawler_source_runs.c.registry_id == goal_runs.c.registry_id,
                    sa.or_(
                        goal_runs.c.source_id.is_(None),
                        crawler_source_runs.c.source_id == goal_runs.c.source_id,
                    ),
                ),
            )
            .join(
                canonical_jobs,
                canonical_jobs.c.id == crawler_job_ingestion_evidence.c.canonical_job_id,
            )
            .join(companies, companies.c.id == canonical_jobs.c.company_id)
            .join(
                job_postings,
                job_postings.c.id == crawler_job_ingestion_evidence.c.job_posting_id,
            )
            .join(
                job_posting_versions,
                job_posting_versions.c.id
                == crawler_job_ingestion_evidence.c.job_posting_version_id,
            )
        )
        .where(
            crawler_job_ingestion_evidence.c.source_row_id
            == _uuid_param("p_source_row_id", query.source_row_id),
            crawler_job_ingestion_evidence.c.run_id
            == _uuid_param("p_crawler_run_id", query.crawler_run_id),
        )
        .subquery("goal_run_ranked_candidate_jobs")
    )
    return (
        sa.select(ranked)
        .where(ranked.c._canonical_rank == 1)
        .order_by(
            ranked.c.captured_at.desc(),
            ranked.c.company_name.asc(),
            ranked.c.title.asc(),
            ranked.c.canonical_job_id.asc(),
            ranked.c.job_posting_version_id.desc(),
        )
        .limit(_integer_param("p_limit", query.limit))
    )


def goal_run_record_gmail_match_statement(
    command: GoalRunRecordGmailMatchCommand,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_record_gmail_match(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _uuid_param("p_source_row_id", command.source_row_id),
            _uuid_param("p_crawler_run_id", command.crawler_run_id),
            _uuid_param("p_canonical_job_id", command.canonical_job_id),
            _uuid_param("p_job_posting_id", command.job_posting_id),
            _uuid_param("p_job_posting_version_id", command.job_posting_version_id),
            _numeric_param("p_match_score", Decimal(str(command.match_score))),
            _jsonb_param("p_match_reasons", command.match_reasons),
            _text_param("p_match_snapshot_sha256", command.match_snapshot_sha256),
        ).label("goal_run_gmail_match")
    )


def goal_run_prepare_gmail_statement(command: GoalRunPrepareGmailCommand) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_prepare_gmail(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _jsonb_param("p_target", command.target),
            _jsonb_param("p_payload", command.payload),
            _jsonb_param("p_attachment_refs", command.attachment_refs),
            _optional_text_param("p_payload_hash", command.payload_hash),
        ).label("goal_run_gmail_draft")
    )


def goal_run_dispatch_gmail_statement(
    command: GoalRunDispatchGmailCommand,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_dispatch_gmail(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
        ).label("goal_run_gmail_dispatch")
    )


def goal_run_inspect_gmail_statement(query: GoalRunInspectGmailQuery) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_inspect_gmail(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_goal_run_id", query.goal_run_id),
        ).label("goal_run_gmail_inspection")
    )


def goal_run_candidate_from_mapping(
    row: RowMapping | Mapping[str, object],
) -> GoalRunDiscoveredJobCandidate:
    mapping = cast("Mapping[str, object]", row)
    structured = _json_object(mapping.get("structured_data"), "structured_data")
    return GoalRunDiscoveredJobCandidate(
        source_row_id=_uuid(mapping.get("source_row_id"), "source_row_id"),
        crawler_run_id=_uuid(mapping.get("crawler_run_id"), "crawler_run_id"),
        crawler_run_event_id=_uuid(mapping.get("crawler_run_event_id"), "crawler_run_event_id"),
        canonical_job_id=_uuid(mapping.get("canonical_job_id"), "canonical_job_id"),
        job_posting_id=_uuid(mapping.get("job_posting_id"), "job_posting_id"),
        job_posting_version_id=_uuid(
            mapping.get("job_posting_version_id"),
            "job_posting_version_id",
        ),
        source_id=_required_str(mapping, "source_id"),
        discovered_job_id=_required_str(mapping, "discovered_job_id"),
        company_name=_required_str(mapping, "company_name"),
        title=_required_str(mapping, "title"),
        canonical_url=_required_str(mapping, "canonical_url"),
        description=_required_str(mapping, "description"),
        keywords=_string_tuple(mapping.get("keywords"), "keywords", allow_empty=True),
        evidence_sha256=_required_str(mapping, "evidence_sha256"),
        structured_data=structured,
        location=_optional_str(mapping.get("location"), "location"),
        locations=_string_tuple(
            mapping.get("locations", ()),
            "locations",
            allow_empty=True,
        ),
        department=_optional_str(mapping.get("department"), "department"),
        apply_url=_optional_str(mapping.get("apply_url"), "apply_url"),
        employment_type=_optional_str(mapping.get("employment_type"), "employment_type"),
        work_mode=_optional_str(mapping.get("work_mode"), "work_mode"),
        remote=_optional_remote(mapping.get("remote")),
        seniority=_optional_str(mapping.get("seniority"), "seniority"),
        salary=_optional_json_value(mapping.get("salary"), "salary"),
        authorization=_optional_json_value(mapping.get("authorization"), "authorization"),
    )


def goal_run_gmail_composition_record_from_mapping(
    value: Mapping[str, object],
) -> GoalRunGmailCompositionRecord:
    return GoalRunGmailCompositionRecord(
        composition_id=_uuid(value.get("composition_id"), "composition_id"),
        goal_run_id=_uuid(value.get("goal_run_id"), "goal_run_id"),
        actor_user_id=_uuid(value.get("actor_user_id"), "actor_user_id"),
        source_row_id=_uuid(value.get("source_row_id"), "source_row_id"),
        crawler_run_id=_uuid(value.get("crawler_run_id"), "crawler_run_id"),
        crawler_run_event_id=_uuid(value.get("crawler_run_event_id"), "crawler_run_event_id"),
        job_posting_id=_uuid(value.get("job_posting_id"), "job_posting_id"),
        job_posting_version_id=_uuid(value.get("job_posting_version_id"), "job_posting_version_id"),
        canonical_job_id=_uuid(value.get("canonical_job_id"), "canonical_job_id"),
        state=_required_str(value, "state"),
        receipt_state=_optional_str(value.get("receipt_state"), "receipt_state"),
        payload_hash=_optional_str(value.get("payload_hash"), "payload_hash"),
        action_intent_id=_optional_uuid(value.get("action_intent_id"), "action_intent_id"),
        payload_version_id=_optional_uuid(value.get("payload_version_id"), "payload_version_id"),
        approval_request_id=_optional_uuid(value.get("approval_request_id"), "approval_request_id"),
        authorization_id=_optional_uuid(value.get("authorization_id"), "authorization_id"),
        outbox_event_id=_optional_uuid(value.get("outbox_event_id"), "outbox_event_id"),
        reservation_id=_optional_uuid(value.get("reservation_id"), "reservation_id"),
    )


def goal_run_gmail_inspection_from_mapping(value: Mapping[str, object]) -> GoalRunGmailInspection:
    return GoalRunGmailInspection(
        goal_run_id=_uuid(value.get("goal_run_id"), "goal_run_id"),
        state=_required_str(value, "state"),
        action_intent_id=_optional_uuid(value.get("action_intent_id"), "action_intent_id"),
        payload_version_id=_optional_uuid(value.get("payload_version_id"), "payload_version_id"),
        payload_hash=_optional_str(value.get("payload_hash"), "payload_hash"),
        outbox_event_id=_optional_uuid(value.get("outbox_event_id"), "outbox_event_id"),
        outbox_status=_optional_str(value.get("outbox_status"), "outbox_status"),
        provider_state=_optional_str(value.get("provider_state"), "provider_state"),
        reconciliation_status=_optional_str(
            value.get("reconciliation_status"),
            "reconciliation_status",
        ),
        reconciliation_last_error_code=_optional_str(
            value.get("reconciliation_last_error_code"),
            "reconciliation_last_error_code",
        ),
        provider_message_id=_optional_str(value.get("provider_message_id"), "provider_message_id"),
        provider_thread_id=_optional_str(value.get("provider_thread_id"), "provider_thread_id"),
    )


def _uuid_param(key: str, value: object) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.UUID(as_uuid=True))


def _text_param(key: str, value: str) -> sa.BindParameter[str]:
    return sa.bindparam(key, value, type_=sa.Text())


def _optional_text_param(key: str, value: str | None) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=sa.Text())


def _integer_param(key: str, value: int) -> sa.BindParameter[int]:
    return sa.bindparam(key, value, type_=sa.Integer())


def _numeric_param(key: str, value: Decimal) -> sa.BindParameter[Decimal]:
    return sa.bindparam(key, value, type_=sa.Numeric(6, 5))


def _jsonb_param(key: str, value: object) -> sa.BindParameter[object]:
    return sa.bindparam(key, value, type_=postgresql.JSONB())


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    _validate_text(value, key)
    return value


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    _validate_text(value, label)
    return value


def _optional_remote(value: object) -> bool | str | None:
    if value is None or isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise ValueError("remote must be a boolean or string")
    _validate_text(value, "remote", max_length=160)
    return value


def _optional_json_value(value: object, label: str) -> JsonValue | None:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain only JSON values") from exc
    return cast("JsonValue", decoded)


def _validate_optional_json_value(value: object, label: str) -> None:
    if value is not None:
        _optional_json_value(value, label)


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


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    raw = cast("Mapping[object, JsonValue]", value)
    for key in raw:
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
    return cast("dict[str, JsonValue]", dict(raw))


def _string_tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{label} must be a list")
    items = cast("Sequence[object]", value)
    if any(not isinstance(item, str) for item in items):
        raise ValueError(f"{label} items must be strings")
    string_items = cast("Sequence[str]", items)
    normalized = tuple(sorted({item.strip().lower() for item in string_items if item.strip()}))
    if not normalized and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _validate_identifier(value: str, label: str) -> None:
    if _SOURCE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_text(value: str, label: str, *, max_length: int = _MAX_TEXT_LENGTH) -> None:
    if not value.strip() or len(value) > max_length:
        raise ValueError(f"{label} must be non-empty and bounded")
    if any(ord(character) < 32 and character not in ("\n", "\t") for character in value):
        raise ValueError(f"{label} must not contain control characters")


def _validate_sha256(value: str, label: str) -> None:
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase sha256 hex digest")


def _raise_sanitized_database_error(exc: DBAPIError) -> NoReturn:
    sqlstate = _sqlstate(exc)
    reason_code = _SQLSTATE_REASON_CODES.get(
        sqlstate or "",
        "GOAL_RUN_GMAIL_COMPOSITION_DATABASE_ERROR",
    )
    raise GoalRunGmailCompositionRepositoryError(reason_code, sqlstate=sqlstate) from None


def _sqlstate(exc: DBAPIError) -> str | None:
    origin = exc.orig
    value = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
    if isinstance(value, str) and len(value) == 5:
        return value
    return None


def compile_query_for_test(
    statement: sa.ClauseElement | Executable,
    *,
    literal_binds: bool = True,
) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


PostgresGoalRunCompositionRepository = PostgresGoalRunGmailCompositionRepository


__all__ = [
    "GoalRunCandidateQuery",
    "GoalRunDiscoveredJobCandidate",
    "GoalRunDispatchGmailCommand",
    "GoalRunGmailCompositionRecord",
    "GoalRunGmailCompositionRepositoryError",
    "GoalRunGmailInspection",
    "GoalRunInspectGmailQuery",
    "GoalRunPrepareGmailCommand",
    "GoalRunRecordGmailMatchCommand",
    "PostgresGoalRunCompositionRepository",
    "PostgresGoalRunGmailCompositionRepository",
    "compile_query_for_test",
    "goal_run_candidate_from_mapping",
    "goal_run_candidate_jobs_statement",
    "goal_run_dispatch_gmail_statement",
    "goal_run_gmail_composition_record_from_mapping",
    "goal_run_gmail_inspection_from_mapping",
    "goal_run_inspect_gmail_statement",
    "goal_run_prepare_gmail_statement",
    "goal_run_record_gmail_match_statement",
]
