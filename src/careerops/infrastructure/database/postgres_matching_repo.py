"""PostgreSQL-backed implementation of the matching read repository.

Implements the same interface as ``InMemoryMatchingReadRepository`` using the
careerops schema tables: candidates, evidence_items, match_results,
compensation_records, canonical_jobs, and job_posting_versions.

Tables that do not exist in the schema (remote_eligibility) return empty/None
results.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine

from careerops.domain.candidates import (
    CompensationRecord,
    EvidenceItem,
    EvidenceKind,
    MatchLevel,
    MatchResult,
    MatchTier,
    RemoteEligibility,
    RemoteEligibilityVerdict,
    RequirementMatch,
)
from careerops.infrastructure.database.schema import (
    candidates,
    canonical_jobs,
    companies,
    compensation_records,
    evidence_items,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
    match_results,
)


def _row_to_evidence_item(row: dict[str, object]) -> EvidenceItem:
    """Convert a DB row dict to an EvidenceItem domain object."""
    return EvidenceItem(
        id=cast("UUID", row["id"]),
        candidate_id=cast("UUID", row["candidate_id"]),
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
        created_at=row.get("created_at"),  # type: ignore[arg-type]
    )


def _row_to_match_result(row: dict[str, object]) -> MatchResult:
    """Convert a DB row dict to a MatchResult domain object."""
    raw_matches = row.get("requirement_matches", [])
    requirement_matches: list[RequirementMatch] = []
    if isinstance(raw_matches, list):
        typed_matches: list[Any] = cast("list[Any]", raw_matches)
        for m in typed_matches:
            if isinstance(m, dict):
                md: dict[str, Any] = cast("dict[str, Any]", m)
                requirement_matches.append(
                    RequirementMatch(
                        requirement_name=str(md.get("requirement_name", "")),
                        level=MatchLevel(str(md.get("level", "unsupported"))),
                        evidence_ids=tuple(UUID(str(eid)) for eid in md.get("evidence_ids", [])),
                        confidence=float(md.get("confidence", 0.0)),
                        reason=str(md.get("reason", "")),
                    )
                )

    return MatchResult(
        id=cast("UUID", row["id"]),
        candidate_id=cast("UUID", row["candidate_id"]),
        canonical_job_id=cast("UUID", row["canonical_job_id"]),
        tier=MatchTier(str(row["tier"])),
        overall_score=float(cast("Any", row["overall_score"])),
        requirement_matches=tuple(requirement_matches),
        geographic_blocked=bool(row.get("geographic_blocked", False)),
        remote_verdict=RemoteEligibilityVerdict(str(row.get("remote_verdict", "unknown"))),
        rules_version=str(row.get("rules_version", "")),
        input_hash=str(row.get("input_hash", "")),
        output_hash=str(row.get("output_hash", "")),
        created_at=row.get("created_at"),  # type: ignore[arg-type]
    )


def _evidence_to_dict(item: EvidenceItem) -> dict[str, object]:
    """Convert an EvidenceItem to a plain dict (matching in-memory repo output)."""
    return {
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
        "created_at": item.created_at,
    }


def _match_to_dict(result: MatchResult) -> dict[str, object]:
    """Convert a MatchResult to a plain dict (matching in-memory repo output)."""
    return {
        "id": result.id,
        "candidate_id": result.candidate_id,
        "canonical_job_id": result.canonical_job_id,
        "tier": result.tier.value,
        "overall_score": result.overall_score,
        "requirement_matches": [
            {
                "requirement_name": m.requirement_name,
                "level": m.level.value,
                "evidence_ids": [str(eid) for eid in m.evidence_ids],
                "confidence": m.confidence,
                "reason": m.reason,
            }
            for m in result.requirement_matches
        ],
        "geographic_blocked": result.geographic_blocked,
        "remote_verdict": result.remote_verdict.value,
        "rules_version": result.rules_version,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
        "created_at": result.created_at,
    }


class PostgresMatchingReadRepository:
    """PostgreSQL-backed matching repository.

    Each method opens its own transaction via ``engine.begin()``.
    For schema tables that do not exist (remote_eligibility), methods return
    empty or None results.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- Candidates ---------------------------------------------------------

    def add_candidate(self, candidate_id: UUID, display_name: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                postgresql.insert(candidates)
                .values(id=candidate_id, display_name=display_name)
                .on_conflict_do_update(
                    index_elements=[candidates.c.id],
                    set_={"display_name": display_name, "updated_at": sa.func.now()},
                )
            )

    def list_candidates(
        self, *, limit: int = 50, candidate_id: UUID | None = None
    ) -> list[dict[str, object]]:
        with self._engine.begin() as conn:
            evidence_count = (
                sa.select(
                    evidence_items.c.candidate_id,
                    sa.func.count().label("cnt"),
                )
                .group_by(evidence_items.c.candidate_id)
                .subquery()
            )
            stmt = (
                sa.select(
                    candidates.c.id,
                    candidates.c.display_name,
                    sa.func.coalesce(evidence_count.c.cnt, 0).label("evidence_count"),
                )
                .outerjoin(
                    evidence_count,
                    candidates.c.id == evidence_count.c.candidate_id,
                )
                .order_by(candidates.c.created_at.desc())
                .limit(limit)
            )
            if candidate_id is not None:
                stmt = stmt.where(candidates.c.id == candidate_id)
            rows = conn.execute(stmt).mappings().all()
            return [dict(row) for row in rows]

    # -- Evidence -----------------------------------------------------------

    def add_evidence(self, item: EvidenceItem) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                postgresql.insert(evidence_items)
                .values(
                    id=item.id,
                    candidate_id=item.candidate_id,
                    kind=item.kind.value,
                    name=item.name,
                    description=item.description,
                    repository=item.repository,
                    commit_sha=item.commit_sha,
                    path=item.path,
                    symbol=item.symbol,
                    content_hash=item.content_hash,
                    source_url=item.source_url,
                    verified=item.verified,
                )
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

    def list_evidence(self, candidate_id: str) -> list[dict[str, object]]:
        cid = UUID(candidate_id)
        items = self.find_by_candidate(cid)
        return [_evidence_to_dict(e) for e in items]

    def find_by_candidate(self, candidate_id: UUID) -> list[EvidenceItem]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(evidence_items)
                    .where(evidence_items.c.candidate_id == candidate_id)
                    .order_by(evidence_items.c.created_at)
                )
                .mappings()
                .all()
            )
            return [_row_to_evidence_item(dict(row)) for row in rows]

    # -- Matches ------------------------------------------------------------

    def add_match(self, result: MatchResult) -> None:
        with self._engine.begin() as conn:
            rm_json = [
                {
                    "requirement_name": m.requirement_name,
                    "level": m.level.value,
                    "evidence_ids": [str(eid) for eid in m.evidence_ids],
                    "confidence": m.confidence,
                    "reason": m.reason,
                }
                for m in result.requirement_matches
            ]
            conn.execute(
                postgresql.insert(match_results)
                .values(
                    id=result.id,
                    candidate_id=result.candidate_id,
                    canonical_job_id=result.canonical_job_id,
                    tier=result.tier.value,
                    overall_score=result.overall_score,
                    requirement_matches=rm_json,
                    geographic_blocked=result.geographic_blocked,
                    remote_verdict=result.remote_verdict.value,
                    rules_version=result.rules_version,
                    input_hash=result.input_hash,
                    output_hash=result.output_hash,
                )
                .on_conflict_do_nothing(index_elements=[match_results.c.id])
            )

    def list_matches(
        self,
        *,
        candidate_id: str | None = None,
        canonical_job_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        with self._engine.begin() as conn:
            stmt = sa.select(match_results)
            if candidate_id is not None:
                stmt = stmt.where(match_results.c.candidate_id == UUID(candidate_id))
            if canonical_job_id is not None:
                stmt = stmt.where(match_results.c.canonical_job_id == UUID(canonical_job_id))
            if cursor is not None:
                stmt = stmt.where(match_results.c.id > UUID(cursor))
            stmt = stmt.order_by(match_results.c.id).limit(limit)
            rows = conn.execute(stmt).mappings().all()
            return [_match_to_dict(_row_to_match_result(dict(row))) for row in rows]

    # -- Remote eligibility -------------------------------------------------
    # No remote_eligibility table in schema; return empty/None.

    def set_remote_eligibility(self, eligibility: RemoteEligibility) -> None:
        """No-op: remote_eligibility table does not exist in the schema."""
        return None

    def get_remote_eligibility(self, job_id: str) -> dict[str, object] | None:
        """No-op: remote_eligibility table does not exist in the schema."""
        return None

    # -- Compensation -------------------------------------------------------

    def set_compensation(self, record: CompensationRecord) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                postgresql.insert(compensation_records)
                .values(
                    id=record.id,
                    canonical_job_id=record.canonical_job_id,
                    currency=record.currency,
                    amount_min=record.amount_min,
                    amount_max=record.amount_max,
                    period=record.period,
                    normalized_amount_min=record.normalized_amount_min,
                    normalized_amount_max=record.normalized_amount_max,
                    normalized_currency=record.normalized_currency,
                    score=record.score,
                    source_text=record.source_text,
                )
                .on_conflict_do_update(
                    index_elements=[compensation_records.c.canonical_job_id],
                    set_={
                        "currency": record.currency,
                        "amount_min": record.amount_min,
                        "amount_max": record.amount_max,
                        "period": record.period,
                        "normalized_amount_min": record.normalized_amount_min,
                        "normalized_amount_max": record.normalized_amount_max,
                        "normalized_currency": record.normalized_currency,
                        "score": record.score,
                        "source_text": record.source_text,
                    },
                )
            )

    def get_compensation(self, job_id: str) -> dict[str, object] | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(compensation_records).where(
                        compensation_records.c.canonical_job_id == UUID(job_id)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return {
                "id": row["id"],
                "canonical_job_id": row["canonical_job_id"],
                "currency": row["currency"],
                "amount_min": row["amount_min"],
                "amount_max": row["amount_max"],
                "period": row["period"],
                "normalized_amount_min": row["normalized_amount_min"],
                "normalized_amount_max": row["normalized_amount_max"],
                "normalized_currency": row["normalized_currency"],
                "score": row["score"],
            }

    # -- MatchDataRepository protocol (for MatchOrchestrator) ---------------

    def get_job_structured_data(self, canonical_job_id: UUID) -> dict[str, object] | None:
        """Return structured data for a canonical job.

        Joins canonical_jobs -> job_posting_assignments -> job_posting_versions
        to retrieve the latest structured_data blob.
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(job_posting_versions.c.structured_data)
                    .select_from(
                        canonical_jobs.join(
                            job_posting_assignments,
                            canonical_jobs.c.id == job_posting_assignments.c.canonical_job_id,
                        ).join(
                            job_posting_versions,
                            job_posting_assignments.c.job_posting_id
                            == job_posting_versions.c.job_posting_id,
                        )
                    )
                    .where(canonical_jobs.c.id == canonical_job_id)
                    .order_by(job_posting_versions.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            structured: Any = row["structured_data"]
            if isinstance(structured, dict):
                return cast("dict[str, object]", structured)
            return None

    def get_candidate_evidence(self, candidate_id: UUID) -> list[EvidenceItem]:
        return self.find_by_candidate(candidate_id)

    def get_job_projection_data(self, canonical_job_id: UUID) -> dict[str, Any] | None:
        """Return the latest canonical job data required by inbox projection."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(
                        canonical_jobs.c.canonical_title,
                        canonical_jobs.c.aggregate_state,
                        companies.c.name.label("company_name"),
                        job_posting_versions.c.structured_data,
                    )
                    .select_from(
                        canonical_jobs.join(
                            companies,
                            companies.c.id == canonical_jobs.c.company_id,
                        )
                        .join(
                            job_posting_assignments,
                            canonical_jobs.c.id
                            == job_posting_assignments.c.canonical_job_id,
                        )
                        .join(
                            job_posting_versions,
                            job_posting_assignments.c.job_posting_id
                            == job_posting_versions.c.job_posting_id,
                        )
                    )
                    .where(canonical_jobs.c.id == canonical_job_id)
                    .order_by(job_posting_versions.c.captured_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            source_states = set(
                conn.execute(
                    sa.select(job_postings.c.source_state)
                    .join(
                        job_posting_assignments,
                        job_posting_assignments.c.job_posting_id == job_postings.c.id,
                    )
                    .where(
                        job_posting_assignments.c.canonical_job_id == canonical_job_id
                    )
                ).scalars()
            )

        raw_structured = row["structured_data"]
        structured = (
            cast("dict[str, object]", raw_structured)
            if isinstance(raw_structured, dict)
            else {}
        )
        return {
            "title": structured.get("title") or row["canonical_title"],
            "location": structured.get("location", ""),
            "text": structured.get("description_text")
            or structured.get("description", ""),
            "company_name": row["company_name"],
            "aggregate_state": row["aggregate_state"],
            "source_states": source_states,
            "compensation": self.get_compensation(str(canonical_job_id)),
        }

    # -- EvidenceRepository protocol (for EvidenceImportService) ------------

    def find_by_idempotency_key(
        self,
        candidate_id: UUID,
        repository: str,
        commit_sha: str,
        path: str,
        symbol: str,
        content_hash: str,
    ) -> EvidenceItem | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(evidence_items).where(
                        evidence_items.c.candidate_id == candidate_id,
                        evidence_items.c.repository == repository,
                        evidence_items.c.commit_sha == commit_sha,
                        evidence_items.c.path == path,
                        evidence_items.c.symbol == symbol,
                        evidence_items.c.content_hash == content_hash,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return _row_to_evidence_item(dict(row))

    def save(self, item: EvidenceItem) -> None:
        self.add_evidence(item)
