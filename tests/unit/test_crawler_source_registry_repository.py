from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.application.crawler_source_registry import (
    CrawlerSourceCompletion,
    CrawlerSourceFailure,
    CrawlerSourceLease,
)
from careerops.infrastructure.database.crawler_source_registry import (
    CrawlerSourceLeaseConflictError,
    CrawlerSourceRegistryRepositoryError,
    PostgresCrawlerSourceRegistryRepository,
    claim_due_source_statement,
    claim_source_statement,
    compile_query_for_test,
    complete_source_statement,
    fail_source_statement,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


class NonTransactionalConnection:
    def in_transaction(self) -> bool:
        return False


class MappingResult:
    def __init__(
        self,
        row: dict[str, object] | None,
        *,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows

    def mappings(self) -> MappingResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row

    def all(self) -> list[dict[str, object]]:
        if self._rows is not None:
            return self._rows
        return [] if self._row is None else [self._row]


class FixedResultConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.statements: list[object] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: object) -> MappingResult:
        self.statements.append(statement)
        return MappingResult(self.row)


class RaisingConnection:
    def __init__(self, error: DBAPIError) -> None:
        self.error = error

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: object) -> MappingResult:
        del statement
        raise self.error


class RegistrationConnection:
    def __init__(
        self,
        registry_id: UUID | None,
        *,
        existing: dict[str, object] | None = None,
        persisted_source_count: int = 1,
        persisted_sources: list[dict[str, object]] | None = None,
    ) -> None:
        self.registry_id = registry_id
        self.existing = existing
        self.existing_served = False
        self.persisted_sources = persisted_sources
        self.scalar_results: list[object] = [registry_id, persisted_source_count]
        self.statements: list[Any] = []

    def in_transaction(self) -> bool:
        return True

    def scalar(self, statement: object) -> object:
        del statement
        return self.scalar_results.pop(0)

    def execute(self, statement: Any) -> MappingResult:
        self.statements.append(statement)
        if self.existing is not None and not self.existing_served:
            self.existing_served = True
            return MappingResult(self.existing)
        if self.persisted_sources is not None and statement.is_select:
            return MappingResult(None, rows=self.persisted_sources)
        return MappingResult(None)


class ResolvedPlan:
    source_id = "public-ats"

    def __init__(
        self,
        source_sha256: str,
        *,
        adapter: str = "recruitment.public_ats_feed",
        enabled: bool = True,
    ) -> None:
        self.adapter = adapter
        self.enabled = enabled
        self.source_sha256 = source_sha256

    def as_dict(self, *, root: object) -> dict[str, object]:
        del root
        return {
            "adapter": self.adapter,
            "command": ["python", "crawl.py"],
            "dependency_artifacts": [],
            "dependency_source_ids": [],
            "enabled": self.enabled,
            "input_artifact": "datasets/private/triage.jsonl",
            "output_dir": "datasets/raw/ats",
            "schedule": {
                "budget": {"max_feed_bytes": 1_000, "retry_rounds": 1},
                "cadence_seconds": 3_600,
                "canonical_ingestion_policy": "canonical_job_ingestion",
                "cursor": "effective-cursor",
                "dedupe_policy": "hybrid",
                "provenance_policy": "preserve",
                "rate_limits": {"concurrency": 2},
                "retry_rounds": 3,
                "robots_terms_policy": "respect",
            },
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
        }


def _registration_inputs(
    *,
    plan_adapter: str = "recruitment.public_ats_feed",
) -> tuple[SimpleNamespace, tuple[ResolvedPlan, ...], SimpleNamespace]:
    source_document = {
        "adapter": "recruitment.public_ats_feed",
        "enabled": True,
        "schedule": {
            "budget": {"max_feed_bytes": 9_000_000},
            "rate_limits": {"concurrency": 8},
        },
        "source_id": "public-ats",
    }
    source = SimpleNamespace(
        adapter=source_document["adapter"],
        enabled=source_document["enabled"],
        model_dump=lambda *, mode: source_document,
        source_id=source_document["source_id"],
    )
    plan = ResolvedPlan(
        _test_sha256_json(source_document),
        adapter=plan_adapter,
    )
    manifest = SimpleNamespace(version=1, sources=(source,))
    registry_document = SimpleNamespace(
        generated_at=NOW.isoformat(),
        kind="configured_crawl_source_registry",
        manifest={"version": 1, "sources": [source_document]},
        manifest_path="datasets/manifests/sources.json",
        manifest_sha256="a" * 64,
        registry_sha256="b" * 64,
        resolved_plans=(plan.as_dict(root=object()),),
    )
    return manifest, (plan,), registry_document


