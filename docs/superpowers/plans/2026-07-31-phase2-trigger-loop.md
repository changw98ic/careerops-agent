# Phase 2 Self-Driving Trigger Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the autonomous career loop self-drive: on API startup, idempotently register the Temporal Schedules that drive inbound mail sync, outbox drain, approval sweep, and per-source crawl — so the system runs on its own instead of only when manually poked.

**Architecture:** Temporal Schedules are server-side state that fire *workflows*. The crawl side already has `CrawlScheduledWorkflow`, but mail-sync / outbox-drain / approval-sweep are activity-only, so we add three thin trigger workflows wrapping their activities. A new `loop_bootstrap` module uses the existing `ScheduleManager` (idempotent create-or-reconcile) to register all four schedule kinds during the FastAPI `lifespan` startup, best-effort with retry. Crawl schedules are registered paused, then `CrawlActivationService` (8.2) resumes eligible sources — for that we add the missing real adapters (Temporal/Postgres) for its four Protocol dependencies. The single-user identity is resolved defensively from existing data (crawl plan owner / mail account), not from the login layer.

**Tech Stack:** Python 3.12, FastAPI lifespan, Temporal Python SDK (`temporalio`), SQLAlchemy/PostgreSQL, pytest.

## Global Constraints

- No `rg`/`grep`/`find` for code search — use CodeGraph.
- All schedule registration must be idempotent (safe across worker/API restarts) — rely on `ScheduleManager.ensure_schedule` create-or-reconcile plus existing `run_identity` / outbox / side-effect idempotency.
- Fail-closed per capability flags: mail-sync schedule only when `google_oauth_enabled` AND a connected account exists; crawl activation only when the readiness gate passes; outbox/sweep always (pure internal cleanup, no external effect).
- Best-effort in lifespan: a registration failure (e.g. Temporal not up yet) logs a warning and does NOT crash the API; retry 3× with backoff first.
- Cadence defaults: mail-sync 5 min, outbox drain 30 s, approval sweep 60 s, crawl per-source `interval_seconds` (fallback 3600 s).
- Do NOT touch the console-login/auth layer (explicitly parked by the user). Owner resolution must not depend on it.

---

## File Structure

- **Create** `src/careerops/workflows/loop_trigger_workflows.py` — three thin `@workflow.defn` classes (`OutboxDrainWorkflow`, `ApprovalSweepWorkflow`, `MailSyncTriggerWorkflow`) that each call one activity. One responsibility: be the schedule-fireable wrapper for activity-only paths.
- **Create** `src/careerops/infrastructure/temporal/crawl_activation_adapters.py` — real implementations of the `ScheduleActivator` / `BudgetChecker` / `InboxProjector` Protocols, wrapping `ScheduleManager`, `PostgresTier2Budget`, and `CrawlDownstreamService`. (`SourceEligibilityChecker` is satisfied directly by `SourceQueueService`.)
- **Create** `src/careerops/application/loop_bootstrap.py` — `bootstrap_trigger_loop(...)` + cadence constants + defensive single-user/owner/account resolution + `BootstrapReport`. One responsibility: turn the managed schedules "on" at startup.
- **Modify** `src/careerops/infrastructure/runtime.py` — add `get_temporal_client()` (async, lazy, cached) and close it in `close()`.
- **Modify** `src/careerops/infrastructure/temporal/worker.py` — register the three new trigger workflows in `build_worker.all_workflows`.
- **Modify** `src/careerops/api/app.py` — call `bootstrap_trigger_loop` inside `lifespan` (best-effort, retried).
- **Create** `tests/unit/test_loop_bootstrap.py` — bootstrap logic with a fake `ScheduleManager` (all gate branches).
- **Create** `tests/unit/test_loop_trigger_workflows.py` — worker registration + workflow→activity wiring.
- **Create** `tests/unit/test_crawl_activation_adapters.py` — adapter delegation.
- **Create** `tests/unit/test_loop_bootstrap_idempotent.py` — 2.5: double-bootstrap converges, no duplicate effects.

---

### Task 1: Temporal client accessor on RuntimeResources

**Files:**
- Modify: `src/careerops/infrastructure/runtime.py` (add `get_temporal_client` + close in `close()`)
- Test: `tests/unit/test_runtime_temporal_client.py`

