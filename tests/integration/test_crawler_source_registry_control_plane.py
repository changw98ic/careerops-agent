from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DBAPIError

from careerops.infrastructure.database.schema import (
    crawler_source_registries,
    crawler_source_registry_sources,
    crawler_source_runs,
)

pytestmark = pytest.mark.integration

DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
NOW = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)

SCHEDULER_RUNTIME_COLUMNS = {
    "lease_owner",
    "lease_token",
    "lease_until",
    "active_run_id",
    "attempt_count",
    "consecutive_failures",
    "completed_at",
}
CLAIM_FUNCTION = "careerops.claim_due_crawler_source(uuid, text, uuid, integer)"
TARGETED_CLAIM_FUNCTION = "careerops.claim_crawler_source(uuid, text, text, uuid, integer)"
COMPLETE_FUNCTION = "careerops.complete_crawler_source_run(uuid, text, uuid, text, text, text)"
FAIL_FUNCTION = "careerops.fail_crawler_source_run(uuid, text, uuid, text)"

PAGE_COLLECTION_BUDGET = {
    "timeout_seconds": 20.0,
    "max_stored_bytes": 1_073_741_824,
    "max_response_bytes": 1_000_000,
    "depth": 1,
    "retry_rounds": 1,
}
PAGE_COLLECTION_RATE_LIMITS = {
    "concurrency": 4,
    "max_concurrency_per_host": 2,
    "host_failure_cooldown_threshold": 3,
    "host_failure_cooldown_seconds": 30.0,
}
SITEMAP_BUDGET = {
    "timeout_seconds": 20.0,
    "max_sitemap_bytes": 5_000_000,
    "retry_rounds": 1,
}
SITEMAP_RATE_LIMITS = {"concurrency": 4}
PUBLIC_ATS_BUDGET = {
    "timeout_seconds": 20.0,
    "max_feed_bytes": 10_000_000,
    "retry_rounds": 1,
}
PUBLIC_ATS_RATE_LIMITS: dict[str, object] = {"concurrency": 4}


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "crawler source registry integration tests require a disposable database named "
            f"{DISPOSABLE_DATABASE_PREFIX}*"
        )
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")

    database_engine = sa.create_engine(database_url)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def test_api_registry_registration_is_idempotent_and_does_not_rewrite_snapshot(
    connection: Connection,
) -> None:
    registry_id = uuid4()
    source_ids = ("greenhouse", "workday")

    connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
    first_registry_id = connection.scalar(
        sa.text(
            "INSERT INTO careerops.crawler_source_registries ("
            "id, kind, manifest_path, manifest_sha256, registry_sha256, source_count, "
            "created_by, generated_at, manifest_json"
            ") VALUES ("
            ":id, 'configured_crawl_source_registry', "
            "'datasets/manifests/crawl-sources.json', :manifest_sha256, :registry_sha256, "
            ":source_count, 'careerops-crawl-sources', :generated_at, "
            "CAST(:manifest_json AS jsonb)"
            ") ON CONFLICT (manifest_sha256) DO NOTHING RETURNING id"
        ),
        {
            "id": registry_id,
            "manifest_sha256": "a" * 64,
            "registry_sha256": "b" * 64,
            "source_count": len(source_ids),
            "generated_at": NOW,
            "manifest_json": json.dumps({"version": "test", "sources": list(source_ids)}),
        },
    )
    for source_id in source_ids:
        connection.execute(
            crawler_source_registry_sources.insert().values(
                id=uuid4(),
                registry_id=registry_id,
                source_id=source_id,
                adapter="recruitment.page_collection",
                enabled=True,
                input_artifact=f"datasets/input/{source_id}.jsonl",
                output_dir=f"datasets/output/{source_id}",
                dependency_source_ids=[],
                dependency_artifacts=[],
                command_sha256=_sha(source_id, "command"),
                source_sha256=_sha(source_id, "source"),
                source_json={"source_id": source_id},
                cursor=None,
                cadence_seconds=900,
                retry_rounds=1,
                budget_json=PAGE_COLLECTION_BUDGET,
                rate_limit_json=PAGE_COLLECTION_RATE_LIMITS,
                robots_terms_policy="respect",
                canonical_ingestion_policy="dedupe_only",
                provenance_policy="preserve",
                dedupe_policy="hybrid",
                last_run_at=None,
                next_run_at=NOW,
                last_result=None,
                last_error=None,
            )
        )
    replay_registry_id = connection.scalar(
        sa.text(
            "INSERT INTO careerops.crawler_source_registries ("
            "id, kind, manifest_path, manifest_sha256, registry_sha256, source_count, "
            "created_by, generated_at, manifest_json"
            ") VALUES ("
            ":id, 'configured_crawl_source_registry', "
            "'datasets/manifests/crawl-sources.json', :manifest_sha256, :registry_sha256, "
            ":source_count, 'careerops-crawl-sources', :generated_at, "
            "CAST(:manifest_json AS jsonb)"
            ") ON CONFLICT (manifest_sha256) DO NOTHING RETURNING id"
        ),
        {
            "id": uuid4(),
            "manifest_sha256": "a" * 64,
            "registry_sha256": "c" * 64,
            "source_count": len(source_ids),
            "generated_at": NOW + timedelta(minutes=10),
            "manifest_json": json.dumps({"version": "test", "sources": list(source_ids)}),
        },
    )
    connection.execute(sa.text("RESET ROLE"))

    assert first_registry_id == registry_id
    assert replay_registry_id is None
    assert _count(connection, crawler_source_registries) == 1
    assert _count(connection, crawler_source_registry_sources) == len(source_ids)
    registry = connection.execute(sa.select(crawler_source_registries)).mappings().one()
    assert registry["registry_sha256"] == "b" * 64
    assert registry["source_count"] == len(source_ids)


