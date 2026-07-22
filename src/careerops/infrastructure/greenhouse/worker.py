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
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.application.greenhouse_submit import GreenhouseAttachmentRef
from careerops.application.greenhouse_submit_outbox import (
    GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
    GreenhouseSubmitCoordinator,
    GreenhouseSubmitOutboxSink,
    PreparedGreenhouseSubmitDispatch,
    PreparedGreenhouseSubmitState,
)
from careerops.application.outbox import ClaimedOutboxEvent, InternalDeliveryError
from careerops.config import Settings
from careerops.infrastructure.database.greenhouse_submit import (
    PostgresGreenhouseSubmitCoordinator,
    PostgresGreenhouseSubmitOutboxStore,
)
from careerops.infrastructure.greenhouse.client import (
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
    UnixGreenhouseSubmissionBroker,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_ATTACHMENT_BROKER_VERSION = "careerops.greenhouse.attachment-broker.v1"
_DEFAULT_SUBMIT_SOCKET = Path("/run/careerops-greenhouse/broker.sock")
_DEFAULT_ATTACHMENT_SOCKET = Path("/run/careerops-greenhouse/attachments.sock")


class GreenhouseSubmitWorkerOutcome(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    ACCEPTED_UNVERIFIED = "accepted_unverified"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    DEFERRED_PREPOST = "deferred_prepost"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GreenhouseSubmitWorkerStatus:
    enabled: bool
    owner: str
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GreenhouseSubmitWorkerRunResult:
    outcome: GreenhouseSubmitWorkerOutcome
    claimed: int
    accepted_unverified: int
    rejected: int
    ambiguous: int
    deferred_prepost: int
    failed: int
    disabled_reason: str | None = None


class GreenhouseSubmitStore(Protocol):
    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]: ...

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None: ...

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
    ) -> None: ...


class GreenhouseSubmitDispatcher(Protocol):
    def dispatch_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> GreenhouseSubmitWorkerRunResult: ...


class GreenhouseSubmitWorker:
    def __init__(
        self,
        dispatcher: GreenhouseSubmitDispatcher,
        *,
        owner: str,
        enabled: bool,
        disabled_reason: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_owner(owner)
        self._dispatcher = dispatcher
        self._owner = owner
        self._enabled = enabled
        self._disabled_reason = disabled_reason
        self._clock = clock or _utc_now

    def status(self) -> GreenhouseSubmitWorkerStatus:
        disabled_reason = None
        if not self._enabled:
            disabled_reason = self._disabled_reason or "GREENHOUSE_SUBMIT_DISABLED"
        return GreenhouseSubmitWorkerStatus(
            enabled=self._enabled,
            owner=self._owner,
            disabled_reason=disabled_reason,
        )

    def run_once(
        self,
        *,
        limit: int = 10,
        lease_for: timedelta = timedelta(seconds=30),
    ) -> GreenhouseSubmitWorkerRunResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if not self._enabled:
            return GreenhouseSubmitWorkerRunResult(
                outcome=GreenhouseSubmitWorkerOutcome.DISABLED,
                claimed=0,
                accepted_unverified=0,
                rejected=0,
                ambiguous=0,
                deferred_prepost=0,
                failed=0,
                disabled_reason=self._disabled_reason or "GREENHOUSE_SUBMIT_DISABLED",
            )
        return self._dispatcher.dispatch_batch(
            owner=self._owner,
            now=self._clock(),
            lease_for=lease_for,
            limit=limit,
        )


class DisabledGreenhouseSubmitDispatcher:
    def dispatch_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> GreenhouseSubmitWorkerRunResult:
        del owner, now, lease_for, limit
        return GreenhouseSubmitWorkerRunResult(
            outcome=GreenhouseSubmitWorkerOutcome.DISABLED,
            claimed=0,
            accepted_unverified=0,
            rejected=0,
            ambiguous=0,
            deferred_prepost=0,
            failed=0,
            disabled_reason="GREENHOUSE_SUBMIT_DISABLED",
        )


class OneShotGreenhouseSubmitDispatcher:
    """Dispatch Greenhouse submit events without retrying post-start outcomes."""

    def __init__(
        self,
        store: GreenhouseSubmitStore,
        sink: GreenhouseSubmitOutboxSink,
        *,
        retry_delay: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        self._store = store
        self._sink = sink
        self._retry_delay = retry_delay
        self._clock = clock or _utc_now

    def dispatch_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> GreenhouseSubmitWorkerRunResult:
        events = self._store.claim(
            owner=owner,
            now=now,
            lease_for=lease_for,
            limit=limit,
            event_key_prefix=GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX,
        )
        accepted_unverified = 0
        rejected = 0
        ambiguous = 0
        deferred_prepost = 0
        failed = 0
        for event in events:
            try:
                self._sink.deliver(event)
            except InternalDeliveryError as error:
                completed_at = self._clock()
                self._store.release(
                    event.event_id,
                    owner=owner,
                    lease_token=event.lease_token,
                    now=completed_at,
                    retry_at=completed_at + self._retry_delay,
                    error_code=error.error_code,
                    terminal=not error.retryable,
                )
                if error.retryable:
                    deferred_prepost += 1
                elif "AMBIGUOUS" in error.error_code:
                    ambiguous += 1
                else:
                    failed += 1
                continue
            transition = last_greenhouse_submit_transition(self._sink)
            self._store.mark_published(
                event.event_id,
                owner=owner,
                lease_token=event.lease_token,
                now=self._clock(),
            )
            if transition is _GreenhouseSubmitTransition.REJECTED:
                rejected += 1
            elif transition is _GreenhouseSubmitTransition.AMBIGUOUS:
                ambiguous += 1
            else:
                accepted_unverified += 1
        return GreenhouseSubmitWorkerRunResult(
            outcome=_batch_outcome(
                claimed=len(events),
                accepted_unverified=accepted_unverified,
                rejected=rejected,
                ambiguous=ambiguous,
                deferred_prepost=deferred_prepost,
                failed=failed,
            ),
            claimed=len(events),
            accepted_unverified=accepted_unverified,
            rejected=rejected,
            ambiguous=ambiguous,
            deferred_prepost=deferred_prepost,
            failed=failed,
        )


class UnixGreenhouseAttachmentResolver:
    def __init__(
        self,
        *,
        socket_path: Path,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 20 * 1024 * 1024 + 8192,
    ) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("Greenhouse attachment broker socket path must be absolute")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if max_response_bytes < 256 or max_response_bytes > 24 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 256 and 24MiB")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def resolve(self, ref: object) -> GreenhouseResolvedAttachment:
        if not isinstance(ref, GreenhouseAttachmentRef):
            raise RuntimeError("Greenhouse attachment resolver received an unsupported ref")
        request = {
            "version": _ATTACHMENT_BROKER_VERSION,
            "object_key": ref.object_key,
            "expected_filename": ref.filename,
            "expected_content_type": ref.content_type,
            "expected_size_bytes": ref.size_bytes,
            "expected_sha256": ref.sha256,
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
            raise RuntimeError("Greenhouse attachment broker returned invalid base64") from None
        if len(data) != ref.size_bytes:
            raise RuntimeError("Greenhouse attachment broker returned mismatched size")
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RuntimeError("Greenhouse attachment broker returned mismatched sha256")
        return GreenhouseResolvedAttachment(ref=ref, data=data)


class ObservedGreenhouseSubmitCoordinator:
    def __init__(self, delegate: GreenhouseSubmitCoordinator) -> None:
        self._delegate = delegate
        self.last_transition: _GreenhouseSubmitTransition | None = None

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGreenhouseSubmitDispatch:
        self.last_transition = None
        dispatch = self._delegate.prepare(event)
        if (
            dispatch.state is PreparedGreenhouseSubmitState.ALREADY_TERMINAL
            and dispatch.terminal_evidence is not None
        ):
            self.last_transition = _transition_from_evidence(dispatch.terminal_evidence)
        return dispatch

    def record_accepted_unverified(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence,
    ) -> None:
        self._delegate.record_accepted_unverified(dispatch, evidence=evidence)
        self.last_transition = _GreenhouseSubmitTransition.ACCEPTED_UNVERIFIED

    def record_rejected(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
        reason_codes: Sequence[str],
    ) -> None:
        self._delegate.record_rejected(
            dispatch,
            evidence=evidence,
            error_code=error_code,
            reason_codes=reason_codes,
        )
        self.last_transition = _GreenhouseSubmitTransition.REJECTED

    def record_ambiguous(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
    ) -> None:
        self._delegate.record_ambiguous(dispatch, evidence=evidence, error_code=error_code)
        self.last_transition = _GreenhouseSubmitTransition.AMBIGUOUS

    def record_prepost_failure(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        error_code: str,
    ) -> None:
        self._delegate.record_prepost_failure(dispatch, error_code=error_code)
        self.last_transition = _GreenhouseSubmitTransition.PREPOST_FAILURE


def create_runtime_greenhouse_submit_worker(
    *,
    settings: Settings,
    engine: Engine,
    owner: str,
    attachment_resolver: UnixGreenhouseAttachmentResolver | None = None,
    clock: Callable[[], datetime] | None = None,
) -> GreenhouseSubmitWorker:
    enabled = settings.greenhouse_submit_runtime_enabled
    disabled_reason = None if enabled else "GREENHOUSE_SUBMIT_DISABLED"
    submit_socket = settings.greenhouse_submit_credential_broker_socket or _DEFAULT_SUBMIT_SOCKET
    attachment_socket = settings.greenhouse_submit_attachment_broker_socket
    resolved_attachment_resolver = attachment_resolver
    if resolved_attachment_resolver is None and attachment_socket is not None:
        resolved_attachment_resolver = UnixGreenhouseAttachmentResolver(
            socket_path=Path(attachment_socket)
        )
    if enabled and resolved_attachment_resolver is None:
        enabled = False
        disabled_reason = "GREENHOUSE_SUBMIT_ATTACHMENT_RESOLVER_UNCONFIGURED"
    if not enabled:
        return GreenhouseSubmitWorker(
            DisabledGreenhouseSubmitDispatcher(),
            owner=owner,
            enabled=False,
            disabled_reason=disabled_reason,
            clock=clock,
        )

    coordinator = ObservedGreenhouseSubmitCoordinator(
        PostgresGreenhouseSubmitCoordinator(engine, owner=owner)
    )
    sink = GreenhouseSubmitOutboxSink(
        coordinator,
        UnixGreenhouseSubmissionBroker(socket_path=submit_socket, now=clock),
        resolved_attachment_resolver
        or UnixGreenhouseAttachmentResolver(socket_path=_DEFAULT_ATTACHMENT_SOCKET),
        clock=clock,
    )
    dispatcher = OneShotGreenhouseSubmitDispatcher(
        PostgresGreenhouseSubmitOutboxStore(engine),
        sink,
        clock=clock,
    )
    return GreenhouseSubmitWorker(
        dispatcher,
        owner=owner,
        enabled=enabled,
        disabled_reason=disabled_reason,
        clock=clock,
    )


def run_worker_forever(
    worker: GreenhouseSubmitWorker,
    *,
    poll_seconds: float,
    limit: int,
) -> None:
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("poll_seconds must be between 0 and 60")
    while True:
        worker.run_once(limit=limit)
        sleep(poll_seconds)


class _GreenhouseSubmitTransition(StrEnum):
    ACCEPTED_UNVERIFIED = "accepted_unverified"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    PREPOST_FAILURE = "prepost_failure"


def last_greenhouse_submit_transition(
    sink: GreenhouseSubmitOutboxSink,
) -> _GreenhouseSubmitTransition | None:
    coordinator = getattr(sink, "_coordinator", None)
    if isinstance(coordinator, ObservedGreenhouseSubmitCoordinator):
        return coordinator.last_transition
    return None


def _batch_outcome(
    *,
    claimed: int,
    accepted_unverified: int,
    rejected: int,
    ambiguous: int,
    deferred_prepost: int,
    failed: int,
) -> GreenhouseSubmitWorkerOutcome:
    if claimed == 0:
        return GreenhouseSubmitWorkerOutcome.IDLE
    if failed:
        return GreenhouseSubmitWorkerOutcome.FAILED
    if deferred_prepost:
        return GreenhouseSubmitWorkerOutcome.DEFERRED_PREPOST
    if ambiguous:
        return GreenhouseSubmitWorkerOutcome.AMBIGUOUS
    if rejected:
        return GreenhouseSubmitWorkerOutcome.REJECTED
    if accepted_unverified:
        return GreenhouseSubmitWorkerOutcome.ACCEPTED_UNVERIFIED
    return GreenhouseSubmitWorkerOutcome.ACCEPTED_UNVERIFIED


def _transition_from_evidence(
    evidence: GreenhouseSubmissionEvidence,
) -> _GreenhouseSubmitTransition:
    if evidence.outcome is GreenhouseSubmissionOutcome.REJECTED:
        return _GreenhouseSubmitTransition.REJECTED
    if evidence.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS:
        return _GreenhouseSubmitTransition.AMBIGUOUS
    return _GreenhouseSubmitTransition.ACCEPTED_UNVERIFIED


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
        raise RuntimeError("Greenhouse attachment broker unavailable") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Greenhouse attachment broker returned invalid json") from None
    if not isinstance(decoded, Mapping):
        raise RuntimeError("Greenhouse attachment broker envelope must be an object")
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
            raise RuntimeError("Greenhouse attachment broker response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Greenhouse attachment broker missing {field_name}")
    return value


def _validate_owner(owner: str) -> None:
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("owner must be a bounded actor identifier")


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__: Sequence[str] = (
    "DisabledGreenhouseSubmitDispatcher",
    "GreenhouseSubmitWorker",
    "GreenhouseSubmitWorkerOutcome",
    "GreenhouseSubmitWorkerRunResult",
    "GreenhouseSubmitWorkerStatus",
    "ObservedGreenhouseSubmitCoordinator",
    "OneShotGreenhouseSubmitDispatcher",
    "UnixGreenhouseAttachmentResolver",
    "create_runtime_greenhouse_submit_worker",
    "run_worker_forever",
)