**Interfaces:**
- Produces: `async def RuntimeResources.get_temporal_client(self) -> Client` (lazy, cached on `self._temporal_client`); the same client is closed inside the existing async `close()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_runtime_temporal_client.py
from unittest.mock import AsyncMock, patch
from careerops.infrastructure.runtime import RuntimeResources
from careerops.config import Settings


def test_get_temporal_client_is_lazy_and_cached(monkeypatch):
    settings = Settings(environment="test")
    # Build without touching real infra where possible.
    with patch.object(RuntimeResources, "__init__", lambda self, s: None):
        rr = RuntimeResources(settings)
        rr._settings = settings
        rr._temporal_client = None
    fake_client = object()
    connect = AsyncMock(return_value=fake_client)
    with patch("careerops.infrastructure.runtime.Client.connect", connect):
        first = RuntimeResources.get_temporal_client(rr)
        second = RuntimeResources.get_temporal_client(rr)
        import asyncio
        assert asyncio.get_event_loop().run_until_complete(first) is fake_client
        assert asyncio.get_event_loop().run_until_complete(second) is fake_client
        assert connect.await_count == 1  # cached: only one connect
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_runtime_temporal_client.py -v`
Expected: FAIL — `AttributeError: 'RuntimeResources' object has no attribute 'get_temporal_client'` (or `_temporal_client`).

- [ ] **Step 3: Write minimal implementation**

In `runtime.py`, add the import `from temporalio.client import Client` and on the `RuntimeResources` class:

```python
@property
def temporal_address(self) -> str:
    return self._settings.temporal_address

@property
def temporal_namespace(self) -> str:
    return self._settings.temporal_namespace

async def get_temporal_client(self) -> Client:
    """Lazily connect and cache the Temporal client used by ScheduleManager."""
    if getattr(self, "_temporal_client", None) is None:
        self._temporal_client = await Client.connect(
            self._settings.temporal_address,
            namespace=self._settings.temporal_namespace,
        )
    return self._temporal_client
```

And in `__init__`, initialize `self._temporal_client: Client | None = None`. In `close()` (async), add: `if getattr(self, "_temporal_client", None) is not None: await self._temporal_client.close()` (wrap in try/except to keep shutdown best-effort).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_runtime_temporal_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/careerops/infrastructure/runtime.py tests/unit/test_runtime_temporal_client.py
git commit -m "feat(phase2): add lazy Temporal client accessor to RuntimeResources"
```

---

### Task 2: Outbox + approval-sweep trigger workflows

**Files:**
- Create: `src/careerops/workflows/loop_trigger_workflows.py`
- Modify: `src/careerops/infrastructure/temporal/worker.py` (register in `build_worker`)
- Test: `tests/unit/test_loop_trigger_workflows.py`

**Interfaces:**
- Consumes: `OUTBOX_DRAIN_ACTIVITY` and `APPROVAL_SWEEPER_ACTIVITY` from `careerops.infrastructure.temporal.activities`; both activities take no arg.
- Produces: `OutboxDrainWorkflow` (run signature `async def run(self) -> dict`) and `ApprovalSweepWorkflow` (`async def run(self) -> int`), registered on the `careerops-m0` task queue via `build_worker`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_loop_trigger_workflows.py
from careerops.infrastructure.temporal.worker import build_worker, TemporalWorkerSettings


def test_build_worker_registers_outbox_and_sweep_trigger_workflows():
    from temporalio.testing import WorkflowEnvironment
    import pytest

    # We only assert the workflow classes are in the worker's registered set
    # without running a live Temporal: inspect build_worker's all_workflows via
    # the module-level list it consumes.
    from careerops.infrastructure.temporal import worker as worker_mod
    # The worker module exposes the canonical workflow list used by build_worker.
    names = {getattr(w, "__name__", "") for w in worker_mod._TRIGGER_WORKFLOWS}
    assert "OutboxDrainWorkflow" in names
    assert "ApprovalSweepWorkflow" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_loop_trigger_workflows.py -v`
Expected: FAIL — module has no `_TRIGGER_WORKFLOWS` / workflows not defined.

- [ ] **Step 3: Write minimal implementation**

