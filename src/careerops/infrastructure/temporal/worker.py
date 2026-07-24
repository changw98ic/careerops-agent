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
from careerops.infrastructure.temporal.m1_activities import (
    CrawlActivitySink,
    M1CrawlActivities,
    M1DiscoveryActivities,
    M1PurgeActivities,
)
from careerops.workflows.m1_workflows import (
    CompanyDiscoveryWorkflow,
    CrawlJobSourceWorkflow,
    RawDocumentPurgeWorkflow,
)
from careerops.workflows.smoke import RecoverableSmokeWorkflow

# Explicit callable type prevents pyright from inferring a broken union of
# incompatible activity signatures (the @activity.defn decorators produce
# heterogeneous coroutine types that confuse the Protocol conformance check).
_TemporalActivity = Callable[..., object]


@dataclass(frozen=True, slots=True)
class TemporalWorkerSettings:
    target: str = "127.0.0.1:7233"
    namespace: str = "default"
    task_queue: str = "careerops-m1"
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
            task_queue=values.get("CAREEROPS_TEMPORAL_TASK_QUEUE", "careerops-m1"),
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

    all_workflows = [
        RecoverableSmokeWorkflow,
        CompanyDiscoveryWorkflow,
        CrawlJobSourceWorkflow,
        RawDocumentPurgeWorkflow,
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
    ]

    if m1.outbox_drain is not None:
        all_activities.append(m1.outbox_drain.drain_outbox)
    if m1.approval_sweeper is not None:
        all_activities.append(m1.approval_sweeper.sweep_expired_approvals)

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


def main() -> None:
    from careerops.application.outbox import OutboxPublisher
    from careerops.application.side_effect_kernel import SideEffectKernel
    from careerops.config import get_settings
    from careerops.infrastructure.database.engine import create_database_engine
    from careerops.infrastructure.database.outbox import PostgresOutboxStore
    from careerops.infrastructure.database.side_effect_postgres import PostgresSideEffectStore
    from careerops.infrastructure.temporal.activities import (
        ApprovalSweeperActivities,
        OutboxDrainActivities,
    )
    from careerops.infrastructure.temporal.internal_event_sink import LoggingInternalEventSink
    from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
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

    m1_activities = M1ActivityBundles(
        outbox_drain=OutboxDrainActivities(publisher=outbox_publisher),
        approval_sweeper=ApprovalSweeperActivities(kernel=kernel),
    )

    asyncio.run(
        run_worker(
            TemporalWorkerSettings.from_environment(),
            m1_activities=m1_activities,
            crawl_sink=RealCrawlActivitySink(fetcher=fetch, engine=engine),
        )
    )


if __name__ == "__main__":
    main()
