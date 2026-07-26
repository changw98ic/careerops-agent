"""In-memory repository implementations for non-production API routes.

These back the matching, jobs, and applications routers so the REST API
returns real (empty-to-start) data instead of silently returning 503.
In PRODUCTION a proper Postgres-backed implementation should replace each
class; until then, the in-memory stores are correct for development and
integration testing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from careerops.api.errors import ConflictError, NotFoundError
from careerops.domain.applications import (
    Application,
    ApplicationCycle,
    ApplicationEvent,
    ApplicationPackage,
    ConfirmationStatus,
    FollowUpReminder,
    ResumeParseStatus,
    ResumeVersion,
)
from careerops.domain.candidates import (
    CompensationRecord,
    EvidenceItem,
    MatchResult,
    RemoteEligibility,
)
from careerops.domain.contacts import RecruitingContact
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import (
    CrawlPlanVersion,
    CrawlPolicyStatus,
    CrawlRun,
    CrawlRunCounters,
    CrawlSource,
    CrawlSourceState,
)
from careerops.domain.jobs import (
    AggregateState,
    CanonicalJob,
    JobMergeDecision,
    JobPosting,
    JobPostingVersion,
    PostingSourceState,
)
from careerops.domain.profiles import ProfileVersion
from careerops.infrastructure.database.profile_validation import (
    validate_profile_preferences,
)

# ---------------------------------------------------------------------------
# Matching read repository (candidates, evidence, matches, compensation)
# ---------------------------------------------------------------------------


class InMemoryMatchingReadRepository:
    """Stores candidates, evidence, match results, remote eligibility, and
    compensation records in memory.  Satisfies the interface expected by
    ``matching.py`` routes *and* the ``MatchDataRepository`` protocol used
    by ``MatchOrchestrator``."""

    def __init__(self) -> None:
        self._candidates: dict[UUID, dict[str, object]] = {}
        self._evidence: dict[UUID, list[EvidenceItem]] = {}
        self._matches: list[MatchResult] = []
        self._remote_eligibility: dict[str, RemoteEligibility] = {}
        self._compensation: dict[str, CompensationRecord] = {}

    # -- Candidates ---------------------------------------------------------

    def add_candidate(self, candidate_id: UUID, display_name: str) -> None:
        self._candidates[candidate_id] = {
            "id": candidate_id,
            "display_name": display_name,
            "evidence_count": 0,
        }
        self._evidence.setdefault(candidate_id, [])

    def list_candidates(self, *, limit: int = 50) -> list[dict[str, object]]:
        items = list(self._candidates.values())
        for item in items:
            cid: UUID = item["id"]  # type: ignore[assignment]
            item["evidence_count"] = len(self._evidence.get(cid, []))
        return items[:limit]

    # -- Evidence -----------------------------------------------------------

    def add_evidence(self, item: EvidenceItem) -> None:
        self._evidence.setdefault(item.candidate_id, []).append(item)

    def list_evidence(self, candidate_id: str) -> list[dict[str, object]]:
        cid = UUID(candidate_id)
        return [_evidence_to_dict(e) for e in self._evidence.get(cid, [])]

    def find_by_candidate(self, candidate_id: UUID) -> list[EvidenceItem]:
        return list(self._evidence.get(candidate_id, []))

    # -- Matches ------------------------------------------------------------

    def add_match(self, result: MatchResult) -> None:
        self._matches.append(result)

    def list_matches(
        self,
        *,
        candidate_id: str | None = None,
        canonical_job_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        filtered = self._matches
        if candidate_id is not None:
            filtered = [m for m in filtered if str(m.candidate_id) == candidate_id]
        if canonical_job_id is not None:
            filtered = [m for m in filtered if str(m.canonical_job_id) == canonical_job_id]
        return [_match_to_dict(m) for m in filtered[:limit]]

    # -- Remote eligibility -------------------------------------------------

    def set_remote_eligibility(self, eligibility: RemoteEligibility) -> None:
        self._remote_eligibility[str(eligibility.canonical_job_id)] = eligibility

    def get_remote_eligibility(self, job_id: str) -> dict[str, object] | None:
        eligibility = self._remote_eligibility.get(job_id)
        if eligibility is None:
            return None
        return {
            "canonical_job_id": eligibility.canonical_job_id,
            "verdict": eligibility.verdict.value,
            "confidence": eligibility.confidence,
            "evidence_spans": list(eligibility.evidence_spans),
            "reason": eligibility.reason,
            "rules_version": eligibility.rules_version,
        }

    # -- Compensation -------------------------------------------------------

    def set_compensation(self, record: CompensationRecord) -> None:
        self._compensation[str(record.canonical_job_id)] = record

    def get_compensation(self, job_id: str) -> dict[str, object] | None:
        comp = self._compensation.get(job_id)
        if comp is None:
            return None
        return {
            "id": comp.id,
            "canonical_job_id": comp.canonical_job_id,
            "currency": comp.currency,
            "amount_min": comp.amount_min,
            "amount_max": comp.amount_max,
            "period": comp.period,
            "normalized_amount_min": comp.normalized_amount_min,
            "normalized_amount_max": comp.normalized_amount_max,
            "normalized_currency": comp.normalized_currency,
            "score": comp.score,
        }

    # -- MatchDataRepository protocol (for MatchOrchestrator) ---------------

    def get_job_structured_data(self, canonical_job_id: UUID) -> dict[str, object] | None:
        """Return structured data for a canonical job.

        This stub returns None; when a Postgres-backed implementation exists
        it will join canonical_jobs -> job_posting_versions to retrieve the
        latest structured_data blob.
        """
        return None

    def get_candidate_evidence(self, candidate_id: UUID) -> list[EvidenceItem]:
        return self.find_by_candidate(candidate_id)

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
        for item in self._evidence.get(candidate_id, []):
            if (
                item.repository == repository
                and item.commit_sha == commit_sha
                and item.path == path
                and item.symbol == symbol
                and item.content_hash == content_hash
            ):
                return item
        return None

    def save(self, item: EvidenceItem) -> None:
        self.add_evidence(item)


# ---------------------------------------------------------------------------
# Job read repository (companies, canonical jobs, job detail)
# ---------------------------------------------------------------------------


class InMemoryJobReadRepository:
    """In-memory store for companies, canonical jobs, postings, versions,
    and merge decisions.  Satisfies both the jobs.py route interface and the
    ``JobRepository`` protocol used by ``JobIngestionService``."""

    def __init__(self) -> None:
        self._companies: dict[UUID, dict[str, object]] = {}
        self._canonical_jobs: dict[UUID, CanonicalJob] = {}
        self._postings: dict[UUID, JobPosting] = {}
        self._versions: dict[UUID, list[JobPostingVersion]] = {}  # posting_id -> versions
        self._merge_decisions: list[JobMergeDecision] = []
        self._posting_assignments: dict[UUID, UUID] = {}  # posting_id -> canonical_job_id
        self._canonical_postings: dict[UUID, list[UUID]] = {}  # canonical_job_id -> [posting_ids]
        self._posting_by_source_ext: dict[
            tuple[UUID, str], UUID
        ] = {}  # (source_id, ext_id) -> posting_id
        self._canonical_by_fingerprint: dict[str, UUID] = {}

    # -- Companies (read) ---------------------------------------------------

    def add_company(
        self,
        company_id: UUID,
        name: str,
        normalized_name: str,
        official_domains: list[str] | None = None,
        terms_status: str = "unknown",
    ) -> None:
        self._companies[company_id] = {
            "id": company_id,
            "name": name,
            "normalized_name": normalized_name,
            "official_domains": official_domains or [],
            "terms_status": terms_status,
        }

    def list_companies(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        from datetime import datetime

        _EPOCH = datetime.min.replace(tzinfo=UTC)

        def _sort_key(c: dict[str, object]) -> tuple[datetime, UUID]:
            cid = cast("UUID", c["id"])
            ca = c.get("created_at")
            if ca is None:
                return (_EPOCH, cid)
            if isinstance(ca, str):
                return (datetime.fromisoformat(ca), cid)
            return (cast("datetime", ca), cid)

        items = sorted(self._companies.values(), key=_sort_key)
        total = len(items)
        # Apply cursor filter.
        if cursor is not None:
            import base64

            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = decoded.rsplit("|", 1)
            cur_created = datetime.fromisoformat(ts_str)
            cur_id = UUID(id_str)
            items = [c for c in items if _sort_key(c) > (cur_created, cur_id)]
        has_more = len(items) > limit
        page = items[:limit]
        next_cursor = None
        if has_more and page:
            import base64

            last = page[-1]
            last_key = _sort_key(last)
            next_cursor = base64.urlsafe_b64encode(
                f"{last_key[0].isoformat()}|{last_key[1]}".encode()
            ).decode()
        return {"items": page, "total": total, "next_cursor": next_cursor}

    # -- Canonical jobs (read) ----------------------------------------------

    def list_canonical_jobs(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        state: str | None = None,
        q: str | None = None,
    ) -> dict[str, object]:
        from datetime import datetime

        _EPOCH = datetime.min.replace(tzinfo=UTC)

        def _sort_key(j: CanonicalJob) -> tuple[datetime, UUID]:
            return (j.created_at or _EPOCH, j.id)

        jobs = list(self._canonical_jobs.values())
        if state is not None:
            jobs = [j for j in jobs if j.aggregate_state.value == state]
        if q is not None and q.strip():
            pattern = q.strip().lower()
            jobs = [j for j in jobs if pattern in j.canonical_title.lower()]
        jobs.sort(key=_sort_key)
        total = len(jobs)
        # Apply cursor filter.
        if cursor is not None:
            import base64

            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = decoded.rsplit("|", 1)
            cur_created = datetime.fromisoformat(ts_str)
            cur_id = UUID(id_str)
            jobs = [j for j in jobs if _sort_key(j) > (cur_created, cur_id)]
        has_more = len(jobs) > limit
        page = jobs[:limit]
        next_cursor = None
        if has_more and page:
            import base64

            last = page[-1]
            last_key = _sort_key(last)
            next_cursor = base64.urlsafe_b64encode(
                f"{last_key[0].isoformat()}|{last_key[1]}".encode()
            ).decode()
        return {
            "items": [
                {
                    "id": j.id,
                    "company_id": j.company_id,
                    "canonical_title": j.canonical_title,
                    "aggregate_state": j.aggregate_state.value,
                }
                for j in page
            ],
            "total": total,
            "next_cursor": next_cursor,
        }

    def get_job_detail(self, job_id: str) -> dict[str, object] | None:
        jid = UUID(job_id)
        job = self._canonical_jobs.get(jid)
        if job is None:
            return None
        posting_ids = self._canonical_postings.get(jid, [])
        postings = [self._postings[pid] for pid in posting_ids if pid in self._postings]
        versions: list[dict[str, object]] = []
        merge_decisions: list[dict[str, object]] = []
        for pid in posting_ids:
            for v in self._versions.get(pid, []):
                versions.append(
                    {
                        "id": str(v.id),
                        "posting_id": str(v.job_posting_id),
                        "content_hash": v.content_hash,
                        "structured_data": v.structured_data,
                        "captured_at": v.captured_at.isoformat() if v.captured_at else None,
                    }
                )
            for md in self._merge_decisions:
                if md.job_posting_id == pid:
                    merge_decisions.append(
                        {
                            "id": str(md.id),
                            "posting_id": str(md.job_posting_id),
                            "decision_kind": md.decision_kind.value,
                            "rule": md.rule,
                            "reason": md.reason,
                        }
                    )
        return {
            "id": job.id,
            "company_id": job.company_id,
            "canonical_title": job.canonical_title,
            "aggregate_state": job.aggregate_state.value,
            "postings": [
                {
                    "id": str(p.id),
                    "source_id": str(p.source_id),
                    "external_id": p.external_id,
                    "canonical_url": p.canonical_url,
                    "source_state": p.source_state.value,
                    "first_seen_at": p.first_seen_at.isoformat() if p.first_seen_at else None,
                    "last_seen_at": p.last_seen_at.isoformat() if p.last_seen_at else None,
                }
                for p in postings
            ],
            "versions": versions,
            "merge_decisions": merge_decisions,
        }

    # -- JobRepository protocol (for JobIngestionService) -------------------

    def find_posting_by_source_and_external_id(
        self, source_id: UUID, external_id: str
    ) -> JobPosting | None:
        pid = self._posting_by_source_ext.get((source_id, external_id))
        return self._postings.get(pid) if pid else None

    def find_latest_version(self, posting_id: UUID) -> JobPostingVersion | None:
        versions = self._versions.get(posting_id, [])
        return versions[-1] if versions else None

    def find_canonical_by_fingerprint(self, fingerprint: str) -> CanonicalJob | None:
        cid = self._canonical_by_fingerprint.get(fingerprint)
        return self._canonical_jobs.get(cid) if cid else None

    def find_canonical_by_id(self, canonical_job_id: UUID) -> CanonicalJob | None:
        return self._canonical_jobs.get(canonical_job_id)

    def find_canonical_for_posting(self, posting_id: UUID) -> UUID | None:
        return self._posting_assignments.get(posting_id)

    def find_postings_for_canonical(self, canonical_job_id: UUID) -> list[JobPosting]:
        pids = self._canonical_postings.get(canonical_job_id, [])
        return [self._postings[pid] for pid in pids if pid in self._postings]

    def save_posting(self, posting: JobPosting) -> None:
        self._postings[posting.id] = posting
        self._posting_by_source_ext[(posting.source_id, posting.external_id)] = posting.id

    def save_version(self, version: JobPostingVersion) -> None:
        self._versions.setdefault(version.job_posting_id, []).append(version)

    def save_canonical_job(self, job: CanonicalJob) -> None:
        self._canonical_jobs[job.id] = job

    def save_merge_decision(self, decision: JobMergeDecision) -> None:
        self._merge_decisions.append(decision)

    def save_posting_assignment(
        self, posting_id: UUID, canonical_job_id: UUID, decision_id: UUID
    ) -> None:
        self._posting_assignments[posting_id] = canonical_job_id
        self._canonical_postings.setdefault(canonical_job_id, []).append(posting_id)

    def update_posting_state(
        self, posting_id: UUID, state: PostingSourceState, now: datetime
    ) -> None:
        posting = self._postings.get(posting_id)
        if posting is not None:
            self._postings[posting_id] = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=posting.last_seen_at,
                closed_at=now if state == PostingSourceState.CLOSED else posting.closed_at,
                created_at=posting.created_at,
                updated_at=now,
            )

    def update_canonical_state(
        self, canonical_job_id: UUID, state: AggregateState, now: datetime
    ) -> None:
        job = self._canonical_jobs.get(canonical_job_id)
        if job is not None:
            self._canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=state,
                primary_posting_id=job.primary_posting_id,
                created_at=job.created_at,
                updated_at=now,
            )

    def update_posting_last_seen(self, posting_id: UUID, now: datetime) -> None:
        posting = self._postings.get(posting_id)
        if posting is not None:
            self._postings[posting_id] = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=posting.source_state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=now,
                closed_at=posting.closed_at,
                created_at=posting.created_at,
                updated_at=now,
            )

    def update_canonical_primary_posting(self, canonical_job_id: UUID, posting_id: UUID) -> None:
        job = self._canonical_jobs.get(canonical_job_id)
        if job is not None:
            self._canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=job.aggregate_state,
                primary_posting_id=posting_id,
            )


# ---------------------------------------------------------------------------
# Contact repository
# ---------------------------------------------------------------------------


class InMemoryContactRepository:
    """In-memory contact store.  Satisfies ``ContactRepository`` protocol."""

    def __init__(self) -> None:
        self._contacts: dict[UUID, RecruitingContact] = {}
        self._by_email: dict[tuple[UUID, str], UUID] = {}  # (company_id, email) -> id
        self._by_company: dict[UUID, list[UUID]] = {}

    def find_by_email(self, company_id: UUID, email: str) -> RecruitingContact | None:
        cid = self._by_email.get((company_id, email))
        return self._contacts.get(cid) if cid else None

    def find_by_company(self, company_id: UUID) -> list[RecruitingContact]:
        ids = self._by_company.get(company_id, [])
        return [self._contacts[cid] for cid in ids if cid in self._contacts]

    def save(self, contact: RecruitingContact) -> None:
        self._contacts[contact.id] = contact
        self._by_email[(contact.company_id, contact.email)] = contact.id
        self._by_company.setdefault(contact.company_id, []).append(contact.id)

    # -- Route-facing helper (returns dicts for the API response) -----------

    def list_contacts(self, company_id: str) -> list[dict[str, object]]:
        cid = UUID(company_id)
        contacts = self.find_by_company(cid)
        return [
            {
                "id": c.id,
                "company_id": c.company_id,
                "email": c.email,
                "name": c.name,
                "role": c.role,
                "source": c.source.value,
                "source_url": c.source_url,
                "publicly_listed": c.publicly_listed,
                "domain_match": c.domain_match,
                "confidence": c.confidence.value,
                "allowed_actions": [a.value for a in c.allowed_actions],
            }
            for c in contacts
        ]


# ---------------------------------------------------------------------------
# Application repository
# ---------------------------------------------------------------------------


class InMemoryApplicationRepository:
    """In-memory application, event, resume, package, and follow-up store.

    Satisfies the ``ApplicationRepository``, ``ResumeRepository``,
    ``PackageRepository``, and ``FollowUpRepository`` protocols.
    """

    def __init__(self) -> None:
        self._applications: dict[UUID, Application] = {}
        self._events: dict[UUID, list[ApplicationEvent]] = {}
        self._by_candidate_job: dict[tuple[UUID, UUID], UUID] = {}
        self._resumes: dict[UUID, ResumeVersion] = {}
        self._resume_latest: dict[UUID, ResumeVersion] = {}  # candidate_id -> latest
        self._packages: dict[UUID, ApplicationPackage] = {}
        self._follow_ups: dict[UUID, FollowUpReminder] = {}

    # -- ApplicationRepository protocol ------------------------------------

    def find_by_id(self, application_id: UUID) -> Application | None:
        return self._applications.get(application_id)

    def find_by_candidate_and_job(
        self, candidate_id: UUID, canonical_job_id: UUID
    ) -> Application | None:
        aid = self._by_candidate_job.get((candidate_id, canonical_job_id))
        return self._applications.get(aid) if aid else None

    def save(self, application: Application) -> None:
        self._applications[application.id] = application
        self._by_candidate_job[(application.candidate_id, application.canonical_job_id)] = (
            application.id
        )

    def append_event(self, event: ApplicationEvent) -> None:
        self._events.setdefault(event.application_id, []).append(event)

    def get_events(self, application_id: UUID) -> list[ApplicationEvent]:
        return list(self._events.get(application_id, []))

    # -- Resume operations --------------------------------------------------

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None:
        return self._resume_latest.get(candidate_id)

    def save_resume(self, version: ResumeVersion) -> None:
        self._resumes[version.id] = version
        current = self._resume_latest.get(version.candidate_id)
        if current is None or version.version_number > current.version_number:
            self._resume_latest[version.candidate_id] = version

    # -- Resume lifecycle (task 2.4): content-addressed dedupe + eligibility -

    def find_resume_by_content_hash(
        self, candidate_id: UUID, content_hash: str
    ) -> ResumeVersion | None:
        """Content-addressed dedupe: return the existing version for a hash."""
        if not content_hash:
            return None
        matches = [
            r
            for r in self._resumes.values()
            if r.candidate_id == candidate_id and r.content_hash == content_hash
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version_number)

    def find_resume_by_id(self, candidate_id: UUID, version_id: UUID) -> ResumeVersion | None:
        version = self._resumes.get(version_id)
        if version is None or version.candidate_id != candidate_id:
            return None
        return version

    def find_eligible_resumes(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """Parsed + confirmed resumes — the only ones eligible for packages."""
        eligible = [
            r
            for r in self._resumes.values()
            if r.candidate_id == candidate_id
            and r.parse_status == ResumeParseStatus.PARSED
            and r.confirmation_status == ConfirmationStatus.CONFIRMED
        ]
        eligible.sort(key=lambda r: r.version_number, reverse=True)
        return eligible[:limit]

    def list_resumes(self, candidate_id: UUID, *, limit: int = 50) -> list[ResumeVersion]:
        """All resume versions for the candidate, newest version_number first.

        Additive read (Section 3 task 3.3) mirroring the Postgres repo.
        """
        items = [r for r in self._resumes.values() if r.candidate_id == candidate_id]
        items.sort(key=lambda r: r.version_number, reverse=True)
        return items[:limit]

    # -- Package operations -------------------------------------------------

    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None:
        return self._packages.get(application_id)

    def save_package(self, package: ApplicationPackage) -> None:
        self._packages[package.application_id] = package

    # -- Follow-up operations -----------------------------------------------

    def find_follow_up_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._follow_ups.get(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        for fu in self._follow_ups.values():
            if (
                fu.application_id == application_id
                and fu.rule_version == rule_version
                and fu.state.value == "active"
            ):
                return fu
        return None

    def save_follow_up(self, reminder: FollowUpReminder) -> None:
        self._follow_ups[reminder.id] = reminder

    # -- Route-facing helpers (returns dicts for API responses) -------------

    def list_applications(
        self,
        *,
        candidate_id: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        from datetime import datetime

        _EPOCH = datetime.min.replace(tzinfo=UTC)

        def _sort_key(a: Application) -> tuple[datetime, UUID]:
            return (a.created_at or _EPOCH, a.id)

        apps = list(self._applications.values())
        if candidate_id is not None:
            cid = UUID(candidate_id)
            apps = [a for a in apps if a.candidate_id == cid]
        if state is not None:
            apps = [a for a in apps if a.state.value == state]
        # Sort descending by created_at, id (matches Postgres ordering).
        apps.sort(key=_sort_key, reverse=True)
        total = len(apps)
        # Apply cursor filter (descending: cursor < items).
        if cursor is not None:
            import base64

            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = decoded.rsplit("|", 1)
            cur_created = datetime.fromisoformat(ts_str)
            cur_id = UUID(id_str)
            apps = [a for a in apps if _sort_key(a) < (cur_created, cur_id)]
        has_more = len(apps) > limit
        page = apps[:limit]
        next_cursor = None
        if has_more and page:
            import base64

            last = page[-1]
            last_key = _sort_key(last)
            next_cursor = base64.urlsafe_b64encode(
                f"{last_key[0].isoformat()}|{last_key[1]}".encode()
            ).decode()
        return {
            "items": [
                {
                    "id": a.id,
                    "candidate_id": a.candidate_id,
                    "canonical_job_id": a.canonical_job_id,
                    "state": a.state.value,
                    "apply_url": a.apply_url,
                    "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
                    "follow_up_due_at": (
                        a.follow_up_due_at.isoformat() if a.follow_up_due_at else None
                    ),
                    "version": a.version,
                }
                for a in page
            ],
            "total": total,
            "next_cursor": next_cursor,
        }


# ---------------------------------------------------------------------------
# Profile version repository (end-to-end-career-application-loop, Section 2)
# ---------------------------------------------------------------------------


class InMemoryProfileRepository:
    """In-memory profile-version store, behaviorally identical to
    :class:`careerops.infrastructure.database.postgres_profile_repo.PostgresProfileRepository`.

    Exactly one active version per candidate (invariants mirror the DB partial
    unique index). ``create_version`` and ``activate`` both validate
    contradictory preferences before mutating state so an invalid version can
    never become active.
    """

    def __init__(self) -> None:
        self._versions: dict[UUID, ProfileVersion] = {}
        self._active_by_candidate: dict[UUID, UUID] = {}

    def get_active_for(self, candidate_id: UUID) -> ProfileVersion | None:
        active_id = self._active_by_candidate.get(candidate_id)
        if active_id is None:
            return None
        return self._versions.get(active_id)

    def get_by_version_id(self, candidate_id: UUID, version_id: UUID) -> ProfileVersion:
        version = self._versions.get(version_id)
        if version is None or version.candidate_id != candidate_id:
            raise NotFoundError("profile version not found for candidate")
        return version

    def list_versions(self, candidate_id: UUID, *, limit: int = 50) -> list[ProfileVersion]:
        items = [v for v in self._versions.values() if v.candidate_id == candidate_id]
        items.sort(key=lambda v: v.version, reverse=True)
        return items[:limit]

    def create_version(self, profile: ProfileVersion) -> ProfileVersion:
        validate_profile_preferences(profile)
        # Store inactive first; flip to active under the same logical op so the
        # one-active invariant never transiently breaks.
        stored = ProfileVersion(
            id=profile.id,
            candidate_id=profile.candidate_id,
            version=profile.version,
            is_active=False,
            target_roles=profile.target_roles,
            locations=profile.locations,
            remote_rules=profile.remote_rules,
            compensation=profile.compensation,
            seniority=profile.seniority,
            authorization=profile.authorization,
            include_keywords=profile.include_keywords,
            exclude_keywords=profile.exclude_keywords,
            hard_exclusions=profile.hard_exclusions,
            rules_version=profile.rules_version,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )
        self._versions[profile.id] = stored
        if profile.is_active:
            return self._activate(candidate_id=profile.candidate_id, version_id=profile.id)
        return stored

    def activate(
        self, candidate_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> ProfileVersion:
        target = self._versions.get(version_id)
        if target is None or target.candidate_id != candidate_id:
            raise NotFoundError("profile version not found for candidate")
        # Re-validate: a version that became contradictory after a rules change
        # must not flip to active.
        validate_profile_preferences(target)
        return self._activate(candidate_id=candidate_id, version_id=version_id)

    def _activate(self, *, candidate_id: UUID, version_id: UUID) -> ProfileVersion:
        prior_active_id = self._active_by_candidate.get(candidate_id)
        if prior_active_id is not None and prior_active_id != version_id:
            prior = self._versions.get(prior_active_id)
            if prior is not None:
                # frozen dataclass -> replace. Mutate the dict in place so the
                # re-spread stays ``dict[str, Any]`` (a merge literal would
                # widen to ``dict[str, Any | bool]`` and break the typed kwargs).
                prior_dict = _profile_as_dict(prior)
                prior_dict["is_active"] = False
                self._versions[prior_active_id] = ProfileVersion(**prior_dict)
        target = self._versions[version_id]
        target_dict = _profile_as_dict(target)
        target_dict["is_active"] = True
        self._versions[version_id] = ProfileVersion(**target_dict)
        self._active_by_candidate[candidate_id] = version_id
        return self._versions[version_id]

    @staticmethod
    def validate(profile: ProfileVersion) -> None:
        validate_profile_preferences(profile)


def _profile_as_dict(profile: ProfileVersion) -> dict[str, Any]:
    """Return a mutable dict for the frozen dataclass re-build pattern."""
    # dataclasses.asdict would deep-copy tuples/dicts; we want the SAME
    # references so the rebuild preserves identity for nested frozen types.
    # Values are typed ``Any`` (not ``object``) so the ``**`` re-spread into
    # ``ProfileVersion(...)`` is accepted by pyright: the rebuild is structurally
    # identical to the source (we only ever flip ``is_active``).
    return {
        "id": profile.id,
        "candidate_id": profile.candidate_id,
        "version": profile.version,
        "is_active": profile.is_active,
        "target_roles": profile.target_roles,
        "locations": profile.locations,
        "remote_rules": profile.remote_rules,
        "compensation": profile.compensation,
        "seniority": profile.seniority,
        "authorization": profile.authorization,
        "include_keywords": profile.include_keywords,
        "exclude_keywords": profile.exclude_keywords,
        "hard_exclusions": profile.hard_exclusions,
        "rules_version": profile.rules_version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


# ---------------------------------------------------------------------------
# Application cycle repository (end-to-end-career-application-loop, Section 2)
# ---------------------------------------------------------------------------


class InMemoryApplicationCycleRepository:
    """In-memory application-cycle store mirroring
    :class:`careerops.infrastructure.database.postgres_application_cycle_repo.PostgresApplicationCycleRepository`.

    Enforces one active cycle per (candidate, canonical_job) in memory. Re-
    application closes the prior active cycle and opens a new one linked via
    ``prior_cycle_id``, preserving full history.
    """

    def __init__(self) -> None:
        self._cycles: dict[UUID, ApplicationCycle] = {}

    def get_active_for(self, candidate_id: UUID, canonical_job_id: UUID) -> ApplicationCycle | None:
        for cycle in self._cycles.values():
            if (
                cycle.candidate_id == candidate_id
                and cycle.canonical_job_id == canonical_job_id
                and cycle.active
            ):
                return cycle
        return None

    def get_by_id(self, candidate_id: UUID, cycle_id: UUID) -> ApplicationCycle:
        cycle = self._cycles.get(cycle_id)
        if cycle is None or cycle.candidate_id != candidate_id:
            raise NotFoundError("application cycle not found for candidate")
        return cycle

    def list_history(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        limit: int = 50,
    ) -> list[ApplicationCycle]:
        items = [
            c
            for c in self._cycles.values()
            if c.candidate_id == candidate_id and c.canonical_job_id == canonical_job_id
        ]
        items.sort(
            key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return items[:limit]

    def get_or_create_active(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle:
        existing = self.get_active_for(candidate_id, canonical_job_id)
        if existing is not None:
            return existing
        created_at = now or datetime.now(UTC)
        cycle = ApplicationCycle(
            id=cycle_id,
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            active=True,
            prior_cycle_id=None,
            reason=reason,
            created_at=created_at,
            closed_at=None,
        )
        self._cycles[cycle_id] = cycle
        return cycle

    def open_reapplication_cycle(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        new_cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle:
        prior = self.get_active_for(candidate_id, canonical_job_id)
        if prior is None:
            raise NotFoundError("no active application cycle to reapply from")
        closed_at = now or datetime.now(UTC)
        # Close prior
        self._cycles[prior.id] = ApplicationCycle(
            id=prior.id,
            candidate_id=prior.candidate_id,
            canonical_job_id=prior.canonical_job_id,
            active=False,
            prior_cycle_id=prior.prior_cycle_id,
            reason=prior.reason,
            created_at=prior.created_at,
            closed_at=closed_at,
        )
        # Open new linked cycle
        new_cycle = ApplicationCycle(
            id=new_cycle_id,
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            active=True,
            prior_cycle_id=prior.id,
            reason=reason,
            created_at=closed_at,
            closed_at=None,
        )
        self._cycles[new_cycle_id] = new_cycle
        return new_cycle


# ---------------------------------------------------------------------------
# Evidence repository (resume-derived lifecycle)
# ---------------------------------------------------------------------------


class InMemoryEvidenceRepository:
    """In-memory evidence lifecycle store mirroring
    :class:`careerops.infrastructure.database.postgres_evidence_repo.PostgresEvidenceRepository`.

    Stores resume-derived evidence with source_span / extractor_version /
    confirmation_status / evidence_hash / resume_version_id. Confirm/reject
    only flips confirmation_status — evidence rows are never deleted, so the
    created_at-ordered sequence IS the append-only audit trail.
    """

    def __init__(self) -> None:
        self._items: dict[UUID, EvidenceItem] = {}

    def get_by_id(self, candidate_id: UUID, evidence_id: UUID) -> EvidenceItem:
        item = self._items.get(evidence_id)
        if item is None or item.candidate_id != candidate_id:
            raise NotFoundError("evidence item not found for candidate")
        return item

    def list_for_candidate(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]:
        items = [i for i in self._items.values() if i.candidate_id == candidate_id]
        items.sort(
            key=lambda i: i.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return items[:limit]

    def list_confirmed_for(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]:
        confirmed = [
            i
            for i in self._items.values()
            if i.candidate_id == candidate_id
            and i.confirmation_status == ConfirmationStatus.CONFIRMED
        ]
        confirmed.sort(
            key=lambda i: i.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return confirmed[:limit]

    def find_by_evidence_hash(self, candidate_id: UUID, evidence_hash: str) -> EvidenceItem | None:
        if not evidence_hash:
            return None
        for item in self._items.values():
            if item.candidate_id == candidate_id and item.evidence_hash == evidence_hash:
                return item
        return None

    def store(self, item: EvidenceItem) -> EvidenceItem:
        # Idempotency on the repository-derived key: if a row with the same
        # (candidate_id, repository, commit_sha, path, symbol, content_hash)
        # already exists, keep the existing row (mirrors the DB partial key).
        existing = self._find_by_repo_key(item)
        if existing is not None:
            return existing
        self._items[item.id] = item
        return item

    def confirm(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        *,
        now: datetime | None = None,
    ) -> EvidenceItem:
        return self._set_confirmation(candidate_id, evidence_id, ConfirmationStatus.CONFIRMED)

    def reject(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        *,
        now: datetime | None = None,
    ) -> EvidenceItem:
        return self._set_confirmation(candidate_id, evidence_id, ConfirmationStatus.REJECTED)

    def _find_by_repo_key(self, item: EvidenceItem) -> EvidenceItem | None:
        for existing in self._items.values():
            if (
                existing.candidate_id == item.candidate_id
                and existing.repository == item.repository
                and existing.commit_sha == item.commit_sha
                and existing.path == item.path
                and existing.symbol == item.symbol
                and existing.content_hash == item.content_hash
            ):
                return existing
        return None

    def _set_confirmation(
        self,
        candidate_id: UUID,
        evidence_id: UUID,
        status: ConfirmationStatus,
    ) -> EvidenceItem:
        existing = self.get_by_id(candidate_id, evidence_id)
        if existing.confirmation_status is status:
            return existing
        updated = EvidenceItem(
            id=existing.id,
            candidate_id=existing.candidate_id,
            kind=existing.kind,
            name=existing.name,
            description=existing.description,
            repository=existing.repository,
            commit_sha=existing.commit_sha,
            path=existing.path,
            symbol=existing.symbol,
            content_hash=existing.content_hash,
            source_url=existing.source_url,
            verified=existing.verified,
            extractor_version=existing.extractor_version,
            source_span=existing.source_span,
            confirmation_status=status,
            evidence_hash=existing.evidence_hash,
            resume_version_id=existing.resume_version_id,
            created_at=existing.created_at,
        )
        self._items[evidence_id] = updated
        return updated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence_to_dict(e: EvidenceItem) -> dict[str, object]:
    return {
        "id": str(e.id),
        "candidate_id": str(e.candidate_id),
        "kind": e.kind.value,
        "name": e.name,
        "description": e.description,
        "repository": e.repository,
        "commit_sha": e.commit_sha,
        "path": e.path,
        "symbol": e.symbol,
        "verified": e.verified,
    }


def _match_to_dict(m: MatchResult) -> dict[str, object]:
    return {
        "id": str(m.id),
        "candidate_id": str(m.candidate_id),
        "canonical_job_id": str(m.canonical_job_id),
        "tier": m.tier.value,
        "overall_score": m.overall_score,
        "geographic_blocked": m.geographic_blocked,
        "remote_verdict": m.remote_verdict.value,
        "requirement_matches": [
            {
                "requirement_name": rm.requirement_name,
                "level": rm.level.value,
                "confidence": rm.confidence,
                "reason": rm.reason,
                "evidence_ids": [str(eid) for eid in rm.evidence_ids],
            }
            for rm in m.requirement_matches
        ],
        "rules_version": m.rules_version,
        "input_hash": m.input_hash,
        "output_hash": m.output_hash,
    }


# ---------------------------------------------------------------------------
# Crawl source / plan / run repositories (end-to-end-career-application-loop,
# Section 4). Behaviorally identical to the Postgres repos in
# ``infrastructure/database/postgres_crawl_repo.py``. Every method scopes by
# the server-resolved ``owner_id`` (Iron Rule 2).
# ---------------------------------------------------------------------------


class InMemoryCrawlSourceRepository:
    """In-memory crawl-source store mirroring
    :class:`careerops.infrastructure.database.postgres_crawl_repo.PostgresCrawlSourceRepository`.

    Sources are keyed by id; ``owner_id`` is tracked per source (the single-
    user runtime means it is constant today, but the in-memory store keeps the
    field so the ownership-scoping pattern is exercised the same way as the
    Postgres path).
    """

    def __init__(self) -> None:
        self._sources: dict[UUID, CrawlSource] = {}

    def get_by_id(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        source = self._sources.get(source_id)
        if source is None:
            raise NotFoundError("crawl source not found")
        return _source_with_owner(source, owner_id)

    def list_for(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        items = list(self._sources.values())
        items.sort(
            key=lambda s: (s.created_at or datetime.min.replace(tzinfo=UTC), s.id),
            reverse=True,
        )
        return [_source_with_owner(s, owner_id) for s in items[:limit]]

    def save(self, source: CrawlSource) -> CrawlSource:
        self._sources[source.id] = source
        return source

    def update_state(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        state: CrawlSourceState | None = None,
        enabled: bool | None = None,
        trust_status: CrawlPolicyStatus | None = None,
        terms_status: CrawlPolicyStatus | None = None,
        robots_status: CrawlPolicyStatus | None = None,
        adapter_version: str | None = None,
        last_run_at: datetime | None = None,
        last_run_metadata: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> CrawlSource:
        existing = self._sources.get(source_id)
        if existing is None:
            raise NotFoundError("crawl source not found")
        updated = dataclasses.replace(
            existing,
            state=state if state is not None else existing.state,
            enabled=enabled if enabled is not None else existing.enabled,
            trust_status=trust_status if trust_status is not None else existing.trust_status,
            terms_status=terms_status if terms_status is not None else existing.terms_status,
            robots_status=robots_status if robots_status is not None else existing.robots_status,
            adapter_version=adapter_version if adapter_version is not None else existing.adapter_version,
            last_run_at=last_run_at if last_run_at is not None else existing.last_run_at,
            last_run_metadata=(
                dict(last_run_metadata) if last_run_metadata is not None else existing.last_run_metadata
            ),
            updated_at=now or datetime.now(UTC),
        )
        self._sources[source_id] = updated
        return _source_with_owner(updated, owner_id)

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        if source_id not in self._sources:
            raise NotFoundError("crawl source not found")
        del self._sources[source_id]


def _source_with_owner(source: CrawlSource, owner_id: UUID) -> CrawlSource:
    """Stamp the server-resolved owner onto the source (mirrors the Postgres
    repo which has no owner column on ``job_sources``)."""
    if source.owner_id == owner_id:
        return source
    return dataclasses.replace(source, owner_id=owner_id)


class InMemoryCrawlPlanRepository:
    """In-memory crawl-plan-version store mirroring
    :class:`careerops.infrastructure.database.postgres_crawl_repo.PostgresCrawlPlanRepository`.

    Exactly one active version per owner (invariants mirror the DB partial
    unique index ``ix_crawl_plan_versions_owner_active``).
    """

    def __init__(self) -> None:
        self._versions: dict[UUID, CrawlPlanVersion] = {}
        self._active_by_owner: dict[UUID, UUID] = {}

    def get_active_for(self, owner_id: UUID) -> CrawlPlanVersion | None:
        active_id = self._active_by_owner.get(owner_id)
        if active_id is None:
            return None
        return self._versions.get(active_id)

    def get_by_id(self, owner_id: UUID, version_id: UUID) -> CrawlPlanVersion:
        version = self._versions.get(version_id)
        if version is None or version.owner_id != owner_id:
            raise NotFoundError("crawl plan version not found for owner")
        return version

    def list_versions(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlPlanVersion]:
        items = [v for v in self._versions.values() if v.owner_id == owner_id]
        items.sort(key=lambda v: v.version, reverse=True)
        return items[:limit]

    def plan_belongs_to_owner(self, plan_version_id: UUID, owner_id: UUID) -> bool:
        """Transitive-ownership helper for the run repo (Iron Rule 2).

        Public so :class:`InMemoryCrawlRunRepository` can resolve whether a
        plan version belongs to an owner without reaching into this repo's
        private ``_versions`` dict.
        """
        version = self._versions.get(plan_version_id)
        return version is not None and version.owner_id == owner_id

    def create_version(self, plan: CrawlPlanVersion) -> CrawlPlanVersion:
        stored = dataclasses.replace(plan, is_active=False)
        self._versions[plan.id] = stored
        if plan.is_active:
            return self._activate(owner_id=plan.owner_id, version_id=plan.id)
        return stored

    def activate(
        self, owner_id: UUID, version_id: UUID, *, now: datetime | None = None
    ) -> CrawlPlanVersion:
        target = self._versions.get(version_id)
        if target is None or target.owner_id != owner_id:
            raise NotFoundError("crawl plan version not found for owner")
        return self._activate(owner_id=owner_id, version_id=version_id)

    def deactivate_all(self, owner_id: UUID, *, now: datetime | None = None) -> None:
        prior_id = self._active_by_owner.pop(owner_id, None)
        if prior_id is None:
            return
        prior = self._versions.get(prior_id)
        if prior is not None:
            self._versions[prior_id] = dataclasses.replace(prior, is_active=False)

    def _activate(self, *, owner_id: UUID, version_id: UUID) -> CrawlPlanVersion:
        prior_id = self._active_by_owner.get(owner_id)
        if prior_id is not None and prior_id != version_id:
            prior = self._versions.get(prior_id)
            if prior is not None:
                self._versions[prior_id] = dataclasses.replace(prior, is_active=False)
        target = self._versions[version_id]
        self._versions[version_id] = dataclasses.replace(target, is_active=True)
        self._active_by_owner[owner_id] = version_id
        return self._versions[version_id]


class InMemoryCrawlRunRepository:
    """In-memory crawl-run store mirroring
    :class:`careerops.infrastructure.database.postgres_crawl_repo.PostgresCrawlRunRepository`.

    Ownership is transitive via the plan version; a run whose
    ``plan_version_id`` belongs to a different owner is never returned (Iron
    Rule 2). ``run_identity`` uniqueness is enforced in-memory (Iron Rule 4).
    """

    def __init__(self, plans: InMemoryCrawlPlanRepository) -> None:
        # Bound to the plan repo so ownership can be resolved transitively
        # without a separate owner index.
        self._plans = plans
        self._runs: dict[UUID, CrawlRun] = {}
        self._by_identity: dict[str, UUID] = {}

    def _owner_matches(self, plan_version_id: UUID, owner_id: UUID) -> bool:
        return self._plans.plan_belongs_to_owner(plan_version_id, owner_id)

    def create(self, owner_id: UUID, run: CrawlRun) -> CrawlRun:
        if not self._owner_matches(run.plan_version_id, owner_id):
            raise NotFoundError("crawl plan version not found for owner")
        if run.run_identity in self._by_identity:
            raise ConflictError("crawl run identity already exists")
        self._runs[run.id] = run
        self._by_identity[run.run_identity] = run.id
        return run

    def get_by_id(self, owner_id: UUID, run_id: UUID) -> CrawlRun:
        run = self._runs.get(run_id)
        if run is None or not self._owner_matches(run.plan_version_id, owner_id):
            raise NotFoundError("crawl run not found for owner")
        return run

    def get_by_identity(
        self, owner_id: UUID, run_identity: str
    ) -> CrawlRun | None:
        run_id = self._by_identity.get(run_identity)
        if run_id is None:
            return None
        run = self._runs.get(run_id)
        if run is None or not self._owner_matches(run.plan_version_id, owner_id):
            return None
        return run

    def list_for_owner(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlRun]:
        items = [
            r
            for r in self._runs.values()
            if self._owner_matches(r.plan_version_id, owner_id)
        ]
        items.sort(
            key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.id),
            reverse=True,
        )
        return items[:limit]

    def list_for_plan(
        self, owner_id: UUID, plan_version_id: UUID, *, limit: int = 50
    ) -> list[CrawlRun]:
        if not self._owner_matches(plan_version_id, owner_id):
            return []
        items = [r for r in self._runs.values() if r.plan_version_id == plan_version_id]
        items.sort(
            key=lambda r: (r.created_at or datetime.min.replace(tzinfo=UTC), r.id),
            reverse=True,
        )
        return items[:limit]

    def update_terminal(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        state: CrawlRunState,
        counters: CrawlRunCounters | None = None,
        error_category: str | None = None,
        next_eligible_at: datetime | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CrawlRun:
        existing = self._runs.get(run_id)
        if existing is None or not self._owner_matches(existing.plan_version_id, owner_id):
            raise NotFoundError("crawl run not found for owner")
        updated = dataclasses.replace(
            existing,
            state=state,
            counters=counters if counters is not None else existing.counters,
            error_category=error_category if error_category is not None else existing.error_category,
            next_eligible_at=(
                next_eligible_at if next_eligible_at is not None else existing.next_eligible_at
            ),
            started_at=started_at if started_at is not None else existing.started_at,
            ended_at=ended_at if ended_at is not None else existing.ended_at,
        )
        self._runs[run_id] = updated
        return updated
