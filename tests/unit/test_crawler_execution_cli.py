from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from careerops.application.crawler_execution import (
    CrawlerExecutionRequestDraft,
    CrawlerExecutionRequestSummary,
)
from careerops.cli import crawler_execution

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for script_name in (
        "discover_recruitment_sitemaps.py",
        "discover_recruitment_commoncrawl.py",
        "discover_public_ats_jobs.py",
        "collect_recruitment_pages.py",
    ):
        (scripts / script_name).write_text("# registered test crawler\n", encoding="utf-8")
    triage = root / "datasets" / "private" / "triage.jsonl"
    triage.parent.mkdir(parents=True)
    triage.write_text('{"registrable_domain":"example.com"}\n', encoding="utf-8")
    manifest = {
        "version": 1,
        "sources": [
            {
                "adapter": "recruitment.public_ats_feed",
                "input_artifact": "datasets/private/triage.jsonl",
                "limits": {"concurrency": 2, "retry_rounds": 1, "timeout_seconds": 5.0},
                "output_dir": "datasets/raw/public-ats",
                "source_id": "public-ats",
            }
        ],
    }
    manifest_path = _write_json(root / "datasets" / "manifests" / "crawler.json", manifest)
    return root, manifest_path


class FakeRequestStore:
    def __init__(self) -> None:
        self.created: CrawlerExecutionRequestDraft | None = None

    def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID:
        self.created = draft
        return draft.request_id

    def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft | None:
        if self.created is not None and self.created.request_id == request_id:
            return self.created
        return None

    def list_pending_requests(
        self,
        *,
        owner_user_id: UUID,
        limit: int = 50,
    ) -> tuple[CrawlerExecutionRequestSummary, ...]:
        del owner_user_id
        del limit
        return ()


def test_reviewed_request_uses_manifest_sources_and_binds_artifact_hash(tmp_path: Path) -> None:
    root, manifest_path = _repo(tmp_path)
    actor_id = uuid4()

    reviewed = crawler_execution.create_reviewed_request(
        root=root,
        config_path=manifest_path,
        source_ids=["public-ats"],
        owner_user_id=actor_id,
        reason="refresh public ATS feeds",
        expires_in=timedelta(hours=2),
        now=NOW,
    )

    assert reviewed.request_artifact_path.is_file()
    assert reviewed.draft.request_id == reviewed.request_id
    assert reviewed.draft.owner_user_id == actor_id
    assert reviewed.draft.manifest_path == "datasets/manifests/crawler.json"
    assert reviewed.draft.request_artifact_path.startswith(
        "datasets/private/crawler-execution-reviews/"
    )
    assert reviewed.draft.request_artifact_path.endswith(f"{reviewed.request_id}.json")
    assert reviewed.draft.request_sha256 == reviewed.request_artifact_sha256
    assert reviewed.draft.hash_bindings["manifest"]
    assert reviewed.draft.hash_bindings["reviewed_plan"]
    assert reviewed.draft.hash_bindings["payload"]
    assert reviewed.draft.source_ids == ("public-ats",)

    submit_store = FakeRequestStore()
    assert crawler_execution.submit_reviewed_request(reviewed, submit_store) == reviewed.request_id
    assert submit_store.created == reviewed.draft


def test_reviewed_request_rejects_manifest_outside_datasets(tmp_path: Path) -> None:
    root, manifest_path = _repo(tmp_path)
    outside_manifest = root / "crawler.json"
    outside_manifest.write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(crawler_execution.CrawlerExecutionCliError, match="under datasets"):
        crawler_execution.create_reviewed_request(
            root=root,
            config_path=outside_manifest,
            source_ids=["public-ats"],
            owner_user_id=uuid4(),
            reason="refresh public ATS feeds",
            expires_in=timedelta(hours=2),
            now=NOW,
        )


def test_reviewed_request_rejects_raw_url_manifest_fields(tmp_path: Path) -> None:
    root, manifest_path = _repo(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["url"] = "https://internal.example/jobs"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Exception, match="invalid manifest fields"):
        crawler_execution.create_reviewed_request(
            root=root,
            config_path=manifest_path,
            source_ids=["public-ats"],
            owner_user_id=uuid4(),
            reason="refresh public ATS feeds",
            expires_in=timedelta(hours=2),
            now=NOW,
        )


def test_main_persists_request_with_api_role_and_no_approval_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest_path = _repo(tmp_path)
    store = FakeRequestStore()

    class FakeTransaction(AbstractContextManager[object]):
        def __enter__(self) -> object:
            return object()

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            del exc_type, exc, traceback
            return False

    class FakeEngine:
        disposed = False

        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        def dispose(self) -> None:
            self.disposed = True

    fake_engine = FakeEngine()
    monkeypatch.setattr(crawler_execution, "create_database_engine", lambda _settings: fake_engine)
    monkeypatch.setattr(
        crawler_execution,
        "PostgresCrawlerExecutionRepository",
        lambda _connection: store,
    )

    result = crawler_execution.main(
        [
            "--root",
            str(root),
            "request",
            "--config",
            str(manifest_path),
            "--source",
            "public-ats",
            "--owner-user-id",
            str(uuid4()),
            "--reason",
            "refresh public ATS feeds",
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pending_console_review"
    assert payload["request"].startswith("datasets/private/crawler-execution-reviews/")
    assert store.created is not None
    assert fake_engine.disposed is True

    with pytest.raises(SystemExit):
        crawler_execution.main(["approve"])