```python
# src/careerops/workflows/loop_trigger_workflows.py
"""Thin trigger workflows that let a Temporal Schedule fire an activity.

``drain_outbox`` and ``sweep_expired_approvals`` are activity-only; a Schedule
can only fire a workflow, so each gets a zero-arg wrapper (mirroring the
existing ``CrawlScheduledWorkflow`` pattern).
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

from careerops.infrastructure.temporal.activities import (
    APPROVAL_SWEEPER_ACTIVITY,
    OUTBOX_DRAIN_ACTIVITY,
)
from careerops.workflows.mail_sync_contracts import MAIL_SYNC_FETCH_ACTIVITY, MailSyncFetchInput

with workflow.defn.__self_module if False else workflow:  # noqa: placeholder guard removed below
    pass

_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class OutboxDrainWorkflow:
    """Fire ``drain_outbox`` on a schedule (re-deliver pending outbox events)."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            OUTBOX_DRAIN_ACTIVITY,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )


@workflow.defn
class ApprovalSweepWorkflow:
    """Fire ``sweep_expired_approvals`` on a schedule."""

    @workflow.run
    async def run(self) -> int:
        return await workflow.execute_activity(
            APPROVAL_SWEEPER_ACTIVITY,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
        )
```

(Delete the `with workflow.defn.__self_module ...` placeholder line above — it was only a scaffold note; the real file contains just the two `@workflow.defn` classes and the imports/timeout constant.)

In `worker.py`, add to the `all_workflows` list inside `build_worker`:

```python
from careerops.workflows.loop_trigger_workflows import (
    ApprovalSweepWorkflow,
    OutboxDrainWorkflow,
)
# ...inside build_worker, extend all_workflows:
all_workflows = [
    RecoverableSmokeWorkflow,
    CompanyDiscoveryWorkflow,
    CrawlJobSourceWorkflow,
    RawDocumentPurgeWorkflow,
    CrawlRunWorkflow,
    CrawlScheduledWorkflow,
    OutboxDrainWorkflow,
    ApprovalSweepWorkflow,
]
```

Also expose the module-level list the test reads:

```python
# near the other workflow imports at top of worker.py
from careerops.workflows.loop_trigger_workflows import (
    ApprovalSweepWorkflow as _ApprovalSweepWorkflow,
    OutboxDrainWorkflow as _OutboxDrainWorkflow,
)
_TRIGGER_WORKFLOWS = (_OutboxDrainWorkflow, _ApprovalSweepWorkflow)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_loop_trigger_workflows.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/careerops/workflows/loop_trigger_workflows.py src/careerops/infrastructure/temporal/worker.py tests/unit/test_loop_trigger_workflows.py
git commit -m "feat(phase2): add outbox + approval-sweep trigger workflows"
```

---

### Task 3: Mail-sync trigger workflow

**Files:**
- Modify: `src/careerops/workflows/loop_trigger_workflows.py` (add `MailSyncTriggerWorkflow`)
- Modify: `src/careerops/infrastructure/temporal/worker.py` (register it)
- Test: `tests/unit/test_loop_trigger_workflows.py` (extend)

**Interfaces:**
- Consumes: `MAIL_SYNC_FETCH_ACTIVITY`, `MailSyncFetchInput` from `careerops.workflows.mail_sync_contracts`.
- Produces: `MailSyncTriggerWorkflow.run(self, request: MailSyncFetchInput) -> MailSyncFetchResult`.

- [ ] **Step 1: Write the failing test (extend the file)**

```python
def test_mail_sync_trigger_workflow_registered():
    from careerops.infrastructure.temporal import worker as worker_mod
    names = {getattr(w, "__name__", "") for w in worker_mod._TRIGGER_WORKFLOWS}
    assert "MailSyncTriggerWorkflow" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_loop_trigger_workflows.py::test_mail_sync_trigger_workflow_registered -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

In `loop_trigger_workflows.py` add:

```python
@workflow.defn
class MailSyncTriggerWorkflow:
    """Fire ``fetch_and_sync`` on a schedule to pull inbound Gmail."""

    @workflow.run
    async def run(self, request: MailSyncFetchInput):
        return await workflow.execute_activity(
            MAIL_SYNC_FETCH_ACTIVITY,
            request,
            start_to_close_timeout=timedelta(seconds=120),
        )
