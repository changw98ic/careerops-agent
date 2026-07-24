"""Full crawler: ATS APIs + Company career pages (ego-browser) + Reddit + X.

Usage:
    python3 scripts/crawl_full.py [--output-dir data/crawl_YYYYMMDD]

Requires: ego-browser CLI available in PATH.
Company career pages are rendered via ego-browser (handles JS/SPA sites).
Reddit and X are also crawled via ego-browser (direct API is blocked).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from careerops.adapters.job_sources import JsonLdAdapter, RawJobRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / f"crawl_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

# --- Company career pages to crawl via ego-browser ---
CAREER_PAGES = [
    ("Google", "https://www.google.com/about/careers/applications/jobs/results/"),
    ("Meta", "https://www.metacareers.com/jobs/"),
    ("Apple", "https://jobs.apple.com/en-us/search"),
    ("Microsoft", "https://careers.microsoft.com/us/en/search-results"),
    ("Amazon", "https://www.amazon.jobs/en/search"),
    ("Spotify", "https://www.lifeatspotify.com/jobs"),
    ("GitHub", "https://github.com/about/careers"),
    ("GitLab", "https://about.gitlab.com/jobs/"),
    ("Cloudflare", "https://www.cloudflare.com/careers/jobs/"),
    ("Supabase", "https://supabase.com/careers"),
    ("Neon", "https://neon.tech/careers"),
    ("Temporal", "https://temporal.io/careers"),
    ("Databricks", "https://www.databricks.com/company/careers"),
    ("Stripe", "https://stripe.com/jobs"),
    ("Airbnb", "https://careers.airbnb.com/positions/"),
    ("Netflix", "https://jobs.netflix.com/jobs"),
    ("Shopify", "https://www.shopify.com/careers"),
    ("Figma", "https://www.figma.com/careers/"),
    ("Notion", "https://www.notion.com/careers"),
    ("Vercel", "https://vercel.com/careers"),
]

REDDIT_SUBS = ["forhire", "remotejobs", "jobbit", "cscareerquestions"]

X_QUERIES = [
    '(hiring OR "we are hiring" OR "join our team") (software OR engineer OR developer) remote',
    '(hiring OR "is hiring") (senior OR staff OR principal) (engineer OR developer OR SRE) remote',
    '"we are hiring" (backend OR frontend OR fullstack OR platform) engineer',
]

# Phrases that indicate a job title vs noise
NOISE_PHRASES = [
    "cookie",
    "privacy",
    "sign in",
    "skip to",
    "search",
    "menu",
    "button",
    "next",
    "prev",
    "filter",
    "showing",
    "sort",
    "page ",
    "load more",
    "all rights",
    "terms of",
    "accessibility",
    "equal opportunity",
]


def run_ego(script: str, timeout: int = 60) -> str:
    """Run an ego-browser -e script and return stdout."""
    try:
        result = subprocess.run(
            ["ego-browser", "nodejs", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"ERROR: {e}"


def ego_save_page(url: str, output_file: str, wait_seconds: int = 8) -> bool:
    """Open a URL in ego-browser and save the snapshot text to a file."""
    script = (
        f"const task = await useOrCreateTaskSpace('career-crawl'); "
        f"await openOrReuseTab('{url}', {{ wait: true, timeout: 30 }}); "
        f"await wait({wait_seconds}); "
        f"await scrollBy(0, 1500); await wait(2); "
        f"const text = await snapshotText(); "
        f"const fs = await import('fs'); "
        f"fs.writeFileSync('{output_file}', text); "
        f"cliLog('saved ' + text.length)"
    )
    output = run_ego(script, timeout=60)
    return "saved" in output


def ego_save_raw_html(url: str, output_file: str, wait_seconds: int = 8) -> bool:
    """Open a URL in ego-browser and save the raw HTML (outerHTML) to a file.

    Unlike ego_save_page which uses snapshotText() (accessibility tree),
    this captures the actual DOM including <script> tags needed for JSON-LD.
    """
    script = (
        f"const task = await useOrCreateTaskSpace('career-crawl'); "
        f"await openOrReuseTab('{url}', {{ wait: true, timeout: 30 }}); "
        f"await wait({wait_seconds}); "
        f"await scrollBy(0, 1500); await wait(2); "
        f"const html = await evaluate(() => document.documentElement.outerHTML); "
        f"const fs = await import('fs'); "
        f"fs.writeFileSync('{output_file}', html); "
        f"cliLog('saved ' + html.length)"
    )
    output = run_ego(script, timeout=60)
    return "saved" in output


_JSONLD_ADAPTER = JsonLdAdapter()


def _records_to_dicts(
    records: tuple[RawJobRecord, ...], company: str, url: str
) -> list[dict[str, Any]]:
    """Map adapter RawJobRecords to the crawl output dict format."""
    return [
        {
            "company": company,
            "source_type": "career_page_jsonld",
            "title": r.title,
            "url": r.url or url,
            "location": r.location,
            "description": r.description,
            "fetched_at": datetime.now(UTC).isoformat(),
            "source_url": url,
        }
        for r in records
        if r.title
    ]


def parse_career_page(text: str, company: str, url: str) -> list[dict]:
    """Extract job listings from career page content.

    *text* may be raw HTML (from ego_save_raw_html) or an accessibility-tree
    snapshot (from ego_save_page / snapshotText).

    Strategy:
      1. Try JsonLdAdapter on the raw HTML to extract JobPosting JSON-LD.
      2. If no JSON-LD found, fall back to the legacy regex heuristic on the
         snapshot text.
    """
    # --- Strategy 1: JSON-LD via adapter ---
    result = _JSONLD_ADAPTER.list_jobs(text)
    if result.jobs:
        return _records_to_dicts(result.jobs, company, url)

    # --- Strategy 2: regex fallback (snapshotText format) ---
    jobs: list[dict[str, Any]] = []
    titles = re.findall(r'text "([^"]{10,150})"', text)
    role_keywords = [
        "engineer",
        "developer",
        "manager",
        "designer",
        "analyst",
        "scientist",
        "architect",
        "lead",
        "director",
        "specialist",
        "consultant",
        "administrator",
        "coordinator",
        "intern",
        "researcher",
        "product",
        "program",
        "operations",
        "marketing",
        "sales",
        "support",
        "infrastructure",
        "platform",
        "security",
        "data",
        "machine learning",
        "ai",
        "ml",
        "sre",
        "devops",
        "frontend",
        "backend",
        "fullstack",
        "full-stack",
        "mobile",
        "ios",
        "android",
        "cloud",
        "network",
        "systems",
        "software",
        "technical",
        "senior",
        "staff",
        "principal",
        "junior",
        "associate",
        "vp",
        "head of",
        "chief",
    ]
    seen: set[str] = set()
    for title in titles:
        title = title.strip()
        if title in seen or len(title) < 15:
            continue
        if any(noise in title.lower() for noise in NOISE_PHRASES):
            continue
        if any(kw in title.lower() for kw in role_keywords):
            seen.add(title)
            jobs.append(
                {
                    "company": company,
                    "source_type": "career_page",
                    "title": title,
                    "url": url,
                    "location": "",
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "source_url": url,
                }
            )
    return jobs


def crawl_career_pages(output_dir: Path) -> list[dict]:
    """Crawl all company career pages via ego-browser."""
    all_jobs = []
    tmp_dir = Path("/tmp/careerops_crawl")
    tmp_dir.mkdir(exist_ok=True)

    for company, url in CAREER_PAGES:
        slug = company.lower().replace(" ", "_")
        print(f"  [{company}] Rendering {url[:60]}...", end=" ", flush=True)
        tmp_html = str(tmp_dir / f"{slug}.html")

        if ego_save_raw_html(url, tmp_html):
            html = Path(tmp_html).read_text(errors="replace")
            jobs = parse_career_page(html, company, url)
            print(f"{len(jobs)} jobs")
            all_jobs.extend(jobs)
        else:
            print("failed")
        time.sleep(2)

    outfile = output_dir / "career_pages.jsonl"
    with open(outfile, "w", encoding="utf-8") as f:
        for job in all_jobs:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")
    print(f"  Total: {len(all_jobs)} jobs -> {outfile}")
    return all_jobs


def crawl_reddit(output_dir: Path) -> list[dict]:
    """Crawl Reddit hiring subreddits via ego-browser."""
    all_posts = []

    for sub in REDDIT_SUBS:
        print(f"  [r/{sub}] Crawling...", end=" ", flush=True)
        tmp_file = f"/tmp/careerops_crawl/reddit_{sub}.txt"
        url = f"https://www.reddit.com/r/{sub}/hot/"

        if ego_save_page(url, tmp_file, wait_seconds=6):
            text = Path(tmp_file).read_text(errors="replace")
            titles = re.findall(r'text "([^"]{15,200})"', text)
            posts = []
            seen = set()
            for t in titles:
                t = t.strip()
                if t in seen:
                    continue
                if any(noise in t.lower() for noise in ["跳到", "搜索", "cookie", "privacy"]):
                    continue
                if any(
                    kw in t.lower()
                    for kw in [
                        "hir",
                        "hire",
                        "remote",
                        "role",
                        "position",
                        "developer",
                        "engineer",
                        "$",
                        "salary",
                    ]
                ):
                    seen.add(t)
                    posts.append(
                        {
                            "source_type": "reddit",
                            "subreddit": sub,
                            "title": t,
                            "url": f"https://www.reddit.com/r/{sub}/",
                            "fetched_at": datetime.now(UTC).isoformat(),
                        }
                    )
            print(f"{len(posts)} posts")
            all_posts.extend(posts)
        else:
            print("failed")
        time.sleep(2)

    outfile = output_dir / "reddit.jsonl"
    with open(outfile, "w", encoding="utf-8") as f:
        for post in all_posts:
            f.write(json.dumps(post, ensure_ascii=False) + "\n")
    print(f"  Total: {len(all_posts)} posts -> {outfile}")
    return all_posts


def crawl_x(output_dir: Path) -> list[dict]:
    """Crawl X/Twitter hiring posts via ego-browser."""
    import urllib.parse

    all_tweets = []

    for i, query in enumerate(X_QUERIES):
        encoded = urllib.parse.quote(query)
        print(f"  [X query {i + 1}] {query[:60]}...", end=" ", flush=True)
        tmp_file = f"/tmp/careerops_crawl/x_query_{i}.txt"
        url = f"https://x.com/search?q={encoded}&f=live"

        if ego_save_page(url, tmp_file, wait_seconds=10):
            text = Path(tmp_file).read_text(errors="replace")
            # Extract tweet content from article elements
            articles = text.split('article "')
            tweets = []
            for article in articles[1:21]:
                title_end = article.find('" [ref=')
                title = article[:title_end] if title_end > -1 else article[:200]
                if any(
                    kw in title.lower()
                    for kw in ["hir", "join", "role", "position", "engineer", "developer"]
                ):
                    tweets.append(
                        {
                            "source_type": "x_twitter",
                            "title": title[:200],
                            "url": "https://x.com/search",
                            "query": query,
                            "fetched_at": datetime.now(UTC).isoformat(),
                        }
                    )
            print(f"{len(tweets)} tweets")
            all_tweets.extend(tweets)
        else:
            print("failed")
        time.sleep(3)

    outfile = output_dir / "x_twitter.jsonl"
    with open(outfile, "w", encoding="utf-8") as f:
        for tweet in all_tweets:
            f.write(json.dumps(tweet, ensure_ascii=False) + "\n")
    print(f"  Total: {len(all_tweets)} tweets -> {outfile}")
    return all_tweets


def crawl_ats(output_dir: Path) -> None:
    """Run the ATS API crawler (Phase 1)."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "crawl_jobs.py")],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    print(result.stdout)
    # Move JSONL files to output dir
    data_dir = PROJECT_ROOT / "data"
    for f in data_dir.glob("crawl_*.jsonl"):
        dest = output_dir / f.name
        if not dest.exists():
            f.rename(dest)


def main():
    output_dir = DEFAULT_OUTPUT
    if len(sys.argv) > 2 and sys.argv[1] == "--output-dir":
        output_dir = Path(sys.argv[2])
    output_dir.mkdir(parents=True, exist_ok=True)
    Path("/tmp/careerops_crawl").mkdir(exist_ok=True)

    print(f"CareerOps Full Crawler - {datetime.now(UTC).isoformat()}")
    print(f"Output: {output_dir}")
    print("=" * 60)

    print("\n[Phase 1] ATS APIs")
    crawl_ats(output_dir)

    print("\n[Phase 2] Company Career Pages (ego-browser)")
    crawl_career_pages(output_dir)

    print("\n[Phase 3] Reddit")
    crawl_reddit(output_dir)

    print("\n[Phase 4] X/Twitter")
    crawl_x(output_dir)

    # Summary
    print("\n" + "=" * 60)
    print("Crawl complete!")
    total = 0
    for f in sorted(output_dir.glob("*.jsonl")):
        with open(f) as fh:
            count = sum(1 for _ in fh)
        total += count
        print(f"  {f.name}: {count} records")
    print(f"  TOTAL: {total} records")


if __name__ == "__main__":
    main()
