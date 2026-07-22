from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from careerops.cli import gmail_broker


class RecordingHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def import_client(
        self,
        payload: Mapping[str, object],
        *,
        label: str | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(("import_client", {"payload": dict(payload), "label": label}))
        return {
            "ok": True,
            "client_id": payload.get("client_id"),
            "client_secret": payload.get("client_secret"),
            "label": label,
        }

    def build_authorization_url(self, request: gmail_broker.AuthorizationRequest) -> str:
        self.calls.append(("build_authorization_url", request))
        return (
            "https://accounts.google.example/auth?"
            f"state={request.state}&challenge={request.code_challenge}"
        )

    def exchange_authorization_code(
        self,
        grant: gmail_broker.AuthorizationCodeGrant,
    ) -> Mapping[str, object]:
        self.calls.append(("exchange_authorization_code", grant))
        return {
            "ok": True,
            "scope": grant.scope,
            "scopes": grant.scopes,
            "account_subject": "user@example.com",
            "refresh_token": "refresh-secret",
            "handle": "gmail:readonly:user@example.com",
        }

    def serve(
        self,
        *,
        readonly_socket: Path | None = None,
        send_socket: Path | None = None,
        attachment_socket: Path | None = None,
        attachment_root: Path | None = None,
    ) -> Mapping[str, object] | None:
        self.calls.append(
            (
                "serve",
                {
                    "readonly_socket": readonly_socket,
                    "send_socket": send_socket,
                    "attachment_socket": attachment_socket,
                    "attachment_root": attachment_root,
                },
            )
        )
        return {"ok": True, "readonly_socket": readonly_socket, "send_socket": send_socket}

    def status(self) -> Mapping[str, object]:
        self.calls.append(("status", None))
        return {
            "ok": True,
            "handle": "gmail:status:do-not-print",
            "readonly": {"configured": True, "refresh_token": "hidden"},
            "send": {"configured": False},
        }

    def revoke(
        self,
        *,
        scope: str | None = None,
        account_subject: str | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(("revoke", {"scope": scope, "account_subject": account_subject}))
        return {"ok": True, "scope": scope, "account_subject": account_subject}

    def smoke_send(self, *, account_subject: str) -> Mapping[str, object]:
        self.calls.append(("smoke_send", {"account_subject": account_subject}))
        return {
            "account_subject": account_subject,
            "evidence_sha256": "a" * 64,
            "live_qualified": True,
            "ok": True,
            "scope": "send",
        }

    def recover_smoke_send(
        self,
        *,
        account_subject: str,
        expected_subject_sha256: str | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(
            (
                "recover_smoke_send",
                {
                    "account_subject": account_subject,
                    "expected_subject_sha256": expected_subject_sha256,
                },
            )
        )
        return {
            "account_subject": account_subject,
            "evidence_sha256": "b" * 64,
            "live_qualified": True,
            "ok": True,
            "recovered": True,
            "scope": "send",
        }


class FakeCallbackServer:
    def __init__(self, *, code: str = "auth-code") -> None:
        self.redirect_uri = "http://127.0.0.1:49152/oauth2/callback"
        self.code = code
        self.expected_states: list[str] = []
        self.closed = False

    def wait_for_code(self, *, expected_state: str) -> str:
        self.expected_states.append(expected_state)
        return self.code

    def close(self) -> None:
        self.closed = True


def test_gmail_broker_import_client_redacts_client_secret(tmp_path: Path, capsys) -> None:
    client_file = tmp_path / "client.json"
    client_file.write_text(
        json.dumps({"client_id": "client-id", "client_secret": "raw-secret"}),
        encoding="utf-8",
    )
    host = RecordingHost()

    assert (
        gmail_broker.main(
            ("--json", "import-client", str(client_file), "--label", "readonly"),
            host=host,
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "client_id": "<redacted>",
        "client_secret": "<redacted>",
        "label": "readonly",
        "ok": True,
    }
    assert "raw-secret" not in json.dumps(payload)
    assert host.calls == [
        (
            "import_client",
            {
                "payload": {"client_id": "client-id", "client_secret": "raw-secret"},
                "label": "readonly",
            },
        )
    ]


def test_gmail_broker_authorize_uses_state_pkce_and_injected_callback_without_browser(
    capsys,
) -> None:
    host = RecordingHost()
    callback = FakeCallbackServer()
    opened: list[str] = []

    assert (
        gmail_broker.main(
            (
                "--json",
                "authorize",
                "send",
                "--account-subject",
                "user@example.com",
                "--no-open-browser",
                "--timeout-seconds",
                "5",
            ),
            host=host,
            callback_server_factory=lambda timeout: callback,
            browser_opener=opened.append,
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["scope"] == "send"
    assert payload["refresh_token"] == "<redacted>"
    assert len(payload["handle_sha256"]) == 64
    assert "handle" not in payload
    assert "gmail:readonly:user@example.com" not in json.dumps(payload)
    assert opened == []
    assert callback.closed is True
    assert callback.expected_states == ["token-32"]

    build_call = cast("gmail_broker.AuthorizationRequest", host.calls[0][1])
    exchange_call = cast("gmail_broker.AuthorizationCodeGrant", host.calls[1][1])
    assert host.calls[0][0] == "build_authorization_url"
    assert host.calls[1][0] == "exchange_authorization_code"
    assert build_call.redirect_uri == callback.redirect_uri
    assert build_call.account_subject == "user@example.com"
    assert build_call.profile == "send"
    assert build_call.state == "token-32"
    assert build_call.code_challenge_method == "S256"
    assert build_call.code_verifier == "token-64"
    assert build_call.code_challenge == gmail_broker._pkce_challenge("token-64")
    assert exchange_call.code == "auth-code"
    assert exchange_call.state == "token-32"
    assert exchange_call.code_verifier == "token-64"


def test_gmail_broker_authorize_normalizes_account_before_persisting(capsys) -> None:
    host = RecordingHost()
    callback = FakeCallbackServer()

    assert (
        gmail_broker.main(
            (
                "--json",
                "authorize",
                "send",
                "--account-subject",
                " User@Example.COM ",
                "--no-open-browser",
                "--timeout-seconds",
                "5",
            ),
            host=host,
            callback_server_factory=lambda timeout: callback,
            browser_opener=lambda url: None,
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["ok"] is True
    request = cast("gmail_broker.AuthorizationRequest", host.calls[0][1])
    grant = cast("gmail_broker.AuthorizationCodeGrant", host.calls[1][1])
    assert request.account_subject == "user@example.com"
    assert grant.account_subject == "user@example.com"


def test_gmail_broker_authorize_no_open_browser_emits_url_before_waiting(capsys) -> None:
    host = RecordingHost()
    opened: list[str] = []
    observed_pending_events: list[dict[str, object]] = []

    class CapturingCallbackServer(FakeCallbackServer):
        def wait_for_code(self, *, expected_state: str) -> str:
            captured = capsys.readouterr()
            assert captured.out == ""
            event = json.loads(captured.err)
            observed_pending_events.append(event)
            return super().wait_for_code(expected_state=expected_state)

    callback = CapturingCallbackServer()

    assert (
        gmail_broker.main(
            (
                "--json",
                "authorize",
                "readonly",
                "--account-subject",
                "user@example.com",
                "--no-open-browser",
            ),
            host=host,
            callback_server_factory=lambda timeout: callback,
            browser_opener=opened.append,
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 0
    )

    final_output = json.loads(capsys.readouterr().out)
    expected_url = (
        "https://accounts.google.example/auth?state=token-32&challenge="
        + gmail_broker._pkce_challenge("token-64")
    )
    assert observed_pending_events == [
        {
            "authorization_url": expected_url,
            "event": "gmail_oauth_authorization_url",
            "ok": True,
        }
    ]
    assert final_output["ok"] is True
    assert opened == []

    rendered_events = json.dumps(observed_pending_events)
    assert "token-64" not in rendered_events
    assert "refresh-secret" not in rendered_events
    assert "client_secret" not in rendered_events


def test_gmail_broker_authorize_no_open_browser_normal_mode_emits_url_to_stderr(capsys) -> None:
    host = RecordingHost()

    assert (
        gmail_broker.main(
            (
                "authorize",
                "readonly",
                "--account-subject",
                "user@example.com",
                "--no-open-browser",
            ),
            host=host,
            callback_server_factory=lambda timeout: FakeCallbackServer(),
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "gmail broker ok: scope=readonly" in captured.out
    assert "https://accounts.google.example/auth?state=token-32" in captured.err
    assert "token-64" not in captured.err
    assert "refresh-secret" not in captured.err


def test_gmail_broker_authorize_refuses_to_print_sensitive_authorization_url(capsys) -> None:
    class UnsafeAuthorizationHost(RecordingHost):
        def build_authorization_url(self, request: gmail_broker.AuthorizationRequest) -> str:
            self.calls.append(("build_authorization_url", request))
            return (
                "https://accounts.google.example/auth?"
                f"state={request.state}&code_verifier={request.code_verifier}"
            )

    assert (
        gmail_broker.main(
            (
                "--json",
                "authorize",
                "readonly",
                "--account-subject",
                "user@example.com",
                "--no-open-browser",
            ),
            host=UnsafeAuthorizationHost(),
            callback_server_factory=lambda timeout: FakeCallbackServer(),
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "token-64" not in captured.err
    assert "code_verifier" not in captured.err
    assert json.loads(captured.err)["errors"] == [
        "Gmail authorization URL contains a sensitive query parameter"
    ]


def test_gmail_broker_authorize_opens_browser_by_default(capsys) -> None:
    host = RecordingHost()
    opened: list[str] = []

    assert (
        gmail_broker.main(
            ("--json", "authorize", "readonly", "--account-subject", "user@example.com"),
            host=host,
            callback_server_factory=lambda timeout: FakeCallbackServer(),
            browser_opener=opened.append,
            token_urlsafe=lambda size: f"state-{size}",
        )
        == 0
    )

    capsys.readouterr()
    expected_url = (
        "https://accounts.google.example/auth?state=state-32&challenge="
        + gmail_broker._pkce_challenge("state-64")
    )
    assert opened == [expected_url]


def test_gmail_broker_serve_status_smoke_recover_and_revoke_parse_arguments(capsys) -> None:
    host = RecordingHost()

    assert (
        gmail_broker.main(
            (
                "--json",
                "serve",
                "--readonly-socket",
                "/tmp/readonly.sock",
                "--send-socket",
                "/tmp/send.sock",
                "--attachment-socket",
                "/tmp/attach.sock",
                "--attachment-root",
                "/tmp/attachments",
            ),
            host=host,
        )
        == 0
    )
    assert gmail_broker.main(("--json", "status", "--scope", "readonly"), host=host) == 0
    assert (
        gmail_broker.main(
            ("--json", "smoke-send", "--account-subject", "user@example.com"),
            host=host,
        )
        == 0
    )
    assert (
        gmail_broker.main(
            (
                "--json",
                "recover-smoke-send",
                "--account-subject",
                "user@example.com",
                "--expected-subject-sha256",
                "c" * 64,
            ),
            host=host,
        )
        == 0
    )
    assert (
        gmail_broker.main(
            ("--json", "revoke", "--scope", "send", "--account-subject", "user@example.com"),
            host=host,
        )
        == 0
    )

    output = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert output[0] == {
        "ok": True,
        "readonly_socket": "/tmp/readonly.sock",
        "send_socket": "/tmp/send.sock",
    }
    assert output[1]["readonly"]["refresh_token"] == "<redacted>"
    assert len(output[1]["handle_sha256"]) == 64
    assert "handle" not in output[1]
    assert output[1]["scope"] == "readonly"
    assert output[2]["live_qualified"] is True
    assert output[3]["live_qualified"] is True
    assert output[3]["recovered"] is True
    assert output[4] == {"ok": True, "scope": "send", "account_subject": "user@example.com"}
    assert host.calls[0] == (
        "serve",
        {
            "readonly_socket": Path("/tmp/readonly.sock"),
            "send_socket": Path("/tmp/send.sock"),
            "attachment_socket": Path("/tmp/attach.sock"),
            "attachment_root": Path("/tmp/attachments"),
        },
    )
    assert host.calls[1:] == [
        ("status", None),
        ("smoke_send", {"account_subject": "user@example.com"}),
        (
            "recover_smoke_send",
            {
                "account_subject": "user@example.com",
                "expected_subject_sha256": "c" * 64,
            },
        ),
        ("revoke", {"scope": "send", "account_subject": "user@example.com"}),
    ]


def test_gmail_broker_serve_keyboard_interrupt_returns_130_without_traceback(capsys) -> None:
    class InterruptingHost(RecordingHost):
        def serve(
            self,
            *,
            readonly_socket: Path | None = None,
            send_socket: Path | None = None,
            attachment_socket: Path | None = None,
            attachment_root: Path | None = None,
        ) -> Mapping[str, object] | None:
            self.calls.append(
                (
                    "serve",
                    {
                        "readonly_socket": readonly_socket,
                        "send_socket": send_socket,
                        "attachment_socket": attachment_socket,
                        "attachment_root": attachment_root,
                    },
                )
            )
            raise KeyboardInterrupt

    host = InterruptingHost()

    assert gmail_broker.main(("serve", "--readonly-socket", "/tmp/readonly.sock"), host=host) == 130

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert host.calls == [
        (
            "serve",
            {
                "readonly_socket": Path("/tmp/readonly.sock"),
                "send_socket": None,
                "attachment_socket": None,
                "attachment_root": None,
            },
        )
    ]


def test_gmail_broker_serve_json_keyboard_interrupt_returns_130_without_traceback(
    capsys,
) -> None:
    class InterruptingHost(RecordingHost):
        def serve(
            self,
            *,
            readonly_socket: Path | None = None,
            send_socket: Path | None = None,
            attachment_socket: Path | None = None,
            attachment_root: Path | None = None,
        ) -> Mapping[str, object] | None:
            raise KeyboardInterrupt

    assert gmail_broker.main(("--json", "serve"), host=InterruptingHost()) == 130

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_gmail_broker_has_required_commands_and_safe_callback_headers() -> None:
    parser = gmail_broker._parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    choices = cast("dict[str, object]", subparsers.choices)
    assert set(choices) == {
        "authorize",
        "import-client",
        "recover-smoke-send",
        "revoke",
        "serve",
        "smoke-send",
        "status",
    }

    headers = dict(gmail_broker._callback_response_headers(len(gmail_broker.CALLBACK_HTML)))
    assert headers["Cache-Control"] == "no-store"
    assert headers["Pragma"] == "no-cache"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "secret" not in gmail_broker.CALLBACK_HTML.lower()


def test_gmail_broker_errors_redact_sensitive_values(capsys) -> None:
    class FailingHost(RecordingHost):
        def status(self) -> Mapping[str, object]:
            raise RuntimeError("refresh_token raw-secret leaked upstream")

    assert gmail_broker.main(("--json", "status"), host=FailingHost()) == 1

    captured = capsys.readouterr()
    assert "raw-secret" not in captured.err
    assert json.loads(captured.err)["errors"] == ["sensitive Gmail broker value redacted"]


def test_gmail_broker_rejects_unbounded_authorize_timeout(capsys) -> None:
    assert (
        gmail_broker.main(
            (
                "--json",
                "authorize",
                "readonly",
                "--account-subject",
                "user@example.com",
                "--timeout-seconds",
                "601",
            ),
            host=RecordingHost(),
            callback_server_factory=lambda timeout: FakeCallbackServer(),
            token_urlsafe=lambda size: f"token-{size}",
        )
        == 1
    )
    assert "timeout_seconds" in capsys.readouterr().err