def test_due_source_claims_are_exclusive_and_lease_backed(connection: Connection) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(connection, registry_id, "greenhouse")
    competing_token = uuid4()

    _assert_scheduler_runtime_contract(connection)
    first_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_due_crawler_source("
                ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "lease_owner": "crawler-worker-a",
                "lease_token": uuid4(),
                "lease_seconds": 300,
            },
        )
        .mappings()
        .all()
    )
    competing_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_due_crawler_source("
                ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "lease_owner": "crawler-worker-b",
                "lease_token": competing_token,
                "lease_seconds": 300,
            },
        )
        .mappings()
        .all()
    )

    assert [row["id"] for row in first_claim] == [source_id]
    assert first_claim[0]["run_id"] == first_claim[0]["active_run_id"]
    assert competing_claim == []
    assert _count(connection, crawler_source_runs) == 1


def test_targeted_source_claim_uses_same_due_and_lease_gates(connection: Connection) -> None:
    registry_id = _insert_registry(connection)
    _insert_source(connection, registry_id, "greenhouse", robots_terms_policy="review_required")
    source_id = _insert_source(connection, registry_id, "lever")
    lease_token = uuid4()

    blocked_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_crawler_source("
                ":registry_id, :source_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "source_id": "greenhouse",
                "lease_owner": "crawler-worker",
                "lease_token": uuid4(),
                "lease_seconds": 300,
            },
        )
        .mappings()
        .all()
    )
    targeted_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_crawler_source("
                ":registry_id, :source_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "source_id": "lever",
                "lease_owner": "crawler-worker",
                "lease_token": lease_token,
                "lease_seconds": 300,
            },
        )
        .mappings()
        .all()
    )

    assert blocked_claim == []
    assert [row["id"] for row in targeted_claim] == [source_id]
    assert targeted_claim[0]["lease_token"] == lease_token


