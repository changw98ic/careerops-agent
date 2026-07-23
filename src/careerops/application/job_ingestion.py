"""Application service for job ingestion, normalization, and dedup."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.domain.jobs import (
    ActorType,
    AggregateState,
    CanonicalJob,
    JobMergeDecision,
    JobPosting,
    JobPostingVersion,
    MergeDecisionKind,
    PostingSourceState,
)


def normalize_title(title: str) -> str:
    """Normalize a job title for dedup comparison."""
    text = unicodedata.normalize("NFKC", title)
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compute_content_hash(structured_data: dict[str, object]) -> str:
    """Compute a deterministic SHA-256 hash of structured job data."""
    import json

    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_fingerprint(title: str, location: str, company: str) -> str:
    """Compute a dedup fingerprint from normalized fields."""
    norm_title = normalize_title(title)
    norm_location = normalize_title(location)
    norm_company = normalize_title(company)
    raw = f"{norm_company}|{norm_title}|{norm_location}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class IngestedPosting:
    posting: JobPosting
    version: JobPostingVersion
    canonical_job: CanonicalJob
    merge_decision: JobMergeDecision


@dataclass(frozen=True, slots=True)
class IngestResult:
    posting_id: UUID
    version_id: UUID
    canonical_job_id: UUID
    is_new_posting: bool
    is_new_version: bool
    is_new_canonical: bool


class JobRepository(Protocol):
    def find_posting_by_source_and_external_id(
        self, source_id: UUID, external_id: str
    ) -> JobPosting | None: ...

    def find_latest_version(self, posting_id: UUID) -> JobPostingVersion | None: ...

    def find_canonical_by_fingerprint(self, fingerprint: str) -> CanonicalJob | None: ...

    def find_canonical_by_id(self, canonical_job_id: UUID) -> CanonicalJob | None: ...

    def find_canonical_for_posting(self, posting_id: UUID) -> UUID | None: ...

    def find_postings_for_canonical(self, canonical_job_id: UUID) -> list[JobPosting]: ...

    def save_posting(self, posting: JobPosting) -> None: ...

    def save_version(self, version: JobPostingVersion) -> None: ...

    def save_canonical_job(self, job: CanonicalJob) -> None: ...

    def save_merge_decision(self, decision: JobMergeDecision) -> None: ...

    def save_posting_assignment(
        self, posting_id: UUID, canonical_job_id: UUID, decision_id: UUID
    ) -> None: ...

    def update_posting_state(
        self, posting_id: UUID, state: PostingSourceState, now: datetime
    ) -> None: ...

    def update_canonical_state(
        self, canonical_job_id: UUID, state: AggregateState, now: datetime
    ) -> None: ...

    def update_posting_last_seen(self, posting_id: UUID, now: datetime) -> None: ...


class JobIngestionService:
    """Handles ingestion of crawled job postings with dedup and versioning."""

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def ingest_posting(
        self,
        *,
        source_id: UUID,
        external_id: str,
        canonical_url: str,
        structured_data: dict[str, object],
        source_url: str,
        parser_version: str,
        company_name: str,
        now: datetime,
    ) -> IngestResult:
        content_hash = compute_content_hash(structured_data)
        title = str(structured_data.get("title", ""))
        location = str(structured_data.get("location", ""))
        fingerprint = compute_fingerprint(title, location, company_name)

        existing_posting = self._repository.find_posting_by_source_and_external_id(
            source_id, external_id
        )

        if existing_posting is not None:
            return self._handle_existing_posting(
                existing_posting, content_hash, structured_data, source_url, parser_version, now
            )

        return self._handle_new_posting(
            source_id=source_id,
            external_id=external_id,
            canonical_url=canonical_url,
            content_hash=content_hash,
            structured_data=structured_data,
            source_url=source_url,
            parser_version=parser_version,
            title=title,
            location=location,
            fingerprint=fingerprint,
            now=now,
        )

    def _handle_existing_posting(
        self,
        posting: JobPosting,
        content_hash: str,
        structured_data: dict[str, object],
        source_url: str,
        parser_version: str,
        now: datetime,
    ) -> IngestResult:
        self._repository.update_posting_last_seen(posting.id, now)

        latest_version = self._repository.find_latest_version(posting.id)
        if latest_version is not None and latest_version.content_hash == content_hash:
            canonical_id = self._find_canonical_for_posting(posting.id)
            return IngestResult(
                posting_id=posting.id,
                version_id=latest_version.id,
                canonical_job_id=canonical_id if canonical_id is not None else uuid4(),
                is_new_posting=False,
                is_new_version=False,
                is_new_canonical=False,
            )

        changed_fields = self._compute_changed_fields(latest_version, structured_data)
        version_id = uuid4()
        version = JobPostingVersion(
            id=version_id,
            job_posting_id=posting.id,
            content_hash=content_hash,
            source_url=source_url,
            parser_version=parser_version,
            structured_data=structured_data,
            changed_fields=changed_fields,
            captured_at=now,
        )
        self._repository.save_version(version)

        if posting.source_state != PostingSourceState.ACTIVE:
            self._repository.update_posting_state(posting.id, PostingSourceState.ACTIVE, now)
            canonical_id = self._find_canonical_for_posting(posting.id)
            if canonical_id is not None:
                self._recompute_canonical_state(canonical_id, now)

        canonical_id_result = self._find_canonical_for_posting(posting.id)
        return IngestResult(
            posting_id=posting.id,
            version_id=version_id,
            canonical_job_id=canonical_id_result if canonical_id_result is not None else uuid4(),
            is_new_posting=False,
            is_new_version=True,
            is_new_canonical=False,
        )

    def _handle_new_posting(
        self,
        *,
        source_id: UUID,
        external_id: str,
        canonical_url: str,
        content_hash: str,
        structured_data: dict[str, object],
        source_url: str,
        parser_version: str,
        title: str,
        location: str,
        fingerprint: str,
        now: datetime,
    ) -> IngestResult:
        posting_id = uuid4()
        posting = JobPosting(
            id=posting_id,
            source_id=source_id,
            external_id=external_id,
            canonical_url=canonical_url,
            source_state=PostingSourceState.ACTIVE,
            first_seen_at=now,
            last_seen_at=now,
        )
        self._repository.save_posting(posting)

        version_id = uuid4()
        version = JobPostingVersion(
            id=version_id,
            job_posting_id=posting_id,
            content_hash=content_hash,
            source_url=source_url,
            parser_version=parser_version,
            structured_data=structured_data,
            changed_fields=(),
            captured_at=now,
        )
        self._repository.save_version(version)

        existing_canonical = self._repository.find_canonical_by_fingerprint(fingerprint)
        if existing_canonical is not None:
            decision_id = uuid4()
            decision = JobMergeDecision(
                id=decision_id,
                job_posting_id=posting_id,
                to_canonical_job_id=existing_canonical.id,
                decision_kind=MergeDecisionKind.MERGE,
                rule="fingerprint_exact",
                reason=f"Fingerprint match: {fingerprint[:16]}",
                score=1.0,
                actor_type=ActorType.RULE,
                algorithm_version="dedup-v1",
            )
            self._repository.save_merge_decision(decision)
            self._repository.save_posting_assignment(posting_id, existing_canonical.id, decision_id)
            return IngestResult(
                posting_id=posting_id,
                version_id=version_id,
                canonical_job_id=existing_canonical.id,
                is_new_posting=True,
                is_new_version=True,
                is_new_canonical=False,
            )

        canonical_id = uuid4()
        normalized = normalize_title(title)
        canonical_job = CanonicalJob(
            id=canonical_id,
            company_id=source_id,
            canonical_title=title,
            normalized_title=normalized,
            aggregate_state=AggregateState.ACTIVE,
            primary_posting_id=posting_id,
        )
        self._repository.save_canonical_job(canonical_job)

        decision_id = uuid4()
        decision = JobMergeDecision(
            id=decision_id,
            job_posting_id=posting_id,
            to_canonical_job_id=canonical_id,
            decision_kind=MergeDecisionKind.MERGE,
            rule="new_canonical",
            reason="First posting for this fingerprint",
            score=1.0,
            actor_type=ActorType.RULE,
            algorithm_version="dedup-v1",
        )
        self._repository.save_merge_decision(decision)
        self._repository.save_posting_assignment(posting_id, canonical_id, decision_id)

        return IngestResult(
            posting_id=posting_id,
            version_id=version_id,
            canonical_job_id=canonical_id,
            is_new_posting=True,
            is_new_version=True,
            is_new_canonical=True,
        )

    def close_posting(self, posting_id: UUID, *, now: datetime) -> None:
        """Mark a posting as closed and recompute canonical state."""
        self._repository.update_posting_state(posting_id, PostingSourceState.CLOSED, now)
        canonical_id = self._find_canonical_for_posting(posting_id)
        if canonical_id is not None:
            self._recompute_canonical_state(canonical_id, now)

    def reopen_posting(self, posting_id: UUID, *, now: datetime) -> None:
        """Reopen a closed posting and recompute canonical state."""
        self._repository.update_posting_state(posting_id, PostingSourceState.ACTIVE, now)
        canonical_id = self._find_canonical_for_posting(posting_id)
        if canonical_id is not None:
            self._recompute_canonical_state(canonical_id, now)

    def _recompute_canonical_state(self, canonical_job_id: UUID, now: datetime) -> None:
        postings = self._repository.find_postings_for_canonical(canonical_job_id)
        has_active = any(p.source_state == PostingSourceState.ACTIVE for p in postings)
        new_state = AggregateState.ACTIVE if has_active else AggregateState.CLOSED
        self._repository.update_canonical_state(canonical_job_id, new_state, now)

    def _find_canonical_for_posting(self, posting_id: UUID) -> UUID | None:
        """Find the canonical job ID for a posting via merge decisions."""
        return self._repository.find_canonical_for_posting(posting_id)

    def _compute_changed_fields(
        self,
        previous: JobPostingVersion | None,
        new_data: dict[str, object],
    ) -> tuple[str, ...]:
        if previous is None:
            return ()
        changed: list[str] = []
        for key in new_data:
            old_val = previous.structured_data.get(key)
            new_val = new_data.get(key)
            if old_val != new_val:
                changed.append(key)
        return tuple(changed)
