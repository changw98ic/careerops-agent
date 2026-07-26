"""Domain models for the versioned career profile and search preferences.

End-to-end-career-application-loop, Section 2 (career-profile-and-resume spec).

A ``ProfileVersion`` is an immutable copy-on-write snapshot of the user's
search preferences. Exactly one version is active per candidate at a time
(enforced at the DB by a partial unique index on ``profile_versions``). Match
results and crawl filters bind to the profile version that produced them so
every decision is traceable to the exact preference inputs (design Decision 4,
spec: "Matching result records profile provenance").

Preference fields are structured types, not free text: each compound
preference is a frozen dataclass so the service layer can validate
contradictory inputs (excluded location == only required location, invalid
compensation range, ...) before a version becomes active (task 2.3 schema
guards + stage-3 service validation). The persistence layer serializes these
to the jsonb columns on ``profile_versions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

__all__ = [
    "Authorization",
    "CompensationPeriod",
    "CompensationPreference",
    "HardExclusions",
    "LocationKind",
    "LocationPreference",
    "ProfileVersion",
    "RemoteRules",
    "TargetRole",
]


class LocationKind(StrEnum):
    """Closure of location-preference kinds.

    ``REQUIRED`` locations must be satisfied (hard filter); ``PREFERRED`` are
    soft signals; ``EXCLUDED`` are hard exclusions. The service layer rejects
    contradictory sets (e.g. a location that is both the only ``REQUIRED`` and
    ``EXCLUDED``) before activating a version.
    """

    PREFERRED = "preferred"
    REQUIRED = "required"
    EXCLUDED = "excluded"


class CompensationPeriod(StrEnum):
    """Pay period for compensation bounds. Kept as a closed set for validation."""

    ANNUAL = "annual"
    MONTHLY = "monthly"
    HOURLY = "hourly"


@dataclass(frozen=True, slots=True)
class TargetRole:
    """A target role the candidate is searching for.

    ``title`` is free-form but required (e.g. "Backend Engineer"); ``seniority``
    and ``notes`` are optional filters the service layer may normalize.
    """

    title: str
    seniority: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class LocationPreference:
    """A location constraint. ``name`` is a normalized place label."""

    name: str
    kind: LocationKind = LocationKind.PREFERRED
    radius_km: int | None = None


@dataclass(frozen=True, slots=True)
class RemoteRules:
    """Remote-work rules. At least one allowance should be true for the
    profile to be usable; the service layer validates the combination."""

    remote_allowed: bool = False
    hybrid_allowed: bool = False
    onsite_required: bool = False
    timezone: str = ""


@dataclass(frozen=True, slots=True)
class CompensationPreference:
    """Compensation expectations. ``amount_min``/``amount_max`` are in
    ``currency`` units for the given ``period``. Schema guard enforces
    min <= max when both are numbers; the service layer validates currency
    and period coherence."""

    currency: str = ""
    amount_min: int | None = None
    amount_max: int | None = None
    period: CompensationPeriod | str = ""
    equity: bool = False


@dataclass(frozen=True, slots=True)
class Authorization:
    """Work-authorization constraints. ``work_authorization`` is a normalized
    status string (e.g. "citizen", "permanent_resident",
    "requires_sponsorship"); ``locale_restrictions`` lists jurisdictions the
    candidate can/must work in."""

    work_authorization: str = ""
    visa_sponsorship_required: bool = False
    locale_restrictions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HardExclusions:
    """Explicit hard exclusions. Any job matching a listed company, title, or
    keyword is filtered out regardless of other preferences."""

    companies: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    """An immutable, copy-on-write profile version.

    ``version`` is per-candidate monotonically increasing; ``is_active`` is
    true for at most one version per candidate (DB-enforced). ``rules_version``
    records the matching/filter ruleset this version was validated against, so
    a rules change can be detected and re-validated.
    """

    id: UUID
    candidate_id: UUID
    version: int
    is_active: bool = False
    target_roles: tuple[TargetRole, ...] = ()
    locations: tuple[LocationPreference, ...] = ()
    remote_rules: RemoteRules = field(default_factory=RemoteRules)
    compensation: CompensationPreference = field(default_factory=CompensationPreference)
    seniority: str = ""
    authorization: Authorization = field(default_factory=Authorization)
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    hard_exclusions: HardExclusions = field(default_factory=HardExclusions)
    rules_version: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
