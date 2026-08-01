"""Automated job crawler over the declarative recipe catalog.

DEPRECATED (Phase C / Task 11). The hard-coded Greenhouse / Lever / Ashby
adapter classes and their per-source ``crawl_greenhouse`` / ``crawl_lever`` /
``crawl_ashby`` functions were removed when the crawl sink became fully
recipe-driven. The seed list and DB-upsert helpers below are kept so this
script can still seed ``job_sources`` rows and then run each source through
:class:`careerops.infrastructure.temporal.m1_crawl_sink.RealCrawlActivitySink`,
which dispatches every request to :class:`careerops.recipes.engine.RecipeEngine`
loaded from ``vendor/crawl-recipes/``.

Usage:
    uv run python scripts/crawl_jobs.py [--dry-run]

What the script does now:
1. Ensures the seed companies + ``job_sources`` rows exist (same as before).
2. Constructs a ``RealCrawlActivitySink`` against the live database.
3. For each seed entry, builds a :class:`CrawlJobSourceInput` and calls
   ``sink.crawl_source`` so the recipe engine handles fetch + parse, then
   ingests each posting via ``sink.ingest_posting``.

The bespoke ``CrawledJob`` shape, the per-ATS list/detail adapters, and the
``fetch_json`` retry loop are gone — the recipe engine plus
``http_fetcher.fetch`` already provide fetch retries, SSRF protection, and
the per-source structured parsing contract.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, uuid5

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.config import Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.schema import companies, job_sources
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawlJobSourceInput

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

# Per-source-type API endpoint templates. Used only to seed the
# ``job_sources.base_url`` column; the recipe engine owns the live endpoint
# contract (a recipe's ``fetch.endpoint`` is the source of truth at runtime).
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
LEVER_API = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

_RATE_LIMIT_SECONDS = 1.0


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
    """Return the canonical API base URL for a source type + board slug.

    The greenhouse URL carries ``?content=true`` so the list-step recipe
    inlines the JD body in the ``content`` field (the recipe design that
    ``test_greenhouse_bulk_fetch.py`` pins); the per-job detail fan-out is
    then a no-op for any row whose description already came back populated.
    """
    if source_type == "greenhouse":
        return GREENHOUSE_API.format(board=board) + "?content=true"
    if source_type == "lever":
        return LEVER_API.format(board=board)
    if source_type == "ashby":
        return ASHBY_API.format(board=board)
    return ""


async def _crawl_one(
    sink: RealCrawlActivitySink,
    *,
    source_id: str,
    company_name: str,
    source_type: str,
    board: str,
    base_url: str,
) -> tuple[int, int, list[str]]:
    """Run one seed source through the recipe-driven sink and ingest results.

    Returns ``(new_postings, new_versions, errors)``.
    """
    new_postings = 0
    new_versions = 0
    errors: list[str] = []

    request = CrawlJobSourceInput(
        source_id=source_id,
        company_id="",
        company_name=company_name,
        source_type=source_type,
        base_url=base_url,
        executor_mode="http",
    )
    try:
        postings = await sink.crawl_source(request)
    except Exception as exc:
        errors.append(f"{company_name}/{board}: crawl failed: {exc}")
        return 0, 0, errors

    for posting in postings:
        try:
            result = await sink.ingest_posting(posting)
            if result.get("is_new_posting"):
                new_postings += 1
            if result.get("is_new_version"):
                new_versions += 1
        except Exception as exc:
            errors.append(f"{company_name}/{board}/{posting.external_id}: {exc}")
    return new_postings, new_versions, errors


def crawl_all(dry_run: bool = False) -> None:
    print(f"CareerOps Job Crawler - {datetime.now(UTC).isoformat()}")
    print(f"Seed sources: {len(SEED_SOURCES)}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 60)

    if dry_run:
        print("Dry run: no database writes.")
        return

    settings = Settings()
    from careerops.config import RuntimeEnvironment

    engine = create_database_engine(
        settings, enforce_role=settings.environment is RuntimeEnvironment.PRODUCTION
    )
    sink = RealCrawlActivitySink(engine=engine)
    print("Ensuring companies and job_sources rows exist...")
    source_map, _company_map = _ensure_source_rows(engine, SEED_SOURCES)
    print(f"  Resolved {len(source_map)} source IDs.")

    import asyncio

    total_new_postings = 0
    total_new_versions = 0
    all_errors: list[str] = []
    total_sources = 0
    failed_sources = 0

    for source in SEED_SOURCES:
        company = source["company"]
        source_type = source["type"]
        board = source["board"]
        sid = source_map.get(board)
        if sid is None:
            all_errors.append(f"{company}: no source_id for board '{board}'")
            failed_sources += 1
            continue

        print(f"  [{company}] Crawling {source_type}/{board}...", end=" ", flush=True)
        try:
            np, nv, errs = asyncio.run(
                _crawl_one(
                    sink,
                    source_id=sid,
                    company_name=company,
                    source_type=source_type,
                    board=board,
                    base_url=_base_url_for(source_type, board),
                )
            )
        except Exception as exc:
            print(f"failed: {exc}")
            failed_sources += 1
            time.sleep(_RATE_LIMIT_SECONDS)
            continue

        total_sources += 1
        total_new_postings += np
        total_new_versions += nv
        all_errors.extend(errs)
        if np or nv:
            print(f"{np} new postings, {nv} new versions")
        else:
            print("up to date")
        if errs:
            for err in errs:
                print(f"    -> ERROR: {err}")

        time.sleep(_RATE_LIMIT_SECONDS)

    print("=" * 60)
    print(f"Summary: {total_sources} sources crawled ({failed_sources} failed)")
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
