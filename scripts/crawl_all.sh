#!/bin/bash
# CareerOps Full Crawler: ATS + Company Career Pages + X + Reddit
# Usage: bash scripts/crawl_all.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN="${1:-}"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
OUTPUT_DIR="data/crawl_${TIMESTAMP}"
mkdir -p "$OUTPUT_DIR"

echo "CareerOps Full Crawler - $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Output: $OUTPUT_DIR"
echo "============================================================"

# --- Phase 1: ATS APIs ---
echo ""
echo "[Phase 1] ATS APIs (Greenhouse, Lever, Ashby)"
python3 scripts/crawl_jobs.py ${DRY_RUN:+--dry-run} 2>&1 | tee "$OUTPUT_DIR/ats_log.txt"
mv data/crawl_*.jsonl "$OUTPUT_DIR/" 2>/dev/null || true

# --- Phase 2: Company Career Pages (JSON-LD / Sitemap / Static HTML) ---
echo ""
echo "[Phase 2] Company Career Pages"
python3 - "$OUTPUT_DIR" << 'PYEOF'
import json, hashlib, re, sys, time, urllib.request, urllib.error
from datetime import datetime, UTC

output_dir = sys.argv[1]
JSON_LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)

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
    ("HashiCorp", "https://www.hashicorp.com/careers"),
    ("Elastic", "https://www.elastic.co/careers/jobs"),
    ("Grafana", "https://grafana.com/careers/jobs/"),
    ("Supabase", "https://supabase.com/careers"),
    ("Neon", "https://neon.tech/careers"),
    ("Railway", "https://railway.app/careers"),
    ("Fly.io", "https://fly.io/docs/about/careers/"),
    ("Tailscale", "https://tailscale.com/careers"),
    ("PlanetScale", "https://planetscale.com/careers"),
    ("Temporal", "https://temporal.io/careers"),
    ("Databricks", "https://www.databricks.com/company/careers"),
]

results = []
for company, url in CAREER_PAGES:
    print(f"  [{company}] Fetching {url[:60]}...", end=" ", flush=True)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        jobs_found = 0
        # Try JSON-LD
        for match in JSON_LD_RE.finditer(html):
            try:
                data = json.loads(match.group(1))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        jobs_found += 1
                        results.append({
                            "company": company,
                            "source_type": "career_page_jsonld",
                            "title": item.get("title", ""),
                            "url": item.get("url", url),
                            "location": str(item.get("jobLocation", {}).get("address", {}).get("addressLocality", "")) if isinstance(item.get("jobLocation"), dict) else "",
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "source_url": url,
                        })
            except (json.JSONDecodeError, ValueError):
                continue

        # Try job-title class patterns
        if jobs_found == 0:
            title_matches = re.findall(r'class=["\'][^"\']*job[-_]?title[^"\']*["\'][^>]*>(.*?)<', html, re.IGNORECASE | re.DOTALL)
            for t in title_matches[:50]:
                title = t.strip()
                if title and len(title) > 3:
                    jobs_found += 1
                    results.append({
                        "company": company,
                        "source_type": "career_page_html",
                        "title": title,
                        "url": url,
                        "location": "",
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "source_url": url,
                    })

        print(f"{jobs_found} jobs")
    except Exception as e:
        print(f"failed ({type(e).__name__})")
    time.sleep(1)

outfile = f"{output_dir}/career_pages.jsonl"
with open(outfile, "w") as f:
    for r in results:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"  Total career page jobs: {len(results)} -> {outfile}")
PYEOF

# --- Phase 3: Reddit (via ego-browser) ---
echo ""
echo "[Phase 3] Reddit Hiring Posts"
SUBREDDITS=("forhire" "remotejobs" "jobbit" "cscareerquestions")
for sub in "${SUBREDDITS[@]}"; do
    echo "  [r/$sub] Crawling..."
    ego-browser nodejs -e "
const task = await useOrCreateTaskSpace('reddit-${sub}');
await openOrReuseTab('https://www.reddit.com/r/${sub}/hot/', { wait: true, timeout: 30 });
await wait(5);
await scrollBy(0, 1500);
await wait(2);
const text = await snapshotText();
const lines = text.split('\n');
const posts = [];
for (let i = 0; i < lines.length; i++) {
  const line = lines[i];
  if (line.includes('text \"[') || (line.includes('text \"') && line.length > 40 && (line.toLowerCase().includes('hir') || line.toLowerCase().includes('hire') || line.toLowerCase().includes('remote')))) {
    const m = line.match(/text \"(.+)\"/);
    if (m && m[1].length > 15 && !m[1].includes('跳到') && !m[1].includes('搜索')) {
      posts.push(m[1].substring(0, 200));
    }
  }
}
const unique = [...new Set(posts)];
cliLog('r/${sub}: ' + unique.length + ' posts');
unique.slice(0, 20).forEach(p => cliLog('POST: ' + p));
" 2>&1 | tee -a "$OUTPUT_DIR/reddit_log.txt"
    sleep 2
done

# --- Phase 4: X/Twitter (via ego-browser) ---
echo ""
echo "[Phase 4] X/Twitter Hiring Posts"
X_QUERIES=(
    '(hiring OR "we are hiring" OR "join our team") (software OR engineer OR developer) remote'
    '(hiring OR "is hiring") (senior OR staff OR principal) (engineer OR developer OR SRE) remote'
    '"we are hiring" (backend OR frontend OR fullstack OR platform) engineer'
)
for i in "${!X_QUERIES[@]}"; do
    QUERY="${X_QUERIES[$i]}"
    ENCODED=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$QUERY'))")
    echo "  [X query $((i+1))] $QUERY"
    ego-browser nodejs -e "
const task = await useOrCreateTaskSpace('x-crawler');
await openOrReuseTab('https://x.com/search?q=${ENCODED}&f=live', { wait: true, timeout: 30 });
await wait(8);
await scrollBy(0, 2000);
await wait(3);
const text = await snapshotText();
const articles = text.split('article \"');
const posts = [];
for (let i = 1; i < articles.length && i <= 20; i++) {
  const a = articles[i];
  const titleEnd = a.indexOf('\" [ref=');
  const title = titleEnd > -1 ? a.substring(0, titleEnd) : a.substring(0, 200);
  if (title.toLowerCase().includes('hir') || title.toLowerCase().includes('join') || title.toLowerCase().includes('role')) {
    posts.push(title.substring(0, 200));
  }
}
cliLog('X results: ' + posts.length);
posts.forEach(p => cliLog('TWEET: ' + p));
" 2>&1 | tee -a "$OUTPUT_DIR/x_log.txt"
    sleep 3
done

# --- Summary ---
echo ""
echo "============================================================"
echo "Crawl complete. Output in: $OUTPUT_DIR"
ls -la "$OUTPUT_DIR"/*.jsonl 2>/dev/null | awk '{print "  " $NF " (" $5 " bytes)"}'
echo ""
echo "Logs:"
for f in "$OUTPUT_DIR"/*_log.txt; do
    [ -f "$f" ] && echo "  $f: $(grep -c 'POST:\|TWEET:' "$f" 2>/dev/null || echo 0) items"
done
