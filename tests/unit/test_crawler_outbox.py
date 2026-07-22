from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import RowMapping

from careerops.application import crawler_outbox
from careerops.application.crawler_outbox import (
    CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
    CrawlerDispatchError,
    CrawlerExecutionDispatch,
    CrawlerExecutionOutboxSink,
)
from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxPublisher,
)
from careerops.infrastructure.database.crawler_outbox import _dispatch_from_row


def _claimed_event() -> ClaimedOutboxEvent:
    now = datetime.now(UTC)
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"{CRAWLER_EXECUTION_EVENT_KEY_PREFIX}{uuid4().hex}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type="workflow_signal",
        available_at=now,
        attempt_count=1,
        lease_token=uuid4(),
        lease_until=now + timedelta(seconds=30),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dispatch(root: Path, event: ClaimedOutboxEvent) -> CrawlerExecutionDispatch:
    config_path = root / "datasets/manifests/sources.json"
    review_root = root / "datasets/private/crawler-execution-reviews"
    request_path = review_root / "request.json"
    approval_path = review_root / "approval.json"
    config_path.parent.mkdir(parents=True)
    review_root.mkdir(parents=True)
    config_path.write_text("{}\n", encoding="utf-8")
    request_path.write_text('{"request":"original"}\n', encoding="utf-8")
    approval_path.write_text('{"approval":"original"}\n', encoding="utf-8")
    return CrawlerExecutionDispatch(
        event_id=event.event_id,
        event_key=event.event_key,
        action_intent_id=event.action_intent_id,
        payload_version_id=event.payload_version_id,
        execution_key=event.event_key,
        config_path=config_path,
        request_path=request_path,
        approval_path=approval_path,
        request_sha256=_sha256(request_path),
        approval_sha256=_sha256(approval_path),
    )


@dataclass(frozen=True, slots=True)
class _RequestDocument:
    request_id: str


@dataclass(frozen=True, slots=True)
class _ApprovalDocument:
    request_id: str
    request_sha256: str


class _StaticReader:
    def __init__(self, dispatch: CrawlerExecutionDispatch) -> None:
        self.dispatch = dispatch
        self.events: list[ClaimedOutboxEvent] = []

    def load_dispatch(self, event: ClaimedOutboxEvent) -> CrawlerExecutionDispatch:
        self.events.append(event)
        return self.dispatch


class _RecordingRunner:
    def __init__(self, *, result: int = 0, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[Path, Path, Path, Path]] = []

    def execute(
        self,
        *,
        root: Path,
        config_path: Path,
        request_path: Path,
        approval_path: Path,
    ) -> int:
        self.calls.append((root, config_path, request_path, approval_path))
        if self.error is not None:
            raise self.error
        return self.result


def _patch_bound_documents(
    monkeypatch: pytest.MonkeyPatch,
    dispatch: CrawlerExecutionDispatch,
    *,
    approval_request_id: str | None = None,
    approval_request_sha256: str | None = None,
) -> str:
    request_id = str(uuid4())

    def load_request(path: Path) -> _RequestDocument:
        assert path == dispatch.request_path
        return _RequestDocument(request_id=request_id)

    def load_approval(path: Path) -> _ApprovalDocument:
        assert path == dispatch.approval_path
        return _ApprovalDocument(
            request_id=approval_request_id or request_id,
            request_sha256=approval_request_sha256 or dispatch.request_sha256,
        )

    monkeypatch.setattr(crawler_outbox, "load_execution_request", load_request)
    monkeypatch.setattr(crawler_outbox, "load_execution_approval", load_approval)
    return request_id


def _sink(
    reader: _StaticReader,
    runner: _RecordingRunner,
    *,
    root: Path,
) -> CrawlerExecutionOutboxSink:
    return CrawlerExecutionOutboxSink(reader, runner, root=root)


def test_sink_rejects_dispatch_identity_mismatch_before_running(tmp_path: Path) -> None:
    event = _claimed_event()
    dispatch = replace(_dispatch(tmp_path, event), event_id=uuid4())
    reader = _StaticReader(dispatch)
    runner = _RecordingRunner()

    with pytest.raises(InternalDeliveryError) as error:
        _sink(reader, runner, root=tmp_path).deliver(event)

    assert error.value.error_code == "CRAWLER_DISPATCH_EVENT_MISMATCH"
    assert error.value.retryable is False
    assert reader.events == [event]
    assert runner.calls == []


@pytest.mark.parametrize(
    ("path_name", "error_code"),
    [
        ("request_path", "CRAWLER_REQUEST_ARTIFACT_DRIFT"),
        ("approval_path", "CRAWLER_APPROVAL_ARTIFACT_DRIFT"),
    ],
)
def test_sink_rejects_artifact_hash_drift_before_running(
    tmp_path: Path,
    path_name: str,
    error_code: str,
) -> None:
    event = _claimed_event()
    dispatch = _dispatch(tmp_path, event)
    getattr(dispatch, path_name).write_text('{"changed":true}\n', encoding="utf-8")
    runner = _RecordingRunner()

    with pytest.raises(InternalDeliveryError) as error:
        _sink(_StaticReader(dispatch), runner, root=tmp_path).deliver(event)

    assert error.value.error_code == error_code
    assert error.value.retryable is False
    assert runner.calls == []


@pytest.mark.parametrize(
    ("approval_request_id", "approval_request_sha256", "error_code"),
    [
        (str(uuid4()), None, "CRAWLER_APPROVAL_BINDING_MISMATCH"),
        (None, "f" * 64, "CRAWLER_APPROVAL_REQUEST_HASH_MISMATCH"),
    ],
)
def test_sink_requires_approval_binding_to_the_dispatched_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    approval_request_id: str | None,
    approval_request_sha256: str | None,
    error_code: str,
) -> None:
    event = _claimed_event()
    dispatch = _dispatch(tmp_path, event)
    _patch_bound_documents(
        monkeypatch,
        dispatch,
        approval_request_id=approval_request_id,
        approval_request_sha256=approval_request_sha256,
    )
    runner = _RecordingRunner()

    with pytest.raises(InternalDeliveryError) as error:
        _sink(_StaticReader(dispatch), runner, root=tmp_path).deliver(event)

    assert error.value.error_code == error_code
    assert error.value.retryable is False
    assert runner.calls == []


def test_existing_crawler_claim_requires_terminal_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = _claimed_event()
    dispatch = _dispatch(tmp_path, event)
    request_id = _patch_bound_documents(monkeypatch, dispatch)
    claim_path = (
        tmp_path / "datasets" / "private" / "crawler-execution-claims" / f"{request_id}.json"
    )
    claim_path.parent.mkdir(parents=True)
    claim_path.write_text('{"claim":"already-started"}\n', encoding="utf-8")
    runner = _RecordingRunner()

    with pytest.raises(InternalDeliveryError) as error:
        _sink(_StaticReader(dispatch), runner, root=tmp_path).deliver(event)

    assert error.value.error_code == "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED"
    assert error.value.retryable is False
    assert runner.calls == []


class _TerminalRecordingStore:
    def __init__(self, event: ClaimedOutboxEvent) -> None:
        self.event = event
        self.releases: list[tuple[UUID, str, bool, UUID]] = []

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        assert event_key_prefix == CRAWLER_EXECUTION_EVENT_KEY_PREFIX
        return (self.event,)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del event_id, owner, lease_token, now
        raise AssertionError("a failed crawler execution must not be published")

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        del owner, now, retry_at
        self.releases.append((event_id, error_code, terminal, lease_token))


class _RetryThenSuccessStore:
    """Small outbox-store double that exposes two independently claimed attempts."""

    def __init__(self, events: tuple[ClaimedOutboxEvent, ...]) -> None:
        self._events = list(events)
        self.releases: list[tuple[UUID, str, bool, UUID]] = []
        self.published: list[tuple[UUID, UUID]] = []

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        assert event_key_prefix == CRAWLER_EXECUTION_EVENT_KEY_PREFIX
        if not self._events:
            return ()
        return (self._events.pop(0),)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del owner, now
        self.published.append((event_id, lease_token))

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        del owner, now, retry_at
        self.releases.append((event_id, error_code, terminal, lease_token))


