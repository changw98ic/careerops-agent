from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Final, Protocol, cast

GMAIL_READONLY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE: Final = "https://www.googleapis.com/auth/gmail.send"
CALLBACK_PATH: Final = "/oauth2/callback"
CALLBACK_HTML: Final = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<meta http-equiv='Cache-Control' content='no-store'>"
    "<title>CareerOps Gmail authorization</title></head>"
    "<body>Authorization received. You can close this window.</body></html>"
)
_REDACTED: Final = "<redacted>"
_SENSITIVE_KEY_PARTS: Final = (
    "access_token",
    "authorization_code",
    "client_id",
    "client_secret",
    "code_verifier",
    "id_token",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_RAW_HANDLE_KEYS: Final = frozenset(
    {
        "credential_handle",
        "handle",
        "opaque_handle",
    }
)
_AUTHORIZATION_URL_FORBIDDEN_QUERY_KEYS: Final = frozenset(
    {
        "access_token",
        "authorization_code",
        "client_secret",
        "code_verifier",
        "id_token",
        "private_key",
        "refresh_token",
    }
)
_SCOPES: Final = {
    "readonly": (GMAIL_READONLY_SCOPE,),
    "send": (GMAIL_SEND_SCOPE,),
}


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    scope: str
    scopes: tuple[str, ...]
    profile: str
    account_subject: str
    redirect_uri: str
    state: str
    code_verifier: str
    code_challenge: str
    code_challenge_method: str = "S256"

    def __repr__(self) -> str:
        return (
            "AuthorizationRequest("
            f"scope={self.scope!r}, scopes={self.scopes!r}, profile={self.profile!r}, "
            f"account_subject={self.account_subject!r}, redirect_uri={self.redirect_uri!r}, "
            "state=<redacted>, code_verifier=<redacted>, code_challenge=<redacted>, "
            "code_challenge_method='S256')"
        )


@dataclass(frozen=True, slots=True)
class AuthorizationCodeGrant:
    scope: str
    scopes: tuple[str, ...]
    profile: str
    account_subject: str
    redirect_uri: str
    code: str
    state: str
    code_verifier: str

    def __repr__(self) -> str:
        return (
            "AuthorizationCodeGrant("
            f"scope={self.scope!r}, scopes={self.scopes!r}, profile={self.profile!r}, "
            f"account_subject={self.account_subject!r}, redirect_uri={self.redirect_uri!r}, "
            "code=<redacted>, state=<redacted>, code_verifier=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    account_subject: str | None = None
    handle_sha256: str | None = None
    scopes: tuple[str, ...] = ()
    expires_at: str | None = None


class CallbackServerPort(Protocol):
    @property
    def redirect_uri(self) -> str: ...

    def wait_for_code(self, *, expected_state: str) -> str: ...

    def close(self) -> None: ...


class GmailBrokerHostPort(Protocol):
    def import_client(
        self,
        payload: Mapping[str, object],
        *,
        label: str | None = None,
    ) -> Mapping[str, object]: ...

    def build_authorization_url(self, request: AuthorizationRequest) -> str: ...

    def exchange_authorization_code(
        self, grant: AuthorizationCodeGrant
    ) -> Mapping[str, object]: ...

    def serve(
        self,
        *,
        readonly_socket: Path | None = None,
        send_socket: Path | None = None,
        attachment_socket: Path | None = None,
        attachment_root: Path | None = None,
    ) -> Mapping[str, object] | None: ...

    def status(self) -> Mapping[str, object]: ...

    def revoke(
        self, *, scope: str | None = None, account_subject: str | None = None
    ) -> Mapping[str, object]: ...

    def smoke_send(self, *, account_subject: str) -> Mapping[str, object]: ...

    def recover_smoke_send(
        self,
        *,
        account_subject: str,
        expected_subject_sha256: str | None = None,
    ) -> Mapping[str, object]: ...


CallbackServerFactory = Callable[[float], CallbackServerPort]
BrowserOpener = Callable[[str], object]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the host-side Gmail OAuth broker.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable redacted JSON")
    subcommands = parser.add_subparsers(dest="command", required=True)

    import_client = subcommands.add_parser("import-client")
    import_client.add_argument("client_json", type=Path)
    import_client.add_argument("--label", choices=sorted(_SCOPES), required=True)

    authorize = subcommands.add_parser("authorize")
    authorize.add_argument("scope", choices=sorted(_SCOPES))
    authorize.add_argument("--profile", choices=sorted(_SCOPES))
    authorize.add_argument("--account-subject", required=True)
    authorize.add_argument("--timeout-seconds", type=float, default=180.0)
    authorize.add_argument("--no-open-browser", action="store_true")

    serve = subcommands.add_parser("serve")
    serve.add_argument("--readonly-socket", type=Path)
    serve.add_argument("--send-socket", type=Path)
    serve.add_argument("--attachment-socket", type=Path)
    serve.add_argument("--attachment-root", type=Path)

    status = subcommands.add_parser("status")
    status.add_argument("--scope", choices=sorted(_SCOPES))

    smoke_send = subcommands.add_parser("smoke-send")
    smoke_send.add_argument("--account-subject", required=True)

    recover_smoke_send = subcommands.add_parser("recover-smoke-send")
    recover_smoke_send.add_argument("--account-subject", required=True)
    recover_smoke_send.add_argument("--expected-subject-sha256")

    revoke = subcommands.add_parser("revoke")
    revoke.add_argument("--scope", choices=sorted(_SCOPES), required=True)
    revoke.add_argument("--account-subject", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    host: GmailBrokerHostPort | None = None,
    callback_server_factory: CallbackServerFactory | None = None,
    browser_opener: BrowserOpener | None = None,
    token_urlsafe: Callable[[int], str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(args.json)
    try:
        resolved_host = host or _load_default_host()
        result = _run(
            args,
            host=resolved_host,
            callback_server_factory=callback_server_factory or create_loopback_callback_server,
            browser_opener=browser_opener or webbrowser.open,
            token_urlsafe=token_urlsafe or secrets.token_urlsafe,
        )
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    _emit(result, json_output=json_output)
    return 0


def _run(
    args: argparse.Namespace,
    *,
    host: GmailBrokerHostPort,
    callback_server_factory: CallbackServerFactory,
    browser_opener: BrowserOpener,
    token_urlsafe: Callable[[int], str],
) -> Mapping[str, object]:
    if args.command == "import-client":
        return host.import_client(_load_json_object(args.client_json), label=args.label)
    if args.command == "authorize":
        return _authorize(
            args,
            host=host,
            callback_server_factory=callback_server_factory,
            browser_opener=browser_opener,
            token_urlsafe=token_urlsafe,
        )
    if args.command == "serve":
        result = host.serve(
            readonly_socket=args.readonly_socket,
            send_socket=args.send_socket,
            attachment_socket=args.attachment_socket,
            attachment_root=args.attachment_root,
        )
        return result or {"ok": True}
    if args.command == "status":
        payload = dict(host.status())
        if args.scope is not None:
            payload["scope"] = args.scope
        return payload
    if args.command == "smoke-send":
        return host.smoke_send(account_subject=args.account_subject)
    if args.command == "recover-smoke-send":
        return host.recover_smoke_send(
            account_subject=args.account_subject,
            expected_subject_sha256=args.expected_subject_sha256,
        )
    if args.command == "revoke":
        return host.revoke(scope=args.scope, account_subject=args.account_subject)
    raise ValueError("unknown gmail broker command")


def _authorize(
    args: argparse.Namespace,
    *,
    host: GmailBrokerHostPort,
    callback_server_factory: CallbackServerFactory,
    browser_opener: BrowserOpener,
    token_urlsafe: Callable[[int], str],
) -> Mapping[str, object]:
    if args.timeout_seconds <= 0 or args.timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 0 and 600")
    account_subject = _normalize_account_subject(args.account_subject)
    state = token_urlsafe(32)
    code_verifier = token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)
    callback_server = callback_server_factory(args.timeout_seconds)
    try:
        request = AuthorizationRequest(
            scope=args.scope,
            scopes=_SCOPES[args.scope],
            profile=args.profile or args.scope,
            account_subject=account_subject,
            redirect_uri=callback_server.redirect_uri,
            state=state,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )
        authorization_url = host.build_authorization_url(request)
        _validate_authorization_url_safe_to_print(authorization_url)
        if not args.no_open_browser:
            browser_opener(authorization_url)
        else:
            _emit_authorization_pending(
                authorization_url,
                json_output=bool(getattr(args, "json", False)),
            )
        code = callback_server.wait_for_code(expected_state=state)
        result = dict(
            host.exchange_authorization_code(
                AuthorizationCodeGrant(
                    scope=args.scope,
                    scopes=_SCOPES[args.scope],
                    profile=args.profile or args.scope,
                    account_subject=account_subject,
                    redirect_uri=callback_server.redirect_uri,
                    code=code,
                    state=state,
                    code_verifier=code_verifier,
                )
            )
        )
        _validate_authorization_result(
            result,
            requested_scope=args.scope,
            expected_scopes=_SCOPES[args.scope],
            expected_account_subject=account_subject,
        )
    finally:
        callback_server.close()
    result.setdefault("ok", True)
    result.setdefault("scope", args.scope)
    return result


class LoopbackCallbackServer:
    def __init__(self, *, timeout_seconds: float) -> None:
        self._httpd = _OAuthHTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self._httpd.timeout = min(1.0, timeout_seconds)
        self._httpd.deadline = time.monotonic() + timeout_seconds
        self._httpd.expected_state = None
        self._httpd.consumed_state = False
        self._httpd.authorization_code = None
        self._httpd.error = None
        self._lock = threading.Lock()

    @property
    def redirect_uri(self) -> str:
        host, port = cast("tuple[str, int]", self._httpd.server_address)
        return f"http://{host}:{port}{CALLBACK_PATH}"

    def wait_for_code(self, *, expected_state: str) -> str:
        with self._lock:
            self._httpd.expected_state = expected_state
            while time.monotonic() < self._httpd.deadline:
                self._httpd.handle_request()
                if self._httpd.authorization_code is not None:
                    return self._httpd.authorization_code
                if self._httpd.error is not None:
                    raise RuntimeError(self._httpd.error)
        raise TimeoutError("timed out waiting for Gmail OAuth callback")

    def close(self) -> None:
        self._httpd.server_close()


class _OAuthHTTPServer(HTTPServer):
    expected_state: str | None
    consumed_state: bool
    authorization_code: str | None
    error: str | None
    deadline: float


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        oauth_server = cast("_OAuthHTTPServer", self.server)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._send_page(404)
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            received_state = _single_query_value(query, "state")
            code = _single_query_value(query, "code")
            provider_error = _single_query_value(query, "error")
        except ValueError:
            oauth_server.error = "OAuth callback query was invalid"
            self._send_page(400)
            return
        if oauth_server.consumed_state:
            oauth_server.error = "OAuth callback state was already used"
            self._send_page(400)
            return
        if (
            received_state is None
            or oauth_server.expected_state is None
            or not secrets.compare_digest(received_state, oauth_server.expected_state)
        ):
            oauth_server.error = "OAuth callback state mismatch"
            self._send_page(400)
            return
        oauth_server.consumed_state = True
        if provider_error is not None:
            oauth_server.error = (
                "OAuth authorization was denied"
                if provider_error == "access_denied"
                else "OAuth provider returned an authorization error"
            )
            self._send_page(400)
            return
        if code is None:
            oauth_server.error = "OAuth callback missing code"
            self._send_page(400)
            return
        oauth_server.authorization_code = code
        self._send_page(200)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_page(self, status: int) -> None:
        body = CALLBACK_HTML.encode("utf-8")
        self.send_response(status)
        for header, value in _callback_response_headers(len(body)):
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)


def create_loopback_callback_server(timeout_seconds: float) -> LoopbackCallbackServer:
    return LoopbackCallbackServer(timeout_seconds=timeout_seconds)


def _callback_response_headers(content_length: int) -> tuple[tuple[str, str], ...]:
    return (
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(content_length)),
        ("Cache-Control", "no-store"),
        ("Pragma", "no-cache"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
    )


def _single_query_value(query: Mapping[str, Sequence[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    if len(values) != 1 or not values[0] or len(values[0]) > 4096:
        raise ValueError("invalid OAuth callback query")
    return values[0]


def _validate_authorization_result(
    result: Mapping[str, object],
    *,
    requested_scope: str,
    expected_scopes: tuple[str, ...],
    expected_account_subject: str,
) -> None:
    if result.get("ok") is not True or result.get("scope") != requested_scope:
        raise RuntimeError("OAuth authorization result mismatch")
    scopes = result.get("scopes")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        raise RuntimeError("OAuth authorization result scope mismatch")
    if tuple(str(item) for item in cast("Sequence[object]", scopes)) != expected_scopes:
        raise RuntimeError("OAuth authorization result scope mismatch")
    if result.get("account_subject") != expected_account_subject:
        raise RuntimeError("OAuth authorization result account mismatch")


def _normalize_account_subject(value: str) -> str:
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 321
        or "@" not in normalized
        or any(char.isspace() for char in normalized)
    ):
        raise ValueError("account_subject must be a bounded email address")
    local, _, domain = normalized.partition("@")
    if not local or not domain:
        raise ValueError("account_subject must be a bounded email address")
    return normalized


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _load_json_object(path: Path) -> Mapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("client json must contain an object")
    return cast("Mapping[str, object]", payload)


def _load_default_host() -> GmailBrokerHostPort:
    for module_name in (
        "careerops.infrastructure.gmail.broker",
        "careerops.infrastructure.gmail.oauth_broker",
    ):
        try:
            module = __import__(module_name, fromlist=["create_gmail_broker_host"])
        except ModuleNotFoundError:
            continue
        factory = getattr(module, "create_gmail_broker_host", None)
        if callable(factory):
            return cast("GmailBrokerHostPort", factory())
    raise RuntimeError("Gmail broker host implementation is unavailable")


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    redacted = cast("Mapping[str, object]", _redact(payload))
    if json_output:
        print(json.dumps(redacted, sort_keys=True, separators=(",", ":")))
        return
    ok = redacted.get("ok", True)
    status = "ok" if ok else "failed"
    scope = redacted.get("scope")
    if scope is None:
        print(f"gmail broker {status}")
    else:
        print(f"gmail broker {status}: scope={scope}")


def _emit_authorization_pending(authorization_url: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "authorization_url": authorization_url,
                    "event": "gmail_oauth_authorization_url",
                    "ok": True,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return
    print(
        "Open this Gmail OAuth authorization URL, then complete consent in the browser:\n"
        f"{authorization_url}",
        file=sys.stderr,
    )


def _emit_error(message: str, *, json_output: bool) -> None:
    safe_message = _redact_string(message)
    if json_output:
        print(json.dumps({"errors": [safe_message], "ok": False}, sort_keys=True), file=sys.stderr)
    else:
        print(f"ERROR: {safe_message}", file=sys.stderr)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, item in cast("Mapping[object, object]", value).items():
            key_text = str(key)
            if _is_raw_handle_key(key_text):
                if isinstance(item, str):
                    redacted[f"{key_text}_sha256"] = hashlib.sha256(
                        item.encode("utf-8")
                    ).hexdigest()
                else:
                    redacted[f"{key_text}_sha256"] = _REDACTED
            elif _is_sensitive_key(key_text):
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = _redact(item)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_scalar(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_raw_handle_key(key: str) -> bool:
    return key.lower() in _RAW_HANDLE_KEYS


def _redact_string(message: str) -> str:
    lowered = message.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "sensitive Gmail broker value redacted"
    if _looks_like_secret(message):
        return "sensitive Gmail broker value redacted"
    return html.escape(message, quote=False)


def _redact_scalar(value: str) -> str:
    if _looks_like_secret(value):
        return _REDACTED
    return value


def _looks_like_secret(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS) or stripped.startswith(
        ("ya29.", "1//", "GOCSPX-", "Bearer ")
    )


def _validate_authorization_url_safe_to_print(authorization_url: str) -> None:
    parsed = urllib.parse.urlparse(authorization_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Gmail authorization URL is invalid")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for raw_key, values in query.items():
        key = raw_key.lower()
        if key in _AUTHORIZATION_URL_FORBIDDEN_QUERY_KEYS:
            raise ValueError("Gmail authorization URL contains a sensitive query parameter")
        if any(_looks_like_authorization_url_secret(value) for value in values):
            raise ValueError("Gmail authorization URL contains a sensitive query value")


def _looks_like_authorization_url_secret(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("ya29.", "1//", "GOCSPX-", "Bearer "))


__all__: Sequence[str] = (
    "CALLBACK_HTML",
    "CALLBACK_PATH",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SEND_SCOPE",
    "AuthorizationCodeGrant",
    "AuthorizationRequest",
    "AuthorizationResult",
    "LoopbackCallbackServer",
    "create_loopback_callback_server",
    "main",
)


if __name__ == "__main__":
    raise SystemExit(main())
