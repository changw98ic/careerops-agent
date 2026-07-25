"""Real ``CrawlActivitySink`` backed by http_fetcher + JobSourceAdapter.

Implements the ``CrawlActivitySink`` protocol from ``m1_activities.py`` by
fetching the source URL via ``http_fetcher.fetch`` and running the matching
``JobSourceAdapter.list_jobs`` parser. Each ``RawJobRecord`` is mapped to a
``CrawledPostingRecord`` with the fetch provenance attached.

``ingest_posting`` writes to ``job_postings`` + ``job_posting_versions``
with dedup by ``source_id + external_id`` (posting) and
``job_posting_id + content_hash`` (version).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError, loads
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
    SitemapAdapter,
    StaticHtmlAdapter,
)
from careerops.infrastructure.database.schema import job_posting_versions, job_postings
from careerops.workflows.m1_contracts import (
    CrawledPostingRecord,
    CrawlJobSourceInput,
)

FetcherFn = Callable[[str], FetchedResponse]

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
    ) -> None:
        self._adapters = adapters or dict(_ADAPTER_REGISTRY)
        # Allow injecting a fake fetcher for tests.
        self._fetch: FetcherFn = fetcher or fetch
        self._engine = engine

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        adapter = self._adapters.get(request.source_type)
        if adapter is None:
            return []

        resp: FetchedResponse = self._fetch(request.base_url)
        result = adapter.list_jobs(_parse_body(resp.body))

        fetched_at = resp.fetched_at.isoformat()
        postings: list[CrawledPostingRecord] = []

        # For Greenhouse: fetch detail endpoint for each job to get description (content).
        # Greenhouse list API does not include the JD body.
        detail_cache: dict[str, str] = {}
        if request.source_type == "greenhouse":
            detail_cache = self._fetch_greenhouse_details(
                request.base_url, result.jobs
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

    def _fetch_greenhouse_details(
        self,
        base_url: str,
        jobs: tuple[Any, ...],
        *,
        batch_size: int = 10,
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
                try:
                    detail_resp = self._fetch(detail_url)
                    detail_data = _parse_body(detail_resp.body)
                    detail_record = detail_adapter.fetch_job(
                        detail_data,
                        source_url=detail_url,
                        fetched_at=detail_resp.fetched_at,
                    )
                    if detail_record.description:
                        descriptions[ext_id] = detail_record.description
                except Exception:
                    pass  # Skip failed detail fetches; list data is still useful
        return descriptions

    async def ingest_posting(self, record: CrawledPostingRecord) -> dict[str, bool]:
        """Insert into job_postings + job_posting_versions with idempotent dedup.

        Dedup rules:
        - Posting: unique on (source_id, external_id).
        - Version: unique on (job_posting_id, content_hash).

        Returns ``is_new_posting`` / ``is_new_version`` flags matching the
        workflow contract.
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
            insert_version = (
                pg_insert(job_posting_versions)
                .values(
                    id=uuid4(),
                    job_posting_id=posting_id,
                    content_hash=content_hash,
                    source_url=record.source_url,
                    parser_version=record.parser_version,
                    structured_data=record.structured_data,
                    captured_at=fetched_at,
                )
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
