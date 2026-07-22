from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine

from careerops.api import source_registry
from careerops.api.source_registry import (
    RuntimeSourceRegistryOperatorProvider,
    SourceRegistryUnavailable,
)
from careerops.config import RuntimeEnvironment, Settings

REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000a01")
SOURCE_ROW_ID = UUID("00000000-0000-0000-0000-000000000a02")
RUN_ID = UUID("00000000-0000-0000-0000-000000000a03")
RUN_EVENT_ID = UUID("00000000-0000-0000-0000-000000000a04")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000a05")


class FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        if len(self._rows) > 1:
            raise AssertionError("expected at most one row")
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, object]]:
        return self._rows


class FakeConnection:
    def __init__(self, source_row: dict[str, object], run_event: dict[str, object]) -> None:
        self.source_row = source_row
        self.run_event = run_event
        self.ingested_records: list[Any] = []
        self.version_keys: set[tuple[str, str, str]] = set()
        self.jsonl_path_to_mutate: Path | None = None

    def execute(self, statement: Any) -> FakeResult:
        table_names = {table.name for table in statement.get_final_froms()}
        if table_names == {"crawler_source_registry_sources"}:
            return FakeResult([self.source_row])
        if table_names == {"crawler_source_runs"}:
            return FakeResult([self.run_event])
        raise AssertionError(f"unexpected query tables: {table_names}")


class FakeTransaction:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    def __enter__(self) -> FakeConnection:
        self._engine.begin_calls += 1
        return self._engine.connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> None:
        if exc_type is None:
            self._engine.commits += 1
        else:
            self._engine.rollbacks += 1


class FakeEngine:
    def __init__(self, source_row: dict[str, object], run_event: dict[str, object]) -> None:
        self.connection = FakeConnection(source_row, run_event)
        self.begin_calls = 0
        self.commits = 0
        self.rollbacks = 0

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class RecordingCanonicalRepository:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def ingest_public_ats_job(self, record: Any) -> SimpleNamespace:
        key = (
            record.row.source_identifier,
            record.row.external_id,
            record.row.effective_content_hash,
        )
        inserted_version = key not in self._connection.version_keys
        self._connection.version_keys.add(key)
        self._connection.ingested_records.append(record)
        return SimpleNamespace(inserted_version=inserted_version)


class MutatingCanonicalRepository(RecordingCanonicalRepository):
    def ingest_public_ats_job(self, record: Any) -> SimpleNamespace:
        result = super().ingest_public_ats_job(record)
        if len(self._connection.ingested_records) == 1:
            path = self._connection.jsonl_path_to_mutate
            assert path is not None
            path.write_bytes(b'{"tampered":true}\n')
        return result


class MismatchedCrawlerSourceRepository:
    def __init__(self, _connection: FakeConnection) -> None:
        pass

    def claim_source(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "registry_id": REGISTRY_ID,
            "run_id": RUN_ID,
            "source_row_id": SOURCE_ROW_ID,
            "source_id": "other-source",
            "lease_token": UUID("00000000-0000-0000-0000-000000000a06"),
            "lease_owner": "worker",
            "lease_expires_at": datetime(2026, 7, 20, 12, 5, tzinfo=UTC),
        }

    def complete_source(self, **_kwargs: object) -> dict[str, object]:
        return {
            "run_id": RUN_ID,
            "source_row_id": SOURCE_ROW_ID,
            "source_id": "other-source",
            "lease_owner": "worker",
            "output_manifest_sha256": "a" * 64,
            "cursor": None,
        }

    def fail_source(self, **_kwargs: object) -> dict[str, object]:
        return {
            "run_id": RUN_ID,
            "source_row_id": SOURCE_ROW_ID,
            "source_id": "other-source",
            "lease_owner": "worker",
        }


def test_runtime_public_ats_ingestion_is_bounded_and_replay_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1), _job_row(2)])
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    first = asyncio.run(
        provider.ingest_public_ats(
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            run_id=RUN_ID,
            source_row_id=SOURCE_ROW_ID,
            actor_id=ACTOR_ID,
            max_records=2,
        )
    )
    replay = asyncio.run(
        provider.ingest_public_ats(
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            run_id=RUN_ID,
            source_row_id=SOURCE_ROW_ID,
            actor_id=ACTOR_ID,
            max_records=2,
        )
    )

    assert first.run_event_id == RUN_EVENT_ID
    assert first.observed_records == 2
    assert first.inserted_versions == 2
    assert first.reused_versions == 0
    assert replay.observed_records == 2
    assert replay.inserted_versions == 0
    assert replay.reused_versions == 2
    assert engine.begin_calls == 2
    assert engine.commits == 2
    assert engine.rollbacks == 0
    assert len(engine.connection.ingested_records) == 4
    assert all(
        record.provenance.run_event_id == RUN_EVENT_ID
        and record.provenance.output_artifact_sha256 == manifest_sha256
        and record.dedupe_policy.value == "hybrid"
        and record.provenance_policy == "preserve"
        for record in engine.connection.ingested_records
    )


