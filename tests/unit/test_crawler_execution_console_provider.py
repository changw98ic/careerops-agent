from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Engine

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionRequestDraft,
)
from careerops.cli.crawl_sources import CrawlExecutionRequestDocument, _sha256_json
from careerops.infrastructure.database import crawler_execution_console as console_module
from careerops.infrastructure.database.crawler_execution_console import (
    RuntimeCrawlerExecutionConsoleProvider,
)
from careerops.web.crawler_execution import CrawlerExecutionConsoleCapabilityState

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000701")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000801")


class FakeEngine:
    def __init__(self) -> None:
        self.begin_calls = 0

    def begin(self) -> FakeTransaction:
        self.begin_calls += 1
        return FakeTransaction()


class FakeTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *args: object) -> None:
        return None


def test_pending_requests_fails_closed_when_review_directory_is_unavailable(tmp_path: Path) -> None:
    engine = FakeEngine()
    provider = RuntimeCrawlerExecutionConsoleProvider(cast(Engine, engine), workspace_root=tmp_path)

    snapshot = asyncio.run(provider.pending_requests(actor_id=ACTOR_ID, now=NOW))

    assert snapshot.capability.state is CrawlerExecutionConsoleCapabilityState.DISABLED
    assert snapshot.capability.reason_code == "CRAWLER_WORKSPACE_UNAVAILABLE"
    assert snapshot.requests == ()
    assert engine.begin_calls == 0


@pytest.mark.parametrize(
    ("raw_path", "reason_code"),
    [
        ("/tmp/request.json", "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID"),
        (
            "datasets/private/crawler-execution-reviews/../request.json",
            "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID",
        ),
        ("datasets/private/not-reviews/request.json", "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID"),
    ],
)
def test_safe_review_file_rejects_untrusted_record_paths(
    tmp_path: Path,
    raw_path: str,
    reason_code: str,
) -> None:
    _make_review_root(tmp_path)

    with pytest.raises(console_module.CrawlerExecutionConsoleError) as error:
        console_module._safe_existing_review_file(tmp_path, raw_path)

    assert error.value.reason_code == reason_code


