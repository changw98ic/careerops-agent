"""PostgreSQL-backed evidence-item repository (resume-derived lifecycle).

end-to-end-career-application-loop, Section 2 (career-profile-and-resume spec,
task 2.5).

The existing :class:`PostgresMatchingReadRepository` covers the
repository/commit/path idempotency path used by the deterministic matcher. This
module focuses on the resume-derived lifecycle added in migration 0014:
``source_span``, ``extractor_version``, ``confirmation_status``,
``evidence_hash``, and ``resume_version_id``.

Invariants honored:

- **Append-only history**: evidence rows are never deleted; ``confirm`` and
  ``reject`` only flip ``confirmation_status``. The created_at-ordered
  sequence of evidence rows IS the audit trail — every extraction persists a
  new row, and a confirmation status change never destroys the prior state's
  evidence row.
- **Server-side candidate ownership** (Iron Rule 2): every read/write is
  scoped by the server-resolved ``candidate_id``; a client-supplied
  ``evidence_id`` for another candidate's evidence raises
  :class:`NotFoundError` rather than leaking the row.
- **Default-deny for packages**: only ``confirmation_status == CONFIRMED``
  evidence is eligible for approved application packages
  (:meth:`list_confirmed_for`).

Not wired into ``RuntimeResources`` yet (task 2.11).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.api.errors import NotFoundError
from careerops.domain.applications import ConfirmationStatus
from careerops.domain.candidates import EvidenceItem, EvidenceKind
from careerops.infrastructure.database.schema import evidence_items

__all__ = ["PostgresEvidenceRepository"]


def _row_to_evidence(row: sa.RowMapping) -> EvidenceItem:
    return EvidenceItem(
        id=row["id"],
        candidate_id=row["candidate_id"],
        kind=EvidenceKind(str(row["kind"])),
        name=str(row["name"]),
        description=str(row.get("description", "")),
        repository=str(row.get("repository", "")),
        commit_sha=str(row.get("commit_sha", "")),
        path=str(row.get("path", "")),
        symbol=str(row.get("symbol", "")),
        content_hash=str(row.get("content_hash", "")),
        source_url=str(row.get("source_url", "")),
        verified=bool(row.get("verified", False)),
        extractor_version=str(row.get("extractor_version", "")),
        source_span=str(row.get("source_span", "")),
        confirmation_status=ConfirmationStatus(str(row.get("confirmation_status", "unconfirmed"))),
        evidence_hash=str(row.get("evidence_hash", "")),
        resume_version_id=row.get("resume_version_id"),
        created_at=row.get("created_at"),
    )


class PostgresEvidenceRepository:
    """PostgreSQL-backed evidence lifecycle store.

    Coexists with :class:`PostgresMatchingReadRepository`, which still serves
    the deterministic-matcher read path. This repository owns the
    resume-derived lifecycle: bounded source spans, extractor version,
    confirmation state, and resume linkage.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- reads --------------------------------------------------------------

    def get_by_id(self, candidate_id: UUID, evidence_id: UUID) -> EvidenceItem:
        """Return an evidence item, scoped by candidate ownership."""
        stmt = sa.select(evidence_items).where(
            sa.and_(
                evidence_items.c.id == evidence_id,
                evidence_items.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFoundError("evidence item not found for candidate")
        return _row_to_evidence(row)

    def list_for_candidate(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]:
        stmt = (
            sa.select(evidence_items)
            .where(evidence_items.c.candidate_id == candidate_id)
            .order_by(evidence_items.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_evidence(row) for row in rows]

    def list_confirmed_for(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]:
        """Return CONFIRMED evidence only — the set eligible for packages."""
        stmt = (
            sa.select(evidence_items)
            .where(
                sa.and_(
                    evidence_items.c.candidate_id == candidate_id,
                    evidence_items.c.confirmation_status == ConfirmationStatus.CONFIRMED.value,
                )
            )
            .order_by(evidence_items.c.created_at.desc())
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_evidence(row) for row in rows]

    def find_by_evidence_hash(self, candidate_id: UUID, evidence_hash: str) -> EvidenceItem | None:
        """Return an existing evidence row by its bounded evidence_hash.

        Used for resume-extraction idempotency: an extractor that re-runs on
        the same resume content produces the same ``evidence_hash`` and gets
        the existing row back instead of a duplicate.
        """
        if not evidence_hash:
            return None
        stmt = sa.select(evidence_items).where(
            sa.and_(
                evidence_items.c.candidate_id == candidate_id,
                evidence_items.c.evidence_hash == evidence_hash,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_evidence(row) if row else None

    # -- writes -------------------------------------------------------------

    def store(self, item: EvidenceItem) -> EvidenceItem:
        """Persist a resume-derived evidence item.

        The idempotency key (candidate_id, repository, commit_sha, path,
        symbol, content_hash) stays intact for the repository-derived path;
        resume-derived evidence additionally carries source_span /
        extractor_version / confirmation_status / evidence_hash /
        resume_version_id. The DB-level unique constraint on the idempotency
        key means a re-store of the same repository-derived row is a no-op
        (on_conflict_do_nothing); resume-derived rows with a fresh
        evidence_hash always insert.
        """
        values = {
            "id": item.id,
            "candidate_id": item.candidate_id,
            "kind": item.kind.value,
            "name": item.name,
            "description": item.description,
            "repository": item.repository,
            "commit_sha": item.commit_sha,
            "path": item.path,
            "symbol": item.symbol,
            "content_hash": item.content_hash,
            "source_url": item.source_url,
            "verified": item.verified,
            "extractor_version": item.extractor_version,
            "source_span": item.source_span,
            "confirmation_status": item.confirmation_status.value,
            "evidence_hash": item.evidence_hash,
            "resume_version_id": item.resume_version_id,
        }
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(evidence_items)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        evidence_items.c.candidate_id,
                        evidence_items.c.repository,
                        evidence_items.c.commit_sha,
                        evidence_items.c.path,
                        evidence_items.c.symbol,
                        evidence_items.c.content_hash,
                    ]
                )
            )
        return self.get_by_id(item.candidate_id, item.id)

    def confirm(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        *,
        now: datetime | None = None,
    ) -> EvidenceItem:
        """Mark an evidence item CONFIRMED (scoped by candidate ownership).

        Idempotent: confirming an already-confirmed item is a no-op. The
        append-only invariant is preserved because the evidence row is never
        deleted — only its confirmation_status advances.
        """
        return self._set_confirmation(candidate_id, evidence_id, ConfirmationStatus.CONFIRMED)

    def reject(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        *,
        now: datetime | None = None,
    ) -> EvidenceItem:
        """Mark an evidence item REJECTED (scoped by candidate ownership)."""
        return self._set_confirmation(candidate_id, evidence_id, ConfirmationStatus.REJECTED)

    def _set_confirmation(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        status: ConfirmationStatus,
    ) -> EvidenceItem:
        # Verify ownership first so a client-supplied evidence_id for another
        # candidate raises NotFoundError instead of silently returning that
        # the row was unchanged.
        existing = self.get_by_id(candidate_id, evidence_id)
        if existing.confirmation_status is status:
            return existing
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(evidence_items)
                .where(
                    sa.and_(
                        evidence_items.c.id == evidence_id,
                        evidence_items.c.candidate_id == candidate_id,
                    )
                )
                .values(confirmation_status=status.value)
            )
            if result.rowcount == 0:
                raise NotFoundError("evidence item not found for candidate")
        return self.get_by_id(candidate_id, evidence_id)