def test_preexecution_artifact_unavailability_defers_then_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_event = _claimed_event()
    retry_event = replace(
        first_event,
        attempt_count=first_event.attempt_count + 1,
        available_at=first_event.available_at + timedelta(minutes=5),
        lease_token=uuid4(),
        lease_until=first_event.lease_until + timedelta(minutes=5),
    )
    dispatch = _dispatch(tmp_path, first_event)
    request_bytes = dispatch.request_path.read_bytes()
    dispatch.request_path.unlink()
    _patch_bound_documents(monkeypatch, dispatch)
    runner = _RecordingRunner()
    store = _RetryThenSuccessStore((first_event, retry_event))
    publisher = OutboxPublisher(
        store,
        _sink(_StaticReader(dispatch), runner, root=tmp_path),
        max_attempts=2,
        event_key_prefix=CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
        clock=lambda: first_event.available_at,
    )

    first_result = publisher.publish_batch(
        owner="crawler-outbox",
        now=first_event.available_at,
    )

    assert first_result.claimed == 1
    assert first_result.published == 0
    assert first_result.deferred == 1
    assert first_result.failed == 0
    assert store.releases == [
        (
            first_event.event_id,
            "CRAWLER_ARTIFACT_UNAVAILABLE",
            False,
            first_event.lease_token,
        )
    ]
    assert runner.calls == []

    dispatch.request_path.write_bytes(request_bytes)
    second_result = publisher.publish_batch(
        owner="crawler-outbox",
        now=retry_event.available_at,
    )

    assert second_result.claimed == 1
    assert second_result.published == 1
    assert second_result.deferred == 0
    assert second_result.failed == 0
    assert store.published == [(retry_event.event_id, retry_event.lease_token)]
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("runner_result", "runner_error", "expected_error_code"),
    [
        (1, None, "CRAWLER_EXECUTION_FAILED"),
        (
            0,
            RuntimeError("crawler process crashed"),
            "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED",
        ),
    ],
)
def test_execution_failure_is_terminal_in_the_filtered_outbox_publisher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner_result: int,
    runner_error: Exception | None,
    expected_error_code: str,
) -> None:
    event = _claimed_event()
    dispatch = _dispatch(tmp_path, event)
    _patch_bound_documents(monkeypatch, dispatch)
    runner = _RecordingRunner(result=runner_result, error=runner_error)
    store = _TerminalRecordingStore(event)
    publisher = OutboxPublisher(
        store,
        _sink(_StaticReader(dispatch), runner, root=tmp_path),
        max_attempts=2,
        event_key_prefix=CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
        clock=lambda: event.available_at,
    )

    result = publisher.publish_batch(owner="crawler-outbox", now=event.available_at)

    assert result.claimed == 1
    assert result.published == 0
    assert result.deferred == 0
    assert result.failed == 1
    assert store.releases == [(event.event_id, expected_error_code, True, event.lease_token)]
    assert len(runner.calls) == 1


def _dispatch_row(event: ClaimedOutboxEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "event_key": event.event_key,
        "action_intent_id": event.action_intent_id,
        "payload_version_id": event.payload_version_id,
        "execution_key": event.event_key,
        "manifest_path": "datasets/manifests/sources.json",
        "request_artifact_path": "datasets/private/crawler-execution-reviews/request.json",
        "request_sha256": "a" * 64,
        "approval_artifact_path": "datasets/private/crawler-execution-reviews/approval.json",
        "approval_artifact_sha256": "b" * 64,
    }


def test_reader_resolves_only_repository_bound_review_artifacts(tmp_path: Path) -> None:
    event = _claimed_event()

    dispatch = _dispatch_from_row(cast("RowMapping", _dispatch_row(event)), root=tmp_path)

    assert dispatch.config_path == (tmp_path / "datasets/manifests/sources.json").resolve()
    assert (
        dispatch.request_path
        == (tmp_path / "datasets/private/crawler-execution-reviews/request.json").resolve()
    )
    assert (
        dispatch.approval_path
        == (tmp_path / "datasets/private/crawler-execution-reviews/approval.json").resolve()
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("manifest_path", "/tmp/sources.json", "repository-relative"),
        (
            "request_artifact_path",
            "datasets/private/not-review-artifacts/request.json",
            "must stay under crawler execution reviews",
        ),
        ("approval_artifact_path", "../approval.json", "escapes the repository root"),
        ("approval_artifact_sha256", None, "missing approval artifact binding"),
    ],
)
def test_reader_rejects_unbound_or_unsafe_dispatch_metadata(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    row = _dispatch_row(_claimed_event())
    row[field] = value

    with pytest.raises(CrawlerDispatchError, match=message):
        _dispatch_from_row(cast("RowMapping", row), root=tmp_path)
