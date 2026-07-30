"""Workflow contracts for the inbound mail-sync trigger (Phase 1.3 / 2.1).

Activity names + input/output dataclasses for the Temporal wiring that pulls
new Gmail mail incrementally and refreshes the shared Gmail token on a
schedule. Follows the same pattern as ``s5_contracts.py``: frozen dataclasses,
string constants for activity names, no runtime imports from infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Activity names
# ---------------------------------------------------------------------------

#: Fetch new inbound mail via ``GmailReader`` and hand it to
#: ``MailSyncService.run_sync_step``. Drives the inbound half of the career
#: loop so replies flow back without a manual poll.
MAIL_SYNC_FETCH_ACTIVITY = "careerops.mail_sync.fetch_and_sync"

#: Refresh the shared Gmail access token (the scheduled layer of the dual-
#: layer refresh, spec ``proactive-trigger-loop``). Runs roughly hourly; the
#: send layer is handled inline by ``GmailTokenStore.get_access_token``.
GMAIL_TOKEN_REFRESH_ACTIVITY = "careerops.mail_sync.refresh_gmail_token"


# ---------------------------------------------------------------------------
# Activity I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailSyncFetchInput:
    """Input for the fetch_and_sync activity.

    ``candidate_id`` / ``account_id`` scope the sync run. ``start_history_id``
    is optional; when empty the activity reads the durable cursor (or the
    account's ``history_id``) via ``MailSyncService.request_sync``.
    """

    candidate_id: str
    account_id: str
    start_history_id: str = ""


@dataclass(frozen=True, slots=True)
class MailSyncFetchResult:
    """Outcome of one incremental fetch+sync pass.

    ``messages_fetched`` is the count handed to ``run_sync_step``.
    ``messages_processed`` / ``messages_skipped`` mirror the run counters.
    ``history_expired`` is True when Gmail's start cursor was too old and a
    full re-sync direction should be requested on the next pass.
    """

    run_id: str
    messages_fetched: int
    messages_processed: int
    messages_skipped: int
    history_expired: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class GmailTokenRefreshResult:
    """Outcome of the hourly token-refresh activity."""

    refreshed: bool
    error: str = ""
