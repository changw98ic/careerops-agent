from __future__ import annotations

import base64
import errno
import hashlib
import json
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from time import sleep
from typing import Any, Protocol, cast

from sqlalchemy.engine import Engine

from careerops.application.gmail_send import GmailSendAttachmentRef, GmailSendProviderState
from careerops.application.gmail_send_outbox import (
    GMAIL_SEND_EVENT_KEY_PREFIX,
    GmailSendCredentialError,
    GmailSendOutboxSink,
    GmailSendProviderReceipt,
    GmailSendReconciliationJob,
    GmailSendReconciliationService,
    GmailSentMetadata,
    PreparedGmailSendDispatch,
)
from careerops.application.gmail_send_outbox import (
    GmailSendAccessToken as AppGmailSendAccessToken,
)
from careerops.application.outbox import OutboxPublisher, PublishBatchResult
from careerops.config import Settings
from careerops.infrastructure.gmail.client import GmailApiError, GmailReadOnlyHttpClient
from careerops.infrastructure.gmail.credentials import (
    GMAIL_READONLY_SCOPE,
    GmailAccessToken,
    GmailCredentialBrokerError,
    GmailCredentialHandle,
    UnixGmailCredentialBroker,
)
from careerops.infrastructure.gmail.send_client import (
    GmailSendApiError,
    GmailSendHttpClient,
    GmailSendResolvedAttachment,
)
from careerops.infrastructure.gmail.send_credentials import (
    GMAIL_SEND_SCOPE,
    GmailSendCredentialBrokerError,
    GmailSendCredentialHandle,
    UnixGmailSendCredentialBroker,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_BROKER_VERSION = "careerops.gmail-send.attachment-broker.v1"


class GmailSendWorkerOutcome(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    SENT = "sent"
    RECONCILED = "reconciled"
    DEFERRED = "deferred"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GmailSendWorkerStatus:
    enabled: bool
    owner: str
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GmailSendWorkerRunResult:
    outcome: GmailSendWorkerOutcome
    claimed: int
    published: int
    deferred: int
    failed: int
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GmailSendReconciliationRunResult:
    outcome: GmailSendWorkerOutcome
    claimed: int
    confirmed: int
    ambiguous: int
    disabled_reason: str | None = None


class GmailSendWorker:
    def __init__(
        self,
        publisher: OutboxPublisher,
        reconciler: GmailSendReconciliationService,
        *,
        owner: str,
        enabled: bool,
        disabled_reason: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_owner(owner)
        self._publisher = publisher
        self._reconciler = reconciler
        self._owner = owner
        self._enabled = enabled
        self._disabled_reason = disabled_reason
        self._clock = clock or (lambda: datetime.now(UTC))

    def status(self) -> GmailSendWorkerStatus:
        disabled_reason = None
        if not self._enabled:
            disabled_reason = self._disabled_reason or "GMAIL_SEND_DISABLED"
        return GmailSendWorkerStatus(
            enabled=self._enabled,
            owner=self._owner,
            disabled_reason=disabled_reason,
        )

    def run_once(
        self,
        *,
        limit: int = 10,
        lease_for: timedelta = timedelta(seconds=30),
    ) -> GmailSendWorkerRunResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if not self._enabled:
            return GmailSendWorkerRunResult(
                outcome=GmailSendWorkerOutcome.DISABLED,
                claimed=0,
                published=0,
                deferred=0,
                failed=0,
                disabled_reason=self._disabled_reason or "GMAIL_SEND_DISABLED",
            )
        result = self._publisher.publish_batch(
            owner=self._owner,
            now=self._clock(),
            lease_for=lease_for,
            limit=limit,
        )
        return _run_result_from_publish(result)

    def reconcile_once(self, *, limit: int = 10) -> GmailSendReconciliationRunResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if not self._enabled:
            return GmailSendReconciliationRunResult(
                outcome=GmailSendWorkerOutcome.DISABLED,
                claimed=0,
                confirmed=0,
                ambiguous=0,
                disabled_reason=self._disabled_reason or "GMAIL_SEND_DISABLED",
            )
        claimed, confirmed, ambiguous = self._reconciler.reconcile_batch(
            owner=self._owner,
            now=self._clock(),
            limit=limit,
        )
        return GmailSendReconciliationRunResult(
            outcome=GmailSendWorkerOutcome.IDLE
            if claimed == 0
            else GmailSendWorkerOutcome.RECONCILED
            if confirmed and not ambiguous
            else GmailSendWorkerOutcome.DEFERRED,
            claimed=claimed,
            confirmed=confirmed,
            ambiguous=ambiguous,
        )


class BrokerGmailSendCredentialResolver:
    def __init__(
        self,
        *,
        send_broker: UnixGmailSendCredentialBroker,
        readonly_broker: UnixGmailCredentialBroker,
    ) -> None:
        self._send_broker = send_broker
        self._readonly_broker = readonly_broker

    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> AppGmailSendAccessToken:
        try:
            return cast(
                "AppGmailSendAccessToken",
                self._send_broker.resolve(
                    GmailSendCredentialHandle(
                        opaque_handle=dispatch.send_credential_handle,
                        account_subject=dispatch.account_subject,
                        granted_scopes=(GMAIL_SEND_SCOPE,),
                    )
                ),
            )
        except GmailSendCredentialBrokerError as exc:
            raise GmailSendCredentialError("GMAIL_SEND_CREDENTIAL_BROKER_UNAVAILABLE") from exc

    def resolve_readonly(self, dispatch: PreparedGmailSendDispatch) -> AppGmailSendAccessToken:
        try:
            return cast(
                "AppGmailSendAccessToken",
                self._readonly_broker.resolve(
                    GmailCredentialHandle(
                        opaque_handle=dispatch.readonly_credential_handle,
                        account_subject=dispatch.account_subject,
                        granted_scopes=(GMAIL_READONLY_SCOPE,),
                    )
                ),
            )
        except GmailCredentialBrokerError as exc:
            raise GmailSendCredentialError("GMAIL_SEND_READONLY_BROKER_UNAVAILABLE") from exc


class HttpGmailSendProvider:
    def __init__(self, client: Any = None) -> None:
        self._client = client or GmailSendHttpClient()

    def send_message(
        self,
        *,
        token: AppGmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        attachments: Sequence[object],
        now: datetime,
    ) -> GmailSendProviderReceipt:
        try:
            client = cast("Any", self._client)
            receipt = client.send_message(
                token,
                payload=dispatch.payload,
                attachments=tuple(
                    cast("GmailSendResolvedAttachment", item) for item in attachments
                ),
            )
        except GmailSendApiError as exc:
            raise GmailSendCredentialError(exc.error_code) from exc
        return GmailSendProviderReceipt(
            provider_message_id=receipt.provider_message_id,
            provider_thread_id=receipt.provider_thread_id,
            rfc_message_id=dispatch.payload.message_id_header,
            provider_state=GmailSendProviderState.SENT_CONFIRMED,
            received_at=now,
        )


class HttpGmailSentMetadataProvider:
    def __init__(self, client: GmailReadOnlyHttpClient | None = None) -> None:
        self._client = client or GmailReadOnlyHttpClient()

    def get_sent_metadata(
        self,
        *,
        token: AppGmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> GmailSentMetadata:
        del dispatch
        try:
            message = self._client.get_message(
                cast("GmailAccessToken", token),
                message_id=receipt.provider_message_id,
                metadata_headers=("From", "To", "Subject", "Message-ID"),
            )
        except GmailApiError as exc:
            raise GmailSendCredentialError(exc.error_code) from exc
        return _sent_metadata_from_message(message)

    def find_sent_by_rfc_message_id(
        self,
        *,
        token: AppGmailSendAccessToken,
        job: GmailSendReconciliationJob,
    ) -> GmailSentMetadata | None:
        try:
            message = self._client.find_sent_message_by_rfc_message_id(
                cast("GmailAccessToken", token),
                rfc_message_id=job.payload.message_id_header,
                metadata_headers=("From", "To", "Subject", "Message-ID"),
            )
        except GmailApiError as exc:
            raise GmailSendCredentialError(exc.error_code) from exc
        return None if message is None else _sent_metadata_from_message(message)


class UnixAttachmentResolver:
    def __init__(
        self,
        *,
        socket_path: Path,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 25 * 1024 * 1024 + 8192,
    ) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("attachment broker socket path must be absolute")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if max_response_bytes < 256 or max_response_bytes > 32 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 256 and 32MiB")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def resolve(self, ref: GmailSendAttachmentRef) -> GmailSendResolvedAttachment:
        request = {
            "version": _BROKER_VERSION,
            "object_key": ref.object_key,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "filename": ref.filename,
            "content_type": ref.content_type,
        }
        response = _roundtrip_unix_json(
            socket_path=self._socket_path,
            timeout_seconds=self._timeout_seconds,
            max_response_bytes=self._max_response_bytes,
            request=request,
        )
        data_base64 = _required_str(response, "data_base64")
        try:
            data = base64.b64decode(data_base64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError):
            raise RuntimeError("attachment broker returned invalid base64") from None
        if len(data) != ref.size_bytes:
            raise RuntimeError("attachment broker returned mismatched attachment size")
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RuntimeError("attachment broker returned mismatched attachment digest")
        return GmailSendResolvedAttachment(ref=ref, data=data)


class DisabledGmailSendSink:
    def deliver(self, event: object) -> None:
        del event
        raise GmailSendCredentialError("GMAIL_SEND_DISABLED")


def create_runtime_gmail_send_worker(
    *,
    settings: Settings,
    engine: Engine,
    owner: str,
    attachment_resolver: UnixAttachmentResolver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> GmailSendWorker:
    from careerops.infrastructure.database.gmail_send import (
        PostgresGmailSendCoordinator,
        PostgresGmailSendOutboxStore,
        PostgresGmailSendReconciliationRepository,
    )

    enabled = settings.gmail_send_runtime_enabled
    disabled_reason = None if enabled else "GMAIL_SEND_DISABLED"
    send_socket = settings.gmail_send_broker_socket or Path("/run/careerops-gmail-send/broker.sock")
    readonly_socket = settings.mailbox_broker_socket or Path("/run/careerops-gmail/broker.sock")

    outbox_store = PostgresGmailSendOutboxStore(engine)
    attachment_socket = getattr(settings, "gmail_send_attachment_broker_socket", None)
    resolved_attachment_resolver = attachment_resolver
    if resolved_attachment_resolver is None and attachment_socket is not None:
        resolved_attachment_resolver = UnixAttachmentResolver(socket_path=Path(attachment_socket))

    if enabled and resolved_attachment_resolver is not None:
        credential_resolver = BrokerGmailSendCredentialResolver(
            send_broker=UnixGmailSendCredentialBroker(socket_path=send_socket),
            readonly_broker=UnixGmailCredentialBroker(socket_path=readonly_socket),
        )
        sent_metadata = HttpGmailSentMetadataProvider()
        sink = GmailSendOutboxSink(
            PostgresGmailSendCoordinator(engine, owner=owner),
            credential_resolver,
            HttpGmailSendProvider(),
            sent_metadata,
            resolved_attachment_resolver,
            clock=clock,
        )
        reconciler = GmailSendReconciliationService(
            PostgresGmailSendReconciliationRepository(engine),
            credential_resolver,
            sent_metadata,
            clock=clock,
        )
    else:
        sink = None
        reconciler = GmailSendReconciliationService(
            PostgresGmailSendReconciliationRepository(engine),
            _DisabledResolver(),
            _DisabledSentMetadata(),
            clock=clock,
        )
        if enabled and resolved_attachment_resolver is None:
            enabled = False
            disabled_reason = "GMAIL_SEND_ATTACHMENT_RESOLVER_UNCONFIGURED"

    return GmailSendWorker(
        OutboxPublisher(
            outbox_store,
            sink,
            event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
            clock=clock,
        ),
        reconciler,
        owner=owner,
        enabled=enabled,
        disabled_reason=disabled_reason,
        clock=clock,
    )


def run_worker_forever(
    worker: GmailSendWorker,
    *,
    poll_seconds: float,
    limit: int,
) -> None:
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("poll_seconds must be between 0 and 60")
    while True:
        worker.run_once(limit=limit)
        worker.reconcile_once(limit=limit)
        sleep(poll_seconds)


class _DisabledResolver:
    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> AppGmailSendAccessToken:
        del dispatch
        raise GmailSendCredentialError("GMAIL_SEND_DISABLED")

    def resolve_readonly(self, dispatch: PreparedGmailSendDispatch) -> AppGmailSendAccessToken:
        del dispatch
        raise GmailSendCredentialError("GMAIL_SEND_DISABLED")


class _DisabledSentMetadata:
    def get_sent_metadata(
        self,
        *,
        token: object,
        dispatch: object,
        receipt: object,
    ) -> GmailSentMetadata:
        del token, dispatch, receipt
        raise GmailSendCredentialError("GMAIL_SEND_DISABLED")

    def find_sent_by_rfc_message_id(
        self,
        *,
        token: object,
        job: object,
    ) -> GmailSentMetadata | None:
        del token, job
        raise GmailSendCredentialError("GMAIL_SEND_DISABLED")


def _sent_metadata_from_message(message: object) -> GmailSentMetadata:
    metadata = cast("AnyGmailMetadataMessage", message)
    return GmailSentMetadata(
        provider_message_id=metadata.id,
        provider_thread_id=metadata.thread_id,
        label_ids=metadata.label_ids,
        headers=metadata.headers,
    )


def _run_result_from_publish(result: PublishBatchResult) -> GmailSendWorkerRunResult:
    outcome = (
        GmailSendWorkerOutcome.IDLE
        if result.claimed == 0
        else GmailSendWorkerOutcome.FAILED
        if result.failed
        else GmailSendWorkerOutcome.DEFERRED
        if result.deferred
        else GmailSendWorkerOutcome.SENT
    )
    return GmailSendWorkerRunResult(
        outcome=outcome,
        claimed=result.claimed,
        published=result.published,
        deferred=result.deferred,
        failed=result.failed,
    )


def _roundtrip_unix_json(
    *,
    socket_path: Path,
    timeout_seconds: float,
    max_response_bytes: int,
    request: Mapping[str, object],
) -> Mapping[str, object]:
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            client.sendall(encoded)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                if exc.errno not in {errno.ENOTCONN, errno.EPIPE}:
                    raise
            raw = _recv_bounded(client, max_response_bytes)
    except OSError as exc:
        raise RuntimeError("attachment broker unavailable") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("attachment broker returned invalid json") from None
    if not isinstance(decoded, Mapping):
        raise RuntimeError("attachment broker envelope must be an object")
    return cast("Mapping[str, object]", decoded)


def _recv_bounded(client: socket.socket, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = client.recv(min(4096, max_bytes + 1 - received))
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise RuntimeError("attachment broker response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"attachment broker missing {field_name}")
    return value


def _validate_owner(owner: str) -> None:
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("owner must be a bounded actor identifier")


class AnyGmailSendAccessToken(Protocol):
    account_subject: str

    def is_expired(self, *, now: datetime | None = None) -> bool: ...

    @property
    def value(self) -> str: ...


class AnyGmailMetadataMessage(Protocol):
    id: str
    thread_id: str
    label_ids: tuple[str, ...]
    headers: Mapping[str, str]


__all__: Sequence[str] = (
    "BrokerGmailSendCredentialResolver",
    "GmailSendReconciliationRunResult",
    "GmailSendWorker",
    "GmailSendWorkerOutcome",
    "GmailSendWorkerRunResult",
    "GmailSendWorkerStatus",
    "HttpGmailSendProvider",
    "HttpGmailSentMetadataProvider",
    "UnixAttachmentResolver",
    "create_runtime_gmail_send_worker",
    "run_worker_forever",
)
