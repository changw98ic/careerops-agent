from __future__ import annotations

import ctypes
import hashlib
import json
import re
import secrets
import sys
import threading
import urllib.parse
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Final, Protocol, Self, cast

from careerops.application.gmail_send import GmailSendPayload
from careerops.infrastructure.gmail.broker_server import (
    BrokerServerError,
    CredentialTokenEnvelope,
    GmailCredentialBrokerServer,
    GmailSendAttachmentBrokerServer,
)
from careerops.infrastructure.gmail.client import (
    GmailApiError,
    GmailMetadataMessage,
    GmailReadOnlyHttpClient,
    GmailSentSmokeProof,
)
from careerops.infrastructure.gmail.credentials import GmailAccessToken
from careerops.infrastructure.gmail.send_client import GmailSendHttpClient, GmailSendReceipt
from careerops.infrastructure.gmail.send_credentials import GmailSendAccessToken
from careerops.infrastructure.google_oauth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GoogleAuthorizationSession,
    GoogleInstalledClient,
    GoogleOAuthClient,
    GoogleOAuthTokens,
    HttpRequest,
    HttpTransport,
    scope_for_capability,
    urllib_transport,
)

CLIENT_KEYCHAIN_SERVICE: Final = "careerops.gmail.oauth.client"
CREDENTIAL_KEYCHAIN_SERVICE: Final = "careerops.gmail.oauth.credential"
GMAIL_PROFILE_URI: Final = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

_PROFILES: Final = frozenset(("readonly", "send"))
_PROFILE_CAPABILITIES: Final = {
    "readonly": "gmail_readonly",
    "send": "gmail_send",
}
_CAPABILITY_PROFILES: Final = {value: key for key, value in _PROFILE_CAPABILITIES.items()}
_PROFILE_SCOPES: Final = {
    "readonly": GMAIL_READONLY_SCOPE,
    "send": GMAIL_SEND_SCOPE,
}
_MAX_JSON_BYTES: Final = 65536
_SMOKE_BODY: Final = (
    "CareerOps controlled Gmail OAuth qualification message. "
    "It verifies the reviewed local send path and may be safely deleted."
)
_SMOKE_SUBJECT: Final = re.compile(r"^CareerOps Gmail qualification ([0-9a-f]{16})$")
_RFC_MESSAGE_ID: Final = re.compile(r"^<[^<>\s@]{1,160}@[^<>\s@]{1,160}>$")
_SMOKE_RECOVERY_WINDOW: Final = timedelta(days=1)


class GmailOAuthBrokerError(RuntimeError):
    """Host-side Gmail OAuth broker failure with bounded, non-secret details."""


class SecretStore(Protocol):
    def put(self, account: str, value: str) -> None: ...

    def get(self, account: str) -> str: ...

    def delete(self, account: str) -> None: ...


class _KeychainBackend(Protocol):
    def put(self, service: str, account: str, value: str) -> None: ...

    def get(self, service: str, account: str) -> str: ...

    def delete(self, service: str, account: str) -> None: ...


class _BlockingServer(Protocol):
    def serve_forever(self, *, stop_event: threading.Event | None = None) -> None: ...


class GmailSendClientPort(Protocol):
    def send_message(
        self,
        token: GmailSendAccessToken,
        *,
        payload: GmailSendPayload,
    ) -> GmailSendReceipt: ...


