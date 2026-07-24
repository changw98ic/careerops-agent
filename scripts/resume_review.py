"""Resume review: analyze a resume against job requirements.

Thin CLI shell. Pure analysis logic (skill detection, level estimation, job
parsing, match report) lives in ``careerops.application.resume_analysis``; this
module only handles file I/O, argument parsing, and report rendering. The
analysis functions are re-exported so legacy ``from resume_review import ...``
callers (e.g. sibling scripts) keep working.

Usage:
    python3 scripts/resume_review.py --resume path/to/resume.txt --job "Senior Backend Engineer"
    python3 scripts/resume_review.py --resume resume.txt \\
        --job-file data/crawl_*/crawl_greenhouse_stripe_*.jsonl --index 5

Produces a match report: matched skills, gaps, experience analysis, suggestions.
Works deterministically (no LLM required).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from careerops.application.resume_analysis import (  # noqa: E402
    detect_level,
    detect_skills,
    parse_job,
    review,
)

__all__ = ["detect_level", "detect_skills", "parse_job", "review"]


def extract_text(path: Path) -> str:
    """Extract text from a resume file (txt/md/pdf)."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        return path.read_text(errors="replace")
    if suffix == ".pdf":
        # Try pdftotext, fall back to raw extraction
        import subprocess

        try:
            result = subprocess.run(
                ["pdftotext", str(path), "-"], capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback: extract readable strings from PDF
        raw = path.read_bytes()
        return raw.decode("latin-1", errors="replace")
    return path.read_text(errors="replace")


def print_report(report: dict) -> None:
    job = report["job"]
    print("=" * 60)
    print(f"  Resume Review: {job['title']}")
    print(f"  Company: {job['company']}  |  Location: {job['location']}")
    print(f"  Level: {job['level']}  |  Remote: {'yes' if job['remote'] else 'no'}")
    if job["url"]:
        print(f"  URL: {job['url'][:80]}")
    print("=" * 60)

    score = report["match_score"]
    bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
    print(f"\n  Match Score: {bar} {int(score * 100)}%")

    print(f"\n  Matched skills ({len(report['matched_skills'])}):")
    for s in report["matched_skills"]:
        print(f"    + {s}")

    if report["gap_skills"]:
        print(f"\n  Gaps ({len(report['gap_skills'])}):")
        for s in report["gap_skills"]:
            print(f"    - {s}")

    print(f"\n  Level: {report['level_note']}")

    print("\n  Suggestions:")
    for i, s in enumerate(report["suggestions"], 1):
        print(f"    {i}. {s}")
    print()


def load_job_from_file(path: str, index: int) -> dict:
    """Load a job record from a crawled JSONL file by index."""
    lines = Path(path).read_text().strip().split("\n")
    if index >= len(lines):
        print(f"Error: index {index} out of range (file has {len(lines)} jobs)")
        sys.exit(1)
    return json.loads(lines[index])


def main():
    parser = argparse.ArgumentParser(description="Resume review against job requirements")
    parser.add_argument("--resume", required=True, help="Path to resume file (txt/md/pdf)")
    parser.add_argument("--job", help="Job title string to review against")
    parser.add_argument("--job-file", help="Crawled JSONL file to pick a job from")
    parser.add_argument("--index", type=int, default=0, help="Job index in the JSONL file")
    args = parser.parse_args()

    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume file not found: {resume_path}")
        sys.exit(1)

    resume_text = extract_text(resume_path)
    if len(resume_text.strip()) < 50:
        print("Error: could not extract meaningful text from resume")
        sys.exit(1)

    if args.job_file:
        job = load_job_from_file(args.job_file, args.index)
    elif args.job:
        job = {"title": args.job, "company": "", "location": "", "url": ""}
    else:
        print("Error: provide --job or --job-file")
        sys.exit(1)

    report = review(resume_text, job)
    print_report(report)

    # Also save as JSON
    out = resume_path.with_suffix(".review.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  Report saved: {out}")


if __name__ == "__main__":
    main()