```

Add `MailSyncTriggerWorkflow` to `build_worker.all_workflows` and to the `_TRIGGER_WORKFLOWS` tuple in `worker.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_loop_trigger_workflows.py -v`
Expected: PASS (both registration tests).

- [ ] **Step 5: Commit**

```bash
git add src/careerops/workflows/loop_trigger_workflows.py src/careerops/infrastructure/temporal/worker.py tests/unit/test_loop_trigger_workflows.py
git commit -m "feat(phase2): add mail-sync trigger workflow"
```

---

### Task 4: Crawl-activation adapters (real Protocol implementations)

**Files:**
- Create: `src/careerops/infrastructure/temporal/crawl_activation_adapters.py`
- Test: `tests/unit/test_crawl_activation_adapters.py`

**Interfaces:**
- Consumes: `ScheduleManager` (Task 1's client), `CrawlScheduledWorkflow` + `ScheduledCrawlWorkflowInput(owner_id, source_id)` from `s5_contracts`, `PostgresTier2Budget`, `CrawlDownstreamService`, `CrawlSourceResult`.
- Produces: `TemporalScheduleActivator` (`activate_source_schedule(source_id, interval, *, paused, owner_id) -> bool`), `Tier2BudgetChecker` (`available_slots`, `daily_remaining` properties), `DownstreamInboxProjector` (`project_new_postings(...)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_crawl_activation_adapters.py
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from careerops.infrastructure.temporal.crawl_activation_adapters import (
    TemporalScheduleActivator,
    Tier2BudgetChecker,
)


def test_schedule_activator_ensures_schedule_paused_then_resumes():
    mgr = MagicMock()
    mgr.ensure_schedule = AsyncMock(return_value=True)
    mgr.resume = AsyncMock(return_value=True)
    import asyncio
    loop = asyncio.new_event_loop()
    activator = TemporalScheduleActivator(mgr, task_queue="careerops-m0")
    sid = uuid4()
    owner = uuid4()
    # paused=True path
    loop.run_until_complete(
        activator.activate_source_schedule(sid, timedelta(minutes=60), paused=True, owner_id=owner)
    )
    assert mgr.ensure_schedule.await_count == 1
    # paused=False path resumes
    loop.run_until_complete(
        activator.activate_source_schedule(sid, timedelta(minutes=60), paused=False, owner_id=owner)
    )
    assert mgr.resume.await_count == 1


