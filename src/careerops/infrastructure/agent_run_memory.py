"""Thread-safe in-memory Agent-run repository for contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from careerops.domain.agent_runs import (
    AgentCapability,
    AgentRun,
    AgentRunRepository,
    AgentRunReview,
    AgentRunState,
)

__all__ = ["InMemoryAgentRunRepository"]


class InMemoryAgentRunRepository(AgentRunRepository):
    def __init__(self) -> None:
        self._lock = RLock()
        self._runs: dict[UUID, AgentRun] = {}
        self._by_key: dict[tuple[UUID, AgentCapability, str], UUID] = {}
        self._reviews: dict[UUID, list[AgentRunReview]] = {}

    def create(self, run: AgentRun) -> AgentRun:
        with self._lock:
            key = (run.candidate_id, run.capability, run.idempotency_key)
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                return self._runs[existing_id]
            self._runs[run.id] = run
            self._by_key[key] = run.id
            self._reviews.setdefault(run.id, [])
            return run

    def get(self, candidate_id: UUID, run_id: UUID) -> AgentRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run if run is not None and run.candidate_id == candidate_id else None

    def find_by_idempotency(
        self, candidate_id: UUID, capability: AgentCapability, idempotency_key: str
    ) -> AgentRun | None:
        with self._lock:
            run_id = self._by_key.get((candidate_id, capability, idempotency_key))
            return self._runs.get(run_id) if run_id is not None else None

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        capability: AgentCapability | None = None,
        limit: int = 50,
    ) -> list[AgentRun]:
        with self._lock:
            runs = [run for run in self._runs.values() if run.candidate_id == candidate_id]
            if capability is not None:
                runs = [run for run in runs if run.capability is capability]
            runs.sort(key=lambda run: (run.created_at or _MIN, run.id), reverse=True)
            return runs[:limit]

    def update(self, run: AgentRun) -> AgentRun:
        with self._lock:
            existing = self._runs.get(run.id)
            if existing is None or existing.candidate_id != run.candidate_id:
                raise KeyError("agent run not found")
            self._runs[run.id] = run
            return run

    def claim(self, candidate_id: UUID, run_id: UUID, now: datetime) -> AgentRun | None:
        with self._lock:
            existing = self._runs.get(run_id)
            if existing is None or existing.candidate_id != candidate_id:
                return None
            if existing.state is not AgentRunState.PENDING:
                return None
            claimed = replace(existing, state=AgentRunState.RUNNING, started_at=now)
            self._runs[run_id] = claimed
            return claimed

    def add_review(self, review: AgentRunReview) -> AgentRunReview:
        with self._lock:
            run = self._runs.get(review.run_id)
            if run is None or run.candidate_id != review.candidate_id:
                raise KeyError("agent run not found")
            self._reviews.setdefault(review.run_id, []).append(review)
            return review

    def record_review(self, review: AgentRunReview, run: AgentRun) -> AgentRun:
        with self._lock:
            existing = self._runs.get(run.id)
            if existing is None or existing.candidate_id != run.candidate_id:
                raise KeyError("agent run not found")
            self._reviews.setdefault(review.run_id, []).append(review)
            self._runs[run.id] = run
            return run

    def list_reviews(self, candidate_id: UUID, run_id: UUID) -> list[AgentRunReview]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.candidate_id != candidate_id:
                return []
            return list(self._reviews.get(run_id, ()))


_MIN = datetime.min.replace(tzinfo=UTC)
