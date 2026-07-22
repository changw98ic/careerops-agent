from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import careerops.infrastructure.gmail.broker_server as broker_server
from careerops.application.gmail_send import GmailSendAttachmentRef
from careerops.infrastructure.gmail.broker_server import (
    ATTACHMENT_BROKER_VERSION,
    READONLY_CREDENTIAL_BROKER_VERSION,
    SEND_CREDENTIAL_BROKER_VERSION,
    BrokerServerError,
    CredentialTokenEnvelope,
    GmailCredentialBrokerServer,
    GmailSendAttachmentBrokerServer,
    StaticCredentialTokenProvider,
)
from careerops.infrastructure.gmail.credentials import (
    GMAIL_READONLY_SCOPE,
    GmailCredentialHandle,
    UnixGmailCredentialBroker,
)
from careerops.infrastructure.gmail.send_credentials import (
    GMAIL_SEND_SCOPE,
    GmailSendCredentialHandle,
    UnixGmailSendCredentialBroker,
)
from careerops.infrastructure.gmail.send_worker import UnixAttachmentResolver

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _token_provider() -> StaticCredentialTokenProvider:
    return StaticCredentialTokenProvider(
        {
            (
                "gmail:readonly:opaque",
                "candidate@example.com",
                GMAIL_READONLY_SCOPE,
                None,
            ): CredentialTokenEnvelope(
                access_token="ya29.readonly-runtime",
                account_subject="candidate@example.com",
                scope=GMAIL_READONLY_SCOPE,
                expires_at=NOW + timedelta(minutes=10),
            ),
            (
                "gmail:send:opaque",
                "candidate@example.com",
                GMAIL_SEND_SCOPE,
                "send_email",
            ): CredentialTokenEnvelope(
                access_token="ya29.send-runtime",
                account_subject="candidate@example.com",
                scope=GMAIL_SEND_SCOPE,
                expires_at=NOW + timedelta(minutes=10),
            ),
        }
    )


class _BlockingTokenProvider:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def issue_token(
        self,
        *,
        opaque_handle: str,
        account_subject: str,
        scope: str,
        action: str | None,
    ) -> CredentialTokenEnvelope:
        del opaque_handle, action
        self.started.set()
        assert self.release.wait(timeout=1)
        return CredentialTokenEnvelope(
            access_token="ya29.readonly-runtime",
            account_subject=account_subject,
            scope=scope,
            expires_at=NOW + timedelta(minutes=10),
        )