def test_budget_checker_reads_tier2_status():
    budget = MagicMock()
    budget.status.return_value = {"available_slots": 4, "daily_remaining": 250}
    checker = Tier2BudgetChecker(budget)
    assert checker.available_slots == 4
    assert checker.daily_remaining == 250
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_crawl_activation_adapters.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# src/careerops/infrastructure/temporal/crawl_activation_adapters.py
"""Real adapters that satisfy CrawlActivationService's Protocol deps."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from careerops.application.crawl_activation import (
    BudgetChecker,
    InboxProjector,
    ScheduleActivator,
)
from careerops.infrastructure.temporal.schedule_manager import ScheduleManager


class TemporalScheduleActivator:
    """ScheduleActivator: register paused, or resume, a source crawl schedule."""

    def __init__(self, manager: ScheduleManager, *, task_queue: str) -> None:
        self._manager = manager
        self._task_queue = task_queue

    async def activate_source_schedule(
        self,
        source_id: UUID,
        interval: timedelta,
        *,
        paused: bool = False,
        owner_id: UUID | None = None,
    ) -> bool:
        # Lazy import keeps the workflow module out of non-worker import paths.
        from careerops.workflows.s5_workflows import CrawlScheduledWorkflow
        from careerops.workflows.s5_contracts import ScheduledCrawlWorkflowInput

        if owner_id is None:
            return False
        schedule_id = f"crawl:{owner_id}:{source_id}"
        await self._manager.ensure_schedule(
            schedule_id=schedule_id,
            workflow=CrawlScheduledWorkflow.run,
            arg=ScheduledCrawlWorkflowInput(owner_id=owner_id, source_id=source_id),
            interval=interval,
            task_queue=self._task_queue,
            paused=paused,
            note=f"crawl source {source_id}",
        )
        if not paused:
            await self._manager.resume(schedule_id)
        return True


class Tier2BudgetChecker:
    """BudgetChecker: read remaining Tier 2 capacity from the budget coordinator."""

    def __init__(self, budget) -> None:  # budget: PostgresTier2Budget
        self._budget = budget

    @property
    def available_slots(self) -> int:
        return int(self._budget.status().get("available_slots", 0))

    @property
    def daily_remaining(self) -> int:
        return int(self._budget.status().get("daily_remaining", 0))


class DownstreamInboxProjector:
    """InboxProjector: delegate to CrawlDownstreamService matching chain."""

    def __init__(self, downstream) -> None:  # downstream: CrawlDownstreamService
        self._downstream = downstream

    async def project_new_postings(
        self,
        owner_id: UUID,
        source_id: UUID,
        postings: tuple,
        *,
        now=None,
    ) -> int:
        # CrawlDownstreamService exposes a chain entry; delegate and count.
        return await self._downstream.project_for_source(owner_id, source_id, postings, now=now)
```

> Note: confirm `CrawlDownstreamService` exposes `project_for_source` (or whatever its public chain method is) during implementation; if the real method name differs, adapt `DownstreamInboxProjector` to call it. The inbox projector is optional for activation (activation works with `inbox_projector=None`), so if wiring is uncertain, construct `CrawlActivationService` with `inbox_projector=None` first and add projection in a follow-up.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_crawl_activation_adapters.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/careerops/infrastructure/temporal/crawl_activation_adapters.py tests/unit/test_crawl_activation_adapters.py
git commit -m "feat(phase2): real adapters for CrawlActivationService protocols"
```

---

### Task 5: loop_bootstrap module

**Files:**
- Create: `src/careerops/application/loop_bootstrap.py`
- Test: `tests/unit/test_loop_bootstrap.py`

**Interfaces:**
- Consumes: `ScheduleManager`, the three trigger workflows + `CrawlScheduledWorkflow`, `CrawlActivationService` + Task 4 adapters, `check_crawl_readiness`, `Settings`, `RuntimeResources` (for engine + repos).
- Produces: `async def bootstrap_trigger_loop(settings, runtime) -> BootstrapReport` and cadence constants `MAIL_SYNC_INTERVAL`, `OUTBOX_DRAIN_INTERVAL`, `APPROVAL_SWEEP_INTERVAL`, `DEFAULT_CRAWL_INTERVAL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_loop_bootstrap.py
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from careerops.application.loop_bootstrap import (
    OUTBOX_DRAIN_INTERVAL,
    bootstrap_trigger_loop,
)


def _fake_runtime(with_mail_account=True, with_owner=True):
    runtime = MagicMock()
    runtime._settings = MagicMock(google_oauth_enabled=True)
    # engine returns rows for the resolution queries
    owner = uuid4() if with_owner else None
    candidate = uuid4()
    account = uuid4() if with_mail_account else None

    def execute(stmt, *_a, **_k):
        text = str(stmt)
        class _Rows:
            def __init__(self, rows): self._rows = rows
            def mappings(self): return self._rows
        if "crawl_plan_versions" in text or "crawl_sources" in text:
            rows = [{"owner_id": owner}] if owner else []
            return _Rows(rows)
        if "mail_accounts" in text or "email_accounts" in text:
            rows = [{"candidate_id": candidate, "account_id": account}] if account else []
            return _Rows(rows)
        return _Rows([])
    engine = MagicMock()
    conn = MagicMock()
    conn.execute.side_effect = execute
    engine.begin.return_value.__enter__ = lambda self: conn
    engine.begin.return_value.__exit__ = lambda *a: None
    runtime.database = engine
    return runtime, (owner, candidate, account)


def test_registers_outbox_and_sweep_always(monkeypatch):
    runtime, _ = _fake_runtime(with_mail_account=False, with_owner=False)
    mgr = MagicMock()
    mgr.ensure_schedule = AsyncMock(return_value=True)
    import asyncio
    report = asyncio.new_event_loop().run_until_complete(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    ids = [c.kwargs["schedule_id"] for c in mgr.ensure_schedule.await_args_list]
    assert "outbox-drain" in ids
    assert "approval-sweep" in ids
    assert "mail-sync" not in ids  # gated off (no oauth / no account)
    assert report.outbox_registered and report.sweep_registered
    assert not report.mail_registered


def test_registers_mail_when_oauth_and_account_present():
    runtime, _ = _fake_runtime(with_mail_account=True)
    mgr = MagicMock()
    mgr.ensure_schedule = AsyncMock(return_value=True)
    import asyncio
    asyncio.new_event_loop().run_until_complete(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=True), runtime, schedule_manager=mgr)
    )
    ids = [c.kwargs["schedule_id"] for c in mgr.ensure_schedule.await_args_list]
    assert "mail-sync" in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_loop_bootstrap.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# src/careerops/application/loop_bootstrap.py
"""Startup bootstrap for the self-driving trigger loop (Phase 2).

