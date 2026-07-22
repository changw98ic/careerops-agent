"""PostgreSQL integration coverage for canonical crawler job ingestion."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.canonical_job_ingestion import (
    CanonicalJobDedupePolicy,
    CanonicalJobIngestionRecord,
    CrawlerRunProvenance,
    PublicAtsJobRow,
    sha256_json,
)
from careerops.infrastructure.database.canonical_job_ingestion import (
    CanonicalJobIngestionRepositoryError,
    PostgresCanonicalJobIngestionRepository,
)
from careerops.infrastructure.database.schema import (
    canonical_jobs,
    companies,
    crawler_job_deduplication_keys,
    crawler_job_ingestion_evidence,
    crawler_source_registries,
    crawler_source_registry_sources,
    crawler_source_runs,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
)

pytestmark = pytest.mark.integration

_DATABASE_PREFIX = "careerops_test_"
_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
_OUTPUT_MANIFEST_HASH = "a" * 64
_OTHER_OUTPUT_MANIFEST_HASH = "b" * 64


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DATABASE_PREFIX):
        pytest.skip(f"canonical ingestion tests require {_DATABASE_PREFIX}* database")
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


def test_ingests_public_ats_job_only_when_bound_to_succeeded_crawler_run(
    connection: Connection,
) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)
    record = _record(source_row_id, registry_id, source_id, run_id, run_event_id)

    connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
    result = PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(record)
    connection.execute(sa.text("RESET ROLE"))

    assert result.inserted_version is True
    evidence = connection.execute(sa.select(crawler_job_ingestion_evidence)).mappings().one()
    assert evidence["source_row_id"] == source_row_id
    assert evidence["run_id"] == run_id
    assert evidence["run_event_id"] == run_event_id
    assert evidence["provenance_json"]["output_artifact_sha256"] == _OUTPUT_MANIFEST_HASH
    assert evidence["dedupe_key_sha256"] == result.dedupe_key


def test_replaying_same_public_ats_record_is_idempotent(connection: Connection) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)
    repository = PostgresCanonicalJobIngestionRepository(connection)
    record = _record(source_row_id, registry_id, source_id, run_id, run_event_id)

    first = repository.ingest_public_ats_job(record)
    replay = repository.ingest_public_ats_job(record)

    assert replay.company_id == first.company_id
    assert replay.source_id == first.source_id
    assert replay.posting_id == first.posting_id
    assert replay.version_id == first.version_id
    assert replay.canonical_job_id == first.canonical_job_id
    assert replay.evidence_id == first.evidence_id
    assert replay.inserted_version is False
    assert _count(connection, job_posting_versions) == 1
    assert _count(connection, crawler_job_ingestion_evidence) == 1


def test_public_crawl_company_identity_cannot_collide_with_manual_display_name(
    connection: Connection,
) -> None:
    manual_company_id = uuid4()
    connection.execute(
        sa.insert(companies).values(
            id=manual_company_id,
            name="Example AI",
            normalized_name="example ai",
            official_domains=[],
            terms_status="unknown",
        )
    )
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)

    result = PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(
        _record(
            source_row_id,
            registry_id,
            source_id,
            run_id,
            run_event_id,
            company_domain="example.ai",
        )
    )

    assert result.company_id != manual_company_id
    crawler_company = (
        connection.execute(sa.select(companies).where(companies.c.id == result.company_id))
        .mappings()
        .one()
    )
    assert crawler_company["normalized_name"] == "crawler-domain:example.ai"
    assert _count(connection, companies) == 2


def test_changed_public_ats_content_adds_version_without_changing_hybrid_canonical_identity(
    connection: Connection,
) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)
    repository = PostgresCanonicalJobIngestionRepository(connection)
    original = _record(source_row_id, registry_id, source_id, run_id, run_event_id)
    changed = _record(
        source_row_id,
        registry_id,
        source_id,
        run_id,
        run_event_id,
        title="Staff Agent Platform Engineer",
        content_hash=sha256_json({"title": "Staff Agent Platform Engineer", "revision": 2}),
    )

    first = repository.ingest_public_ats_job(original)
    second = repository.ingest_public_ats_job(changed)

    assert second.posting_id == first.posting_id
    assert second.version_id != first.version_id
    assert second.canonical_job_id == first.canonical_job_id
    assert second.inserted_version is True
    assert _count(connection, job_posting_versions) == 2
    assert _count(connection, canonical_jobs) == 1


def test_url_policy_merges_cross_source_records_only_on_exact_canonical_url(
    connection: Connection,
) -> None:
    (
        first_source_row_id,
        first_registry_id,
        first_source_id,
        first_run_id,
        first_event_id,
    ) = _succeeded_crawler_run(
        connection,
        source_id="public-ats-a",
        dedupe_policy="url",
    )
    (
        second_source_row_id,
        second_registry_id,
        second_source_id,
        second_run_id,
        second_event_id,
    ) = _succeeded_crawler_run(
        connection,
        source_id="public-ats-b",
        dedupe_policy="url",
    )
    repository = PostgresCanonicalJobIngestionRepository(connection)

    first = repository.ingest_public_ats_job(
        _record(
            first_source_row_id,
            first_registry_id,
            first_source_id,
            first_run_id,
            first_event_id,
            source_identifier="greenhouse-board",
            dedupe_policy=CanonicalJobDedupePolicy.URL,
        )
    )
    same_url_other_source = repository.ingest_public_ats_job(
        _record(
            second_source_row_id,
            second_registry_id,
            second_source_id,
            second_run_id,
            second_event_id,
            external_id="other-source-123",
            source_identifier="lever-board",
            dedupe_policy=CanonicalJobDedupePolicy.URL,
        )
    )
    different_url_other_source = repository.ingest_public_ats_job(
        _record(
            second_source_row_id,
            second_registry_id,
            second_source_id,
            second_run_id,
            second_event_id,
            external_id="other-source-456",
            source_identifier="lever-board",
            canonical_url="https://jobs.example.com/jobs/456",
            dedupe_policy=CanonicalJobDedupePolicy.URL,
        )
    )

    assert same_url_other_source.canonical_job_id == first.canonical_job_id
    assert different_url_other_source.canonical_job_id != first.canonical_job_id
    assert _count(connection, canonical_jobs) == 2
    assert _count(connection, job_posting_assignments) == 3


@pytest.mark.parametrize(
    "invalid_case",
    ("nonexistent_event", "claimed_event", "failed_event", "mismatched_manifest_hash"),
)
def test_rejects_ingestion_without_matching_succeeded_run_event(
    connection: Connection,
    invalid_case: str,
) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _invalid_run_binding(
        connection,
        invalid_case,
    )
    output_manifest_hash = (
        _OTHER_OUTPUT_MANIFEST_HASH
        if invalid_case == "mismatched_manifest_hash"
        else _OUTPUT_MANIFEST_HASH
    )
    record = _record(
        source_row_id,
        registry_id,
        source_id,
        run_id,
        run_event_id,
        output_manifest_hash=output_manifest_hash,
    )
    before = _table_counts(connection)

    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError):
            PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(record)
    finally:
        savepoint.rollback()

    assert _table_counts(connection) == before


@pytest.mark.parametrize(
    ("canonical_ingestion_policy", "source_dedupe_policy", "source_provenance_policy"),
    [
        ("dedupe_only", "hybrid", "preserve"),
        ("canonical_job_ingestion", "url", "preserve"),
        ("canonical_job_ingestion", "hybrid", "compact"),
    ],
)
def test_rejects_ingestion_when_registered_source_policy_does_not_match(
    connection: Connection,
    canonical_ingestion_policy: str,
    source_dedupe_policy: str,
    source_provenance_policy: str,
) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(
        connection,
        canonical_ingestion_policy=canonical_ingestion_policy,
        dedupe_policy=source_dedupe_policy,
        provenance_policy=source_provenance_policy,
    )
    record = _record(source_row_id, registry_id, source_id, run_id, run_event_id)
    before = _table_counts(connection)

    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError):
            PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(record)
    finally:
        savepoint.rollback()

    assert _table_counts(connection) == before


def test_dedupe_conflict_rolls_back_without_orphan_canonical_job(connection: Connection) -> None:
    source_row_id, registry_id, source_id, run_id, run_event_id = _succeeded_crawler_run(connection)
    record = _record(
        source_row_id,
        registry_id,
        source_id,
        run_id,
        run_event_id,
        dedupe_policy=CanonicalJobDedupePolicy.URL,
    )
    _bind_conflicting_dedupe_key(connection, record.dedupe_policy.value, record.dedupe_key)
    before = _table_counts(connection)

    savepoint = connection.begin_nested()
    try:
        with pytest.raises(CanonicalJobIngestionRepositoryError):
            PostgresCanonicalJobIngestionRepository(connection).ingest_public_ats_job(record)
    finally:
        savepoint.rollback()

    assert _table_counts(connection) == before


def _record(
    source_row_id: UUID,
    registry_id: UUID,
    source_id: str,
    run_id: UUID,
    run_event_id: UUID,
    *,
    title: str = "Agent Platform Engineer",
    company_domain: str = "example.com",
    external_id: str = "job-123",
    source_identifier: str = "public-ats-feed",
    canonical_url: str = "https://jobs.example.com/jobs/123",
    content_hash: str | None = None,
    dedupe_policy: CanonicalJobDedupePolicy = CanonicalJobDedupePolicy.HYBRID,
    output_manifest_hash: str = _OUTPUT_MANIFEST_HASH,
) -> CanonicalJobIngestionRecord:
    return _canonical_record(
        source_row_id,
        registry_id,
        source_id,
        run_id,
        run_event_id,
        row=PublicAtsJobRow(
            company_name=company_domain,
            company_domain=company_domain,
            source_type="public_ats",
            source_identifier=source_identifier,
            base_url="https://jobs.example.com/",
            external_id=external_id,
            canonical_url=canonical_url,
            title=title,
            location="Remote",
            department="Engineering",
            captured_at=_NOW,
            parser_version="public-ats-jsonl:v1",
            source_url=canonical_url,
            structured_data=MappingProxyType({"source_record_id": external_id}),
            content_hash=content_hash,
        ),
        dedupe_policy=dedupe_policy,
        output_manifest_hash=output_manifest_hash,
    )


def _canonical_record(
    source_row_id: UUID,
    registry_id: UUID,
    source_id: str,
    run_id: UUID,
    run_event_id: UUID,
    *,
    row: PublicAtsJobRow,
    dedupe_policy: CanonicalJobDedupePolicy,
    output_manifest_hash: str,
) -> CanonicalJobIngestionRecord:
    return CanonicalJobIngestionRecord(
        provenance=CrawlerRunProvenance(
            crawler_source_row_id=source_row_id,
            crawler_run_id=run_id,
            run_event_id=run_event_id,
            registry_id=registry_id,
            source_id=source_id,
            adapter="recruitment.public_ats_feed",
            output_artifact_sha256=output_manifest_hash,
        ),
        row=row,
        dedupe_policy=dedupe_policy,
    )


def _succeeded_crawler_run(
    connection: Connection,
    *,
    source_id: str = "public-ats",
    dedupe_policy: str = "hybrid",
    canonical_ingestion_policy: str = "canonical_job_ingestion",
    provenance_policy: str = "preserve",
) -> tuple[UUID, UUID, str, UUID, UUID]:
    registry_id = _insert_registry(connection)
    source_row_id = _insert_source(
        connection,
        registry_id,
        source_id,
        dedupe_policy=dedupe_policy,
        canonical_ingestion_policy=canonical_ingestion_policy,
        provenance_policy=provenance_policy,
    )
    lease_token = uuid4()
    claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_crawler_source("
                ":registry_id, :source_id, :worker_id, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "source_id": source_id,
                "worker_id": "canonical-ingestion-test",
                "lease_token": lease_token,
                "lease_seconds": 300,
            },
        )
        .mappings()
        .one()
    )
    connection.execute(
        sa.text(
            "SELECT * FROM careerops.complete_crawler_source_run("
            ":source_row_id, :worker_id, :lease_token, :cursor, :result, :manifest_hash)"
        ),
        {
            "source_row_id": source_row_id,
            "worker_id": "canonical-ingestion-test",
            "lease_token": lease_token,
            "cursor": "cursor-after",
            "result": "succeeded",
            "manifest_hash": _OUTPUT_MANIFEST_HASH,
        },
    ).mappings().one()
    event_id = connection.scalar(
        sa.select(crawler_source_runs.c.event_id).where(
            crawler_source_runs.c.run_id == claim["run_id"],
            crawler_source_runs.c.event_kind == "succeeded",
        )
    )
    assert isinstance(event_id, UUID)
    return source_row_id, registry_id, source_id, claim["run_id"], event_id


def _invalid_run_binding(
    connection: Connection,
    invalid_case: str,
) -> tuple[UUID, UUID, str, UUID, UUID]:
    if invalid_case == "nonexistent_event":
        registry_id = _insert_registry(connection)
        source_row_id = _insert_source(connection, registry_id, "public-ats")
        return source_row_id, registry_id, "public-ats", uuid4(), uuid4()
    if invalid_case == "mismatched_manifest_hash":
        return _succeeded_crawler_run(connection)

    registry_id = _insert_registry(connection)
    source_row_id = _insert_source(connection, registry_id, f"public-ats-{invalid_case[:6]}")
    lease_token = uuid4()
    claim = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.claim_crawler_source("
                ":registry_id, :source_id, :worker_id, :lease_token, :lease_seconds)"
            ),
            {
                "registry_id": registry_id,
                "source_id": f"public-ats-{invalid_case[:6]}",
                "worker_id": "canonical-ingestion-test",
                "lease_token": lease_token,
                "lease_seconds": 300,
            },
        )
        .mappings()
        .one()
    )
    if invalid_case == "claimed_event":
        event_id = connection.scalar(
            sa.select(crawler_source_runs.c.event_id).where(
                crawler_source_runs.c.run_id == claim["run_id"],
                crawler_source_runs.c.event_kind == "claimed",
            )
        )
        assert isinstance(event_id, UUID)
        return (
            source_row_id,
            registry_id,
            f"public-ats-{invalid_case[:6]}",
            claim["run_id"],
            event_id,
        )
    if invalid_case == "failed_event":
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.fail_crawler_source_run("
                ":source_row_id, :worker_id, :lease_token, :error)"
            ),
            {
                "source_row_id": source_row_id,
                "worker_id": "canonical-ingestion-test",
                "lease_token": lease_token,
                "error": "synthetic failure",
            },
        ).mappings().one()
        event_id = connection.scalar(
            sa.select(crawler_source_runs.c.event_id).where(
                crawler_source_runs.c.run_id == claim["run_id"],
                crawler_source_runs.c.event_kind == "failed",
            )
        )
        assert isinstance(event_id, UUID)
        return (
            source_row_id,
            registry_id,
            f"public-ats-{invalid_case[:6]}",
            claim["run_id"],
            event_id,
        )
    raise AssertionError(f"unknown invalid case: {invalid_case}")


def _insert_registry(connection: Connection) -> UUID:
    registry_id = uuid4()
    manifest_hash = sha256_json({"registry_id": str(registry_id)})
    connection.execute(
        sa.insert(crawler_source_registries).values(
            id=registry_id,
            kind="configured-recruitment-crawlers",
            manifest_path="config/job-sources/public-ats.json",
            manifest_sha256=manifest_hash,
            registry_sha256=sha256_json({"sources": [str(registry_id)]}),
            source_count=1,
            created_by="canonical-ingestion-test",
            generated_at=_NOW,
            manifest_json={"sources": []},
        )
    )
    return registry_id


def _insert_source(
    connection: Connection,
    registry_id: UUID,
    source_id: str,
    *,
    dedupe_policy: str = "hybrid",
    canonical_ingestion_policy: str = "canonical_job_ingestion",
    provenance_policy: str = "preserve",
) -> UUID:
    source_row_id = uuid4()
    connection.execute(
        sa.insert(crawler_source_registry_sources).values(
            id=source_row_id,
            registry_id=registry_id,
            source_id=source_id,
            adapter="recruitment.public_ats_feed",
            enabled=True,
            input_artifact="datasets/recruitment/sources/public_ats_seed.json",
            output_dir="datasets/recruitment/discovered",
            dependency_source_ids=[],
            dependency_artifacts=[],
            command_sha256=sha256_json({"command": source_id}),
            source_sha256=sha256_json({"source": source_id}),
            source_json={"source_id": source_id},
            cursor=None,
            cadence_seconds=3600,
            retry_rounds=2,
            budget_json={
                "timeout_seconds": 30,
                "max_feed_bytes": 10_000_000,
                "retry_rounds": 2,
            },
            rate_limit_json={"concurrency": 1},
            robots_terms_policy="respect",
            canonical_ingestion_policy=canonical_ingestion_policy,
            provenance_policy=provenance_policy,
            dedupe_policy=dedupe_policy,
            next_run_at=_NOW - timedelta(days=1),
        )
    )
    return source_row_id


def _bind_conflicting_dedupe_key(
    connection: Connection,
    dedupe_policy: str,
    dedupe_key: str,
) -> None:
    company_id = uuid4()
    canonical_job_id = uuid4()
    connection.execute(
        sa.insert(companies).values(
            id=company_id,
            name="conflict.example",
            normalized_name="conflict example",
            official_domains=["conflict.example"],
            terms_status="unknown",
        )
    )
    connection.execute(
        sa.insert(canonical_jobs).values(
            id=canonical_job_id,
            company_id=company_id,
            canonical_title="Conflicting Job",
            normalized_title="conflicting job",
            aggregate_state="active",
        )
    )
    connection.execute(
        sa.insert(crawler_job_deduplication_keys).values(
            id=uuid4(),
            dedupe_policy=dedupe_policy,
            dedupe_key_sha256=dedupe_key,
            company_id=company_id,
            canonical_job_id=canonical_job_id,
            rule=f"deterministic:{dedupe_policy}:v1",
            algorithm_version="canonical-job-ingestion:v1",
        )
    )


def _count(connection: Connection, table: sa.Table) -> int:
    return int(connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0)


def _table_counts(connection: Connection) -> dict[str, int]:
    return {
        "canonical_jobs": _count(connection, canonical_jobs),
        "dedupe_keys": _count(connection, crawler_job_deduplication_keys),
        "evidence": _count(connection, crawler_job_ingestion_evidence),
        "postings": _count(connection, job_postings),
        "versions": _count(connection, job_posting_versions),
    }
