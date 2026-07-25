"""Automated job crawler: discovers and fetches jobs from public ATS APIs.

Usage:
    uv run python scripts/crawl_jobs.py [--dry-run]

Crawls a seed list of companies across Greenhouse, Lever, and Ashby,
ingesting postings into the CareerOps database with dedup and versioning.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.adapters import (
    AshbyAdapter,
    GreenhouseAdapter,
    GreenhouseDetailAdapter,
    LeverAdapter,
    RawJobRecord,
)
from careerops.adapters.http_fetcher import (
    CircuitOpenError,
    FetchError,
    SSRFError,
    fetch,
)
from careerops.config import Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.postgres_job_repo import PostgresJobReadRepository
from careerops.infrastructure.database.schema import companies, job_sources
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawledPostingRecord

# --- Seed list: companies with known public ATS boards ---

SEED_SOURCES: list[dict[str, str]] = [
    # Greenhouse boards
    {"company": "Stripe", "type": "greenhouse", "board": "stripe"},
    {"company": "Datadog", "type": "greenhouse", "board": "datadog"},
    {"company": "Coinbase", "type": "greenhouse", "board": "coinbase"},
    {"company": "Figma", "type": "greenhouse", "board": "figma"},
    {"company": "Notion", "type": "greenhouse", "board": "notion"},
    {"company": "Ramp", "type": "greenhouse", "board": "ramp"},
    {"company": "Vercel", "type": "greenhouse", "board": "vercel"},
    {"company": "Retool", "type": "greenhouse", "board": "retool"},
    {"company": "Airtable", "type": "greenhouse", "board": "airtable"},
    {"company": "Plaid", "type": "greenhouse", "board": "plaid"},
    {"company": "Scale AI", "type": "greenhouse", "board": "scaleai"},
    {"company": "Rippling", "type": "greenhouse", "board": "rippling"},
    {"company": "Gong", "type": "greenhouse", "board": "gong"},
    {"company": "Wiz", "type": "greenhouse", "board": "wiz"},
    {"company": "Linear", "type": "greenhouse", "board": "linear"},
    # Lever boards (airbnb/shopify removed — 404 as of 2026-07)
    {"company": "Netflix", "type": "lever", "board": "netflix"},
    # Ashby boards
    {"company": "OpenAI", "type": "ashby", "board": "openai"},
    {"company": "Anthropic", "type": "ashby", "board": "anthropic"},
    {"company": "Perplexity", "type": "ashby", "board": "perplexity"},
]

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

_TIMEOUT = 30
_RATE_LIMIT_SECONDS = 1.0
_MAX_RETRIES = 2


@dataclass
class CrawledJob:
    company: str
    source_type: str
    external_id: str
    title: str
    location: str
    url: str
    raw_data: dict = field(default_factory=dict)
    description: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --- Adapter instances (list + detail) ---

_GREENHOUSE_ADAPTER = GreenhouseAdapter()
_LEVER_ADAPTER = LeverAdapter()
_ASHBY_ADAPTER = AshbyAdapter()
_GREENHOUSE_DETAIL_ADAPTER = GreenhouseDetailAdapter()


def _extract_desc(raw: dict[str, Any], source_type: str) -> str:
    """Best-effort description extraction from list-payload raw_data.

    Greenhouse list omits the JD body entirely (fetched per-job via the
    detail endpoint in ``crawl_greenhouse``). Lever list carries both
    ``descriptionPlain`` (text) and ``description`` (HTML) -- prefer the
    plain text. Ashby list typically carries ``description`` (HTML) but
    not ``descriptionPlain``.
    """
    if source_type == "lever":
        return str(raw.get("descriptionPlain", "")) or str(raw.get("description", ""))
    if source_type == "ashby":
        return str(raw.get("description", "")) or str(raw.get("descriptionPlain", ""))
    return ""


def _raw_to_crawled(company: str, source_type: str, raw: RawJobRecord) -> CrawledJob:
    """Build a CrawledJob from an adapter's RawJobRecord.

    The list adapters leave ``description`` empty; for Lever/Ashby we pull
    it from ``raw_data`` here. Greenhouse description is filled later by
    the per-job detail fetch in ``crawl_greenhouse``.
    """
    description = raw.description or _extract_desc(raw.raw_data, source_type)
    return CrawledJob(
        company=company,
        source_type=source_type,
        external_id=raw.external_id,
        title=raw.title,
        location=raw.location,
        url=raw.url,
        description=description,
        raw_data=dict(raw.raw_data),
    )


def fetch_json(url: str) -> object | None:
    """Fetch JSON from a URL with retries and rate limiting.

    Uses careerops.adapters.http_fetcher.fetch which provides SSRF protection,
    circuit breaker, and per-domain rate limiting.  Custom headers are not
    supported by fetch(); the module's built-in User-Agent is used instead.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            result = fetch(url, timeout=float(_TIMEOUT))
            return json.loads(result.body)
        except urllib.error.HTTPError as e:
            # http_fetcher.fetch re-raises HTTPError after recording breaker state.
            if e.code == 429 and attempt < _MAX_RETRIES:
                retry_after = int(e.headers.get("Retry-After", "5"))
                print(f"    Rate limited, waiting {retry_after}s...")
                time.sleep(retry_after)
                continue
            if e.code == 404:
                return None
            if attempt < _MAX_RETRIES:
                time.sleep(2)
                continue
            print(f"    HTTP {e.code}: {e.reason}")
            return None
        except (SSRFError, CircuitOpenError, FetchError) as e:
            # Non-retryable: SSRF policy, circuit breaker, size violation
            print(f"    Fetch rejected: {e}")
            return None
        except (urllib.error.URLError, OSError) as e:
            if attempt < _MAX_RETRIES:
                time.sleep(2)
                continue
            print(f"    Network error: {e}")
            return None
    return None