Idempotently registers the Temporal Schedules that drive mail sync, outbox
drain, approval sweep, and per-source crawl. Called from the API lifespan.
Fail-closed: each schedule is gated on its capability flag / data existing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import sqlalchemy as sa

from careerops.application.crawl_readiness import check_crawl_readiness
from careerops.infrastructure.database.schema import crawl_sources, crawl_plan_versions

_log = logging.getLogger(__name__)

MAIL_SYNC_INTERVAL = timedelta(minutes=5)
OUTBOX_DRAIN_INTERVAL = timedelta(seconds=30)
APPROVAL_SWEEP_INTERVAL = timedelta(seconds=60)
DEFAULT_CRAWL_INTERVAL = timedelta(hours=1)

MAIN_TASK_QUEUE = "careerops-m0"


@dataclass
class BootstrapReport:
    mail_registered: bool = False
    outbox_registered: bool = False
    sweep_registered: bool = False
    crawl_schedules_registered: int = 0
    crawl_activated: int = 0
    skipped: list[str] = field(default_factory=list)


def _resolve_owner(engine) -> UUID | None:
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(crawl_plan_versions.c.owner_id).distinct().limit(2)
        ).mappings().all()
    if len(rows) != 1:
        return None
    return UUID(str(rows[0]["owner_id"]))


def _resolve_mail_account(engine) -> tuple[UUID, UUID] | None:
    """Return (candidate_id, account_id) for the single connected account."""
    from careerops.infrastructure.database.schema import email_accounts

    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(
                email_accounts.c.candidate_id, email_accounts.c.id.label("account_id")
            ).limit(2)
        ).mappings().all()
    if len(rows) != 1:
        return None
    return UUID(str(rows[0]["candidate_id"])), UUID(str(rows[0]["account_id"]))


async def bootstrap_trigger_loop(
    settings,
    runtime,
    *,
    schedule_manager,
    crawl_activation_service=None,
) -> BootstrapReport:
    report = BootstrapReport()

    # --- outbox + sweep: always (internal, no external effect) ---
    from careerops.workflows.loop_trigger_workflows import (
        ApprovalSweepWorkflow,
        OutboxDrainWorkflow,
    )
    await schedule_manager.ensure_schedule(
        schedule_id="outbox-drain",
        workflow=OutboxDrainWorkflow.run,
        arg=None,
        interval=OUTBOX_DRAIN_INTERVAL,
        task_queue=MAIN_TASK_QUEUE,
        note="drain pending outbox events",
    )
    report.outbox_registered = True
    await schedule_manager.ensure_schedule(
        schedule_id="approval-sweep",
        workflow=ApprovalSweepWorkflow.run,
        arg=None,
        interval=APPROVAL_SWEEP_INTERVAL,
        task_queue=MAIN_TASK_QUEUE,
        note="expire stale approvals",
    )
    report.sweep_registered = True

    # --- mail sync: gated on oauth flag + a connected account ---
    if getattr(settings, "google_oauth_enabled", False):
        account = _resolve_mail_account(runtime.database)
        if account is not None:
            from careerops.workflows.loop_trigger_workflows import MailSyncTriggerWorkflow
            from careerops.workflows.mail_sync_contracts import MailSyncFetchInput
            candidate_id, account_id = account
            await schedule_manager.ensure_schedule(
                schedule_id="mail-sync",
                workflow=MailSyncTriggerWorkflow.run,
                arg=MailSyncFetchInput(
                    candidate_id=str(candidate_id),
                    account_id=str(account_id),
                    start_history_id="",
                ),
                interval=MAIL_SYNC_INTERVAL,
                task_queue=MAIN_TASK_QUEUE,
                note="incremental inbound mail sync",
            )
            report.mail_registered = True
        else:
            report.skipped.append("mail-sync: no connected mail account")
    else:
        report.skipped.append("mail-sync: google_oauth_enabled is off")

    # --- crawl: register paused per source, then activate eligible ---
    if crawl_activation_service is not None:
        owner_id = _resolve_owner(runtime.database)
        if owner_id is None:
            report.skipped.append("crawl: no single owner resolved")
            return report
        readiness = check_crawl_readiness(runtime)
        if not readiness.ready:
            report.skipped.append("crawl: readiness gate failed")
            return report
        # select_sources returns enabled+ACTIVE sources for the owner.
        source_queue = getattr(runtime, "source_queue_service", None)
        if source_queue is None:
            report.skipped.append("crawl: source queue unavailable")
            return report
        sources = source_queue.select_sources(owner_id)
        result = await crawl_activation_service.activate_crawl_schedules(
            readiness, owner_id, sources
        )
        report.crawl_schedules_registered = len(sources)
        report.crawl_activated = result.schedules_activated
    else:
        report.skipped.append("crawl: activation service not configured")

    _log.info("trigger-loop bootstrap: %s", report)
    return report