def test_page_collection_accepts_effective_one_gibibyte_storage_budget(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(
        connection,
        registry_id,
        "page-source",
        budget_json=PAGE_COLLECTION_BUDGET,
        rate_limit_json=PAGE_COLLECTION_RATE_LIMITS,
    )

    row = _source_row(connection, source_id)

    assert row["adapter"] == "recruitment.page_collection"
    assert row["budget_json"]["max_stored_bytes"] == 1_073_741_824
    assert row["budget_json"]["retry_rounds"] == 1


def test_expired_lease_reclaim_records_expiry_and_rejects_old_completion(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(connection, registry_id, "greenhouse")
    old_token = uuid4()
    new_token = uuid4()

    first_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_due_crawler_source("
                ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "lease_owner": "crawler-worker-a",
                "lease_token": old_token,
                "lease_seconds": 30,
            },
        )
        .mappings()
        .one()
    )
    connection.execute(
        sa.text(
            "UPDATE careerops.crawler_source_registry_sources "
            "SET last_run_at = clock_timestamp() - interval '31 seconds', "
            "lease_until = clock_timestamp() - interval '1 second', "
            "next_run_at = clock_timestamp() - interval '1 second' "
            "WHERE id = :source_id"
        ),
        {"source_id": source_id},
    )

    second_claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_due_crawler_source("
                ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "lease_owner": "crawler-worker-b",
                "lease_token": new_token,
                "lease_seconds": 300,
            },
        )
        .mappings()
        .one()
    )

    assert second_claim["run_id"] != first_claim["run_id"]
    assert second_claim["attempt_count"] == 1
    assert second_claim["consecutive_failures"] == 1
    events = connection.execute(
        sa.select(crawler_source_runs.c.event_kind, crawler_source_runs.c.lease_token)
        .where(crawler_source_runs.c.source_row_id == source_id)
        .order_by(crawler_source_runs.c.created_at)
    ).all()
    assert [(event.event_kind, event.lease_token) for event in events] == [
        ("claimed", old_token),
        ("expired", old_token),
        ("claimed", new_token),
    ]

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as stale_completion_error:
            connection.execute(
                sa.text(
                    "SELECT careerops.complete_crawler_source_run("
                    ":source_row_id, :lease_owner, :lease_token, :cursor, :result, "
                    ":output_manifest_sha256)"
                ),
                {
                    "source_row_id": source_id,
                    "lease_owner": "crawler-worker-a",
                    "lease_token": old_token,
                    "cursor": "page=stale",
                    "result": "succeeded",
                    "output_manifest_sha256": "e" * 64,
                },
            )
        savepoint.rollback()

    assert _sqlstate(stale_completion_error.value) == "55000"
    assert _source_row(connection, source_id)["lease_token"] == new_token


def test_cursor_completion_clears_lease_and_schedules_next_run(connection: Connection) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(connection, registry_id, "lever", cursor="page=1")
    lease_token = uuid4()

    _assert_scheduler_runtime_contract(connection)
    connection.execute(
        sa.text(
            "SELECT * FROM careerops.claim_due_crawler_source("
            ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
        ),
        {
            "registry_id": registry_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "lease_seconds": 300,
        },
    )
    connection.execute(
        sa.text(
            "SELECT careerops.complete_crawler_source_run("
            ":source_row_id, :lease_owner, :lease_token, :cursor, :result, "
            ":output_manifest_sha256)"
        ),
        {
            "source_row_id": source_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "cursor": "page=2",
            "result": "succeeded",
            "output_manifest_sha256": "c" * 64,
        },
    )
    row = _source_row(connection, source_id)

    assert row["cursor"] == "page=2"
    assert row["last_result"] == "succeeded"
    assert row["lease_owner"] is None
    assert row["active_run_id"] is None
    assert row["completed_at"] is not None
    assert row["next_run_at"] > row["completed_at"]
    events = connection.execute(
        sa.select(crawler_source_runs.c.event_kind, crawler_source_runs.c.output_manifest_sha256)
        .where(crawler_source_runs.c.source_row_id == source_id)
        .order_by(crawler_source_runs.c.created_at)
    ).all()
    assert [event.event_kind for event in events] == ["claimed", "succeeded"]
    assert events[-1].output_manifest_sha256 == "c" * 64


