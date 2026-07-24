"""PostgreSQL-backed ``JobRepository`` for job ingestion and API routes.

Implements the same interface as ``InMemoryJobReadRepository`` using the
``careerops`` schema tables (``companies``, ``canonical_jobs``,
``job_postings``, ``job_posting_versions``, ``job_merge_decisions``,
``job_posting_assignments``) defined in ``infrastructure.database.schema``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from careerops.domain.jobs import (
    AggregateState,
    CanonicalJob,
    JobMergeDecision,
    JobPosting,
    JobPostingVersion,
    PostingSourceState,
)
from careerops.infrastructure.database.schema import (
    canonical_jobs,
    companies,
    job_merge_decisions,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
)


def _uuid(val: Any) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val))


def _row_to_posting(row: Any) -> JobPosting:
    return JobPosting(
        id=_uuid(row["id"]),
        source_id=_uuid(row["source_id"]),
        external_id=row["external_id"],
        canonical_url=row["canonical_url"],
        source_state=PostingSourceState(row["source_state"]),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        closed_at=row["closed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_canonical(row: Any) -> CanonicalJob:
    return CanonicalJob(
        id=_uuid(row["id"]),
        company_id=_uuid(row["company_id"]),
        canonical_title=row["canonical_title"],
        normalized_title=row["normalized_title"],
        aggregate_state=AggregateState(row["aggregate_state"]),
        primary_posting_id=_uuid(row["primary_posting_id"]) if row["primary_posting_id"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_version(row: Any) -> JobPostingVersion:
    return JobPostingVersion(
        id=_uuid(row["id"]),
        job_posting_id=_uuid(row["job_posting_id"]),
        raw_snapshot_id=_uuid(row["raw_snapshot_id"]) if row["raw_snapshot_id"] else None,
        content_hash=row["content_hash"],
        source_url=row["source_url"],
        parser_version=row["parser_version"],
        structured_data=row["structured_data"] or {},
        changed_fields=tuple(row["changed_fields"]) if row["changed_fields"] else (),
        captured_at=row["captured_at"],
        created_at=row["created_at"],
    )


class PostgresJobReadRepository:
    """SQLAlchemy-backed store implementing the ``JobRepository`` protocol.

    All reads and writes go through the ``careerops`` schema tables.  The
    store is **not** thread-safe at the Python level; concurrency control is
    delegated to PostgreSQL unique constraints and row-level locks.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- Companies (read/write) --------------------------------------------

    def add_company(
        self,
        company_id: UUID,
        name: str,
        normalized_name: str,
        official_domains: list[str] | None = None,
        terms_status: str = "unknown",
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(companies)
                .values(
                    id=company_id,
                    name=name,
                    normalized_name=normalized_name,
                    official_domains=official_domains or [],
                    terms_status=terms_status,
                )
                .on_conflict_do_update(
                    index_elements=[companies.c.normalized_name],
                    set_={
                        "name": name,
                        "official_domains": official_domains or [],
                        "terms_status": terms_status,
                        "updated_at": datetime.now(timezone.utc),  # noqa: UP017,
                    },
                )
            )

    def list_companies(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        with self._engine.begin() as conn:
            stmt = select(companies).order_by(companies.c.created_at).limit(limit)
            if cursor is not None:
                stmt = stmt.where(companies.c.id > _uuid(cursor))
            rows = conn.execute(stmt).mappings().fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "normalized_name": row["normalized_name"],
                "official_domains": row["official_domains"],
                "terms_status": row["terms_status"],
            }
            for row in rows
        ]

    # -- Canonical jobs (read) ---------------------------------------------

    def list_canonical_jobs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        state: str | None = None,
    ) -> list[dict[str, object]]:
        with self._engine.begin() as conn:
            stmt = select(canonical_jobs).order_by(canonical_jobs.c.created_at).limit(limit)
            if cursor is not None:
                stmt = stmt.where(canonical_jobs.c.id > _uuid(cursor))
            if state is not None:
                stmt = stmt.where(canonical_jobs.c.aggregate_state == state)
            rows = conn.execute(stmt).mappings().fetchall()
        return [
            {
                "id": row["id"],
                "company_id": row["company_id"],
                "canonical_title": row["canonical_title"],
                "aggregate_state": row["aggregate_state"],
            }
            for row in rows
        ]

    def get_job_detail(self, job_id: str) -> dict[str, object] | None:
        jid = _uuid(job_id)
        with self._engine.begin() as conn:
            # Fetch canonical job.
            job_row = (
                conn.execute(select(canonical_jobs).where(canonical_jobs.c.id == jid))
                .mappings()
                .fetchone()
            )
            if job_row is None:
                return None

            # Fetch associated postings via assignment table.
            posting_rows = (
                conn.execute(
                    select(job_postings)
                    .join(
                        job_posting_assignments,
                        job_posting_assignments.c.job_posting_id == job_postings.c.id,
                    )
                    .where(job_posting_assignments.c.canonical_job_id == jid)
                )
                .mappings()
                .fetchall()
            )

            # Fetch versions for all associated postings.
            posting_ids = [r["id"] for r in posting_rows]
            version_rows: list[Any] = []
            merge_rows: list[Any] = []
            if posting_ids:
                version_rows = list(
                    conn.execute(
                        select(job_posting_versions).where(
                            job_posting_versions.c.job_posting_id.in_(posting_ids)
                        )
                    )
                    .mappings()
                    .fetchall()
                )
                merge_rows = list(
                    conn.execute(
                        select(job_merge_decisions).where(
                            job_merge_decisions.c.job_posting_id.in_(posting_ids)
                        )
                    )
                    .mappings()
                    .fetchall()
                )

        return {
            "id": job_row["id"],
            "company_id": job_row["company_id"],
            "canonical_title": job_row["canonical_title"],
            "aggregate_state": job_row["aggregate_state"],
            "postings": [
                {
                    "id": str(r["id"]),
                    "source_id": str(r["source_id"]),
                    "external_id": r["external_id"],
                    "canonical_url": r["canonical_url"],
                    "source_state": r["source_state"],
                    "first_seen_at": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                    "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                }
                for r in posting_rows
            ],
            "versions": [
                {
                    "id": str(r["id"]),
                    "posting_id": str(r["job_posting_id"]),
                    "content_hash": r["content_hash"],
                    "structured_data": r["structured_data"],
                    "captured_at": r["captured_at"].isoformat() if r["captured_at"] else None,
                }
                for r in version_rows
            ],
            "merge_decisions": [
                {
                    "id": str(r["id"]),
                    "posting_id": str(r["job_posting_id"]),
                    "decision_kind": r["decision_kind"],
                    "rule": r["rule"],
                    "reason": r["reason"],
                }
                for r in merge_rows
            ],
        }

    # -- JobRepository protocol (for JobIngestionService) -------------------

    def find_posting_by_source_and_external_id(
        self, source_id: UUID, external_id: str
    ) -> JobPosting | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(job_postings).where(
                        job_postings.c.source_id == source_id,
                        job_postings.c.external_id == external_id,
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return _row_to_posting(row)

    def find_latest_version(self, posting_id: UUID) -> JobPostingVersion | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(job_posting_versions)
                    .where(job_posting_versions.c.job_posting_id == posting_id)
                    .order_by(job_posting_versions.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return _row_to_version(row)

    def find_canonical_by_fingerprint(self, fingerprint: str) -> CanonicalJob | None:
        """Match by ``normalized_title`` (the fingerprint used during ingestion)."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(canonical_jobs).where(canonical_jobs.c.normalized_title == fingerprint)
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return _row_to_canonical(row)

    def find_canonical_by_id(self, canonical_job_id: UUID) -> CanonicalJob | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(select(canonical_jobs).where(canonical_jobs.c.id == canonical_job_id))
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return _row_to_canonical(row)

    def find_canonical_for_posting(self, posting_id: UUID) -> UUID | None:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(job_posting_assignments.c.canonical_job_id).where(
                        job_posting_assignments.c.job_posting_id == posting_id
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return _uuid(row["canonical_job_id"])

    def find_postings_for_canonical(self, canonical_job_id: UUID) -> list[JobPosting]:
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    select(job_postings)
                    .join(
                        job_posting_assignments,
                        job_posting_assignments.c.job_posting_id == job_postings.c.id,
                    )
                    .where(job_posting_assignments.c.canonical_job_id == canonical_job_id)
                )
                .mappings()
                .fetchall()
            )
        return [_row_to_posting(r) for r in rows]

    def save_posting(self, posting: JobPosting) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(job_postings)
                .values(
                    id=posting.id,
                    source_id=posting.source_id,
                    external_id=posting.external_id,
                    canonical_url=posting.canonical_url,
                    source_state=posting.source_state.value,
                    first_seen_at=posting.first_seen_at,
                    last_seen_at=posting.last_seen_at,
                    closed_at=posting.closed_at,
                )
                .on_conflict_do_update(
                    index_elements=[job_postings.c.source_id, job_postings.c.external_id],
                    set_={
                        "canonical_url": posting.canonical_url,
                        "source_state": posting.source_state.value,
                        "last_seen_at": posting.last_seen_at,
                        "closed_at": posting.closed_at,
                        "updated_at": datetime.now(timezone.utc),  # noqa: UP017,
                    },
                )
            )

    def save_version(self, version: JobPostingVersion) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(job_posting_versions)
                .values(
                    id=version.id,
                    job_posting_id=version.job_posting_id,
                    raw_snapshot_id=version.raw_snapshot_id,
                    content_hash=version.content_hash,
                    source_url=version.source_url,
                    parser_version=version.parser_version,
                    structured_data=version.structured_data,
                    changed_fields=list(version.changed_fields),
                    captured_at=version.captured_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        job_posting_versions.c.job_posting_id,
                        job_posting_versions.c.content_hash,
                    ],
                )
            )

    def save_canonical_job(self, job: CanonicalJob) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(canonical_jobs)
                .values(
                    id=job.id,
                    company_id=job.company_id,
                    canonical_title=job.canonical_title,
                    normalized_title=job.normalized_title,
                    aggregate_state=job.aggregate_state.value,
                    primary_posting_id=job.primary_posting_id,
                )
                .on_conflict_do_update(
                    index_elements=[canonical_jobs.c.id],
                    set_={
                        "canonical_title": job.canonical_title,
                        "normalized_title": job.normalized_title,
                        "aggregate_state": job.aggregate_state.value,
                        "primary_posting_id": job.primary_posting_id,
                        "updated_at": datetime.now(timezone.utc),  # noqa: UP017,
                    },
                )
            )

    def save_merge_decision(self, decision: JobMergeDecision) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                job_merge_decisions.insert().values(
                    id=decision.id,
                    job_posting_id=decision.job_posting_id,
                    from_canonical_job_id=decision.from_canonical_job_id,
                    to_canonical_job_id=decision.to_canonical_job_id,
                    decision_kind=decision.decision_kind.value,
                    rule=decision.rule,
                    reason=decision.reason,
                    score=decision.score,
                    actor_type=decision.actor_type.value,
                    actor_id=decision.actor_id,
                    algorithm_version=decision.algorithm_version,
                    supersedes_decision_id=decision.supersedes_decision_id,
                )
            )

    def save_posting_assignment(
        self, posting_id: UUID, canonical_job_id: UUID, decision_id: UUID
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(job_posting_assignments)
                .values(
                    job_posting_id=posting_id,
                    canonical_job_id=canonical_job_id,
                    decision_id=decision_id,
                )
                .on_conflict_do_update(
                    index_elements=[job_posting_assignments.c.job_posting_id],
                    set_={
                        "canonical_job_id": canonical_job_id,
                        "decision_id": decision_id,
                    },
                )
            )

    def update_posting_state(
        self, posting_id: UUID, state: PostingSourceState, now: datetime
    ) -> None:
        values: dict[str, Any] = {
            "source_state": state.value,
            "updated_at": now,
        }
        if state == PostingSourceState.CLOSED:
            values["closed_at"] = now
        with self._engine.begin() as conn:
            conn.execute(
                job_postings.update().where(job_postings.c.id == posting_id).values(**values)
            )

    def update_canonical_state(
        self, canonical_job_id: UUID, state: AggregateState, now: datetime
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                canonical_jobs.update()
                .where(canonical_jobs.c.id == canonical_job_id)
                .values(aggregate_state=state.value, updated_at=now)
            )

    def update_posting_last_seen(self, posting_id: UUID, now: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                job_postings.update()
                .where(job_postings.c.id == posting_id)
                .values(last_seen_at=now, updated_at=now)
            )