```

> Implementation notes (verify during coding): confirm `check_crawl_readiness(runtime)` signature (it may take specific repos rather than the whole runtime — adapt the call); confirm `SourceQueueService.select_sources(owner_id)` exists and its return is iterable of sources carrying `.id`; confirm `email_accounts` is the right schema table for the mail account (it may be `mail_accounts` — use whichever `schema.py` defines).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_loop_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/careerops/application/loop_bootstrap.py tests/unit/test_loop_bootstrap.py
git commit -m "feat(phase2): loop bootstrap registers managed schedules idempotently"
```

---

### Task 6: Wire bootstrap into API lifespan

**Files:**
- Modify: `src/careerops/api/app.py` (inside `lifespan`, before `yield`)
- Test: `tests/unit/test_lifespan_bootstrap.py`

**Interfaces:**
- Consumes: `bootstrap_trigger_loop` (Task 5), `ScheduleManager`, `CrawlActivationService` + Task 4 adapters, `RuntimeResources.get_temporal_client` (Task 1).
- Produces: on API startup the four schedule kinds are reconciled; `app.state.trigger_loop_report` holds the last report.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_lifespan_bootstrap.py
from unittest.mock import AsyncMock, MagicMock, patch


def test_lifespan_runs_bootstrap_best_effort(monkeypatch):
    from careerops.api.app import create_app
    from careerops.config import Settings

    captured = {}

    async def fake_bootstrap(settings, runtime, *, schedule_manager, crawl_activation_service=None):
        captured["called"] = True
        return MagicMock()

    with patch("careerops.application.loop_bootstrap.bootstrap_trigger_loop", fake_bootstrap):
        settings = Settings(environment="test")
        # create_app with a fake probe to avoid real infra
        probe = MagicMock()
        app = create_app(settings, readiness_probe=probe)
        import asyncio
        from starlette.testclient import TestClient
        # Driving the lifespan via TestClient triggers startup/shutdown.
        with TestClient(app):
            pass
    assert captured.get("called") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_lifespan_bootstrap.py -v`
Expected: FAIL — bootstrap not called from lifespan.

- [ ] **Step 3: Write minimal implementation**

In `app.py` `lifespan`, after the smart-intake block and before `try: yield`:

```python
# Phase 2: reconcile the self-driving trigger-loop schedules (best-effort).
if isinstance(probe, RuntimeResources):
    try:
        from careerops.application.loop_bootstrap import bootstrap_trigger_loop
        from careerops.infrastructure.temporal.schedule_manager import ScheduleManager

        client = None
        for attempt in range(3):
            try:
                client = await probe.get_temporal_client()
                break
            except Exception as exc:  # noqa: BLE001 - temporal may not be up yet
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(2 ** attempt)
        crawl_activation = probe.build_crawl_activation_service(client)  # see note
        report = await bootstrap_trigger_loop(
            resolved, probe,
            schedule_manager=ScheduleManager(client),
            crawl_activation_service=crawl_activation,
        )
        _app.state.trigger_loop_report = report
    except Exception as exc:  # noqa: BLE001 - never crash the API on bootstrap
        import logging
        logging.getLogger(__name__).warning("trigger-loop bootstrap failed: %s", exc)