def test_failed_source_run_releases_lease_and_applies_retry_backoff(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(connection, registry_id, "workday", retry_rounds=2)
    lease_token = uuid4()

    _assert_scheduler_runtime_contract(connection)
    connection.execute(
        sa.text(
            "SELECT * FROM careerops.claim_due_crawler_source("
            ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
        ),
        {
            "registry_id": registry_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "lease_seconds": 300,
        },
    )
    connection.execute(
        sa.text(
            "SELECT careerops.fail_crawler_source_run("
            ":source_row_id, :lease_owner, :lease_token, :error)"
        ),
        {
            "source_row_id": source_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "error": "HTTP_429",
        },
    )
    row = _source_row(connection, source_id)

    assert row["cursor"] is None
    assert row["last_result"] == "failed"
    assert row["last_error"] == "HTTP_429"
    assert row["lease_owner"] is None
    assert row["active_run_id"] is None
    assert row["attempt_count"] == 1
    assert row["next_run_at"] > row["last_run_at"]
    assert row["next_run_at"] < row["last_run_at"] + timedelta(hours=1)


def test_mismatched_completion_token_is_rejected_and_preserves_lease(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(connection, registry_id, "workday")
    lease_token = uuid4()

    connection.execute(
        sa.text(
            "SELECT * FROM careerops.claim_due_crawler_source("
            ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
        ),
        {
            "registry_id": registry_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "lease_seconds": 300,
        },
    )

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as stale_completion_error:
            connection.execute(
                sa.text(
                    "SELECT careerops.complete_crawler_source_run("
                    ":source_row_id, :lease_owner, :lease_token, :cursor, :result, "
                    ":output_manifest_sha256)"
                ),
                {
                    "source_row_id": source_id,
                    "lease_owner": "crawler-worker",
                    "lease_token": uuid4(),
                    "cursor": "page=2",
                    "result": "succeeded",
                    "output_manifest_sha256": "d" * 64,
                },
            )
        savepoint.rollback()

    assert _sqlstate(stale_completion_error.value) == "55000"
    assert _source_row(connection, source_id)["lease_token"] == lease_token


def test_canonical_ingestion_completion_requires_output_manifest_evidence(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)
    source_id = _insert_source(
        connection,
        registry_id,
        "canonical-source",
        adapter="recruitment.public_ats_feed",
        budget_json=PUBLIC_ATS_BUDGET,
        rate_limit_json=PUBLIC_ATS_RATE_LIMITS,
        canonical_ingestion_policy="canonical_job_ingestion",
    )
    lease_token = uuid4()
    connection.execute(
        sa.text(
            "SELECT * FROM careerops.claim_due_crawler_source("
            ":registry_id, :lease_owner, :lease_token, :lease_seconds)"
        ),
        {
            "registry_id": registry_id,
            "lease_owner": "crawler-worker",
            "lease_token": lease_token,
            "lease_seconds": 300,
        },
    )

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as missing_evidence_error:
            connection.execute(
                sa.text(
                    "SELECT careerops.complete_crawler_source_run("
                    ":source_row_id, :lease_owner, :lease_token, :cursor, :result, NULL)"
                ),
                {
                    "source_row_id": source_id,
                    "lease_owner": "crawler-worker",
                    "lease_token": lease_token,
                    "cursor": None,
                    "result": "succeeded",
                },
            )
        savepoint.rollback()

    assert _sqlstate(missing_evidence_error.value) == "22023"
    assert _source_row(connection, source_id)["lease_token"] == lease_token


def test_readonly_role_can_see_registry_and_scheduler_state(connection: Connection) -> None:
    registry_id = _insert_registry(connection)
    _insert_source(connection, registry_id, "ashby", cursor="offset=10")

    connection.execute(sa.text("SET LOCAL ROLE careerops_readonly"))
    rows = (
        connection.execute(
            sa.text(
                "SELECT source_id, cursor, next_run_at, last_result "
                "FROM careerops.crawler_source_registry_sources "
                "WHERE registry_id = :registry_id"
            ),
            {"registry_id": registry_id},
        )
        .mappings()
        .all()
    )

    assert len(rows) == 1
    assert rows[0]["source_id"] == "ashby"
    assert rows[0]["cursor"] == "offset=10"
    assert rows[0]["next_run_at"] == NOW
    assert rows[0]["last_result"] is None


def test_api_role_cannot_mutate_registry_identity_or_delete_sources(engine: Engine) -> None:
    with engine.begin() as connection:
        registry_id = _insert_registry(connection)
        _insert_source(connection, registry_id, "greenhouse")

    with (
        pytest.raises(DBAPIError) as registry_identity_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                "UPDATE careerops.crawler_source_registries "
                "SET manifest_sha256 = :manifest_sha256 WHERE id = :registry_id"
            ),
            {"manifest_sha256": "9" * 64, "registry_id": registry_id},
        )

    with (
        pytest.raises(DBAPIError) as delete_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                "DELETE FROM careerops.crawler_source_registry_sources "
                "WHERE registry_id = :registry_id"
            ),
            {"registry_id": registry_id},
        )

    assert _sqlstate(registry_identity_error.value) == "42501"
    assert _sqlstate(delete_error.value) == "42501"


