"""Real sink -> ingest end-to-end regression (PR #7 review round 2).

The earlier ``test_crawl_full.py`` targeted the now-removed legacy
``JsonLdAdapter`` and was deleted when the 6 hard-coded adapters went away.
PR #7 round 2 rewrites the crawl sink internals substantially (the sink is now
a pure Tier 1 signal source; the agent escalation path was removed from it).
That change needs a real-link regression test guarding the structured Tier 1
chain end to end:

    real RecipeEngine (vendor/crawl-recipes/)
        -> real ``_records_to_postings`` mapping (CrawledPostingRecord)
        -> real ``JobIngestionService`` dedup / version logic.

It does NOT touch a real Postgres (the canonical projection schema uses
``JSONB`` columns SQLite cannot host); instead it drives the same
``JobIngestionService`` code that ``RealCrawlActivitySink.ingest_posting``
calls internally, via an in-memory repository double. The assertions that
matter for guarding this change — ``parser_version=recipe:<type>:<ver>``,
stable ``external_id``, idempotent re-ingest, and that the sink runs with no
agent attached — are all exercised here.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.application.job_ingestion import JobIngestionService
from careerops.domain.jobs import (
    AggregateState,
    CanonicalJob,
    JobPosting,
    JobPostingVersion,
    PostingSourceState,
)
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawlJobSourceInput

GREENHOUSE_LIST_BODY = json.dumps(
    {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "location": {"name": "Remote"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                "content": "<p>Build APIs.</p>",
            },
            {
                "id": 2,
                "title": "Frontend Engineer",
                "location": {"name": "NYC"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                "content": "<p>Build UIs.</p>",
            },
        ]
    }
)


def _greenhouse_fetcher(body: str = GREENHOUSE_LIST_BODY):
    """Fake fetcher returning the Greenhouse list JSON for any URL.

    The recipe's detail step short-circuits (``when: @.description==''``) when
    every list row already carries ``content``, so only the list fetch fires
    and the same body satisfies every call.
    """

    def fetch(url: str) -> FetchedResponse:
        return FetchedResponse(
            status_code=200,
            final_url=url,
            fetched_at=datetime(2026, 8, 2, tzinfo=UTC),
            response_hash="g" * 64,
            body=body,
        )

    return fetch


def _greenhouse_request() -> CrawlJobSourceInput:
    return CrawlJobSourceInput(
        source_id="source-greenhouse",
        company_id="company-1",
        company_name="Acme",
        source_type="greenhouse",
        base_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        executor_mode="http",
    )


class _InMemoryJobRepository:
    """In-memory JobRepository satisfying the Protocol JobIngestionService
    consumes. Mirrors the production ``ingest_posting`` ingest path so this
    test exercises the real dedup / version logic without a Postgres."""

    def __init__(self) -> None:
        self.postings: dict[object, JobPosting] = {}
        self.versions: dict[object, JobPostingVersion] = {}
        self.canonical_jobs: dict[object, CanonicalJob] = {}
        self._posting_by_source_ext: dict[tuple[object, str], JobPosting] = {}
        self._versions_by_posting: dict[object, list[JobPostingVersion]] = {}
        self._postings_by_canonical: dict[object, list[JobPosting]] = {}
        self._canonical_by_posting: dict[object, UUID] = {}
        self._fingerprint_by_canonical: dict[str, object] = {}

    def find_posting_by_source_and_external_id(
        self, source_id: object, external_id: str
    ) -> JobPosting | None:
        return self._posting_by_source_ext.get((source_id, external_id))

    def find_latest_version(self, posting_id: object) -> JobPostingVersion | None:
        versions = self._versions_by_posting.get(posting_id, [])
        return versions[-1] if versions else None

    def find_canonical_by_fingerprint(self, fingerprint: str) -> CanonicalJob | None:
        canonical_id = self._fingerprint_by_canonical.get(fingerprint)
        if canonical_id is None:
            return None
        return self.canonical_jobs.get(canonical_id)

    def find_canonical_by_id(self, canonical_job_id: object) -> CanonicalJob | None:
        return self.canonical_jobs.get(canonical_job_id)

    def find_canonical_for_posting(self, posting_id: object) -> UUID | None:
        return self._canonical_by_posting.get(posting_id)

    def find_postings_for_canonical(self, canonical_job_id: object) -> list[JobPosting]:
        return self._postings_by_canonical.get(canonical_job_id, [])

    def save_posting(self, posting: JobPosting) -> None:
        self.postings[posting.id] = posting
        self._posting_by_source_ext[(posting.source_id, posting.external_id)] = posting

    def save_version(self, version: JobPostingVersion) -> None:
        self.versions[version.id] = version
        self._versions_by_posting.setdefault(version.job_posting_id, []).append(version)

    def save_canonical_job(self, job: CanonicalJob) -> None:
        self.canonical_jobs[job.id] = job

    def save_merge_decision(self, decision: object) -> None:
        pass

    def save_posting_assignment(
        self, posting_id: object, canonical_job_id: object, decision_id: object
    ) -> None:
        del decision_id
        self._canonical_by_posting[posting_id] = canonical_job_id  # pyright: ignore[reportArgumentType]
        posting = self.postings.get(posting_id)
        if posting is not None:
            self._postings_by_canonical.setdefault(canonical_job_id, []).append(posting)

    def update_posting_state(
        self, posting_id: object, state: PostingSourceState, now: datetime
    ) -> None:
        posting = self.postings.get(posting_id)
        if posting:
            self.postings[posting_id] = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=posting.last_seen_at,
                closed_at=now if state == PostingSourceState.CLOSED else None,
            )

    def update_canonical_state(
        self, canonical_job_id: object, state: AggregateState, now: datetime
    ) -> None:
        del now
        job = self.canonical_jobs.get(canonical_job_id)
        if job:
            self.canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=state,
                primary_posting_id=job.primary_posting_id,
            )

    def update_posting_last_seen(self, posting_id: object, now: datetime) -> None:
        posting = self.postings.get(posting_id)
        if posting:
            self.postings[posting_id] = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=posting.source_state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=now,
            )

    def update_canonical_primary_posting(
        self, canonical_job_id: object, posting_id: object
    ) -> None:
        job = self.canonical_jobs.get(canonical_job_id)
        if job:
            self.canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=job.aggregate_state,
                primary_posting_id=UUID(str(posting_id)),
            )


@pytest.mark.asyncio
async def test_real_recipe_engine_maps_postings_with_recipe_provenance() -> None:
    """Real RecipeEngine + real mapping produces postings whose
    ``parser_version`` names the recipe and whose ``external_id`` is the
    recipe-extracted job id (stable across runs). This guards the Tier 1 sink
    contract that the round-2 refactor preserved."""
    sink = RealCrawlActivitySink(fetcher=_greenhouse_fetcher())
    result = await sink.crawl_source_with_signals(_greenhouse_request())

    assert result.had_recipe is True
    assert result.status_code == 200
    assert len(result.postings) == 2

    by_id = {p.external_id: p for p in result.postings}
    # external_id mirrors the recipe's ``id`` field (stable, not a hash here).
    assert set(by_id) == {"1", "2"}
    # parser_version names the recipe namespace.
    assert all(p.parser_version == "recipe:greenhouse:1" for p in result.postings)
    assert by_id["1"].structured_data["title"] == "Backend Engineer"
    # apply_url is the canonical Greenhouse posting URL the recipe mapped.
    assert by_id["1"].structured_data["apply_url"] == ("https://boards.greenhouse.io/acme/jobs/1")
    # description flowed through the html_unescape transform.
    assert by_id["2"].structured_data["description"] == "<p>Build UIs.</p>"


@pytest.mark.asyncio
async def test_postings_ingest_idempotently_through_real_job_ingestion_service() -> None:
    """The Tier 1 postings flow through the real ``JobIngestionService``
    (the same service ``RealCrawlActivitySink.ingest_posting`` calls) with
    idempotent dedup: first ingest is new, re-ingest of the same content is
    a no-op (no new posting, no new version)."""
    sink = RealCrawlActivitySink(fetcher=_greenhouse_fetcher())
    result = await sink.crawl_source_with_signals(_greenhouse_request())

    service = JobIngestionService(_InMemoryJobRepository())
    now = datetime(2026, 8, 2, tzinfo=UTC)
    source_id = uuid4()

    first = [
        service.ingest_posting(
            source_id=source_id,
            external_id=p.external_id,
            canonical_url=p.canonical_url,
            structured_data=dict(p.structured_data),
            source_url=p.source_url,
            parser_version=p.parser_version,
            company_name="Acme",
            now=now,
        )
        for p in result.postings
    ]
    assert all(r.is_new_posting for r in first)
    assert all(r.is_new_version for r in first)

    # Re-ingest identical content: idempotent — no new posting / version.
    second = [
        service.ingest_posting(
            source_id=source_id,
            external_id=p.external_id,
            canonical_url=p.canonical_url,
            structured_data=dict(p.structured_data),
            source_url=p.source_url,
            parser_version=p.parser_version,
            company_name="Acme",
            now=now,
        )
        for p in result.postings
    ]
    assert all(not r.is_new_posting for r in second)
    assert all(not r.is_new_version for r in second)
    # Same posting ids on re-ingest (dedup by source_id + external_id).
    assert {r.posting_id for r in second} == {r.posting_id for r in first}


def test_sink_constructor_has_no_agent_parameter() -> None:
    """Architecture guard (round 2): the sink must NOT accept an ``agent``
    parameter — it is a pure Tier 1 signal source and holds no Tier 2 agent
    reference. The agent is owned by ``BoundedTier2Orchestrator``."""
    sig = inspect.signature(RealCrawlActivitySink.__init__)
    assert "agent" not in sig.parameters
    assert "tier2_agent" not in sig.parameters
