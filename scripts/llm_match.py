"""LLM job matching CLI: rank crawled jobs by semantic fit to your resume.

Usage:
    python3 scripts/llm_match.py --resume resume.txt --filter backend --limit 5
    python3 scripts/llm_match.py --resume resume.txt --provider zhipu --limit 8

Uses a qualified model (xiaomi/zhipu) to semantically match your skill profile
against crawled jobs and rank them. Output is advisory (review_only).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

DATA_DIR = PROJECT_ROOT / "data"


def load_jobs(filter_str: str | None, limit: int) -> list[dict]:
    crawl_dirs = sorted(DATA_DIR.glob("crawl_*"), key=lambda p: p.name, reverse=True)
    jobs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for crawl_dir in crawl_dirs:
        for jsonl in sorted(crawl_dir.glob("*.jsonl")):
            if jsonl.name in ("career_pages.jsonl", "reddit.jsonl", "x_twitter.jsonl"):
                continue
            for line in jsonl.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not job.get("title"):
                    continue
                key = (job.get("company", ""), job.get("title", ""))
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(job)
        if jobs:
            break
    if filter_str:
        f = filter_str.lower()
        jobs = [j for j in jobs if f in json.dumps(j).lower()]
    return jobs[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-based job matching")
    parser.add_argument("--resume", required=True)
    parser.add_argument("--filter", help="Filter jobs by keyword")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--provider", default="xiaomi", help="xiaomi | zhipu | disabled")
    args = parser.parse_args()

    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume not found: {resume_path}")
        sys.exit(1)

    from careerops.application.llm_matching import (
        LLMJobMatcher,
        build_skill_profile,
    )
    from careerops.model_gateway import create_model_client

    client = create_model_client(args.provider)
    if not client.is_enabled:
        print(f"Error: provider '{args.provider}' is not enabled.")
        sys.exit(1)

    resume_text = resume_path.read_text(errors="replace")
    profile = build_skill_profile(resume_text)
    print(f"Model: {client.model_id}")
    print(
        f"Skill profile: {len(profile.skills)} skills, level={profile.level}, years={profile.years}"
    )
    print(f"Skills: {', '.join(profile.skills[:12])}")
    print()

    jobs = load_jobs(args.filter, args.limit)
    if not jobs:
        print("No jobs matched. Run the crawler first or adjust --filter.")
        return

    print(f"Matching {len(jobs)} jobs (this calls the LLM per job)...\n")
    matcher = LLMJobMatcher(client)
    results = matcher.match_jobs(jobs, profile)

    print("=" * 78)
    print(f"{'Score':>5}  {'Tier':<9} {'Rec':<8} {'Company':<12} {'Title':<32}")
    print("=" * 78)
    for r in results:
        if r.error:
            print(f"  ERR  {r.company[:12]:<12} {r.title[:32]:<32}  [{r.error[:30]}]")
            continue
        print(
            f"{r.match_score:>5}  {r.tier:<9} {r.recommendation:<8} "
            f"{r.company[:12]:<12} {r.title[:32]:<32}"
        )
    print("=" * 78)

    # Detail for top recommendations
    print("\nDetailed analysis (top matches):")
    for r in [x for x in results if not x.error][:3]:
        print(f"\n  {r.company} — {r.title}")
        print(
            f"  Score: {r.match_score}/100  |  Tier: {r.tier}  |  "
            f"Seniority: {r.seniority_fit}  |  Remote: {r.remote_compatible}  |  "
            f"Confidence: {r.confidence:.2f}"
        )
        if r.matched_requirements:
            print(f"  Matches: {', '.join(r.matched_requirements[:6])}")
        if r.gaps:
            print(f"  Gaps: {', '.join(r.gaps[:6])}")
        if r.transferable_skills:
            print(f"  Transferable: {', '.join(r.transferable_skills[:6])}")
        print(f"  Why: {r.reasoning}")
        print(f"  Recommendation: {r.recommendation.upper()}")

    # Save results
    out = DATA_DIR / "llm_matches.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(
                json.dumps(
                    {
                        "company": r.company,
                        "title": r.title,
                        "match_score": r.match_score,
                        "tier": r.tier,
                        "recommendation": r.recommendation,
                        "seniority_fit": r.seniority_fit,
                        "remote_compatible": r.remote_compatible,
                        "reasoning": r.reasoning,
                        "confidence": r.confidence,
                        "model_id": r.model_id,
                        "is_review_only": r.is_review_only,
                        "error": r.error,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"\nResults saved: {out}")
    print("Note: matches are advisory (review_only). The model does not decide for you.")


if __name__ == "__main__":
    main()