def test_registry_snapshot_is_append_only_even_for_owner(connection: Connection) -> None:
    registry_id = _insert_registry(connection)

    with pytest.raises(DBAPIError) as mutation_error:
        connection.execute(
            sa.text(
                "UPDATE careerops.crawler_source_registries "
                "SET created_by = 'tampered' WHERE id = :registry_id"
            ),
            {"registry_id": registry_id},
        )

    assert _sqlstate(mutation_error.value) == "55000"


@pytest.mark.parametrize(
    ("adapter", "budget_json", "rate_limit_json"),
    [
        (
            "recruitment.sitemap_discovery",
            PAGE_COLLECTION_BUDGET,
            SITEMAP_RATE_LIMITS,
        ),
        (
            "recruitment.page_collection",
            {**PAGE_COLLECTION_BUDGET, "timeout_seconds": 61.0},
            PAGE_COLLECTION_RATE_LIMITS,
        ),
        (
            "recruitment.page_collection",
            PAGE_COLLECTION_BUDGET,
            {**PAGE_COLLECTION_RATE_LIMITS, "concurrency": 9},
        ),
        ("recruitment.page_collection", {}, PAGE_COLLECTION_RATE_LIMITS),
        ("recruitment.page_collection", PAGE_COLLECTION_BUDGET, {}),
    ],
)
def test_source_schedule_json_rejects_wrong_adapter_out_of_range_or_empty_controls(
    connection: Connection,
    adapter: str,
    budget_json: dict[str, object],
    rate_limit_json: dict[str, object],
) -> None:
    registry_id = _insert_registry(connection)

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as invalid_controls_error:
            _insert_source(
                connection,
                registry_id,
                "hostile-controls",
                adapter=adapter,
                budget_json=budget_json,
                rate_limit_json=rate_limit_json,
            )
        savepoint.rollback()

    assert _sqlstate(invalid_controls_error.value) == "23514"


def test_non_public_ats_source_cannot_claim_canonical_ingestion_policy(
    connection: Connection,
) -> None:
    registry_id = _insert_registry(connection)

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as invalid_policy_error:
            _insert_source(
                connection,
                registry_id,
                "hostile-canonical-policy",
                canonical_ingestion_policy="canonical_job_ingestion",
            )
        savepoint.rollback()

    assert _sqlstate(invalid_policy_error.value) == "23514"


