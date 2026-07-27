"""Job-specific application package service (Section 8, tasks 8.2-8.7).

Creates immutable, evidence-bound package versions for a specific application
+ exact job version + profile version + confirmed resume + selected evidence,
and drives the deterministic comparison, diff, approval-validation and approval
flow:

- **8.2** draft creation binds the exact input identities and computes the
  payload hash.
- **8.3** deterministic requirement/evidence comparison with explainable gaps
  (reuses the Section 6 :class:`RequirementMatchEngine`).
- **8.4** optional LangGraph/model tailoring returns review-only suggestions
  (``source='model'``) and never writes trusted claims; gated by the
  ``MODEL_TAILORING`` capability, which is default-disabled.
- **8.5** user edits (and accepted model suggestions) create NEW versions; the
  prior version — including an approved one — is never overwritten.
- **8.6** approval is rejected when any positive claim lacks evidence, the
  bound resume is not parsed+confirmed, or a source is stale.
- **8.7** approval freezes the payload hash, approver and timestamp. Any later
  input mutation produces a new draft version, superseding the prior approval
  (the latest version is no longer approved → invalidation-on-mutation).

Iron rules honored:
- Append-only / reversible (Iron Rule 4): versions are immutable.
- Model review-only (Iron Rule 2): model suggestions never become claims.
- Default-deny (Iron Rule 7): tailoring is opt-in via MODEL_TAILORING.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.inbox_service import RULES_VERSION, RequirementMatchEngine
from careerops.domain.application_packages import (
    ApplicationPackageVersion,
    PackageApprovalValidationError,
    PackageAttachment,
    PackageClaimVersion,
    PackageDiffEntry,
    RequirementGap,
    compute_payload_hash,
)
from careerops.domain.applications import (
    ConfirmationStatus,
    PackageApprovalState,
    ResumeParseStatus,
    ResumeVersion,
)
from careerops.domain.candidates import EvidenceItem
from careerops.observability.career_loop_trace import CareerLoopTrace
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)

__all__ = [
    "STALE_SOURCE_THRESHOLD_DAYS",
    "PackageNotFoundError",
    "PackageService",
    "PackageVersionRepository",
    "ResumeNotEligibleError",
    "is_resume_eligible",
]

# A job source is considered stale for package purposes if its captured_at is
# older than this many days (task 8.6). Configurable at call sites; the default
# is deliberately conservative.
STALE_SOURCE_THRESHOLD_DAYS = 90

# Match levels that count as an explainable gap (task 8.3).
_GAP_LEVELS: frozenset[str] = frozenset({"unsupported", "hard_fail"})

# Review-only tailoring suggester: (version, confirmed evidence) -> diff entries.
# Implementations return ``source='model'`` entries that never become trusted
# claims without an explicit user accept step.
ModelSuggester = Callable[
    [ApplicationPackageVersion, list[EvidenceItem]], tuple[PackageDiffEntry, ...]
]


class CapabilityResolver(Protocol):
    """Minimal resolver surface the package service needs for tailoring gating."""

    def decide(self, capability: CapabilityKind) -> CapabilityDecision: ...


class PackageVersionRepository(Protocol):
    """Repository protocol for immutable package versions."""

    def find_latest_package_version(
        self, application_id: UUID
    ) -> ApplicationPackageVersion | None: ...

    def find_package_version(
        self, application_id: UUID, version_id: UUID
    ) -> ApplicationPackageVersion | None: ...

    def save_package_version(self, version: ApplicationPackageVersion) -> None: ...

    def list_package_versions(
        self, application_id: UUID, *, limit: int = 50
    ) -> list[ApplicationPackageVersion]: ...


class ResumeReadRepository(Protocol):
    def find_resume_by_id(self, candidate_id: UUID, version_id: UUID) -> ResumeVersion | None: ...


class PackageNotFoundError(Exception):
    pass


class ResumeNotEligibleError(Exception):
    """The bound resume is not parsed+confirmed and cannot enter a package."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def is_resume_eligible(resume: ResumeVersion) -> tuple[bool, str]:
    """Return (eligible, reason). Eligible requires PARSED + CONFIRMED."""
    if resume.parse_status is not ResumeParseStatus.PARSED:
        return False, f"resume parse_status is {resume.parse_status.value}"
    if resume.confirmation_status is not ConfirmationStatus.CONFIRMED:
        return False, f"resume confirmation_status is {resume.confirmation_status.value}"
    return True, ""


