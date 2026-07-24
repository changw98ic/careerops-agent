from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.worker import Worker

from careerops.adapters.http_fetcher import fetch
from careerops.infrastructure.temporal.activities import SmokeActivities
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
            task_queue=values.get("CAREEROPS_TEMPORAL_TASK_QUEUE", "careerops-m0"),
            identity=identity,
        )


@dataclass(frozen=True, slots=True)
class M1ActivityBundles:
    """M1 activity bundles injected into the worker."""

    discovery: M1DiscoveryActivities | None = None
    crawl: M1CrawlActivities | None = None
    purge: M1PurgeActivities | None = None


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
    all_activities = [
        smoke.record_started,
        smoke.record_completed,
        discovery.discover_company_sources,
        crawl.crawl_job_source,
        crawl.ingest_posting,
        purge.purge_raw_documents,
    ]

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
    from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink

    asyncio.run(
        run_worker(
            TemporalWorkerSettings.from_environment(),
            crawl_sink=RealCrawlActivitySink(fetcher=fetch),
        )
    )


if __name__ == "__main__":
    main()
