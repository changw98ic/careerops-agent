"""M6 Gmail send adapter: sends email via the Gmail API.

This is the send capability that M4 deliberately excluded (M4 is read-only).
It requires an OAuth token granted the ``gmail.send`` scope, obtained through
the separate send-scope authorization flow (scripts/gmail_auth_send.py).

Safety posture:
- This adapter only sends exactly what it is given. It never decides recipients
  or content on its own. All policy/review gates live in the calling pipeline.
- Recipients must be supplied by trusted business state (a publicly-listed
  recruiting contact or an operator-entered address). This module does not
  guess or derive addresses.
- Every send returns the provider message/thread id so the caller can record an
  audit trail and reconcile.
- Tokens are loaded from a gitignored secret file, never committed or logged.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105


class GmailSendError(RuntimeError):
    """Raised when a send fails."""


@dataclass(frozen=True, slots=True)
class SendResult:
    """Provider receipt for a sent message."""

    provider_message_id: str
    thread_id: str
    label_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    """A fully-specified outgoing email. All fields are caller-supplied/trusted."""

    to: str
    subject: str
    body: str
    from_account: str = "me"
    attachments: tuple[Path, ...] = ()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_raw_message(email: OutgoingEmail) -> str:
    """Build a base64url-encoded RFC 2822 message for the Gmail API."""
    msg = EmailMessage()
    msg["To"] = email.to
    msg["Subject"] = email.subject
    msg["Date"] = formatdate(localtime=True)

    if email.attachments:
        msg.set_content(email.body)
        for path in email.attachments:
            data = path.read_bytes()
            maintype, _, subtype = _guess_mime(path.suffix.lower())
            msg.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )
    else:
        msg.set_content(email.body)

    return _b64url(bytes(msg))


def _guess_mime(suffix: str) -> tuple[str, str, str]:
    table = {
        ".pdf": ("application", "pdf", "pdf"),
        ".txt": ("text", "plain", "txt"),
        ".doc": ("application", "msword", "doc"),
        ".docx": (
            "application",
            "vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
    }
    return table.get(suffix, ("application", "octet-stream", suffix.lstrip(".") or "bin"))


class GmailSender:
    """Sends email through the Gmail API using a bearer access token.

    The sender is stateless with respect to policy: it sends exactly the
    messages it is handed and reports the provider receipt. Review, approval,
    recipient validation, and audit logging are the caller's responsibility.
    """

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise GmailSendError("access token is required")
        self._access_token = access_token

    def send(self, email: OutgoingEmail) -> SendResult:
        """Send a single email. Raises GmailSendError on failure."""
        if not email.to or "@" not in email.to:
            raise GmailSendError("a valid recipient address is required")
        if not email.subject.strip():
            raise GmailSendError("subject is required")
        if not email.body.strip():
            raise GmailSendError("body is required")

        raw = build_raw_message(email)
        payload = json.dumps({"raw": raw}).encode("utf-8")
        req = urllib.request.Request(
            GMAIL_SEND_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise GmailSendError(f"Gmail API HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise GmailSendError(f"Gmail API network error: {e.reason}") from e

        return SendResult(
            provider_message_id=str(data.get("id", "")),
            thread_id=str(data.get("threadId", "")),
            label_ids=tuple(data.get("labelIds", [])),
        )


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> str:
    """Exchange a refresh token for a fresh access token."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GMAIL_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise GmailSendError(f"token refresh HTTP {e.code}: {detail}") from e
    token = data.get("access_token")
    if not token:
        raise GmailSendError("token refresh returned no access_token")
    return token
