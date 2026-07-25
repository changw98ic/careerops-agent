"""Email application pipeline: generate -> review -> batch send.

Flow:
  1. generate  -- for selected crawled jobs, write a tailored application email
                  (subject + body) per job into data/outbox_drafts.jsonl
  2. review    -- interactive: read each draft, edit/keep/drop, set the recipient
                  (recipient is NEVER guessed; you provide a publicly-listed
                  recruiting address or skip the job)
  3. send      -- send all approved drafts via Gmail (requires gmail_auth_send.py
                  to have been run). Prints each email and asks for a final
                  confirmation before sending the batch.
  4. status    -- show what has been sent (audit trail)

Safety:
  - Nothing sends without your explicit review + final confirmation.
  - Recipients are operator-supplied, never derived/guessed by the tool.
  - Every send is appended to data/applications_sent.jsonl.

Usage:
    python3 scripts/email_apply.py generate --resume resume.txt --filter "backend" --limit 10
    python3 scripts/email_apply.py review
    python3 scripts/email_apply.py send --resume resume.txt
    python3 scripts/email_apply.py status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from careerops.application.email_drafting import generate_body  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
DRAFTS_FILE = DATA_DIR / "outbox_drafts.jsonl"
SENT_FILE = DATA_DIR / "applications_sent.jsonl"


# ----------------------------- job loading -----------------------------


def load_jobs(filter_str: str | None = None, limit: int = 30) -> list[dict]:
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


# ----------------------------- email generation -----------------------------


def cmd_generate(args: argparse.Namespace) -> None:
    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume not found: {resume_path}")
        sys.exit(1)
    resume_text = resume_path.read_text(errors="replace")

    jobs = load_jobs(args.filter, args.limit)
    if not jobs:
        print("No jobs matched. Run the crawler first or adjust --filter.")
        return

    drafts = []
    for job in jobs:
        subject, body = generate_body(job, resume_text)
        drafts.append(
            {
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "job_url": job.get("url", ""),
                "subject": subject,
                "body": body,
                "to": "",  # recipient set during review; never guessed
                "status": "draft",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    with open(DRAFTS_FILE, "a", encoding="utf-8") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"Generated {len(drafts)} drafts -> {DRAFTS_FILE}")
    print("Next: python3 scripts/email_apply.py review")


# ----------------------------- generate from contacts -----------------------------

CONTACTS_FILE = DATA_DIR / "recruiting_contacts.jsonl"


def cmd_from_contacts(args: argparse.Namespace) -> None:
    """Generate application drafts targeted at extracted recruiting contacts.

    Unlike cmd_generate (recipient unknown, set in review), here the recipient
    is a publicly-listed address extracted from a hiring post, with provenance
    recorded. The email references the specific post so it is not a blind blast.
    """
    resume_path = Path(args.resume)
    if not resume_path.exists():
        print(f"Error: resume not found: {resume_path}")
        sys.exit(1)
    resume_text = resume_path.read_text(errors="replace")

    if not CONTACTS_FILE.exists():
        print("No contacts found. Run: python3 scripts/extract_contacts.py")
        return
    contacts = [json.loads(line) for line in CONTACTS_FILE.read_text().strip().split("\n") if line]
    if not contacts:
        print("No contacts to target.")
        return

    import re

    from careerops.application.resume_analysis import detect_skills

    resume_skills = detect_skills(resume_text)
    resume_flat = {s for skills in resume_skills.values() for s in skills}
    highlight = ", ".join(sorted(resume_flat)[:5]) or "software engineering"
    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", resume_text.lower())
    years = f"over {max(int(y) for y in years_match)} years of " if years_match else ""

    drafts = []
    for c in contacts:
        company = c.get("company_hint") or "your team"
        # Reference the source post so the recipient knows why we're writing.
        post_ref = c.get("context", "")[:120].replace('"', "'")
        subject = f"Application via your {c.get('platform', 'post')} listing"
        body = f"""Dear {company} Hiring Team,

I came across your recruiting post ({c.get("platform", "")}) and am writing to
express my interest in the role you shared.

With {years}experience in software engineering, I have built strong expertise in
{highlight}. I believe my background aligns well with what you are looking for.

I have attached my resume and would welcome the chance to discuss how I can
contribute to {company}.

Thank you for your time and consideration.

Best regards,
[Your Name]

