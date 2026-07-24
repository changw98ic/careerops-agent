"""Extract publicly-listed recruiting emails from crawled X/Reddit content.

Thin CLI shell. Pure extraction logic (regex filtering, recruiting-context
checks, r/forhire flair classification, platform detection) lives in
``careerops.application.contact_extraction``; this module only walks the crawl
directory, dedups, writes the JSONL output, and prints results.

Safety principle (per Spec M3 contacts):
- Only emails that are EXPLICITLY present in a hiring post are extracted.
  This tool never guesses, derives, or fabricates addresses.
- Each extracted email carries provenance: the platform, the source post
  text snippet, and a context window proving it appeared in a recruiting post.
- Obvious noise (example.com, noreply@, test@, image/file names) is filtered.

Usage:
    python3 scripts/extract_contacts.py                 # parse /tmp/careerops_crawl
    python3 scripts/extract_contacts.py --dir <dir>     # parse a specific dir
    python3 scripts/extract_contacts.py --show          # print extracted contacts

Output: data/recruiting_contacts.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from careerops.application.contact_extraction import extract_from_file  # noqa: E402

DEFAULT_CRAWL_DIR = Path("/tmp/careerops_crawl")
OUTPUT_FILE = PROJECT_ROOT / "data" / "recruiting_contacts.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract recruiting emails from crawled content")
    parser.add_argument("--dir", default=str(DEFAULT_CRAWL_DIR), help="Crawl snapshot directory")
    parser.add_argument("--show", action="store_true", help="Print extracted contacts")
    args = parser.parse_args()

    crawl_dir = Path(args.dir)
    if not crawl_dir.exists():
        print(f"Error: crawl dir not found: {crawl_dir}")
        print("Run scripts/crawl_daily.sh first to produce snapshots.")
        return

    all_contacts: list[dict] = []
    for txt in sorted(crawl_dir.glob("*.txt")):
        if txt.name.startswith("google") or txt.name in ("career_pages",):
            continue
        contacts = extract_from_file(txt)
        all_contacts.extend(contacts)

    # Dedup by email, keep first (with provenance)
    by_email: dict[str, dict] = {}
    for c in all_contacts:
        if c["email"] not in by_email:
            by_email[c["email"]] = c
    unique = list(by_email.values())

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for c in unique:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"Extracted {len(unique)} unique recruiting contacts -> {OUTPUT_FILE}")
    if args.show or len(unique) <= 20:
        print()
        for c in unique:
            company = f" ({c['company_hint']})" if c["company_hint"] else ""
            print(f"  {c['email']}{company}  [{c['platform']}]")
            print(f"    ...{c['context'][:120]}...")
            print()


if __name__ == "__main__":
    main()
