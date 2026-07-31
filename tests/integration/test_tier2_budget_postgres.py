"""Real PostgreSQL verification for the shared Tier 2 budget coordinator."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from careerops.infrastructure.database.postgres_tier2_budget import (
    PostgresTier2Budget,
)
from careerops.infrastructure.database.schema import (
    tier2_budget_daily,
    tier2_budget_leases,
)


@pytest.fixture()
def database_engine() -> sa.Engine:
    url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required")
    engine = sa.create_engine(url, pool_size=20, max_overflow=20)
    with engine.begin() as conn:
        conn.execute(tier2_budget_leases.delete())
        conn.execute(tier2_budget_daily.delete())
    yield engine
    with engine.begin() as conn:
        conn.execute(tier2_budget_leases.delete())
        conn.execute(tier2_budget_daily.delete())
    engine.dispose()


def test_concurrent_workers_never_exceed_slot_limit(
    database_engine: sa.Engine,
) -> None:
    worker_count = 16
    barrier = threading.Barrier(worker_count)

    def acquire(worker: int) -> bool:
        coordinator = PostgresTier2Budget(
            database_engine,
            max_concurrent_slots=3,
            daily_action_budget=100,
        )
        barrier.wait()
        return coordinator.acquire(f"source-{worker}") is not None

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        acquired = list(pool.map(acquire, range(worker_count)))

    assert sum(acquired) == 3
    assert PostgresTier2Budget(
        database_engine,
        max_concurrent_slots=3,
        daily_action_budget=100,
    ).active_slots == 3


def test_concurrent_workers_never_overrun_daily_actions(
    database_engine: sa.Engine,
) -> None:
    worker_count = 100
    barrier = threading.Barrier(worker_count)

    def consume(_: int) -> int:
        coordinator = PostgresTier2Budget(
            database_engine,
            max_concurrent_slots=10,
            daily_action_budget=37,
        )
        barrier.wait()
        return coordinator.consume(1)

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        consumed = list(pool.map(consume, range(worker_count)))

    assert sum(consumed) == 37
    coordinator = PostgresTier2Budget(
        database_engine,
        max_concurrent_slots=10,
        daily_action_budget=37,
    )
    assert coordinator.daily_consumed == 37
    assert coordinator.daily_remaining == 0


def test_new_worker_recovers_orphaned_leases(
    database_engine: sa.Engine,
) -> None:
    first_worker = PostgresTier2Budget(
        database_engine,
        max_concurrent_slots=2,
        daily_action_budget=10,
    )
    assert first_worker.acquire("orphan-a") is not None
    assert first_worker.acquire("orphan-b") is not None

    restarted_worker = PostgresTier2Budget(
        database_engine,
        max_concurrent_slots=2,
        daily_action_budget=10,
    )
    assert restarted_worker.recover_stale_leases(active_source_ids=set()) == 2
    assert restarted_worker.available_slots == 2
