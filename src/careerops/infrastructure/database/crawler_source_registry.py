from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.base import Executable

from careerops.application.crawler_source_registry import (
    CrawlerSourceCompletion,
    CrawlerSourceFailure,
    CrawlerSourceLease,
)
from careerops.infrastructure.database.schema import (
    crawler_source_registries,
    crawler_source_registry_sources,
)

_CREATED_BY = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMMUTABLE_SOURCE_SNAPSHOT_FIELDS = (
    "source_id",
    "adapter",
    "input_artifact",
    "output_dir",
    "dependency_source_ids",
    "dependency_artifacts",
    "command_sha256",
    "source_sha256",
    "source_json",
    "cadence_seconds",
    "retry_rounds",
    "budget_json",
    "rate_limit_json",
    "robots_terms_policy",
    "canonical_ingestion_policy",
    "provenance_policy",
    "dedupe_policy",
)


class CrawlerSourceRegistryRepositoryError(RuntimeError):
    pass


class CrawlerSourceLeaseConflictError(CrawlerSourceRegistryRepositoryError):
    pass


class PostgresCrawlerSourceRegistryRepository:
    """Persist crawler registries and invoke the database-owned scheduler state machine."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("crawler source registry repository requires an explicit transaction")
        self._connection = connection

    def register_registry(
        self,
        manifest: Any,
        plans: Sequence[Any],
        registry_document: Any,
        *,
        root: Any,
        created_by: str,
        now: datetime,
    ) -> UUID:
        _require_created_by(created_by)
        _require_aware(now, "now")
        manifest_sources = {source.source_id: source for source in manifest.sources}
        source_documents = {
            source.source_id: source.model_dump(mode="json") for source in manifest.sources
        }
        plan_bindings = {plan.source_id: plan.as_dict(root=root) for plan in plans}
        registry_bindings = {
            binding["source_id"]: binding for binding in registry_document.resolved_plans
        }
        _validate_registration_bindings(
            manifest=manifest,
            registry_document=registry_document,
            manifest_sources=manifest_sources,
            source_documents=source_documents,
            plan_bindings=plan_bindings,
            registry_bindings=registry_bindings,
        )
        source_values = {
            source_id: _source_registration_values(
                source=source,
                source_document=source_documents[source_id],
                plan_binding=plan_bindings[source_id],
            )
            for source_id, source in manifest_sources.items()
        }

        replayed_registry = False
        registry_id = self._connection.scalar(
            postgresql.insert(crawler_source_registries)
            .values(
                id=uuid4(),
                kind=registry_document.kind,
                manifest_path=registry_document.manifest_path,
                manifest_sha256=registry_document.manifest_sha256,
                registry_sha256=registry_document.registry_sha256,
                source_count=len(manifest.sources),
                created_by=created_by,
                generated_at=_parse_timestamp(registry_document.generated_at),
                manifest_json=registry_document.manifest,
            )
            .on_conflict_do_nothing(index_elements=[crawler_source_registries.c.manifest_sha256])
            .returning(crawler_source_registries.c.id)
        )
        if not isinstance(registry_id, UUID):
            existing = (
                self._connection.execute(
                    sa.select(
                        crawler_source_registries.c.id,
                        crawler_source_registries.c.manifest_path,
                        crawler_source_registries.c.registry_sha256,
                        crawler_source_registries.c.source_count,
                    ).where(
                        crawler_source_registries.c.manifest_sha256
                        == registry_document.manifest_sha256
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None or not isinstance(existing["id"], UUID):
                raise CrawlerSourceRegistryRepositoryError(
                    "crawler source registry idempotency conflict did not yield a row"
                )
            _validate_existing_registry(
                existing,
                registry_document=registry_document,
                source_count=len(manifest_sources),
            )
            registry_id = cast("UUID", existing["id"])
            replayed_registry = True

        for source_id, source in manifest_sources.items():
            values = source_values[source_id]
            self._connection.execute(
                postgresql.insert(crawler_source_registry_sources)
                .values(
                    id=uuid4(),
                    registry_id=registry_id,
                    **values,
                    last_run_at=None,
                    next_run_at=now if source.enabled else None,
                    last_result=None,
                    last_error=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        crawler_source_registry_sources.c.registry_id,
                        crawler_source_registry_sources.c.source_id,
                    ]
                )
            )
        persisted_source_count = self._connection.scalar(
            sa.select(sa.func.count())
            .select_from(crawler_source_registry_sources)
            .where(crawler_source_registry_sources.c.registry_id == registry_id)
        )
        if persisted_source_count != len(manifest_sources):
            raise CrawlerSourceRegistryRepositoryError(
                "crawler source registry has an incomplete source snapshot"
            )
        if replayed_registry:
            persisted_sources = (
                self._connection.execute(_existing_source_snapshots_statement(registry_id))
                .mappings()
                .all()
            )
            _validate_existing_source_snapshots(persisted_sources, expected=source_values)
        return registry_id

    def get_registry(self, registry_id: UUID) -> RowMapping | None:
        return (
            self._connection.execute(
                sa.select(crawler_source_registries).where(
                    crawler_source_registries.c.id == registry_id
                )
            )
            .mappings()
            .one_or_none()
        )

    def get_registry_by_manifest_sha256(self, manifest_sha256: str) -> RowMapping | None:
        if _SHA256.fullmatch(manifest_sha256) is None:
            raise ValueError("manifest_sha256 must be a lowercase sha256 hex digest")
        return (
            self._connection.execute(
                sa.select(crawler_source_registries).where(
                    crawler_source_registries.c.manifest_sha256 == manifest_sha256
                )
            )
            .mappings()
            .one_or_none()
        )

    def list_due_sources(
        self,
        registry_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[RowMapping, ...]:
        _require_aware(now, "now")
        _require_limit(limit)
        rows = (
            self._connection.execute(
                sa.select(crawler_source_registry_sources)
                .where(
                    crawler_source_registry_sources.c.registry_id == registry_id,
                    crawler_source_registry_sources.c.enabled.is_(True),
                    crawler_source_registry_sources.c.robots_terms_policy == "respect",
                    sa.or_(
                        crawler_source_registry_sources.c.next_run_at.is_(None),
                        crawler_source_registry_sources.c.next_run_at <= now,
                    ),
                    sa.or_(
                        crawler_source_registry_sources.c.lease_token.is_(None),
                        crawler_source_registry_sources.c.lease_until < now,
                    ),
                )
                .order_by(
                    sa.func.coalesce(
                        crawler_source_registry_sources.c.next_run_at,
                        crawler_source_registry_sources.c.created_at,
                    ),
                    crawler_source_registry_sources.c.source_id,
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return tuple(rows)

    def claim_source(
        self,
        registry_id: UUID,
        source_id: str,
        *,
        worker_id: str,
        lease_for: timedelta,
    ) -> RowMapping:
        lease_token = uuid4()
        row = self._execute_scheduler_function(
            claim_source_statement(
                registry_id=registry_id,
                source_id=source_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_for=lease_for,
            ),
            action="claim",
        )
        if row is None:
            raise CrawlerSourceLeaseConflictError("crawler source is not claimable")
        _validate_claim_row(
            row,
            registry_id=registry_id,
            source_id=source_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        return row

    def claim_due_source(
        self,
        registry_id: UUID,
        *,
        worker_id: str,
        lease_for: timedelta,
    ) -> RowMapping | None:
        lease_token = uuid4()
        row = self._execute_scheduler_function(
            claim_due_source_statement(
                registry_id=registry_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_for=lease_for,
            ),
            action="claim",
        )
        if row is None:
            return None
        _validate_claim_row(
            row,
            registry_id=registry_id,
            source_id=None,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        return row

    def complete_source(
        self,
        *,
        source_row_id: UUID,
        run_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> RowMapping:
        completion = CrawlerSourceCompletion(
            run_id=run_id,
            source_row_id=source_row_id,
            worker_id=worker_id,
            lease_token=lease_token,
            output_manifest_sha256=output_manifest_sha256,
            cursor=cursor,
            result=result,
        )
        row = self._execute_scheduler_function(
            complete_source_statement(completion),
            action="completion",
        )
        _require_matching_run(row, run_id=run_id, action="completion")
        return cast("RowMapping", row)

    def fail_source(
        self,
        *,
        source_row_id: UUID,
        run_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> RowMapping:
        failure = CrawlerSourceFailure(
            run_id=run_id,
            source_row_id=source_row_id,
            worker_id=worker_id,
            lease_token=lease_token,
            error=error,
        )
        row = self._execute_scheduler_function(
            fail_source_statement(failure),
            action="failure",
        )
        _require_matching_run(row, run_id=run_id, action="failure")
        return cast("RowMapping", row)

    def _execute_scheduler_function(
        self,
        statement: sa.TextClause,
        *,
        action: str,
    ) -> RowMapping | None:
        try:
            return self._connection.execute(statement).mappings().one_or_none()
        except DBAPIError as error:
            normalized = _normalize_scheduler_error(error, action=action)
            if normalized is None:
                raise
            raise normalized from None


def claim_source_statement(
    *,
    registry_id: UUID,
    source_id: str,
    worker_id: str,
    lease_token: UUID,
    lease_for: timedelta,
) -> sa.TextClause:
    _require_source_id(source_id)
    _require_worker_id(worker_id)
    lease_seconds = _lease_seconds(lease_for)
    return sa.text(
        """