@pytest.mark.parametrize(
    ("source_id", "cursor", "cadence_seconds", "retry_rounds"),
    [
        ("Bad_Source", None, 900, 1),
        ("hostile-cursor", "x" * 161, 900, 1),
        ("hostile-cadence", None, 604_801, 1),
        ("hostile-retries", None, 900, 11),
    ],
)
def test_source_schedule_scalar_bounds_reject_invalid_values(
    connection: Connection,
    source_id: str,
    cursor: str | None,
    cadence_seconds: int,
    retry_rounds: int,
) -> None:
    registry_id = _insert_registry(connection)

    with connection.begin_nested() as savepoint:
        with pytest.raises(DBAPIError) as invalid_schedule_error:
            _insert_source(
                connection,
                registry_id,
                source_id,
                cursor=cursor,
                cadence_seconds=cadence_seconds,
                retry_rounds=retry_rounds,
            )
        savepoint.rollback()

    assert _sqlstate(invalid_schedule_error.value) == "23514"


def _assert_scheduler_runtime_contract(connection: Connection) -> None:
    columns = {
        row[0]
        for row in connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'careerops' "
                "AND table_name = 'crawler_source_registry_sources'"
            )
        )
    }
    missing_columns = sorted(SCHEDULER_RUNTIME_COLUMNS - columns)
    assert not missing_columns, (
        "crawler source scheduler runtime columns are missing from "
        "careerops.crawler_source_registry_sources: "
        f"{missing_columns}. Expected lease/attempt/completion state owned by PostgreSQL."
    )

    missing_functions = [
        function_name
        for function_name in (
            CLAIM_FUNCTION,
            TARGETED_CLAIM_FUNCTION,
            COMPLETE_FUNCTION,
            FAIL_FUNCTION,
        )
        if not connection.scalar(
            sa.text("SELECT to_regprocedure(:function_name) IS NOT NULL"),
            {"function_name": function_name},
        )
    ]
    assert not missing_functions, (
        "crawler source scheduler PostgreSQL API is missing expected functions: "
        f"{missing_functions}. Expected claim to be lease-safe and completion to own "
        "cursor/result/backoff transitions."
    )


def _insert_registry(connection: Connection) -> UUID:
    registry_id = uuid4()
    connection.execute(
        crawler_source_registries.insert().values(
            id=registry_id,
            kind="configured_crawl_source_registry",
            manifest_path="datasets/manifests/crawl-sources.json",
            manifest_sha256=_sha(registry_id, "manifest"),
            registry_sha256=_sha(registry_id, "registry"),
            source_count=1,
            created_by="test-suite",
            generated_at=NOW,
            manifest_json={"version": "test", "sources": []},
        )
    )
    return registry_id


def _insert_source(
    connection: Connection,
    registry_id: UUID,
    source_id: str,
    *,
    cursor: str | None = None,
    cadence_seconds: int = 900,
    retry_rounds: int = 1,
    robots_terms_policy: str = "respect",
    adapter: str = "recruitment.page_collection",
    budget_json: dict[str, object] | None = None,
    rate_limit_json: dict[str, object] | None = None,
    canonical_ingestion_policy: str = "dedupe_only",
) -> UUID:
    row_id = uuid4()
    connection.execute(
        crawler_source_registry_sources.insert().values(
            id=row_id,
            registry_id=registry_id,
            source_id=source_id,
            adapter=adapter,
            enabled=True,
            input_artifact=f"datasets/input/{source_id}.jsonl",
            output_dir=f"datasets/output/{source_id}",
            dependency_source_ids=[],
            dependency_artifacts=[],
            command_sha256=_sha(row_id, "command"),
            source_sha256=_sha(row_id, "source"),
            source_json={"source_id": source_id},
            cursor=cursor,
            cadence_seconds=cadence_seconds,
            retry_rounds=retry_rounds,
            budget_json=dict(PAGE_COLLECTION_BUDGET) if budget_json is None else budget_json,
            rate_limit_json=(
                dict(PAGE_COLLECTION_RATE_LIMITS) if rate_limit_json is None else rate_limit_json
            ),
            robots_terms_policy=robots_terms_policy,
            canonical_ingestion_policy=canonical_ingestion_policy,
            provenance_policy="preserve",
            dedupe_policy="hybrid",
            last_run_at=None,
            next_run_at=NOW,
            last_result=None,
            last_error=None,
        )
    )
    return row_id


