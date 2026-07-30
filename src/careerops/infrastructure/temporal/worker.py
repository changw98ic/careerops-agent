from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.worker import Worker

from careerops.adapters.http_fetcher import fetch
from careerops.infrastructure.temporal.activities import (
    ApprovalSweeperActivities,
    OutboxDrainActivities,
    SmokeActivities,
)
from careerops.infrastructure.temporal.agent_activities import AgentActivities
from careerops.infrastructure.temporal.ego_browser_executor import EgoBrowserExecutor
from careerops.infrastructure.temporal.mail_sync_activities import (
    GmailTokenRefreshActivities,
    MailSyncReaderActivities,
)
from careerops.infrastructure.temporal.m1_activities import (
    CrawlActivitySink,
    M1CrawlActivities,
    M1DiscoveryActivities,
    M1PurgeActivities,
)
from careerops.infrastructure.temporal.s5_activities import S5CrawlExecutionActivities
from careerops.workflows.agent_workflows import AgentRunWorkflow
from careerops.workflows.m1_workflows import (
    CompanyDiscoveryWorkflow,
    CrawlJobSourceWorkflow,
    RawDocumentPurgeWorkflow,
)
from careerops.workflows.s5_workflows import CrawlRunWorkflow, CrawlScheduledWorkflow
from careerops.workflows.smoke import RecoverableSmokeWorkflow

# Explicit callable type prevents pyright from inferring a broken union of
# incompatible activity signatures (the @activity.defn decorators produce
# heterogeneous coroutine types that confuse the Protocol conformance check).
_TemporalActivity = Callable[..., object]