SELECT claimed.*, claimed.id AS source_row_id, claimed.lease_until AS lease_expires_at
FROM careerops.claim_crawler_source(
    :registry_id, :source_id, :worker_id, :lease_token, :lease_seconds
) AS claimed
"""
    ).bindparams(
        sa.bindparam("registry_id", registry_id, type_=sa.Uuid()),
        sa.bindparam("source_id", source_id, type_=sa.Text()),
        sa.bindparam("worker_id", worker_id, type_=sa.Text()),
        sa.bindparam("lease_token", lease_token, type_=sa.Uuid()),
        sa.bindparam("lease_seconds", lease_seconds, type_=sa.Integer()),
    )


def claim_due_source_statement(
    *,
    registry_id: UUID,
    worker_id: str,
    lease_token: UUID,
    lease_for: timedelta,
) -> sa.TextClause:
    _require_worker_id(worker_id)
    lease_seconds = _lease_seconds(lease_for)
    return sa.text(
        """
SELECT claimed.*, claimed.id AS source_row_id, claimed.lease_until AS lease_expires_at
FROM careerops.claim_due_crawler_source(
    :registry_id, :worker_id, :lease_token, :lease_seconds
) AS claimed
"""
    ).bindparams(
        sa.bindparam("registry_id", registry_id, type_=sa.Uuid()),
        sa.bindparam("worker_id", worker_id, type_=sa.Text()),
        sa.bindparam("lease_token", lease_token, type_=sa.Uuid()),
        sa.bindparam("lease_seconds", lease_seconds, type_=sa.Integer()),
    )


def complete_source_statement(completion: CrawlerSourceCompletion) -> sa.TextClause:
    return sa.text(
        """