class GmailReadonlyClientPort(Protocol):
    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage: ...

    def find_sent_smoke_message(
        self,
        token: GmailAccessToken,
        *,
        provider_message_id: str,
        provider_thread_id: str,
        sender: str,
        recipient: str,
        subject: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage | None: ...

    def find_recent_sent_smoke_messages(
        self,
        token: GmailAccessToken,
        *,
        account_subject: str,
        not_before: datetime,
        expected_body_sha256: str,
    ) -> tuple[GmailSentSmokeProof, ...]: ...


@dataclass(frozen=True, slots=True)
class GmailOAuthCredentialRecord:
    handle: str
    account_subject: str
    profile: str
    capability: str
    scope: str
    tokens: GoogleOAuthTokens
    active: bool
    account_bound_by_operator: bool = False
    live_qualified: bool = False
    qualification_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_profile(self.profile)
        expected_capability = _PROFILE_CAPABILITIES[self.profile]
        if self.capability != expected_capability:
            raise ValueError("credential capability mismatch")
        expected_scope = scope_for_capability(self.capability)
        if self.scope != expected_scope or self.tokens.scope != expected_scope:
            raise ValueError("credential scope mismatch")
        if self.tokens.capability != self.capability:
            raise ValueError("credential token capability mismatch")
        _validate_account_subject(self.account_subject)
        _validate_handle(self.handle)
        if self.qualification_evidence_sha256 is not None:
            _validate_sha256(self.qualification_evidence_sha256)

    def __repr__(self) -> str:
        return (
            "GmailOAuthCredentialRecord("
            f"account_subject={self.account_subject!r}, "
            f"profile={self.profile!r}, "
            f"capability={self.capability!r}, "
            f"scope={self.scope!r}, "
            f"handle_sha256={_sha256_text(self.handle)!r}, "
            f"active={self.active!r}, "
            f"account_bound_by_operator={self.account_bound_by_operator!r}, "
            f"live_qualified={self.live_qualified!r}, "
            f"qualification_evidence_sha256={self.qualification_evidence_sha256!r}, "
            "tokens=<redacted>)"
        )

    __str__ = __repr__

    def to_json(self) -> str:
        return _dump_json(
            {
                "account_bound_by_operator": self.account_bound_by_operator,
                "account_subject": self.account_subject,
                "active": self.active,
                "capability": self.capability,
                "handle": self.handle,
                "live_qualified": self.live_qualified,
                "qualification_evidence_sha256": self.qualification_evidence_sha256,
                "profile": self.profile,
                "scope": self.scope,
                "tokens": json.loads(self.tokens.to_json()),
            }
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> Self:
        payload = _load_json_object(value, "stored Gmail OAuth credential")
        tokens_value = payload.get("tokens")
        if not isinstance(tokens_value, Mapping):
            raise GmailOAuthBrokerError("stored Gmail OAuth credential is invalid")
        tokens_payload = cast("Mapping[str, object]", tokens_value)
        return cls(
            handle=_required_str(payload, "handle"),
            account_subject=_required_str(payload, "account_subject"),
            profile=_required_str(payload, "profile"),
            capability=_required_str(payload, "capability"),
            scope=_required_str(payload, "scope"),
            tokens=GoogleOAuthTokens.from_json(_dump_json(tokens_payload)),
            active=_required_bool(payload, "active"),
            account_bound_by_operator=_optional_bool(
                payload,
                "account_bound_by_operator",
                default=False,
            ),
            live_qualified=_optional_bool(payload, "live_qualified", default=False),
            qualification_evidence_sha256=_optional_str(
                payload,
                "qualification_evidence_sha256",
            ),
        )

    def public_status(self) -> Mapping[str, object]:
        return {
            "account_bound_by_operator": self.account_bound_by_operator,
            "account_subject": self.account_subject,
            "active": self.active,
            "expires_at": self.tokens.expires_at.isoformat(),
            "handle_sha256": _sha256_text(self.handle),
            "live_qualified": self.live_qualified,
            "qualification_evidence_sha256": self.qualification_evidence_sha256,
            "profile": self.profile,
            "scope": self.scope,
            "scopes": (self.scope,),
        }

    def local_onboarding_status(self) -> Mapping[str, object]:
        return {
            **self.public_status(),
            "handle": self.handle,
        }


class MacOSKeychainSecretStore:
    def __init__(
        self,
        *,
        service: str,
        backend: _KeychainBackend | None = None,
    ) -> None:
        if not service or len(service) > 255 or any(char.isspace() for char in service):
            raise ValueError("keychain service must be bounded and contain no whitespace")
        self._service = service
        self._backend = backend or _MacOSSecurityFrameworkBackend()

    def put(self, account: str, value: str) -> None:
        _validate_slot(account)
        _validate_stored_value(value)
        try:
            self._backend.put(self._service, account, value)
        except _KeychainBackendError as exc:
            raise GmailOAuthBrokerError("macOS Keychain operation failed") from exc

    def get(self, account: str) -> str:
        _validate_slot(account)
        try:
            value = self._backend.get(self._service, account)
            _validate_stored_value(value)
        except (_KeychainBackendError, ValueError) as exc:
            raise GmailOAuthBrokerError("macOS Keychain operation failed") from exc
        return value

    def delete(self, account: str) -> None:
        _validate_slot(account)
        try:
            self._backend.delete(self._service, account)
        except _KeychainBackendError as exc:
            raise GmailOAuthBrokerError("macOS Keychain operation failed") from exc


class _KeychainBackendError(RuntimeError):
    pass


class _MacOSSecurityFrameworkBackend:
    """Use Security.framework so secret bytes never appear in process arguments."""

    _ITEM_NOT_FOUND: Final = -25300

    def __init__(self) -> None:
        self._security: ctypes.CDLL | None = None
        self._core_foundation: ctypes.CDLL | None = None

    def put(self, service: str, account: str, value: str) -> None:
        security, core_foundation = self._frameworks()
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        value_bytes = value.encode("utf-8")
        item = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            None,
            None,
            ctypes.byref(item),
        )
        if status == self._ITEM_NOT_FOUND:
            value_buffer = ctypes.create_string_buffer(value_bytes)
            status = security.SecKeychainAddGenericPassword(
                None,
                len(service_bytes),
                service_bytes,
                len(account_bytes),
                account_bytes,
                len(value_bytes),
                ctypes.cast(value_buffer, ctypes.c_void_p),
                ctypes.byref(item),
            )
        elif status == 0:
            value_buffer = ctypes.create_string_buffer(value_bytes)
            status = security.SecKeychainItemModifyAttributesAndData(
                item,
                None,
                len(value_bytes),
                ctypes.cast(value_buffer, ctypes.c_void_p),
            )
        try:
            self._require_success(status)
        finally:
            if item.value:
                core_foundation.CFRelease(item)

    def get(self, service: str, account: str) -> str:
        security, core_foundation = self._frameworks()
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        self._require_success(status)
        try:
            value_bytes = ctypes.string_at(password_data, password_length.value)
            return value_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _KeychainBackendError("Keychain value is not UTF-8") from exc
        finally:
            if password_data.value:
                security.SecKeychainItemFreeContent(None, password_data)
            if item.value:
                core_foundation.CFRelease(item)

    def delete(self, service: str, account: str) -> None:
        security, core_foundation = self._frameworks()
        service_bytes = service.encode("utf-8")
        account_bytes = account.encode("utf-8")
        item = ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            None,
            None,
            ctypes.byref(item),
        )
        if status == self._ITEM_NOT_FOUND:
            return
        try:
            self._require_success(status)
            self._require_success(security.SecKeychainItemDelete(item))
        finally:
            if item.value:
                core_foundation.CFRelease(item)

    def _frameworks(self) -> tuple[ctypes.CDLL, ctypes.CDLL]:
        if sys.platform != "darwin":
            raise _KeychainBackendError("macOS Keychain is unavailable")
        if self._security is None or self._core_foundation is None:
            try:
                security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
                core_foundation = ctypes.CDLL(
                    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
                )
            except OSError as exc:
                raise _KeychainBackendError("macOS Keychain is unavailable") from exc
            self._configure_functions(security, core_foundation)
            self._security = security
            self._core_foundation = core_foundation
        return self._security, self._core_foundation

    @staticmethod
    def _configure_functions(
        security: ctypes.CDLL,
        core_foundation: ctypes.CDLL,
    ) -> None:
        security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        security.SecKeychainItemDelete.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_foundation.CFRelease.restype = None

    @staticmethod
    def _require_success(status: int) -> None:
        if status != 0:
            raise _KeychainBackendError("macOS Keychain operation failed")


