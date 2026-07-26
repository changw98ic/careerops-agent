"""PostgreSQL-backed profile-version repository.

end-to-end-career-application-loop, Section 2 (career-profile-and-resume spec,
tasks 2.2 + 2.3).

A :class:`ProfileVersion` is an immutable copy-on-write snapshot of the user's
search preferences (design Decision 4). Exactly one version is active per
candidate at a time — enforced at the DB by the partial unique index
``ix_profile_versions_candidate_active`` and reinforced transactionally by
:class:`PostgresProfileRepository.activate`, which deactivates the prior
active version in the same transaction that flips the new one to active.

Server-side candidate ownership (Iron Rule 2): every method takes a
``candidate_id`` parameter that the caller resolves from the authenticated
console user (``console_users.candidate_id``). The repository scopes every
read/write by that id; ``get_by_version_id`` rejects a version id that belongs
to a different candidate instead of silently returning it.

This module is intentionally NOT wired into ``RuntimeResources`` yet
(task 2.11). The capability gating that routes external-effect profile
decisions through the Phase 0 resolver lives at the service layer (stage 3).
"""

# The jsonb <-> domain helpers below read untyped JSONB columns (target_roles,
# locations, remote_rules, ...) out of ``sa.RowMapping`` and reshape them.
# SQLAlchemy types RowMapping values as ``Any``, so every ``.get()`` / index on
# those values is unknown-type noise that obscures the real mapping logic.
# Suppress the unknown-family reports for this file the same way
# ``infrastructure/runtime.py`` does for its sqlalchemy/graph wiring.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.domain.profiles import (
    Authorization,
    CompensationPreference,
    HardExclusions,
    LocationKind,
    LocationPreference,
    ProfileVersion,
    RemoteRules,
    TargetRole,
)
from careerops.infrastructure.database.profile_validation import (
    validate_profile_preferences,
)
from careerops.infrastructure.database.schema import profile_versions

__all__ = ["PostgresProfileRepository"]


# ---------------------------------------------------------------------------
# Row <-> domain mapping
# ---------------------------------------------------------------------------


