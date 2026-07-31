"""Unit tests for the Phase 2 trigger-loop bootstrap.

The bootstrap is pure orchestration over an injected ``ScheduleManager`` and
``CrawlActivationService``; these tests fake both and assert the gate logic:
outbox+sweep always register, mail is gated on oauth+account, crawl is gated on
a resolved owner + the readiness gate.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from careerops.application.loop_bootstrap import (
    APPROVAL_SWEEP_INTERVAL,
    MAIL_SYNC_INTERVAL,
    OUTBOX_DRAIN_INTERVAL,
    bootstrap_trigger_loop,
)

_OWNER = UUID("11111111-1111-1111-1111-111111111111")
_CANDIDATE = UUID("22222222-2222-2222-2222-222222222222")
_ACCOUNT = UUID("33333333-3333-3333-3333-333333333333")


@dataclass
class _FakeActivationResult:
    sources_considered: int = 0
    schedules_activated: int = 0


class _FakeActivationService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def activate_crawl_schedules(self, readiness, owner_id, sources):
        self.calls.append((readiness.ready, owner_id, list(sources)))
        return _FakeActivationResult(
            sources_considered=len(sources), schedules_activated=len(sources)
        )


class _FakeScheduleManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def ensure_schedule(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True


def _fake_engine(*, plan_rows, mail_rows) -> MagicMock:
    """Engine whose .begin() yields a conn branching on the queried table."""

    class _Result:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class _Conn:
        def execute(self, stmt):
            text = str(stmt)
            if "crawl_plan_versions" in text:
                return _Result(plan_rows)
            if "email_accounts" in text:
                return _Result(mail_rows)
            return _Result([])

    conn = _Conn()

    class _Engine:
        def begin(self):
            @contextlib.contextmanager
            def _cm():
                yield conn
            return _cm()

    return _Engine()


def _runtime(*, plan_rows=(), mail_rows=(), crawl=False) -> tuple[MagicMock, _FakeActivationService | None]:
    runtime = MagicMock()
    runtime.database = _fake_engine(plan_rows=plan_rows, mail_rows=mail_rows)
    runtime.tier2_budget.max_concurrent_slots = 3
    runtime.tier2_budget.daily_action_budget = 500
    runtime.crawl_permission_repo = object() if crawl else None
    runtime.source_queue_service.select_sources.return_value = ["src-1", "src-2"]
    activation = _FakeActivationService() if crawl else None
    return runtime, activation


def _ids(mgr: _FakeScheduleManager) -> list[str]:
    return [c["schedule_id"] for c in mgr.calls]


def test_outbox_and_sweep_always_registered_and_mail_off_when_oauth_disabled():
    runtime, _ = _runtime()
    mgr = _FakeScheduleManager()
    settings = MagicMock(google_oauth_enabled=False)
    report = asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))
    ids = _ids(mgr)
    assert "outbox-drain" in ids
    assert "approval-sweep" in ids
    assert "mail-sync" not in ids
    assert report.outbox_registered and report.sweep_registered
    assert not report.mail_registered


def test_intervals_match_plan_defaults():
    runtime, _ = _runtime()
    mgr = _FakeScheduleManager()
    asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    by_id = {c["schedule_id"]: c for c in mgr.calls}
    assert by_id["outbox-drain"]["interval"] == OUTBOX_DRAIN_INTERVAL
    assert by_id["approval-sweep"]["interval"] == APPROVAL_SWEEP_INTERVAL


def test_mail_registered_when_oauth_and_single_account_present():
    runtime, _ = _runtime(mail_rows=[(str(_CANDIDATE), str(_ACCOUNT))])
    mgr = _FakeScheduleManager()
    asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=True), runtime, schedule_manager=mgr)
    )
    by_id = {c["schedule_id"]: c for c in mgr.calls}
    assert "mail-sync" in by_id
    assert by_id["mail-sync"]["interval"] == MAIL_SYNC_INTERVAL
    arg = by_id["mail-sync"]["arg"]
    assert arg.candidate_id == str(_CANDIDATE)
    assert arg.account_id == str(_ACCOUNT)


def test_mail_skipped_when_no_account():
    runtime, _ = _runtime(mail_rows=[])
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=True), runtime, schedule_manager=mgr)
    )
    assert "mail-sync" not in _ids(mgr)
    assert not report.mail_registered
    assert any("no mail account" in s for s in report.skipped)


def test_crawl_skipped_when_no_activation_service():
    runtime, _ = _runtime(plan_rows=[(str(_OWNER),)], crawl=False)
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    assert any("activation service not provided" in s for s in report.skipped)


def test_crawl_activates_when_owner_resolved_and_ready(monkeypatch):
    from careerops.application.crawl_readiness import CrawlReadiness

    monkeypatch.setattr(
        "careerops.application.loop_bootstrap.check_crawl_readiness",
        lambda *a, **k: CrawlReadiness(ready=True),
    )
    runtime, activation = _runtime(plan_rows=[(str(_OWNER),)], crawl=True)
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(
            MagicMock(google_oauth_enabled=False),
            runtime,
            schedule_manager=mgr,
            crawl_activation_service=activation,
        )
    )
    assert activation.calls and activation.calls[0][0] is True  # readiness.ready
    assert activation.calls[0][1] == _OWNER
    runtime.source_queue_service.select_sources.assert_called_once_with(_OWNER)
    assert report.crawl_schedules_considered == 2
    assert report.crawl_activated == 2


def test_crawl_skipped_when_multiple_owners():
    # Two distinct owners -> cannot resolve the single user -> crawl skipped.
    runtime, activation = _runtime(
        plan_rows=[(str(_OWNER),), (str(_CANDIDATE),)], crawl=True
    )
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(
            MagicMock(google_oauth_enabled=False),
            runtime,
            schedule_manager=mgr,
            crawl_activation_service=activation,
        )
    )
    assert activation.calls == []
    assert any("no single owner" in s for s in report.skipped)
