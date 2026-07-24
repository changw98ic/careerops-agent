#!/bin/bash
# Daily automated crawl - all 4 sources (ATS + career pages + Reddit + X)
# ego-browser must be called from bash directly (Python subprocess fails)
set -uo pipefail
cd /Volumes/ORICO/career
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

TS=$(date -u +%Y%m%d_%H%M%S)
OUT="data/crawl_${TS}"
TMP="/tmp/careerops_crawl"
mkdir -p "$OUT" "$TMP"

echo "=== CareerOps Daily Crawl: $(date -u) ==="
echo "Output: $OUT"

# ---------- Phase 1: ATS APIs (Python, no ego-browser needed) ----------
echo ""
echo "[Phase 1] ATS APIs"
python3 scripts/crawl_jobs.py 2>&1 | tail -3
mv data/crawl_*.jsonl "$OUT/" 2>/dev/null || true

# ---------- Phase 2: Company career pages via ego-browser ----------
echo ""
echo "[Phase 2] Company career pages"
declare -a PAGES=(
  "google|https://www.google.com/about/careers/applications/jobs/results/"
  "meta|https://www.metacareers.com/jobs/"
  "apple|https://jobs.apple.com/en-us/search"
  "microsoft|https://careers.microsoft.com/us/en/search-results"
  "amazon|https://www.amazon.jobs/en/search"
  "spotify|https://www.lifeatspotify.com/jobs"
  "github|https://github.com/about/careers"
  "gitlab|https://about.gitlab.com/jobs/"
  "cloudflare|https://www.cloudflare.com/careers/jobs/"
  "supabase|https://supabase.com/careers"
  "neon|https://neon.tech/careers"
  "temporal|https://temporal.io/careers"
  "databricks|https://www.databricks.com/company/careers"
  "stripe|https://stripe.com/jobs"
  "airbnb|https://careers.airbnb.com/positions/"
  "netflix|https://jobs.netflix.com/jobs"
  "shopify|https://www.shopify.com/careers"
  "figma|https://www.figma.com/careers/"
  "notion|https://www.notion.com/careers"
  "vercel|https://vercel.com/careers"
)

for entry in "${PAGES[@]}"; do
  name="${entry%%|*}"
  url="${entry#*|}"
  printf "  [%s] %s ... " "$name" "${url:0:50}"
  ego-browser nodejs -e "const task = await useOrCreateTaskSpace('career-crawl'); await openOrReuseTab('${url}', { wait: true, timeout: 30 }); await wait(8); await scrollBy(0, 1500); await wait(2); const text = await snapshotText(); const fs = await import('fs'); fs.writeFileSync('${TMP}/${name}.txt', text); cliLog('ok')" >/dev/null 2>&1
  if [ -s "${TMP}/${name}.txt" ]; then
    echo "ok"
  else
    echo "failed"
  fi
  sleep 2
done

# ---------- Phase 3: Reddit via ego-browser ----------
echo ""
echo "[Phase 3] Reddit"
for sub in forhire remotejobs jobbit cscareerquestions; do
  printf "  [r/%s] ... " "$sub"
  ego-browser nodejs -e "const task = await useOrCreateTaskSpace('reddit-${sub}'); await openOrReuseTab('https://www.reddit.com/r/${sub}/hot/', { wait: true, timeout: 30 }); await wait(6); await scrollBy(0, 1500); await wait(2); const text = await snapshotText(); const fs = await import('fs'); fs.writeFileSync('${TMP}/reddit_${sub}.txt', text); cliLog('ok')" >/dev/null 2>&1
  if [ -s "${TMP}/reddit_${sub}.txt" ]; then
    echo "ok"
  else
    echo "failed"
  fi
  sleep 2
done

# ---------- Phase 4: X/Twitter via ego-browser ----------
echo ""
echo "[Phase 4] X/Twitter"
declare -a XQ=(
  "(hiring OR \"we are hiring\" OR \"join our team\") (software OR engineer OR developer) remote"
  "(hiring OR \"is hiring\") (senior OR staff OR principal) (engineer OR developer OR SRE) remote"
  "\"we are hiring\" (backend OR frontend OR fullstack OR platform) engineer"
)
for i in 0 1 2; do
  q="${XQ[$i]}"
  enc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$q")
  printf "  [X query %d] ... " $((i+1))
  ego-browser nodejs -e "const task = await useOrCreateTaskSpace('x-crawler'); await openOrReuseTab('https://x.com/search?q=${enc}&f=live', { wait: true, timeout: 30 }); await wait(10); await scrollBy(0, 2000); await wait(3); const text = await snapshotText(); const fs = await import('fs'); fs.writeFileSync('${TMP}/x_query_${i}.txt', text); cliLog('ok')" >/dev/null 2>&1
  if [ -s "${TMP}/x_query_${i}.txt" ]; then
    echo "ok"
  else
    echo "failed"
  fi
  sleep 3
done

