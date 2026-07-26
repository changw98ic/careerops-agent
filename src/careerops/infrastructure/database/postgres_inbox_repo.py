"""PostgreSQL-backed inbox repository (Section 6).

Persists filter decisions and requirement match results. Reads the job
projection (canonical_jobs + job_postings + job_posting_versions with
provenance) joined to the active profile and crawl-plan provenance to build
the inbox projection.

Iron rules honored:
- Server-side candidate ownership (Iron Rule 2): every read/write scoped by
  server-resolved ``candidate_id``.
- Idempotent (Iron Rule 3): upsert on the unique constraint
  ``(canonical_job_id, candidate_id, profile_version_id)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.domain.inbox import (
    BlockingReason,
    FilterDecision,
    FilterVerdict,
    InboxItem,
    InboxProvenance,
    RequirementMatchResult,
    SemanticRankingStatus,
)
from careerops.infrastructure.database.schema import (
    applications,
    canonical_jobs,
    companies,
    crawl_plan_versions,
    crawl_runs,
    filter_decisions,
    inbox_snoozes,
    job_posting_assignments,
    job_posting_versions,
    job_postings,
    job_sources,
    profile_versions,
    requirement_match_results,
)

__all__ = ["PostgresInboxRepository"]


def _uuid(val: Any) -> UUID:
    if isinstance(val, UUID):
        return val
    return UUID(str(val))


class PostgresInboxRepository:
    """PostgreSQL-backed inbox read/write store.

    All reads and writes go through the ``careerops`` schema tables.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- writes -------------------------------------------------------------

    def upsert_filter_decision(
        self,
        candidate_id: UUID,
        decision: FilterDecision,
        *,
        now: datetime | None = None,
    ) -> UUID:
        """Persist or update a filter decision. Returns the decision id.

        Idempotent on (canonical_job_id, candidate_id, profile_version_id).
        """
        values: dict[str, Any] = {
            "id": decision.profile_version_id,  # deterministic id from profile+job
            "canonical_job_id": decision.evidence_refs.get("_canonical_job_id", ""),
            "candidate_id": candidate_id,
            "profile_version_id": decision.profile_version_id,
            "verdict": decision.verdict.value,
            "rules_version": decision.rules_version,
            "blocking_reasons": [r.value for r in decision.blocking_reasons],
            "evidence_refs": {
                k: v for k, v in decision.evidence_refs.items() if not k.startswith("_")
            },
        }
        # Generate a deterministic id from (canonical_job_id, candidate_id, profile_version_id)
        import hashlib

        cjid = str(decision.evidence_refs.get("_canonical_job_id", ""))
        key = f"{cjid}:{candidate_id}:{decision.profile_version_id}"
        det_id = UUID(hashlib.sha256(key.encode()).hexdigest()[:32])
        values["id"] = det_id

        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(filter_decisions)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[
                        filter_decisions.c.canonical_job_id,
                        filter_decisions.c.candidate_id,
                        filter_decisions.c.profile_version_id,
                    ],
                    set_={
                        "verdict": values["verdict"],
                        "rules_version": values["rules_version"],
                        "blocking_reasons": values["blocking_reasons"],
                        "evidence_refs": values["evidence_refs"],
                        "created_at": sa.func.now(),
                    },
                )
            )
        return det_id

    def save_requirement_matches(
        self,
        filter_decision_id: UUID,
        matches: tuple[RequirementMatchResult, ...],
    ) -> None:
        """Persist requirement match results for a filter decision.

        Deletes prior results for this decision and inserts fresh ones
        (idempotent replacement).
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(requirement_match_results).where(
                    requirement_match_results.c.filter_decision_id == filter_decision_id
                )
            )
            for m in matches:
                conn.execute(
                    requirement_match_results.insert().values(
                        id=UUID(int=0),  # will be replaced by DB default
                        filter_decision_id=filter_decision_id,
                        requirement_name=m.requirement_name,
                        match_level=m.match_level,
                        evidence_ids=[str(eid) for eid in m.evidence_ids],
                        confidence=m.confidence,
                        reason=m.reason,
                        rules_version=m.rules_version,
                        model_version=m.model_version,
                    )
                )

    # -- snooze -------------------------------------------------------------

    def upsert_snooze(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        snoozed_until: datetime,
    ) -> None:
        """Create or update a snooze record.  Idempotent on
        ``(candidate_id, canonical_job_id)``.

        Snooze is NOT an application state change -- it is a separate user
        decision that hides the job from the inbox until ``snoozed_until``
        passes.  Recording a snooze never deletes source history.
        """
        import uuid as _uuid

        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(inbox_snoozes)
                .values(
                    id=_uuid.uuid4(),
                    candidate_id=candidate_id,
                    canonical_job_id=canonical_job_id,
                    snoozed_until=snoozed_until,
                )
                .on_conflict_do_update(
                    index_elements=[
                        inbox_snoozes.c.candidate_id,
                        inbox_snoozes.c.canonical_job_id,
                    ],
                    set_={"snoozed_until": snoozed_until},
                )
            )

    def clear_snooze(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
    ) -> None:
        """Remove a snooze record (e.g. when the user acts on the job)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.delete(inbox_snoozes).where(
                    sa.and_(
                        inbox_snoozes.c.candidate_id == candidate_id,
                        inbox_snoozes.c.canonical_job_id == canonical_job_id,
                    )
                )
            )

    def get_snoozed_job_ids(
        self,
        candidate_id: UUID,
        now: datetime,
    ) -> set[UUID]:
        """Return the set of canonical_job_ids currently snoozed for this
        candidate (``snoozed_until > now``)."""
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.select(inbox_snoozes.c.canonical_job_id).where(
                        sa.and_(
                            inbox_snoozes.c.candidate_id == candidate_id,
                            inbox_snoozes.c.snoozed_until > now,
                        )
                    )
                )
                .scalars()
                .fetchall()
            )
        return set(rows)

    def get_snooze(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
    ) -> datetime | None:
        """Return the ``snoozed_until`` timestamp for a job, or ``None``."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(inbox_snoozes.c.snoozed_until).where(
                        sa.and_(
                            inbox_snoozes.c.candidate_id == candidate_id,
                            inbox_snoozes.c.canonical_job_id == canonical_job_id,
                        )
                    )
                )
                .scalar()
            )
        return row  # type: ignore[return-value]

    # -- reads (inbox projection) -------------------------------------------

    def get_inbox_items(
        self,
        candidate_id: UUID,
        *,
        tab: str = "recommended",
        cursor: str | None = None,
        limit: int = 50,
        q: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Build the inbox projection for the candidate.

        Returns items with filter decisions, provenance, and application state.
        ``tab`` is ``recommended`` (verdict=recommended), ``excluded``
        (verdict=excluded), or ``all`` (no verdict filter).

        Items whose ``(candidate_id, canonical_job_id)`` has an active snooze
        (``snoozed_until > now``) are excluded from all tabs.  Use the
        dedicated snooze query to list snoozed items separately.
        """
        effective_now = now or datetime.now(tz=UTC)

        # Build base filter conditions
        base_conditions = [
            filter_decisions.c.candidate_id == candidate_id,
        ]

        with self._engine.begin() as conn:
            # Get the active profile version id for this candidate
            active_profile = conn.execute(
                sa.select(profile_versions.c.id).where(
                    sa.and_(
                        profile_versions.c.candidate_id == candidate_id,
                        profile_versions.c.is_active.is_(True),
                    )
                )
            ).scalar()

            if active_profile is None:
                return {"items": [], "total": 0, "next_cursor": None, "has_active_profile": False}

            base_conditions.append(filter_decisions.c.profile_version_id == active_profile)

            # Tab filter: "all" has no verdict restriction
            if tab in ("recommended", "excluded"):
                verdict_filter = FilterVerdict.RECOMMENDED if tab == "recommended" else FilterVerdict.EXCLUDED
                base_conditions.append(filter_decisions.c.verdict == verdict_filter.value)

            # Exclude snoozed items
            snoozed_subq = (
                sa.select(inbox_snoozes.c.canonical_job_id)
                .where(
                    sa.and_(
                        inbox_snoozes.c.candidate_id == candidate_id,
                        inbox_snoozes.c.snoozed_until > effective_now,
                    )
                )
                .scalar_subquery()
            )

            # Count total
            count_stmt = (
                sa.select(sa.func.count())
                .select_from(filter_decisions)
                .where(
                    sa.and_(
                        *base_conditions,
                        filter_decisions.c.canonical_job_id.notin_(snoozed_subq),
                    )
                )
            )
            total = conn.execute(count_stmt).scalar_one()

            # Main query: filter_decisions joined with canonical_jobs + companies
            stmt = (
                sa.select(
                    filter_decisions,
                    canonical_jobs,
                    companies.c.name.label("company_name"),
                )
                .join(canonical_jobs, canonical_jobs.c.id == filter_decisions.c.canonical_job_id)
                .join(companies, companies.c.id == canonical_jobs.c.company_id)
                .where(
                    sa.and_(
                        *base_conditions,
                        filter_decisions.c.canonical_job_id.notin_(snoozed_subq),
                    )
                )
                .order_by(canonical_jobs.c.created_at.desc(), canonical_jobs.c.id.desc())
                .limit(limit + 1)
            )

            if cursor is not None:
                cur_created, cur_id = _decode_cursor(cursor)
                stmt = stmt.where(
                    sa.or_(
                        canonical_jobs.c.created_at < cur_created,
                        sa.and_(
                            canonical_jobs.c.created_at == cur_created,
                            canonical_jobs.c.id < cur_id,
                        ),
                    )
                )

            if q and q.strip():
                pattern = f"%{q.strip()}%"
                stmt = stmt.where(canonical_jobs.c.canonical_title.ilike(pattern))

            rows = conn.execute(stmt).mappings().fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more else None
        )

        # Batch-fetch postings, versions, provenance, and application state
        job_ids = [row["id"] for row in rows]
        items: list[dict[str, object]] = []

        if job_ids:
            with self._engine.begin() as conn2:
                # Fetch latest posting + version + provenance per canonical job
                posting_data = self._fetch_posting_data(conn2, job_ids)
                # Fetch application state per canonical job for this candidate
                app_states = self._fetch_application_states(conn2, candidate_id, job_ids)
                # Fetch requirement match results for these filter decisions
                decision_ids = [row[filter_decisions.c.id] for row in rows]
                req_matches = self._fetch_requirement_matches(conn2, decision_ids)

        for row in rows:
            cjid = row["id"]
            pd = posting_data.get(cjid, {})  # pyright: ignore[reportPossiblyUnboundVariable]
            app = app_states.get(cjid, {})  # pyright: ignore[reportPossiblyUnboundVariable]
            rmatches = req_matches.get(row[filter_decisions.c.id], [])  # pyright: ignore[reportPossiblyUnboundVariable]

            blocking = [
                BlockingReason(r) for r in (row["blocking_reasons"] or []) if _is_valid_blocking_reason(r)
            ]

            item: dict[str, object] = {
                "canonical_job_id": str(cjid),
                "canonical_title": row["canonical_title"],
                "company_id": str(row["company_id"]),
                "company_name": row.get("company_name", ""),
                "aggregate_state": row["aggregate_state"],
                "posting_id": pd.get("posting_id"),
                "version_id": pd.get("version_id"),
                "structured_data": pd.get("structured_data", {}),
                "apply_url": pd.get("apply_url", ""),
                "provenance": pd.get("provenance", {}),
                "filter_decision": {
                    "verdict": row["verdict"],
                    "profile_version_id": str(row["profile_version_id"]),
                    "rules_version": row["rules_version"],
                    "blocking_reasons": [r.value for r in blocking],
                    "evidence_refs": row["evidence_refs"] or {},
                },
                "requirement_matches": rmatches,
                "application_state": app.get("state"),
                "application_id": app.get("application_id"),
                "first_seen_at": pd.get("first_seen_at"),
                "last_seen_at": pd.get("last_seen_at"),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            items.append(item)

        return {
            "items": items,
            "total": total,
            "next_cursor": next_cursor,
            "has_active_profile": True,
        }

    def _fetch_posting_data(
        self, conn: sa.Connection, job_ids: list[UUID]
    ) -> dict[UUID, dict[str, object]]:
        """Fetch latest posting + version + provenance for each canonical job."""
        # Get postings via assignments
        posting_rows = (
            conn.execute(
                sa.select(
                    job_posting_assignments.c.canonical_job_id,
                    job_postings.c.id.label("posting_id"),
                    job_postings.c.source_id,
                    job_postings.c.source_state,
                    job_postings.c.first_seen_at,
                    job_postings.c.last_seen_at,
                    job_sources.c.source_type,
                    job_sources.c.source_identifier,
                )
                .join(job_postings, job_postings.c.id == job_posting_assignments.c.job_posting_id)
                .join(job_sources, job_sources.c.id == job_postings.c.source_id)
                .where(job_posting_assignments.c.canonical_job_id.in_(job_ids))
            )
            .mappings()
            .fetchall()
        )

        posting_ids = [r["posting_id"] for r in posting_rows]
        version_data: dict[UUID, dict[str, Any]] = {}
        if posting_ids:
            # Latest version per posting (with provenance)
            version_rows = (
                conn.execute(
                    sa.select(job_posting_versions)
                    .where(job_posting_versions.c.job_posting_id.in_(posting_ids))
                    .order_by(
                        job_posting_versions.c.job_posting_id,
                        job_posting_versions.c.captured_at.desc(),
                    )
                )
                .mappings()
                .fetchall()
            )
            seen: set[UUID] = set()
            for vr in version_rows:
                pid = vr["job_posting_id"]
                if pid in seen:
                    continue
                seen.add(pid)
                sd = vr["structured_data"] or {}
                version_data[pid] = {
                    "version_id": str(vr["id"]),
                    "structured_data": sd,
                    "apply_url": sd.get("apply_url", ""),
                    "crawl_run_id": str(vr["crawl_run_id"]) if vr["crawl_run_id"] else None,
                    "plan_version_id": str(vr["plan_version_id"]) if vr["plan_version_id"] else None,
                    "captured_at": vr["captured_at"].isoformat() if vr["captured_at"] else None,
                }

        # Group by canonical job
        result: dict[UUID, dict[str, object]] = {}
        for pr in posting_rows:
            cjid = pr["canonical_job_id"]
            if cjid in result:
                continue  # take first posting per canonical job
            vd = version_data.get(pr["posting_id"], {})
            result[cjid] = {
                "posting_id": str(pr["posting_id"]),
                "version_id": vd.get("version_id"),
                "structured_data": vd.get("structured_data", {}),
                "apply_url": vd.get("apply_url", ""),
                "provenance": {
                    "crawl_run_id": vd.get("crawl_run_id"),
                    "plan_version_id": vd.get("plan_version_id"),
                    "source_type": pr["source_type"],
                    "source_identifier": pr["source_identifier"],
                    "captured_at": vd.get("captured_at"),
                },
                "first_seen_at": pr["first_seen_at"].isoformat() if pr["first_seen_at"] else None,
                "last_seen_at": pr["last_seen_at"].isoformat() if pr["last_seen_at"] else None,
            }
        return result

    def _fetch_application_states(
        self, conn: sa.Connection, candidate_id: UUID, job_ids: list[UUID]
    ) -> dict[UUID, dict[str, object]]:
        """Fetch application state for the candidate + canonical jobs."""
        rows = (
            conn.execute(
                sa.select(
                    applications.c.canonical_job_id,
                    applications.c.id.label("application_id"),
                    applications.c.state,
                ).where(
                    sa.and_(
                        applications.c.candidate_id == candidate_id,
                        applications.c.canonical_job_id.in_(job_ids),
                    )
                )
            )
            .mappings()
            .fetchall()
        )
        return {
            row["canonical_job_id"]: {
                "application_id": str(row["application_id"]),
                "state": row["state"],
            }
            for row in rows
        }

    def _fetch_requirement_matches(
        self, conn: sa.Connection, decision_ids: list[Any]
    ) -> dict[Any, list[dict[str, object]]]:
        """Fetch requirement match results grouped by filter_decision_id."""
        if not decision_ids:
            return {}
        rows = (
            conn.execute(
                sa.select(requirement_match_results).where(
                    requirement_match_results.c.filter_decision_id.in_(decision_ids)
                )
            )
            .mappings()
            .fetchall()
        )
        result: dict[Any, list[dict[str, object]]] = {}
        for row in rows:
            did = row["filter_decision_id"]
            result.setdefault(did, []).append(
                {
                    "requirement_name": row["requirement_name"],
                    "match_level": row["match_level"],
                    "evidence_ids": row["evidence_ids"] or [],
                    "confidence": float(row["confidence"]),
                    "reason": row["reason"],
                    "rules_version": row["rules_version"],
                    "model_version": row["model_version"],
                }
            )
        return result

    def get_filter_decision(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        profile_version_id: UUID,
    ) -> FilterDecision | None:
        """Retrieve an existing filter decision."""
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.select(filter_decisions).where(
                        sa.and_(
                            filter_decisions.c.candidate_id == candidate_id,
                            filter_decisions.c.canonical_job_id == canonical_job_id,
                            filter_decisions.c.profile_version_id == profile_version_id,
                        )
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None
        return FilterDecision(
            verdict=FilterVerdict(row["verdict"]),
            profile_version_id=row["profile_version_id"],
            rules_version=row["rules_version"],
            blocking_reasons=tuple(
                BlockingReason(r)
                for r in (row["blocking_reasons"] or [])
                if _is_valid_blocking_reason(r)
            ),
            evidence_refs=row["evidence_refs"] or {},
        )

    def get_excluded_reasons(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
    ) -> dict[str, object] | None:
        """Return blocking reasons with evidence refs for an excluded job.

        Returns ``None`` when no filter decision exists or the job is not
        excluded for the candidate's active profile.
        """
        with self._engine.begin() as conn:
            active_profile = conn.execute(
                sa.select(profile_versions.c.id).where(
                    sa.and_(
                        profile_versions.c.candidate_id == candidate_id,
                        profile_versions.c.is_active.is_(True),
                    )
                )
            ).scalar()
            if active_profile is None:
                return None

            row = (
                conn.execute(
                    sa.select(filter_decisions).where(
                        sa.and_(
                            filter_decisions.c.candidate_id == candidate_id,
                            filter_decisions.c.canonical_job_id == canonical_job_id,
                            filter_decisions.c.profile_version_id == active_profile,
                        )
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            return None

        blocking = [
            r
            for r in (row["blocking_reasons"] or [])
            if _is_valid_blocking_reason(r)
        ]
        return {
            "verdict": row["verdict"],
            "blocking_reasons": blocking,
            "evidence_refs": row["evidence_refs"] or {},
            "rules_version": row["rules_version"],
            "profile_version_id": str(row["profile_version_id"]),
        }

    def get_job_detail_with_evidence(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
    ) -> dict[str, object] | None:
        """Build a full detail projection for a single canonical job.

        Includes posting data, provenance, filter decision, requirement
        matches, application state, and snooze state.  Returns ``None`` when
        the canonical job does not exist.
        """
        with self._engine.begin() as conn:
            job_row = (
                conn.execute(
                    sa.select(
                        canonical_jobs,
                        companies.c.name.label("company_name"),
                    )
                    .join(companies, companies.c.id == canonical_jobs.c.company_id)
                    .where(canonical_jobs.c.id == canonical_job_id)
                )
                .mappings()
                .fetchone()
            )
        if job_row is None:
            return None

        # Posting + provenance
        posting_data: dict[str, object] = {}
        with self._engine.begin() as conn:
            pd = self._fetch_posting_data(conn, [canonical_job_id])
            posting_data = pd.get(canonical_job_id, {})

        # Filter decision
        active_profile: UUID | None = None
        decision_data: dict[str, object] | None = None
        with self._engine.begin() as conn:
            active_profile = conn.execute(
                sa.select(profile_versions.c.id).where(
                    sa.and_(
                        profile_versions.c.candidate_id == candidate_id,
                        profile_versions.c.is_active.is_(True),
                    )
                )
            ).scalar()
        if active_profile is not None:
            decision = self.get_filter_decision(candidate_id, canonical_job_id, active_profile)
            if decision is not None:
                decision_data = {
                    "verdict": decision.verdict.value,
                    "blocking_reasons": [r.value for r in decision.blocking_reasons],
                    "evidence_refs": decision.evidence_refs,
                    "rules_version": decision.rules_version,
                    "profile_version_id": str(decision.profile_version_id),
                }

        # Requirement matches
        req_matches: list[dict[str, object]] = []
        if decision_data and active_profile:
            with self._engine.begin() as conn:
                det_id_row = conn.execute(
                    sa.select(filter_decisions.c.id).where(
                        sa.and_(
                            filter_decisions.c.candidate_id == candidate_id,
                            filter_decisions.c.canonical_job_id == canonical_job_id,
                            filter_decisions.c.profile_version_id == active_profile,
                        )
                    )
                ).scalar()
                if det_id_row is not None:
                    req_matches = self._fetch_requirement_matches(conn, [det_id_row]).get(det_id_row, [])

        # Application state
        app_data: dict[str, object] = {}
        with self._engine.begin() as conn:
            app_data = self._fetch_application_states(conn, candidate_id, [canonical_job_id]).get(canonical_job_id, {})

        # Snooze state
        snoozed_until = self.get_snooze(candidate_id, canonical_job_id)

        return {
            "canonical_job_id": str(canonical_job_id),
            "canonical_title": job_row["canonical_title"],
            "company_id": str(job_row["company_id"]),
            "company_name": job_row.get("company_name", ""),
            "aggregate_state": job_row["aggregate_state"],
            "posting_id": posting_data.get("posting_id"),
            "version_id": posting_data.get("version_id"),
            "structured_data": posting_data.get("structured_data", {}),
            "apply_url": posting_data.get("apply_url", ""),
            "provenance": posting_data.get("provenance", {}),
            "filter_decision": decision_data,
            "requirement_matches": req_matches,
            "application_state": app_data.get("state"),
            "application_id": app_data.get("application_id"),
            "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
            "first_seen_at": posting_data.get("first_seen_at"),
            "last_seen_at": posting_data.get("last_seen_at"),
            "created_at": job_row["created_at"].isoformat() if job_row["created_at"] else None,
        }


def _is_valid_blocking_reason(value: str) -> bool:
    try:
        BlockingReason(value)
        return True
    except ValueError:
        return False


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    import base64

    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    import base64

    decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = decoded.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), UUID(id_str)