def test_runtime_public_ats_ingestion_parses_the_same_bytes_it_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1), _job_row(2)])
    engine = _engine(manifest_sha256)
    engine.connection.jsonl_path_to_mutate = (
        tmp_path / "datasets/raw/ats/discovered_public_ats_jobs.jsonl"
    )
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        MutatingCanonicalRepository,
    )

    response = asyncio.run(
        provider.ingest_public_ats(
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            run_id=RUN_ID,
            source_row_id=SOURCE_ROW_ID,
            actor_id=ACTOR_ID,
            max_records=2,
        )
    )

    assert response.observed_records == 2
    assert [record.row.external_id for record in engine.connection.ingested_records] == [
        "job-1",
        "job-2",
    ]


def test_runtime_public_ats_ingestion_rejects_records_beyond_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1), _job_row(2)])
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="more than max_records=1"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert len(engine.connection.ingested_records) == 1
    assert engine.commits == 0
    assert engine.rollbacks == 1


def test_runtime_public_ats_ingestion_rejects_total_jsonl_bytes_over_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(source_registry, "_MAX_JSONL_BYTES", 32)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match=r"byte count|total size limit"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []
    assert engine.rollbacks == 1


def test_runtime_public_ats_ingestion_rejects_manifest_hash_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_artifacts(tmp_path, [_job_row(1)])
    engine = _engine("f" * 64)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="manifest does not match"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []
    assert engine.commits == 0
    assert engine.rollbacks == 1


def test_runtime_public_ats_ingestion_rejects_manifest_without_jsonl_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(
        tmp_path,
        [_job_row(1)],
        include_discovered_jobs_digest=False,
    )
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="invalid discovered_jobs digest"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []
    assert engine.rollbacks == 1


@pytest.mark.parametrize(
    ("kind", "version", "message"),
    [
        ("legacy_public_ats_manifest", 1, "manifest kind is unsupported"),
        ("public_ats_job_feed_manifest", 2, "manifest version is unsupported"),
        ("public_ats_job_feed_manifest", True, "manifest version is unsupported"),
    ],
)
def test_runtime_public_ats_ingestion_requires_versioned_manifest_contract(
    tmp_path: Path,
    kind: str,
    version: object,
    message: str,
) -> None:
    manifest_sha256 = _write_artifacts(
        tmp_path,
        [_job_row(1)],
        kind=kind,
        version=version,
    )
    provider = _provider(tmp_path, _engine(manifest_sha256))

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )


def test_runtime_public_ats_ingestion_rejects_jsonl_tampered_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    jsonl_path = tmp_path / "datasets/raw/ats/discovered_public_ats_jobs.jsonl"
    jsonl_path.write_text(json.dumps(_job_row(2)) + "\n", encoding="utf-8")
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="does not match its manifest digest"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []
    assert engine.rollbacks == 1


def test_runtime_public_ats_ingestion_rejects_jsonl_byte_count_mismatch(
    tmp_path: Path,
) -> None:
    manifest_sha256 = _write_artifacts(
        tmp_path,
        [_job_row(1)],
        declared_jsonl_bytes=1,
    )
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)

    with pytest.raises(ValueError, match="byte count does not match"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []
    assert engine.rollbacks == 1


def test_runtime_public_ats_ingestion_rejects_symlinked_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    jsonl_path = tmp_path / "datasets/raw/ats/discovered_public_ats_jobs.jsonl"
    external_path = tmp_path / "outside.jsonl"
    external_path.write_text(json.dumps(_job_row(1)) + "\n", encoding="utf-8")
    jsonl_path.unlink()
    jsonl_path.symlink_to(external_path)
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="symbolic links"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []


def test_runtime_public_ats_ingestion_rejects_symlinked_output_directory_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    real_output = tmp_path / "datasets/raw/ats"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "public_ats_job_feed_manifest.json").write_bytes(
        (real_output / "public_ats_job_feed_manifest.json").read_bytes()
    )
    (outside / "discovered_public_ats_jobs.jsonl").write_bytes(
        (real_output / "discovered_public_ats_jobs.jsonl").read_bytes()
    )
    for path in real_output.iterdir():
        path.unlink()
    real_output.rmdir()
    real_output.symlink_to(outside, target_is_directory=True)
    engine = _engine(manifest_sha256)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCanonicalJobIngestionRepository",
        RecordingCanonicalRepository,
    )

    with pytest.raises(ValueError, match="symbolic links"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )

    assert engine.connection.ingested_records == []


