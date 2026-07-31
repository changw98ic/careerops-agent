from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.domain.candidates import Candidate


class CandidateRepository(Protocol):
    """Duck-typed contract for candidate persistence backends."""

    def list_all(self, *, limit: int = 50) -> list[Candidate]: ...

    def create(self, candidate: Candidate) -> Candidate: ...

    def get(self, candidate_id: UUID) -> Candidate | None: ...


class CandidateService:
    def __init__(self, repository: CandidateRepository):
        self._repo = repository

    def list_all(self, *, limit: int = 50) -> list[Candidate]:
        return self._repo.list_all(limit=limit)

    def create(self, display_name: str) -> Candidate:
        now = datetime.now(UTC)
        c = Candidate(
            id=uuid4(),
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        return self._repo.create(c)

    def get(self, candidate_id: UUID) -> Candidate | None:
        return self._repo.get(candidate_id)
