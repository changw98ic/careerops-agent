"""Semantic deduplication for job descriptions via SimHash (Task D).

``dedup_jobs`` is a pure function: it takes a tuple of ``RawJobDTO`` and a
``DedupCriteria`` and returns the kept tuple alongside the duplicate jobs with
their Hamming distance to the canonical (first-seen) entry. It performs NO I/O,
NO model calls, and is NOT a match decision (ADR 0006).

Algorithm:

1. Strip HTML tags from the ``description`` field, normalize whitespace.
2. Compute a 64-bit SimHash over the cleaned text.
3. For each job, compare against the ``seen`` set of hashes; if the Hamming
   distance to any previously seen hash is <= ``threshold``, the job is
   classified as a duplicate candidate.
4. Duplicates are dropped (v1: no merge, just dropped). Human merge UI is v1.1.

The threshold is configurable (default 3 out of 64 bits).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from careerops.orchestration.state import RawJobDTO

__all__ = [
    "DedupCriteria",
    "DedupResult",
    "DuplicateJob",
    "dedup_jobs",
    "hamming_distance",
    "simhash",
    "strip_html",
]

# ---------------------------------------------------------------------------
# HTML stripping (stdlib only)
# ---------------------------------------------------------------------------

_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


class _HTMLStripper(HTMLParser):
    """Minimal HTML-to-text stripper using stdlib ``html.parser``."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def strip_html(text: str) -> str:
    """Strip HTML tags and normalize whitespace.

    Falls back to regex stripping if the HTML parser fails on malformed input.
    """
    if not text:
        return ""
    try:
        stripper = _HTMLStripper()
        stripper.feed(text)
        cleaned = stripper.get_text()
    except Exception:
        # Fallback: regex strip (malformed HTML edge case).
        cleaned = _HTML_TAG_RE.sub(" ", text)
    # Collapse whitespace runs to single spaces and strip.
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


# ---------------------------------------------------------------------------
# SimHash (64-bit)
# ---------------------------------------------------------------------------

_HASH_BITS: int = 64


def _token_hash(token: str) -> int:
    """Deterministic 64-bit hash for a single token (SHA-256 truncated)."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def simhash(text: str) -> int:
    """Compute a 64-bit SimHash over the given text.

    Tokenizes on whitespace; each token is hashed via SHA-256 (truncated to
    64 bits) for deterministic cross-process behavior. The algorithm builds a
    64-bit vector where each bit is set if more tokens hash to 1 than 0 at
    that bit position.
    """
    if not text:
        return 0

    vector = [0] * _HASH_BITS
    tokens = text.lower().split()

    for token in tokens:
        h = _token_hash(token)
        for i in range(_HASH_BITS):
            if h & (1 << i):
                vector[i] += 1
            else:
                vector[i] -= 1

    fingerprint = 0
    for i in range(_HASH_BITS):
        if vector[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Hamming distance between two 64-bit fingerprints."""
    diff = a ^ b
    return bin(diff).count("1")


# ---------------------------------------------------------------------------
# Dedup criteria and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DedupCriteria:
    """Configuration for semantic deduplication.

    ``threshold`` is the maximum Hamming distance (out of 64 bits) for two
    jobs to be considered duplicates. Default is 3.
    """

    threshold: int = 3


@dataclass(frozen=True, slots=True)
class DuplicateJob:
    """One duplicate job and the distance to its nearest canonical entry."""

    external_id: str
    title: str
    distance: int


@dataclass(frozen=True, slots=True)
class DedupResult:
    """Outcome of ``dedup_jobs``: the kept DTOs and the duplicate reasons.

    ``duplicates`` carries every dropped job with its Hamming distance to
    the nearest canonical entry; callers aggregate counts from that tuple.
    """

    kept: tuple[RawJobDTO, ...] = ()
    duplicates: tuple[DuplicateJob, ...] = ()

    def count(self) -> int:
        """Number of duplicates dropped."""
        return len(self.duplicates)


# ---------------------------------------------------------------------------
# Core dedup logic
# ---------------------------------------------------------------------------


def _duplicate(job: RawJobDTO, distance: int) -> DuplicateJob:
    return DuplicateJob(
        external_id=job.get("external_id", ""),
        title=job.get("title", ""),
        distance=distance,
    )


def _nearest_distance(fingerprint: int, seen: frozenset[int], threshold: int) -> int | None:
    """Return the minimum Hamming distance to any seen hash, or None if > threshold."""
    best: int | None = None
    for h in seen:
        dist = hamming_distance(fingerprint, h)
        if dist <= threshold and (best is None or dist < best):
            best = dist
    return best


def dedup_jobs(
    jobs: tuple[RawJobDTO, ...], *, criteria: DedupCriteria | None = None
) -> DedupResult:
    """Deduplicate ``jobs`` by SimHash similarity on the description text.

    Returns ``DedupResult(kept, duplicates)``. Pure and deterministic:
    identical input + criteria produce identical output. The first occurrence
    of a near-duplicate cluster is always kept; subsequent occurrences within
    the Hamming distance threshold are classified as duplicates.
    """
    crit = criteria or DedupCriteria()
    threshold = crit.threshold

    kept: list[RawJobDTO] = []
    duplicates: list[DuplicateJob] = []
    seen_hashes: frozenset[int] = frozenset()

    for job in jobs:
        description = job.get("description", "")
        cleaned = strip_html(description)
        fingerprint = simhash(cleaned)

        distance = _nearest_distance(fingerprint, seen_hashes, threshold)
        if distance is not None:
            duplicates.append(_duplicate(job, distance))
        else:
            kept.append(job)
            seen_hashes = seen_hashes | {fingerprint}

    return DedupResult(
        kept=tuple(kept),
        duplicates=tuple(duplicates),
    )
