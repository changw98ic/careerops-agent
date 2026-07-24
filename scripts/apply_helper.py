"""One-click apply helper: prepare and track job applications.

Usage:
    python3 scripts/apply_helper.py --list                     # list crawled jobs
    python3 scripts/apply_helper.py --list --filter "backend"  # filter jobs
    python3 scripts/apply_helper.py --apply 12 --resume resume.txt   # prepare application #12
    python3 scripts/apply_helper.py --status                   # show application tracking

What this does:
  1. Opens the job's application page in ego-browser (for you to submit manually)
  2. Generates a tailored cover letter draft from your resume + job requirements
  3. Records the application in data/applications.jsonl for follow-up tracking

What this does NOT do (by design, per safety policy):
  - It never submits applications automatically. You click submit yourself.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
APP_TRACKING = DATA_DIR / "applications.jsonl"


def load_all_jobs() -> list[dict]:
    """Load all crawled jobs from the latest crawl directory."""
    crawl_dirs = sorted(DATA_DIR.glob("crawl_*"), key=lambda p: p.name, reverse=True)
    jobs = []
    seen_ids = set()
    for crawl_dir in crawl_dirs:
        for jsonl in sorted(crawl_dir.glob("*.jsonl")):
            if jsonl.name in (
                "career_pages.jsonl",
                "reddit.jsonl",
                "x_twitter.jsonl",
                "applications.jsonl",
            ):
                continue
            for line in jsonl.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    job = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Dedup by (company, title)
                key = (job.get("company", ""), job.get("title", ""))
                if key in seen_ids or not job.get("title"):
                    continue
                seen_ids.add(key)
                job["_source_file"] = str(jsonl)
                jobs.append(job)
        if jobs:
            break  # Only use the latest crawl
    return jobs


def list_jobs(jobs: list[dict], filter_str: str | None = None, limit: int = 30) -> None:
    """Print a numbered list of jobs."""
    if filter_str:
        f = filter_str.lower()
        jobs = [j for j in jobs if f in json.dumps(j).lower()]

    print(f"{'#':>4}  {'Company':<14} {'Title':<55} {'Location':<20}")
    print("-" * 100)
    for i, job in enumerate(jobs[:limit]):
        company = job.get("company", "")[:14]
        title = job.get("title", "")[:55]
        location = job.get("location", "")[:20]
        print(f"{i:>4}  {company:<14} {title:<55} {location:<20}")
    print(f"\n  Showing {min(limit, len(jobs))} of {len(jobs)} jobs")
    if filter_str:
        print(f"  Filter: '{filter_str}'")


def generate_cover_letter(resume_text: str, job: dict) -> str:
    """Generate a tailored cover letter draft from resume + job."""
    from resume_review import detect_skills

    title = job.get("title", "the position")
    company = job.get("company", "your company")

    resume_skills = detect_skills(resume_text)
    job_skills = detect_skills(json.dumps(job.get("raw_data", {})) + " " + title)

    resume_flat = {s for skills in resume_skills.values() for s in skills}
    job_flat = {s for skills in job_skills.values() for s in skills}
    matched = sorted(resume_flat & job_flat)
    top_skills = ", ".join(matched[:5]) if matched else ", ".join(sorted(resume_flat)[:5])

    years = ""
    import re

    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", resume_text.lower())
    if years_match:
        years = f"over {max(int(y) for y in years_match)} years of"

    letter = f"""Dear Hiring Manager,

I am writing to express my interest in the {title} position at {company}.

With {years + " " if years else ""}professional experience in software engineering, I have
develop strong expertise in {top_skills}. My background aligns well with the
requirements of this role, and I am confident I can contribute meaningfully to
your team from day one.

In my previous roles, I have consistently delivered high-impact work, collaborated
effectively across functions, and taken ownership of complex problems end-to-end.
I am particularly drawn to {company}'s mission and the opportunity to grow with
an ambitious team.

I would welcome the chance to discuss how my experience and skills can benefit
{company}. Thank you for your time and consideration.