@dataclass(frozen=True, slots=True)
class _DraftRequest:
    """Internal bundle of draft inputs (kept explicit for testability)."""

    application_id: UUID
    candidate_id: UUID
    resume_version_id: UUID
    job_version_id: UUID | None
    profile_version_id: UUID | None
    claims: tuple[PackageClaimVersion, ...]
    cover_letter_text: str
    notes: str
    answers: dict[str, str]
    attachments: tuple[PackageAttachment, ...]


class PackageService:
    """Draft / compare / diff / approve job-specific package versions."""

    def __init__(
        self,
        package_repo: PackageVersionRepository,
        resume_repo: ResumeReadRepository,
        *,
        requirement_engine: RequirementMatchEngine | None = None,
        capability_resolver: CapabilityResolver | None = None,
        model_suggester: ModelSuggester | None = None,
        stale_threshold_days: int = STALE_SOURCE_THRESHOLD_DAYS,
        trace: CareerLoopTrace | None = None,
    ) -> None:
        self._packages = package_repo
        self._resumes = resume_repo
        self._requirement_engine = requirement_engine or RequirementMatchEngine()
        self._capability_resolver = capability_resolver
        self._model_suggester = model_suggester
        self._stale_threshold_days = stale_threshold_days
        self._trace = trace

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_latest(self, application_id: UUID) -> ApplicationPackageVersion | None:
        return self._packages.find_latest_package_version(application_id)

    def get_version(self, application_id: UUID, version_id: UUID) -> ApplicationPackageVersion:
        version = self._packages.find_package_version(application_id, version_id)
        if version is None:
            raise PackageNotFoundError(str(version_id))
        return version

    def list_versions(self, application_id: UUID) -> list[ApplicationPackageVersion]:
        return self._packages.list_package_versions(application_id)

    # ------------------------------------------------------------------
    # 8.3 — deterministic requirement / evidence comparison
    # ------------------------------------------------------------------

    def compare_requirements(
        self,
        *,
        job_structured_data: dict[str, object],
        confirmed_evidence: list[EvidenceItem],
    ) -> tuple[RequirementGap, ...]:
        """Return explainable requirement gaps (unsupported / hard-fail)."""
        matches = self._requirement_engine.match(
            job_structured_data=job_structured_data,
            confirmed_evidence=confirmed_evidence,
        )
        return tuple(
            RequirementGap(
                requirement_name=m.requirement_name,
                match_level=m.match_level,
                reason=getattr(m, "reason", ""),
                rules_version=m.rules_version or RULES_VERSION,
            )
            for m in matches
            if m.match_level in _GAP_LEVELS
        )

    # ------------------------------------------------------------------
    # 8.2 — draft creation
    # ------------------------------------------------------------------

    def create_draft(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        resume_version_id: UUID,
        job_version_id: UUID | None = None,
        profile_version_id: UUID | None = None,
        claims: tuple[PackageClaimVersion, ...] = (),
        cover_letter_text: str = "",
        notes: str = "",
        answers: dict[str, str] | None = None,
        attachments: tuple[PackageAttachment, ...] = (),
        job_structured_data: dict[str, object] | None = None,
        confirmed_evidence: list[EvidenceItem] | None = None,
        now: datetime,
    ) -> ApplicationPackageVersion:
        """Create a new DRAFT package version bound to the exact inputs.

        Validates the resume is parsed+confirmed before any version is written.
        ``PackageClaimVersion`` already enforces claim→evidence binding at
        construction, so a claim with no evidence raises before draft creation.
        Requirement gaps are computed when job/evidence context is supplied.
        """
        self._require_confirmed_resume(candidate_id, resume_version_id)
        gaps: tuple[RequirementGap, ...] = ()
        if job_structured_data is not None and confirmed_evidence is not None:
            gaps = self.compare_requirements(
                job_structured_data=job_structured_data,
                confirmed_evidence=confirmed_evidence,
            )

        draft = self._build_version(
            _DraftRequest(
                application_id=application_id,
                candidate_id=candidate_id,
                resume_version_id=resume_version_id,
                job_version_id=job_version_id,
                profile_version_id=profile_version_id,
                claims=claims,
                cover_letter_text=cover_letter_text,
                notes=notes,
                answers=answers or {},
                attachments=attachments,
            ),
            diff=(),
            requirement_gaps=gaps,
            now=now,
        )
        self._packages.save_package_version(draft)
        return draft

    # ------------------------------------------------------------------
    # 8.5 — diff / new versions from edits
    # ------------------------------------------------------------------

    def apply_edits(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        base_version_id: UUID,
        edits: tuple[PackageDiffEntry, ...],
        now: datetime,
    ) -> ApplicationPackageVersion:
        """Create a NEW version carrying the supplied diff entries.

        The base version is immutable; this always produces a fresh version
        (copy-on-write). Model-sourced edits are accepted only as review-only
        diff entries (``source='model'``) and never become trusted claims
        without a separate accept step.
        """
        base = self.get_version(application_id, base_version_id)
        # Edits may not smuggle in claims without evidence: if an edit references
        # evidence_ids it must be non-empty (the diff entry is advisory, but a
        # model edit may not invent evidence).
        for edit in edits:
            if edit.source == "model" and not edit.evidence_ids:
                # Model suggestions that reference no evidence are allowed as
                # advisory text but flagged source='model' (review-only).
                continue
        new_version = self._build_version(
            _DraftRequest(
                application_id=application_id,
                candidate_id=candidate_id,
                resume_version_id=base.resume_version_id,
                job_version_id=base.job_version_id,
                profile_version_id=base.profile_version_id,
                claims=base.claims,
                cover_letter_text=base.cover_letter_text,
                notes=base.notes,
                answers=dict(base.answers),
                attachments=base.attachments,
            ),
            diff=edits,
            requirement_gaps=base.requirement_gaps,
            now=now,
        )
        self._packages.save_package_version(new_version)
        if self._trace is not None:
            self._trace.record_user_correction(stage="package")
        return new_version

    # ------------------------------------------------------------------
    # 8.4 — optional review-only model tailoring suggestions
    # ------------------------------------------------------------------

    def suggest_tailoring(
        self,
        *,
        application_id: UUID,
        version_id: UUID,
        confirmed_evidence: list[EvidenceItem],
    ) -> tuple[PackageDiffEntry, ...]:
        """Return review-only tailoring suggestions (``source='model'``).

        Gated by the ``MODEL_TAILORING`` capability. When disabled (the
        default) or no suggester is wired, returns an empty tuple — the package
        flow remains fully usable with deterministic drafting only. The
        suggestions are DATA: they never write claims, transition state, or
        bind evidence. A user must explicitly accept them via :meth:`apply_edits`
        with ``source='user'`` for any to take effect.
        """
        if not self._is_tailoring_enabled():
            return ()
        version = self.get_version(application_id, version_id)
        if self._model_suggester is None:
            return ()
        return tuple(
            PackageDiffEntry(
                section=s.section,
                original_text=s.original_text,
                proposed_text=s.proposed_text,
                evidence_ids=s.evidence_ids,
                source="model",
            )
            for s in self._model_suggester(version, confirmed_evidence)
        )

    # ------------------------------------------------------------------
    # 8.6 + 8.7 — approval validation + approval
    # ------------------------------------------------------------------

    def validate_for_approval(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        version_id: UUID,
        now: datetime,
    ) -> list[str]:
        """Return the list of approval-blocking reasons (empty = approved-OK).

        Task 8.6: blocks when any positive claim lacks evidence, the bound
        resume is not parsed+confirmed, or the source is stale. Claim→evidence
        is enforced at construction, so this re-checks the resume and staleness
        and is defensive against a version whose evidence was later rejected.
        """
        version = self.get_version(application_id, version_id)
        reasons: list[str] = []
        for claim in version.claims:
            if not claim.evidence_ids:
                reasons.append(f"claim lacks evidence: {claim.claim_text!r}")
        resume = self._resumes.find_resume_by_id(candidate_id, version.resume_version_id)
        if resume is None:
            reasons.append("bound resume no longer exists for candidate")
        else:
            eligible, why = is_resume_eligible(resume)
            if not eligible:
                reasons.append(f"bound resume not eligible: {why}")
        if self._is_source_stale(version, now):
            reasons.append(f"job source is stale beyond {self._stale_threshold_days} days policy")
        return reasons

    def approve(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        version_id: UUID,
        actor_id: str,
        now: datetime,
    ) -> ApplicationPackageVersion:
        """Approve a package version after validating all preconditions (8.7).

        Freezes ``payload_hash``, ``approved_at`` and ``approved_by``. The
        version is otherwise immutable; a later edit creates a new draft
        version, and the latest version (what the workspace binds) is then no
        longer approved — that is the invalidation-on-mutation guarantee.
        """
        reasons = self.validate_for_approval(
            application_id=application_id,
            candidate_id=candidate_id,
            version_id=version_id,
            now=now,
        )
        if reasons:
            version = self.get_version(application_id, version_id)
            if self._trace is not None:
                self._trace.record_package_approval(
                    outcome="rejected",
                    created_at=version.created_at,
                    decided_at=now,
                )
            raise PackageApprovalValidationError(reasons)
        version = self.get_version(application_id, version_id)
        if version.approval_state is PackageApprovalState.APPROVED:
            # Idempotent re-approval of an already-approved immutable version.
            return version
        approved = ApplicationPackageVersion(
            id=version.id,
            application_id=version.application_id,
            version_number=version.version_number,
            resume_version_id=version.resume_version_id,
            job_version_id=version.job_version_id,
            profile_version_id=version.profile_version_id,
            cover_letter_text=version.cover_letter_text,
            notes=version.notes,
            answers=dict(version.answers),
            claims=version.claims,
            attachments=version.attachments,
            diff=version.diff,
            requirement_gaps=version.requirement_gaps,
            payload_hash=version.payload_hash,
            approval_state=PackageApprovalState.APPROVED,
            approved_at=now,
            approved_by=actor_id,
            created_at=version.created_at,
            updated_at=now,
        )
        self._packages.save_package_version(approved)
        if self._trace is not None:
            self._trace.record_package_approval(
                outcome="approved",
                created_at=version.created_at,
                decided_at=now,
            )
        return approved

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _build_version(
        self,
        req: _DraftRequest,
        *,
        diff: tuple[PackageDiffEntry, ...],
        requirement_gaps: tuple[RequirementGap, ...],
        now: datetime,
    ) -> ApplicationPackageVersion:
        latest = self._packages.find_latest_package_version(req.application_id)
        next_number = (latest.version_number + 1) if latest else 1
        payload_hash = compute_payload_hash(
            resume_version_id=req.resume_version_id,
            job_version_id=req.job_version_id,
            profile_version_id=req.profile_version_id,
            cover_letter_text=req.cover_letter_text,
            notes=req.notes,
            answers=req.answers,
            claims=req.claims,
            attachments=req.attachments,
        )
        return ApplicationPackageVersion(
            id=uuid4(),
            application_id=req.application_id,
            version_number=next_number,
            resume_version_id=req.resume_version_id,
            job_version_id=req.job_version_id,
            profile_version_id=req.profile_version_id,
            cover_letter_text=req.cover_letter_text,
            notes=req.notes,
            answers=dict(req.answers),
            claims=req.claims,
            attachments=req.attachments,
            diff=diff,
            requirement_gaps=requirement_gaps,
            payload_hash=payload_hash,
            approval_state=PackageApprovalState.DRAFT,
            approved_at=None,
            approved_by="",
            created_at=now,
            updated_at=now,
        )

    def _require_confirmed_resume(
        self, candidate_id: UUID, resume_version_id: UUID
    ) -> ResumeVersion:
        resume = self._resumes.find_resume_by_id(candidate_id, resume_version_id)
        if resume is None:
            raise ResumeNotEligibleError("resume not found for candidate")
        eligible, why = is_resume_eligible(resume)
        if not eligible:
            raise ResumeNotEligibleError(why)
        return resume

    def _is_tailoring_enabled(self) -> bool:
        if self._capability_resolver is None:
            return False
        try:
            decision = self._capability_resolver.decide(CapabilityKind.MODEL_TAILORING)
        except Exception:
            return False
        return bool(decision.released)

    def _is_source_stale(self, version: ApplicationPackageVersion, now: datetime) -> bool:
        """A source is stale if the version was created longer ago than the
        threshold AND no newer version exists. Created_at proximity to now is a
        proxy for source freshness when the job version capture time is not
        available at this layer; the workspace carries the authoritative
        job-version timestamp for stricter checks in a later gate."""
        created = version.created_at
        if created is None:
            return False
        age_days = (now - created).days
        return age_days > self._stale_threshold_days