def test_safe_review_file_rejects_symlink_components(tmp_path: Path) -> None:
    _make_review_root(tmp_path)
    (tmp_path / "datasets/private/crawler-execution-reviews/target.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (tmp_path / "datasets/private/crawler-execution-reviews/link.json").symlink_to("target.json")

    with pytest.raises(console_module.CrawlerExecutionConsoleError) as error:
        console_module._safe_existing_review_file(
            tmp_path,
            "datasets/private/crawler-execution-reviews/link.json",
        )

    assert error.value.reason_code == "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID"


def test_approve_request_binds_database_decision_to_local_request_and_console_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, request_path = _write_bound_request(tmp_path)
    approved: list[CrawlerExecutionApprovalDraft] = []

    class FakeService:
        def __init__(self, _request_store: object, _approval_store: object) -> None:
            return None

        def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft:
            assert request_id == request.request_id
            return request

        def approve_request(
            self,
            request_id: UUID,
            approval: CrawlerExecutionApprovalDraft,
            *,
            now: datetime,
        ) -> UUID:
            assert request_id == request.request_id
            assert now == NOW
            approved.append(approval)
            return UUID("00000000-0000-0000-0000-000000000901")

    monkeypatch.setattr(
        console_module,
        "PostgresCrawlerExecutionRepository",
        lambda _conn: object(),
    )
    monkeypatch.setattr(console_module, "CrawlerExecutionService", FakeService)
    provider = RuntimeCrawlerExecutionConsoleProvider(
        cast(Engine, FakeEngine()),
        workspace_root=tmp_path,
    )

    result = asyncio.run(
        provider.approve_request(actor_id=ACTOR_ID, request_id=request.request_id, now=NOW)
    )

    approval_path = (
        tmp_path
        / "datasets/private/crawler-execution-reviews"
        / f"{request.request_id}.console-approval.json"
    )
    approval_document = json.loads(approval_path.read_text(encoding="utf-8"))
    assert result.accepted is True
    assert approved == [
        CrawlerExecutionApprovalDraft(
            request_id=request.request_id,
            outcome=CrawlerExecutionApprovalOutcome.APPROVE,
            decided_by_user_id=ACTOR_ID,
            decision_reason="Approved through the authenticated crawler review console.",
            decided_at=NOW,
            approval_artifact_path=str(approval_path.relative_to(tmp_path)),
            approval_artifact_sha256=console_module._sha256_file(approval_path),
            local_cli_approval_present=True,
        )
    ]
    assert approval_document["request_id"] == str(request.request_id)
    assert approval_document["request_sha256"] == request.request_sha256
    assert approval_document["manifest_sha256"] == request.manifest_sha256
    assert approval_document["plan_sha256"] == request.reviewed_plan_sha256
    assert approval_document["source_ids"] == list(request.source_ids)
    assert approval_document["approved_by"] == str(ACTOR_ID)
    assert request_path.read_text(encoding="utf-8")


def test_approve_request_refuses_drift_without_reconstructing_from_database_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request, _request_path = _write_bound_request(tmp_path, reason="reviewed safe sources")
    drifted_request = CrawlerExecutionRequestDraft(
        request_id=request.request_id,
        owner_user_id=request.owner_user_id,
        action_intent_id=request.action_intent_id,
        payload_version_id=request.payload_version_id,
        payload_hash=request.payload_hash,
        manifest_path=request.manifest_path,
        request_artifact_path=request.request_artifact_path,
        manifest_sha256=request.manifest_sha256,
        request_sha256=request.request_sha256,
        reviewed_plan_sha256=request.reviewed_plan_sha256,
        source_ids=request.source_ids,
        reason="raw route form tried to replace reviewed evidence",
        created_at=request.created_at,
        expires_at=request.expires_at,
    )

    class FakeService:
        def __init__(self, _request_store: object, _approval_store: object) -> None:
            return None

        def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft:
            assert request_id == request.request_id
            return drifted_request

        def approve_request(self, *_args: object, **_kwargs: object) -> UUID:
            raise AssertionError("drifted local evidence must not reach database approval")

    monkeypatch.setattr(
        console_module,
        "PostgresCrawlerExecutionRepository",
        lambda _conn: object(),
    )
    monkeypatch.setattr(console_module, "CrawlerExecutionService", FakeService)
    provider = RuntimeCrawlerExecutionConsoleProvider(
        cast(Engine, FakeEngine()),
        workspace_root=tmp_path,
    )

    result = asyncio.run(
        provider.approve_request(actor_id=ACTOR_ID, request_id=request.request_id, now=NOW)
    )

    assert result.accepted is False
    assert result.reason_code == "CRAWLER_REQUEST_ARTIFACT_DRIFT"
    assert not (
        tmp_path
        / "datasets/private/crawler-execution-reviews"
        / f"{request.request_id}.console-approval.json"
    ).exists()


def _write_bound_request(
    root: Path,
    *,
    reason: str = "reviewed crawler request may be queued",
) -> tuple[CrawlerExecutionRequestDraft, Path]:
    review_root = _make_review_root(root)
    document = _request_document(reason=reason)
    request_path = review_root / "request.json"
    request_path.write_text(
        json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    request = CrawlerExecutionRequestDraft(
        request_id=UUID(document.request_id),
        owner_user_id=ACTOR_ID,
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        payload_hash="0" * 64,
        manifest_path="datasets/manifests/recruitment-crawler-sources.example.json",
        request_artifact_path="datasets/private/crawler-execution-reviews/request.json",
        manifest_sha256=document.manifest_sha256,
        request_sha256=document.fingerprint,
        reviewed_plan_sha256=document.plan_sha256,
        source_ids=document.source_ids,
        reason=document.reason,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    return request, request_path


def _request_document(*, reason: str) -> CrawlExecutionRequestDocument:
    reviewed_plan = [
        {"source_id": "greenhouse-sitemap"},
        {"source_id": "workday-pages"},
    ]
    return CrawlExecutionRequestDocument(
        version=1,
        kind="configured_crawl_execution_request",
        request_id=str(REQUEST_ID),
        manifest_sha256="a" * 64,
        plan_sha256=_sha256_json(reviewed_plan),
        reviewed_plan=reviewed_plan,
        source_ids=["greenhouse-sitemap", "workday-pages"],
        requested_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=2)).isoformat(),
        reason=reason,
    )


def _make_review_root(root: Path) -> Path:
    review_root = root / "datasets/private/crawler-execution-reviews"
    review_root.mkdir(parents=True, exist_ok=True)
    return review_root