SELECT
    completed.run_id,
    completed.source_id,
    :source_row_id AS source_row_id,
    :worker_id AS lease_owner,
    :output_manifest_sha256 AS output_manifest_sha256,
    :cursor AS cursor
FROM careerops.complete_crawler_source_run(
    :source_row_id,
    :worker_id,
    :lease_token,
    :cursor,
    :result,
    :output_manifest_sha256
) AS completed
"""
    ).bindparams(
        sa.bindparam("source_row_id", completion.source_row_id, type_=sa.Uuid()),
        sa.bindparam("worker_id", completion.worker_id, type_=sa.Text()),
        sa.bindparam("lease_token", completion.lease_token, type_=sa.Uuid()),
        sa.bindparam("cursor", completion.cursor, type_=sa.Text()),
        sa.bindparam("result", completion.result, type_=sa.Text()),
        sa.bindparam(
            "output_manifest_sha256",
            completion.output_manifest_sha256,
            type_=sa.Text(),
        ),
    )


def fail_source_statement(failure: CrawlerSourceFailure) -> sa.TextClause:
    return sa.text(
        """
SELECT
    failed.run_id,
    failed.source_id,
    :source_row_id AS source_row_id,
    :worker_id AS lease_owner
FROM careerops.fail_crawler_source_run(
    :source_row_id, :worker_id, :lease_token, :error
) AS failed
"""
    ).bindparams(
        sa.bindparam("source_row_id", failure.source_row_id, type_=sa.Uuid()),
        sa.bindparam("worker_id", failure.worker_id, type_=sa.Text()),
        sa.bindparam("lease_token", failure.lease_token, type_=sa.Uuid()),
        sa.bindparam("error", failure.error, type_=sa.Text()),
    )


def _source_registration_values(
    *,
    source: Any,
    source_document: object,
    plan_binding: Mapping[str, Any],
) -> dict[str, object]:
    schedule = _resolved_schedule(plan_binding.get("schedule"))
    return {
        "source_id": source.source_id,
        "adapter": source.adapter,
        "enabled": source.enabled,
        "input_artifact": plan_binding["input_artifact"],
        "output_dir": plan_binding["output_dir"],
        "dependency_source_ids": plan_binding["dependency_source_ids"],
        "dependency_artifacts": plan_binding["dependency_artifacts"],
        "command_sha256": _sha256_json(plan_binding["command"]),
        "source_sha256": plan_binding["source_sha256"],
        "source_json": source_document,
        "cursor": schedule["cursor"],
        "cadence_seconds": schedule["cadence_seconds"],
        "retry_rounds": schedule["retry_rounds"],
        "budget_json": dict(cast("Mapping[str, object]", schedule["budget"])),
        "rate_limit_json": dict(cast("Mapping[str, object]", schedule["rate_limits"])),
        "robots_terms_policy": schedule["robots_terms_policy"],
        "canonical_ingestion_policy": schedule["canonical_ingestion_policy"],
        "provenance_policy": schedule["provenance_policy"],
        "dedupe_policy": schedule["dedupe_policy"],
    }


def _existing_source_snapshots_statement(registry_id: UUID) -> sa.Select[Any]:
    return sa.select(
        crawler_source_registry_sources.c.source_id,
        crawler_source_registry_sources.c.adapter,
        crawler_source_registry_sources.c.input_artifact,
        crawler_source_registry_sources.c.output_dir,
        crawler_source_registry_sources.c.dependency_source_ids,
        crawler_source_registry_sources.c.dependency_artifacts,
        crawler_source_registry_sources.c.command_sha256,
        crawler_source_registry_sources.c.source_sha256,
        crawler_source_registry_sources.c.source_json,
        crawler_source_registry_sources.c.cadence_seconds,
        crawler_source_registry_sources.c.retry_rounds,
        crawler_source_registry_sources.c.budget_json,
        crawler_source_registry_sources.c.rate_limit_json,
        crawler_source_registry_sources.c.robots_terms_policy,
        crawler_source_registry_sources.c.canonical_ingestion_policy,
        crawler_source_registry_sources.c.provenance_policy,
        crawler_source_registry_sources.c.dedupe_policy,
    ).where(crawler_source_registry_sources.c.registry_id == registry_id)


def _validate_existing_source_snapshots(
    rows: Sequence[RowMapping],
    *,
    expected: Mapping[str, Mapping[str, object]],
) -> None:
    actual: dict[str, dict[str, object]] = {}
    for row in rows:
        source_id = row["source_id"]
        if not isinstance(source_id, str) or source_id in actual:
            raise CrawlerSourceRegistryRepositoryError(
                "crawler source registry has an invalid source snapshot"
            )
        actual[source_id] = {
            field: cast("object", row[field]) for field in _IMMUTABLE_SOURCE_SNAPSHOT_FIELDS
        }
    expected_static = {
        source_id: {field: values[field] for field in _IMMUTABLE_SOURCE_SNAPSHOT_FIELDS}
        for source_id, values in expected.items()
    }
    try:
        snapshots_match = _stable_json(actual) == _stable_json(expected_static)
    except (TypeError, ValueError) as error:
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source registry source snapshots must be stable JSON objects"
        ) from error
    if not snapshots_match:
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source registry has a different source snapshot"
        )


def _validate_registration_bindings(
    *,
    manifest: Any,
    registry_document: Any,
    manifest_sources: Mapping[str, Any],
    source_documents: Mapping[str, object],
    plan_bindings: Mapping[str, Any],
    registry_bindings: Mapping[str, Any],
) -> None:
    source_ids = set(manifest_sources)
    if (
        len(manifest_sources) != len(manifest.sources)
        or len(plan_bindings) != len(registry_document.resolved_plans)
        or source_ids != set(plan_bindings)
        or source_ids != set(registry_bindings)
    ):
        raise CrawlerSourceRegistryRepositoryError(
            "registry sources must match the manifest and resolved plans"
        )

    expected_manifest = {
        "version": manifest.version,
        "sources": [source_documents[source.source_id] for source in manifest.sources],
    }
    try:
        if _stable_json(registry_document.manifest) != _stable_json(expected_manifest):
            raise CrawlerSourceRegistryRepositoryError(
                "registry manifest snapshot does not match the manifest"
            )
        for source_id, source in manifest_sources.items():
            plan_binding = plan_bindings[source_id]
            if _stable_json(registry_bindings[source_id]) != _stable_json(plan_binding):
                raise CrawlerSourceRegistryRepositoryError(
                    "registry resolved plan snapshot does not match the execution plan"
                )
            if (
                plan_binding.get("adapter") != source.adapter
                or plan_binding.get("enabled") is not source.enabled
                or plan_binding.get("source_sha256") != _sha256_json(source_documents[source_id])
            ):
                raise CrawlerSourceRegistryRepositoryError(
                    "resolved crawler source plan does not match its manifest source"
                )
    except (TypeError, ValueError) as error:
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source registry bindings must be stable JSON objects"
        ) from error


def _validate_existing_registry(
    existing: RowMapping,
    *,
    registry_document: Any,
    source_count: int,
) -> None:
    if (
        existing["registry_sha256"] != registry_document.registry_sha256
        or existing["source_count"] != source_count
        or existing["manifest_path"] != registry_document.manifest_path
    ):
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source registry idempotency conflict has a different snapshot"
        )


def _resolved_schedule(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CrawlerSourceRegistryRepositoryError(
            "resolved crawler source plan must include a schedule"
        )
    untyped_schedule = cast("Mapping[object, object]", value)
    if not all(isinstance(key, str) for key in untyped_schedule):
        raise CrawlerSourceRegistryRepositoryError(
            "resolved crawler source schedule keys must be strings"
        )
    schedule = cast("Mapping[str, object]", value)
    required = {
        "budget",
        "cadence_seconds",
        "canonical_ingestion_policy",
        "cursor",
        "dedupe_policy",
        "provenance_policy",
        "rate_limits",
        "retry_rounds",
        "robots_terms_policy",
    }
    if not required.issubset(schedule):
        raise CrawlerSourceRegistryRepositoryError("resolved crawler source schedule is incomplete")
    if not isinstance(schedule["budget"], Mapping) or not isinstance(
        schedule["rate_limits"], Mapping
    ):
        raise CrawlerSourceRegistryRepositoryError(
            "resolved crawler source budget and rate limits must be objects"
        )
    return schedule


def _validate_claim_row(
    row: RowMapping,
    *,
    registry_id: UUID,
    source_id: str | None,
    worker_id: str,
    lease_token: UUID,
) -> None:
    try:
        lease = CrawlerSourceLease(
            registry_id=cast("UUID", row["registry_id"]),
            source_id=cast("str", row["source_id"]),
            run_id=cast("UUID", row["run_id"]),
            source_row_id=cast("UUID", row["source_row_id"]),
            worker_id=cast("str", row["lease_owner"]),
            lease_token=cast("UUID", row["lease_token"]),
            lease_expires_at=cast("datetime", row["lease_expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source claim returned an invalid lease"
        ) from error
    if (
        lease.registry_id != registry_id
        or (source_id is not None and lease.source_id != source_id)
        or lease.worker_id != worker_id
        or lease.lease_token != lease_token
    ):
        raise CrawlerSourceRegistryRepositoryError(
            "crawler source claim returned mismatched fencing identifiers"
        )


def _require_matching_run(
    row: RowMapping | None,
    *,
    run_id: UUID,
    action: str,
) -> None:
    if row is None:
        raise CrawlerSourceLeaseConflictError(
            f"crawler source {action} did not match an active lease"
        )
    if row["run_id"] != run_id:
        raise CrawlerSourceLeaseConflictError(
            f"crawler source {action} returned a mismatched run_id"
        )


def _normalize_scheduler_error(error: DBAPIError, *, action: str) -> Exception | None:
    sqlstate = getattr(error.orig, "sqlstate", None)
    if sqlstate == "55000":
        return CrawlerSourceLeaseConflictError(
            f"crawler source {action} did not match an active lease"
        )
    if sqlstate == "22023":
        return ValueError(f"crawler source {action} arguments are invalid")
    return None


def _parse_timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_created_by(created_by: str) -> None:
    if _CREATED_BY.fullmatch(created_by) is None:
        raise ValueError("created_by must be a bounded actor identifier")


def _require_worker_id(worker_id: str) -> None:
    if _CREATED_BY.fullmatch(worker_id) is None:
        raise ValueError("worker_id must be a bounded worker identifier")


def _require_source_id(source_id: str) -> None:
    if _SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("source_id must be a bounded source identifier")


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


def _lease_seconds(lease_for: timedelta) -> int:
    total_seconds = lease_for.total_seconds()
    if not total_seconds.is_integer():
        raise ValueError("lease_for must use whole seconds")
    lease_seconds = int(total_seconds)
    if not 30 <= lease_seconds <= 3_600:
        raise ValueError("lease_for must be between 30 and 3600 seconds")
    return lease_seconds


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


__all__ = [
    "CrawlerSourceLeaseConflictError",
    "CrawlerSourceRegistryRepositoryError",
    "PostgresCrawlerSourceRegistryRepository",
    "claim_due_source_statement",
    "claim_source_statement",
    "compile_query_for_test",
    "complete_source_statement",
    "fail_source_statement",
]