def crawl_greenhouse(company: str, board: str) -> list[CrawledJob]:
    url = GREENHOUSE_API.format(board=board)
    data = fetch_json(url)
    result = _GREENHOUSE_ADAPTER.list_jobs(data if data is not None else {})
    jobs: list[CrawledJob] = []
    for raw in result.jobs:
        job = _raw_to_crawled(company, "greenhouse", raw)
        # Greenhouse list endpoint omits JD body; fetch per-job detail.
        detail_url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{raw.external_id}"
        detail_data = fetch_json(detail_url)
        if isinstance(detail_data, dict):
            detail = _GREENHOUSE_DETAIL_ADAPTER.fetch_job(
                detail_data,
                source_url=detail_url,
                fetched_at=datetime.now(UTC),
            )
            job.description = detail.description
        time.sleep(_RATE_LIMIT_SECONDS)
        jobs.append(job)
    return jobs


def crawl_lever(company: str, board: str) -> list[CrawledJob]:
    url = LEVER_API.format(board=board)
    data = fetch_json(url)
    result = _LEVER_ADAPTER.list_jobs(data if data is not None else [])
    return [_raw_to_crawled(company, "lever", raw) for raw in result.jobs]


def crawl_ashby(company: str, board: str) -> list[CrawledJob]:
    url = ASHBY_API.format(board=board)
    data = fetch_json(url)
    result = _ASHBY_ADAPTER.list_jobs(data if data is not None else {})
    # TODO: Ashby detail endpoint is not confirmed; description comes from
    # the list payload's ``description`` field only. If it is empty, the
    # JD body is left blank rather than guessing a detail URL.
    return [_raw_to_crawled(company, "ashby", raw) for raw in result.jobs]


CRAWLERS = {
    "greenhouse": crawl_greenhouse,
    "lever": crawl_lever,
    "ashby": crawl_ashby,
}


def _company_slug(name: str) -> str:
    """Lowercase, hyphen-joined company name for slug / domain derivation."""
    return name.lower().replace(" ", "-")


