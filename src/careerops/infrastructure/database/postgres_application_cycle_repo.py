"""PostgreSQL-backed application-cycle repository.

end-to-end-career-application-loop, Section 2 (application-workspace spec,
tasks 2.6 + design Decision 8).

At most one :class:`ApplicationCycle` is active per (candidate, canonical_job)
pair at a time — enforced at the DB by the partial unique index
``ix_application_cycles_candidate_job_active`` and reinforced transactionally
by :meth:`PostgresApplicationCycleRepository.open_reapplication_cycle`, which
closes the prior active cycle and opens a new one linked via
``prior_cycle_id`` inside one transaction (preserving full history).

Server-side candidate ownership (Iron Rule 2): every method scopes by the
server-resolved ``candidate_id``. The capability gating that may route
re-application decisions through the Phase 0 resolver lives at the service
layer (stage 3); this repository performs the data invariants only.

Not wired into ``RuntimeResources`` yet (task 2.11).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from careerops.api.errors import NotFoundError
from careerops.domain.applications import ApplicationCycle
from careerops.infrastructure.database.schema import application_cycles

__all__ = ["PostgresApplicationCycleRepository"]


def _row_to_cycle(row: sa.RowMapping) -> ApplicationCycle:
    return ApplicationCycle(
        id=row["id"],
        candidate_id=row["candidate_id"],
        canonical_job_id=row["canonical_job_id"],
        active=bool(row["active"]),
        prior_cycle_id=row["prior_cycle_id"],
        reason=str(row["reason"]),
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


class PostgresApplicationCycleRepository:
    """PostgreSQL-backed application-cycle store."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- reads --------------------------------------------------------------

    def get_active_for(self, candidate_id: UUID, canonical_job_id: UUID) -> ApplicationCycle | None:
        """Return the active cycle for the (candidate, job) pair, if any."""
        stmt = sa.select(application_cycles).where(
            sa.and_(
                application_cycles.c.candidate_id == candidate_id,
                application_cycles.c.canonical_job_id == canonical_job_id,
                application_cycles.c.active.is_(True),
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_cycle(row) if row else None

    def get_by_id(self, candidate_id: UUID, cycle_id: UUID) -> ApplicationCycle:
        """Return a cycle by id, scoped by candidate ownership."""
        stmt = sa.select(application_cycles).where(
            sa.and_(
                application_cycles.c.id == cycle_id,
                application_cycles.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFoundError("application cycle not found for candidate")
        return _row_to_cycle(row)

    def list_history(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        limit: int = 50,
    ) -> list[ApplicationCycle]:
        """Return the cycle history (newest first) for a candidate/job pair."""
        stmt = (
            sa.select(application_cycles)
            .where(
                sa.and_(
                    application_cycles.c.candidate_id == candidate_id,
                    application_cycles.c.canonical_job_id == canonical_job_id,
                )
            )
            .order_by(application_cycles.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_cycle(row) for row in rows]

    # -- writes -------------------------------------------------------------

    def get_or_create_active(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle:
        """Return the active cycle for the pair, or create one.

        On conflict with an existing active cycle (partial unique index), the
        existing cycle is returned unchanged — the caller-provided ``cycle_id``
        is only used when there is no active cycle yet. This keeps the
        one-active-cycle invariant while remaining idempotent for retries.
        """
        existing = self.get_active_for(candidate_id, canonical_job_id)
        if existing is not None:
            return existing

        values = {
            "id": cycle_id,
            "candidate_id": candidate_id,
            "canonical_job_id": canonical_job_id,
            "active": True,
            "prior_cycle_id": None,
            "reason": reason,
        }
        with self._engine.begin() as conn:
            try:
                conn.execute(pg_insert(application_cycles).values(**values))
            except IntegrityError:
                # Another request won the race to create the active cycle.
                conn.rollback()
            # Either we just inserted or a concurrent txn did — re-read.
        return self._must_get_active(candidate_id, canonical_job_id)

    def _must_get_active(self, candidate_id: UUID, canonical_job_id: UUID) -> ApplicationCycle:
        active = self.get_active_for(candidate_id, canonical_job_id)
        if active is None:
            raise NotFoundError("active application cycle disappeared after get-or-create")
        return active

    def open_reapplication_cycle(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        new_cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle:
        """Close the current active cycle and open a new one linked to it.

        The prior cycle's ``active`` flips to false and ``closed_at`` is set;
        the new cycle references the prior via ``prior_cycle_id``. The pair is
        mutated in one transaction so the partial unique index never
        transiently sees two active rows for the same (candidate, job).
        """
        closed_at = now or datetime.now(UTC)
        with self._engine.begin() as conn:
            prior = (
                conn.execute(
                    sa.select(application_cycles).where(
                        sa.and_(
                            application_cycles.c.candidate_id == candidate_id,
                            application_cycles.c.canonical_job_id == canonical_job_id,
                            application_cycles.c.active.is_(True),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if prior is None:
                raise NotFoundError("no active application cycle to reapply from")
            prior_id: UUID = prior["id"]
            # Close the prior cycle first so the partial unique index allows
            # the new active row.
            conn.execute(
                sa.update(application_cycles)
                .where(application_cycles.c.id == prior_id)
                .values(active=False, closed_at=closed_at)
            )
            conn.execute(
                application_cycles.insert().values(
                    id=new_cycle_id,
                    candidate_id=candidate_id,
                    canonical_job_id=canonical_job_id,
                    active=True,
                    prior_cycle_id=prior_id,
                    reason=reason,
                )
            )
        return self._must_get_active(candidate_id, canonical_job_id)
