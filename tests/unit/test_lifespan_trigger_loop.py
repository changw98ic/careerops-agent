"""Tests for the API lifespan trigger-loop bootstrap wiring (Phase 2).

The lifespan calls ``_bootstrap_trigger_loop_safe`` only when the readiness
probe is a real ``RuntimeResources``; that helper builds the collaborators
(``ScheduleManager`` + ``CrawlActivationService``) from the runtime's existing
repos and delegates to ``bootstrap_trigger_loop``. These tests cover the
construction/delegation and the best-effort retry semantics without standing
up real infra.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


def test_run_trigger_loop_bootstrap_constructs_and_delegates(monkeypatch) -> None:
    from careerops.api.app import _run_trigger_loop_bootstrap

    async def fake_get_client():
        return "fake-client"

    probe = MagicMock()
    probe.get_temporal_client = fake_get_client
    probe.tier2_budget = object()
    probe.source_queue_service = object()

    captured: dict = {}

    class _FakeSM:
        def __init__(self, client):
            captured["client"] = client

    class _FakeActivator:
        def __init__(self, sm, *, task_queue):
            captured["task_queue"] = task_queue

    class _FakeActivation:
        def __init__(
            self,
            *,
            schedule_activator,
            budget_checker,
            eligibility_checker,
            inbox_projector=None,
        ):
            captured["budget"] = budget_checker
            captured["eligibility"] = eligibility_checker

    async def fake_bootstrap(settings, runtime, *, schedule_manager, crawl_activation_service):
        captured["sm_passed"] = schedule_manager
        captured["act_passed"] = crawl_activation_service
        return "REPORT"

    monkeypatch.setattr(
        "careerops.infrastructure.temporal.schedule_manager.ScheduleManager", _FakeSM
    )
    monkeypatch.setattr(
        "careerops.application.crawl_activation.TemporalScheduleActivator", _FakeActivator
    )
    monkeypatch.setattr(
        "careerops.application.crawl_activation.CrawlActivationService", _FakeActivation
    )
    monkeypatch.setattr(
        "careerops.application.loop_bootstrap.bootstrap_trigger_loop", fake_bootstrap
    )

    report = asyncio.run(_run_trigger_loop_bootstrap(probe, MagicMock()))
    assert report == "REPORT"
    assert captured["client"] == "fake-client"
    assert captured["task_queue"] == "careerops-m0"
    assert captured["budget"] is probe.tier2_budget
    assert captured["eligibility"] is probe.source_queue_service
    assert isinstance(captured["sm_passed"], _FakeSM)
    assert isinstance(captured["act_passed"], _FakeActivation)


def test_bootstrap_safe_swallows_failure_and_does_not_crash(monkeypatch) -> None:
    from careerops.api.app import _bootstrap_trigger_loop_safe

    calls = {"n": 0}

    async def always_raises(probe, settings):
        calls["n"] += 1
        raise RuntimeError("temporal down")

    # Patch the module global the safe helper calls.
    monkeypatch.setattr("careerops.api.app._run_trigger_loop_bootstrap", always_raises)

    app = MagicMock()
    # Must not raise despite 3 failed attempts.
    asyncio.run(_bootstrap_trigger_loop_safe(MagicMock(), MagicMock(), app))
    assert calls["n"] == 3  # retried 3 times then gave up


def test_bootstrap_safe_stores_report_on_success(monkeypatch) -> None:
    from careerops.api.app import _bootstrap_trigger_loop_safe

    async def ok(probe, settings):
        return "REPORT"

    monkeypatch.setattr("careerops.api.app._run_trigger_loop_bootstrap", ok)
    app = MagicMock()
    asyncio.run(_bootstrap_trigger_loop_safe(MagicMock(), MagicMock(), app))
    assert app.state.trigger_loop_report == "REPORT"
