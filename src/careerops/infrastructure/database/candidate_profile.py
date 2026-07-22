from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.sql import Executable

from careerops.application.candidate_profile import (
    ApprovedCandidateProfileQuery,
    CandidateProfileDecisionCommand,
    CandidateProfileGetQuery,
    CandidateProfileImportCommand,
    CandidateProfileListPage,
    CandidateProfileListQuery,
    CandidateProfileRepositoryError,
    CandidateProfileSnapshotRecord,
    candidate_profile_list_page_from_mapping,
    candidate_profile_record_from_mapping,
)

_SQLSTATE_REASON_CODES = {
    "22023": "CANDIDATE_PROFILE_INPUT_INVALID",
    "23503": "CANDIDATE_PROFILE_NOT_FOUND",
    "23505": "CANDIDATE_PROFILE_IDEMPOTENCY_CONFLICT",
    "23514": "CANDIDATE_PROFILE_STATE_CONFLICT",
    "40001": "CANDIDATE_PROFILE_RETRY_SERIALIZATION",
    "40P01": "CANDIDATE_PROFILE_RETRY_DEADLOCK",
}


class PostgresCandidateProfileRepository:
    """Candidate profile persistence restricted to migration-provided definer functions."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("candidate profile repository requires an explicit transaction")
        self._connection = connection

    def import_snapshot(
        self,
        command: CandidateProfileImportCommand,
    ) -> CandidateProfileSnapshotRecord:
        value = self._execute_json(candidate_profile_import_statement(command))
        return candidate_profile_record_from_mapping(value)

    def decide(
        self,
        command: CandidateProfileDecisionCommand,
    ) -> CandidateProfileSnapshotRecord:
        value = self._execute_json(candidate_profile_decide_statement(command))
        return candidate_profile_record_from_mapping(value)

    def get(self, query: CandidateProfileGetQuery) -> CandidateProfileSnapshotRecord:
        value = self._execute_json(candidate_profile_get_statement(query))
        return candidate_profile_record_from_mapping(value)

    def list(self, query: CandidateProfileListQuery) -> CandidateProfileListPage:
        value = self._execute_json(candidate_profile_list_statement(query))
        return candidate_profile_list_page_from_mapping(value)

    def get_approved(
        self,
        query: ApprovedCandidateProfileQuery,
    ) -> CandidateProfileSnapshotRecord:
        value = self._execute_json(candidate_profile_get_approved_statement(query))
        return candidate_profile_record_from_mapping(value)

    def _execute_json(self, statement: sa.Select[tuple[Any]]) -> Mapping[str, object]:
        try:
            value = self._connection.execute(statement).scalar_one()
        except DBAPIError as error:
            _raise_sanitized_database_error(error)
        except SQLAlchemyError:
            raise CandidateProfileRepositoryError("CANDIDATE_PROFILE_DATABASE_ERROR") from None
        if not isinstance(value, Mapping):
            raise CandidateProfileRepositoryError(
                "CANDIDATE_PROFILE_DATABASE_RESULT_INVALID"
            )
        return cast("Mapping[str, object]", value)


def candidate_profile_import_statement(
    command: CandidateProfileImportCommand,
) -> sa.Select[tuple[Any]]:
    snapshot = command.snapshot
    bundle = snapshot.material_bundle
    return sa.select(
        sa.func.careerops.candidate_profile_import(
            _uuid_param("p_actor_id", snapshot.owner_user_id),
            _uuid_param("p_candidate_id", snapshot.candidate_id),
            _text_param("p_display_name", command.display_name),
            _uuid_param("p_profile_version_id", command.profile_version_id),
            _uuid_param("p_material_bundle_id", bundle.bundle_id),
            _jsonb_param("p_profile_json", snapshot.profile.to_json()),
            _jsonb_param("p_preferences_json", snapshot.preferences.to_json()),
            _jsonb_param("p_materials_json", bundle.manifest_json),
            _text_param("p_material_bundle_sha256", bundle.bundle_sha256),
            _text_param("p_snapshot_sha256", snapshot.snapshot_sha256),
            _text_param("p_material_bundle_canonical_json", bundle.canonical_json),
            _text_param("p_snapshot_canonical_json", snapshot.canonical_json),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("candidate_profile")
    )


def candidate_profile_decide_statement(
    command: CandidateProfileDecisionCommand,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.candidate_profile_decide(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_candidate_id", command.candidate_id),
            _uuid_param("p_profile_version_id", command.profile_version_id),
            _text_param("p_snapshot_sha256", command.snapshot_sha256),
            _text_param("p_decision", command.decision.value),
            _text_param("p_reason", command.reason),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("candidate_profile")
    )


def candidate_profile_get_statement(
    query: CandidateProfileGetQuery,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.candidate_profile_get(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_candidate_id", query.candidate_id),
            _uuid_param("p_profile_version_id", query.profile_version_id),
        ).label("candidate_profile")
    )


def candidate_profile_list_statement(
    query: CandidateProfileListQuery,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.candidate_profile_list(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_candidate_id", query.candidate_id),
            _integer_param("p_limit", query.limit),
        ).label("candidate_profiles")
    )


def candidate_profile_get_approved_statement(
    query: ApprovedCandidateProfileQuery,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.candidate_profile_get_approved(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_candidate_id", query.candidate_id),
        ).label("candidate_profile")
    )


def _uuid_param(key: str, value: object) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.UUID(as_uuid=True))


def _text_param(key: str, value: str) -> sa.BindParameter[str]:
    return sa.bindparam(key, value, type_=sa.Text())


def _integer_param(key: str, value: int) -> sa.BindParameter[int]:
    return sa.bindparam(key, value, type_=sa.Integer())


def _jsonb_param(key: str, value: object) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.JSONB())


def _raise_sanitized_database_error(error: DBAPIError) -> NoReturn:
    sqlstate = _sqlstate(error)
    reason_code = _SQLSTATE_REASON_CODES.get(sqlstate or "", "CANDIDATE_PROFILE_DATABASE_ERROR")
    raise CandidateProfileRepositoryError(reason_code, sqlstate=sqlstate) from None


def _sqlstate(error: DBAPIError) -> str | None:
    origin = error.orig
    value = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
    if isinstance(value, str) and len(value) == 5:
        return value
    return None


def compile_candidate_profile_query_for_test(
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


__all__ = [
    "PostgresCandidateProfileRepository",
    "candidate_profile_decide_statement",
    "candidate_profile_get_approved_statement",
    "candidate_profile_get_statement",
    "candidate_profile_import_statement",
    "candidate_profile_list_statement",
    "compile_candidate_profile_query_for_test",
]