def _target_roles_to_domain(raw: Any) -> tuple[TargetRole, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[TargetRole] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        items.append(
            TargetRole(
                title=str(entry.get("title", "")),
                seniority=str(entry.get("seniority", "")),
                notes=str(entry.get("notes", "")),
            )
        )
    return tuple(items)


def _locations_to_domain(raw: Any) -> tuple[LocationPreference, ...]:
    if not isinstance(raw, list):
        return ()
    items: list[LocationPreference] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind_value = str(entry.get("kind", "preferred"))
        try:
            kind = LocationKind(kind_value)
        except ValueError:
            kind = LocationKind.PREFERRED
        radius = entry.get("radius_km")
        items.append(
            LocationPreference(
                name=str(entry.get("name", "")),
                kind=kind,
                radius_km=int(radius) if isinstance(radius, int) else None,
            )
        )
    return tuple(items)


def _remote_rules_to_domain(raw: Any) -> RemoteRules:
    if not isinstance(raw, dict):
        return RemoteRules()
    return RemoteRules(
        remote_allowed=bool(raw.get("remote_allowed", False)),
        hybrid_allowed=bool(raw.get("hybrid_allowed", False)),
        onsite_required=bool(raw.get("onsite_required", False)),
        timezone=str(raw.get("timezone", "")),
    )


def _compensation_to_domain(raw: Any) -> CompensationPreference:
    if not isinstance(raw, dict):
        return CompensationPreference()
    amount_min = raw.get("amount_min")
    amount_max = raw.get("amount_max")
    period_value = raw.get("period", "")
    return CompensationPreference(
        currency=str(raw.get("currency", "")),
        amount_min=int(amount_min) if isinstance(amount_min, (int, float)) else None,
        amount_max=int(amount_max) if isinstance(amount_max, (int, float)) else None,
        period=str(period_value) if period_value else "",
        equity=bool(raw.get("equity", False)),
    )


def _authorization_to_domain(raw: Any) -> Authorization:
    if not isinstance(raw, dict):
        return Authorization()
    locale_raw = raw.get("locale_restrictions", [])
    locale_tuple: tuple[str, ...] = ()
    if isinstance(locale_raw, list):
        locale_tuple = tuple(str(item) for item in locale_raw)
    return Authorization(
        work_authorization=str(raw.get("work_authorization", "")),
        visa_sponsorship_required=bool(raw.get("visa_sponsorship_required", False)),
        locale_restrictions=locale_tuple,
    )


def _hard_exclusions_to_domain(raw: Any) -> HardExclusions:
    if not isinstance(raw, dict):
        return HardExclusions()

    def _to_tuple(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        return tuple(str(item) for item in value) if isinstance(value, list) else ()

    return HardExclusions(
        companies=_to_tuple("companies"),
        titles=_to_tuple("titles"),
        keywords=_to_tuple("keywords"),
    )


def _keywords_to_domain(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


def _row_to_profile(row: sa.RowMapping) -> ProfileVersion:
    return ProfileVersion(
        id=row["id"],
        candidate_id=row["candidate_id"],
        version=row["version"],
        is_active=bool(row["is_active"]),
        target_roles=_target_roles_to_domain(row["target_roles"]),
        locations=_locations_to_domain(row["locations"]),
        remote_rules=_remote_rules_to_domain(row["remote_rules"]),
        compensation=_compensation_to_domain(row["compensation"]),
        seniority=str(row["seniority"]),
        authorization=_authorization_to_domain(row["authorization_rules"]),
        include_keywords=_keywords_to_domain(row["include_keywords"]),
        exclude_keywords=_keywords_to_domain(row["exclude_keywords"]),
        hard_exclusions=_hard_exclusions_to_domain(row["hard_exclusions"]),
        rules_version=str(row["rules_version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Domain -> jsonb serialization
# ---------------------------------------------------------------------------


def _target_roles_to_json(roles: tuple[TargetRole, ...]) -> list[dict[str, object]]:
    return [{"title": r.title, "seniority": r.seniority, "notes": r.notes} for r in roles]


def _locations_to_json(locations: tuple[LocationPreference, ...]) -> list[dict[str, object]]:
    return [
        {
            "name": loc.name,
            "kind": loc.kind.value,
            "radius_km": loc.radius_km,
        }
        for loc in locations
    ]


def _remote_rules_to_json(rules: RemoteRules) -> dict[str, object]:
    return {
        "remote_allowed": rules.remote_allowed,
        "hybrid_allowed": rules.hybrid_allowed,
        "onsite_required": rules.onsite_required,
        "timezone": rules.timezone,
    }


def _compensation_to_json(comp: CompensationPreference) -> dict[str, object]:
    return {
        "currency": comp.currency,
        "amount_min": comp.amount_min,
        "amount_max": comp.amount_max,
        "period": getattr(comp.period, "value", str(comp.period)),
        "equity": comp.equity,
    }


def _authorization_to_json(auth: Authorization) -> dict[str, object]:
    return {
        "work_authorization": auth.work_authorization,
        "visa_sponsorship_required": auth.visa_sponsorship_required,
        "locale_restrictions": list(auth.locale_restrictions),
    }


def _hard_exclusions_to_json(excl: HardExclusions) -> dict[str, object]:
    return {
        "companies": list(excl.companies),
        "titles": list(excl.titles),
        "keywords": list(excl.keywords),
    }


class PostgresProfileRepository:
    """PostgreSQL-backed profile-version store.

    Every method opens its own transaction via ``engine.begin()``. The
    ``activate`` path performs the deactivate-prior + activate-new pair inside
    a single transaction so the partial unique index
    ``ix_profile_versions_candidate_active`` is never transiently violated.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- reads --------------------------------------------------------------

    def get_active_for(self, candidate_id: UUID) -> ProfileVersion | None:
        """Return the active profile version for the server-resolved candidate."""
        stmt = sa.select(profile_versions).where(
            sa.and_(
                profile_versions.c.candidate_id == candidate_id,
                profile_versions.c.is_active.is_(True),
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_profile(row) if row else None

    def get_by_version_id(self, candidate_id: UUID, version_id: UUID) -> ProfileVersion:
        """Return the requested version, scoped by the server-resolved candidate.

        Raises :class:`NotFoundError` if the version does not exist or belongs
        to a different candidate — a client-supplied ``version_id`` for another
        candidate never leaks through (Iron Rule 2).
        """
        stmt = sa.select(profile_versions).where(
            sa.and_(
                profile_versions.c.id == version_id,
                profile_versions.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFoundError("profile version not found for candidate")
        return _row_to_profile(row)

    def list_versions(self, candidate_id: UUID, *, limit: int = 50) -> list[ProfileVersion]:
        stmt = (
            sa.select(profile_versions)
            .where(profile_versions.c.candidate_id == candidate_id)
            .order_by(profile_versions.c.version.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_profile(row) for row in rows]

    # -- writes -------------------------------------------------------------

    def create_version(self, profile: ProfileVersion) -> ProfileVersion:
        """Persist a new immutable profile version.

        Validates contradictory preferences first (task 2.3 hook) so an invalid
        version never reaches the DB and never becomes active. The version is
        stored inactive unless ``is_active`` is already true on the domain
        object, in which case :meth:`activate` is used to flip it atomically.
        """
        validate_profile_preferences(profile)

        values = {
            "id": profile.id,
            "candidate_id": profile.candidate_id,
            "version": profile.version,
            "is_active": False,  # never store active directly; activate() flips it
            "target_roles": _target_roles_to_json(profile.target_roles),
            "locations": _locations_to_json(profile.locations),
            "remote_rules": _remote_rules_to_json(profile.remote_rules),
            "compensation": _compensation_to_json(profile.compensation),
            "seniority": profile.seniority,
            "authorization_rules": _authorization_to_json(profile.authorization),
            "include_keywords": list(profile.include_keywords),
            "exclude_keywords": list(profile.exclude_keywords),
            "hard_exclusions": _hard_exclusions_to_json(profile.hard_exclusions),
            "rules_version": profile.rules_version,
        }
        with self._engine.begin() as conn:
            conn.execute(pg_insert(profile_versions).values(**values))
            if profile.is_active:
                self._activate_in_connection(conn, profile.candidate_id, profile.id)

        return self.get_by_version_id(profile.candidate_id, profile.id)

    def activate(
        self, candidate_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> ProfileVersion:
        """Activate one version and deactivate the prior active version.

        Performs the deactivate-prior + activate-new pair in a single
        transaction. Re-validates the target version's preferences so a
        version that became contradictory after the rules changed cannot be
        re-activated. Raises :class:`NotFoundError` if the version id is not
        owned by ``candidate_id``.
        """
        with self._engine.begin() as conn:
            target = (
                conn.execute(
                    sa.select(profile_versions).where(
                        sa.and_(
                            profile_versions.c.id == version_id,
                            profile_versions.c.candidate_id == candidate_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                raise NotFoundError("profile version not found for candidate")
            # Re-validate before activating: a version that became
            # contradictory after the rules changed must not flip to active.
            validate_profile_preferences(_row_to_profile(target))
            self._activate_in_connection(conn, candidate_id, version_id)

        return self.get_by_version_id(candidate_id, version_id)

    @staticmethod
    def _activate_in_connection(conn: sa.Connection, candidate_id: UUID, version_id: UUID) -> None:
        """Deactivate the current active version, then flip the target active.

        Ordering matters: the partial unique index
        ``ix_profile_versions_candidate_active`` allows at most one active row
        per candidate, so the prior active row MUST be cleared before the new
        one is set within the same statement sequence.
        """
        conn.execute(
            sa.update(profile_versions)
            .where(
                sa.and_(
                    profile_versions.c.candidate_id == candidate_id,
                    profile_versions.c.is_active.is_(True),
                    profile_versions.c.id != version_id,
                )
            )
            .values(is_active=False, updated_at=sa.func.now())
        )
        conn.execute(
            sa.update(profile_versions)
            .where(profile_versions.c.id == version_id)
            .values(is_active=True, updated_at=sa.func.now())
        )

    # -- validation hook (re-exported for the service layer) ----------------

    @staticmethod
    def validate(profile: ProfileVersion) -> None:
        """Repository-level validation hook for the service layer (task 2.3).

        Raises :class:`InvalidStateError` on contradictory preferences. The
        service layer calls this before constructing a ``ProfileVersion`` so
        the error surfaces before any persistence attempt; the repository
        calls it again internally on create/activate to stay defense-in-depth.
        """
        validate_profile_preferences(profile)


# Re-export InvalidStateError so callers can import everything from one place.
__all__ += ["InvalidStateError"]
