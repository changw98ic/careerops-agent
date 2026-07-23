"""Raw document TTL/purge service (M1.13).

Before deleting full content, persists long-term evidence snippet
(URL, fetched_at, content hash, decision snippet). Verifies no
dangling version references remain.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from careerops.domain.crawl import EvidenceSnippet, PurgeResult, PurgeState


class EvidenceRepository(Protocol):
    """Repository for persisting evidence snippets before purge."""

    def persist_evidence_snippet(self, snippet: EvidenceSnippet) -> UUID: ...

    def count_version_references(self, blob_id: UUID) -> int: ...

    def mark_content_purged(self, content_object_id: UUID, blob_id: UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class PurgeCandidate:
    content_object_id: UUID
    blob_id: UUID
    object_key: str
    sha256: str
    source_url: str
    fetched_at: datetime
    decision_snippet: str


def compute_snippet_hash(snippet_text: str) -> str:
    """Compute SHA-256 hash of the evidence snippet text."""
    return hashlib.sha256(snippet_text.encode("utf-8")).hexdigest()


def build_evidence_snippet(candidate: PurgeCandidate) -> EvidenceSnippet:
    """Build an evidence snippet from a purge candidate."""
    snippet_hash = compute_snippet_hash(candidate.decision_snippet)
    return EvidenceSnippet(
        source_url=candidate.source_url,
        fetched_at=candidate.fetched_at,
        content_hash=candidate.sha256,
        decision_snippet=candidate.decision_snippet,
        snippet_hash=snippet_hash,
    )


class RawDocumentPurgeService:
    """Orchestrates safe purge of expired raw documents.

    Safety invariants:
    1. Evidence snippet is persisted BEFORE content deletion.
    2. Dangling version references are checked before purge.
    3. If dangling references exist, purge is blocked.
    """

    def __init__(self, repository: EvidenceRepository) -> None:
        self._repository = repository

    def purge_document(self, candidate: PurgeCandidate) -> PurgeResult:
        """Purge a single document with evidence preservation."""
        dangling = self._repository.count_version_references(candidate.blob_id)
        if dangling > 0:
            return PurgeResult(
                content_object_id=candidate.content_object_id,
                blob_id=candidate.blob_id,
                state=PurgeState.FAILED,
                evidence_persisted=False,
                dangling_references=dangling,
            )

        snippet = build_evidence_snippet(candidate)
        self._repository.persist_evidence_snippet(snippet)

        self._repository.mark_content_purged(candidate.content_object_id, candidate.blob_id)

        return PurgeResult(
            content_object_id=candidate.content_object_id,
            blob_id=candidate.blob_id,
            state=PurgeState.PURGED,
            evidence_persisted=True,
            dangling_references=0,
        )

    def purge_batch(self, candidates: list[PurgeCandidate]) -> list[PurgeResult]:
        """Purge a batch of documents, collecting results."""
        results: list[PurgeResult] = []
        for candidate in candidates:
            result = self.purge_document(candidate)
            results.append(result)
        return results
