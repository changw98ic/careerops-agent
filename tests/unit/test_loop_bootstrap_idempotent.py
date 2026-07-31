"""Task 2.5: restart-idempotency for the trigger-loop bootstrap.

A worker/API restart calls ``bootstrap_trigger_loop`` again. Because every
schedule goes through ``ScheduleManager.ensure_schedule`` (create-or-reconcile),
the second pass must converge on the same schedule set rather than creating
duplicates. This test fakes the manager to record create-vs-reconcile and
asserts convergence across two passes.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

from careerops.application.loop_bootstrap import (
    MAIL_SYNC_INTERVAL,
    OUTBOX_DRAIN_INTERVAL,
    bootstrap_trigger_loop,
)


class _RecordingManager:
    """Fake ScheduleManager: first call per id 'creates', later calls 'reconcile'."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.created = 0
        self.updated = 0
        self.last_spec: dict[str, dict] = {}

    async def ensure_schedule(self, *, schedule_id, **kwargs) -> bool:
        if schedule_id in self.seen:
            self.updated += 1
        else:
            self.seen.add(schedule_id)
            self.created += 1
        self.last_spec[schedule_id] = kwargs
        return schedule_id not in self.seen  # True only on the create call


def _engine_returning(rows_map) -> MagicMock:
    class _Result:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class _Conn:
        def execute(self, stmt):
            text = str(stmt)
            for key, rows in rows_map.items():
                if key in text:
                    return _Result(rows)
            return _Result([])

    conn = _Conn()

    class _Engine:
        def begin(self):
            @contextlib.contextmanager
            def _cm():
                yield conn
            return _cm()

    return _Engine()


def _runtime_with(engine_rows) -> MagicMock:
    runtime = MagicMock()
    runtime.database = _engine_returning(engine_rows)
    return runtime


def test_double_bootstrap_converges_without_duplicates() -> None:
    # No mail account, no crawl activation -> only outbox + sweep register.
    runtime = _runtime_with({"crawl_plan_versions": [], "email_accounts": []})
    mgr = _RecordingManager()
    settings = MagicMock(google_oauth_enabled=False)

    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))
    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))

    # Exactly two schedule ids, each created once and reconciled once.
    assert mgr.seen == {"outbox-drain", "approval-sweep"}
    assert mgr.created == 2
    assert mgr.updated == 2


def test_reconciled_spec_is_identical_across_restarts() -> None:
    runtime = _runtime_with({"crawl_plan_versions": [], "email_accounts": []})
    mgr = _RecordingManager()
    settings = MagicMock(google_oauth_enabled=False)

    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))
    first_spec = dict(mgr.last_spec)
    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))

    # The second pass must supply the same cadence (no drift on restart).
    for schedule_id, spec in first_spec.items():
        assert mgr.last_spec[schedule_id]["interval"] == spec["interval"]
    assert mgr.last_spec["outbox-drain"]["interval"] == OUTBOX_DRAIN_INTERVAL


def test_mail_schedule_stable_across_restarts_when_account_present() -> None:
    candidate = "22222222222222222222222222222222"
    account = "33333333333333333333333333333333"
    runtime = _runtime_with(
        {"crawl_plan_versions": [], "email_accounts": [(candidate, account)]}
    )
    mgr = _RecordingManager()
    settings = MagicMock(google_oauth_enabled=True)

    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))
    asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))

    assert "mail-sync" in mgr.seen
    # All three (outbox + sweep + mail) created once, then reconciled on pass 2.
    assert mgr.created == 3
    assert mgr.updated == 3
    assert mgr.last_spec["mail-sync"]["interval"] == MAIL_SYNC_INTERVAL
