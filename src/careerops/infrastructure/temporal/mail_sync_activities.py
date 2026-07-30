"""Temporal activities for the inbound mail-sync trigger (Phase 1.3 / 2.1).

Two activity bundles, both injected conditionally into the worker (see
``M1ActivityBundles``), so a worker without mail reading/token refresh
configured stays fail-closed:

- :class:`MailSyncReaderActivities` — the ``fetch_and_sync`` activity. It is
  the caller that connects Gmail to mail intelligence: it drives
  :class:`~careerops.integrations.gmail_reader.GmailReader` (history.list +
  messages.get) and hands the fetched message dicts to
  :meth:`~careerops.application.mail_sync_service.MailSyncService.run_sync_step`.
  ``MailSyncService`` does no network calls; this activity is the only hop that
  performs them.

- :class:`GmailTokenRefreshActivities` — the ``refresh_gmail_token`` activity,
  the *scheduled layer* of the dual-layer token refresh. It force-refreshes the
  shared :class:`~careerops.integrations.gmail_token_store.GmailTokenStore` so
  the send path (the *send layer*) usually finds a fresh cached token and skips
  its own refresh.

Both run in a worker thread: ``GmailReader`` and the token store are
synchronous (urllib), so the activity offloads the network work via
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from temporalio import activity

from careerops.application.mail_sync_service import (
    AccountNotConnectedError,
    MailSyncError,
    MailSyncService,
)
from careerops.domain.mail_sync import SyncDirection
from careerops.integrations.gmail_reader import GmailReadError, GmailReader
from careerops.integrations.gmail_token_store import GmailTokenStore
from careerops.workflows.mail_sync_contracts import (
    GMAIL_TOKEN_REFRESH_ACTIVITY,
    MAIL_SYNC_FETCH_ACTIVITY,
    GmailTokenRefreshResult,
    MailSyncFetchInput,
    MailSyncFetchResult,
)


class MailSyncReaderActivities:
    """Activities that fetch inbound Gmail mail and feed the sync path.

    Holds the shared :class:`GmailReader` and the :class:`MailSyncService`.
    The reader authenticates with the same token store as the sender
    (``gmail.readonly`` + ``gmail.send`` were granted together).
    """

    def __init__(self, *, reader: GmailReader, service: MailSyncService) -> None:
        self._reader = reader
        self._service = service

    @activity.defn(name=MAIL_SYNC_FETCH_ACTIVITY)
    async def fetch_and_sync(self, request: MailSyncFetchInput) -> MailSyncFetchResult:
        candidate_id = UUID(request.candidate_id)
        account_id = UUID(request.account_id)
        now = datetime.now(UTC)

        try:
            run = self._service.request_sync(
                candidate_id=candidate_id,
                account_id=account_id,
                now=now,
            )
        except AccountNotConnectedError as exc:
            return MailSyncFetchResult(
                run_id="",
                messages_fetched=0,
                messages_processed=0,
                messages_skipped=0,
                error=f"ACCOUNT_NOT_CONNECTED:{exc}",
            )

        start_history_id = request.start_history_id or run.history_id_start
        try:
            result = await asyncio.to_thread(
                self._reader.fetch_incremental, start_history_id
            )
        except GmailReadError as exc:
            return MailSyncFetchResult(
                run_id=str(run.id),
                messages_fetched=0,
                messages_processed=0,
                messages_skipped=0,
                error=f"GMAIL_READ_ERROR:{exc}",
            )

        # Gmail's history cursor only reaches back ~1 week. When it reports the
        # start cursor as expired, signal the caller that the next pass should
        # request a FULL sync direction (re-pull rather than incremental).
        if result.history_expired:
            return MailSyncFetchResult(
                run_id=str(run.id),
                messages_fetched=0,
                messages_processed=0,
                messages_skipped=0,
                history_expired=True,
                error="HISTORY_EXPIRED",
            )

        if not result.messages:
            return MailSyncFetchResult(
                run_id=str(run.id),
                messages_fetched=0,
                messages_processed=0,
                messages_skipped=0,
            )

        try:
            updated = self._service.run_sync_step(
                candidate_id=candidate_id,
                run_id=run.id,
                messages=result.messages,
                now=datetime.now(UTC),
            )
        except MailSyncError as exc:
            return MailSyncFetchResult(
                run_id=str(run.id),
                messages_fetched=len(result.messages),
                messages_processed=0,
                messages_skipped=0,
                error=f"SYNC_STEP_FAILED:{exc}",
            )

        return MailSyncFetchResult(
            run_id=str(updated.id),
            messages_fetched=len(result.messages),
            messages_processed=updated.messages_processed,
            messages_skipped=updated.messages_skipped,
            history_expired=False,
        )


class GmailTokenRefreshActivities:
    """The scheduled layer of the dual-layer Gmail token refresh.

    Force-refreshes the shared token store hourly; the send layer
    (``GmailTokenStore.get_access_token``) refreshes inline near expiry but
    skips when this layer just refreshed it.
    """

    def __init__(self, token_store: GmailTokenStore) -> None:
        self._token_store = token_store

    @activity.defn(name=GMAIL_TOKEN_REFRESH_ACTIVITY)
    async def refresh_gmail_token(self) -> GmailTokenRefreshResult:
        try:
            await asyncio.to_thread(self._token_store.refresh_now)
            return GmailTokenRefreshResult(refreshed=True)
        except Exception as exc:  # noqa: BLE001 - activity must not crash the worker
            activity.logger.warning("gmail token refresh failed: %s", exc)
            return GmailTokenRefreshResult(refreshed=False, error=str(exc)[:200])
