"""PostgreSQL repository for candidate-scoped Agent runs and reviews."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.domain.agent_runs import (
    AgentCapability,
    AgentReviewDecision,
    AgentRun,
    AgentRunRepository,
    AgentRunReview,
    AgentRunState,
)
from careerops.infrastructure.database.schema import agent_run_reviews, agent_runs

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

__all__ = ["PostgresAgentRunRepository"]


def _run(row: sa.RowMapping) -> AgentRun:
    raw_evidence = row.get("evidence_ids")
    evidence_ids = (
        tuple(UUID(str(value)) for value in raw_evidence if value)
        if isinstance(raw_evidence, list)
        else ()
    )
    raw_identities = row.get("input_identities")
    identities = dict(raw_identities) if isinstance(raw_identities, dict) else {}
    raw_result = row.get("result")
    result = dict(raw_result) if isinstance(raw_result, dict) else {}
    review = row.get("review_decision")
    return AgentRun(
        id=cast(UUID, row["id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        capability=AgentCapability(str(row["capability"])),
        state=AgentRunState(str(row["state"])),
        idempotency_key=str(row["idempotency_key"]),
        input_hash=str(row["input_hash"]),
        input_identities=identities,
        evidence_ids=evidence_ids,
        schema_version=str(row["schema_version"]),
        prompt_version=str(row["prompt_version"]),
        model_id=str(row["model_id"]),
        trace_id=str(row["trace_id"]),
        result=result,
        error_category=str(row["error_category"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        review_decision=AgentReviewDecision(str(review)) if review else None,
        reviewed_by=str(row["reviewed_by"]),
        review_note=str(row["review_note"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        reviewed_at=row["reviewed_at"],
    )


def _review(row: sa.RowMapping) -> AgentRunReview:
    raw = row.get("edited_result")
    return AgentRunReview(
        id=cast(UUID, row["id"]),
        run_id=cast(UUID, row["run_id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        decision=AgentReviewDecision(str(row["decision"])),
        actor_id=str(row["actor_id"]),
        note=str(row["note"]),
        edited_result=dict(raw) if isinstance(raw, dict) else {},
        created_at=row["created_at"],
    )


class PostgresAgentRunRepository(AgentRunRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, run: AgentRun) -> AgentRun:
        values = _run_values(run)
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(agent_runs)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        agent_runs.c.candidate_id,
                        agent_runs.c.capability,
                        agent_runs.c.idempotency_key,
                    ]
                )
            )
        stored = self.get(run.candidate_id, run.id)
        if stored is not None:
            return stored
        winner = self.find_by_idempotency(run.candidate_id, run.capability, run.idempotency_key)
        if winner is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent run was not persisted")
        return winner

    def get(self, candidate_id: UUID, run_id: UUID) -> AgentRun | None:
        stmt = sa.select(agent_runs).where(
            sa.and_(agent_runs.c.id == run_id, agent_runs.c.candidate_id == candidate_id)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _run(row) if row is not None else None

    def find_by_idempotency(
        self, candidate_id: UUID, capability: AgentCapability, idempotency_key: str
    ) -> AgentRun | None:
        stmt = sa.select(agent_runs).where(
            sa.and_(
                agent_runs.c.candidate_id == candidate_id,
                agent_runs.c.capability == capability.value,
                agent_runs.c.idempotency_key == idempotency_key,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _run(row) if row is not None else None

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        capability: AgentCapability | None = None,
        limit: int = 50,
    ) -> list[AgentRun]:
        stmt = sa.select(agent_runs).where(agent_runs.c.candidate_id == candidate_id)
        if capability is not None:
            stmt = stmt.where(agent_runs.c.capability == capability.value)
        stmt = stmt.order_by(agent_runs.c.created_at.desc(), agent_runs.c.id.desc()).limit(limit)
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_run(row) for row in rows]

    def update(self, run: AgentRun) -> AgentRun:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(agent_runs)
                .where(
                    sa.and_(
                        agent_runs.c.id == run.id, agent_runs.c.candidate_id == run.candidate_id
                    )
                )
                .values(**_run_values(run, include_identity=False))
            )
        stored = self.get(run.candidate_id, run.id)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent run disappeared during update")
        return stored

    def claim(self, candidate_id: UUID, run_id: UUID, now: datetime) -> AgentRun | None:
        """Atomically move one pending run to running."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(agent_runs)
                .where(
                    sa.and_(
                        agent_runs.c.id == run_id,
                        agent_runs.c.candidate_id == candidate_id,
                        agent_runs.c.state == AgentRunState.PENDING.value,
                    )
                )
                .values(state=AgentRunState.RUNNING.value, started_at=now)
            )
            if result.rowcount == 0:
                return None
        return self.get(candidate_id, run_id)

    def add_review(self, review: AgentRunReview) -> AgentRunReview:
        with self._engine.begin() as conn:
            conn.execute(
                agent_run_reviews.insert().values(
                    id=review.id,
                    run_id=review.run_id,
                    candidate_id=review.candidate_id,
                    decision=review.decision.value,
                    actor_id=review.actor_id,
                    note=review.note,
                    edited_result=dict(review.edited_result),
                    created_at=review.created_at,
                )
            )
        return review

    def record_review(self, review: AgentRunReview, run: AgentRun) -> AgentRun:
        """Append the review and mark the run reviewed in one transaction."""
        with self._engine.begin() as conn:
            conn.execute(
                agent_run_reviews.insert().values(
                    id=review.id,
                    run_id=review.run_id,
                    candidate_id=review.candidate_id,
                    decision=review.decision.value,
                    actor_id=review.actor_id,
                    note=review.note,
                    edited_result=dict(review.edited_result),
                    created_at=review.created_at,
                )
            )
            result = conn.execute(
                sa.update(agent_runs)
                .where(
                    sa.and_(
                        agent_runs.c.id == run.id,
                        agent_runs.c.candidate_id == run.candidate_id,
                    )
                )
                .values(**_run_values(run, include_identity=False))
            )
            if result.rowcount == 0:  # pragma: no cover - database invariant
                raise RuntimeError("agent run disappeared during review")
        stored = self.get(run.candidate_id, run.id)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent run disappeared during review")
        return stored

    def list_reviews(self, candidate_id: UUID, run_id: UUID) -> list[AgentRunReview]:
        stmt = (
            sa.select(agent_run_reviews)
            .where(
                sa.and_(
                    agent_run_reviews.c.candidate_id == candidate_id,
                    agent_run_reviews.c.run_id == run_id,
                )
            )
            .order_by(agent_run_reviews.c.created_at, agent_run_reviews.c.id)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_review(row) for row in rows]


def _run_values(run: AgentRun, *, include_identity: bool = True) -> dict[str, Any]:
    values: dict[str, Any] = {
        "state": run.state.value,
        "input_hash": run.input_hash,
        "input_identities": dict(run.input_identities),
        "evidence_ids": [str(value) for value in run.evidence_ids],
        "schema_version": run.schema_version,
        "prompt_version": run.prompt_version,
        "model_id": run.model_id,
        "trace_id": run.trace_id,
        "result": dict(run.result),
        "error_category": run.error_category,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "review_decision": run.review_decision.value if run.review_decision else None,
        "reviewed_by": run.reviewed_by,
        "review_note": run.review_note,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "reviewed_at": run.reviewed_at,
    }
    if include_identity:
        values.update(
            {
                "id": run.id,
                "candidate_id": run.candidate_id,
                "capability": run.capability.value,
                "idempotency_key": run.idempotency_key,
                "created_at": run.created_at,
            }
        )
    return values
