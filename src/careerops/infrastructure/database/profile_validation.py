"""Shared validation hook for versioned career profile preferences.

end-to-end-career-application-loop, Section 2 (career-profile-and-resume spec,
task 2.3 service-level validation).

The DB layer enforces shape guards and the compensation-range check directly
on ``profile_versions`` (migration 0014). This module supplies the
application-level contradictions the DB cannot express today (a location that
is simultaneously the only ``REQUIRED`` and ``EXCLUDED``). Both the in-memory
and Postgres profile repositories call :func:`validate_profile_preferences`
before persisting or activating a version so an invalid version never becomes
active (Iron Rule 6 — immutable versions, one active per candidate).

The full service-level validator (task 2.3) layers additional coherence checks
on top of this hook (currency/period normalization, radius sanity, ...). This
module deliberately keeps to the contradictions the schema and repositories
MUST agree on regardless of service policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from careerops.api.errors import InvalidStateError
from careerops.domain.profiles import LocationKind

if TYPE_CHECKING:
    from careerops.domain.profiles import ProfileVersion

__all__ = ["validate_profile_preferences"]


def validate_profile_preferences(profile: ProfileVersion) -> None:
    """Raise :class:`InvalidStateError` for contradictory profile preferences.

    Checked contradictions (the set the DB cannot express alone):

    1. A location name appears as both ``REQUIRED`` and ``EXCLUDED`` — the
       candidate would simultaneously demand and forbid the same place.
    2. A location name appears as both ``PREFERRED`` and ``EXCLUDED`` — the
       candidate would both welcome and forbid the same place. (PREFERRED is a
       soft signal, but a hard exclusion must win; flagging the contradiction
       avoids a confusing filter outcome.)
    3. Compensation ``amount_min`` > ``amount_max`` when both are numbers. The
       DB check constraint enforces the same on jsonb values; this catches the
       error before the round trip and keeps the in-memory and Postgres paths
       identical.

    The schema-level guards (jsonb shape, compensation range, version > 0) and
    the service-level normalization (currency/period coherence, radius sanity,
    seniority/role normalization) are intentionally NOT duplicated here.
    """
    # 1 + 2: location contradictions. A name appearing under two conflicting
    # kinds is ambiguous regardless of which one wins, so collect each kind's
    # names and intersect.
    required_names: set[str] = set()
    preferred_names: set[str] = set()
    excluded_names: set[str] = set()
    for location in profile.locations:
        if not location.name:
            continue
        if location.kind is LocationKind.REQUIRED:
            required_names.add(location.name)
        elif location.kind is LocationKind.PREFERRED:
            preferred_names.add(location.name)
        elif location.kind is LocationKind.EXCLUDED:
            excluded_names.add(location.name)

    required_excluded = required_names & excluded_names
    if required_excluded:
        raise InvalidStateError(
            f"location(s) cannot be both required and excluded: {sorted(required_excluded)}"
        )
    preferred_excluded = preferred_names & excluded_names
    if preferred_excluded:
        raise InvalidStateError(
            f"location(s) cannot be both preferred and excluded: {sorted(preferred_excluded)}"
        )

    # 3: compensation range. ``amount_min``/``amount_max`` may be None on
    # either side (open range); only flag when both are present and inverted.
    comp = profile.compensation
    if (
        comp.amount_min is not None
        and comp.amount_max is not None
        and comp.amount_min > comp.amount_max
    ):
        raise InvalidStateError(
            f"compensation amount_min ({comp.amount_min}) exceeds amount_max ({comp.amount_max})"
        )