class InMemorySecretStore:
    def __init__(self) -> None:
        self._values: MutableMapping[str, str] = {}

    def put(self, account: str, value: str) -> None:
        _validate_slot(account)
        _validate_stored_value(value)
        self._values[account] = value

    def get(self, account: str) -> str:
        _validate_slot(account)
        try:
            return self._values[account]
        except KeyError as exc:
            raise GmailOAuthBrokerError("secret slot is not configured") from exc

    def delete(self, account: str) -> None:
        _validate_slot(account)
        self._values.pop(account, None)


class KeychainGmailOAuthBrokerHost:
    def __init__(
        self,
        *,
        client_store: SecretStore | None = None,
        credential_store: SecretStore | None = None,
        transport: HttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
        token_urlsafe: Callable[[int], str] | None = None,
        send_client: GmailSendClientPort | None = None,
        readonly_client: GmailReadonlyClientPort | None = None,
    ) -> None:
        self._client_store = client_store or MacOSKeychainSecretStore(
            service=CLIENT_KEYCHAIN_SERVICE
        )
        self._credential_store = credential_store or MacOSKeychainSecretStore(
            service=CREDENTIAL_KEYCHAIN_SERVICE
        )
        self._transport = transport or urllib_transport
        self._now = now or (lambda: datetime.now(UTC))
        self._token_urlsafe = token_urlsafe or __import__("secrets").token_urlsafe
        self._send_client = send_client or cast("GmailSendClientPort", GmailSendHttpClient())
        self._readonly_client = readonly_client or cast(
            "GmailReadonlyClientPort",
            GmailReadOnlyHttpClient(),
        )

    def import_client(
        self,
        payload: Mapping[str, object],
        *,
        label: str | None = None,
    ) -> Mapping[str, object]:
        profile = _profile_from_label_or_payload(label, payload)
        raw_json = _dump_json(payload)
        GoogleInstalledClient.from_json(raw_json)
        self._client_store.put(profile, raw_json)
        return {"client_configured": True, "ok": True, "profile": profile, "scope": profile}

    def build_authorization_url(self, request: object) -> str:
        profile = _profile_from_request(request)
        if tuple(_request_scopes(request)) != (_PROFILE_SCOPES[profile],):
            raise ValueError("Gmail authorization request must contain exactly one scope")
        session = self._oauth(profile).authorization_url(
            capability=_PROFILE_CAPABILITIES[profile],
            redirect_uri=_request_str(request, "redirect_uri"),
            state=_request_str(request, "state"),
            code_verifier=_request_str(request, "code_verifier"),
            login_hint=_request_str(request, "account_subject"),
        )
        generated = urllib.parse.parse_qs(urllib.parse.urlparse(session.authorization_url).query)
        challenge = generated.get("code_challenge", [""])[0]
        if not secrets.compare_digest(challenge, _request_str(request, "code_challenge")):
            raise ValueError("Gmail authorization PKCE challenge mismatch")
        return session.authorization_url

    def exchange_authorization_code(self, grant: object) -> Mapping[str, object]:
        profile = _profile_from_request(grant)
        if tuple(_request_scopes(grant)) != (_PROFILE_SCOPES[profile],):
            raise ValueError("Gmail authorization grant must contain exactly one scope")
        oauth = self._oauth(profile)
        session = GoogleAuthorizationSession(
            authorization_url="https://accounts.google.com/o/oauth2/v2/auth?<redacted>",
            state=_request_str(grant, "state"),
            code_verifier=_request_str(grant, "code_verifier"),
            capability=_PROFILE_CAPABILITIES[profile],
            scope=_PROFILE_SCOPES[profile],
            redirect_uri=_request_str(grant, "redirect_uri"),
        )
        tokens = oauth.exchange_authorization_code(
            code=_request_str(grant, "code"),
            session=session,
        )
        expected_account = _optional_account_subject(grant)
        if profile == "readonly":
            account_subject = self._gmail_profile_email(tokens)
            if expected_account is not None and account_subject != expected_account:
                raise GmailOAuthBrokerError(
                    "authorized Gmail account did not match expected account"
                )
            active = True
            account_bound_by_operator = False
        else:
            if expected_account is None:
                raise ValueError("send authorization requires expected_account_subject")
            account_subject = expected_account
            active = True
            account_bound_by_operator = True
        record = GmailOAuthCredentialRecord(
            handle=f"gmail:{profile}:{self._token_urlsafe(24)}",
            account_subject=account_subject,
            profile=profile,
            capability=_PROFILE_CAPABILITIES[profile],
            scope=_PROFILE_SCOPES[profile],
            tokens=tokens,
            active=active,
            account_bound_by_operator=account_bound_by_operator,
            live_qualified=profile == "readonly",
            qualification_evidence_sha256=None,
        )
        self._credential_store.put(profile, record.to_json())
        return {
            "account_bound_by_operator": record.account_bound_by_operator,
            "account_subject": record.account_subject,
            "active": record.active,
            "handle_sha256": _sha256_text(record.handle),
            "live_qualified": record.live_qualified,
            "ok": True,
            "scope": profile,
            "scopes": (record.scope,),
        }

    def serve(
        self,
        *,
        readonly_socket: Path | None = None,
        send_socket: Path | None = None,
        attachment_socket: Path | None = None,
        attachment_root: Path | None = None,
    ) -> Mapping[str, object] | None:
        if attachment_socket is not None and attachment_root is None:
            raise RuntimeError("attachment broker serve requires attachment_root")
        servers: list[tuple[str, _BlockingServer]] = []
        if readonly_socket is not None:
            servers.append(
                (
                    "readonly",
                    GmailCredentialBrokerServer(
                        socket_path=readonly_socket,
                        token_provider=self,
                        now=self._now,
                    ),
                )
            )
        if send_socket is not None:
            servers.append(
                (
                    "send",
                    GmailCredentialBrokerServer(
                        socket_path=send_socket,
                        token_provider=self,
                        now=self._now,
                    ),
                )
            )
        if attachment_socket is not None and attachment_root is not None:
            servers.append(
                (
                    "attachment",
                    GmailSendAttachmentBrokerServer(
                        socket_path=attachment_socket,
                        attachment_root=attachment_root,
                    ),
                )
            )
        if not servers:
            return {"ok": True, "served": ()}
        stop_event = threading.Event()
        threads: list[threading.Thread] = []
        for _, server in servers[1:]:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"stop_event": stop_event},
                daemon=False,
            )
            thread.start()
            threads.append(thread)
        try:
            servers[0][1].serve_forever(stop_event=stop_event)
        finally:
            stop_event.set()
            for thread in threads:
                thread.join(timeout=2)
        return None

    def status(self) -> Mapping[str, object]:
        clients = {
            profile: self._slot_configured(self._client_store, profile)
            for profile in sorted(_PROFILES)
        }
        credentials: dict[str, object] = {}
        for profile in sorted(_PROFILES):
            try:
                credentials[profile] = self._credential_record(profile).public_status()
            except GmailOAuthBrokerError:
                credentials[profile] = {"configured": False, "profile": profile}
        return {"clients": clients, "credentials": credentials, "ok": True}

    def local_onboarding_status(self) -> Mapping[str, object]:
        clients = {
            profile: self._slot_configured(self._client_store, profile)
            for profile in sorted(_PROFILES)
        }
        credentials: dict[str, object] = {}
        for profile in sorted(_PROFILES):
            try:
                credentials[profile] = self._credential_record(profile).local_onboarding_status()
            except GmailOAuthBrokerError:
                credentials[profile] = {"configured": False, "profile": profile}
        return {"clients": clients, "credentials": credentials, "ok": True}

    def smoke_send(self, *, account_subject: str) -> Mapping[str, object]:
        """Prove the send-only grant belongs to the read-only Gmail mailbox.

        Gmail's send-only scope cannot read the current profile.  The broker therefore
        sends one controlled message to a plus-address of the expected account and
        retrieves the resulting Sent message through the separately authorized
        read-only grant.  Qualification is persisted only after all immutable headers
        and provider identifiers reconcile.
        """
        account = _normalize_account_subject(account_subject)
        readonly_record = self._credential_record("readonly")
        send_record = self._credential_record("send")
        if readonly_record.account_subject != account or send_record.account_subject != account:
            raise GmailOAuthBrokerError(
                "Gmail live qualification requires matching readonly and send accounts"
            )
        if not readonly_record.active or not send_record.active:
            raise GmailOAuthBrokerError(
                "Gmail live qualification requires active readonly and send credentials"
            )
        if send_record.live_qualified and send_record.qualification_evidence_sha256 is not None:
            return {
                "already_qualified": True,
                "evidence_sha256": send_record.qualification_evidence_sha256,
                "live_qualified": True,
                "ok": True,
                "scope": "send",
                "scopes": (GMAIL_SEND_SCOPE,),
            }

        readonly_envelope = self.issue_token(
            opaque_handle=readonly_record.handle,
            account_subject=account,
            scope=GMAIL_READONLY_SCOPE,
            action=None,
        )
        send_envelope = self._issue_token(
            opaque_handle=send_record.handle,
            account_subject=account,
            scope=GMAIL_SEND_SCOPE,
            action="send_email",
            allow_unqualified_send=True,
        )
        readonly_token = GmailAccessToken(
            access_token=readonly_envelope.access_token,
            account_subject=account,
            granted_scopes=(GMAIL_READONLY_SCOPE,),
            expires_at=readonly_envelope.expires_at,
        )
        send_token = GmailSendAccessToken(
            access_token=send_envelope.access_token,
            account_subject=account,
            granted_scopes=(GMAIL_SEND_SCOPE,),
            expires_at=send_envelope.expires_at,
        )

        nonce = hashlib.sha256(self._token_urlsafe(24).encode("utf-8")).hexdigest()[:16]
        payload = _smoke_payload(account, nonce)
        receipt = self._send_client.send_message(send_token, payload=payload)
        verification_path = "provider_id_get"
        try:
            message = self._readonly_client.get_message(
                readonly_token,
                message_id=receipt.provider_message_id,
                metadata_headers=("From", "To", "Subject", "Message-ID"),
            )
        except GmailApiError as exc:
            if exc.error_code not in {"GMAIL_TRANSPORT_ERROR", "GMAIL_HTTP_404"}:
                raise GmailOAuthBrokerError(
                    "Gmail live qualification evidence could not be read"
                ) from None
            try:
                fallback_message = self._readonly_client.find_sent_smoke_message(
                    readonly_token,
                    provider_message_id=receipt.provider_message_id,
                    provider_thread_id=receipt.provider_thread_id,
                    sender=account,
                    recipient=payload.recipient,
                    subject=payload.subject,
                    metadata_headers=("From", "To", "Subject", "Message-ID"),
                )
            except (GmailApiError, ValueError):
                raise GmailOAuthBrokerError(
                    "Gmail live qualification evidence did not match the controlled message"
                ) from None
            if fallback_message is None:
                raise GmailOAuthBrokerError(
                    "Gmail live qualification evidence did not match the controlled message"
                ) from None
            message = fallback_message
            verification_path = "sent_search_after_get_failure"

        return self._persist_smoke_qualification(
            account=account,
            send_record=send_record,
            message=message,
            expected_recipient=payload.recipient,
            expected_subject=payload.subject,
            expected_body_sha256=payload.body_sha256,
            generated_message_id=payload.message_id_header,
            receipt=receipt,
            verification_path=verification_path,
        )

    def recover_smoke_send(
        self,
        *,
        account_subject: str,
        expected_subject_sha256: str | None = None,
    ) -> Mapping[str, object]:
        """Qualify from the one existing controlled message; this never sends mail."""
        account = _normalize_account_subject(account_subject)
        if expected_subject_sha256 is not None:
            _validate_sha256(expected_subject_sha256)
        readonly_record = self._credential_record("readonly")
        send_record = self._credential_record("send")
        if readonly_record.account_subject != account or send_record.account_subject != account:
            raise GmailOAuthBrokerError(
                "Gmail live qualification requires matching readonly and send accounts"
            )
        if not readonly_record.active or not send_record.active:
            raise GmailOAuthBrokerError(
                "Gmail live qualification requires active readonly and send credentials"
            )
        if send_record.live_qualified and send_record.qualification_evidence_sha256 is not None:
            return {
                "already_qualified": True,
                "evidence_sha256": send_record.qualification_evidence_sha256,
                "live_qualified": True,
                "ok": True,
                "scope": "send",
                "scopes": (GMAIL_SEND_SCOPE,),
            }

        readonly_envelope = self.issue_token(
            opaque_handle=readonly_record.handle,
            account_subject=account,
            scope=GMAIL_READONLY_SCOPE,
            action=None,
        )
        readonly_token = GmailAccessToken(
            access_token=readonly_envelope.access_token,
            account_subject=account,
            granted_scopes=(GMAIL_READONLY_SCOPE,),
            expires_at=readonly_envelope.expires_at,
        )
        try:
            proofs = self._readonly_client.find_recent_sent_smoke_messages(
                readonly_token,
                account_subject=account,
                not_before=self._now().astimezone(UTC) - _SMOKE_RECOVERY_WINDOW,
                expected_body_sha256=_sha256_text(_SMOKE_BODY),
            )
        except (GmailApiError, ValueError):
            raise GmailOAuthBrokerError(
                "Gmail live qualification recovery evidence could not be read"
            ) from None
        verification_path = "unique_recent_exact_raw_sent_message"
        if expected_subject_sha256 is not None:
            proofs = tuple(
                proof
                for proof in proofs
                if _sha256_text(_message_header(proof.message.headers, "subject"))
                == expected_subject_sha256
            )
            verification_path = "expected_subject_exact_raw_sent_message"
        if len(proofs) != 1:
            raise GmailOAuthBrokerError(
                "Gmail live qualification recovery requires one exact recent Sent message"
            )
        proof = proofs[0]
        subject = _message_header(proof.message.headers, "subject")
        match = _SMOKE_SUBJECT.fullmatch(subject)
        if match is None:
            raise GmailOAuthBrokerError(
                "Gmail live qualification evidence did not match the controlled message"
            )
        local_part, domain = account.split("@", 1)
        recipient = f"{local_part}+careerops-smoke-{match.group(1)}@{domain}"
        return self._persist_smoke_qualification(
            account=account,
            send_record=send_record,
            message=proof.message,
            expected_recipient=recipient,
            expected_subject=subject,
            expected_body_sha256=proof.body_sha256,
            generated_message_id=None,
            receipt=None,
            verification_path=verification_path,
        )

    def _persist_smoke_qualification(
        self,
        *,
        account: str,
        send_record: GmailOAuthCredentialRecord,
        message: GmailMetadataMessage,
        expected_recipient: str,
        expected_subject: str,
        expected_body_sha256: str,
        generated_message_id: str | None,
        receipt: GmailSendReceipt | None,
        verification_path: str,
    ) -> Mapping[str, object]:
        actual_from = _message_header(message.headers, "from")
        actual_to = _message_header(message.headers, "to")
        actual_subject = _message_header(message.headers, "subject")
        actual_message_id = _message_header(message.headers, "message-id")
        identifiers_match = receipt is None or (
            message.id == receipt.provider_message_id
            and message.thread_id == receipt.provider_thread_id
        )
        if (
            "SENT" not in message.label_ids
            or not identifiers_match
            or _header_address(actual_from) != account
            or _header_address(actual_to) != expected_recipient
            or actual_subject != expected_subject
            or _RFC_MESSAGE_ID.fullmatch(actual_message_id) is None
        ):
            raise GmailOAuthBrokerError(
                "Gmail live qualification evidence did not match the controlled message"
            )

        qualified_at = self._now().astimezone(UTC).isoformat()
        evidence_payload: Mapping[str, object] = {
            "account_subject": account,
            "body_sha256": expected_body_sha256,
            "generated_message_id_sha256": (
                _sha256_text(generated_message_id) if generated_message_id is not None else None
            ),
            "provider_message_id": message.id,
            "provider_message_id_header_sha256": _sha256_text(actual_message_id),
            "provider_thread_id": message.thread_id,
            "qualified_at": qualified_at,
            "recipient_sha256": _sha256_text(expected_recipient),
            "subject_sha256": _sha256_text(expected_subject),
            "verification_path": verification_path,
            "verified_via_readonly": True,
            "version": "careerops.gmail-send-live-qualification.v2",
        }
        evidence_sha256 = _canonical_sha256(evidence_payload)

        current_send_record = self._credential_record("send")
        if (
            current_send_record.handle != send_record.handle
            or current_send_record.account_subject != account
            or not current_send_record.active
        ):
            raise GmailOAuthBrokerError("Gmail send credential changed during live qualification")
        qualified_record = GmailOAuthCredentialRecord(
            handle=current_send_record.handle,
            account_subject=current_send_record.account_subject,
            profile=current_send_record.profile,
            capability=current_send_record.capability,
            scope=current_send_record.scope,
            tokens=current_send_record.tokens,
            active=current_send_record.active,
            account_bound_by_operator=current_send_record.account_bound_by_operator,
            live_qualified=True,
            qualification_evidence_sha256=evidence_sha256,
        )
        self._credential_store.put("send", qualified_record.to_json())
        return {
            "account_subject": account,
            "evidence_sha256": evidence_sha256,
            "live_qualified": True,
            "ok": True,
            "provider_message_id_sha256": _sha256_text(message.id),
            "provider_thread_id_sha256": _sha256_text(message.thread_id),
            "qualified_at": qualified_at,
            "recovered_without_send": verification_path
            in {
                "unique_recent_exact_raw_sent_message",
                "expected_subject_exact_raw_sent_message",
            },
            "scope": "send",
            "scopes": (GMAIL_SEND_SCOPE,),
            "verification_path": verification_path,
        }

    def revoke(
        self,
        *,
        scope: str | None = None,
        account_subject: str | None = None,
    ) -> Mapping[str, object]:
        if scope is None:
            raise ValueError("revoke requires scope")
        if account_subject is None:
            raise ValueError("revoke requires account_subject")
        profiles = (_normalize_profile(scope),)
        account = _normalize_account_subject(account_subject)
        revoked: list[Mapping[str, object]] = []
        for profile in profiles:
            try:
                record = self._credential_record(profile)
            except GmailOAuthBrokerError:
                continue
            if record.account_subject != account:
                continue
            self._oauth(profile).revoke(record.tokens.refresh_token)
            self._credential_store.delete(profile)
            revoked.append(
                {
                    "account_subject": record.account_subject,
                    "handle_sha256": _sha256_text(record.handle),
                    "scope": profile,
                }
            )
        return {"ok": True, "revoked": tuple(revoked)}

    def issue_token(
        self,
        *,
        opaque_handle: str,
        account_subject: str,
        scope: str,
        action: str | None,
    ) -> CredentialTokenEnvelope:
        return self._issue_token(
            opaque_handle=opaque_handle,
            account_subject=account_subject,
            scope=scope,
            action=action,
            allow_unqualified_send=False,
        )

    def _issue_token(
        self,
        *,
        opaque_handle: str,
        account_subject: str,
        scope: str,
        action: str | None,
        allow_unqualified_send: bool,
    ) -> CredentialTokenEnvelope:
        profile = _profile_from_scope(scope)
        record = self._credential_record(profile)
        if not record.active:
            raise BrokerServerError("credential inactive")
        if record.handle != opaque_handle or record.account_subject != account_subject:
            raise BrokerServerError("credential not found")
        if profile == "readonly" and action is not None:
            raise BrokerServerError("credential action mismatch")
        if profile == "send" and action != "send_email":
            raise BrokerServerError("credential action mismatch")
        if (
            profile == "send"
            and not allow_unqualified_send
            and (not record.live_qualified or record.qualification_evidence_sha256 is None)
        ):
            raise BrokerServerError("credential not live qualified")
        tokens = record.tokens
        current = self._now()
        if tokens.expires_at <= current + timedelta(seconds=60):
            tokens = self._oauth(profile).refresh(tokens)
            record = GmailOAuthCredentialRecord(
                handle=record.handle,
                account_subject=record.account_subject,
                profile=record.profile,
                capability=record.capability,
                scope=record.scope,
                tokens=tokens,
                active=record.active,
                account_bound_by_operator=record.account_bound_by_operator,
                live_qualified=record.live_qualified,
                qualification_evidence_sha256=record.qualification_evidence_sha256,
            )
            self._credential_store.put(profile, record.to_json())
        return CredentialTokenEnvelope(
            access_token=tokens.access_token,
            account_subject=record.account_subject,
            scope=record.scope,
            expires_at=tokens.expires_at,
        )

    def _oauth(self, profile: str) -> GoogleOAuthClient:
        return GoogleOAuthClient(
            installed_client=self._installed_client(profile),
            transport=self._transport,
            now=self._now,
        )

    def _installed_client(self, profile: str) -> GoogleInstalledClient:
        _validate_profile(profile)
        return GoogleInstalledClient.from_json(self._client_store.get(profile))

    def _credential_record(self, profile: str) -> GmailOAuthCredentialRecord:
        _validate_profile(profile)
        return GmailOAuthCredentialRecord.from_json(self._credential_store.get(profile))

    def _gmail_profile_email(self, tokens: GoogleOAuthTokens) -> str:
        response = self._transport(
            HttpRequest(
                method="GET",
                url=GMAIL_PROFILE_URI,
                headers={"Authorization": f"Bearer {tokens.access_token}"},
                body=b"",
            )
        )
        if response.status < 200 or response.status >= 300:
            raise GmailOAuthBrokerError(
                f"Gmail profile endpoint rejected request: status {response.status}"
            )
        payload = _load_json_object(response.body, "Gmail profile response")
        return _normalize_account_subject(_required_str(payload, "emailAddress"))

    @staticmethod
    def _slot_configured(store: SecretStore, profile: str) -> bool:
        try:
            store.get(profile)
        except GmailOAuthBrokerError:
            return False
        return True


