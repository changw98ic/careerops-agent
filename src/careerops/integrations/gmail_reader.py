"""Gmail read adapter: incremental inbound mail via the Gmail history list.

This is Phase 1.3 of ``real-autonomous-career-loop``. ``MailSyncService``
performs NO provider network calls — it consumes already-fetched message
dicts via :meth:`~careerops.application.mail_sync_service.MailSyncService.run_sync_step`.
``GmailReader`` is the first hop that connects Gmail to mail intelligence: it
pulls new inbound mail incrementally (resuming from the stored history-id
cursor), fetches full message details, and assembles the message dicts
``run_sync_step`` expects.

OAuth: the ``gmail.readonly`` scope was granted alongside ``gmail.send``
during the single authorization flow, so the reader reuses the same
:class:`~careerops.integrations.gmail_token_store.GmailTokenStore` as the
sender — one refresh token serves both directions.

Incremental protocol (Gmail history API):

1. Start from ``start_history_id`` (the durable cursor, or the account's
   ``history_id`` on first sync).
2. ``GET .../users/me/history?startHistoryId=<cursor>&historyTypes=messageAdded``
   returns ``history[]``; each entry carries a new ``id`` (the next cursor)
   and ``messages[]`` (``id`` + ``threadId``).
3. For each new message id, ``GET .../users/me/messages/{id}?format=FULL``
   yields ``snippet``/``payload`` (headers + body parts)/``internalDate``/
   ``threadId``/``historyId``.
4. The history cursor is only ~1 week deep. A ``404 historyNotAvailable``
   response means the cursor is stale; the caller should fall back to a full
   sync direction (``SyncDirection.FULL``).

The reader never advances the cursor itself — :meth:`run_sync_step` does that
from the last ``history_id`` in the batch it ingests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr

from careerops.integrations.gmail_token_store import GmailTokenStore

GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
# How many history entries to request per page.
_DEFAULT_HISTORY_MAX = 100
# Cap the total messages fetched in one incremental pass so a single run is
# bounded; the next scheduled poll picks up the rest.
_DEFAULT_MESSAGE_CAP = 200


class GmailReadError(RuntimeError):
    """Raised when a Gmail read operation fails."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one incremental fetch pass.

    ``messages`` are the dicts to hand to ``MailSyncService.run_sync_step``.
    ``last_history_id`` is the newest history id observed (may be ``""`` when
    nothing was fetched). ``history_expired`` is True when Gmail reported the
    start cursor is too old and a full re-sync is required.
    """

    messages: tuple[dict[str, object], ...]
    last_history_id: str
    history_expired: bool


class GmailReader:
    """Reads new inbound Gmail mail incrementally for the sync path.

    Construct with the shared :class:`GmailTokenStore`. The HTTP transport is
    injectable (``http_get``) so tests can drive the history/messages flow
    without network access; production uses :func:`_urllib_get`.
    """

    def __init__(
        self,
        token_store: GmailTokenStore,
        *,
        http_get: Callable[[str, str], dict[str, object]] | None = None,
        history_max: int = _DEFAULT_HISTORY_MAX,
        message_cap: int = _DEFAULT_MESSAGE_CAP,
    ) -> None:
        self._token_store = token_store
        self._http_get = http_get or _urllib_get
        self._history_max = history_max
        self._message_cap = message_cap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_incremental(self, start_history_id: str) -> FetchResult:
        """Fetch new messages added since ``start_history_id``.

        Returns a :class:`FetchResult`. When ``start_history_id`` is empty the
        reader treats it as a cold start and seeds the cursor from the user's
        current ``historyId`` (``users/getProfile``) without fetching history —
        the next poll then pulls incrementally from that point.
        """
        if not start_history_id:
            seeded = self._seed_history_id()
            return FetchResult(messages=(), last_history_id=seeded, history_expired=False)

        return self._fetch_history(start_history_id)

    def current_history_id(self) -> str:
        """Return the user's current ``historyId`` via ``users.getProfile``."""
        profile = self._get(f"{GMAIL_BASE_URL}/profile")
        return str(profile.get("historyId", "") or "")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _seed_history_id(self) -> str:
        """Cold start: record the current historyId so the next poll is incremental."""
        try:
            return self.current_history_id()
        except GmailReadError:
            # No profile access -> nothing to seed; caller falls back to full.
            return ""

    def _fetch_history(self, start_history_id: str) -> FetchResult:
        """Page through history.list, then fetch each new message in full."""
        collected: list[dict[str, object]] = []
        last_history_id = ""
        page_token: str | None = None
        fetched_ids: set[str] = set()

        while True:
            params: dict[str, str] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "maxResults": str(self._history_max),
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                data = self._get(
                    f"{GMAIL_BASE_URL}/history", params=params
                )
            except GmailReadError as exc:
                if _is_history_not_available(exc):
                    return FetchResult(
                        messages=(),
                        last_history_id="",
                        history_expired=True,
                    )
                raise

            history = data.get("history") or []
            for entry in history:
                entry_id = str(entry.get("id", "") or "")
                if entry_id:
                    last_history_id = entry_id
                for msg_ref in entry.get("messages") or []:
                    msg_id = str(msg_ref.get("id", "") or "")
                    if not msg_id or msg_id in fetched_ids:
                        continue
                    fetched_ids.add(msg_id)
                    message = self._fetch_message(msg_id)
                    if message is not None:
                        collected.append(message)
                        if len(collected) >= self._message_cap:
                            return FetchResult(
                                messages=tuple(collected),
                                last_history_id=last_history_id,
                                history_expired=False,
                            )

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return FetchResult(
            messages=tuple(collected),
            last_history_id=last_history_id,
            history_expired=False,
        )

    def _fetch_message(self, message_id: str) -> dict[str, object] | None:
        """GET a single message (FULL) and assemble the run_sync_step dict."""
        data = self._get(f"{GMAIL_BASE_URL}/messages/{message_id}", params={"format": "FULL"})
        return _message_to_dict(data)

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, object]:
        """Perform an authenticated GET, returning parsed JSON."""
        target = url
        if params:
            target = f"{url}?{urllib.parse.urlencode(params)}"
        access_token = self._token_store.get_access_token()
        return self._http_get(target, access_token)


