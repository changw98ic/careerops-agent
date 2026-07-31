"""Unit tests for the Phase 2 trigger-loop bootstrap.

The bootstrap is pure orchestration over an injected ``ScheduleManager`` and
``CrawlActivationService``; these tests fake both and assert the gate logic:
outbox+sweep always register, mail is gated on oauth+connected accounts (one
``mail-sync:<candidate_id>`` schedule per connected account), crawl is gated
on the readiness gate and iterates EVERY candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from careerops.application.loop_bootstrap import (
    APPROVAL_SWEEP_INTERVAL,
    MAIL_SYNC_INTERVAL,
    OUTBOX_DRAIN_INTERVAL,
    bootstrap_trigger_loop,
)

_CANDIDATE = UUID("22222222-2222-2222-2222-222222222222")
_CANDIDATE2 = UUID("44444444-4444-4444-4444-444444444444")
_ACCOUNT = UUID("33333333-3333-3333-3333-333333333333")
_ACCOUNT2 = UUID("55555555-5555-5555-5555-555555555555")


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


def _fake_engine(*, candidate_rows, mail_rows) -> MagicMock:
    """Engine whose .begin() yields a conn branching on the queried table."""

    class _Result:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class _Conn:
        def execute(self, stmt):
            text = str(stmt)
            if "candidates" in text:
                return _Result(candidate_rows)
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


def _runtime(*, candidate_rows=(), mail_rows=(), crawl=False) -> tuple[MagicMock, _FakeActivationService | None]:
    runtime = MagicMock()
    runtime.database = _fake_engine(candidate_rows=candidate_rows, mail_rows=mail_rows)
    runtime.tier2_budget.max_concurrent_slots = 3
    runtime.tier2_budget.daily_action_budget = 500
    runtime.crawl_permission_repo = object() if crawl else None
    runtime.source_queue_service.select_sources.return_value = ["src-1", "src-2"]
    activation = _FakeActivationService() if crawl else None
    return runtime, activation


def _ids(mgr: _FakeScheduleManager) -> list[str]:
    return [c["schedule_id"] for c in mgr.calls]


def _mail_schedules(mgr: _FakeScheduleManager) -> dict[str, dict]:
    return {c["schedule_id"]: c for c in mgr.calls if c["schedule_id"].startswith("mail-sync")}


def test_outbox_and_sweep_always_registered_and_mail_off_when_oauth_disabled():
    runtime, _ = _runtime()
    mgr = _FakeScheduleManager()
    settings = MagicMock(google_oauth_enabled=False)
    report = asyncio.run(bootstrap_trigger_loop(settings, runtime, schedule_manager=mgr))
    ids = _ids(mgr)
    assert "outbox-drain" in ids
    assert "approval-sweep" in ids
    assert not any(i.startswith("mail-sync") for i in ids)
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


def test_mail_sync_schedule_per_candidate():
    # Two candidates, each with one connected account -> one mail-sync
    # schedule per candidate, each carrying that candidate's own account.
    runtime, _ = _runtime(
        mail_rows=[
            (str(_CANDIDATE), str(_ACCOUNT)),
            (str(_CANDIDATE2), str(_ACCOUNT2)),
        ]
    )
    mgr = _FakeScheduleManager()
    asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=True), runtime, schedule_manager=mgr)
    )
    schedules = _mail_schedules(mgr)
    assert set(schedules) == {f"mail-sync:{_CANDIDATE}", f"mail-sync:{_CANDIDATE2}"}
    for schedule_id, spec in schedules.items():
        assert spec["interval"] == MAIL_SYNC_INTERVAL
        assert spec["arg"].candidate_id == str(schedule_id.removeprefix("mail-sync:"))
    args_by_candidate = {
        str(cid): schedules[f"mail-sync:{cid}"]["arg"].account_id
        for cid in (_CANDIDATE, _CANDIDATE2)
    }
    assert args_by_candidate == {
        str(_CANDIDATE): str(_ACCOUNT),
        str(_CANDIDATE2): str(_ACCOUNT2),
    }


def test_mail_skipped_when_no_account():
    runtime, _ = _runtime(mail_rows=[])
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=True), runtime, schedule_manager=mgr)
    )
    assert not _mail_schedules(mgr)
    assert not report.mail_registered
    assert any("no mail account" in s for s in report.skipped)


def test_crawl_skipped_when_no_activation_service():
    runtime, _ = _runtime(crawl=False)
    mgr = _FakeScheduleManager()
    report = asyncio.run(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    assert any("activation service not provided" in s for s in report.skipped)


def test_crawl_activated_for_each_candidate(monkeypatch):
    from careerops.application.crawl_readiness import CrawlReadiness

    monkeypatch.setattr(
        "careerops.application.loop_bootstrap.check_crawl_readiness",
        lambda *a, **k: CrawlReadiness(ready=True),
    )
    runtime, activation = _runtime(
        candidate_rows=[(str(_CANDIDATE),), (str(_CANDIDATE2),)], crawl=True
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
    # One activation pass per candidate, in candidate order, all ready.
    assert [c[1] for c in activation.calls] == [_CANDIDATE, _CANDIDATE2]
    assert all(c[0] is True for c in activation.calls)  # readiness.ready
    # Each candidate's sources selected with that candidate id as owner.
    assert runtime.source_queue_service.select_sources.call_args_list == [
        call(_CANDIDATE),
        call(_CANDIDATE2),
    ]
    # Two sources per candidate -> 4 considered / 4 activated.
    assert report.crawl_schedules_considered == 4
    assert report.crawl_activated == 4


def test_crawl_skipped_when_no_candidates():
    # No candidates -> nothing to activate; crawl skipped fail-closed.
    runtime, activation = _runtime(crawl=True)
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
    assert any("no candidates" in s for s in report.skipped)
