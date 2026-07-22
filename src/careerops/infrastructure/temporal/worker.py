from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.worker import Worker

from careerops.infrastructure.temporal.activities import (
    GoalRunSourceRegistryActivities,
    SmokeActivities,
)
from careerops.workflows.goal_run import GoalRunWorkflow
from careerops.workflows.smoke import RecoverableSmokeWorkflow


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


def build_worker(
    client: Client,
    settings: TemporalWorkerSettings,
    *,
    activities: SmokeActivities | None = None,
    goal_activities: GoalRunSourceRegistryActivities | None = None,
    max_cached_workflows: int = 1000,
) -> Worker:
    smoke_activities = activities or SmokeActivities()
    workflow_activities = goal_activities or GoalRunSourceRegistryActivities()
    return Worker(
        client,
        task_queue=settings.task_queue,
        identity=settings.identity,
        workflows=[RecoverableSmokeWorkflow, GoalRunWorkflow],
        activities=[
            smoke_activities.record_started,
            smoke_activities.record_completed,
            workflow_activities.ensure_run,
            workflow_activities.load_run,
            workflow_activities.checkpoint,
            workflow_activities.claim_source,
            workflow_activities.run_discovery,
            workflow_activities.complete_source,
            workflow_activities.fail_source,
            workflow_activities.ingest_public_ats,
            workflow_activities.match_jobs,
            workflow_activities.prepare_drafts,
            workflow_activities.request_review,
            workflow_activities.dispatch_goal,
            workflow_activities.reconcile_goal,
        ],
        max_cached_workflows=max_cached_workflows,
    )


async def run_worker(
    settings: TemporalWorkerSettings,
    *,
    activities: SmokeActivities | None = None,
    goal_activities: GoalRunSourceRegistryActivities | None = None,
) -> None:
    client = await Client.connect(
        settings.target,
        namespace=settings.namespace,
        identity=settings.identity,
    )
    await build_worker(
        client,
        settings,
        activities=activities,
        goal_activities=goal_activities,
    ).run()


def main() -> None:
    asyncio.run(run_worker(TemporalWorkerSettings.from_environment()))


if __name__ == "__main__":
    main()