def test_credential_server_serves_existing_readonly_and_send_client_protocols(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-broker-", dir="/tmp"))
    socket_path = socket_dir / "gmail.sock"
    stop_event = threading.Event()
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    thread = _start_server_thread(server, stop_event)

    try:
        readonly = UnixGmailCredentialBroker(socket_path=socket_path, now=lambda: NOW).resolve(
            GmailCredentialHandle(
                opaque_handle="gmail:readonly:opaque",
                account_subject="candidate@example.com",
                granted_scopes=(GMAIL_READONLY_SCOPE,),
            )
        )
        send = UnixGmailSendCredentialBroker(socket_path=socket_path, now=lambda: NOW).resolve(
            GmailSendCredentialHandle(
                opaque_handle="gmail:send:opaque",
                account_subject="candidate@example.com",
                granted_scopes=(GMAIL_SEND_SCOPE,),
            )
        )
    finally:
        parent_mode = socket_path.parent.stat().st_mode & 0o777
        stop_event.set()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert readonly.value == "ya29.readonly-runtime"
    assert readonly.granted_scopes == (GMAIL_READONLY_SCOPE,)
    assert send.value == "ya29.send-runtime"
    assert send.granted_scopes == (GMAIL_SEND_SCOPE,)
    assert parent_mode == 0o700


def test_credential_server_rejects_wrong_missing_and_extra_request_fields(tmp_path) -> None:
    server = GmailCredentialBrokerServer(
        socket_path=tmp_path / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    valid = {
        "version": READONLY_CREDENTIAL_BROKER_VERSION,
        "provider": "gmail",
        "opaque_handle": "gmail:readonly:opaque",
        "account_subject": "candidate@example.com",
        "scope": GMAIL_READONLY_SCOPE,
    }

    for bad in (
        {**valid, "provider": "google"},
        {**valid, "scope": GMAIL_SEND_SCOPE},
        {**valid, "opaque_handle": "../secret"},
        {**valid, "account_subject": "candidate example.com"},
        {**valid, "action": "send_email"},
        {key: value for key, value in valid.items() if key != "scope"},
    ):
        assert json.loads(server.handle_request_bytes(_dump(bad))) == {
            "error": "broker request rejected"
        }

    send_without_action = {
        "version": SEND_CREDENTIAL_BROKER_VERSION,
        "provider": "gmail",
        "opaque_handle": "gmail:send:opaque",
        "account_subject": "candidate@example.com",
        "scope": GMAIL_SEND_SCOPE,
    }
    assert json.loads(server.handle_request_bytes(_dump(send_without_action))) == {
        "error": "broker request rejected"
    }


def test_credential_server_bounds_request_and_error_response(tmp_path) -> None:
    server = GmailCredentialBrokerServer(
        socket_path=tmp_path / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )

    response = server.handle_request_bytes(b"{" + (b'"x":' + b'"y"' * 4096))

    assert len(response) <= 512
    assert json.loads(response) == {"error": "broker request rejected"}
    assert b"ya29" not in response


def test_credential_server_rejects_invalid_connection_timeout(tmp_path) -> None:
    with pytest.raises(ValueError, match="connection_timeout_seconds"):
        GmailCredentialBrokerServer(
            socket_path=tmp_path / "broker.sock",
            token_provider=_token_provider(),
            connection_timeout_seconds=0,
        )


def test_attachment_server_rejects_invalid_max_concurrent_connections(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()

    with pytest.raises(ValueError, match="max_concurrent_connections"):
        GmailSendAttachmentBrokerServer(
            socket_path=tmp_path / "attachment.sock",
            attachment_root=root,
            max_concurrent_connections=0,
        )


def test_credential_server_times_out_incomplete_socket_clients(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-timeout-", dir="/tmp"))
    socket_path = socket_dir / "broker.sock"
    stop_event = threading.Event()
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
        connection_timeout_seconds=0.05,
    )
    thread = _start_server_thread(server, stop_event)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(socket_path))
            client.sendall(b"{")
            response = client.recv(1024)
    finally:
        stop_event.set()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert json.loads(response) == {"error": "broker request rejected"}
    assert b"ya29" not in response


def test_credential_server_rejects_clients_over_concurrency_limit(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-capacity-", dir="/tmp"))
    socket_path = socket_dir / "broker.sock"
    stop_event = threading.Event()
    provider = _BlockingTokenProvider()
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=provider,
        now=lambda: NOW,
        connection_timeout_seconds=1,
        max_concurrent_connections=1,
    )
    thread = _start_server_thread(server, stop_event)

    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        first.settimeout(1)
        first.connect(str(socket_path))
        first.sendall(
            _dump(
                {
                    "version": READONLY_CREDENTIAL_BROKER_VERSION,
                    "provider": "gmail",
                    "opaque_handle": "gmail:readonly:opaque",
                    "account_subject": "candidate@example.com",
                    "scope": GMAIL_READONLY_SCOPE,
                }
            )
        )
        first.shutdown(socket.SHUT_WR)
        assert provider.started.wait(timeout=1)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as second:
            second.settimeout(1)
            second.connect(str(socket_path))
            second_response = second.recv(1024)

        provider.release.set()
        first_response = first.recv(1024)
    finally:
        provider.release.set()
        first.close()
        stop_event.set()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert json.loads(second_response) == {"error": "broker request rejected"}
    assert b"ya29" not in second_response
    assert json.loads(first_response)["access_token"] == "ya29.readonly-runtime"


def test_socket_creation_rejects_symlink_and_non_socket_collisions(tmp_path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(real_parent)
    server = GmailCredentialBrokerServer(
        socket_path=symlink_parent / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    try:
        server.serve_forever(stop_event=threading.Event())
    except BrokerServerError as exc:
        assert "parent" in str(exc)
    else:
        raise AssertionError("symlink socket parent was not rejected")

    target = tmp_path / "target"
    target.write_text("not a socket")
    symlink_socket = tmp_path / "linked.sock"
    symlink_socket.symlink_to(target)
    server = GmailCredentialBrokerServer(
        socket_path=symlink_socket,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )

    try:
        server.serve_forever(stop_event=threading.Event())
    except BrokerServerError as exc:
        assert "collision" in str(exc)
    else:
        raise AssertionError("symlink socket collision was not rejected")
    assert symlink_socket.is_symlink()

    plain_socket_path = tmp_path / "plain.sock"
    plain_socket_path.write_text("not a socket")
    server = GmailCredentialBrokerServer(
        socket_path=plain_socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    try:
        server.serve_forever(stop_event=threading.Event())
    except BrokerServerError as exc:
        assert "collision" in str(exc)
    else:
        raise AssertionError("plain file socket collision was not rejected")
    assert plain_socket_path.read_text() == "not a socket"


def test_socket_creation_rejects_shared_parent_without_changing_mode(tmp_path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    socket_path = parent / "broker.sock"
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )

    with pytest.raises(BrokerServerError, match="parent"):
        server.serve_forever(stop_event=threading.Event())

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert not (parent / ".broker.sock.lock").exists()


def test_socket_creation_accepts_existing_owner_only_parent(tmp_path) -> None:
    del tmp_path
    parent = Path(tempfile.mkdtemp(prefix="gmail-private-", dir="/tmp"))
    server = GmailCredentialBrokerServer(
        socket_path=parent / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    stop_event = threading.Event()
    stop_event.set()

    try:
        server.serve_forever(stop_event=stop_event)

        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_socket_creation_sets_created_parent_mode_to_owner_only(tmp_path) -> None:
    del tmp_path
    socket_root = Path(tempfile.mkdtemp(prefix="gmail-created-", dir="/tmp"))
    parent = socket_root / "created"
    server = GmailCredentialBrokerServer(
        socket_path=parent / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    stop_event = threading.Event()
    stop_event.set()

    try:
        server.serve_forever(stop_event=stop_event)

        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(socket_root, ignore_errors=True)


@pytest.mark.parametrize("mode", [0o500, 0o600])
def test_socket_creation_rejects_parent_without_owner_write_or_execute(
    tmp_path,
    mode: int,
) -> None:
    parent = tmp_path / "restricted"
    parent.mkdir(mode=0o700)
    parent.chmod(mode)
    server = GmailCredentialBrokerServer(
        socket_path=parent / "broker.sock",
        token_provider=_token_provider(),
        now=lambda: NOW,
    )

    try:
        with pytest.raises(BrokerServerError, match="parent"):
            server.serve_forever(stop_event=threading.Event())
    finally:
        parent.chmod(0o700)


def test_socket_creation_refuses_live_socket_without_taking_it_over(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-live-", dir="/tmp"))
    socket_path = socket_dir / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    listener.settimeout(1)
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    stop_event = threading.Event()
    stop_event.set()

    try:
        with pytest.raises(BrokerServerError, match="already in use"):
            server.serve_forever(stop_event=stop_event)
        probe_connection, _ = listener.accept()
        probe_connection.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(socket_path))
    finally:
        listener.close()
        with suppress(OSError):
            socket_path.unlink()
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_socket_creation_does_not_remove_other_user_socket(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-other-", dir="/tmp"))
    socket_path = socket_dir / "other.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    real_uid = os.getuid()
    calls = 0

    def _getuid() -> int:
        nonlocal calls
        calls += 1
        return real_uid if calls == 1 else real_uid + 1

    monkeypatch.setattr(broker_server.os, "getuid", _getuid)

    try:
        with pytest.raises(BrokerServerError, match="collision"):
            broker_server._prepare_socket_path(socket_path)

        assert socket_path.exists()
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def test_attachment_server_serves_existing_resolver_protocol(tmp_path) -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-attachment-", dir="/tmp"))
    socket_path = socket_dir / "attachment.sock"
    root = tmp_path / "attachments"
    root.mkdir()
    payload = b"resume bytes"
    object_key = _put_attachment(root, payload)
    ref = GmailSendAttachmentRef(
        object_key=object_key,
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    stop_event = threading.Event()
    server = GmailSendAttachmentBrokerServer(
        socket_path=socket_path,
        attachment_root=root,
        max_attachment_bytes=1024,
    )
    thread = _start_server_thread(server, stop_event)

    try:
        resolved = UnixAttachmentResolver(socket_path=socket_path).resolve(ref)
    finally:
        stop_event.set()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert resolved.ref == ref
    assert resolved.data == payload


def test_attachment_server_rejects_metadata_mismatch_and_path_escape(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    payload = b"resume bytes"
    object_key = _put_attachment(root, payload)
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    (root / "escape").symlink_to(outside)
    server = GmailSendAttachmentBrokerServer(
        socket_path=tmp_path / "attachment.sock",
        attachment_root=root,
        max_attachment_bytes=1024,
    )
    valid = {
        "version": ATTACHMENT_BROKER_VERSION,
        "object_key": object_key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "filename": "resume.pdf",
        "content_type": "application/pdf",
    }

    response = server.handle_request_bytes(_dump(valid))
    assert json.loads(response) == {"data_base64": base64.b64encode(payload).decode("ascii")}

    for bad in (
        {**valid, "object_key": "/tmp/resume-1"},
        {**valid, "object_key": "../resume-1"},
        {**valid, "object_key": "escape"},
        {**valid, "sha256": "0" * 64},
        {**valid, "size_bytes": len(payload) + 1},
        {**valid, "filename": "../resume.pdf"},
        {**valid, "extra": True},
    ):
        assert json.loads(server.handle_request_bytes(_dump(bad))) == {
            "error": "broker request rejected"
        }


def test_attachment_server_rejects_oversized_files(tmp_path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    payload = b"too large"
    object_key = _put_attachment(root, payload)
    server = GmailSendAttachmentBrokerServer(
        socket_path=tmp_path / "attachment.sock",
        attachment_root=root,
        max_attachment_bytes=len(payload) - 1,
    )
    request = {
        "version": ATTACHMENT_BROKER_VERSION,
        "object_key": object_key,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "filename": "resume.pdf",
        "content_type": "application/pdf",
    }

    assert json.loads(server.handle_request_bytes(_dump(request))) == {
        "error": "broker request rejected"
    }


def _dump(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _put_attachment(root: Path, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    leaf = root / "sha256" / digest[:2] / digest[2:4]
    leaf.mkdir(parents=True)
    (leaf / digest).write_bytes(payload)
    return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def _start_server_thread(
    server: GmailCredentialBrokerServer | GmailSendAttachmentBrokerServer,
    stop_event: threading.Event,
) -> threading.Thread:
    ready_event = threading.Event()
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"stop_event": stop_event, "ready_event": ready_event},
    )
    thread.start()
    assert ready_event.wait(timeout=2), f"socket listener was not ready: {server.socket_path}"
    return thread


def test_real_socket_rejects_wrong_peer_uid_when_injected(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-auth-", dir="/tmp"))
    socket_path = socket_dir / "broker.sock"
    stop_event = threading.Event()
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        allowed_uid=os.getuid() + 1,
        now=lambda: NOW,
    )
    thread = _start_server_thread(server, stop_event)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            client.sendall(
                _dump(
                    {
                        "version": READONLY_CREDENTIAL_BROKER_VERSION,
                        "provider": "gmail",
                        "opaque_handle": "gmail:readonly:opaque",
                        "account_subject": "candidate@example.com",
                        "scope": GMAIL_READONLY_SCOPE,
                    }
                )
            )
            client.shutdown(socket.SHUT_WR)
            response = client.recv(1024)
    finally:
        stop_event.set()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir, ignore_errors=True)

    assert json.loads(response) == {"error": "broker request rejected"}
    assert b"ya29" not in response


def test_stale_socket_is_replaced_safely(tmp_path) -> None:
    del tmp_path
    socket_dir = Path(tempfile.mkdtemp(prefix="gmail-stale-", dir="/tmp"))
    socket_path = socket_dir / "broker.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    stop_event = threading.Event()
    server = GmailCredentialBrokerServer(
        socket_path=socket_path,
        token_provider=_token_provider(),
        now=lambda: NOW,
    )
    thread = _start_server_thread(server, stop_event)

    stop_event.set()
    thread.join(timeout=2)
    shutil.rmtree(socket_dir, ignore_errors=True)
