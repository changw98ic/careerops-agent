"""Recruiting contact extraction from crawled social content.

Pure functions lifted out of the legacy ``scripts/extract_contacts.py`` CLI so
that application services and LangGraph nodes can import them without depending
on the uninstalled ``scripts/`` package.

Safety principle (Spec M3 contacts): only emails EXPLICITLY present in a hiring
post are extracted. This module never guesses, derives, or fabricates addresses.
Each result carries full provenance (platform, source file, context window, post
type). Function signatures and behavior are preserved verbatim from the original
script; only the location changed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Domains/patterns that are noise, not recruiting contacts
NOISE_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "foo.com",
    "bar.com",
    "domain.com",
    "email.com",
    "company.com",
    "yourcompany.com",
    "gmail.com.png",
    "outlook.com.png",
}
NOISE_LOCAL = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "test",
    "admin",
    # Not for job applications: accessibility, feedback, support mailboxes
    "accommodations",
    "accommodation",
    "candidatefeedback",
    "candidatefeedback-applications",
    "feedback",
    "support",
    "help",
    "info",
    "careersupport",
    "applicantfeedback",
    "privacy",
    "legal",
    "press",
    "media",
    "sales",
    "marketing",
    "billing",
    "abuse",
    "spam",
    "webmaster",
    "office",
    "contact",
    "hello",
    "enquiries",
}

# File-extension false positives (image@2x, name.png@, etc.)
FALSE_POSITIVE_RE = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|css|js|ico)(@|$)", re.IGNORECASE)

RECRUITING_HINTS = [
    "hir",
    "hire",
    "hiring",
    "apply",
    "application",
    "resume",
    "cv",
    "recruit",
    "talent",
    "career",
    "job",
    "position",
    "role",
    "join",
    "send",
    "email",
    "contact",
    "reach",
    "dm",
    "remote",
    "salary",
    "engineer",
    "developer",
    "designer",
    "manager",
    "intern",
]

# Reddit r/forhire flair tags. The nearest preceding tag determines whether a
# post is an employer hiring ([Hiring]) or a job seeker offering services
# ([For Hire]). Only [Hiring] posts are valid sources of recruiting contacts.
FLAIR_TAG_RE = re.compile(r"\[\s*(for hire|hiring|forhire)\s*\]", re.IGNORECASE)
# Max distance to associate an email with a preceding flair tag.
FLAIR_MAX_DISTANCE = 12000


def classify_post_type(text: str, pos: int) -> str | None:
    """Return the flair type of the post containing position ``pos``.

    Finds the nearest preceding [Hiring]/[For Hire] tag within FLAIR_MAX_DISTANCE
    and returns "hiring", "for_hire", or None if no tag is nearby.
    """
    nearest: tuple[int, str] | None = None
    for m in FLAIR_TAG_RE.finditer(text):
        if m.start() > pos:
            break
        if pos - m.start() <= FLAIR_MAX_DISTANCE:
            kind = "for_hire" if "for" in m.group(1).lower() else "hiring"
            nearest = (m.start(), kind)
    return nearest[1] if nearest else None


def is_noise(email: str, context: str) -> bool:
    """Filter out emails that are clearly not recruiting contacts."""
    local, _, domain = email.partition("@")
    domain_l = domain.lower()
    local_l = local.lower()

    if domain_l in NOISE_DOMAINS:
        return True
    if local_l in NOISE_LOCAL:
        return True
    if FALSE_POSITIVE_RE.search(email):
        return True
    # Reject if it looks like a versioned asset (name@2x)
    return bool(re.search(r"@\d+x$", local_l))


def has_recruiting_context(context: str) -> bool:
    """Check the surrounding text looks like a recruiting post."""
    ctx = context.lower()
    return any(hint in ctx for hint in RECRUITING_HINTS)


def extract_context(text: str, start: int, end: int, window: int = 160) -> str:
    """Grab a context window around the email match."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    snippet = text[ctx_start:ctx_end]
    # Collapse the snapshot's nested-quote noise into readable text
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return snippet


def detect_platform(filename: str) -> str:
    name = filename.lower()
    if name.startswith("x_query") or name.startswith("x_deep"):
        return "x_twitter"
    if name.startswith("reddit_"):
        sub = name.replace("reddit_", "").replace(".txt", "")
        return f"reddit:r/{sub}"
    return "unknown"


def infer_company_hint(context: str) -> str:
    """Try to pull a company name hint from context (best-effort, for display only)."""
    # "Hiring Company: <Name>" (common on X job bots)
    m = re.search(r"Hiring Company:\s*([A-Z][A-Za-z0-9&.\- ]{2,30})", context)
    if m:
        return m.group(1).strip().rstrip(".")
    # "<Company> is hiring"
    m = re.search(r"([A-Z][A-Za-z0-9&.\-]{2,30})\s+is\s+hiring", context)
    if m:
        return m.group(1).strip()
    # "Hiring: <Role> at <Company>" or "at <Company>"
    m = re.search(r"\bat\s+([A-Z][A-Za-z0-9&.\-]{2,30})", context)
    if m:
        name = m.group(1).strip().rstrip(".")
        # Filter out generic non-company words
        if name.lower() not in {"the", "a", "an", "our", "their", "least", "most"}:
            return name
    return ""


def extract_from_file(path: Path) -> list[dict[str, Any]]:
    """Extract recruiting emails from a single snapshot file."""
    text = path.read_text(errors="replace")
    platform = detect_platform(path.name)
    is_forhire = "forhire" in path.name.lower()
    now = datetime.now(UTC).isoformat()
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for match in EMAIL_RE.finditer(text):
        email = match.group(0).strip().rstrip(".,;:)")
        if email in seen:
            continue
        context = extract_context(text, match.start(), match.end())
        if is_noise(email, context):
            continue
        if not has_recruiting_context(context):
            continue

        # For r/forhire, only keep emails from [Hiring] posts (employers),
        # never from [For Hire] posts (job seekers offering services).
        post_type = classify_post_type(text, match.start()) if is_forhire else None
        if is_forhire and post_type == "for_hire":
            continue

        seen.add(email)
        results.append(
            {
                "email": email,
                "platform": platform,
                "company_hint": infer_company_hint(context),
                "post_type": post_type,
                "context": context[:300],
                "source_file": path.name,
                "publicly_listed": True,
                "extracted_at": now,
            }
        )
    return results
