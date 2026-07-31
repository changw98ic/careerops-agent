"""Auto-discover a company's career page from its domain.

Tries common career-page URL patterns + subdomains, verifies each via a
lightweight HTTP GET (checks for job-like content). Returns the first match.

This is the infrastructure that lets the crawler scale: give it a list of
company domains (from the seed list or snowball discovery), and it finds the
career page for each — no manual URL entry needed.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import cast

_COMMON_PATHS: tuple[str, ...] = (
    "/careers",
    "/jobs",
    "/careers/",
    "/about/careers",
    "/about-us/careers",
    "/opportunities",
    "/join-us",
    "/work-with-us",
    "/en/careers",
    "/careers/search",
    "/jobs/search",
    "/career",
    "/recruitment",
    "/zhaopin",
)

_JOB_KEYWORDS = re.compile(
    r"careers?|jobs?|position|opening|vacanc|招聘|岗位|职位|工程师|opportunit",
    re.IGNORECASE,
)
_TIMEOUT_SECONDS = 10.0
_MAX_READ_BYTES = 50_000
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def find_career_page(domain: str) -> str | None:
    """Try common career-page patterns for ``domain``; return the first match.

    Returns ``None`` if no career page is found at any common path.
    """
    candidates: list[str] = []
    for path in _COMMON_PATHS:
        candidates.append(f"https://{domain}{path}")
    for sub in ("careers", "jobs"):
        candidates.append(f"https://{sub}.{domain}")

    for url in candidates:
        if _is_career_page(url):
            return url
    return None


def _is_career_page(url: str) -> bool:
    """Quick HTTP GET: status 200 + job-like content = likely a career page."""
    try:
        req = urllib.request.Request(  # nosec B310 -- https only, discovery
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # nosec B310
            status = cast(int, getattr(resp, "status", 0))
            if status != 200:
                return False
            body = resp.read(_MAX_READ_BYTES).decode("utf-8", errors="ignore")
            return bool(_JOB_KEYWORDS.search(body))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return False
