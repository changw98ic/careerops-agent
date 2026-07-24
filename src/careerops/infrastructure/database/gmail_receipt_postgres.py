"""PostgreSQL-backed ``GmailReceiptStore`` for durable gmail send receipts.

Uses the ``careerops.gmail_send_receipts`` table defined in
``infrastructure.database.schema``.  Each method opens its own connection
and commits immediately (autocommit-equivalent); no long-lived transactions.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert

from careerops.infrastructure.database.schema import gmail_send_receipts
from careerops.integrations.gmail_receipt_store import GmailReceipt


class PostgresGmailReceiptStore:
    """SQLAlchemy-backed ``GmailReceiptStore`` implementation.

    All reads and writes go through the ``gmail_send_receipts`` table.
    ``put`` is idempotent: inserting a receipt with an existing
    ``reconciliation_key`` overwrites the previous row (``ON CONFLICT DO
    UPDATE``).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_by_reconciliation_key(self, key: str) -> GmailReceipt | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(gmail_send_receipts).where(
                        gmail_send_receipts.c.reconciliation_key == key,
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return GmailReceipt(
            reconciliation_key=str(row["reconciliation_key"]),
            provider_message_id=str(row["provider_message_id"]),
            thread_id=str(row["thread_id"]),
            sent_at=row["sent_at"],  # type: ignore[arg-type]
        )

    def put(self, receipt: GmailReceipt) -> None:
        stmt = (
            pg_insert(gmail_send_receipts)
            .values(
                reconciliation_key=receipt.reconciliation_key,
                provider_message_id=receipt.provider_message_id,
                thread_id=receipt.thread_id,
                sent_at=receipt.sent_at,
            )
            .on_conflict_do_update(
                index_elements=[gmail_send_receipts.c.reconciliation_key],
                set_={
                    "provider_message_id": receipt.provider_message_id,
                    "thread_id": receipt.thread_id,
                    "sent_at": receipt.sent_at,
                },
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def list(self) -> tuple[GmailReceipt, ...]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(gmail_send_receipts).order_by(
                        gmail_send_receipts.c.created_at,
                    )
                )
                .mappings()
                .fetchall()
            )
        return tuple(
            GmailReceipt(
                reconciliation_key=str(row["reconciliation_key"]),
                provider_message_id=str(row["provider_message_id"]),
                thread_id=str(row["thread_id"]),
                sent_at=row["sent_at"],  # type: ignore[arg-type]
            )
            for row in rows
        )