def create_gmail_broker_host() -> KeychainGmailOAuthBrokerHost:
    return KeychainGmailOAuthBrokerHost()


def _profile_from_label_or_payload(label: str | None, payload: Mapping[str, object]) -> str:
    del payload
    if label is None:
        raise ValueError("client label must be readonly or send")
    return _normalize_profile(label)


def _profile_from_request(value: object) -> str:
    scope_profile = _profile_from_scope(_request_scope(value))
    raw_profile = getattr(value, "profile", scope_profile)
    if not isinstance(raw_profile, str):
        raise ValueError("Gmail authorization profile must be readonly or send")
    profile = _normalize_profile(raw_profile)
    if profile != scope_profile:
        raise ValueError("Gmail authorization profile must match scope")
    return profile


def _profile_from_scope(scope: str) -> str:
    if scope in _PROFILES:
        return scope
    for profile, profile_scope in _PROFILE_SCOPES.items():
        if scope == profile_scope:
            return profile
    raise ValueError("unsupported Gmail OAuth scope")


def _normalize_profile(value: str) -> str:
    normalized = value.strip().lower()
    _validate_profile(normalized)
    return normalized


def _validate_profile(profile: str) -> None:
    if profile not in _PROFILES:
        raise ValueError("profile must be readonly or send")


