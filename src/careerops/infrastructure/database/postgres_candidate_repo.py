from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from careerops.domain.candidates import Candidate
from careerops.infrastructure.database.schema import candidates


class PostgresCandidateRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def list_all(self, *, limit: int = 50) -> list[Candidate]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(candidates).order_by(candidates.c.created_at).limit(limit)
                )
                .mappings()
                .all()
            )
        return [_row(r) for r in rows]

    def create(self, c: Candidate) -> Candidate:
        with self._engine.begin() as conn:
            conn.execute(candidates.insert().values(id=c.id, display_name=c.display_name))
        return self.get(c.id)

    def get(self, cid: UUID) -> Candidate | None:
        with self._engine.begin() as conn:
            r = (
                conn.execute(sa.select(candidates).where(candidates.c.id == cid))
                .mappings()
                .first()
            )
        return _row(r) if r else None


def _row(r) -> Candidate:
    return Candidate(
        id=r["id"],
        display_name=r["display_name"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )
