from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import struct
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from careerops.infrastructure.gmail.credentials import GMAIL_READONLY_SCOPE
from careerops.infrastructure.gmail.send_credentials import GMAIL_SEND_SCOPE

READONLY_CREDENTIAL_BROKER_VERSION: Final = "careerops.gmail.credential-broker.v1"
SEND_CREDENTIAL_BROKER_VERSION: Final = "careerops.gmail.send-credential-broker.v1"
ATTACHMENT_BROKER_VERSION: Final = "careerops.gmail-send.attachment-broker.v1"
MAX_CREDENTIAL_REQUEST_BYTES: Final = 8192
MAX_ATTACHMENT_REQUEST_BYTES: Final = 8192
DEFAULT_MAX_ATTACHMENT_BYTES: Final = 25 * 1024 * 1024
DEFAULT_CONNECTION_TIMEOUT_SECONDS: Final = 2.0
DEFAULT_MAX_CONCURRENT_CONNECTIONS: Final = 20
_ERROR_RESPONSE_LIMIT: Final = 512
_HANDLE_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@/-")
_OBJECT_KEY = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_LOCK_FILE_FLAGS = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)


class BrokerServerError(RuntimeError):
    """Broker server setup or request handling failed safely."""


@dataclass(frozen=True, slots=True)
class CredentialTokenEnvelope:
    access_token: str
    account_subject: str
    scope: str
    expires_at: datetime


class CredentialTokenProvider(Protocol):
    def issue_token(
        self,
        *,
        opaque_handle: str,
        account_subject: str,
        scope: str,
        action: str | None,
    ) -> CredentialTokenEnvelope: ...


@dataclass(frozen=True, slots=True)
class StaticCredentialTokenProvider:
    """Small injectable provider for tests and local smoke runs."""

    tokens: Mapping[tuple[str, str, str, str | None], CredentialTokenEnvelope]

    def issue_token(
        self,
        *,
        opaque_handle: str,
        account_subject: str,
        scope: str,
        action: str | None,
    ) -> CredentialTokenEnvelope:
        try:
            return self.tokens[(opaque_handle, account_subject, scope, action)]
        except KeyError as exc:
            raise BrokerServerError("credential not found") from exc


class GmailCredentialBrokerServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        token_provider: CredentialTokenProvider,
        allowed_uid: int | None = None,
        now: Callable[[], datetime] | None = None,
        connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        max_concurrent_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    ) -> None:
        self._socket_path = _validate_absolute_socket_path(socket_path)
        self._token_provider = token_provider
        self._allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._now = now or (lambda: datetime.now(UTC))
        self._connection_timeout_seconds = _validate_connection_timeout(connection_timeout_seconds)
        self._max_concurrent_connections = _validate_max_concurrent_connections(
            max_concurrent_connections
        )

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def handle_request_bytes(self, raw: bytes) -> bytes:
        try:
            request = _decode_request(raw, MAX_CREDENTIAL_REQUEST_BYTES)
            is_send = request.get("version") == SEND_CREDENTIAL_BROKER_VERSION
            _validate_credential_request(request, is_send=is_send)
            envelope = self._token_provider.issue_token(
                opaque_handle=cast("str", request["opaque_handle"]),
                account_subject=cast("str", request["account_subject"]),
                scope=cast("str", request["scope"]),
                action=cast("str | None", request.get("action")),
            )
            _validate_token_envelope(envelope, request, now=self._now())
            return _json_response(
                {
                    "access_token": envelope.access_token,
                    "account_subject": envelope.account_subject,
                    "scope": envelope.scope,
                    "expires_at": envelope.expires_at.isoformat(),
                }
            )
        except Exception as exc:
            return _error_response(exc)

    def serve_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        ready_event: threading.Event | None = None,
    ) -> None:
        _serve_unix_json(
            socket_path=self._socket_path,
            allowed_uid=self._allowed_uid,
            max_request_bytes=MAX_CREDENTIAL_REQUEST_BYTES,
            handler=self.handle_request_bytes,
            stop_event=stop_event,
            ready_event=ready_event,
            connection_timeout_seconds=self._connection_timeout_seconds,
            max_concurrent_connections=self._max_concurrent_connections,
        )


class GmailSendAttachmentBrokerServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        attachment_root: Path,
        allowed_uid: int | None = None,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS,
        max_concurrent_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS,
    ) -> None:
        self._socket_path = _validate_absolute_socket_path(socket_path)
        self._attachment_root = Path(attachment_root)
        if not self._attachment_root.is_absolute():
            raise ValueError("attachment_root must be absolute")
        self._attachment_root = self._attachment_root.resolve(strict=True)
        if not self._attachment_root.is_dir():
            raise ValueError("attachment_root must be a directory")
        if max_attachment_bytes < 1 or max_attachment_bytes > 32 * 1024 * 1024:
            raise ValueError("max_attachment_bytes must be between 1 and 32MiB")
        self._allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._max_attachment_bytes = max_attachment_bytes
        self._connection_timeout_seconds = _validate_connection_timeout(connection_timeout_seconds)
        self._max_concurrent_connections = _validate_max_concurrent_connections(
            max_concurrent_connections
        )

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def handle_request_bytes(self, raw: bytes) -> bytes:
        try:
            request = _decode_request(raw, MAX_ATTACHMENT_REQUEST_BYTES)
            _validate_attachment_request(request)
            data = self._read_attachment(
                object_key=cast("str", request["object_key"]),
                expected_size=cast("int", request["size_bytes"]),
                expected_sha256=cast("str", request["sha256"]),
            )
            return _json_response({"data_base64": base64.b64encode(data).decode("ascii")})
        except Exception as exc:
            return _error_response(exc)

    def serve_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        ready_event: threading.Event | None = None,
    ) -> None:
        _serve_unix_json(
            socket_path=self._socket_path,
            allowed_uid=self._allowed_uid,
            max_request_bytes=MAX_ATTACHMENT_REQUEST_BYTES,
            handler=self.handle_request_bytes,
            stop_event=stop_event,
            ready_event=ready_event,
            connection_timeout_seconds=self._connection_timeout_seconds,
            max_concurrent_connections=self._max_concurrent_connections,
        )

    def _read_attachment(
        self,
        *,
        object_key: str,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes:
        match = _OBJECT_KEY.fullmatch(object_key)
        if match is None:
            raise BrokerServerError("attachment path rejected")
        first, second, digest = match.groups()
        if digest[:2] != first or digest[2:4] != second or digest != expected_sha256:
            raise BrokerServerError("attachment digest mismatch")
        descriptors: list[int] = []
        try:
            descriptor = os.open(self._attachment_root, _DIRECTORY_FLAGS)
            descriptors.append(descriptor)
            for part in ("sha256", first, second):
                descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                descriptors.append(descriptor)
            file_descriptor = os.open(digest, _FILE_FLAGS, dir_fd=descriptor)
            descriptors.append(file_descriptor)
            st = os.fstat(file_descriptor)
            if not stat.S_ISREG(st.st_mode):
                raise BrokerServerError("attachment path rejected")
            if st.st_size != expected_size or st.st_size > self._max_attachment_bytes:
                raise BrokerServerError("attachment size mismatch")
            chunks: list[bytes] = []
            remaining = expected_size + 1
            while remaining > 0:
                chunk = os.read(file_descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) != expected_size:
                raise BrokerServerError("attachment size mismatch")
            if hashlib.sha256(data).hexdigest() != expected_sha256:
                raise BrokerServerError("attachment digest mismatch")
            return data
        except OSError as exc:
            raise BrokerServerError("attachment path rejected") from exc
        finally:
            for descriptor in reversed(descriptors):
                with suppress(OSError):
                    os.close(descriptor)


def _serve_unix_json(
    *,
    socket_path: Path,
    allowed_uid: int,
    max_request_bytes: int,
    handler: Callable[[bytes], bytes],
    stop_event: threading.Event | None,
    ready_event: threading.Event | None,
    connection_timeout_seconds: float,
    max_concurrent_connections: int,
) -> None:
    _prepare_socket_parent(socket_path.parent)
    with _socket_path_lock(socket_path):
        _prepare_socket_path(socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            bound_st = socket_path.lstat()
            listener.listen(20)
            listener.settimeout(0.2)
            if ready_event is not None:
                ready_event.set()
            available_slots = threading.BoundedSemaphore(max_concurrent_connections)
            try:
                while stop_event is None or not stop_event.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    connection.settimeout(connection_timeout_seconds)
                    if not available_slots.acquire(blocking=False):
                        with connection, suppress(OSError):
                            connection.sendall(
                                _error_response(BrokerServerError("broker is at capacity"))
                            )
                        continue
                    thread = threading.Thread(
                        target=_handle_connection_with_slot,
                        args=(
                            connection,
                            allowed_uid,
                            max_request_bytes,
                            handler,
                            available_slots,
                        ),
                        daemon=True,
                    )
                    thread.start()
            finally:
                _unlink_socket_if_unchanged(socket_path, bound_st)


def _handle_connection_with_slot(
    connection: socket.socket,
    allowed_uid: int,
    max_request_bytes: int,
    handler: Callable[[bytes], bytes],
    available_slots: threading.BoundedSemaphore,
) -> None:
    try:
        _handle_connection(connection, allowed_uid, max_request_bytes, handler)
    finally:
        available_slots.release()


def _handle_connection(
    connection: socket.socket,
    allowed_uid: int,
    max_request_bytes: int,
    handler: Callable[[bytes], bytes],
) -> None:
    with connection:
        try:
            peer_uid = _peer_uid(connection)
            response = (
                _error_response(BrokerServerError("unauthorized peer"))
                if peer_uid != allowed_uid
                else handler(_recv_one_request(connection, max_request_bytes))
            )
            connection.sendall(response)
        except (BrokerServerError, OSError):
            with suppress(OSError):
                connection.sendall(_error_response(BrokerServerError("broker request rejected")))
            return


def _prepare_socket_path(socket_path: Path) -> None:
    _validate_socket_parent(socket_path.parent)
    try:
        st = socket_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode):
        raise BrokerServerError("socket path collision")
    if st.st_uid != os.getuid():
        raise BrokerServerError("socket path collision")
    if _socket_accepts_connections(socket_path):
        raise BrokerServerError("socket path already in use")
    _unlink_socket_if_unchanged(socket_path, st)


def _prepare_socket_parent(parent: Path) -> None:
    created = False
    try:
        parent_st = parent.lstat()
    except FileNotFoundError:
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=False)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise BrokerServerError("socket parent rejected") from exc
        try:
            parent_st = parent.lstat()
        except OSError as exc:
            raise BrokerServerError("socket parent rejected") from exc
    except OSError as exc:
        raise BrokerServerError("socket parent rejected") from exc

    if (
        stat.S_ISLNK(parent_st.st_mode)
        or not stat.S_ISDIR(parent_st.st_mode)
        or parent_st.st_uid != os.getuid()
    ):
        raise BrokerServerError("socket parent rejected")

    if created:
        try:
            os.chmod(parent, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise BrokerServerError("socket parent rejected") from exc

    _validate_socket_parent(parent)


def _validate_socket_parent(parent: Path) -> None:
    try:
        parent_st = parent.lstat()
    except OSError as exc:
        raise BrokerServerError("socket parent rejected") from exc
    mode = stat.S_IMODE(parent_st.st_mode)
    if (
        stat.S_ISLNK(parent_st.st_mode)
        or not stat.S_ISDIR(parent_st.st_mode)
        or parent_st.st_uid != os.getuid()
        or mode & 0o077
        or mode & 0o300 != 0o300
    ):
        raise BrokerServerError("socket parent rejected")


@contextmanager
def _socket_path_lock(socket_path: Path) -> Generator[None]:
    lock_path = socket_path.with_name(f".{socket_path.name}.lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, _LOCK_FILE_FLAGS, 0o600)
        lock_st = os.fstat(descriptor)
        if not stat.S_ISREG(lock_st.st_mode) or lock_st.st_uid != os.getuid():
            raise BrokerServerError("socket lock rejected")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise BrokerServerError("socket path already in use") from exc
    except OSError as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise BrokerServerError("socket lock rejected") from exc
    except Exception:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise
    try:
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(descriptor)


def _socket_accepts_connections(socket_path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.1)
        try:
            client.connect(str(socket_path))
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                return False
            raise BrokerServerError("socket liveness probe failed") from exc
    return True


def _unlink_socket_if_unchanged(socket_path: Path, expected_st: os.stat_result) -> None:
    try:
        st = socket_path.lstat()
    except FileNotFoundError:
        return
    if (
        st.st_ino != expected_st.st_ino
        or st.st_dev != expected_st.st_dev
        or st.st_uid != expected_st.st_uid
        or not stat.S_ISSOCK(st.st_mode)
    ):
        raise BrokerServerError("socket path changed")
    socket_path.unlink()


def _validate_absolute_socket_path(socket_path: Path) -> Path:
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("socket_path must be absolute")
    return path


def _validate_connection_timeout(value: float) -> float:
    if value <= 0 or value > 30:
        raise ValueError("connection_timeout_seconds must be between 0 and 30")
    return value


def _validate_max_concurrent_connections(value: int) -> int:
    if isinstance(value, bool) or value < 1 or value > 256:
        raise ValueError("max_concurrent_connections must be between 1 and 256")
    return value


def _recv_one_request(connection: socket.socket, max_request_bytes: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = connection.recv(min(4096, max_request_bytes + 1 - received))
        if not chunk:
            break
        received += len(chunk)
        if received > max_request_bytes:
            raise BrokerServerError("request too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_request(raw: bytes, max_request_bytes: int) -> dict[str, object]:
    if not raw or len(raw) > max_request_bytes:
        raise BrokerServerError("invalid request")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerServerError("invalid request") from exc
    if not isinstance(decoded, dict):
        raise BrokerServerError("invalid request")
    return cast("dict[str, object]", decoded)


def _validate_credential_request(request: Mapping[str, object], *, is_send: bool) -> None:
    expected_keys = (
        {"version", "provider", "action", "opaque_handle", "account_subject", "scope"}
        if is_send
        else {"version", "provider", "opaque_handle", "account_subject", "scope"}
    )
    if set(request) != expected_keys:
        raise BrokerServerError("invalid credential request")
    expected_version = (
        SEND_CREDENTIAL_BROKER_VERSION if is_send else READONLY_CREDENTIAL_BROKER_VERSION
    )
    expected_scope = GMAIL_SEND_SCOPE if is_send else GMAIL_READONLY_SCOPE
    if request["version"] != expected_version:
        raise BrokerServerError("invalid credential request")
    if request["provider"] != "gmail":
        raise BrokerServerError("invalid credential request")
    if is_send and request["action"] != "send_email":
        raise BrokerServerError("invalid credential request")
    _require_bounded_handle(request["opaque_handle"])
    _require_bounded_account(request["account_subject"])
    if request["scope"] != expected_scope:
        raise BrokerServerError("invalid credential request")


def _validate_token_envelope(
    envelope: CredentialTokenEnvelope,
    request: Mapping[str, object],
    *,
    now: datetime,
) -> None:
    if envelope.account_subject != request["account_subject"]:
        raise BrokerServerError("credential account mismatch")
    if envelope.scope != request["scope"]:
        raise BrokerServerError("credential scope mismatch")
    if not 8 <= len(envelope.access_token) <= 4096:
        raise BrokerServerError("invalid credential")
    if envelope.expires_at.tzinfo is None or envelope.expires_at.utcoffset() is None:
        raise BrokerServerError("invalid credential")
    if now.tzinfo is None or now.utcoffset() is None:
        raise BrokerServerError("invalid broker clock")
    if envelope.expires_at <= now:
        raise BrokerServerError("expired credential")


def _validate_attachment_request(request: Mapping[str, object]) -> None:
    expected_keys = {"version", "object_key", "sha256", "size_bytes", "filename", "content_type"}
    if set(request) != expected_keys:
        raise BrokerServerError("invalid attachment request")
    if request["version"] != ATTACHMENT_BROKER_VERSION:
        raise BrokerServerError("invalid attachment request")
    _require_bounded_handle(request["object_key"])
    if not isinstance(request["sha256"], str) or len(request["sha256"]) != 64:
        raise BrokerServerError("invalid attachment request")
    try:
        int(request["sha256"], 16)
    except ValueError as exc:
        raise BrokerServerError("invalid attachment request") from exc
    if not isinstance(request["size_bytes"], int) or request["size_bytes"] < 0:
        raise BrokerServerError("invalid attachment request")
    if not isinstance(request["filename"], str) or not 1 <= len(request["filename"]) <= 255:
        raise BrokerServerError("invalid attachment request")
    if "/" in request["filename"] or "\x00" in request["filename"]:
        raise BrokerServerError("invalid attachment request")
    if not isinstance(request["content_type"], str) or not 1 <= len(request["content_type"]) <= 255:
        raise BrokerServerError("invalid attachment request")
    if "\x00" in request["content_type"]:
        raise BrokerServerError("invalid attachment request")


def _require_bounded_handle(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 255:
        raise BrokerServerError("invalid request")
    if any(char not in _HANDLE_ALLOWED for char in value):
        raise BrokerServerError("invalid request")


def _require_bounded_account(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 321
        or "@" not in value
        or any(c.isspace() for c in value)
    ):
        raise BrokerServerError("invalid request")
    local, _, domain = value.partition("@")
    if not local or not domain:
        raise BrokerServerError("invalid request")


def _peer_uid(connection: socket.socket) -> int:
    os_getpeereid = cast("Any", getattr(os, "getpeereid", None))
    if os_getpeereid is not None:
        return cast("tuple[int, int]", os_getpeereid(connection.fileno()))[0]
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    try:
        getpeereid = ctypes.CDLL(None).getpeereid
    except AttributeError:
        getpeereid = None
    if getpeereid is not None:
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return int(uid.value)
    so_peercred = cast("Any", getattr(socket, "SO_PEERCRED", None))
    if so_peercred is not None:
        creds = connection.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        peer_pid, peer_uid, peer_gid = struct.unpack("3i", creds)
        del peer_pid, peer_gid
        return int(peer_uid)
    raise BrokerServerError("peer credential unavailable")


def _json_response(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _error_response(exc: Exception) -> bytes:
    del exc
    response = _json_response({"error": "broker request rejected"})
    return response[:_ERROR_RESPONSE_LIMIT]


__all__ = [
    "ATTACHMENT_BROKER_VERSION",
    "DEFAULT_CONNECTION_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "DEFAULT_MAX_CONCURRENT_CONNECTIONS",
    "READONLY_CREDENTIAL_BROKER_VERSION",
    "SEND_CREDENTIAL_BROKER_VERSION",
    "BrokerServerError",
    "CredentialTokenEnvelope",
    "CredentialTokenProvider",
    "GmailCredentialBrokerServer",
    "GmailSendAttachmentBrokerServer",
    "StaticCredentialTokenProvider",
]
