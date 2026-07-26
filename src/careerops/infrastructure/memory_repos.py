"""In-memory repository implementations for non-production API routes.

These back the matching, jobs, and applications routers so the REST API
returns real (empty-to-start) data instead of silently returning 503.
In PRODUCTION a proper Postgres-backed implementation should replace each
class; until then, the in-memory stores are correct for development and
integration testing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from careerops.domain.applications import (
    Application,
    ApplicationEvent,
    ApplicationPackage,
    FollowUpReminder,
    ResumeVersion,
)
from careerops.domain.candidates import (
    CompensationRecord,
    EvidenceItem,
    MatchResult,
    RemoteEligibility,
)
from careerops.domain.contacts import RecruitingContact
from careerops.domain.jobs import (
    AggregateState,
    CanonicalJob,
    JobMergeDecision,
    JobPosting,
    JobPostingVersion,
    PostingSourceState,
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
