from __future__ import annotations

import base64
import hashlib
import json
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from careerops.application.gmail_send import (
    GmailSendAttachmentRef,
    GmailSendPayload,
    GmailSendProviderState,
)
from careerops.application.gmail_send_outbox import (
    GmailSendAccessToken,
    GmailSendProviderReceipt,
    GmailSendReconciliationJob,
    GmailSentMetadata,
    PreparedGmailSendDispatch,
)
from careerops.application.outbox import PublishBatchResult
from careerops.config import Settings
from careerops.infrastructure.gmail.send_client import GmailSendReceipt
from careerops.infrastructure.gmail.send_worker import (
    GmailSendWorker,
    GmailSendWorkerOutcome,
    HttpGmailSendProvider,
    HttpGmailSentMetadataProvider,
    UnixAttachmentResolver,
    create_runtime_gmail_send_worker,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


class _Publisher:
    def __init__(self, result: PublishBatchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, datetime, timedelta, int]] = []

    def publish_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta = timedelta(seconds=30),
        limit: int = 25,
    ) -> PublishBatchResult:
        self.calls.append((owner, now, lease_for, limit))
        return self.result


class _Reconciler:
    def __init__(self, result: tuple[int, int, int]) -> None:
        self.result = result
        self.calls: list[tuple[str, datetime, int]] = []

    def reconcile_batch(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int = 25,
    ) -> tuple[int, int, int]:
        self.calls.append((owner, now, limit))
        return self.result


def test_disabled_worker_status_and_runs_do_not_claim_or_reconcile() -> None:
    publisher = _Publisher(PublishBatchResult(claimed=1, published=1, deferred=0, failed=0))
    reconciler = _Reconciler((1, 1, 0))
    worker = GmailSendWorker(
        publisher,  # type: ignore[arg-type]
        reconciler,  # type: ignore[arg-type]
        owner="unit-gmail-send",
        enabled=False,
        disabled_reason="GMAIL_SEND_DISABLED",
        clock=lambda: NOW,
    )

    assert worker.status().enabled is False
    assert worker.run_once().outcome is GmailSendWorkerOutcome.DISABLED
    assert worker.reconcile_once().outcome is GmailSendWorkerOutcome.DISABLED
    assert publisher.calls == []
    assert reconciler.calls == []


def test_worker_maps_publish_and_reconciliation_outcomes() -> None:
    publisher = _Publisher(PublishBatchResult(claimed=2, published=1, deferred=0, failed=1))
    reconciler = _Reconciler((2, 1, 1))
    worker = GmailSendWorker(
        publisher,  # type: ignore[arg-type]
        reconciler,  # type: ignore[arg-type]
        owner="unit-gmail-send",
        enabled=True,
        clock=lambda: NOW,
    )

    send_result = worker.run_once(limit=2)
    reconcile_result = worker.reconcile_once(limit=2)

    assert send_result.outcome is GmailSendWorkerOutcome.FAILED
    assert send_result.claimed == 2
    assert publisher.calls[0][0] == "unit-gmail-send"
    assert reconcile_result.outcome is GmailSendWorkerOutcome.DEFERRED
    assert reconcile_result.confirmed == 1
    assert reconcile_result.ambiguous == 1


@dataclass(frozen=True, slots=True)
class _Token:
    account_subject: str = "alice@example.com"

    @property
    def value(self) -> str:
        return "ya29.test-token"

    def is_expired(self, *, now: datetime | None = None) -> bool:
        del now
        return False


def _dispatch() -> PreparedGmailSendDispatch:
    payload = GmailSendPayload(
        sender="alice@example.com",
        recipient="hr@example.com",
        subject="Application",
        text_body="Hello",
    )
    return PreparedGmailSendDispatch(
        event_id=__import__("uuid").uuid4(),
        event_key="gmail-send:" + "a" * 64,
        action_intent_id=__import__("uuid").uuid4(),
        payload_version_id=__import__("uuid").uuid4(),
        reservation_key="a" * 64,
        reconciliation_key="b" * 64,
        send_account_id=__import__("uuid").uuid4(),
        send_credential_handle="send-handle",
        readonly_credential_handle="readonly-handle",
        account_subject="alice@example.com",
        payload=payload,
    )


class _SendClient:
    def __init__(self) -> None:
        self.calls: list[GmailSendPayload] = []

    def send_message(
        self,
        token: _Token,
        *,
        payload: GmailSendPayload,
        attachments: tuple[object, ...] = (),
    ) -> GmailSendReceipt:
        del token, attachments
        self.calls.append(payload)
        return GmailSendReceipt(provider_message_id="msg-1", provider_thread_id="thread-1")


def test_http_send_provider_binds_receipt_to_reviewed_message_id() -> None:
    dispatch = _dispatch()
    client = _SendClient()

    receipt = HttpGmailSendProvider(client).send_message(
        token=cast("GmailSendAccessToken", _Token()),
        dispatch=dispatch,
        attachments=(),
        now=NOW,
    )

    assert client.calls == [dispatch.payload]
    assert receipt.provider_state is GmailSendProviderState.SENT_CONFIRMED
    assert receipt.rfc_message_id == dispatch.payload.message_id_header
    assert receipt.received_at == NOW


