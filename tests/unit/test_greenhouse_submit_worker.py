from __future__ import annotations

import base64
import hashlib
import json
import socket
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import UUID, uuid4

import pytest

from careerops.application.greenhouse_submit import GreenhouseAttachmentRef
from careerops.application.greenhouse_submit_outbox import PreparedGreenhouseSubmitState
from careerops.application.outbox import ClaimedOutboxEvent, InternalDeliveryError
from careerops.config import Settings
from careerops.infrastructure.greenhouse.client import GreenhouseSubmissionOutcome
from careerops.infrastructure.greenhouse.worker import (
    GreenhouseSubmitWorker,
    GreenhouseSubmitWorkerOutcome,
    ObservedGreenhouseSubmitCoordinator,
    OneShotGreenhouseSubmitDispatcher,
    UnixGreenhouseAttachmentResolver,
    _GreenhouseSubmitTransition,
    create_runtime_greenhouse_submit_worker,
)

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 7, 21, 9, 1, tzinfo=UTC)
RESUME_BYTES = b"%PDF-reviewed-resume"
RESUME_HASH = hashlib.sha256(RESUME_BYTES).hexdigest()


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, timedelta, int]] = []

    def dispatch_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> object:
        self.calls.append((owner, now, lease_for, limit))
        raise AssertionError("disabled worker must not dispatch")


def test_defaults_keep_greenhouse_submit_runtime_disabled() -> None:
    settings = Settings()
    dispatcher = _Dispatcher()
    worker = GreenhouseSubmitWorker(
        dispatcher,  # type: ignore[arg-type]
        owner="unit-greenhouse-submit",
        enabled=settings.greenhouse_submit_runtime_enabled,
        disabled_reason="GREENHOUSE_SUBMIT_DISABLED",
        clock=lambda: NOW,
    )

    assert settings.greenhouse_submit_runtime_enabled is False
    assert worker.status().enabled is False
    result = worker.run_once()
    assert result.outcome is GreenhouseSubmitWorkerOutcome.DISABLED
    assert result.claimed == 0
    assert dispatcher.calls == []


def test_runtime_disabled_path_returns_disabled_worker() -> None:
    worker = create_runtime_greenhouse_submit_worker(
        settings=Settings(),
        engine=object(),  # type: ignore[arg-type]
        owner="unit-greenhouse-submit",
        clock=lambda: NOW,
    )

    assert worker.status().enabled is False
    assert worker.run_once().outcome is GreenhouseSubmitWorkerOutcome.DISABLED


def test_dispatcher_marks_accepted_and_ambiguous_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(attempt_count=1)
    store = _Store((event,))
    sink = _Sink()
    monkeypatch.setattr(
        "careerops.infrastructure.greenhouse.worker.last_greenhouse_submit_transition",
        lambda delivered_sink: _GreenhouseSubmitTransition.AMBIGUOUS,
    )

    result = OneShotGreenhouseSubmitDispatcher(
        store,
        sink,  # type: ignore[arg-type]
        clock=lambda: COMPLETED_AT,
    ).dispatch_batch(
        owner="greenhouse-submit-worker",
        now=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )

    assert result.outcome is GreenhouseSubmitWorkerOutcome.AMBIGUOUS
    assert result.ambiguous == 1
    assert result.deferred_prepost == 0
    assert result.failed == 0
    assert sink.delivered == [event]
    assert store.published == [(event.event_id, event.lease_token)]
    assert store.releases == []


def test_dispatcher_retries_only_prepost_failures() -> None:
    event = _event(attempt_count=1)
    store = _Store((event,))
    sink = _Sink(
        error=InternalDeliveryError(
            "GREENHOUSE_BROKER_PREPOST_UNAVAILABLE",
            retryable=True,
        )
    )

    result = OneShotGreenhouseSubmitDispatcher(
        store,
        sink,  # type: ignore[arg-type]
        clock=lambda: COMPLETED_AT,
    ).dispatch_batch(
        owner="greenhouse-submit-worker",
        now=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )

    assert result.outcome is GreenhouseSubmitWorkerOutcome.DEFERRED_PREPOST
    assert result.deferred_prepost == 1
    assert store.published == []
    assert store.releases == [
        (
            event.event_id,
            "GREENHOUSE_BROKER_PREPOST_UNAVAILABLE",
            False,
            event.lease_token,
        )
    ]


