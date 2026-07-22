from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from careerops.application.gmail_send import GmailSendAttachmentRef, GmailSendPayload
from careerops.infrastructure.gmail.send_credentials import GmailSendAccessToken

GMAIL_SEND_API_URL: Final = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_TOKEN = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")
_MAX_RAW_ATTACHMENTS_BYTES: Final = 20 * 1024 * 1024
_MAX_MIME_BYTES: Final = 25 * 1024 * 1024


class GmailSendApiError(RuntimeError):
    def __init__(self, error_code: str, *, status_code: int | None = None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GmailSendResolvedAttachment:
    ref: GmailSendAttachmentRef
    data: bytes

    def __post_init__(self) -> None:
        if len(self.data) != self.ref.size_bytes:
            raise ValueError("resolved attachment size does not match approved ref")
        digest = hashlib.sha256(self.data).hexdigest()
        if digest != self.ref.sha256:
            raise ValueError("resolved attachment sha256 does not match approved ref")

    def __repr__(self) -> str:
        return (
            "GmailSendResolvedAttachment("
            f"content_type={self.ref.content_type!r}, size_bytes={self.ref.size_bytes!r}, "
            f"data_len={len(self.data)!r}, sha256={self.ref.sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class GmailSendReceipt:
    provider_message_id: str
    provider_thread_id: str

    def __post_init__(self) -> None:
        _validate_token(self.provider_message_id, "provider_message_id")
        _validate_token(self.provider_thread_id, "provider_thread_id")


class GmailSendHttpClient:
    """Small Gmail REST client restricted to one reviewed send method."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if max_response_bytes < 1 or max_response_bytes > 1_048_576:
            raise ValueError("max_response_bytes must be between 1 and 1048576")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def send_message(
        self,
        token: GmailSendAccessToken,
        *,
        payload: GmailSendPayload,
        attachments: Sequence[GmailSendResolvedAttachment] = (),
    ) -> GmailSendReceipt:
        if token.is_expired():
            raise GmailSendApiError("GMAIL_SEND_ACCESS_TOKEN_EXPIRED", status_code=401)
        if token.account_subject.casefold() != payload.sender.strip().casefold():
            raise GmailSendApiError("GMAIL_SEND_ACCOUNT_SENDER_MISMATCH", status_code=403)
        _validate_resolved_attachments(payload, attachments)
        mime_bytes = _build_email_message(payload, attachments).as_bytes(policy=SMTP)
        if len(mime_bytes) > _MAX_MIME_BYTES:
            raise GmailSendApiError("GMAIL_SEND_MESSAGE_TOO_LARGE")
        request_body: dict[str, object] = {
            "raw": _base64url_message(mime_bytes),
        }
        if payload.thread_id is not None:
            request_body["threadId"] = payload.thread_id
        response = self._post_json(token, request_body)
        return GmailSendReceipt(
            provider_message_id=_mapping_str(response, "id"),
            provider_thread_id=_mapping_str(response, "threadId"),
        )

    def _post_json(
        self,
        token: GmailSendAccessToken,
        body: Mapping[str, object],
    ) -> Mapping[str, object]:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            GMAIL_SEND_API_URL,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {token.value}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        request_error: GmailSendApiError | None = None
        response_body = b""
        status_code: object = None
        try:
            # Fixed Gmail HTTPS endpoint; the request body is locally constructed and bounded.
            with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310
                response_body = response.read(self._max_response_bytes + 1)
                status_code = getattr(response, "status", None) or response.getcode()
        except HTTPError as exc:
            bounded_status = _bounded_http_status(exc.code)
            request_error = GmailSendApiError(
                f"GMAIL_SEND_HTTP_{bounded_status}"
                if bounded_status is not None
                else "GMAIL_SEND_HTTP_ERROR",
                status_code=bounded_status,
            )
        except URLError:
            request_error = GmailSendApiError("GMAIL_SEND_TRANSPORT_ERROR")
        if request_error is not None:
            raise request_error
        if len(response_body) > self._max_response_bytes:
            raise GmailSendApiError("GMAIL_SEND_RESPONSE_TOO_LARGE")
        bounded_status = _bounded_http_status(status_code)
        if bounded_status is None:
            raise GmailSendApiError("GMAIL_SEND_INVALID_HTTP_STATUS")
        if bounded_status < 200 or bounded_status >= 300:
            raise GmailSendApiError(f"GMAIL_SEND_HTTP_{bounded_status}", status_code=bounded_status)
        parsed: object = {}
        invalid_json = False
        try:
            parsed = json.loads(response_body.decode("utf-8")) if response_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid_json = True
        if invalid_json:
            raise GmailSendApiError("GMAIL_SEND_INVALID_JSON")
        if not isinstance(parsed, dict):
            raise GmailSendApiError("GMAIL_SEND_SCHEMA_MISMATCH")
        return cast("Mapping[str, object]", parsed)


def _build_email_message(
    payload: GmailSendPayload,
    attachments: Sequence[GmailSendResolvedAttachment],
) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    message["From"] = payload.sender
    message["To"] = payload.recipient
    message["Subject"] = payload.subject
    message["Message-ID"] = payload.message_id_header
    if payload.in_reply_to_message_id is not None:
        message["In-Reply-To"] = payload.in_reply_to_message_id
        message["References"] = payload.in_reply_to_message_id
    message.set_content(payload.text_body)
    for attachment in attachments:
        maintype, subtype = attachment.ref.content_type.split("/", 1)
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.ref.filename,
        )
    return message


def _base64url_message(message_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(message_bytes).decode("ascii").rstrip("=")


def _validate_resolved_attachments(
    payload: GmailSendPayload,
    attachments: Sequence[GmailSendResolvedAttachment],
) -> None:
    if tuple(attachment.ref for attachment in attachments) != payload.attachment_refs:
        raise ValueError("resolved attachments must exactly match approved payload refs")
    if sum(attachment.ref.size_bytes for attachment in attachments) > _MAX_RAW_ATTACHMENTS_BYTES:
        raise GmailSendApiError("GMAIL_SEND_ATTACHMENTS_TOO_LARGE")


def _mapping_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GmailSendApiError("GMAIL_SEND_SCHEMA_MISMATCH")
    return value.strip()


def _bounded_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        return None
    return value


def _validate_token(value: str, field_name: str) -> None:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")


__all__ = [
    "GMAIL_SEND_API_URL",
    "GmailSendApiError",
    "GmailSendHttpClient",
    "GmailSendReceipt",
    "GmailSendResolvedAttachment",
]
