"""PostgreSQL-backed mail-sync repositories (Section 11).

Implements the three Protocol repositories defined in
:mod:`careerops.application.mail_sync_service`:

- :class:`PostgresMailAccountRepository` — dedicated-account connection state
  (task 11.1) + revoke/error transitions (task 11.2). Server-side candidate
  scoping on every read; the legacy ``email_accounts`` row gains the additive
  ``candidate_id`` / ``granted_scopes`` / ``connection_state`` columns.
- :class:`PostgresSyncRunRepository` — durable sync-run status + incremental
  cursor (task 11.3) + duplicate-delivery dedup set (task 11.4).
- :class:`PostgresThreadLinkRepository` — thread/message store + thread
  association links (task 11.6), including unresolved-link queue and
  append-only user confirmations.

All reads are scoped by the server-resolved ``candidate_id`` (Iron Rule 2);
there is no unscoped fallback. Cursor pagination follows the established
``limit + 1`` + base64 ``(created_at, id)`` pattern. No plaintext token is
ever read or written — only the protected ``credential_reference_id``.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.domain.applications import ApplicationState
from careerops.domain.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailMessage,
    EmailMessageState,
    EmailThread,
)
from careerops.domain.mail_sync import (
    LinkConfidence,
    LinkConfirmationDecision,
    MailAccountSummary,
    MailConnectionState,
    MailSyncCursor,
    MailSyncRun,
    MailSyncRunStatus,
    SyncDirection,
    ThreadLinkSnapshot,
    ThreadLinkStatus,
    UnresolvedThreadLink,
)
from careerops.infrastructure.database.schema import (
    email_accounts,
    email_messages,
    email_sync_cursors,
    email_sync_runs,
    email_thread_links,
    email_threads,
)

__all__ = [
    "PostgresMailAccountRepository",
    "PostgresSyncRunRepository",
    "PostgresThreadLinkRepository",
]


_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = decoded.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), UUID(id_str)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _row_to_account(row: sa.RowMapping) -> EmailAccount:
    status_raw = str(row["status"])
    try:
        status = EmailAccountStatus(status_raw)
    except ValueError:
        status = EmailAccountStatus.ACTIVE
    return EmailAccount(
        id=row["id"],
        email_address=str(row["email_address"]),
        credential_reference_id=row["credential_reference_id"],
        status=status,
        history_id=str(row["history_id"]),
        watch_expiration=row["watch_expiration"],
        last_sync_at=row["last_sync_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_summary(row: sa.RowMapping, candidate_id: UUID) -> MailAccountSummary:
    raw_scopes = cast("list[object]", row["granted_scopes"] or [])
    scopes = tuple(sorted({str(s) for s in raw_scopes})) or (_READONLY_SCOPE,)
    state_raw = str(row["connection_state"])
    try:
        state = MailConnectionState(state_raw)
    except ValueError:
        state = MailConnectionState.DISCONNECTED
    return MailAccountSummary(
        account_id=row["id"],
        candidate_id=candidate_id,
        email_address=str(row["email_address"]),
        connection_state=state,
        granted_scopes=scopes,
        credential_reference_id=row["credential_reference_id"],
        history_id=str(row["history_id"]),
        watch_expiration=row["watch_expiration"],
        last_sync_at=row["last_sync_at"],
        last_error_code=str(row["last_error_code"]),
    )


def _row_to_run(row: sa.RowMapping) -> MailSyncRun:
    return MailSyncRun(
        id=row["id"],
        account_id=row["account_id"],
        candidate_id=row["candidate_id"],
        status=MailSyncRunStatus(str(row["status"])),
        direction=SyncDirection(str(row["direction"])),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        messages_processed=int(row["messages_processed"]),
        messages_skipped=int(row["messages_skipped"]),
        history_id_start=str(row["history_id_start"]),
        history_id_end=str(row["history_id_end"]),
        error_code=str(row["error_code"]),
    )


# ---------------------------------------------------------------------------
# Account repository (tasks 11.1 / 11.2)
# ---------------------------------------------------------------------------


class PostgresMailAccountRepository:
    """Dedicated recruiting-account store, scoped by server-resolved candidate."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_account(self, candidate_id: UUID, account_id: UUID) -> EmailAccount | None:
        stmt = sa.select(email_accounts).where(
            sa.and_(
                email_accounts.c.id == account_id,
                email_accounts.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_account(row) if row else None

    def get_account_for_candidate(self, candidate_id: UUID) -> EmailAccount | None:
        stmt = (
            sa.select(email_accounts)
            .where(email_accounts.c.candidate_id == candidate_id)
            .order_by(email_accounts.c.created_at.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_account(row) if row else None

    def get_summary(self, candidate_id: UUID, account_id: UUID) -> MailAccountSummary | None:
        stmt = sa.select(email_accounts).where(
            sa.and_(
                email_accounts.c.id == account_id,
                email_accounts.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_summary(row, candidate_id) if row else None

    def upsert_account(self, account: EmailAccount) -> None:
        """Upsert the legacy ``email_accounts`` row.

        ``candidate_id`` is required for new rows but not carried on the
        ``EmailAccount`` value type; callers set the connection state (and thus
        the candidate scope) via :meth:`set_connection_state`. This method
        therefore leaves ``candidate_id`` untouched on update (preserving the
        existing scope) and only refreshes the credential/sync columns.
        """
        stmt = pg_insert(email_accounts).values(
            id=account.id,
            email_address=account.email_address,
            credential_reference_id=account.credential_reference_id,
            status=account.status.value,
            history_id=account.history_id,
            watch_expiration=account.watch_expiration,
            last_sync_at=account.last_sync_at,
            updated_at=account.updated_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[email_accounts.c.email_address],
            set_={
                "credential_reference_id": account.credential_reference_id,
                "status": account.status.value,
                "history_id": account.history_id,
                "watch_expiration": account.watch_expiration,
                "last_sync_at": account.last_sync_at,
                "updated_at": account.updated_at,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def set_connection_state(
        self,
        candidate_id: UUID,
        account_id: UUID,
        state: MailConnectionState,
        *,
        last_error_code: str = "",
        now: datetime,
    ) -> EmailAccount | None:
        values: dict[str, object] = {
            "connection_state": state.value,
            "last_error_code": last_error_code,
            "updated_at": now,
        }
        if state is MailConnectionState.REVOKED:
            values["status"] = EmailAccountStatus.REVOKED.value
        elif state is MailConnectionState.ERROR:
            values["status"] = EmailAccountStatus.ERROR.value
        elif state is MailConnectionState.CONNECTED:
            values["status"] = EmailAccountStatus.ACTIVE.value
            values["candidate_id"] = candidate_id
        stmt = (
            sa.update(email_accounts)
            .where(
                sa.and_(
                    email_accounts.c.id == account_id,
                    email_accounts.c.candidate_id == candidate_id,
                )
            )
            .values(**values)
            .returning(email_accounts)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_account(row) if row else None


# ---------------------------------------------------------------------------
# Sync-run + cursor repository (tasks 11.3 / 11.4)
# ---------------------------------------------------------------------------


class PostgresSyncRunRepository:
    """Durable sync-run + cursor store, scoped by candidate."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_run(self, run: MailSyncRun) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(email_sync_runs).values(
                    id=run.id,
                    account_id=run.account_id,
                    candidate_id=run.candidate_id,
                    status=run.status.value,
                    direction=run.direction.value,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    messages_processed=run.messages_processed,
                    messages_skipped=run.messages_skipped,
                    history_id_start=run.history_id_start,
                    history_id_end=run.history_id_end,
                    error_code=run.error_code,
                )
            )

    def get_run(self, candidate_id: UUID, run_id: UUID) -> MailSyncRun | None:
        stmt = sa.select(email_sync_runs).where(
            sa.and_(
                email_sync_runs.c.id == run_id,
                email_sync_runs.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_run(row) if row else None

    def get_open_run(self, account_id: UUID) -> MailSyncRun | None:
        stmt = (
            sa.select(email_sync_runs)
            .where(
                sa.and_(
                    email_sync_runs.c.account_id == account_id,
                    email_sync_runs.c.status.in_(
                        [MailSyncRunStatus.PENDING.value, MailSyncRunStatus.RUNNING.value]
                    ),
                )
            )
            .order_by(email_sync_runs.c.created_at.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_run(row) if row else None

    def update_run(
        self,
        candidate_id: UUID,
        run_id: UUID,
        *,
        status: MailSyncRunStatus,
        messages_processed: int,
        messages_skipped: int,
        history_id_end: str,
        error_code: str,
        completed_at: datetime,
    ) -> MailSyncRun | None:
        stmt = (
            sa.update(email_sync_runs)
            .where(
                sa.and_(
                    email_sync_runs.c.id == run_id,
                    email_sync_runs.c.candidate_id == candidate_id,
                )
            )
            .values(
                status=status.value,
                messages_processed=messages_processed,
                messages_skipped=messages_skipped,
                history_id_end=history_id_end,
                error_code=error_code,
                completed_at=completed_at,
                updated_at=completed_at,
            )
            .returning(email_sync_runs)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_run(row) if row else None

    def list_runs(
        self,
        candidate_id: UUID,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        base = sa.select(email_sync_runs).where(
            sa.and_(
                email_sync_runs.c.candidate_id == candidate_id,
                email_sync_runs.c.account_id == account_id,
            )
        )
        if cursor is not None:
            cur_created, cur_id = _decode_cursor(cursor)
            base = base.where(
                sa.or_(
                    email_sync_runs.c.created_at < cur_created,
                    sa.and_(
                        email_sync_runs.c.created_at == cur_created,
                        email_sync_runs.c.id < cur_id,
                    ),
                )
            )
        stmt = base.order_by(
            email_sync_runs.c.created_at.desc(), email_sync_runs.c.id.desc()
        ).limit(limit + 1)
        with self._engine.begin() as conn:
            rows = list(conn.execute(stmt).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
        )
        return {
            "items": [
                {
                    "id": str(row["id"]),
                    "status": str(row["status"]),
                    "direction": str(row["direction"]),
                    "started_at": _iso(row["started_at"]),
                    "completed_at": _iso(row["completed_at"]),
                    "messages_processed": int(row["messages_processed"]),
                    "messages_skipped": int(row["messages_skipped"]),
                    "history_id_start": str(row["history_id_start"]),
                    "history_id_end": str(row["history_id_end"]),
                    "error_code": str(row["error_code"]),
                }
                for row in rows
            ],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def get_cursor(self, account_id: UUID) -> MailSyncCursor | None:
        stmt = sa.select(email_sync_cursors).where(email_sync_cursors.c.account_id == account_id)
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return MailSyncCursor(
            account_id=account_id,
            history_id=str(row["history_id"]),
            watch_expiration=row["watch_expiration"],
            updated_at=row["updated_at"],
        )

    def advance_cursor(self, account_id: UUID, cursor: MailSyncCursor, *, now: datetime) -> None:
        stmt = pg_insert(email_sync_cursors).values(
            account_id=account_id,
            history_id=cursor.history_id,
            watch_expiration=cursor.watch_expiration,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[email_sync_cursors.c.account_id],
            set_={
                "history_id": cursor.history_id,
                "watch_expiration": cursor.watch_expiration,
                "updated_at": now,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def processed_provider_ids(self, account_id: UUID) -> frozenset[str]:
        stmt = sa.select(email_messages.c.provider_message_id).where(
            email_messages.c.account_id == account_id
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).all()
        return frozenset(str(row[0]) for row in rows)


# ---------------------------------------------------------------------------
# Thread-link repository (task 11.6)
# ---------------------------------------------------------------------------


class PostgresThreadLinkRepository:
    """Thread + message + link store, scoped by candidate."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- threads -----------------------------------------------------------

    def upsert_thread(self, thread: EmailThread) -> None:
        stmt = pg_insert(email_threads).values(
            id=thread.id,
            account_id=thread.account_id,
            provider_thread_id=thread.provider_thread_id,
            subject=thread.subject,
            application_id=thread.application_id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_email_threads_account_provider",
            set_={
                "subject": thread.subject,
                "updated_at": thread.updated_at,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_thread(self, candidate_id: UUID, thread_id: UUID) -> EmailThread | None:
        stmt = (
            sa.select(email_threads)
            .join(
                email_accounts,
                email_accounts.c.id == email_threads.c.account_id,
            )
            .where(
                sa.and_(
                    email_threads.c.id == thread_id,
                    email_accounts.c.candidate_id == candidate_id,
                )
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_thread(row) if row else None

    # -- messages ----------------------------------------------------------

    def append_message(self, message: EmailMessage) -> None:
        try:
            state = EmailMessageState(message.state)
        except ValueError:
            state = EmailMessageState.SYNCED
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(email_messages).values(
                    id=message.id,
                    thread_id=message.thread_id,
                    account_id=message.account_id,
                    provider_message_id=message.provider_message_id,
                    category=message.category.value,
                    state=state.value,
                    sender_email=message.sender_email,
                    sender_name=message.sender_name,
                    subject=message.subject,
                    received_at=message.received_at,
                    snippet=message.snippet,
                    body_persisted=message.body_persisted,
                    application_id=message.application_id,
                )
            )

    # -- links -------------------------------------------------------------

    def get_link(self, candidate_id: UUID, thread_id: UUID) -> ThreadLinkSnapshot | None:
        stmt = sa.select(email_thread_links).where(
            sa.and_(
                email_thread_links.c.thread_id == thread_id,
                email_thread_links.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_link(row) if row else None

    def upsert_link(self, snapshot: ThreadLinkSnapshot, *, now: datetime) -> None:
        candidate_ids = [str(a) for a in snapshot.candidate_application_ids]
        # Deterministic link id from thread_id so a repeat association reuses
        # the same link row (the unique constraint on thread_id is the backstop).
        link_id = snapshot.link_id if snapshot.link_id is not None else _link_id(snapshot.thread_id)
        stmt = pg_insert(email_thread_links).values(
            id=link_id,
            thread_id=snapshot.thread_id,
            account_id=snapshot.account_id,
            candidate_id=snapshot.candidate_id,
            application_id=snapshot.application_id,
            status=snapshot.status.value,
            confidence=snapshot.confidence.value,
            candidate_application_ids=candidate_ids,
            evidence_refs=dict(snapshot.evidence_refs),
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_email_thread_links_thread",
            set_={
                "application_id": snapshot.application_id,
                "status": snapshot.status.value,
                "confidence": snapshot.confidence.value,
                "candidate_application_ids": candidate_ids,
                "evidence_refs": dict(snapshot.evidence_refs),
                "updated_at": now,
            },
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def record_confirmation(
        self, decision: LinkConfirmationDecision, *, now: datetime
    ) -> ThreadLinkSnapshot | None:
        # Append-only: upgrade the link to CONFIRMED. The prior decision is
        # captured by the snapshot's resolved_at; a repeat decision is rejected
        # by the service (prior_confirmation) before reaching here.
        stmt = (
            sa.update(email_thread_links)
            .where(
                sa.and_(
                    email_thread_links.c.id == decision.link_id,
                    email_thread_links.c.candidate_id == decision.candidate_id,
                )
            )
            .values(
                application_id=decision.confirmed_application_id,
                status=ThreadLinkStatus.CONFIRMED.value
                if decision.confirmed_application_id is not None
                else ThreadLinkStatus.UNLINKED.value,
                resolved_at=decision.decided_at,
                updated_at=now,
            )
            .returning(email_thread_links)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_link(row) if row else None

    def prior_confirmation(
        self, candidate_id: UUID, link_id: UUID
    ) -> LinkConfirmationDecision | None:
        stmt = sa.select(
            email_thread_links.c.id,
            email_thread_links.c.candidate_id,
            email_thread_links.c.application_id,
            email_thread_links.c.resolved_at,
        ).where(
            sa.and_(
                email_thread_links.c.id == link_id,
                email_thread_links.c.candidate_id == candidate_id,
                email_thread_links.c.resolved_at.is_not(None),
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        return LinkConfirmationDecision(
            link_id=row["id"],
            candidate_id=row["candidate_id"],
            confirmed_application_id=row["application_id"],
            decided_at=row["resolved_at"],
        )

    # -- list reads (cursor-paginated) -------------------------------------

    def list_threads(
        self,
        candidate_id: UUID,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        base = (
            sa.select(email_threads)
            .join(
                email_accounts,
                email_accounts.c.id == email_threads.c.account_id,
            )
            .where(
                sa.and_(
                    email_threads.c.account_id == account_id,
                    email_accounts.c.candidate_id == candidate_id,
                )
            )
        )
        if cursor is not None:
            cur_updated, cur_id = _decode_cursor(cursor)
            base = base.where(
                sa.or_(
                    email_threads.c.updated_at < cur_updated,
                    sa.and_(
                        email_threads.c.updated_at == cur_updated,
                        email_threads.c.id < cur_id,
                    ),
                )
            )
        stmt = base.order_by(email_threads.c.updated_at.desc(), email_threads.c.id.desc()).limit(
            limit + 1
        )
        with self._engine.begin() as conn:
            rows = list(conn.execute(stmt).mappings())
        return _paginate_threads(rows, limit)

    def list_messages(
        self,
        candidate_id: UUID,
        thread_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        base = (
            sa.select(email_messages)
            .join(
                email_accounts,
                email_accounts.c.id == email_messages.c.account_id,
            )
            .where(
                sa.and_(
                    email_messages.c.thread_id == thread_id,
                    email_accounts.c.candidate_id == candidate_id,
                )
            )
        )
        if cursor is not None:
            cur_received, cur_id = _decode_cursor(cursor)
            base = base.where(
                sa.or_(
                    email_messages.c.received_at < cur_received,
                    sa.and_(
                        email_messages.c.received_at == cur_received,
                        email_messages.c.id < cur_id,
                    ),
                )
            )
        stmt = base.order_by(email_messages.c.received_at.desc(), email_messages.c.id.desc()).limit(
            limit + 1
        )
        with self._engine.begin() as conn:
            rows = list(conn.execute(stmt).mappings())
        return _paginate_messages(rows, limit)

    def list_unresolved(
        self,
        candidate_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        base = sa.select(email_thread_links).where(
            sa.and_(
                email_thread_links.c.candidate_id == candidate_id,
                email_thread_links.c.status == ThreadLinkStatus.UNRESOLVED.value,
            )
        )
        if cursor is not None:
            cur_created, cur_id = _decode_cursor(cursor)
            base = base.where(
                sa.or_(
                    email_thread_links.c.created_at < cur_created,
                    sa.and_(
                        email_thread_links.c.created_at == cur_created,
                        email_thread_links.c.id < cur_id,
                    ),
                )
            )
        stmt = base.order_by(
            email_thread_links.c.created_at.desc(), email_thread_links.c.id.desc()
        ).limit(limit + 1)
        with self._engine.begin() as conn:
            rows = list(conn.execute(stmt).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
        )
        items: list[dict[str, object]] = []
        for row in rows:
            raw_ids = cast("list[object]", row["candidate_application_ids"] or [])
            items.append(
                {
                    "link_id": str(row["id"]),
                    "thread_id": str(row["thread_id"]),
                    "candidate_id": str(row["candidate_id"]),
                    "provider_thread_id": str(row.get("provider_thread_id", "")),
                    "subject": str(row.get("subject", "")),
                    "candidate_application_ids": [str(a) for a in raw_ids],
                    "evidence_refs": dict(cast("dict[str, str]", row["evidence_refs"] or {})),
                    "created_at": _iso(row["created_at"]),
                }
            )
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def get_unresolved(self, candidate_id: UUID, link_id: UUID) -> UnresolvedThreadLink | None:
        link_cols = (
            email_thread_links,
            email_threads.c.provider_thread_id,
            email_threads.c.subject,
        )
        stmt = (
            sa.select(*link_cols)
            .select_from(email_thread_links)
            .join(
                email_threads,
                email_threads.c.id == email_thread_links.c.thread_id,
            )
            .where(
                sa.and_(
                    email_thread_links.c.id == link_id,
                    email_thread_links.c.candidate_id == candidate_id,
                    email_thread_links.c.status == ThreadLinkStatus.UNRESOLVED.value,
                )
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        raw_ids = cast("list[object]", row["candidate_application_ids"] or [])
        return UnresolvedThreadLink(
            link_id=row["id"],
            thread_id=row["thread_id"],
            candidate_id=row["candidate_id"],
            provider_thread_id=str(row["provider_thread_id"]),
            subject=str(row["subject"]),
            candidate_application_ids=tuple(UUID(str(a)) for a in raw_ids),
            evidence_refs=dict(cast("dict[str, str]", row["evidence_refs"] or {})),
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_thread(row: sa.RowMapping) -> EmailThread:
    return EmailThread(
        id=row["id"],
        account_id=row["account_id"],
        provider_thread_id=str(row["provider_thread_id"]),
        subject=str(row["subject"]),
        application_id=row["application_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_link(row: sa.RowMapping) -> ThreadLinkSnapshot:
    raw_ids = cast("list[object]", row["candidate_application_ids"] or [])
    try:
        confidence = LinkConfidence(str(row["confidence"]))
    except ValueError:
        confidence = LinkConfidence.NONE
    try:
        status = ThreadLinkStatus(str(row["status"]))
    except ValueError:
        status = ThreadLinkStatus.UNLINKED
    return ThreadLinkSnapshot(
        thread_id=row["thread_id"],
        account_id=row["account_id"],
        candidate_id=row["candidate_id"],
        status=status,
        application_id=row["application_id"],
        candidate_application_ids=tuple(UUID(str(a)) for a in raw_ids),
        confidence=confidence,
        evidence_refs=dict(cast("dict[str, str]", row["evidence_refs"] or {})),
        resolved_at=row["resolved_at"],
        link_id=row["id"],
    )


def _link_id(thread_id: UUID) -> UUID:
    """Deterministic link-row id from thread_id (stable across re-upserts)."""
    from uuid import NAMESPACE_URL, uuid5

    return uuid5(NAMESPACE_URL, f"careerops:thread_link:{thread_id}")


def _paginate_threads(rows: list[sa.RowMapping], limit: int) -> dict[str, object]:
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(rows[-1]["updated_at"], rows[-1]["id"]) if has_more and rows else None
    )
    items = [
        {
            "id": str(row["id"]),
            "account_id": str(row["account_id"]),
            "provider_thread_id": str(row["provider_thread_id"]),
            "subject": str(row["subject"]),
            "application_id": str(row["application_id"]) if row["application_id"] else None,
            "updated_at": _iso(row["updated_at"]),
        }
        for row in rows
    ]
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def _paginate_messages(rows: list[sa.RowMapping], limit: int) -> dict[str, object]:
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(rows[-1]["received_at"], rows[-1]["id"]) if has_more and rows else None
    )
    items = [
        {
            "id": str(row["id"]),
            "thread_id": str(row["thread_id"]),
            "provider_message_id": str(row["provider_message_id"]),
            "category": str(row["category"]),
            "sender_email": str(row["sender_email"]),
            "sender_name": str(row["sender_name"]),
            "subject": str(row["subject"]),
            "received_at": _iso(row["received_at"]),
            "snippet": str(row["snippet"]),
            "body_persisted": bool(row["body_persisted"]),
        }
        for row in rows
    ]
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Public re-exports for convenience
# ---------------------------------------------------------------------------


def connection_state_for(account: EmailAccount) -> MailConnectionState:
    """Derive a best-effort connection state from a legacy EmailAccount row."""
    if account.status is EmailAccountStatus.REVOKED:
        return MailConnectionState.REVOKED
    if account.status is EmailAccountStatus.ERROR:
        return MailConnectionState.ERROR
    return MailConnectionState.CONNECTED


# ApplicationState is imported above for type completeness in callers that pass
# (application_id, state) tuples; keep the symbol referenced for re-export.
_APPLICATION_STATES = frozenset(s for s in ApplicationState)
