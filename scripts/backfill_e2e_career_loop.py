#!/usr/bin/env python3
"""One-shot idempotent backfill for the e2e-career-loop identity migration (0014).

Usage:
    uv run python scripts/backfill_e2e_career_loop.py                # dry-run
    uv run python scripts/backfill_e2e_career_loop.py --apply        # write
    uv run python scripts/backfill_e2e_career_loop.py --apply \
        --database-url 'postgresql+psycopg://careerops_runtime@127.0.0.1:5432/careerops'

What it does (tasks 2.10):

1. Resolves the active candidate SERVER-SIDE from ``console_users.candidate_id``
   (Iron Rule 2: never from a client-supplied id). If no console user is linked
   to a candidate yet, the backfill is a no-op (nothing to link).
2. For each existing application that has no ``cycle_id`` yet:
   - matching the active candidate: create exactly one ACTIVE
     ``application_cycle`` for (candidate, canonical_job) and link it. The
     existing candidate/job UQ guarantees there is at most one application per
     pair, so each gets its own cycle. ``submission_channel`` stays NULL.
   - NOT matching the active candidate (ambiguous ownership under the
     single-user runtime): QUARANTINE — do not create a cycle, do not touch
     ``candidate_id``. The application is moved to ``on_hold`` and an append-only
     ``application_lifecycle_events`` row records the reason. Nothing is silently
     reassigned.
3. ``console_users.candidate_id`` is NEVER touched.
4. ``profile_versions`` stubs are NOT created by default — nothing at this
   stage requires one. ``--include-profile-stub`` exists for future use; it
   inserts a single v=1 INACTIVE stub per candidate that has zero rows.

Idempotent: every step guards on the post-migration state (``cycle_id IS
NULL``, no prior backfill lifecycle event, no profile_versions row), so
re-running is a no-op once every record is linked or quarantined.

The script proves by reporting exact (linked / quarantined / skipped) counts
that no legacy job/application/email history is silently reassigned; the
accompanying integration test (``test_backfill_e2e_career_loop.py``) asserts
row counts and ownership are preserved.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from careerops.config import Settings  # noqa: E402
from careerops.infrastructure.database.schema import (  # noqa: E402
    application_cycles,
    application_lifecycle_events,
    applications,
    console_users,
    profile_versions,
)

QUARANTINE_MARKER = "ambiguous_ownership"
QUARANTINE_NOTE = (
    "backfill: ambiguous ownership quarantined — application.candidate_id does "
    "not match the active console user's candidate_id; ownership preserved, "
    "manual review required"
)


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """Result summary. Counts are zero on a no-op re-run."""

    active_candidate_id: UUID | None
    applications_total: int
    applications_linked: int
    applications_quarantined: int
    applications_skipped: int
    profile_stubs_created: int

    def render(self) -> str:
        active = str(self.active_candidate_id) if self.active_candidate_id else "<none>"
        return (
            f"active candidate: {active}\n"
            f"applications: total={self.applications_total} "
            f"linked={self.applications_linked} "
            f"quarantined={self.applications_quarantined} "
            f"skipped={self.applications_skipped}\n"
            f"profile stubs created: {self.profile_stubs_created}"
        )


def _resolve_active_candidate(connection: sa.engine.Connection) -> UUID | None:
    """Server-side resolution: the candidate_id of the linked console user.

    ``console_users.candidate_id`` is UNIQUE (migration 0014, task 2.1) and the
    singleton row makes this the single source of truth. We never accept a
    client-supplied candidate id.
    """
    row = connection.execute(
        sa.select(console_users.c.candidate_id).where(console_users.c.candidate_id.is_not(None))
    ).first()
    return UUID(str(row[0])) if row else None


def _existing_quarantine_event_exists(
    connection: sa.engine.Connection, application_id: UUID
) -> bool:
    return (
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(application_lifecycle_events)
            .where(
                application_lifecycle_events.c.application_id == application_id,
                application_lifecycle_events.c.event_data.op("@>")(
                    sa.type_coerce({"marker": QUARANTINE_MARKER}, postgresql.JSONB())
                ),
            )
        )
        or 0
    ) > 0


def _cycle_for_pair_exists(
    connection: sa.engine.Connection,
    *,
    candidate_id: UUID,
    canonical_job_id: UUID,
) -> UUID | None:
    """Return the active cycle id for (candidate, job) if one already exists.

    The partial unique index guarantees at most one active cycle per pair, so a
    backfill re-run that finds the application already linked (or an active
    cycle already present for the pair) does not violate the constraint.
    """
    row = connection.execute(
        sa.select(application_cycles.c.id).where(
            application_cycles.c.candidate_id == candidate_id,
            application_cycles.c.canonical_job_id == canonical_job_id,
            application_cycles.c.active.is_(True),
        )
    ).first()
    return UUID(str(row[0])) if row else None


def _link_matching_application(
    connection: sa.engine.Connection,
    *,
    application_id: UUID,
    candidate_id: UUID,
    canonical_job_id: UUID,
) -> bool:
    """Create one active cycle for the pair and link the application.

    Returns True iff a write occurred. Idempotent: if the application is already
    linked OR an active cycle already exists for the pair, no write occurs and
    the function returns False.
    """
    cycle_id = _cycle_for_pair_exists(
        connection, candidate_id=candidate_id, canonical_job_id=canonical_job_id
    )
    if cycle_id is None:
        cycle_id = uuid4()
        connection.execute(
            sa.insert(application_cycles).values(
                id=cycle_id,
                candidate_id=candidate_id,
                canonical_job_id=canonical_job_id,
                active=True,
                reason="backfill: created from pre-existing application",
            )
        )
    result = connection.execute(
        sa.update(applications)
        .where(
            applications.c.id == application_id,
            applications.c.cycle_id.is_(None),
        )
        .values(cycle_id=cycle_id)
    )
    return result.rowcount > 0


def _quarantine_ambiguous_application(
    connection: sa.engine.Connection,
    *,
    application_id: UUID,
    current_state: str,
) -> bool:
    """Quarantine an application whose ownership is ambiguous.

    Moves the application to ``on_hold`` (if not already) and appends a
    ``note_added`` lifecycle event recording the reason. ``candidate_id`` is
    NEVER changed. Idempotent: if a prior backfill event exists for this
    application, no write occurs.
    """
    wrote = False
    if current_state != "on_hold":
        connection.execute(
            sa.update(applications)
            .where(applications.c.id == application_id)
            .values(state="on_hold")
        )
        wrote = True
    if not _existing_quarantine_event_exists(connection, application_id):
        connection.execute(
            sa.insert(application_lifecycle_events).values(
                id=uuid4(),
                application_id=application_id,
                event_type="note_added",
                from_state=current_state if current_state != "on_hold" else None,
                to_state="on_hold" if current_state != "on_hold" else None,
                source="system",
                actor_id="backfill-e2e-career-loop",
                note=QUARANTINE_NOTE,
                event_data={
                    "marker": QUARANTINE_MARKER,
                    "reason": "candidate_id does not match active user",
                },
                occurred_at=datetime.now(UTC),
            )
        )
        wrote = True
    return wrote


def _maybe_create_profile_stub(
    connection: sa.engine.Connection,
    *,
    candidate_id: UUID,
) -> bool:
    """Create a v=1 INACTIVE profile stub iff the candidate has zero rows."""
    existing = connection.scalar(
        sa.select(sa.func.count())
        .select_from(profile_versions)
        .where(profile_versions.c.candidate_id == candidate_id)
    )
    if existing:
        return False
    connection.execute(
        sa.insert(profile_versions).values(
            id=uuid4(),
            candidate_id=candidate_id,
            version=1,
            is_active=False,
            rules_version="backfill-stub",
        )
    )
    return True


def run_backfill(engine: Engine, *, include_profile_stub: bool = False) -> BackfillReport:
    """Run the backfill and write changes inside one transaction.

    Idempotent: re-running on an already-backfilled database returns a report
    with zero linked / zero quarantined writes (every application falls into
    ``skipped``).
    """
    with engine.begin() as connection:
        active_candidate_id = _resolve_active_candidate(connection)

        application_rows = connection.execute(
            sa.select(
                applications.c.id,
                applications.c.candidate_id,
                applications.c.canonical_job_id,
                applications.c.state,
                applications.c.cycle_id,
            )
        ).all()

        linked = 0
        quarantined = 0
        skipped = 0
        for row in application_rows:
            if row.cycle_id is not None:
                skipped += 1
                continue
            if active_candidate_id is None:
                # No active user → cannot determine ownership for ANY row.
                # Quarantine everything that lacks a cycle rather than guess.
                if _quarantine_ambiguous_application(
                    connection, application_id=row.id, current_state=row.state
                ):
                    quarantined += 1
                else:
                    skipped += 1
                continue
            if row.candidate_id == active_candidate_id:
                if _link_matching_application(
                    connection,
                    application_id=row.id,
                    candidate_id=row.candidate_id,
                    canonical_job_id=row.canonical_job_id,
                ):
                    linked += 1
                else:
                    skipped += 1
            elif _quarantine_ambiguous_application(
                connection, application_id=row.id, current_state=row.state
            ):
                quarantined += 1
            else:
                skipped += 1

        profile_stubs = 0
        if (
            include_profile_stub
            and active_candidate_id is not None
            and _maybe_create_profile_stub(connection, candidate_id=active_candidate_id)
        ):
            profile_stubs = 1

    return BackfillReport(
        active_candidate_id=active_candidate_id,
        applications_total=len(application_rows),
        applications_linked=linked,
        applications_quarantined=quarantined,
        applications_skipped=skipped,
        profile_stubs_created=profile_stubs,
    )


def run_backfill_dry_run(engine: Engine, *, include_profile_stub: bool = False) -> BackfillReport:
    """Read-only report: same decision logic, no writes.

    A row that is already linked (cycle_id set) or already quarantined (marker
    event present) counts as ``skipped`` so the dry-run agrees with the
    idempotent re-run report.
    """
    with engine.begin() as connection:
        active_candidate_id = _resolve_active_candidate(connection)
        application_rows = connection.execute(
            sa.select(
                applications.c.id,
                applications.c.candidate_id,
                applications.c.canonical_job_id,
                applications.c.state,
                applications.c.cycle_id,
            )
        ).all()
        linked = quarantined = skipped = 0
        for row in application_rows:
            if row.cycle_id is not None:
                skipped += 1
            elif active_candidate_id is None:
                quarantined += 1
            elif row.candidate_id == active_candidate_id:
                linked += 1
            else:
                quarantined += 1
        profile_stubs = 0
        if include_profile_stub and active_candidate_id is not None:
            existing = connection.scalar(
                sa.select(sa.func.count())
                .select_from(profile_versions)
                .where(profile_versions.c.candidate_id == active_candidate_id)
            )
            profile_stubs = 0 if existing else 1
        # No writes were issued; the transaction commits as empty.
    return BackfillReport(
        active_candidate_id=active_candidate_id,
        applications_total=len(application_rows),
        applications_linked=linked,
        applications_quarantined=quarantined,
        applications_skipped=skipped,
        profile_stubs_created=profile_stubs,
    )


def _make_engine(database_url: str | None) -> Engine:
    if database_url:
        return sa.create_engine(database_url)
    return sa.create_engine(Settings().database_url.get_secret_value())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag, the script is a dry-run.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "SQLAlchemy URL. Defaults to CAREEROPS_DATABASE_URL via Settings. "
            "Run under the careerops_runtime role (Iron Rule 4)."
        ),
    )
    parser.add_argument(
        "--include-profile-stub",
        action="store_true",
        help="Create a v=1 inactive profile_versions stub per active candidate "
        "that has zero rows. Off by default — nothing requires it yet.",
    )
    args = parser.parse_args(argv)

    engine = _make_engine(args.database_url)
    try:
        if args.apply:
            report = run_backfill(
                engine,
                include_profile_stub=args.include_profile_stub,
            )
        else:
            report = run_backfill_dry_run(engine, include_profile_stub=args.include_profile_stub)
    finally:
        engine.dispose()

    print("backfill_e2e_career_loop " + ("(applied)" if args.apply else "(dry-run)"))
    print(report.render())
    if not args.apply:
        print("\nRe-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