def test_repository_requires_explicit_transaction() -> None:
    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresCrawlerSourceRegistryRepository(cast("Connection", NonTransactionalConnection()))


def test_repository_surface_matches_runtime_provider() -> None:
    claim_parameters = signature(PostgresCrawlerSourceRegistryRepository.claim_source).parameters
    complete_parameters = signature(
        PostgresCrawlerSourceRegistryRepository.complete_source
    ).parameters
    fail_parameters = signature(PostgresCrawlerSourceRegistryRepository.fail_source).parameters

    assert tuple(claim_parameters) == (
        "self",
        "registry_id",
        "source_id",
        "worker_id",
        "lease_for",
    )
    assert tuple(complete_parameters) == (
        "self",
        "source_row_id",
        "run_id",
        "worker_id",
        "lease_token",
        "output_manifest_sha256",
        "cursor",
        "result",
    )
    assert tuple(fail_parameters) == (
        "self",
        "source_row_id",
        "run_id",
        "worker_id",
        "lease_token",
        "error",
    )


def test_targeted_claim_invokes_security_definer_function_and_aliases_api_fields() -> None:
    statement = claim_source_statement(
        registry_id=uuid4(),
        source_id="public-ats",
        worker_id="crawler-worker-1",
        lease_token=uuid4(),
        lease_for=timedelta(minutes=5),
    )
    sql = _compact_sql(compile_query_for_test(statement))

    assert "FROM careerops.claim_crawler_source(" in sql
    assert "claimed.id AS source_row_id" in sql
    assert "claimed.lease_until AS lease_expires_at" in sql
    assert "'public-ats'" in sql
    assert "'crawler-worker-1'" in sql
    assert sql.endswith(") AS claimed")
    assert "UPDATE " not in sql
    assert "next_run_at" not in sql


def test_next_due_claim_invokes_database_owned_scheduler() -> None:
    statement = claim_due_source_statement(
        registry_id=uuid4(),
        worker_id="crawler-worker-1",
        lease_token=uuid4(),
        lease_for=timedelta(seconds=300),
    )
    sql = _compact_sql(compile_query_for_test(statement))

    assert "FROM careerops.claim_due_crawler_source(" in sql
    assert "claimed.id AS source_row_id" in sql
    assert "claimed.lease_until AS lease_expires_at" in sql
    assert "UPDATE " not in sql
    assert "FOR UPDATE" not in sql


def test_due_listing_matches_compliance_and_active_lease_gates() -> None:
    connection = FixedResultConnection(None)
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", connection))

    assert repository.list_due_sources(uuid4(), now=NOW) == ()
    sql = _compact_sql(compile_query_for_test(cast("Any", connection.statements[0])))

    assert "robots_terms_policy = 'respect'" in sql
    assert "lease_token IS NULL" in sql
    assert "lease_until < '2026-07-20 09:00:00+00:00'" in sql


def test_completion_and_failure_only_invoke_fenced_database_functions() -> None:
    run_id = uuid4()
    source_row_id = uuid4()
    lease_token = uuid4()
    completion = CrawlerSourceCompletion(
        run_id=run_id,
        source_row_id=source_row_id,
        worker_id="crawler-worker-1",
        lease_token=lease_token,
        output_manifest_sha256="a" * 64,
        cursor="page=2",
        result="succeeded",
    )
    failure = CrawlerSourceFailure(
        run_id=run_id,
        source_row_id=source_row_id,
        worker_id="crawler-worker-1",
        lease_token=lease_token,
        error="HTTP_429",
    )

    completion_sql = _compact_sql(compile_query_for_test(complete_source_statement(completion)))
    failure_sql = _compact_sql(compile_query_for_test(fail_source_statement(failure)))

    assert "FROM careerops.complete_crawler_source_run(" in completion_sql
    assert "AS source_row_id" in completion_sql
    assert "AS lease_owner" in completion_sql
    assert "AS output_manifest_sha256" in completion_sql
    assert "AS cursor" in completion_sql
    assert "FROM careerops.fail_crawler_source_run(" in failure_sql
    assert "AS source_row_id" in failure_sql
    assert "AS lease_owner" in failure_sql
    for sql in (completion_sql, failure_sql):
        assert "UPDATE " not in sql
        assert "next_run_at" not in sql
        assert "clock_timestamp" not in sql


