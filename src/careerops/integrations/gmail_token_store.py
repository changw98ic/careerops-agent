"""Shared Gmail OAuth token store with dual-layer refresh.

Both the Gmail **send** path (``GmailSender``) and the Gmail **read** path
(``GmailReader``) authenticate with the *same* OAuth refresh token — the
``gmail.readonly`` + ``gmail.send`` scopes are granted together during the
single authorization flow (``scripts/gmail_auth_send.py``). A single
``refresh_access_token`` call therefore yields an access token valid for both
directions, so we keep one token store rather than two secret files.

Dual-layer refresh (spec: ``proactive-trigger-loop``):

1. **Scheduled layer** — a Temporal activity calls
   :meth:`GmailTokenStore.refresh_now` roughly hourly. It force-refreshes and
   caches the new access token (+ expiry).
2. **Send layer** — :meth:`GmailTokenStore.get_access_token` is called right
   before every send. It returns the cached token when it is still fresh, and
   only refreshes when the token is near expiry. Crucially, if a refresh *just*
   happened (within :data:`_RECENT_REFRESH_SKIP_WINDOW`), it reuses the cached
   token rather than hammering Google again — this is the "send layer skips a
   refresh it just did" rule that prevents the two layers from racing into a
   redundant refresh on the same minute.

The store is safe to share across the sender, the reader, and the refresh
activity within one process.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from careerops.integrations.gmail_sender import GmailSendError, refresh_access_token

# Reuse a token when it has more than this long left before its declared
# expiry. Google access tokens live ~3600s; refreshing with 5 minutes of head
# room avoids a mid-send 401.
_REFRESH_MARGIN_SECONDS = 300

# If a refresh happened within this window, ``get_access_token`` reuses the
# cached token even if it looks near expiry — the scheduled layer just
# refreshed it, so a second refresh in the same send path is redundant.
_RECENT_REFRESH_SKIP_WINDOW = 60

# How long (seconds) a freshly refreshed access token is assumed to live.
# Google returns ``expires_in`` of ~3600; we cache the same assumption so the
# store works without parsing the refresh response.
_TOKEN_LIFETIME_SECONDS = 3600.0


class GmailTokenStore:
    """Shared store of a Gmail OAuth refresh credential.

    Holds the refresh token + OAuth client credentials and lazily refreshes
    access tokens on demand. Construct once and inject into both the sender
    and the reader so they share one refresh cadence.
    """

    def __init__(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        clock: Callable[[], float] | None = None,
        refresher: Callable[..., str] | None = None,
    ) -> None:
        if not (refresh_token and client_id and client_secret):
            raise RuntimeError(
                "GmailTokenStore requires refresh_token, client_id and client_secret"
            )
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self._clock: Callable[[], float] = clock or time.time
        # Defaults to the real refresh routine; injectable for deterministic tests.
        self._refresher: Callable[..., str] = refresher or refresh_access_token
        self._access_token = ""
        self._expires_at = 0.0  # epoch seconds; 0 means unknown
        self._last_refresh_at = 0.0  # epoch seconds of the most recent refresh
        self._lock = threading.Lock()

    @classmethod
    def from_token_file(cls, token_file: str | Path) -> "GmailTokenStore":
        """Build a store from the secrets/gmail_send_token.json record.

        Raises ``RuntimeError`` (fail-fast at startup) when the file is missing
        or the record lacks ``refresh_token``/``client_id``/``client_secret`` —
        a broken credential must surface now, not at the first send/read.
        """
        path = Path(token_file)
        if not path.exists():
            raise RuntimeError(f"Gmail token file not found: {path}")
        record = json.loads(path.read_text())
        refresh_token = str(record.get("refresh_token", "") or "")
        client_id = str(record.get("client_id", "") or "")
        client_secret = str(record.get("client_secret", "") or "")
        return cls(
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a usable access token, refreshing only when necessary.

        - ``force_refresh=True`` always refreshes (used by the hourly activity).
        - Otherwise the cached token is returned while it still has more than
          :data:`_REFRESH_MARGIN_SECONDS` of life left, OR if it was refreshed
          within :data:`_RECENT_REFRESH_SKIP_WINDOW` (the scheduled layer just
          did it — skip the redundant call).
        """
        with self._lock:
            now = self._clock()
            if not force_refresh and self._access_token:
                fresh = self._expires_at == 0 or (
                    self._expires_at - now > _REFRESH_MARGIN_SECONDS
                )
                recently_refreshed = (
                    now - self._last_refresh_at
                ) < _RECENT_REFRESH_SKIP_WINDOW
                if fresh or recently_refreshed:
                    return self._access_token
            return self._do_refresh(now)

    def refresh_now(self) -> str:
        """Force a refresh and cache the result (the scheduled-layer entry point)."""
        with self._lock:
            return self._do_refresh(self._clock())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _do_refresh(self, now: float) -> str:
        """Call the refresh routine and cache the result. Caller holds ``_lock``."""
        token = self._refresher(
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=self.refresh_token,
        )
        if not token:
            raise GmailSendError("token refresh returned no access_token")
        self._access_token = token
        self._expires_at = now + _TOKEN_LIFETIME_SECONDS
        self._last_refresh_at = now
        return token


def load_gmail_token_store(token_file: str | Path) -> GmailTokenStore:
    """Convenience wrapper kept for explicit construction sites."""
    return GmailTokenStore.from_token_file(token_file)
