"""Real ``CrawlActivitySink`` backed by http_fetcher + JobSourceAdapter.

Implements the ``CrawlActivitySink`` protocol from ``m1_activities.py`` by
fetching the source URL via ``http_fetcher.fetch`` and running the matching
``JobSourceAdapter.list_jobs`` parser. Each ``RawJobRecord`` is mapped to a
``CrawledPostingRecord`` with the fetch provenance attached.

``ingest_posting`` writes to ``job_postings`` + ``job_posting_versions``
with dedup by ``source_id + external_id`` (posting) and
``job_posting_id + content_hash`` (version).

Section 5 extends the sink with ``crawl_source_with_signals`` which returns
a ``CrawlSourceResult`` carrying postings plus HTTP status, body prefix, and
parse-drift signals for the backoff policy (task 5.7).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError, loads
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.adapters.http_fetcher import FetchedResponse, fetch
from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseAdapter,
    JobSourceAdapter,
    JsonLdAdapter,
    LeverAdapter,
    RawJobRecord,
    SitemapAdapter,
    StaticHtmlAdapter,
)
from careerops.infrastructure.database.schema import job_posting_versions, job_postings
from careerops.infrastructure.temporal.ego_browser_executor import EgoBrowserExecutor
from careerops.workflows.m1_contracts import (
    CrawledPostingRecord,
    CrawlJobSourceInput,
)

FetcherFn = Callable[[str], FetchedResponse]

# Expected fields that every adapter should produce in structured_data.
# If these are missing and zero jobs were found, it signals parse drift.
_EXPECTED_POSTING_FIELDS = ("title",)


@dataclasses.dataclass(frozen=True, slots=True)
class CrawlSourceResult:
    """Extended result from ``crawl_source_with_signals``.

    Carries the postings plus HTTP-level and parse-level signals the backoff
    policy (task 5.7) needs to evaluate 403/429/CAPTCHA/parse-drift.
    The existing ``crawl_source`` method is unchanged — callers that do not
    need signals continue to use it.
    """

    postings: tuple[CrawledPostingRecord, ...]
    status_code: int = 0
    body_prefix: str = ""
    expected_fields_missing: tuple[str, ...] = ()


_ADAPTER_REGISTRY: dict[str, JobSourceAdapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "json_ld": JsonLdAdapter(),
    "sitemap": SitemapAdapter(),
    "static_html": StaticHtmlAdapter(),
}


def _parse_body(body: str) -> object:
    """Parse a fetch body into the form its adapter expects."""
    stripped = body.lstrip()
    if stripped and stripped[0] in "{[":
        try:
            return loads(body)
        except (JSONDecodeError, ValueError):
            return body
    return body


def _content_hash(structured_data: dict[str, str]) -> str:
    """Deterministic SHA-256 hex digest of the structured data payload."""
    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# Default provenance/parser_version when a raw record carries no explicit tag.
# Real records from the agent/extractor carry ``api-capture`` or
# ``llm-extraction``; this is only a defensive fallback.
_DEFAULT_PROVENANCE = "llm-extraction"


def _to_crawled_posting(
    raw: RawJobRecord,
    source_id: UUID | str,
    source_url: str,
    fetched_at: datetime,
    *,
    parser_version: str | None = None,
) -> CrawledPostingRecord:
    """Map a crawled :class:`RawJobRecord` to the sink's :class:`CrawledPostingRecord`.

    This is the single shared mapper for the Tier 2 path (real-autonomous-
    career-loop Phase 4): both the injected ``CrawlAgent`` results and any
    direct LLM extraction flow through it, so provenance is carried consistently
    into ``parser_version`` — the column ``ingest_posting`` writes to
    ``job_posting_versions``.

    ``parser_version`` defaults to the record's ``provenance`` (``api-capture``
    for captured job APIs, ``llm-extraction`` for model-extracted pages), which
    is what distinguishes Tier 2 postings from structured Tier 1 adapter output.
    """
    return CrawledPostingRecord(
        source_id=str(source_id),
        external_id=raw.external_id,
        canonical_url=raw.url or source_url,
        source_url=source_url,
        structured_data={
            "title": raw.title,
            "location": raw.location,
            "description": raw.description,
            "apply_url": raw.url,
        },
        parser_version=parser_version or raw.provenance or _DEFAULT_PROVENANCE,
        fetched_at=fetched_at.isoformat(),
    )


@dataclasses.dataclass(slots=True)
class CrawlCounters:
    """Per-source ingest counters for the canonical Tier 2 ingest path.

    Mirrors the Section-5 run counters at single-source granularity:
    ``discovered`` for new postings, ``updated`` for new versions of existing
    postings, ``failed`` for ingest errors. Used by :func:`ingest_crawled_records`
    so the same mapping + ingest + counting logic is shared between the
    execution service and direct callers (no parallel ingest implementation).
    """

    discovered: int = 0
    updated: int = 0
    failed: int = 0


async def ingest_crawled_records(
    sink: RealCrawlActivitySink,
    records: list[RawJobRecord],
    *,
    source_id: UUID | str,
    source_url: str,
    crawl_run_id: UUID | None = None,
    plan_version_id: UUID | None = None,
    fetched_at: datetime | None = None,
) -> CrawlCounters:
    """Map + ingest crawled records through the canonical ``ingest_posting`` path.

    Folds the former ``CrawlAgentService.crawl_and_ingest`` responsibility into
    the crawl sink module: every record is mapped via ``_to_crawled_posting``
    (preserving ``llm-extraction`` / ``api-capture`` provenance) and ingested
    with the run/plan-version ids, accumulating :class:`CrawlCounters` from the
    idempotent ``is_new_posting`` / ``is_new_version`` flags. This is the same
    path :class:`CrawlExecutionService` uses, so there is one ingest contract.
    """
    counters = CrawlCounters()
    now = fetched_at or datetime.now(tz=UTC)
    for raw in records:
        posting = _to_crawled_posting(raw, source_id, source_url, now)
        try:
            result = await sink.ingest_posting(
                posting,
                crawl_run_id=crawl_run_id,
                plan_version_id=plan_version_id,
            )
        except Exception:
            counters.failed += 1
            continue
        if result.get("is_new_posting"):
            counters.discovered += 1
        elif result.get("is_new_version"):
            counters.updated += 1
    return counters


class RealCrawlActivitySink:
    """Activity-side crawl adapter backed by http_fetcher + adapters.

    For each ``CrawlJobSourceInput``, fetches the ``base_url``, runs the
    list adapter matching ``source_type``, and returns the crawled postings.

    ``ingest_posting`` writes rows to ``job_postings`` and
    ``job_posting_versions`` with idempotent upserts (ON CONFLICT DO NOTHING).
    """

    def __init__(
        self,
        adapters: dict[str, JobSourceAdapter] | None = None,
        *,
        fetcher: FetcherFn | None = None,
        engine: Engine | None = None,
        browser_executor: EgoBrowserExecutor | None = None,
        agent: object | None = None,
    ) -> None:
        self._adapters = adapters or dict(_ADAPTER_REGISTRY)
        # Allow injecting a fake fetcher for tests.
        self._fetch: FetcherFn = fetcher or fetch
        self._engine = engine
        self._browser = browser_executor
        # Tier 2 multi-step agent (CrawlAgent). When set, ego sources are
        # delegated to it (navigate + API capture + LLM extraction) instead of
        # the single-shot structured capture. ``object`` typed to avoid an
        # import cycle with ``application.crawl_agent``; it is duck-typed as
        # ``.crawl(url) -> list[RawJobRecord]``.
        self._agent = agent

    def _fetch_for_request(self, request: CrawlJobSourceInput, url: str) -> FetchedResponse:
        if request.executor_mode == "http":
            return self._fetch(url)
        if request.executor_mode == "ego":
            if self._browser is None:
                raise RuntimeError("ego browser executor is not configured")
            return self._browser.fetch(url)
        raise RuntimeError("unsupported crawl executor mode")

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        adapter = self._adapters.get(request.source_type)
        if adapter is None:
            return []

        resp: FetchedResponse = self._fetch_for_request(request, request.base_url)
        result = adapter.list_jobs(_parse_body(resp.body))

        fetched_at = resp.fetched_at.isoformat()
        postings: list[CrawledPostingRecord] = []

        # For Greenhouse: fetch detail endpoint for each job to get description (content).
        # Greenhouse list API does not include the JD body.
        detail_cache: dict[str, str] = {}
        if request.source_type == "greenhouse":
            detail_cache = self._fetch_greenhouse_details(
                request.base_url, result.jobs, executor_mode=request.executor_mode
            )

        for record in result.jobs:
            # Flatten to dict[str, str] — Temporal JSON converter rejects
            # ``object`` values; stringify non-string scalars.
            raw: dict[str, str] = {}
            for k, v in record.raw_data.items():
                raw[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

            description = record.description or detail_cache.get(record.external_id, "")

            postings.append(
                CrawledPostingRecord(
                    source_id=request.source_id,
                    external_id=record.external_id,
                    canonical_url=record.url or request.base_url,
                    source_url=resp.final_url or request.base_url,
                    structured_data={
                        "title": record.title or "",
                        "location": record.location or "",
                        "description": description,
                        "apply_url": record.url or "",
                        **raw,
                    },
                    parser_version=adapter.parser_version,
                    fetched_at=fetched_at,
                )
            )
        return postings

    async def crawl_source_with_signals(self, request: CrawlJobSourceInput) -> CrawlSourceResult:
        """Fetch and parse a source, returning postings plus backoff signals.

        Section 5 extension (task 5.7): wraps the same fetch + adapter logic
        as ``crawl_source`` but captures HTTP status code, body prefix (for
        CAPTCHA/login-wall detection), and expected-field parse-drift signals.
        The existing ``crawl_source`` method is unchanged — callers that do
        not need signals continue to use it.

        If the adapter is not found, returns an empty result with status 0.
        If the fetch raises, the exception propagates to the caller (the
        execution service catches it and increments the failed counter).

        Tier 2 (real-autonomous-career-loop Phase 4): when a multi-step
        ``CrawlAgent`` is wired, ego sources are delegated to it (navigate +
        API capture + LLM extraction) and its ``RawJobRecord`` results are
        mapped through ``_to_crawled_posting`` so provenance flows into
        ``parser_version``. The structured Tier 1 path below is unchanged and
        remains the default when no agent is configured.
        """
        # Tier 2 (Phase 4): a multi-step agent is used as a FALLBACK for ego
        # sources, not a replacement for structured parsing. Structured Tier 1
        # adapters (json_ld / static_html / ATS) parse first; only when they
        # yield nothing (parse drift) — or when no adapter exists for the source
        # type — does the sink delegate to the injected CrawlAgent.
        agent_available = request.executor_mode == "ego" and self._agent is not None

        adapter = self._adapters.get(request.source_type)
        if adapter is None:
            if agent_available:
                return await self._crawl_via_agent(request)
            return CrawlSourceResult(postings=())

        resp: FetchedResponse = self._fetch_for_request(request, request.base_url)
        result = adapter.list_jobs(_parse_body(resp.body))

        fetched_at = resp.fetched_at.isoformat()
        postings: list[CrawledPostingRecord] = []

        # For Greenhouse: fetch detail endpoint for each job to get description.
        detail_cache: dict[str, str] = {}
        if request.source_type == "greenhouse":
            detail_cache = self._fetch_greenhouse_details(
                request.base_url, result.jobs, executor_mode=request.executor_mode
            )

        for record in result.jobs:
            raw: dict[str, str] = {}
            for k, v in record.raw_data.items():
                raw[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

            description = record.description or detail_cache.get(record.external_id, "")

            postings.append(
                CrawledPostingRecord(
                    source_id=request.source_id,
                    external_id=record.external_id,
                    canonical_url=record.url or request.base_url,
                    source_url=resp.final_url or request.base_url,
                    structured_data={
                        "title": record.title or "",
                        "location": record.location or "",
                        "description": description,
                        "apply_url": record.url or "",
                        **raw,
                    },
                    parser_version=adapter.parser_version,
                    fetched_at=fetched_at,
                )
            )

        # Tier 2 fallback: the structured adapter parsed zero postings on an
        # ego source (parse drift, or a JS-rendered page the adapter cannot
        # read). Delegate to the multi-step agent before declaring the source
        # empty, so discovered dynamic sources still get a reasoned attempt.
        if not postings and agent_available:
            return await self._crawl_via_agent(request)

        # Detect parse drift: expected fields missing across all postings.
        missing_fields: list[str] = []
        if postings:
            # Check the first posting for expected fields.
            sample = postings[0].structured_data
            for field_name in _EXPECTED_POSTING_FIELDS:
                if not sample.get(field_name):
                    missing_fields.append(field_name)
        elif result.jobs:
            # Jobs were found by the adapter but produced no postings — unusual.
            missing_fields.extend(_EXPECTED_POSTING_FIELDS)

        return CrawlSourceResult(
            postings=tuple(postings),
            status_code=resp.status_code,
            body_prefix=resp.body[:4096],
            expected_fields_missing=tuple(missing_fields),
        )

    async def _crawl_via_agent(self, request: CrawlJobSourceInput) -> CrawlSourceResult:
        """Tier 2 ego crawl: delegate to the injected ``CrawlAgent``.

        The agent drives the browser (navigate, capture network, LLM extraction)
        and returns ``RawJobRecord`` objects. Each is mapped through
        :func:`_to_crawled_posting`, which carries the record's provenance
        (``api-capture`` / ``llm-extraction``) into ``parser_version`` so the
        downstream ``ingest_posting`` writes it to ``job_posting_versions``.
        """
        fetched_at = datetime.now(tz=UTC)
        raw_records = self._agent.crawl(request.base_url)  # type: ignore[union-attr]
        postings = tuple(
            _to_crawled_posting(raw, request.source_id, request.base_url, fetched_at)
            for raw in raw_records
        )
        return CrawlSourceResult(
            postings=postings,
            status_code=200 if postings else 0,
        )

    def _fetch_greenhouse_details(
        self,
        base_url: str,
        jobs: tuple[Any, ...],
        *,
        batch_size: int = 10,
        executor_mode: str = "http",
    ) -> dict[str, str]:
        """Fetch Greenhouse detail endpoint for each job to get description.

        Constructs detail URL by appending /{id} to the list URL.
        Returns a map of external_id -> description (HTML content).
        """
        from careerops.adapters.job_sources import GreenhouseDetailAdapter

        detail_adapter = GreenhouseDetailAdapter()
        descriptions: dict[str, str] = {}

        # Process in batches to stay within timeout
        job_list = list(jobs)
        for i in range(0, len(job_list), batch_size):
            batch = job_list[i : i + batch_size]
            for record in batch:
                ext_id = record.external_id
                if not ext_id:
                    continue
                detail_url = f"{base_url.rstrip('/')}/{ext_id}"
                detail_description = ""
                try:
                    detail_request = CrawlJobSourceInput(
                        source_id="detail",
                        company_id="",
                        company_name="",
                        source_type="greenhouse",
                        base_url=detail_url,
                        executor_mode=executor_mode,
                    )
                    detail_resp = self._fetch_for_request(detail_request, detail_url)
                    detail_data = _parse_body(detail_resp.body)
                    detail_record = detail_adapter.fetch_job(
                        detail_data,
                        source_url=detail_url,
                        fetched_at=detail_resp.fetched_at,
                    )
                    detail_description = detail_record.description
                except Exception:
                    detail_description = ""  # List data remains useful on detail failure.
                if detail_description:
                    descriptions[ext_id] = detail_description
        return descriptions

    async def ingest_posting(
        self,
        record: CrawledPostingRecord,
        *,
        crawl_run_id: UUID | None = None,
        plan_version_id: UUID | None = None,
    ) -> dict[str, bool]:
        """Insert into job_postings + job_posting_versions with idempotent dedup.

        Dedup rules:
        - Posting: unique on (source_id, external_id).
        - Version: unique on (job_posting_id, content_hash).

        Returns ``is_new_posting`` / ``is_new_version`` flags matching the
        workflow contract.

        Section 5 provenance (tasks 5.1, 5.5, 5.6): when
        ``crawl_run_id`` / ``plan_version_id`` are supplied they are recorded
        on the version row so every ingested posting is traceable to the run
        and plan-version snapshot that produced it. Both are optional and
        nullable so pre-Section-5 callers (Temporal M1 workflows) continue to
        work unchanged. The idempotent ON CONFLICT DO NOTHING dedup logic is
        NOT modified — provenance columns only appear in the VALUES clause.
        """
        if self._engine is None:
            raise RuntimeError("ingest_posting requires an Engine")

        now = datetime.now(tz=UTC)
        content_hash = _content_hash(record.structured_data)
        fetched_at = datetime.fromisoformat(record.fetched_at) if record.fetched_at else now
        source_id = UUID(record.source_id)

        with self._engine.begin() as conn:
            # --- Upsert job_postings ---
            insert_posting = (
                pg_insert(job_postings)
                .values(
                    id=uuid4(),
                    source_id=source_id,
                    external_id=record.external_id,
                    canonical_url=record.canonical_url,
                    source_state="active",
                    first_seen_at=fetched_at,
                    last_seen_at=fetched_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        job_postings.c.source_id,
                        job_postings.c.external_id,
                    ],
                )
                .returning(job_postings.c.id)
            )
            row = conn.execute(insert_posting).first()
            if row is not None:
                is_new_posting = True
                posting_id: UUID = row[0]
            else:
                is_new_posting = False
                posting_id = conn.execute(
                    sa.select(job_postings.c.id).where(
                        job_postings.c.source_id == source_id,
                        job_postings.c.external_id == record.external_id,
                    )
                ).scalar_one()

            # --- Upsert job_posting_versions ---
            # Provenance columns (crawl_run_id, plan_version_id) are added
            # to the VALUES clause only when provided; they are nullable so
            # existing callers that do not supply them continue to work.
            version_values: dict[str, object] = {
                "id": uuid4(),
                "job_posting_id": posting_id,
                "content_hash": content_hash,
                "source_url": record.source_url,
                "parser_version": record.parser_version,
                "structured_data": record.structured_data,
                "captured_at": fetched_at,
            }
            if crawl_run_id is not None:
                version_values["crawl_run_id"] = crawl_run_id
            if plan_version_id is not None:
                version_values["plan_version_id"] = plan_version_id

            insert_version = (
                pg_insert(job_posting_versions)
                .values(**version_values)
                .on_conflict_do_nothing(
                    index_elements=[
                        job_posting_versions.c.job_posting_id,
                        job_posting_versions.c.content_hash,
                    ],
                )
                .returning(job_posting_versions.c.id)
            )
            version_row = conn.execute(insert_version).first()
            is_new_version = version_row is not None

            # Touch last_seen_at on every visit.
            if not is_new_posting:
                conn.execute(
                    sa.update(job_postings)
                    .where(job_postings.c.id == posting_id)
                    .values(last_seen_at=fetched_at)
                )

        return {"is_new_posting": is_new_posting, "is_new_version": is_new_version}