# ---------- Phase 5: Parse everything into JSONL ----------
echo ""
echo "[Phase 5] Parsing"
python3 - "$OUT" "$TMP" << 'PYEOF'
import json, re, sys
from datetime import datetime, UTC
from pathlib import Path

out = Path(sys.argv[1])
tmp = Path(sys.argv[2])

ROLE_KW = ["engineer","developer","manager","designer","analyst","scientist","architect","lead","director","specialist","consultant","intern","researcher","product","program","operations","marketing","sales","support","infrastructure","platform","security","data","machine learning","ai","ml","sre","devops","frontend","backend","fullstack","full-stack","mobile","ios","android","cloud","network","systems","software","technical","senior","staff","principal","junior","associate","vp","head of","chief","remote","hiring","join"]
NOISE = ["cookie","privacy","sign in","skip to","search","menu","button","next","prev","filter","showing","sort","load more","all rights","terms of","accessibility","equal opportunity","跳到","搜索"]

COMPANIES = {"google":("Google","https://www.google.com/about/careers/applications/jobs/results/"),"meta":("Meta","https://www.metacareers.com/jobs/"),"apple":("Apple","https://jobs.apple.com/en-us/search"),"microsoft":("Microsoft","https://careers.microsoft.com/us/en/search-results"),"amazon":("Amazon","https://www.amazon.jobs/en/search"),"spotify":("Spotify","https://www.lifeatspotify.com/jobs"),"github":("GitHub","https://github.com/about/careers"),"gitlab":("GitLab","https://about.gitlab.com/jobs/"),"cloudflare":("Cloudflare","https://www.cloudflare.com/careers/jobs/"),"supabase":("Supabase","https://supabase.com/careers"),"neon":("Neon","https://neon.tech/careers"),"temporal":("Temporal","https://temporal.io/careers"),"databricks":("Databricks","https://www.databricks.com/company/careers"),"stripe":("Stripe","https://stripe.com/jobs"),"airbnb":("Airbnb","https://careers.airbnb.com/positions/"),"netflix":("Netflix","https://jobs.netflix.com/jobs"),"shopify":("Shopify","https://www.shopify.com/careers"),"figma":("Figma","https://www.figma.com/careers/"),"notion":("Notion","https://www.notion.com/careers"),"vercel":("Vercel","https://vercel.com/careers")}

now = datetime.now(UTC).isoformat()

# Career pages
career = []
for f in sorted(tmp.glob("*.txt")):
    k = f.stem.lower()
    if k.startswith("reddit_") or k.startswith("x_query"):
        continue
    comp, url = COMPANIES.get(k, (k.title(), ""))
    text = f.read_text(errors="replace")
    seen = set()
    for t in re.findall(r'text "([^"]{15,150})"', text):
        t = t.strip()
        if t in seen or any(n in t.lower() for n in NOISE):
            continue
        if any(kw in t.lower() for kw in ROLE_KW):
            seen.add(t)
            career.append({"company":comp,"source_type":"career_page","title":t,"url":url,"location":"","fetched_at":now,"source_url":url})
(out/"career_pages.jsonl").write_text("".join(json.dumps(j,ensure_ascii=False)+"\n" for j in career))
print(f"  Career pages: {len(career)}")

# Reddit
reddit = []
for f in sorted(tmp.glob("reddit_*.txt")):
    sub = f.stem.replace("reddit_","")
    text = f.read_text(errors="replace")
    seen = set()
    for t in re.findall(r'text "([^"]{15,200})"', text):
        t = t.strip()
        if t in seen or any(n in t.lower() for n in NOISE):
            continue
        if any(kw in t.lower() for kw in ["hir","hire","remote","role","position","developer","engineer","$","salary"]):
            seen.add(t)
            reddit.append({"source_type":"reddit","subreddit":sub,"title":t,"url":f"https://www.reddit.com/r/{sub}/","fetched_at":now})
(out/"reddit.jsonl").write_text("".join(json.dumps(p,ensure_ascii=False)+"\n" for p in reddit))
print(f"  Reddit: {len(reddit)}")

# X
xt = []
for f in sorted(tmp.glob("x_query_*.txt")):
    text = f.read_text(errors="replace")
    for art in text.split('article "')[1:25]:
        end = art.find('" [ref=')
        title = art[:end] if end > -1 else art[:200]
        if any(kw in title.lower() for kw in ["hir","join","role","position","engineer","developer","remote"]):
            xt.append({"source_type":"x_twitter","title":title[:200],"url":"https://x.com/search","fetched_at":now})
(out/"x_twitter.jsonl").write_text("".join(json.dumps(t,ensure_ascii=False)+"\n" for t in xt))
print(f"  X/Twitter: {len(xt)}")

ats = sum(1 for f in out.glob("crawl_*.jsonl") for _ in open(f))
print(f"  ATS: {ats}")
print(f"  TOTAL: {ats + len(career) + len(reddit) + len(xt)}")
PYEOF

echo ""
echo "=== Done: $(date -u) ==="
echo "Output: $OUT"