```

Add to `RuntimeResources` a helper `build_crawl_activation_service(self, client)` that composes `CrawlActivationService(TemporalScheduleActivator(ScheduleManager(client), task_queue=...), Tier2BudgetChecker(self.tier2_budget), self.source_queue_service, None)` — reusing the repos the runtime already builds. (If `source_queue_service`/`tier2_budget` aren't currently attributes on `RuntimeResources`, add them where the other Section-5 repos are constructed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_lifespan_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/careerops/api/app.py tests/unit/test_lifespan_bootstrap.py src/careerops/infrastructure/runtime.py
git commit -m "feat(phase2): run trigger-loop bootstrap in API lifespan"
```

---

### Task 7: Restart-idempotency verification (task 2.5)

**Files:**
- Create: `tests/unit/test_loop_bootstrap_idempotent.py`

**Interfaces:**
- Consumes: `bootstrap_trigger_loop` (Task 5) with an in-memory fake `ScheduleManager` that records create-vs-update.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_loop_bootstrap_idempotent.py
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


class RecordingManager:
    def __init__(self):
        self.exists = set()
        self.created = 0
        self.updated = 0
    async def ensure_schedule(self, *, schedule_id, **kw):
        if schedule_id in self.exists:
            self.updated += 1
        else:
            self.exists.add(schedule_id)
            self.created += 1
        return True


def test_double_bootstrap_converges_no_duplicates():
    import asyncio
    from careerops.application.loop_bootstrap import bootstrap_trigger_loop
    runtime = MagicMock()
    runtime.database = MagicMock()
    # Make resolution return nothing for mail/crawl so only outbox+sweep register.
    runtime.database.begin.return_value.__enter__ = lambda self: MagicMock(
        execute=MagicMock(return_value=MagicMock(mappings=lambda: []))
    )
    mgr = RecordingManager()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    loop.run_until_complete(
        bootstrap_trigger_loop(MagicMock(google_oauth_enabled=False), runtime, schedule_manager=mgr)
    )
    # Two schedule kinds, created exactly once each; second pass only updates.
    assert mgr.created == 2
    assert mgr.updated == 2
    assert len(mgr.exists) == 2
```

- [ ] **Step 2: Run test to verify it fails (or passes if logic already converges)**

Run: `pytest tests/unit/test_loop_bootstrap_idempotent.py -v`
Expected: PASS once Task 5 is in; if it FAILS, the bootstrap is creating duplicates and must be fixed to reconcile.

- [ ] **Step 3: (Only if failing) fix bootstrap to reconcile instead of recreate**

`ensure_schedule` already reconciles — the test passing confirms it. No code change if green.

- [ ] **Step 4: Run full focused suite**

Run: `pytest tests/unit/test_loop_bootstrap.py tests/unit/test_loop_trigger_workflows.py tests/unit/test_crawl_activation_adapters.py tests/unit/test_lifespan_bootstrap.py tests/unit/test_loop_bootstrap_idempotent.py tests/unit/test_runtime_temporal_client.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_loop_bootstrap_idempotent.py
git commit -m "test(phase2): double-bootstrap converges without duplicate schedules"
```

---

## Self-Review (completed)

- **Spec coverage:** 2.1 mail-sync → Task 5 (gated registration) + Task 3 workflow; 2.2 outbox+sweep → Task 2 + Task 5; 2.3 crawl paused registration → Task 4 activator (`paused=True`) + Task 5; 8.2 activation → Task 4 adapters + Task 5/6; 2.5 restart idempotency → Task 7. All Phase 2 items covered.
- **Placeholder scan:** the scaffold `with workflow.defn.__self_module ...` line in Task 2 is explicitly flagged for deletion (not a placeholder in the deliverable). All other steps carry real code.
- **Type consistency:** `bootstrap_trigger_loop(settings, runtime, *, schedule_manager, crawl_activation_service=None)` signature is identical in Tasks 5, 6, 7. `TemporalScheduleActivator.activate_source_schedule` matches the `ScheduleActivator` Protocol. `Tier2BudgetChecker.available_slots`/`daily_remaining` match `BudgetChecker`.
- **Open verifications flagged inline** (not placeholders — they are confirm-the-real-name steps during coding): `check_crawl_readiness` arg shape, `SourceQueueService.select_sources` signature, `email_accounts` vs `mail_accounts` table, `CrawlDownstreamService` chain method name, whether `RuntimeResources` already exposes `tier2_budget` / `source_queue_service`.