class _ReadOnlyClient:
    def get_message(
        self,
        token: _Token,
        *,
        message_id: str,
        metadata_headers: tuple[str, ...],
    ) -> object:
        del token, metadata_headers
        assert message_id == "msg-1"
        return _Message()

    def find_sent_message_by_rfc_message_id(
        self,
        token: _Token,
        *,
        rfc_message_id: str,
        metadata_headers: tuple[str, ...],
    ) -> object:
        del token, metadata_headers
        assert rfc_message_id.startswith("<careerops.")
        return _Message()


@dataclass(frozen=True, slots=True)
class _Message:
    id: str = "msg-1"
    thread_id: str = "thread-1"
    label_ids: tuple[str, ...] = ("SENT",)
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(
                self,
                "headers",
                {
                    "from": "alice@example.com",
                    "to": "hr@example.com",
                    "subject": "Application",
                    "message-id": "<careerops.x@gmail-send.careerops.local>",
                },
            )


def test_sent_metadata_provider_uses_fixed_readonly_methods() -> None:
    dispatch = _dispatch()
    receipt = GmailSendProviderReceipt(
        provider_message_id="msg-1",
        provider_thread_id="thread-1",
        rfc_message_id=dispatch.payload.message_id_header,
        provider_state=GmailSendProviderState.SENT_CONFIRMED,
        received_at=NOW,
    )
    provider = HttpGmailSentMetadataProvider(_ReadOnlyClient())  # type: ignore[arg-type]

    direct = provider.get_sent_metadata(
        token=cast("GmailSendAccessToken", _Token()),
        dispatch=dispatch,
        receipt=receipt,
    )
    found = provider.find_sent_by_rfc_message_id(
        token=cast("GmailSendAccessToken", _Token()),
        job=GmailSendReconciliationJob(
            event_id=dispatch.event_id,
            event_key=dispatch.event_key,
            action_intent_id=dispatch.action_intent_id,
            payload_version_id=dispatch.payload_version_id,
            reservation_key=dispatch.reservation_key,
            reconciliation_key=dispatch.reconciliation_key,
            send_account_id=dispatch.send_account_id,
            readonly_credential_handle=dispatch.readonly_credential_handle,
            account_subject=dispatch.account_subject,
            payload=dispatch.payload,
            lease_owner="unit-gmail-send",
            lease_token=uuid4(),
        ),
    )

    assert isinstance(direct, GmailSentMetadata)
    assert found is not None
    assert found.provider_message_id == "msg-1"


def test_runtime_factory_is_disabled_by_default_without_external_configuration() -> None:
    worker = create_runtime_gmail_send_worker(
        settings=Settings.model_validate({}),
        engine=object(),  # type: ignore[arg-type]
        owner="unit-gmail-send",
        clock=lambda: NOW,
    )

    assert worker.status().enabled is False
    assert worker.run_once().outcome is GmailSendWorkerOutcome.DISABLED


def _enabled_settings() -> Settings:
    return Settings.model_validate(
        {
            "external_writes_enabled": True,
            "auto_send_enabled": True,
            "google_oauth_enabled": True,
            "gmail_send_enabled": True,
            "gmail_send_release_attested": True,
            "gmail_send_broker_socket": "/tmp/send.sock",
            "mailbox_broker_socket": "/tmp/readonly.sock",
            "gmail_send_attachment_broker_socket": "/tmp/attachment.sock",
        }
    )


def test_runtime_factory_stays_disabled_if_validated_attachment_gate_is_bypassed() -> None:
    worker = create_runtime_gmail_send_worker(
        settings=_enabled_settings().model_copy(
            update={"gmail_send_attachment_broker_socket": None}
        ),
        engine=object(),  # type: ignore[arg-type]
        owner="unit-gmail-send",
        clock=lambda: NOW,
    )

    assert worker.status().enabled is False
    assert worker.status().disabled_reason == "GMAIL_SEND_DISABLED"


def test_runtime_factory_enables_only_with_explicit_attachment_socket_or_resolver() -> None:
    worker = create_runtime_gmail_send_worker(
        settings=_enabled_settings(),
        engine=object(),  # type: ignore[arg-type]
        owner="unit-gmail-send",
        clock=lambda: NOW,
    )

    assert worker.status().enabled is True


def test_unix_attachment_resolver_uses_bounded_base64_envelope() -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="co-gmail-send-"))
    socket_path = socket_dir / "a.sock"
    payload = b"data"
    ref = GmailSendAttachmentRef(
        object_key="resume-1",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    captured: list[dict[str, object]] = []
    ready = threading.Event()

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                request = connection.recv(8192)
                captured.append(json.loads(request.decode("utf-8")))
                connection.sendall(
                    json.dumps({"data_base64": base64.b64encode(payload).decode("ascii")}).encode(
                        "utf-8"
                    )
                )

    try:
        thread = threading.Thread(target=server)
        thread.start()
        assert ready.wait(timeout=5)
        resolved = UnixAttachmentResolver(socket_path=socket_path).resolve(ref)
        thread.join(timeout=5)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert resolved.data == payload
    assert captured[0]["object_key"] == "resume-1"
    assert "data_hex" not in captured[0]