# ----------------------------------------------------------------------
# Message parsing helpers
# ----------------------------------------------------------------------


def _message_to_dict(data: dict[str, object]) -> dict[str, object] | None:
    """Convert a Gmail messages.get response to a run_sync_step message dict.

    Required by ``MailSyncService.run_sync_step``:
    ``provider_message_id`` (the Gmail message ``id``), ``provider_thread_id``
    (``threadId``), ``subject``, ``snippet``, ``body_text``, ``sender_email``,
    ``received_at`` (from ``internalDate`` ms epoch), and ``history_id``.
    Returns None when the message has no id (run_sync_step would skip it).
    """
    message_id = str(data.get("id", "") or "")
    if not message_id:
        return None
    thread_id = str(data.get("threadId", "") or "")
    history_id = str(data.get("historyId", "") or "")
    snippet = str(data.get("snippet", "") or "")
    payload = data.get("payload")
    headers, body_text = _parse_payload(payload) if isinstance(payload, dict) else ({}, "")
    subject = headers.get("Subject", "")
    from_header = headers.get("From", "")
    sender_email, sender_name = _parse_from(from_header)
    received_at = _internal_date_to_datetime(data.get("internalDate"))
    message: dict[str, object] = {
        "provider_message_id": message_id,
        "provider_thread_id": thread_id,
        "subject": subject,
        "snippet": snippet,
        "body_text": body_text,
        "sender_email": sender_email,
        "history_id": history_id,
        "received_at": received_at,
    }
    if sender_name:
        message["sender_name"] = sender_name
    return message


def _parse_payload(payload: dict[str, object]) -> tuple[dict[str, str], str]:
    """Extract header name->value map and the plain-text body from a payload.

    Gmail returns ``text/plain`` either at the top level or nested in
    ``parts``. We walk the first ``text/plain`` part found (depth-first) so the
    sync path gets usable body text for recruitment classification (Section
    11.5); non-recruitment bodies are discarded later by ``ingest_message``.
    """
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if isinstance(header, dict):
            name = str(header.get("name", "") or "")
            value = str(header.get("value", "") or "")
            if name:
                headers[name] = value
    body = _extract_body(payload)
    return headers, body


def _extract_body(payload: dict[str, object]) -> str:
    """Return the first ``text/plain`` body data, decoded from base64url."""
    mime_type = str(payload.get("mimeType", "") or "")
    body = payload.get("body")
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data and "plain" in mime_type:
            return _decode_body_data(data)
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                text = _extract_body(part)
                if text:
                    return text
    return ""


def _decode_body_data(data: str) -> str:
    """Decode a Gmail body ``data`` field (base64url, URL-safe, no padding)."""
    import base64

    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, UnicodeDecodeError):
        return ""


def _parse_from(from_header: str) -> tuple[str, str]:
    """Split a ``From`` header into (email, display_name).

    Uses :func:`email.utils.parseaddr` so ``"Recruiter Name <a@b.com>"`` yields
    ``("a@b.com", "Recruiter Name")``. ``sender_name`` is empty when the header
    is a bare address.
    """
    if not from_header:
        return "", ""
    name, addr = parseaddr(from_header)
    return addr, name.strip()


def _internal_date_to_datetime(raw: object) -> datetime:
    """Convert Gmail ``internalDate`` (ms epoch string) to an aware datetime.

    Gmail returns ``internalDate`` as a string of epoch milliseconds. Falls
    back to ``now`` when absent/malformed so run_sync_step always gets a real
    timestamp.
    """
    if raw is None:
        return datetime.now(UTC)
    try:
        millis = int(str(raw))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return datetime.fromtimestamp(millis / 1000.0, tz=UTC)


def _is_history_not_available(exc: GmailReadError) -> bool:
    """True when Gmail rejected the start cursor as too old (historyNotAvailable)."""
    msg = str(exc).lower()
    return "historynotavailable" in msg or ("history is not available" in msg)


# ----------------------------------------------------------------------
# Default HTTP transport
# ----------------------------------------------------------------------


def _urllib_get(url: str, access_token: str) -> dict[str, object]:
    """Default authenticated GET using urllib; raises GmailReadError on failure."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise GmailReadError(f"Gmail API HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise GmailReadError(f"Gmail API network error: {e.reason}") from e
    if status >= 400:
        raise GmailReadError(f"Gmail API HTTP {status}: {raw[:500]}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise GmailReadError(f"Gmail API returned non-JSON: {raw[:200]}") from e