def _request_scope(value: object) -> str:
    return _request_str(value, "scope")


def _request_scopes(value: object) -> tuple[str, ...]:
    scopes = getattr(value, "scopes", None)
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        raise ValueError("Gmail authorization request must contain exactly one scope")
    return tuple(str(scope) for scope in cast("Sequence[object]", scopes))


def _request_str(value: object, field_name: str, *, default: str | None = None) -> str:
    item = getattr(value, field_name, default)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"Gmail authorization request missing {field_name}")
    return item.strip()


def _optional_account_subject(value: object) -> str | None:
    for field_name in ("expected_account_subject", "account_subject"):
        item = getattr(value, field_name, None)
        if item is None:
            continue
        if not isinstance(item, str):
            raise ValueError("expected_account_subject must be a bounded email address")
        return _normalize_account_subject(item)
    return None


def _normalize_account_subject(value: str) -> str:
    normalized = value.strip().lower()
    _validate_account_subject(normalized)
    return normalized


def _validate_account_subject(value: str) -> None:
    if not value or len(value) > 321 or "@" not in value or any(char.isspace() for char in value):
        raise ValueError("account_subject must be a bounded email address")
    local, _, domain = value.partition("@")
    if not local or not domain:
        raise ValueError("account_subject must be a bounded email address")


