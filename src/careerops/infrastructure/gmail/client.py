from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Final, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from careerops.infrastructure.gmail.credentials import GmailAccessToken

GMAIL_API_BASE_URL: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")
_HISTORY_ID = re.compile(r"^[0-9]{1,40}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_LABEL_ID = re.compile(r"^[A-Za-z0-9_:-]{1,80}$")
_RFC_MESSAGE_ID = re.compile(r"^<[^<>\s@]{1,160}@[^<>\s@]{1,160}>$")
_SEARCH_QUERY = re.compile(r"^rfc822msgid:<[^<>\s@]{1,160}@[^<>\s@]{1,160}>$")
_SMOKE_SUBJECT = re.compile(r"^CareerOps Gmail qualification ([0-9a-f]{16})$")
_SMOKE_SEARCH_QUERY: Final = 'in:sent subject:"CareerOps Gmail qualification" newer_than:1d'


class GmailApiError(RuntimeError):
    def __init__(self, error_code: str, *, status_code: int | None = None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


class GmailHistoryCursorExpired(GmailApiError):
    """Gmail rejected startHistoryId; caller must schedule an explicit full sync."""


def _require_email_message(value: Message) -> EmailMessage:
    if not isinstance(value, EmailMessage):
        raise GmailApiError("GMAIL_INVALID_RAW_MESSAGE")
    return value


@dataclass(frozen=True, slots=True)
class GmailListedMessage:
    id: str
    thread_id: str

    def __post_init__(self) -> None:
        _validate_token(self.id, "message_id")
        _validate_token(self.thread_id, "thread_id")


@dataclass(frozen=True, slots=True)
class GmailMessageListPage:
    messages: tuple[GmailListedMessage, ...]
    next_page_token: str | None = None


@dataclass(frozen=True, slots=True)
class GmailMetadataMessage:
    id: str
    thread_id: str
    history_id: str
    headers: Mapping[str, str]
    label_ids: tuple[str, ...]
    snippet: str
    internal_date: datetime | None

    def __repr__(self) -> str:
        return (
            "GmailMetadataMessage("
            f"id={self.id!r}, thread_id={self.thread_id!r}, history_id={self.history_id!r}, "
            f"header_names={tuple(sorted(self.headers))!r}, label_ids={self.label_ids!r}, "
            f"internal_date={self.internal_date!r})"
        )


@dataclass(frozen=True, slots=True)
class GmailSentSmokeProof:
    message: GmailMetadataMessage
    body_sha256: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.body_sha256) is None:
            raise ValueError("body_sha256 must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class GmailHistoryPage:
    history: tuple[Mapping[str, object], ...]
    next_page_token: str | None
    checkpoint_history_id: str | None


@dataclass(frozen=True, slots=True)
class GmailProfile:
    email_address: str
    messages_total: int
    threads_total: int
    history_id: str


class GmailReadOnlyHttpClient:
    """Small Gmail REST client restricted to read-only mailbox discovery methods."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("timeout_seconds must be between 0 and 60")
        if max_response_bytes < 1 or max_response_bytes > 10_485_760:
            raise ValueError("max_response_bytes must be between 1 and 10485760")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    def list_messages(
        self,
        token: GmailAccessToken,
        *,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> GmailMessageListPage:
        _validate_max_results(max_results)
        params: list[tuple[str, str | int]] = [("maxResults", max_results)]
        if page_token is not None:
            _validate_token(page_token, "page_token")
            params.append(("pageToken", page_token))
        payload = self._get_json(token, "/messages", params=params)
        raw_messages_obj = payload.get("messages", [])
        if not isinstance(raw_messages_obj, list):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        raw_messages = cast("list[object]", raw_messages_obj)
        messages = tuple(
            GmailListedMessage(
                id=_mapping_str(cast("Mapping[str, object]", message), "id"),
                thread_id=_mapping_str(cast("Mapping[str, object]", message), "threadId"),
            )
            for message in raw_messages
            if isinstance(message, Mapping)
        )
        return GmailMessageListPage(
            messages=messages,
            next_page_token=_optional_str(payload, "nextPageToken"),
        )

    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage:
        _validate_token(message_id, "message_id")
        if not metadata_headers:
            raise ValueError("metadata_headers must not be empty")
        params: list[tuple[str, str]] = [("format", "METADATA")]
        for header in metadata_headers:
            _validate_header_name(header)
            params.append(("metadataHeaders", header))
        payload = self._get_json(token, f"/messages/{message_id}", params=params)
        headers = _extract_headers(payload)
        label_ids_obj = payload.get("labelIds", [])
        label_ids = cast("list[object]", label_ids_obj) if isinstance(label_ids_obj, list) else []
        return GmailMetadataMessage(
            id=_mapping_str(payload, "id"),
            thread_id=_mapping_str(payload, "threadId"),
            history_id=_mapping_str(payload, "historyId"),
            headers=headers,
            label_ids=tuple(str(item) for item in label_ids),
            snippet=_optional_str(payload, "snippet") or "",
            internal_date=_parse_internal_date(_optional_str(payload, "internalDate")),
        )

    def list_history(
        self,
        token: GmailAccessToken,
        *,
        start_history_id: str,
        page_token: str | None = None,
        max_results: int = 100,
    ) -> GmailHistoryPage:
        _validate_history_id(start_history_id)
        _validate_max_results(max_results)
        params: list[tuple[str, str | int]] = [
            ("startHistoryId", start_history_id),
            ("maxResults", max_results),
        ]
        if page_token is not None:
            _validate_token(page_token, "page_token")
            params.append(("pageToken", page_token))
        try:
            payload = self._get_json(token, "/history", params=params)
        except GmailApiError as exc:
            if exc.status_code == 404:
                raise GmailHistoryCursorExpired(
                    "GMAIL_HISTORY_CURSOR_EXPIRED",
                    status_code=404,
                ) from exc
            raise
        raw_history_obj = payload.get("history", [])
        if not isinstance(raw_history_obj, list):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        raw_history = cast("list[object]", raw_history_obj)
        next_page_token = _optional_str(payload, "nextPageToken")
        history_id = _optional_str(payload, "historyId")
        checkpoint = history_id if next_page_token is None else None
        return GmailHistoryPage(
            history=tuple(
                cast("Mapping[str, object]", item)
                for item in raw_history
                if isinstance(item, Mapping)
            ),
            next_page_token=next_page_token,
            checkpoint_history_id=checkpoint,
        )

    def get_profile(self, token: GmailAccessToken) -> GmailProfile:
        payload = self._get_json(token, "/profile", params=())
        return GmailProfile(
            email_address=_mapping_str(payload, "emailAddress"),
            messages_total=_mapping_int(payload, "messagesTotal"),
            threads_total=_mapping_int(payload, "threadsTotal"),
            history_id=_mapping_str(payload, "historyId"),
        )

    def find_sent_message_by_rfc_message_id(
        self,
        token: GmailAccessToken,
        *,
        rfc_message_id: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage | None:
        _validate_rfc_message_id(rfc_message_id)
        if not metadata_headers:
            raise ValueError("metadata_headers must not be empty")
        page = self._list_messages_with_query(
            token,
            max_results=10,
            query=f"rfc822msgid:{rfc_message_id}",
            label_ids=("SENT",),
        )
        if not page.messages:
            return None
        exact_matches: list[GmailMetadataMessage] = []
        for message_ref in page.messages:
            message = self.get_message(
                token,
                message_id=message_ref.id,
                metadata_headers=metadata_headers,
            )
            if message.headers.get("message-id") == rfc_message_id and "SENT" in message.label_ids:
                exact_matches.append(message)
        if len(exact_matches) > 1:
            raise GmailApiError("GMAIL_SENT_RFC_MESSAGE_ID_AMBIGUOUS")
        return exact_matches[0] if exact_matches else None

    def find_recent_sent_smoke_messages(
        self,
        token: GmailAccessToken,
        *,
        account_subject: str,
        not_before: datetime,
        expected_body_sha256: str,
    ) -> tuple[GmailSentSmokeProof, ...]:
        """Find only recent, exact, broker-generated qualification messages.

        This bounded recovery path exists because Gmail can rewrite RFC Message-ID
        during users.messages.send. It never exposes raw message content to callers.
        """
        _validate_account_subject(account_subject)
        if not_before.tzinfo is None or not_before.utcoffset() is None:
            raise ValueError("not_before must be timezone-aware")
        if re.fullmatch(r"[0-9a-f]{64}", expected_body_sha256) is None:
            raise ValueError("expected_body_sha256 must be a SHA-256 digest")
        payload = self._get_json(
            token,
            "/messages",
            params=(
                ("maxResults", 10),
                ("q", _SMOKE_SEARCH_QUERY),
                ("labelIds", "SENT"),
            ),
        )
        raw_messages_obj = payload.get("messages", [])
        if not isinstance(raw_messages_obj, list):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        proofs: list[GmailSentSmokeProof] = []
        for item in cast("list[object]", raw_messages_obj):
            if not isinstance(item, Mapping):
                continue
            message_id = _mapping_str(cast("Mapping[str, object]", item), "id")
            proof = self._get_sent_smoke_proof(
                token,
                message_id=message_id,
                account_subject=account_subject,
            )
            if (
                proof is not None
                and proof.message.internal_date is not None
                and proof.message.internal_date >= not_before.astimezone(UTC)
                and proof.body_sha256 == expected_body_sha256
            ):
                proofs.append(proof)
        return tuple(proofs)

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
    ) -> GmailMetadataMessage | None:
        """Reconcile one just-sent qualification message without sending again."""
        _validate_token(provider_message_id, "provider_message_id")
        _validate_token(provider_thread_id, "provider_thread_id")
        _validate_account_subject(sender)
        _validate_account_subject(recipient)
        match = _SMOKE_SUBJECT.fullmatch(subject)
        if match is None:
            raise ValueError("subject must be a CareerOps Gmail qualification subject")
        local_part, domain = sender.split("@", 1)
        expected_recipient = f"{local_part}+careerops-smoke-{match.group(1)}@{domain}"
        if recipient.casefold() != expected_recipient.casefold():
            raise ValueError("recipient must match the Gmail qualification subject")
        if not metadata_headers:
            raise ValueError("metadata_headers must not be empty")
        for header in metadata_headers:
            _validate_header_name(header)

        page = self._get_json(
            token,
            "/messages",
            params=(
                ("maxResults", 10),
                ("q", _SMOKE_SEARCH_QUERY),
                ("labelIds", "SENT"),
            ),
        )
        raw_messages_obj = page.get("messages", [])
        if not isinstance(raw_messages_obj, list):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        matches: list[GmailMetadataMessage] = []
        for item in cast("list[object]", raw_messages_obj):
            if not isinstance(item, Mapping):
                continue
            item_mapping = cast("Mapping[str, object]", item)
            if (
                _mapping_str(item_mapping, "id") != provider_message_id
                or _mapping_str(item_mapping, "threadId") != provider_thread_id
            ):
                continue
            message = self.get_message(
                token,
                message_id=provider_message_id,
                metadata_headers=metadata_headers,
            )
            if (
                message.id == provider_message_id
                and message.thread_id == provider_thread_id
                and "SENT" in message.label_ids
                and parseaddr(message.headers.get("from", ""))[1].casefold() == sender.casefold()
                and parseaddr(message.headers.get("to", ""))[1].casefold() == recipient.casefold()
                and message.headers.get("subject") == subject
            ):
                matches.append(message)
        if len(matches) > 1:
            raise GmailApiError("GMAIL_SENT_SMOKE_LOOKUP_AMBIGUOUS")
        return matches[0] if matches else None

    def _get_sent_smoke_proof(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        account_subject: str,
    ) -> GmailSentSmokeProof | None:
        payload = self._get_json(
            token,
            f"/messages/{message_id}",
            params=(("format", "RAW"),),
        )
        raw = _mapping_str(payload, "raw")
        try:
            decoded = base64.b64decode(
                raw + "=" * (-len(raw) % 4),
                altchars=b"-_",
                validate=True,
            )
            parsed = _require_email_message(BytesParser(policy=policy.default).parsebytes(decoded))
        except (ValueError, TypeError):
            raise GmailApiError("GMAIL_INVALID_RAW_MESSAGE") from None
        subject = str(parsed.get("Subject", ""))
        match = _SMOKE_SUBJECT.fullmatch(subject)
        local_part, domain = account_subject.split("@", 1)
        expected_recipient = (
            f"{local_part}+careerops-smoke-{match.group(1)}@{domain}" if match else ""
        )
        body_part = parsed.get_body(preferencelist=("plain",))
        body = body_part.get_content().rstrip("\r\n") if body_part is not None else ""
        label_ids_obj = payload.get("labelIds", [])
        label_ids = (
            tuple(str(item) for item in cast("list[object]", label_ids_obj))
            if isinstance(label_ids_obj, list)
            else ()
        )
        from_address = parseaddr(str(parsed.get("From", "")))[1]
        to_address = parseaddr(str(parsed.get("To", "")))[1]
        if (
            match is None
            or "SENT" not in label_ids
            or from_address.casefold() != account_subject.casefold()
            or to_address.casefold() != expected_recipient.casefold()
        ):
            return None
        headers = {
            "from": str(parsed.get("From", "")),
            "to": str(parsed.get("To", "")),
            "subject": subject,
            "message-id": str(parsed.get("Message-ID", "")),
        }
        return GmailSentSmokeProof(
            message=GmailMetadataMessage(
                id=_mapping_str(payload, "id"),
                thread_id=_mapping_str(payload, "threadId"),
                history_id=_mapping_str(payload, "historyId"),
                headers=headers,
                label_ids=label_ids,
                snippet="",
                internal_date=_parse_internal_date(_optional_str(payload, "internalDate")),
            ),
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )

    def _get_json(
        self,
        token: GmailAccessToken,
        path: str,
        *,
        params: Sequence[tuple[str, str | int]],
    ) -> Mapping[str, object]:
        if token.is_expired():
            raise GmailApiError("GMAIL_ACCESS_TOKEN_EXPIRED", status_code=401)
        query = urlencode(params, doseq=True)
        url = f"{GMAIL_API_BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {token.value}",
                "Accept": "application/json",
            },
        )
        request_error: GmailApiError | None = None
        body = b""
        status_code: object = None
        try:
            # The origin is the fixed Gmail HTTPS API and every path is internal/validated.
            with urlopen(request, timeout=self._timeout_seconds) as response:  # nosec B310
                body = response.read(self._max_response_bytes + 1)
                status_code = getattr(response, "status", None) or response.getcode()
        except HTTPError as exc:
            bounded_status = _bounded_http_status(exc.code)
            error_code = (
                f"GMAIL_HTTP_{bounded_status}" if bounded_status is not None else "GMAIL_HTTP_ERROR"
            )
            request_error = GmailApiError(
                error_code,
                status_code=bounded_status,
            )
        except URLError:
            request_error = GmailApiError("GMAIL_TRANSPORT_ERROR")
        # Raise outside the handler so the external exception (which may contain
        # response bodies, headers, URLs, or provider reason text) is not retained
        # as either __cause__ or __context__ on the public exception.
        if request_error is not None:
            raise request_error
        if len(body) > self._max_response_bytes:
            raise GmailApiError("GMAIL_RESPONSE_TOO_LARGE")
        bounded_status = _bounded_http_status(status_code)
        if bounded_status is None:
            raise GmailApiError("GMAIL_INVALID_HTTP_STATUS")
        if bounded_status < 200 or bounded_status >= 300:
            raise GmailApiError(f"GMAIL_HTTP_{bounded_status}", status_code=bounded_status)
        parsed: object = {}
        invalid_json = False
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid_json = True
        # JSON decoder exceptions retain the complete source document. Keep that
        # provider-controlled body out of the application exception chain.
        if invalid_json:
            raise GmailApiError("GMAIL_INVALID_JSON")
        if not isinstance(parsed, dict):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        return cast("Mapping[str, object]", parsed)

    def _list_messages_with_query(
        self,
        token: GmailAccessToken,
        *,
        query: str,
        label_ids: Sequence[str],
        max_results: int,
    ) -> GmailMessageListPage:
        _validate_max_results(max_results)
        _validate_search_query(query)
        params: list[tuple[str, str | int]] = [("maxResults", max_results), ("q", query)]
        for label_id in label_ids:
            _validate_label_id(label_id)
            params.append(("labelIds", label_id))
        payload = self._get_json(token, "/messages", params=params)
        raw_messages_obj = payload.get("messages", [])
        if not isinstance(raw_messages_obj, list):
            raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
        raw_messages = cast("list[object]", raw_messages_obj)
        messages = tuple(
            GmailListedMessage(
                id=_mapping_str(cast("Mapping[str, object]", message), "id"),
                thread_id=_mapping_str(cast("Mapping[str, object]", message), "threadId"),
            )
            for message in raw_messages
            if isinstance(message, Mapping)
        )
        return GmailMessageListPage(messages=messages)


def _extract_headers(payload: Mapping[str, object]) -> Mapping[str, str]:
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, Mapping):
        return {}
    payload_mapping = cast("Mapping[str, object]", raw_payload)
    raw_headers_obj = payload_mapping.get("headers", [])
    if not isinstance(raw_headers_obj, list):
        return {}
    raw_headers = cast("list[object]", raw_headers_obj)
    headers: dict[str, str] = {}
    for header in raw_headers:
        if not isinstance(header, Mapping):
            continue
        header_mapping = cast("Mapping[str, object]", header)
        name = header_mapping.get("name")
        value = header_mapping.get("value")
        if isinstance(name, str) and isinstance(value, str):
            headers[name.lower()] = value
    return dict(sorted(headers.items()))


def _mapping_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
    return value.strip()


def _mapping_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int):
        raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
    return value


def _optional_str(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
    return value


def _parse_internal_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    milliseconds: int | None = None
    with suppress(ValueError):
        milliseconds = int(value)
    if milliseconds is None:
        raise GmailApiError("GMAIL_SCHEMA_MISMATCH")
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)


def _bounded_http_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        return None
    return value


def _validate_max_results(value: int) -> None:
    if value < 1 or value > 500:
        raise ValueError("max_results must be between 1 and 500")


def _validate_token(value: str, field_name: str) -> None:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")


def _validate_history_id(value: str) -> None:
    if _HISTORY_ID.fullmatch(value) is None:
        raise ValueError("start_history_id must be a Gmail history id")


def _validate_header_name(value: str) -> None:
    if _HEADER_NAME.fullmatch(value) is None:
        raise ValueError("metadata header must be a bounded RFC header name")


def _validate_label_id(value: str) -> None:
    if _LABEL_ID.fullmatch(value) is None:
        raise ValueError("Gmail label id must be a bounded identifier")


def _validate_rfc_message_id(value: str) -> None:
    if _RFC_MESSAGE_ID.fullmatch(value) is None:
        raise ValueError("RFC Message-ID must be bounded")


def _validate_search_query(value: str) -> None:
    if _SEARCH_QUERY.fullmatch(value) is None:
        raise ValueError("Gmail search query must be a bounded RFC Message-ID lookup")


def _validate_account_subject(value: str) -> None:
    parsed = parseaddr(value.strip())[1]
    if parsed.casefold() != value.strip().casefold() or parsed.count("@") != 1:
        raise ValueError("account_subject must be one bounded email address")
    if len(parsed) > 320 or any(character in parsed for character in "\r\n"):
        raise ValueError("account_subject must be one bounded email address")


__all__ = [
    "GMAIL_API_BASE_URL",
    "GmailApiError",
    "GmailHistoryCursorExpired",
    "GmailHistoryPage",
    "GmailListedMessage",
    "GmailMessageListPage",
    "GmailMetadataMessage",
    "GmailProfile",
    "GmailReadOnlyHttpClient",
    "GmailSentSmokeProof",
]