def test_dispatcher_terminalizes_post_start_ambiguity_without_retry() -> None:
    event = _event(attempt_count=1)
    store = _Store((event,))
    sink = _Sink(
        error=InternalDeliveryError(
            "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
            retryable=False,
        )
    )

    result = OneShotGreenhouseSubmitDispatcher(
        store,
        sink,  # type: ignore[arg-type]
        clock=lambda: COMPLETED_AT,
    ).dispatch_batch(
        owner="greenhouse-submit-worker",
        now=NOW,
        lease_for=timedelta(seconds=30),
        limit=1,
    )

    assert result.outcome is GreenhouseSubmitWorkerOutcome.AMBIGUOUS
    assert result.ambiguous == 1
    assert result.deferred_prepost == 0
    assert store.published == []
    assert store.releases == [
        (
            event.event_id,
            "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]


def test_mark_published_failure_after_outcome_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(attempt_count=1)
    store = _Store((event,), publish_error=RuntimeError("lease lost"))
    sink = _Sink()
    monkeypatch.setattr(
        "careerops.infrastructure.greenhouse.worker.last_greenhouse_submit_transition",
        lambda delivered_sink: _GreenhouseSubmitTransition.ACCEPTED_UNVERIFIED,
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        OneShotGreenhouseSubmitDispatcher(
            store,
            sink,  # type: ignore[arg-type]
            clock=lambda: COMPLETED_AT,
        ).dispatch_batch(
            owner="greenhouse-submit-worker",
            now=NOW,
            lease_for=timedelta(seconds=30),
            limit=1,
        )

    assert sink.delivered == [event]
    assert store.releases == []


def test_observed_coordinator_preserves_already_terminal_ambiguity() -> None:
    delegate = _AlreadyTerminalCoordinator()
    observed = ObservedGreenhouseSubmitCoordinator(delegate)  # type: ignore[arg-type]

    returned = observed.prepare(_event(attempt_count=1))

    assert returned is delegate.dispatch
    assert observed.last_transition is _GreenhouseSubmitTransition.AMBIGUOUS


def test_unix_attachment_resolver_uses_opaque_ref_and_rechecks_digest(tmp_path: Path) -> None:
    del tmp_path
    with _short_socket_path() as socket_path:
        seen_request: list[dict[str, object]] = []
        thread = _serve_once(
            socket_path,
            seen_request=seen_request,
            response={"data_base64": base64.b64encode(RESUME_BYTES).decode("ascii")},
        )
        resolver = UnixGreenhouseAttachmentResolver(socket_path=socket_path)
        ref = _attachment_ref()

        resolved = resolver.resolve(ref)
        thread.join(timeout=2)

    assert resolved.data == RESUME_BYTES
    assert resolved.ref == ref
    assert seen_request == [
        {
            "version": "careerops.greenhouse.attachment-broker.v1",
            "object_key": "materials/resume/reviewed.pdf",
            "expected_filename": "resume.pdf",
            "expected_content_type": "application/pdf",
            "expected_size_bytes": len(RESUME_BYTES),
            "expected_sha256": RESUME_HASH,
        }
    ]
    assert "path" not in json.dumps(seen_request).lower()
    assert "url" not in json.dumps(seen_request).lower()


def test_unix_attachment_resolver_rejects_mismatched_digest(tmp_path: Path) -> None:
    del tmp_path
    with _short_socket_path() as socket_path:
        thread = _serve_once(
            socket_path,
            seen_request=[],
            response={"data_base64": base64.b64encode(b"tampered").decode("ascii")},
        )
        resolver = UnixGreenhouseAttachmentResolver(socket_path=socket_path)

        with pytest.raises(RuntimeError, match=r"mismatched size|mismatched sha256"):
            resolver.resolve(_attachment_ref())
        thread.join(timeout=2)


class _Sink:
    def __init__(self, error: InternalDeliveryError | None = None) -> None:
        self.error = error
        self.delivered: list[ClaimedOutboxEvent] = []

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        self.delivered.append(event)
        if self.error is not None:
            raise self.error


class _Store:
    def __init__(
        self,
        events: tuple[ClaimedOutboxEvent, ...],
        *,
        publish_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.publish_error = publish_error
        self.published: list[tuple[UUID, UUID]] = []
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
        del owner, now, lease_for
        assert event_key_prefix == "greenhouse-submit:"
        return self.events[:limit]

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del owner, now
        if self.publish_error is not None:
            raise self.publish_error
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


class _AlreadyTerminalCoordinator:
    def __init__(self) -> None:
        self.dispatch = _AlreadyTerminalDispatch()

    def prepare(self, event: ClaimedOutboxEvent) -> object:
        del event
        return self.dispatch


class _AlreadyTerminalDispatch:
    state = PreparedGreenhouseSubmitState.ALREADY_TERMINAL
    terminal_evidence = type(
        "_AlreadyTerminalEvidence",
        (),
        {"outcome": GreenhouseSubmissionOutcome.AMBIGUOUS},
    )()


def _event(*, attempt_count: int) -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"greenhouse-submit:reservation-{uuid4().hex}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type="workflow_signal",
        available_at=NOW,
        attempt_count=attempt_count,
        lease_token=uuid4(),
        lease_until=NOW + timedelta(seconds=30),
    )


def _attachment_ref() -> GreenhouseAttachmentRef:
    return GreenhouseAttachmentRef(
        field_name="resume",
        object_key="materials/resume/reviewed.pdf",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(RESUME_BYTES),
        sha256=RESUME_HASH,
    )


def _serve_once(
    socket_path: Path,
    *,
    seen_request: list[dict[str, object]],
    response: dict[str, object],
) -> threading.Thread:
    ready = threading.Event()

    def run() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                request = connection.recv(65536)
                decoded = json.loads(request.decode("utf-8"))
                assert isinstance(decoded, dict)
                seen_request.append(decoded)
                connection.sendall(json.dumps(response).encode("utf-8"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    return thread


class _ShortSocketPath:
    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="ghatt-", dir="/tmp")
        self.path = Path(self._directory.name) / "a.sock"

    def __enter__(self) -> Path:
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self.path.exists():
            self.path.unlink()
        self._directory.cleanup()


def _short_socket_path() -> _ShortSocketPath:
    return _ShortSocketPath()
