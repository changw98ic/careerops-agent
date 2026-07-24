"""Pure job filter (plan v0.4 §2.2, §2.6 Stage 2).

``filter_jobs`` is an inline pure function: it takes a tuple of ``RawJobDTO``
plus a ``FilterCriteria`` and returns the kept tuple alongside the rejected
jobs with human-readable reasons. It performs NO I/O, NO model calls, and is
NOT a match decision (ADR 0006) — it only drops jobs that deterministically
fail remote / direction / region / salary / description-coverage gates so the
downstream (advisory, v1-disabled) match node has a cleaner input set.

Selection rules (all case-insensitive substring matches against the union of
``title`` / ``location`` / ``description`` / ``raw_data`` text):

- ``description`` coverage is ALWAYS required: a job with no JD body cannot be
  evaluated and is rejected with ``"empty_description"`` (v1 invariant carried
  over from Stage 1).
- ``remote_only``: when True, the job must advertise a remote keyword.
- ``directions``: when non-empty, the job must mention at least one requested
  direction (e.g. ``"backend"`` / ``"frontend"``).
- ``regions``: when non-empty, the job's location/text must mention one.
- ``min_salary``: when set, the job's parsed salary floor must meet it; jobs
  with no salary data are kept (absence is not a negative signal — only an
  explicit sub-floor salary rejects).

Rejected jobs never reach the match node; the reasons are returned to the node
wrapper which records them via the state ``errors`` reducer.
"""

from __future__ import annotations

from dataclasses import dataclass

from careerops.orchestration.state import RawJobDTO

__all__ = [
    "FilterCriteria",
    "FilterResult",
    "RejectedJob",
    "filter_jobs",
]


_REMOTE_KEYWORDS: tuple[str, ...] = (
    "remote",
    "work from home",
    "wfh",
    "distributed",
    "anywhere",
    "home-based",
)

# Common salary floor keys across ATS payloads (Greenhouse/Lever/Ashby/JsonLd).
_SALARY_FLOOR_KEYS: tuple[str, ...] = (
    "salary_min",
    "min_salary",
    "salaryMin",
    "salaryFloor",
    "start_salary",
)


def _job_text(job: RawJobDTO) -> str:
    """Flatten the searchable text of a job to one lowercase string."""
    parts: list[str] = [
        job.get("title", ""),
        job.get("location", ""),
        job.get("description", ""),
    ]
    raw = job.get("raw_data")
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (int, float)):
                parts.append(str(value))
    return " ".join(p for p in parts if p).lower()


def _extract_salary_floor(job: RawJobDTO) -> int | None:
    """Best-effort parse of the salary floor from ``raw_data``.

    Returns the largest numeric floor found across known keys, or None when no
    salary data is present (caller treats None as "no signal", not a rejection).
    """
    raw = job.get("raw_data")
    if not isinstance(raw, dict):
        return None
    floors: list[int] = []
    for key in _SALARY_FLOOR_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            floors.append(int(value))
        elif isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                floors.append(int(digits))
    return max(floors) if floors else None


@dataclass(frozen=True, slots=True)
class FilterCriteria:
    """Selection criteria for the deterministic job filter.

    Empty / None fields mean "no constraint on this dimension". With every
    field at its default the filter still drops jobs with an empty description
    (the v1 coverage invariant).
    """

    remote_only: bool = False
    directions: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    min_salary: int | None = None
    salary_currency: str = ""

    def normalized_directions(self) -> tuple[str, ...]:
        return tuple(sorted({d.lower().strip() for d in self.directions if d.strip()}))

    def normalized_regions(self) -> tuple[str, ...]:
        return tuple(sorted({r.lower().strip() for r in self.regions if r.strip()}))


@dataclass(frozen=True, slots=True)
class RejectedJob:
    """One rejected job and the deterministic reason it was dropped."""

    external_id: str
    title: str
    reason: str


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Outcome of ``filter_jobs``: the kept DTOs and the rejected reasons.

    ``rejected`` carries every dropped job with its reason; callers aggregate
    counts from that tuple (kept fully immutable — no mutable containers on a
    frozen dataclass).
    """

    kept: tuple[RawJobDTO, ...] = ()
    rejected: tuple[RejectedJob, ...] = ()

    def count_by_reason(self) -> dict[str, int]:
        """Aggregate rejected reasons into a stable count map (derived, not stored)."""
        counts: dict[str, int] = {}
        for rejected in self.rejected:
            counts[rejected.reason] = counts.get(rejected.reason, 0) + 1
        return counts


def _reject(job: RawJobDTO, reason: str) -> RejectedJob:
    return RejectedJob(
        external_id=job.get("external_id", ""),
        title=job.get("title", ""),
        reason=reason,
    )


def filter_jobs(
    jobs: tuple[RawJobDTO, ...], *, criteria: FilterCriteria | None = None
) -> FilterResult:
    """Filter ``jobs`` by deterministic coverage / remote / direction / region / salary.

    Returns ``FilterResult(kept, rejected, rejected_by_reason)``. Pure and
    deterministic: identical input + criteria produce identical output, which
    ``review_gate`` resume relies on for idempotent re-runs.
    """
    crit = criteria or FilterCriteria()
    directions = crit.normalized_directions()
    regions = crit.normalized_regions()
    min_salary = crit.min_salary

    kept: list[RawJobDTO] = []
    rejected: list[RejectedJob] = []

    for job in jobs:
        description = (job.get("description") or "").strip()
        if not description:
            rejected.append(_reject(job, "empty_description"))
            continue

        text = _job_text(job)

        if crit.remote_only and not any(kw in text for kw in _REMOTE_KEYWORDS):
            rejected.append(_reject(job, "not_remote"))
            continue

        if directions and not any(d in text for d in directions):
            rejected.append(_reject(job, "direction_mismatch"))
            continue

        if regions and not any(r in text for r in regions):
            rejected.append(_reject(job, "region_mismatch"))
            continue

        if min_salary is not None:
            floor = _extract_salary_floor(job)
            # Only reject on an EXPLICIT sub-floor salary. Missing salary data
            # is not grounds for rejection (absence != negative signal).
            if floor is not None and floor < min_salary:
                rejected.append(_reject(job, "salary_below_minimum"))
                continue

        kept.append(job)

    return FilterResult(
        kept=tuple(kept),
        rejected=tuple(rejected),
    )