Sincerely,
[Your Name]
"""
    return letter


def open_in_browser(url: str) -> None:
    """Open the application URL in ego-browser."""
    if not url:
        print("  No application URL available for this job.")
        return
    print(f"  Opening application page: {url[:80]}")
    try:
        subprocess.run(
            [
                "ego-browser",
                "nodejs",
                "-e",
                f"const task = await useOrCreateTaskSpace('apply'); "
                f"await openOrReuseTab('{url}', {{ wait: true, timeout: 30 }}); "
                f"cliLog('opened')",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        print("  -> Page opened in ego-browser. Complete the application manually.")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print(f"  -> Could not open browser. Visit manually: {url}")


def record_application(job: dict, cover_letter_path: Path) -> None:
    """Record the application in the tracking file."""
    record = {
        "company": job.get("company", ""),
        "title": job.get("title", ""),
        "url": job.get("url", ""),
        "source_type": job.get("source_type", ""),
        "status": "prepared",  # prepared -> submitted -> interview -> offer/rejected
        "cover_letter": str(cover_letter_path),
        "prepared_at": datetime.now(UTC).isoformat(),
        "submitted_at": None,
        "follow_up_due": None,
    }
    with open(APP_TRACKING, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"  Application recorded: {APP_TRACKING}")


def show_status() -> None:
    """Show application tracking status."""
    if not APP_TRACKING.exists():
        print("  No applications tracked yet.")
        return
    records = [json.loads(line) for line in APP_TRACKING.read_text().strip().split("\n") if line]
    print(f"{'Company':<14} {'Title':<40} {'Status':<12} {'Prepared':<22}")
    print("-" * 95)
    for r in records:
        print(
            f"{r['company'][:14]:<14} {r['title'][:40]:<40} "
            f"{r['status']:<12} {r['prepared_at'][:19]:<22}"
        )
    print(f"\n  Total: {len(records)} applications")


def main():
    parser = argparse.ArgumentParser(description="One-click apply helper")
    parser.add_argument("--list", action="store_true", help="List crawled jobs")
    parser.add_argument("--filter", help="Filter jobs by keyword")
    parser.add_argument("--limit", type=int, default=30, help="Max jobs to list")
    parser.add_argument("--apply", type=int, help="Job index to prepare application for")
    parser.add_argument("--resume", help="Path to resume file (for cover letter generation)")
    parser.add_argument("--status", action="store_true", help="Show application tracking")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    jobs = load_all_jobs()
    if not jobs:
        print("No crawled jobs found. Run scripts/crawl_daily.sh first.")
        return

    if args.list:
        list_jobs(jobs, args.filter, args.limit)
        return

    if args.apply is not None:
        if args.apply >= len(jobs):
            print(f"Error: index {args.apply} out of range ({len(jobs)} jobs)")
            return
        job = jobs[args.apply]
        print("\n  Preparing application for:")
        print(f"    {job.get('company', '')} - {job.get('title', '')}")
        print(f"    {job.get('location', '')}")

        # Generate cover letter
        if args.resume:
            resume_path = Path(args.resume)
            if resume_path.exists():
                resume_text = resume_path.read_text(errors="replace")
                letter = generate_cover_letter(resume_text, job)
                company_slug = job.get("company", "job").lower().replace(" ", "_")
                timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                letter_path = DATA_DIR / f"cover_letter_{company_slug}_{timestamp}.txt"
                letter_path.write_text(letter)
                print(f"\n  Cover letter draft: {letter_path}")
                print("  " + "-" * 50)
                for line in letter.split("\n")[:8]:
                    print(f"  {line}")
                print("  ... (edit before sending)")
            else:
                print(f"  Resume not found: {resume_path}, skipping cover letter")

        # Open application page
        url = job.get("url", "")
        if url:
            open_in_browser(url)

        # Record
        record_application(
            job, letter_path if args.resume and Path(args.resume).exists() else Path("")
        )

        print("\n  REMINDER: Submit the application yourself in the browser.")
        print("  This tool never submits applications automatically.")
        return

    # Default: show list
    list_jobs(jobs, args.filter, args.limit)


if __name__ == "__main__":
    main()