@dataclass(frozen=True, slots=True)
class TemporalWorkerSettings:
    target: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = "careerops-m0"
    identity: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("target", "namespace", "task_queue"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> TemporalWorkerSettings:
        values = os.environ if environ is None else environ
        identity = values.get("CAREEROPS_TEMPORAL_WORKER_IDENTITY") or None
        return cls(
            target=values.get("CAREEROPS_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            namespace=values.get("CAREEROPS_TEMPORAL_NAMESPACE", "default"),
            task_queue=values.get("CAREEROPS_TEMPORAL_TASK_QUEUE", "careerops-m0"),
            identity=identity,
        )


@dataclass(frozen=True, slots=True)
class M1ActivityBundles:
    """M1 activity bundles injected into the worker."""

    discovery: M1DiscoveryActivities | None = None
    crawl: M1CrawlActivities | None = None
    purge: M1PurgeActivities | None = None
    outbox_drain: OutboxDrainActivities | None = None
    approval_sweeper: ApprovalSweeperActivities | None = None
    crawl_execution: S5CrawlExecutionActivities | None = None
    # Phase 1.3 / 2.1: inbound mail read + the dual-layer token refresh's
    # scheduled layer. Default None => worker stays fail-closed (no mail
    # reading) until a configured bundle is injected.
    mail_reader: MailSyncReaderActivities | None = None
    gmail_token_refresh: GmailTokenRefreshActivities | None = None


def build_worker(
    client: Client,
    settings: TemporalWorkerSettings,
    *,
    activities: SmokeActivities | None = None,
    m1_activities: M1ActivityBundles | None = None,
    crawl_sink: CrawlActivitySink | None = None,
    max_cached_workflows: int = 1000,
) -> Worker:
    smoke = activities or SmokeActivities()
    m1 = m1_activities or M1ActivityBundles()

    discovery = m1.discovery or M1DiscoveryActivities()
    crawl = m1.crawl or M1CrawlActivities(sink=crawl_sink)
    purge = m1.purge or M1PurgeActivities()
    s5 = m1.crawl_execution or S5CrawlExecutionActivities()

    all_workflows = [
        RecoverableSmokeWorkflow,
        CompanyDiscoveryWorkflow,
        CrawlJobSourceWorkflow,
        RawDocumentPurgeWorkflow,
        CrawlRunWorkflow,
        CrawlScheduledWorkflow,
    ]
    # Explicit callable type prevents pyright from inferring a broken union of
    # incompatible activity signatures (the @activity.defn decorators produce
    # heterogeneous coroutine types that confuse the Protocol conformance check).
    all_activities: list[_TemporalActivity] = [
        smoke.record_started,
        smoke.record_completed,
        discovery.discover_company_sources,
        crawl.crawl_job_source,
        crawl.ingest_posting,
        purge.purge_raw_documents,
        s5.execute_crawl_run,
        s5.create_scheduled_run,
    ]

    if m1.outbox_drain is not None:
        all_activities.append(m1.outbox_drain.drain_outbox)
    if m1.approval_sweeper is not None:
        all_activities.append(m1.approval_sweeper.sweep_expired_approvals)
    # Phase 1.3 / 2.1: conditional registration keeps a worker without a
    # configured mail reader fail-closed (no inbound polling / token refresh).
    if m1.mail_reader is not None:
        all_activities.append(m1.mail_reader.fetch_and_sync)
    if m1.gmail_token_refresh is not None:
        all_activities.append(m1.gmail_token_refresh.refresh_gmail_token)

    return Worker(
        client,
        task_queue=settings.task_queue,
        identity=settings.identity,
        workflows=all_workflows,
        activities=all_activities,
        max_cached_workflows=max_cached_workflows,
    )


async def run_worker(
    settings: TemporalWorkerSettings,
    *,
    activities: SmokeActivities | None = None,
    m1_activities: M1ActivityBundles | None = None,
    crawl_sink: CrawlActivitySink | None = None,
) -> None:
    client = await Client.connect(
        settings.target,
        namespace=settings.namespace,
        identity=settings.identity,
    )
    await build_worker(
        client, settings, activities=activities, m1_activities=m1_activities, crawl_sink=crawl_sink
    ).run()


def build_agent_worker(
    client: Client,
    settings: TemporalWorkerSettings,
    *,
    agent_activities: AgentActivities | None = None,
    max_cached_workflows: int = 1000,
) -> Worker:
    """Build a Temporal worker for the ``careerops-agent`` task queue.

    Registers :class:`AgentRunWorkflow` and its five activities.
    The ``agent_activities`` bundle is injectable for testing; when not
    provided, the default fail-closed stubs are used.
    """
    agent = agent_activities or AgentActivities()

    all_workflows = [AgentRunWorkflow]
    all_activities: list[_TemporalActivity] = [
        agent.resolve_agent_context,
        agent.run_deterministic_stage,
        agent.invoke_model_stage,
        agent.persist_agent_stage,
        agent.reconcile_agent_run,
    ]

    return Worker(
        client,
        task_queue="careerops-agent",
        identity=settings.identity,
        workflows=all_workflows,
        activities=all_activities,
        max_cached_workflows=max_cached_workflows,
    )


async def run_agent_worker(
    settings: TemporalWorkerSettings,
    *,
    agent_activities: AgentActivities | None = None,
) -> None:
    """Run the agent-worker on the ``careerops-agent`` task queue."""
    client = await Client.connect(
        settings.target,
        namespace=settings.namespace,
        identity=settings.identity,
    )
    await build_agent_worker(
        client,
        settings,
        agent_activities=agent_activities,
    ).run()


def main() -> None:
    from careerops.application.crawl_execution import CrawlExecutionService
    from careerops.application.crawl_plan_service import CrawlRunService
    from careerops.application.outbox import OutboxPublisher
    from careerops.application.side_effect_kernel import SideEffectKernel
    from careerops.config import get_settings
    from careerops.infrastructure.database.engine import create_database_engine
    from careerops.infrastructure.database.outbox import PostgresOutboxStore
    from careerops.infrastructure.database.postgres_crawl_repo import (
        PostgresCrawlPlanRepository,
        PostgresCrawlRunRepository,
        PostgresCrawlSourceRepository,
    )
    from careerops.infrastructure.database.side_effect_postgres import PostgresSideEffectStore
    from careerops.infrastructure.temporal.activities import (
        ApprovalSweeperActivities,
        OutboxDrainActivities,
    )
    from careerops.infrastructure.temporal.internal_event_sink import LoggingInternalEventSink
    from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
    from careerops.infrastructure.temporal.s5_activities import S5CrawlExecutionActivities
    from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

    settings = get_settings()
    engine = create_database_engine(settings)

    side_effect_store = PostgresSideEffectStore(engine)
    outbox_store = PostgresOutboxStore(engine)

    kernel = SideEffectKernel(
        store=side_effect_store,
        provider=FakeSideEffectProvider(),
        outbox_store=outbox_store,
    )

    outbox_publisher = OutboxPublisher(
        store=outbox_store,
        sink=LoggingInternalEventSink(),
    )

    # Section 5: wire crawl execution + scheduled-run creation.
    crawl_sink = RealCrawlActivitySink(
        fetcher=fetch,
        engine=engine,
        browser_executor=EgoBrowserExecutor(),
    )
    run_repo = PostgresCrawlRunRepository(engine)
    plan_repo = PostgresCrawlPlanRepository(engine)
    source_repo = PostgresCrawlSourceRepository(engine)

    executor = CrawlExecutionService(
        run_repository=run_repo,
        plan_repository=plan_repo,
        source_repository=source_repo,
        sink=crawl_sink,
    )
    run_creator = CrawlRunService(
        run_repository=run_repo,
        plan_repository=plan_repo,
        source_repository=source_repo,
    )

    # Phase 1.3 / 2.1: wire inbound mail reading + the dual-layer token
    # refresh's scheduled layer. Only injected when a usable Gmail token file
    # exists, so environments without OAuth stay fail-closed (no mail reader).
    mail_reader_bundle = None
    token_refresh_bundle = None
    from pathlib import Path

    token_path = Path("secrets/gmail_send_token.json")
    if token_path.exists():
        from careerops.application.mail_sync_service import MailSyncService
        from careerops.infrastructure.database.postgres_mail_sync_repo import (
            PostgresMailAccountRepository,
            PostgresSyncRunRepository,
            PostgresThreadLinkRepository,
        )
        from careerops.infrastructure.temporal.mail_sync_activities import (
            GmailTokenRefreshActivities,
            MailSyncReaderActivities,
        )
        from careerops.integrations.gmail_reader import GmailReader
        from careerops.integrations.gmail_token_store import GmailTokenStore

        token_store = GmailTokenStore.from_token_file(token_path)
        mail_service = MailSyncService(
            PostgresMailAccountRepository(engine),
            PostgresSyncRunRepository(engine),
            PostgresThreadLinkRepository(engine),
        )
        mail_reader_bundle = MailSyncReaderActivities(
            reader=GmailReader(token_store),
            service=mail_service,
        )
        token_refresh_bundle = GmailTokenRefreshActivities(token_store)

    m1_activities = M1ActivityBundles(
        outbox_drain=OutboxDrainActivities(publisher=outbox_publisher),
        approval_sweeper=ApprovalSweeperActivities(kernel=kernel),
        crawl_execution=S5CrawlExecutionActivities(
            executor=executor,
            run_creator=run_creator,
        ),
        mail_reader=mail_reader_bundle,
        gmail_token_refresh=token_refresh_bundle,
    )

    asyncio.run(
        run_worker(
            TemporalWorkerSettings.from_environment(),
            m1_activities=m1_activities,
            crawl_sink=crawl_sink,
        )
    )


if __name__ == "__main__":
    main()