def test_repository_rejects_completion_for_a_different_active_run() -> None:
    expected_run_id = uuid4()
    connection = FixedResultConnection(
        {
            "cursor": "page=2",
            "lease_owner": "crawler-worker-1",
            "output_manifest_sha256": "a" * 64,
            "run_id": uuid4(),
            "source_id": "public-ats",
            "source_row_id": uuid4(),
        }
    )
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", connection))

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="mismatched run_id"):
        repository.complete_source(
            source_row_id=uuid4(),
            run_id=expected_run_id,
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            output_manifest_sha256="a" * 64,
            cursor="page=2",
            result="succeeded",
        )


def test_repository_rejects_failure_for_a_different_active_run() -> None:
    expected_run_id = uuid4()
    connection = FixedResultConnection(
        {
            "lease_owner": "crawler-worker-1",
            "run_id": uuid4(),
            "source_id": "public-ats",
            "source_row_id": uuid4(),
        }
    )
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", connection))

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="mismatched run_id"):
        repository.fail_source(
            source_row_id=uuid4(),
            run_id=expected_run_id,
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            error="HTTP_429",
        )


@pytest.mark.parametrize(
    ("sqlstate", "expected_error", "message"),
    [
        ("55000", CrawlerSourceLeaseConflictError, "active lease"),
        ("22023", ValueError, "arguments are invalid"),
    ],
)
def test_scheduler_sqlstates_are_normalized_without_database_details(
    sqlstate: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    repository = PostgresCrawlerSourceRegistryRepository(
        cast("Connection", RaisingConnection(_dbapi_error(sqlstate)))
    )

    with pytest.raises(expected_error, match=message) as captured:
        repository.complete_source(
            source_row_id=uuid4(),
            run_id=uuid4(),
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            output_manifest_sha256=None,
            cursor=None,
            result="succeeded",
        )

    assert "sensitive database detail" not in str(captured.value)


def test_unknown_scheduler_database_errors_are_not_swallowed() -> None:
    database_error = _dbapi_error("XX000")
    repository = PostgresCrawlerSourceRegistryRepository(
        cast("Connection", RaisingConnection(database_error))
    )

    with pytest.raises(DBAPIError) as captured:
        repository.fail_source(
            source_row_id=uuid4(),
            run_id=uuid4(),
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            error="provider failed",
        )

    assert captured.value is database_error


def test_registration_persists_effective_resolved_schedule_controls() -> None:
    registry_id = uuid4()
    connection = RegistrationConnection(registry_id)
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", connection))
    manifest, plans, registry_document = _registration_inputs()

    returned_id = repository.register_registry(
        manifest,
        plans,
        registry_document,
        root=object(),
        created_by="registry-api",
        now=NOW,
    )

    assert returned_id == registry_id
    assert len(connection.statements) == 1
    compiled = connection.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.params["cursor"] == "effective-cursor"
    assert compiled.params["cadence_seconds"] == 3_600
    assert compiled.params["retry_rounds"] == 3
    assert compiled.params["budget_json"] == {
        "max_feed_bytes": 1_000,
        "retry_rounds": 1,
    }
    assert compiled.params["rate_limit_json"] == {"concurrency": 2}


def test_registration_rejects_registry_plan_snapshot_drift() -> None:
    manifest, plans, registry_document = _registration_inputs()
    tampered_binding = dict(registry_document.resolved_plans[0])
    tampered_binding["enabled"] = False
    registry_document.resolved_plans = (tampered_binding,)
    repository = PostgresCrawlerSourceRegistryRepository(
        cast("Connection", RegistrationConnection(uuid4()))
    )

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="snapshot"):
        repository.register_registry(
            manifest,
            plans,
            registry_document,
            root=object(),
            created_by="registry-api",
            now=NOW,
        )


