"""Automated job crawler: discovers and fetches jobs from public ATS APIs.

Usage:
    uv run python scripts/crawl_jobs.py [--dry-run]

Crawls a seed list of companies across Greenhouse, Lever, and Ashby,
ingesting postings into the CareerOps database with dedup and versioning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
    # Lever boards
    {"company": "Airbnb", "type": "lever", "board": "airbnb"},
    {"company": "Shopify", "type": "lever", "board": "shopify"},
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
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def fetch_json(url: str) -> object | None:
    """Fetch JSON from a URL with retries and rate limiting."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "CareerOps/0.1 (job discovery; +https://github.com/careerops)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
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
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < _MAX_RETRIES:
                time.sleep(2)
                continue
            print(f"    Network error: {e}")
            return None
    return None


def crawl_greenhouse(company: str, board: str) -> list[CrawledJob]:
    url = GREENHOUSE_API.format(board=board)
    data = fetch_json(url)
    if not isinstance(data, dict):
        return []
    jobs_raw = data.get("jobs", [])
    if not isinstance(jobs_raw, list):
        return []
    results = []
    for job in jobs_raw:
        if not isinstance(job, dict):
            continue
        location = ""
        loc_obj = job.get("location", {})
        if isinstance(loc_obj, dict):
            location = loc_obj.get("name", "")
        results.append(
            CrawledJob(
                company=company,
                source_type="greenhouse",
                external_id=str(job.get("id", "")),
                title=str(job.get("title", "")),
                location=location,
                url=str(job.get("absolute_url", "")),
                raw_data=job,
            )
        )
    return results


def crawl_lever(company: str, board: str) -> list[CrawledJob]:
    url = LEVER_API.format(board=board)
    data = fetch_json(url)
    if not isinstance(data, list):
        return []
    results = []
    for job in data:
        if not isinstance(job, dict):
            continue
        categories = job.get("categories", {})
        location = ""
        if isinstance(categories, dict):
            location = categories.get("location", "")
        results.append(
            CrawledJob(
                company=company,
                source_type="lever",
                external_id=str(job.get("id", "")),
                title=str(job.get("text", "")),
                location=location,
                url=str(job.get("hostedUrl", "")),
                raw_data=job,
            )
        )
    return results


def crawl_ashby(company: str, board: str) -> list[CrawledJob]:
    url = ASHBY_API.format(board=board)
    data = fetch_json(url)
    if not isinstance(data, dict):
        return []
    jobs_raw = data.get("jobs", [])
    if not isinstance(jobs_raw, list):
        return []
    results = []
    for job in jobs_raw:
        if not isinstance(job, dict):
            continue
        results.append(
            CrawledJob(
                company=company,
                source_type="ashby",
                external_id=str(job.get("id", "")),
                title=str(job.get("title", "")),
                location=str(job.get("location", "")),
                url=str(job.get("url", "")),
                raw_data=job,
            )
        )
    return results


CRAWLERS = {
    "greenhouse": crawl_greenhouse,
    "lever": crawl_lever,
    "ashby": crawl_ashby,
}


def crawl_all(dry_run: bool = False) -> None:
    total_jobs = 0
    total_sources = 0
    failed_sources = 0

    print(f"CareerOps Job Crawler - {datetime.now(UTC).isoformat()}")
    print(f"Seed sources: {len(SEED_SOURCES)}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 60)

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

            if not dry_run:
                # Save to a JSONL file for ingestion
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                output_file = f"data/crawl_{source_type}_{board}_{timestamp}.jsonl"
                import os

                os.makedirs("data", exist_ok=True)
                with open(output_file, "w", encoding="utf-8") as f:
                    for job in jobs:
                        record = {
                            "company": job.company,
                            "source_type": job.source_type,
                            "external_id": job.external_id,
                            "title": job.title,
                            "location": job.location,
                            "url": job.url,
                            "fetched_at": job.fetched_at.isoformat(),
                            "content_hash": hashlib.sha256(
                                json.dumps(job.raw_data, sort_keys=True, default=str).encode()
                            ).hexdigest(),
                        }
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")

        time.sleep(_RATE_LIMIT_SECONDS)

    print("=" * 60)
    print(f"Summary: {total_jobs} jobs from {total_sources} sources ({failed_sources} failed)")
    if not dry_run and total_jobs > 0:
        print("Output: data/crawl_*.jsonl")
        print("To ingest into DB, run the ingestion pipeline.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CareerOps automated job crawler")
    parser.add_argument("--dry-run", action="store_true", help="Only count jobs, don't save")
    args = parser.parse_args()
    crawl_all(dry_run=args.dry_run)