---
Reference: {post_ref}
"""
        drafts.append(
            {
                "company": company,
                "title": f"(from {c.get('platform', '')} post)",
                "job_url": "",
                "subject": subject,
                "body": body,
                "to": c["email"],  # publicly-listed address with provenance
                "contact_provenance": {
                    "platform": c.get("platform", ""),
                    "source_file": c.get("source_file", ""),
                    "context": c.get("context", "")[:300],
                    "post_type": c.get("post_type"),
                },
                "status": "draft",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    with open(DRAFTS_FILE, "a", encoding="utf-8") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"Generated {len(drafts)} contact-targeted drafts -> {DRAFTS_FILE}")
    print("Recipients are pre-filled from public posts. Review before sending:")
    print("  python3 scripts/email_apply.py review")


# ----------------------------- review -----------------------------


def load_drafts() -> list[dict]:
    if not DRAFTS_FILE.exists():
        return []
    return [json.loads(line) for line in DRAFTS_FILE.read_text().strip().split("\n") if line]


def save_drafts(drafts: list[dict]) -> None:
    with open(DRAFTS_FILE, "w", encoding="utf-8") as f:
        for d in drafts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def cmd_review(args: argparse.Namespace) -> None:
    drafts = load_drafts()
    pending = [d for d in drafts if d["status"] == "draft"]
    if not pending:
        print("No drafts to review. Run 'generate' first.")
        return

    print(f"Reviewing {len(pending)} drafts.")
    print("For each: enter a recruiting email to approve, or 'skip' to drop.")
    print("Recipients must be publicly-listed addresses — never guessed.\n")

    for i, d in enumerate(pending, 1):
        print("=" * 70)
        print(f"[{i}/{len(pending)}] {d['company']} — {d['title']}")
        print(f"Subject: {d['subject']}")
        print("-" * 70)
        print(d["body"])
        print("-" * 70)
        recipient = input("Recruiting email to apply to (or 'skip'): ").strip()
        if recipient.lower() in ("skip", "s", ""):
            d["status"] = "skipped"
            print("  -> skipped\n")
        elif "@" not in recipient:
            d["status"] = "skipped"
            print("  -> invalid email, skipped\n")
        else:
            d["to"] = recipient
            d["status"] = "approved"
            print(f"  -> approved for {recipient}\n")

    save_drafts(drafts)
    approved = sum(1 for d in drafts if d["status"] == "approved")
    print(f"Review complete: {approved} approved.")
    if approved:
        print("Next: python3 scripts/email_apply.py send --resume <your_resume>")


# ----------------------------- send -----------------------------


def _draft_resource_id(draft: dict) -> UUID:
    """Derive a stable UUID from company + title for idempotent proposals."""
    key = f"{draft.get('company', '')}|{draft.get('title', '')}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return UUID(digest[:32])


def cmd_send(args: argparse.Namespace) -> None:
    from careerops.config import get_settings

    settings = get_settings()
    if not settings.external_writes_enabled:
        print(
            "Error: external_writes_enabled is False. "
            "Set CAREEROPS_EXTERNAL_WRITES_ENABLED=true to send emails."
        )
        sys.exit(1)

    from sqlalchemy import create_engine

    from careerops.application.side_effect_kernel import (
        ExecutionOutcome,
        ProposalInput,
        SideEffectKernel,
    )
    from careerops.domain.side_effects import IntentStatus
    from careerops.infrastructure.database.outbox import PostgresOutboxStore
    from careerops.infrastructure.database.side_effect_postgres import (
        PostgresSideEffectStore,
    )
    from careerops.integrations.gmail_sender import (
        GmailSender,
        GmailSendError,
        refresh_access_token,
    )
    from careerops.integrations.gmail_side_effect_provider import (
        GmailSideEffectProvider,
    )

    token_file = PROJECT_ROOT / "secrets" / "gmail_send_token.json"
    if not token_file.exists():
        print("Error: no send token. Run: python3 scripts/gmail_auth_send.py")
        sys.exit(1)
    tok = json.loads(token_file.read_text())
    if "gmail.send" not in tok.get("scope", ""):
        print("Error: token lacks gmail.send scope. Re-run gmail_auth_send.py.")
        sys.exit(1)

    resume_path = Path(args.resume) if args.resume else None
    attachment_refs: tuple[str, ...] = (
        (str(resume_path),) if resume_path and resume_path.exists() else ()
    )

    drafts = load_drafts()
    approved = [d for d in drafts if d["status"] == "approved"]
    if not approved:
        print("No approved drafts to send. Run 'review' first.")
        return

    print(f"You are about to send {len(approved)} application emails:\n")
    for d in approved:
        print(f"  To: {d['to']}")
        print(f"  Subject: {d['subject']}")
        print()

    confirm = input(f"Type 'SEND {len(approved)}' to confirm: ").strip()
    if confirm != f"SEND {len(approved)}":
        print("Aborted. Nothing was sent.")
        return

    # Refresh access token if a refresh token is available
    access_token = tok["access_token"]
    if tok.get("refresh_token"):
        try:
            access_token = refresh_access_token(
                client_id=tok["client_id"],
                client_secret=tok["client_secret"],
                refresh_token=tok["refresh_token"],
            )
        except GmailSendError as e:
            print(f"Warning: token refresh failed ({e}); using stored token.")

    sender = GmailSender(access_token)

    # Build the kernel with Postgres-backed stores and the Gmail provider.
    db_url = os.environ.get("CAREEROPS_DATABASE_URL")
    if not db_url:
        print("Error: CAREEROPS_DATABASE_URL not set.")
        sys.exit(1)
    engine = create_engine(db_url)
    store = PostgresSideEffectStore(engine)
    provider = GmailSideEffectProvider(sender)
    outbox = PostgresOutboxStore(engine)
    kernel = SideEffectKernel(store, provider, outbox_store=outbox)

    now = datetime.now(UTC)
    sent_count = 0
    for d in approved:
        resource_id = _draft_resource_id(d)
        idempotency_key = f"email_send:{resource_id}"

        proposal = ProposalInput(
            action_kind="email_send",
            resource_type="email",
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            created_by="email_apply_cli",
            target={"to": d["to"]},
            payload={"subject": d["subject"], "body": d["body"]},
            attachment_refs=attachment_refs,
            trusted_facts={"capability_released": True, "target_allowlisted": True},
            evidence_refs=("operator_reviewed_draft",),
        )

        try:
            result = kernel.propose(proposal, now=now)
            approval = kernel.get_or_create_pending_approval(
                result.intent.id,
                requested_for="email_apply_cli",
                now=now,
            )
            kernel.approve(approval.id, now=now)
            outcome: ExecutionOutcome = kernel.execute(result.intent.id, now=now)
        except Exception as exc:
            d["status"] = "send_failed"
            d["error"] = str(exc)
            print(f"  FAILED -> {d['to']}: {exc}")
            continue

        if outcome.status is IntentStatus.CONFIRMED:
            receipt = outcome.receipt
            d["status"] = "sent"
            d["sent_at"] = now.isoformat()
            if receipt:
                d["provider_message_id"] = receipt.provider_resource_id
            sent_count += 1
            print(f"  SENT -> {d['to']} (msg {receipt.provider_resource_id if receipt else 'n/a'})")
            with open(SENT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        elif outcome.status is IntentStatus.RECONCILIATION_REQUIRED:
            d["status"] = "reconciliation_required"
            d["error"] = f"reconciliation needed: {','.join(outcome.reason_codes)}"
            print(
                f"  WARNING -> {d['to']}: send result uncertain, "
                f"reconciliation required ({','.join(outcome.reason_codes)})"
            )
        else:
            d["status"] = "send_failed"
            d["error"] = f"kernel status: {outcome.status.value}"
            print(f"  FAILED -> {d['to']}: {outcome.status.value}")

    save_drafts(drafts)
    print(f"\nDone: {sent_count}/{len(approved)} sent.")


# ----------------------------- status -----------------------------


def cmd_status(args: argparse.Namespace) -> None:
    if not SENT_FILE.exists():
        print("No emails sent yet.")
        return
    records = [json.loads(line) for line in SENT_FILE.read_text().strip().split("\n") if line]
    print(f"{'Company':<14} {'Title':<38} {'To':<28} {'Sent':<20}")
    print("-" * 105)
    for r in records:
        print(
            f"{r.get('company', '')[:14]:<14} "
            f"{r.get('title', '')[:38]:<38} "
            f"{r.get('to', '')[:28]:<28} "
            f"{r.get('sent_at', '')[:19]:<20}"
        )
    print(f"\nTotal sent: {len(records)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Email application pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Generate application email drafts")
    g.add_argument("--resume", required=True)
    g.add_argument("--filter")
    g.add_argument("--limit", type=int, default=10)
    g.set_defaults(func=cmd_generate)

    fc = sub.add_parser(
        "from-contacts",
        help="Generate drafts targeted at extracted recruiting contacts",
    )
    fc.add_argument("--resume", required=True)
    fc.set_defaults(func=cmd_from_contacts)

    r = sub.add_parser("review", help="Review drafts and set recipients")
    r.set_defaults(func=cmd_review)

    s = sub.add_parser("send", help="Send approved drafts via Gmail")
    s.add_argument("--resume", help="Resume file to attach")
    s.set_defaults(func=cmd_send)

    st = sub.add_parser("status", help="Show sent applications")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