@pytest.mark.parametrize("output_dir", [".", "src/careerops", "datasets/../src"])
def test_runtime_public_ats_ingestion_rejects_output_outside_datasets(
    tmp_path: Path,
    output_dir: str,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    engine = _engine(manifest_sha256)
    engine.connection.source_row["output_dir"] = output_dir
    provider = _provider(tmp_path, engine)

    with pytest.raises(ValueError, match=r"safe relative path|datasets root"):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("adapter", "recruitment.page_collection", "adapter does not support"),
        ("canonical_ingestion_policy", "dedupe_only", "does not authorize"),
    ],
)
def test_runtime_public_ats_ingestion_derives_and_enforces_registered_policy(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    manifest_sha256 = _write_artifacts(tmp_path, [_job_row(1)])
    engine = _engine(manifest_sha256)
    engine.connection.source_row[field] = value
    provider = _provider(tmp_path, engine)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            provider.ingest_public_ats(
                registry_id=REGISTRY_ID,
                source_id="public-ats",
                run_id=RUN_ID,
                source_row_id=SOURCE_ROW_ID,
                actor_id=ACTOR_ID,
                max_records=1,
            )
        )


@pytest.mark.parametrize("operation", ["claim", "complete", "fail"])
def test_source_lifecycle_route_mismatch_rolls_back_before_transaction_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    engine = _engine("a" * 64)
    provider = _provider(tmp_path, engine)
    monkeypatch.setattr(
        source_registry,
        "PostgresCrawlerSourceRegistryRepository",
        MismatchedCrawlerSourceRepository,
    )

    with pytest.raises(SourceRegistryUnavailable, match=r"does not match|identifiers"):
        if operation == "claim":
            asyncio.run(
                provider.claim_source(
                    registry_id=REGISTRY_ID,
                    source_id="public-ats",
                    actor_id=ACTOR_ID,
                    worker_id="worker",
                    lease_for=timedelta(minutes=5),
                )
            )
        elif operation == "complete":
            asyncio.run(
                provider.complete_source(
                    registry_id=REGISTRY_ID,
                    source_id="public-ats",
                    actor_id=ACTOR_ID,
                    run_id=RUN_ID,
                    source_row_id=SOURCE_ROW_ID,
                    worker_id="worker",
                    lease_token=UUID("00000000-0000-0000-0000-000000000a06"),
                    output_manifest_sha256="a" * 64,
                    cursor=None,
                    result="ok",
                )
            )
        else:
            asyncio.run(
                provider.fail_source(
                    registry_id=REGISTRY_ID,
                    source_id="public-ats",
                    actor_id=ACTOR_ID,
                    run_id=RUN_ID,
                    source_row_id=SOURCE_ROW_ID,
                    worker_id="worker",
                    lease_token=UUID("00000000-0000-0000-0000-000000000a06"),
                    error="failed",
                )
            )

    assert engine.commits == 0
    assert engine.rollbacks == 1


def _provider(tmp_path: Path, engine: FakeEngine) -> RuntimeSourceRegistryOperatorProvider:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "crawler_workspace_root": tmp_path,
        }
    )
    return RuntimeSourceRegistryOperatorProvider(cast(Engine, engine), settings)


def _engine(manifest_sha256: str) -> FakeEngine:
    return FakeEngine(
        {
            "id": SOURCE_ROW_ID,
            "registry_id": REGISTRY_ID,
            "source_id": "public-ats",
            "adapter": "recruitment.public_ats_feed",
            "canonical_ingestion_policy": "canonical_job_ingestion",
            "output_dir": "datasets/raw/ats",
            "dedupe_policy": "hybrid",
            "provenance_policy": "preserve",
        },
        {
            "event_id": RUN_EVENT_ID,
            "run_id": RUN_ID,
            "source_row_id": SOURCE_ROW_ID,
            "registry_id": REGISTRY_ID,
            "source_id": "public-ats",
            "event_kind": "succeeded",
            "output_manifest_sha256": manifest_sha256,
        },
    )


def _write_artifacts(
    root: Path,
    rows: list[dict[str, object]],
    *,
    include_discovered_jobs_digest: bool = True,
    kind: str = "public_ats_job_feed_manifest",
    version: object = 1,
    declared_jsonl_bytes: int | None = None,
) -> str:
    output_dir = root / "datasets/raw/ats"
    output_dir.mkdir(parents=True)
    jsonl = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
    discovered_jobs: dict[str, object] = {
        "bytes": len(jsonl) if declared_jsonl_bytes is None else declared_jsonl_bytes,
        "path": "ignored/by/runtime/discovered_public_ats_jobs.jsonl",
    }
    if include_discovered_jobs_digest:
        discovered_jobs["artifact_sha256"] = hashlib.sha256(jsonl).hexdigest()
    manifest = (
        json.dumps(
            {
                "artifact_files": {"discovered_jobs": discovered_jobs},
                "kind": kind,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    (output_dir / "public_ats_job_feed_manifest.json").write_bytes(manifest)
    (output_dir / "discovered_public_ats_jobs.jsonl").write_bytes(jsonl)
    return hashlib.sha256(manifest).hexdigest()


def _job_row(index: int) -> dict[str, object]:
    return {
        "canonical_url": f"https://jobs.example.com/jobs/{index}",
        "registrable_domain": "example.com",
        "source_feed_ats": "greenhouse",
        "source_feed_token": "example",
        "source_job_id": f"job-{index}",
        "source_job_title": f"Platform Engineer {index}",
        "source_job_location": "Remote",
        "discovered_at": "2026-07-20T12:00:00Z",
    }