def _validate_handle(value: str) -> None:
    if not 1 <= len(value) <= 255:
        raise ValueError("handle must be bounded")
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@/-"
    if any(char not in allowed for char in value):
        raise ValueError("handle must be bounded")


def _validate_slot(value: str) -> None:
    _validate_profile(value)


def _validate_stored_value(value: str) -> None:
    if not value or len(value.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("stored Gmail OAuth value must be bounded")


def _load_json_object(value: str | bytes, description: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailOAuthBrokerError(f"{description} json is invalid") from exc
    if not isinstance(decoded, dict):
        raise GmailOAuthBrokerError(f"{description} must be an object")
    return cast("Mapping[str, object]", decoded)


def _dump_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    _validate_stored_value(encoded)
    return encoded


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GmailOAuthBrokerError(f"Gmail OAuth payload missing {field_name}")
    return value.strip()


def _required_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise GmailOAuthBrokerError(f"Gmail OAuth payload missing {field_name}")
    return value


def _optional_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name, default)
    if not isinstance(value, bool):
        raise GmailOAuthBrokerError(f"Gmail OAuth payload invalid {field_name}")
    return value


def _optional_str(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GmailOAuthBrokerError(f"Gmail OAuth payload invalid {field_name}")
    return value.strip()


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("qualification evidence must be a lowercase sha256 digest")


def _message_header(headers: Mapping[str, str], name: str) -> str:
    expected_name = name.casefold()
    for header_name, value in headers.items():
        if header_name.casefold() == expected_name:
            return value.strip()
    return ""


def _header_address(value: str) -> str:
    _, parsed = parseaddr(value)
    if not parsed:
        return ""
    try:
        return _normalize_account_subject(parsed)
    except ValueError:
        return ""


def _smoke_payload(account: str, nonce: str) -> GmailSendPayload:
    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise ValueError("Gmail qualification nonce must be 16 lowercase hex characters")
    local_part, domain = account.split("@", 1)
    return GmailSendPayload(
        sender=account,
        recipient=f"{local_part}+careerops-smoke-{nonce}@{domain}",
        subject=f"CareerOps Gmail qualification {nonce}",
        text_body=_SMOKE_BODY,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__: Sequence[str] = (
    "CLIENT_KEYCHAIN_SERVICE",
    "CREDENTIAL_KEYCHAIN_SERVICE",
    "GMAIL_PROFILE_URI",
    "GmailOAuthBrokerError",
    "GmailOAuthCredentialRecord",
    "InMemorySecretStore",
    "KeychainGmailOAuthBrokerHost",
    "MacOSKeychainSecretStore",
    "create_gmail_broker_host",
)