def test_registration_rejects_plan_manifest_source_drift() -> None:
    manifest, plans, registry_document = _registration_inputs(
        plan_adapter="recruitment.page_collection"
    )
    repository = PostgresCrawlerSourceRegistryRepository(
        cast("Connection", RegistrationConnection(uuid4()))
    )

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="manifest source"):
        repository.register_registry(
            manifest,
            plans,
            registry_document,
            root=object(),
            created_by="registry-api",
            now=NOW,
        )


def test_idempotent_registration_rejects_existing_snapshot_collision() -> None:
    manifest, plans, registry_document = _registration_inputs()
    connection = RegistrationConnection(
        None,
        existing={
            "id": uuid4(),
            "manifest_path": registry_document.manifest_path,
            "registry_sha256": "c" * 64,
            "source_count": 1,
        },
    )
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", connection))

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="different snapshot"):
        repository.register_registry(
            manifest,
            plans,
            registry_document,
            root=object(),
            created_by="registry-api",
            now=NOW,
        )


def test_idempotent_registration_rejects_same_count_source_snapshot_drift() -> None:
    registry_id = uuid4()
    manifest, plans, registry_document = _registration_inputs()
    seed_connection = RegistrationConnection(registry_id)
    PostgresCrawlerSourceRegistryRepository(cast("Connection", seed_connection)).register_registry(
        manifest,
        plans,
        registry_document,
        root=object(),
        created_by="registry-api",
        now=NOW,
    )
    persisted_source = dict(
        seed_connection.statements[0].compile(dialect=postgresql.dialect()).params
    )
    persisted_source["dedupe_policy"] = "url"
    replay_connection = RegistrationConnection(
        None,
        existing={
            "id": registry_id,
            "manifest_path": registry_document.manifest_path,
            "registry_sha256": registry_document.registry_sha256,
            "source_count": 1,
        },
        persisted_sources=[persisted_source],
    )
    repository = PostgresCrawlerSourceRegistryRepository(cast("Connection", replay_connection))

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="source snapshot"):
        repository.register_registry(
            manifest,
            plans,
            registry_document,
            root=object(),
            created_by="registry-api",
            now=NOW,
        )


def test_registration_rejects_incomplete_persisted_source_snapshot() -> None:
    manifest, plans, registry_document = _registration_inputs()
    repository = PostgresCrawlerSourceRegistryRepository(
        cast(
            "Connection",
            RegistrationConnection(uuid4(), persisted_source_count=0),
        )
    )

    with pytest.raises(CrawlerSourceRegistryRepositoryError, match="incomplete"):
        repository.register_registry(
            manifest,
            plans,
            registry_document,
            root=object(),
            created_by="registry-api",
            now=NOW,
        )


def test_scheduler_models_require_real_fencing_identifiers() -> None:
    lease = CrawlerSourceLease(
        registry_id=uuid4(),
        source_id="public-ats",
        run_id=uuid4(),
        source_row_id=uuid4(),
        worker_id="crawler-worker-1",
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(minutes=5),
    )

    assert lease.run_id != lease.source_row_id
    assert isinstance(lease.lease_token, UUID)
    with pytest.raises(ValueError, match="output_manifest_sha256"):
        CrawlerSourceCompletion(
            run_id=uuid4(),
            source_row_id=uuid4(),
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            output_manifest_sha256="not-a-sha",
            cursor=None,
            result="succeeded",
        )
    with pytest.raises(ValueError, match="between 30 and 3600"):
        claim_source_statement(
            registry_id=uuid4(),
            source_id="public-ats",
            worker_id="crawler-worker-1",
            lease_token=uuid4(),
            lease_for=timedelta(seconds=29),
        )


def _compact_sql(sql: str) -> str:
    return " ".join(sql.split())


def _test_sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SqlStateError(RuntimeError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("sensitive database detail")
        self.sqlstate = sqlstate


def _dbapi_error(sqlstate: str) -> DBAPIError:
    original = SqlStateError(sqlstate)
    return DBAPIError("SELECT scheduler_function()", {}, original)
