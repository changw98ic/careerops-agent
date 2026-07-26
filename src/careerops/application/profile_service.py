"""Career-profile application service (Section 3, tasks 3.1).

Wraps the Section-2 :class:`PostgresProfileRepository` (and its in-memory twin)
with the copy-on-write version + validate-before-activate lifecycle promised by
the career-profile-and-resume spec.

Iron rules honored:

- **Server-side candidate ownership** (Iron Rule 1): every method takes a
  ``candidate_id`` the caller resolved from the authenticated session. The
  service never reads a candidate id from a request body.
- **Immutable versions** (Iron Rule 3): ``create_version`` persists a new
  inactive snapshot, then activates it in the same logical operation so the
  per-candidate "at most one active" invariant never breaks. Validation runs
  both in the service (early) and in the repository (defense-in-depth); an
  invalid version never becomes active and never replaces the prior active
  version (spec: "Invalid preference cannot become active").
- **Validation** (task 2.3 hook): contradictory preferences
  (required∩excluded location, inverted compensation range) raise
  :class:`InvalidStateError` from Phase 0 *before* any persistence attempt.

The service is intentionally thin: the repository already owns the transactional
activate-pair and the re-validation-on-activate. The service adds the
version-number assignment, the input shaping, and a Protocol seam so routes can
depend on the service while tests substitute the in-memory repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.api.errors import InvalidStateError
from careerops.domain.profiles import (
    Authorization,
    CompensationPreference,
    HardExclusions,
    LocationPreference,
    ProfileVersion,
    RemoteRules,
    TargetRole,
)
from careerops.infrastructure.database.profile_validation import (
    validate_profile_preferences,
)

__all__ = [
    "ProfilePreferences",
    "ProfileRepositoryProtocol",
    "ProfileService",
]

# Bounded rules version for the deterministic matching/filter ruleset a profile
# version is validated against. A rules change in a later stage bumps this so a
# re-activation can detect that a stored version must be re-validated.
PROFILE_RULES_VERSION = "career-profile-v1"


@dataclass(frozen=True, slots=True)
class ProfilePreferences:
    """Structured preference input for a new profile version.

    Carries the same compound types as :class:`ProfileVersion` minus the
    identity/version/active/timestamp fields the service owns. Defaults mirror
    the domain dataclasses so a caller can update a single field.
    """

    target_roles: tuple[TargetRole, ...] = ()
    locations: tuple[LocationPreference, ...] = ()
    remote_rules: RemoteRules = field(default_factory=RemoteRules)
    compensation: CompensationPreference = field(default_factory=CompensationPreference)
    seniority: str = ""
    authorization: Authorization = field(default_factory=Authorization)
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    hard_exclusions: HardExclusions = field(default_factory=HardExclusions)


class ProfileRepositoryProtocol(Protocol):
    """Repository seam consumed by :class:`ProfileService`.

    Satisfied by :class:`PostgresProfileRepository` and
    :class:`InMemoryProfileRepository`. Every method scopes by the
    server-resolved ``candidate_id``; there is no unscoped read.
    """

    def get_active_for(self, candidate_id: UUID) -> ProfileVersion | None: ...

    def get_by_version_id(self, candidate_id: UUID, version_id: UUID) -> ProfileVersion: ...

    def list_versions(self, candidate_id: UUID, *, limit: int = 50) -> list[ProfileVersion]: ...

    def create_version(self, profile: ProfileVersion) -> ProfileVersion: ...

    def activate(
        self, candidate_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> ProfileVersion: ...


class ProfileService:
    """Application service for the versioned career profile.

    The service assigns the per-candidate monotonically increasing version
    number, validates preferences before persistence, and activates exactly one
    version per candidate. It does NOT fall back to an unscoped or in-memory
    store when the repository is missing — the route layer surfaces that as
    ``DependencyNotReadyError`` (503) via :func:`require_repository`.
    """

    def __init__(self, repository: ProfileRepositoryProtocol) -> None:
        self._repo = repository

    # -- reads --------------------------------------------------------------

    def get_active(self, candidate_id: UUID) -> ProfileVersion | None:
        """Return the active profile version for the candidate, or ``None``."""
        return self._repo.get_active_for(candidate_id)

    def list_versions(self, candidate_id: UUID, *, limit: int = 50) -> list[ProfileVersion]:
        """Return profile-version history for the candidate, newest first."""
        return self._repo.list_versions(candidate_id, limit=limit)

    def get_version(self, candidate_id: UUID, version_id: UUID) -> ProfileVersion:
        """Return one version, scoped by candidate ownership.

        Raises :class:`NotFoundError` (404) if the version does not exist or
        belongs to a different candidate — a client-supplied ``version_id`` for
        another candidate never leaks through (Iron Rule 1).
        """
        return self._repo.get_by_version_id(candidate_id, version_id)

    # -- writes -------------------------------------------------------------

    def create_version(
        self,
        candidate_id: UUID,
        preferences: ProfilePreferences,
        *,
        activate: bool = True,
        rules_version: str = PROFILE_RULES_VERSION,
        now: datetime | None = None,
    ) -> ProfileVersion:
        """Validate, persist, and (by default) activate a new profile version.

        The new version is always persisted inactive first, then activated via
        the repository's transactional activate-pair so the per-candidate
        partial unique index is never transiently violated. Validation runs
        here (early, before the version is even constructed) and again inside
        the repository (defense-in-depth) — an invalid preference set raises
        :class:`InvalidStateError` and never displaces the prior active
        version.
        """
        version_number = self._next_version_number(candidate_id)
        profile = ProfileVersion(
            id=uuid4(),
            candidate_id=candidate_id,
            version=version_number,
            is_active=activate,
            target_roles=preferences.target_roles,
            locations=preferences.locations,
            remote_rules=preferences.remote_rules,
            compensation=preferences.compensation,
            seniority=preferences.seniority,
            authorization=preferences.authorization,
            include_keywords=preferences.include_keywords,
            exclude_keywords=preferences.exclude_keywords,
            hard_exclusions=preferences.hard_exclusions,
            rules_version=rules_version,
            created_at=now,
            updated_at=now,
        )
        # Early validation: surface InvalidStateError before touching the repo.
        # The repo re-validates on create + activate, so this is belt-and-
        # braces, but it keeps the error path identical for the in-memory and
        # Postgres implementations and avoids a wasted round-trip.
        validate_profile_preferences(profile)
        return self._repo.create_version(profile)

    def activate_version(
        self, candidate_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> ProfileVersion:
        """Activate an existing version (re-validated against current rules).

        A version that became contradictory after the matching rules changed
        must not flip to active — the repository re-validates and raises
        :class:`InvalidStateError` in that case (spec: "Invalid preference
        cannot become active").
        """
        return self._repo.activate(candidate_id, version_id, now=now)

    # -- helpers ------------------------------------------------------------

    def _next_version_number(self, candidate_id: UUID) -> int:
        """Return the next per-candidate version number (max(history) + 1).

        ``list_versions`` returns history scoped by candidate, newest first.
        The max version seen is the current high-water mark; a brand-new
        candidate with no history starts at 1.
        """
        history = self._repo.list_versions(candidate_id, limit=50)
        if not history:
            return 1
        return max(v.version for v in history) + 1


# Re-export InvalidStateError so route callers can import everything from one
# place; the service is the documented surface for profile lifecycle errors.
__all__ += ["InvalidStateError"]
