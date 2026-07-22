from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from careerops.api.source_registry import (
    ClaimSourceResponse,
    CompleteSourceResponse,
    DueSourcesResponse,
    DueSourceSummary,
    FailSourceResponse,
    IngestPublicAtsResponse,
    ListSourceRegistriesResponse,
    RegisterSourceRegistryResponse,
    SourceRegistrySummary,
)
from careerops.cli import source_registry

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000901")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000902")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000903")
RUN_ID = UUID("00000000-0000-0000-0000-000000000904")
SOURCE_ROW_ID = UUID("00000000-0000-0000-0000-000000000905")
RUN_EVENT_ID = UUID("00000000-0000-0000-0000-000000000906")


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def register(
        self,
        *,
        manifest_ref: str,
        actor_id: UUID,
        now: datetime,
    ) -> RegisterSourceRegistryResponse:
        self.calls.append(("register", (manifest_ref, actor_id, now)))
        return RegisterSourceRegistryResponse(
            registry_id=REGISTRY_ID,
            manifest_sha256="a" * 64,
            registry_sha256="b" * 64,
            source_count=1,
        )

    async def list_registries(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListSourceRegistriesResponse:
        self.calls.append(("list", (actor_id, limit)))
        return ListSourceRegistriesResponse(
            registries=(
                SourceRegistrySummary(
                    registry_id=REGISTRY_ID,
                    manifest_path="datasets/manifests/crawler-sources.json",
                    manifest_sha256="a" * 64,
                    registry_sha256="b" * 64,
                    source_count=1,
                    created_by=str(actor_id),
                    generated_at=NOW,
                    created_at=NOW,
                ),
            )
        )

    async def due_sources(
        self,
        *,
        registry_id: UUID,
        actor_id: UUID,
        now: datetime,
        limit: int,
    ) -> DueSourcesResponse:
        self.calls.append(("due", (registry_id, actor_id, now, limit)))
        return DueSourcesResponse(
            registry_id=registry_id,
            due_sources=(
                DueSourceSummary(
                    registry_id=registry_id,
                    source_id="public-ats",
                    adapter="recruitment.public_ats_feed",
                    enabled=True,
                    input_artifact="datasets/private/triage.jsonl",
                    output_dir="datasets/raw/ats",
                    dependency_source_ids=(),
                    dependency_artifacts=(),
                    command_sha256="c" * 64,
                    source_sha256="d" * 64,
                    cadence_seconds=3600,
                    retry_rounds=1,
                    next_run_at=NOW,
                ),
            ),
        )

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse:
        self.calls.append(("claim", (registry_id, source_id, actor_id, worker_id, lease_for)))
        return ClaimSourceResponse(
            registry_id=registry_id,
            run_id=RUN_ID,
            source_row_id=SOURCE_ROW_ID,
            source_id=source_id,
            lease_token=LEASE_TOKEN,
            lease_owner=worker_id,
            lease_expires_at=NOW + lease_for,
        )

    async def complete_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> CompleteSourceResponse:
        self.calls.append(
            (
                "complete",
                (
                    registry_id,
                    source_id,
                    actor_id,
                    run_id,
                    source_row_id,
                    worker_id,
                    lease_token,
                    output_manifest_sha256,
                    cursor,
                    result,
                ),
            )
        )
        return CompleteSourceResponse(
            registry_id=registry_id,
            run_id=run_id,
            source_row_id=source_row_id,
            source_id=source_id,
            lease_owner=worker_id,
            output_manifest_sha256=output_manifest_sha256,
            cursor=cursor,
        )

    async def fail_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> FailSourceResponse:
        self.calls.append(
            (
                "fail",
                (
                    registry_id,
                    source_id,
                    actor_id,
                    run_id,
                    source_row_id,
                    worker_id,
                    lease_token,
                    error,
                ),
            )
        )
        return FailSourceResponse(
            registry_id=registry_id,
            run_id=run_id,
            source_row_id=source_row_id,
            source_id=source_id,
            lease_owner=worker_id,
        )

    async def ingest_public_ats(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        run_id: UUID,
        source_row_id: UUID,
        actor_id: UUID,
        max_records: int,
    ) -> IngestPublicAtsResponse:
        self.calls.append(
            (
                "ingest-public-ats",
                (registry_id, source_id, run_id, source_row_id, actor_id, max_records),
            )
        )
        return IngestPublicAtsResponse(
            registry_id=registry_id,
            source_row_id=source_row_id,
            source_id=source_id,
            run_id=run_id,
            run_event_id=RUN_EVENT_ID,
            observed_records=2,
            inserted_versions=1,
            reused_versions=1,
        )


def test_source_registry_cli_register_lists_due_and_lifecycle_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = RecordingProvider()
    base = ["--actor-id", str(ACTOR_ID)]

    assert (
        source_registry.main(
            [*base, "register", "--manifest-ref", "crawler-sources.json", "--json"],
            provider=provider,
            now=NOW,
        )
        == 0
    )
    register_payload = json.loads(capsys.readouterr().out)

    assert source_registry.main([*base, "list", "--limit", "5", "--json"], provider=provider) == 0
    list_payload = json.loads(capsys.readouterr().out)

    assert (
        source_registry.main(
            [*base, "due", "--registry-id", str(REGISTRY_ID), "--limit", "3", "--json"],
            provider=provider,
            now=NOW,
        )
        == 0
    )
    due_payload = json.loads(capsys.readouterr().out)

    assert (
        source_registry.main(
            [
                *base,
                "claim",
                "--registry-id",
                str(REGISTRY_ID),
                "--source-id",
                "public-ats",
                "--worker-id",
                "operator@example",
                "--lease-seconds",
                "60",
                "--json",
            ],
            provider=provider,
            now=NOW,
        )
        == 0
    )
    claim_payload = json.loads(capsys.readouterr().out)

    assert (
        source_registry.main(
            [
                *base,
                "complete",
                "--registry-id",
                str(REGISTRY_ID),
                "--source-id",
                "public-ats",
                "--run-id",
                str(RUN_ID),
                "--source-row-id",
                str(SOURCE_ROW_ID),
                "--worker-id",
                "operator@example",
                "--lease-token",
                str(LEASE_TOKEN),
                "--output-manifest-sha256",
                "e" * 64,
                "--cursor",
                "page-2",
                "--result",
                "ok",
                "--json",
            ],
            provider=provider,
            now=NOW,
        )
        == 0
    )
    complete_payload = json.loads(capsys.readouterr().out)

    assert (
        source_registry.main(
            [
                *base,
                "fail",
                "--registry-id",
                str(REGISTRY_ID),
                "--source-id",
                "public-ats",
                "--run-id",
                str(RUN_ID),
                "--source-row-id",
                str(SOURCE_ROW_ID),
                "--worker-id",
                "operator@example",
                "--lease-token",
                str(LEASE_TOKEN),
                "--error",
                "temporary upstream refusal",
                "--json",
            ],
            provider=provider,
            now=NOW,
        )
        == 0
    )
    fail_payload = json.loads(capsys.readouterr().out)

    assert (
        source_registry.main(
            [
                *base,
                "ingest-public-ats",
                "--registry-id",
                str(REGISTRY_ID),
                "--source-id",
                "public-ats",
                "--run-id",
                str(RUN_ID),
                "--source-row-id",
                str(SOURCE_ROW_ID),
                "--max-records",
                "2",
                "--json",
            ],
            provider=provider,
        )
        == 0
    )
    ingest_payload = json.loads(capsys.readouterr().out)

    assert register_payload["status"] == "registered"
    assert list_payload["registries"][0]["registry_id"] == str(REGISTRY_ID)
    assert due_payload["due_sources"][0]["source_id"] == "public-ats"
    assert claim_payload["lease_token"] == str(LEASE_TOKEN)
    assert claim_payload["run_id"] == str(RUN_ID)
    assert claim_payload["source_row_id"] == str(SOURCE_ROW_ID)
    assert complete_payload["status"] == "completed"
    assert complete_payload["run_id"] == str(RUN_ID)
    assert complete_payload["source_row_id"] == str(SOURCE_ROW_ID)
    assert complete_payload["lease_owner"] == "operator@example"
    assert complete_payload["output_manifest_sha256"] == "e" * 64
    assert complete_payload["cursor"] == "page-2"
    assert fail_payload["status"] == "failed"
    assert fail_payload["run_id"] == str(RUN_ID)
    assert fail_payload["source_row_id"] == str(SOURCE_ROW_ID)
    assert fail_payload["lease_owner"] == "operator@example"
    assert ingest_payload == {
        "status": "ingested",
        "registry_id": str(REGISTRY_ID),
        "source_row_id": str(SOURCE_ROW_ID),
        "source_id": "public-ats",
        "run_id": str(RUN_ID),
        "run_event_id": str(RUN_EVENT_ID),
        "observed_records": 2,
        "inserted_versions": 1,
        "reused_versions": 1,
    }
    assert [call[0] for call in provider.calls] == [
        "register",
        "list",
        "due",
        "claim",
        "complete",
        "fail",
        "ingest-public-ats",
    ]


def test_source_registry_cli_rejects_unbounded_limit_before_provider_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = RecordingProvider()

    returncode = source_registry.main(
        ["--actor-id", str(ACTOR_ID), "due", "--registry-id", str(REGISTRY_ID), "--limit", "0"],
        provider=provider,
    )

    assert returncode == 1
    assert "--limit must be between 1 and 500" in capsys.readouterr().err
    assert provider.calls == []


def test_source_registry_cli_rejects_unbounded_ingestion_before_provider_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = RecordingProvider()

    returncode = source_registry.main(
        [
            "--actor-id",
            str(ACTOR_ID),
            "ingest-public-ats",
            "--registry-id",
            str(REGISTRY_ID),
            "--source-id",
            "public-ats",
            "--run-id",
            str(RUN_ID),
            "--source-row-id",
            str(SOURCE_ROW_ID),
            "--max-records",
            "10001",
        ],
        provider=provider,
    )

    assert returncode == 1
    assert "--max-records must be between 1 and 10000" in capsys.readouterr().err
    assert provider.calls == []