def _source_row(connection: Connection, source_row_id: UUID) -> RowMapping:
    return (
        connection.execute(
            sa.select(crawler_source_registry_sources).where(
                crawler_source_registry_sources.c.id == source_row_id
            )
        )
        .mappings()
        .one()
    )


def _count(connection: Connection, table: sa.Table) -> int:
    value = connection.scalar(sa.select(sa.func.count()).select_from(table))
    assert isinstance(value, int)
    return value


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _sha(value: object, label: str) -> str:
    return hashlib.sha256(f"{value}:{label}".encode()).hexdigest()


def _sha_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class _Schedule:
    cursor: str | None = None
    cadence_seconds: int = 900
    retry_rounds: int = 1
    budget: dict[str, int] | None = None
    rate_limits: dict[str, int] | None = None
    robots_terms_policy: str = "respect"
    canonical_ingestion_policy: str = "dedupe_only"
    provenance_policy: str = "preserve"
    dedupe_policy: str = "hybrid"

    def __post_init__(self) -> None:
        if self.budget is None:
            object.__setattr__(
                self,
                "budget",
                {"max_discovered_urls": 10, "timeout_seconds": 30},
            )
        if self.rate_limits is None:
            object.__setattr__(
                self,
                "rate_limits",
                {"concurrency": 1, "max_concurrency_per_host": 1},
            )


@dataclass(frozen=True)
class _Source:
    source_id: str
    adapter: str = "recruitment.page_collection"
    enabled: bool = True
    schedule: _Schedule = _Schedule()

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "source_id": self.source_id,
            "adapter": self.adapter,
            "enabled": self.enabled,
            "schedule": {
                "cadence_seconds": self.schedule.cadence_seconds,
                "retry_rounds": self.schedule.retry_rounds,
            },
        }


@dataclass(frozen=True)
class _Plan:
    source_id: str
    input_artifact: str
    output_dir: str
    adapter: str
    enabled: bool
    source_sha256: str

    def as_dict(self, *, root: object) -> dict[str, object]:
        assert root is not None
        return {
            "adapter": self.adapter,
            "enabled": self.enabled,
            "source_id": self.source_id,
            "input_artifact": self.input_artifact,
            "output_dir": self.output_dir,
            "dependency_source_ids": [],
            "dependency_artifacts": [],
            "command": ["crawl", self.source_id],
            "source_sha256": self.source_sha256,
            "schedule": {
                "cursor": None,
                "cadence_seconds": 900,
                "retry_rounds": 1,
                "budget": {"max_discovered_urls": 10, "timeout_seconds": 30},
                "rate_limits": {"concurrency": 1, "max_concurrency_per_host": 1},
                "robots_terms_policy": "respect",
                "canonical_ingestion_policy": "dedupe_only",
                "provenance_policy": "preserve",
                "dedupe_policy": "hybrid",
            },
        }


def _registry_inputs() -> tuple[SimpleNamespace, Sequence[_Plan], SimpleNamespace]:
    sources = (
        _Source("greenhouse"),
        _Source("workday"),
    )
    plans = tuple(
        _Plan(
            source.source_id,
            f"datasets/input/{source.source_id}.jsonl",
            f"datasets/output/{source.source_id}",
            source.adapter,
            source.enabled,
            _sha_json(source.model_dump(mode="json")),
        )
        for source in sources
    )
    manifest = SimpleNamespace(version="test", sources=sources)
    registry_document = SimpleNamespace(
        kind="configured_crawl_source_registry",
        manifest_path="datasets/manifests/crawl-sources.json",
        manifest_sha256="a" * 64,
        registry_sha256="b" * 64,
        generated_at=NOW.isoformat(),
        manifest={
            "version": "test",
            "sources": [source.model_dump(mode="json") for source in sources],
        },
        resolved_plans=[plan.as_dict(root=SimpleNamespace()) for plan in plans],
    )
    return manifest, plans, registry_document