def _ensure_source_rows(
    engine: Engine,
    seed: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Upsert companies + job_sources for every seed entry.

    Returns ``(source_map, company_map)`` where:
    - ``source_map[board_slug]`` = job_source UUID
    - ``company_map[board_slug]`` = company UUID
    """
    source_map: dict[str, str] = {}
    company_map: dict[str, str] = {}
    with engine.begin() as conn:
        for entry in seed:
            company_name = entry["company"]
            source_type = entry["type"]
            board = entry["board"]
            slug = _company_slug(company_name)
            domain = f"{slug}.com"

            # Upsert company (idempotent on normalized_name).
            company_id = uuid5(NAMESPACE_DNS, f"careerops.company.{slug}")
            conn.execute(
                pg_insert(companies)
                .values(
                    id=company_id,
                    name=company_name,
                    normalized_name=slug,
                    official_domains=[domain],
                )
                .on_conflict_do_nothing(index_elements=[companies.c.normalized_name])
            )

            # Upsert job_source (idempotent on company_id, source_type, source_identifier).
            source_uuid = uuid5(NAMESPACE_DNS, f"careerops.board.{board}")
            base_url = _base_url_for(source_type, board)
            conn.execute(
                pg_insert(job_sources)
                .values(
                    id=source_uuid,
                    company_id=company_id,
                    source_type=source_type,
                    source_identifier=board,
                    base_url=base_url,
                    state="active",
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        job_sources.c.company_id,
                        job_sources.c.source_type,
                        job_sources.c.source_identifier,
                    ]
                )
            )
            source_map[board] = str(source_uuid)
            company_map[board] = str(company_id)
    return source_map, company_map


def _base_url_for(source_type: str, board: str) -> str:
    """Return the canonical API base URL for a source type + board slug."""
    if source_type == "greenhouse":
        return GREENHOUSE_API.format(board=board)
    if source_type == "lever":
        return LEVER_API.format(board=board)
    if source_type == "ashby":
        return ASHBY_API.format(board=board)
    return ""


_ADAPTER_VERSIONS = {
    "greenhouse": "greenhouse-v1",
    "lever": "lever-v1",
    "ashby": "ashby-v1",
}


def _job_to_record(job: CrawledJob, source_id: str) -> CrawledPostingRecord:
    """Convert a CrawledJob to a CrawledPostingRecord for DB ingest."""
    raw: dict[str, str] = {}
    for k, v in job.raw_data.items():
        raw[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return CrawledPostingRecord(
        source_id=source_id,
        external_id=job.external_id,
        canonical_url=job.url,
        source_url=job.url,
        structured_data={
            "title": job.title or "",
            "location": job.location or "",
            "description": job.description or "",
            **raw,
        },
        parser_version=_ADAPTER_VERSIONS.get(job.source_type, "unknown-v1"),
        fetched_at=job.fetched_at.isoformat(),
    )


def _ingest_jobs(
    sink: RealCrawlActivitySink,
    jobs: list[CrawledJob],
    source_id: str,
    company_name: str,
    company_id: str,
    job_repo: object | None = None,
) -> tuple[int, int, list[str]]:
    """Ingest a batch of CrawledJobs into the database.

    Runs the async ingest_posting in a dedicated event loop, then calls
    JobIngestionService to create canonical_jobs + job_posting_assignments.
    Returns (new_postings, new_versions, errors).
    """
    import asyncio

    from careerops.application.job_ingestion import JobIngestionService

    service = JobIngestionService(job_repo) if job_repo is not None else None

    async def _run() -> tuple[int, int, list[str]]:
        new_postings = 0
        new_versions = 0
        errors: list[str] = []
        for job in jobs:
            try:
                record = _job_to_record(job, source_id)
                result = await sink.ingest_posting(record)
                if result.get("is_new_posting"):
                    new_postings += 1
                if result.get("is_new_version"):
                    new_versions += 1

                if service is not None:
                    service.ingest_posting(
                        source_id=UUID(source_id),
                        external_id=record.external_id,
                        canonical_url=record.canonical_url,
                        structured_data=record.structured_data,
                        source_url=record.source_url,
                        parser_version=record.parser_version,
                        company_name=company_name,
                        now=datetime.now(UTC),
                        company_id=UUID(company_id),
                    )
            except Exception as exc:
                errors.append(f"{job.external_id}: {exc}")
        return new_postings, new_versions, errors

    return asyncio.run(_run())


def crawl_all(dry_run: bool = False) -> None:
    total_jobs = 0
    total_sources = 0
    failed_sources = 0
    total_new_postings = 0
    total_new_versions = 0
    all_errors: list[str] = []

    print(f"CareerOps Job Crawler - {datetime.now(UTC).isoformat()}")
    print(f"Seed sources: {len(SEED_SOURCES)}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 60)

    engine: Engine | None = None
    sink: RealCrawlActivitySink | None = None
    job_repo: PostgresJobReadRepository | None = None
    source_map: dict[str, str] = {}

    if not dry_run:
        settings = Settings()
        from careerops.config import RuntimeEnvironment

        engine = create_database_engine(
            settings, enforce_role=settings.environment is RuntimeEnvironment.PRODUCTION
        )
        sink = RealCrawlActivitySink(engine=engine)
        job_repo = PostgresJobReadRepository(engine)
        print("Ensuring companies and job_sources rows exist...")
        source_map, company_map = _ensure_source_rows(engine, SEED_SOURCES)
        print(f"  Resolved {len(source_map)} source IDs.")

    for source in SEED_SOURCES:
        company = source["company"]
        source_type = source["type"]
        board = source["board"]

        crawler = CRAWLERS.get(source_type)
        if crawler is None:
            print(f"  [{company}] No crawler for type '{source_type}'")
            failed_sources += 1
            continue

        print(f"  [{company}] Crawling {source_type}/{board}...", end=" ", flush=True)
        jobs = crawler(company, board)
        total_sources += 1

        if not jobs:
            print("0 jobs (failed or empty)")
            failed_sources += 1
        else:
            print(f"{len(jobs)} jobs")
            total_jobs += len(jobs)

            if sink is not None:
                sid = source_map.get(board)
                if sid is None:
                    all_errors.append(f"{company}: no source_id for board '{board}'")
                else:
                    cid = company_map.get(board, "")
                    np, nv, errs = _ingest_jobs(sink, jobs, sid, company, cid, job_repo)
                    total_new_postings += np
                    total_new_versions += nv
                    all_errors.extend(errs)
                    if np or nv:
                        print(f"    -> DB: {np} new postings, {nv} new versions")
                    if errs:
                        for err in errs:
                            print(f"    -> ERROR: {err}")

        time.sleep(_RATE_LIMIT_SECONDS)

    print("=" * 60)
    print(f"Summary: {total_jobs} jobs from {total_sources} sources ({failed_sources} failed)")
    if sink is not None:
        print(f"DB ingest: {total_new_postings} new postings, {total_new_versions} new versions")
        if all_errors:
            print(f"DB errors: {len(all_errors)}")
            for err in all_errors[:10]:
                print(f"  - {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CareerOps automated job crawler")
    parser.add_argument("--dry-run", action="store_true", help="Only count jobs, don't save")
    args = parser.parse_args()
    crawl_all(dry_run=args.dry_run)
