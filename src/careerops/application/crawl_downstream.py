"""Automatic matching and inbox projection after a successful crawl."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from careerops.application.inbox_service import InboxProjectionService
from careerops.application.matching import MatchOrchestrator
from careerops.domain.inbox import BlockingReason


class JobProjectionRepository(Protocol):
    """Read the complete job shape needed by the inbox filter."""

    def get_job_projection_data(self, canonical_job_id: UUID) -> dict[str, Any] | None: ...


class CrawlDownstreamService:
    """Persist matching and inbox results for newly ingested canonical jobs."""

    def __init__(
        self,
        matcher: MatchOrchestrator,
        inbox: InboxProjectionService,
        jobs: JobProjectionRepository,
    ) -> None:
        self._matcher = matcher
        self._inbox = inbox
        self._jobs = jobs

    def project(
        self,
        candidate_id: UUID,
        canonical_job_ids: set[UUID],
        *,
        now: datetime,
    ) -> int:
        projected = 0
        for canonical_job_id in sorted(canonical_job_ids, key=str):
            job = self._jobs.get_job_projection_data(canonical_job_id)
            if job is None:
                raise RuntimeError(
                    f"canonical job {canonical_job_id} is missing its projection data"
                )
            self._matcher.run_match_for_request(candidate_id, canonical_job_id, now)
            decision = self._inbox.evaluate_job(
                candidate_id,
                canonical_job_id=canonical_job_id,
                job_title=str(job["title"]),
                job_location=str(job["location"]),
                job_text=str(job["text"]),
                company_name=str(job["company_name"]),
                aggregate_state=str(job["aggregate_state"]),
                source_states=set(job["source_states"]),
                job_compensation=job.get("compensation"),
                now=now,
            )
            if BlockingReason.NO_ACTIVE_VERSION in decision.blocking_reasons:
                raise RuntimeError(
                    f"candidate {candidate_id} has no active profile for inbox projection"
                )
            projected += 1
        return projected
