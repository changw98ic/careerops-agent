from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from careerops.domain.candidates import Candidate


class CandidateService:
    def __init__(self, repository):
        self._repo = repository

    def list_all(self, *, limit: int = 50) -> list[Candidate]:
        return self._repo.list_all(limit=limit)

    def create(self, display_name: str) -> Candidate:
        c = Candidate(
            id=uuid4(),
            display_name=display_name,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        return self._repo.create(c)

    def get(self, candidate_id):
        return self._repo.get(candidate_id)
