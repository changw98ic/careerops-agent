"""Unit tests for the Phase 2 trigger-loop bootstrap.

The bootstrap is pure orchestration over an injected ``ScheduleManager`` and
``CrawlActivationService``; these tests fake both and assert the gate logic:
outbox+sweep always register, mail is gated on oauth+connected accounts (one
``mail-sync:<candidate_id>`` schedule per connected account), crawl is gated
on the readiness gate and iterates EVERY candidate, scoped by each candidate's
ACTIVE crawl plan (only the sources selected by the plan get schedules;
candidates without an active plan are skipped). Legacy pre-multi-candidate
schedules (``crawl:{source_id}`` / fixed ``mail-sync``) are deleted on boot.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, call
from uuid import UUID

import pytest

from careerops.application.loop_bootstrap import (
    APPROVAL_SWEEP_INTERVAL,
    MAIL_SYNC_INTERVAL,
    OUTBOX_DRAIN_INTERVAL,
    bootstrap_trigger_loop,
    is_legacy_schedule_id,
)

_CANDIDATE = UUID("22222222-2222-2222-2222-222222222222")
_CANDIDATE2 = UUID("44444444-4444-4444-4444-444444444444")
_ACCOUNT = UUID("33333333-3333-3333-3333-333333333333")
_ACCOUNT2 = UUID("55555555-5555-5555-5555-555555555555")

_SRC1 = UUID("11111111-1111-1111-1111-111111111111")
_SRC2 = UUID("99999999-9999-9999-9999-999999999999")


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


class _RaisingActivationService(_FakeActivationService):
    """Activation that fails for one specific owner (per-candidate isolation)."""

    def __init__(self, failing_owner: UUID) -> None:
        super().__init__()
        self._failing = failing_owner

    async def activate_crawl_schedules(self, readiness, owner_id, sources):
        if owner_id == self._failing:
            raise RuntimeError("boom")
        return await super().activate_crawl_schedules(readiness, owner_id, sources)


class _FakeScheduleManager:
    def __init__(self, *, existing: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.existing: list[str] = list(existing or [])
        self.deleted: list[str] = []

    async def ensure_schedule(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True

    async def list_schedule_ids(self) -> list[str]:
        return list(self.existing)

    async def delete(self, schedule_id: str) -> bool:
        self.deleted.append(schedule_id)
        if schedule_id in self.existing:
            self.existing.remove(schedule_id)
            return True
        return False


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


def _plan(*, source_ids=()):
    """A fake CrawlPlanVersion carrying a ``sources`` tuple of ids."""
    return SimpleNamespace(sources=tuple(source_ids))


def _runtime(
    *,
    candidate_rows=(),
    mail_rows=(),
    crawl=False,
    plans: dict[UUID, object] | None = None,
    source_ids=(_SRC1, _SRC2),
    activation=None,
) -> tuple[MagicMock, _FakeActivationService | None]:
    runtime = MagicMock()
    runtime.database = _fake_engine(candidate_rows=candidate_rows, mail_rows=mail_rows)
    runtime.tier2_budget.max_concurrent_slots = 3
    runtime.tier2_budget.daily_action_budget = 500
    runtime.crawl_permission_repo = object() if crawl else None
    runtime.source_queue_service.select_sources.return_value = [
        SimpleNamespace(id=sid) for sid in source_ids
    ]
    if crawl:
        active_plans = plans or {}
        runtime.crawl_plan_repo.get_active_for.side_effect = lambda cid: active_plans.get(cid)
        activation = activation or _FakeActivationService()
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


def _ready_readiness(monkeypatch):
    from careerops.application.crawl_readiness import CrawlReadiness

    monkeypatch.setattr(
        "careerops.application.loop_bootstrap.check_crawl_readiness",
        lambda *a, **k: CrawlReadiness(ready=True),
    )


def test_crawl_activated_for_each_candidate(monkeypatch):
    _ready_readiness(monkeypatch)
    runtime, activation = _runtime(
        candidate_rows=[(str(_CANDIDATE),), (str(_CANDIDATE2),)],
        crawl=True,
        plans={
            _CANDIDATE: _plan(source_ids=(_SRC1, _SRC2)),
            _CANDIDATE2: _plan(source_ids=(_SRC1, _SRC2)),
        },
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
    # No schedule id matches the OLD single-uuid crawl format (C1 assertion):
    # every registered crawl schedule must carry the candidate.
    import re

    for spec in mgr.calls:
        assert not re.fullmatch(r"crawl:[0-9a-fA-F-]{36}", spec["schedule_id"])


def test_crawl_skipped_for_candidate_without_active_plan(monkeypatch):
    # Only candidate2 has an active plan -> candidate1 gets no crawl schedules.
    _ready_readiness(monkeypatch)
    runtime, activation = _runtime(
        candidate_rows=[(str(_CANDIDATE),), (str(_CANDIDATE2),)],
        crawl=True,
        plans={_CANDIDATE2: _plan(source_ids=(_SRC1, _SRC2))},
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
    # Only the planned candidate is activated.
    assert [c[1] for c in activation.calls] == [_CANDIDATE2]
    assert report.crawl_schedules_considered == 2
    assert report.crawl_activated == 2
    assert any(str(_CANDIDATE) in s and "no active plan" in s for s in report.skipped)


def test_crawl_sources_filtered_by_active_plan(monkeypatch):
    # Candidate1's plan selects only _SRC1; candidate2's plan selects only
    # _SRC2 -> each activation pass receives only the planned source.
    _ready_readiness(monkeypatch)
    runtime, activation = _runtime(
        candidate_rows=[(str(_CANDIDATE),), (str(_CANDIDATE2),)],
        crawl=True,
        plans={
            _CANDIDATE: _plan(source_ids=(_SRC1,)),
            _CANDIDATE2: _plan(source_ids=(_SRC2,)),
        },
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
    by_owner = {c[1]: [s.id for s in c[2]] for c in activation.calls}
    assert by_owner == {_CANDIDATE: [_SRC1], _CANDIDATE2: [_SRC2]}
    assert report.crawl_schedules_considered == 2
    assert report.crawl_activated == 2


def test_one_failing_candidate_does_not_abort_the_round(monkeypatch):
    # Candidate1's activation raises; candidate2 must still be activated.
    _ready_readiness(monkeypatch)
    activation = _RaisingActivationService(failing_owner=_CANDIDATE)
    runtime, _ = _runtime(
        candidate_rows=[(str(_CANDIDATE),), (str(_CANDIDATE2),)],
        crawl=True,
        plans={
            _CANDIDATE: _plan(source_ids=(_SRC1,)),
            _CANDIDATE2: _plan(source_ids=(_SRC2,)),
        },
        activation=activation,
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
    # Candidate1 raised -> only candidate2 recorded a completed activation.
    assert [c[1] for c in activation.calls] == [_CANDIDATE2]
    assert report.crawl_activated == 1
    assert any(str(_CANDIDATE) in s and "failed" in s for s in report.skipped)


def test_legacy_schedules_deleted_on_boot(monkeypatch):
    # Pre-multi-candidate schedules (crawl:{uuid} without candidate, fixed
    # mail-sync) are deleted; per-candidate formats are kept.
    _ready_readiness(monkeypatch)
    legacy_crawl = f"crawl:{_SRC1}"
    legacy_mail = "mail-sync"
    fresh_crawl = f"crawl:{_CANDIDATE}:{_SRC1}"
    fresh_mail = f"mail-sync:{_CANDIDATE}"
    runtime, activation = _runtime(
        candidate_rows=[(str(_CANDIDATE),)],
        crawl=True,
        plans={_CANDIDATE: _plan(source_ids=(_SRC1, _SRC2))},
    )
    mgr = _FakeScheduleManager(
        existing=[legacy_crawl, legacy_mail, fresh_crawl, fresh_mail]
    )
    report = asyncio.run(
        bootstrap_trigger_loop(
            MagicMock(google_oauth_enabled=False),
            runtime,
            schedule_manager=mgr,
            crawl_activation_service=activation,
        )
    )
    assert set(mgr.deleted) == {legacy_crawl, legacy_mail}
    assert set(report.legacy_schedules_deleted) == {legacy_crawl, legacy_mail}
    # Fresh per-candidate schedules are never touched by the cleanup.
    assert fresh_crawl in mgr.existing
    assert fresh_mail in mgr.existing


def test_is_legacy_schedule_id_classification():
    assert is_legacy_schedule_id("mail-sync")
    assert is_legacy_schedule_id(f"crawl:{_SRC1}")
    assert not is_legacy_schedule_id(f"mail-sync:{_CANDIDATE}")
    assert not is_legacy_schedule_id(f"crawl:{_CANDIDATE}:{_SRC1}")
    assert not is_legacy_schedule_id("outbox-drain")


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
