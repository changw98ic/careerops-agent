"""Integration tests for ``PostgresGmailReceiptStore``.

Requires a running Postgres instance with the ``careerops_test`` database
and migrations applied (through ``0012_gmail_send_receipts``).

Run with: ``uv run python -m pytest tests/integration/test_gmail_durable.py``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from careerops.infrastructure.database.gmail_receipt_postgres import (
    PostgresGmailReceiptStore,
)
from careerops.integrations.gmail_receipt_store import GmailReceipt

pytestmark = pytest.mark.integration

DATABASE_URL = "postgresql+psycopg://careerops_runtime@127.0.0.1:5432/careerops_test"


@pytest.fixture(scope="module")
def engine() -> sa.Engine:
    eng = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_table(engine: sa.Engine) -> None:
    """Truncate ``gmail_send_receipts`` before each test."""
    with engine.begin() as conn:
        conn.execute(sa.text("TRUNCATE careerops.gmail_send_receipts RESTART IDENTITY CASCADE"))


@pytest.fixture()
def store(engine: sa.Engine) -> PostgresGmailReceiptStore:
    return PostgresGmailReceiptStore(engine)


def _receipt(
    key: str = "k:h",
    msg_id: str = "msg-1",
    thread_id: str = "thread-1",
    sent_at: datetime | None = None,
) -> GmailReceipt:
    return GmailReceipt(
        reconciliation_key=key,
        provider_message_id=msg_id,
        thread_id=thread_id,
        sent_at=sent_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


class TestPutAndGet:
    def test_put_then_get(self, store: PostgresGmailReceiptStore) -> None:
        receipt = _receipt()
        store.put(receipt)
        got = store.get_by_reconciliation_key("k:h")

        assert got is not None
        assert got.reconciliation_key == "k:h"
        assert got.provider_message_id == "msg-1"
        assert got.thread_id == "thread-1"
        assert got.sent_at == datetime(2026, 1, 1, tzinfo=UTC)

    def test_get_missing_key(self, store: PostgresGmailReceiptStore) -> None:
        assert store.get_by_reconciliation_key("nonexistent") is None

    def test_put_idempotent_overwrite(self, store: PostgresGmailReceiptStore) -> None:
        store.put(_receipt(msg_id="msg-old"))
        store.put(_receipt(msg_id="msg-new"))

        got = store.get_by_reconciliation_key("k:h")
        assert got is not None
        assert got.provider_message_id == "msg-new"


class TestList:
    def test_list_empty(self, store: PostgresGmailReceiptStore) -> None:
        assert store.list() == ()

    def test_list_multiple(self, store: PostgresGmailReceiptStore) -> None:
        store.put(_receipt(key="k1:h1", msg_id="m1"))
        store.put(_receipt(key="k2:h2", msg_id="m2"))

        results = store.list()
        assert len(results) == 2
        keys = {r.reconciliation_key for r in results}
        assert keys == {"k1:h1", "k2:h2"}


class TestDurableAcrossNewStoreInstance:
    """Simulates process restart: new store instance sees old data."""

    def test_survives_new_instance(self, engine: sa.Engine) -> None:
        store1 = PostgresGmailReceiptStore(engine)
        store1.put(_receipt(key="persist:key", msg_id="msg-persist"))

        # New instance (simulates restart)
        store2 = PostgresGmailReceiptStore(engine)
        got = store2.get_by_reconciliation_key("persist:key")

        assert got is not None
        assert got.provider_message_id == "msg-persist"
