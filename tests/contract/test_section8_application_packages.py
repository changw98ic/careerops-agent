"""Contract tests: Section 8 job-specific application packages (tasks 8.9-8.10).

Proves the package slice through the service layer with in-memory repos:

- Draft creation binds exact job/profile/resume identities + payload hash (8.2).
- Deterministic requirement/evidence comparison yields explainable gaps (8.3).
- Optional model tailoring is review-only and default-disabled (8.4).
- User edits create NEW immutable versions; the prior version is unchanged (8.5).
- Approval is rejected when claims lack evidence, the resume is unconfirmed, or
  the source is stale (8.6).
- Approval freezes payload_hash + approver + timestamp (8.7).
- Input mutation produces a new draft version, superseding the prior approval
  (invalidation-on-mutation) — the gate requirement (8.10).

Iron rules honored:
- Append-only / reversible (Iron Rule 4): versions never rewritten.
- Model review-only (Iron Rule 2): suggestions never become claims.
- Default-deny (Iron Rule 7): tailoring disabled unless MODEL_TAILORING released.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.application.package_service import (
    PackageApprovalValidationError,
    PackageService,
    ResumeNotEligibleError,
)
from careerops.domain.application_packages import (
    ApplicationPackageVersion,
    PackageAttachment,
    PackageClaimVersion,
    PackageDiffEntry,
)
from careerops.domain.applications import (
    ConfirmationStatus,
    PackageApprovalState,
    ResumeParseStatus,
    ResumeVersion,
)
from careerops.domain.candidates import EvidenceItem, EvidenceKind

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakePackageRepo:
    def __init__(self) -> None:
        self._versions: dict[UUID, ApplicationPackageVersion] = {}
        self._by_app: dict[UUID, list[UUID]] = {}

    def find_latest_package_version(
        self, application_id: UUID
    ) -> ApplicationPackageVersion | None:
        ids = self._by_app.get(application_id, [])
        if not ids:
            return None
        latest = max((self._versions[i] for i in ids), key=lambda v: v.version_number)
        return latest

    def find_package_version(
        self, application_id: UUID, version_id: UUID
    ) -> ApplicationPackageVersion | None:
        v = self._versions.get(version_id)
        if v is None or v.application_id != application_id:
            return None
        return v

    def save_package_version(self, version: ApplicationPackageVersion) -> None:
        self._versions[version.id] = version
        self._by_app.setdefault(version.application_id, []).append(version.id)

    def list_package_versions(
        self, application_id: UUID, *, limit: int = 50
    ) -> list[ApplicationPackageVersion]:
        ids = self._by_app.get(application_id, [])
        versions = [self._versions[i] for i in ids]
        return sorted(versions, key=lambda v: v.version_number, reverse=True)[:limit]


class _FakeResumeRepo:
    def __init__(self, resume: ResumeVersion | None) -> None:
        self.resume = resume

    def find_resume_by_id(
        self, candidate_id: UUID, version_id: UUID
    ) -> ResumeVersion | None:
        if self.resume is None:
            return None
        if self.resume.id != version_id or self.resume.candidate_id != candidate_id:
            return None
        return self.resume


def _confirmed_resume(
    *, candidate_id: UUID | None = None, resume_id: UUID | None = None
) -> ResumeVersion:
    return ResumeVersion(
        id=resume_id or uuid4(),
        candidate_id=candidate_id or uuid4(),
        version_number=1,
        file_reference="store://r.pdf",
        content_hash="a" * 64,
        parse_status=ResumeParseStatus.PARSED,
        confirmation_status=ConfirmationStatus.CONFIRMED,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _make_service(
    *, resume: ResumeVersion | None = None, stale_days: int = 90
) -> tuple[PackageService, _FakePackageRepo]:
    repo = _FakePackageRepo()
    resume_repo = _FakeResumeRepo(resume)
    svc = PackageService(repo, resume_repo, stale_threshold_days=stale_days)  # type: ignore[arg-type]
    return svc, repo


# ---------------------------------------------------------------------------
# (1) Draft creation + payload hash (8.2)
# ---------------------------------------------------------------------------


class TestDraftCreation:
    def test_draft_binds_inputs_and_computes_payload_hash(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        now = datetime(2026, 7, 20, tzinfo=UTC)
        job_v, prof_v = uuid4(), uuid4()

        draft = svc.create_draft(
            application_id=uuid4(),
            candidate_id=cid,
            resume_version_id=resume.id,
            job_version_id=job_v,
            profile_version_id=prof_v,
            cover_letter_text="I am a great fit.",
            claims=(
                PackageClaimVersion("Python engineer", evidence_ids=(uuid4(),)),
            ),
            attachments=(PackageAttachment(name="resume.pdf", content_hash="b" * 64),),
            now=now,
        )
        assert draft.version_number == 1
        assert draft.approval_state is PackageApprovalState.DRAFT
        assert draft.payload_hash is not None and len(draft.payload_hash) == 64
        assert draft.job_version_id == job_v
        assert draft.profile_version_id == prof_v
        assert draft.resume_version_id == resume.id

    def test_version_number_increments(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        now = datetime(2026, 7, 20, tzinfo=UTC)
        app = uuid4()
        v1 = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id, now=now
        )
        v2 = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id, now=now
        )
        assert v1.version_number == 1
        assert v2.version_number == 2


# ---------------------------------------------------------------------------
# (2) Claim requires evidence (8.6) + resume eligibility (8.2/8.6)
# ---------------------------------------------------------------------------


class TestEvidenceAndResumeGates:
    def test_claim_without_evidence_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            PackageClaimVersion("unstated claim", evidence_ids=())

    def test_draft_rejects_unconfirmed_resume(self) -> None:
        cid = uuid4()
        resume = ResumeVersion(
            id=uuid4(), candidate_id=cid, version_number=1,
            file_reference="x", content_hash="a" * 64,
            parse_status=ResumeParseStatus.PARSED,
            confirmation_status=ConfirmationStatus.UNCONFIRMED,
        )
        svc, _ = _make_service(resume=resume)
        with pytest.raises(ResumeNotEligibleError):
            svc.create_draft(
                application_id=uuid4(), candidate_id=cid,
                resume_version_id=resume.id, now=datetime.now(tz=UTC),
            )

    def test_draft_rejects_unparsed_resume(self) -> None:
        cid = uuid4()
        resume = ResumeVersion(
            id=uuid4(), candidate_id=cid, version_number=1,
            file_reference="x", content_hash="a" * 64,
            parse_status=ResumeParseStatus.FAILED,
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        svc, _ = _make_service(resume=resume)
        with pytest.raises(ResumeNotEligibleError):
            svc.create_draft(
                application_id=uuid4(), candidate_id=cid,
                resume_version_id=resume.id, now=datetime.now(tz=UTC),
            )


# ---------------------------------------------------------------------------
# (3) Requirement / evidence comparison + gaps (8.3)
# ---------------------------------------------------------------------------


class TestRequirementComparison:
    def test_unsupported_requirements_become_gaps(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        evidence = [
            EvidenceItem(
                id=uuid4(), candidate_id=cid, kind=EvidenceKind.SKILL,
                name="Python", confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        ]
        gaps = svc.compare_requirements(
            job_structured_data={"skills": ["Python", "Kubernetes"]},
            confirmed_evidence=evidence,
        )
        names = {g.requirement_name for g in gaps}
        assert "Kubernetes" in names, "unsupported requirement must be a gap"
        assert "Python" not in names, "matched requirement is not a gap"
        assert all(g.match_level == "unsupported" for g in gaps)


# ---------------------------------------------------------------------------
# (4) Model tailoring is review-only + default-disabled (8.4)
# ---------------------------------------------------------------------------


class TestTailoringReviewOnly:
    def test_disabled_by_default_returns_empty(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        now = datetime.now(tz=UTC)
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid,
            resume_version_id=resume.id, now=now,
        )
        suggestions = svc.suggest_tailoring(
            application_id=draft.application_id,
            version_id=draft.id,
            confirmed_evidence=[],
        )
        assert suggestions == (), "tailoring must be disabled by default"

    def test_enabled_suggestions_carry_model_source(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        repo = _FakePackageRepo()
        ev_id = uuid4()

        class _Enabled:
            def decide(self, kind: object) -> object:
                class _D:
                    released = True
                return _D()

        def _suggest(
            version: ApplicationPackageVersion, evidence: list[EvidenceItem]
        ) -> tuple[PackageDiffEntry, ...]:
            return (
                PackageDiffEntry(
                    section="summary", proposed_text="Lead with Python", evidence_ids=(ev_id,)
                ),
            )

        svc = PackageService(  # type: ignore[arg-type]
            repo, _FakeResumeRepo(resume),
            capability_resolver=_Enabled(), model_suggester=_suggest,
        )
        now = datetime.now(tz=UTC)
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid,
            resume_version_id=resume.id, now=now,
        )
        suggestions = svc.suggest_tailoring(
            application_id=draft.application_id, version_id=draft.id, confirmed_evidence=[]
        )
        assert len(suggestions) == 1
        assert all(s.source == "model" for s in suggestions), "suggestions are review-only"


# ---------------------------------------------------------------------------
# (5) Diff / new immutable versions (8.5)
# ---------------------------------------------------------------------------


class TestDiffAndVersions:
    def test_apply_edits_creates_new_version_and_leaves_base_unchanged(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, repo = _make_service(resume=resume)
        now = datetime(2026, 7, 20, tzinfo=UTC)
        app = uuid4()
        v1 = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id, now=now
        )
        edits = (PackageDiffEntry(section="summary", proposed_text="Tailored text"),)
        v2 = svc.apply_edits(
            application_id=app, candidate_id=cid, base_version_id=v1.id,
            edits=edits, now=now,
        )
        assert v2.version_number == 2
        assert v2.id != v1.id
        assert v2.diff == edits
        # Base version is unchanged (immutable)
        base = repo.find_package_version(app, v1.id)
        assert base is not None
        assert base.diff == ()
        assert base.version_number == 1


# ---------------------------------------------------------------------------
# (6) Approval validation (8.6) + (7) approval freeze (8.7)
# ---------------------------------------------------------------------------


class TestApproval:
    def test_approve_freezes_payload_and_actor(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        now = datetime(2026, 7, 20, tzinfo=UTC)
        ev = uuid4()
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid, resume_version_id=resume.id,
            claims=(PackageClaimVersion("Python", evidence_ids=(ev,)),), now=now,
        )
        approved = svc.approve(
            application_id=draft.application_id, candidate_id=cid,
            version_id=draft.id, actor_id=str(cid), now=now,
        )
        assert approved.approval_state is PackageApprovalState.APPROVED
        assert approved.approved_at == now
        assert approved.approved_by == str(cid)
        assert approved.payload_hash == draft.payload_hash, "approval freezes the hash"

    def test_approve_rejected_when_claim_lacks_evidence(self) -> None:
        """Even though PackageClaimVersion enforces evidence at construction,
        approval re-validates defensively (and a resume-only draft with no
        claims but an unconfirmed resume still blocks)."""
        from dataclasses import replace as _replace

        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        resume_repo = _FakeResumeRepo(resume)
        svc = PackageService(_FakePackageRepo(), resume_repo)  # type: ignore[arg-type]
        now = datetime.now(tz=UTC)
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid, resume_version_id=resume.id, now=now
        )
        # Simulate the bound resume later becoming unconfirmed.
        resume_repo.resume = _replace(resume, confirmation_status=ConfirmationStatus.UNCONFIRMED)
        reasons = svc.validate_for_approval(
            application_id=draft.application_id, candidate_id=cid,
            version_id=draft.id, now=now,
        )
        assert any("not eligible" in r for r in reasons)
        with pytest.raises(PackageApprovalValidationError):
            svc.approve(
                application_id=draft.application_id, candidate_id=cid,
                version_id=draft.id, actor_id=str(cid), now=now,
            )

    def test_approve_rejected_when_source_stale(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume, stale_days=10)
        old = datetime(2026, 1, 1, tzinfo=UTC)
        now = datetime(2026, 7, 26, tzinfo=UTC)
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid, resume_version_id=resume.id, now=old
        )
        reasons = svc.validate_for_approval(
            application_id=draft.application_id, candidate_id=cid,
            version_id=draft.id, now=now,
        )
        assert any("stale" in r for r in reasons)
        with pytest.raises(PackageApprovalValidationError):
            svc.approve(
                application_id=draft.application_id, candidate_id=cid,
                version_id=draft.id, actor_id=str(cid), now=now,
            )

    def test_approve_idempotent_for_already_approved(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, _ = _make_service(resume=resume)
        now = datetime.now(tz=UTC)
        draft = svc.create_draft(
            application_id=uuid4(), candidate_id=cid, resume_version_id=resume.id,
            claims=(PackageClaimVersion("Python", evidence_ids=(uuid4(),)),), now=now,
        )
        approve_kwargs = dict(
            application_id=draft.application_id, candidate_id=cid,
            version_id=draft.id, actor_id="u", now=now,
        )
        a1 = svc.approve(**approve_kwargs)
        a2 = svc.approve(**approve_kwargs)
        assert a1.id == a2.id and a2.approval_state is PackageApprovalState.APPROVED


# ---------------------------------------------------------------------------
# (8) Invalidation on mutation — the gate (8.7 / 8.10)
# ---------------------------------------------------------------------------


class TestInvalidationOnMutation:
    def test_edit_after_approval_supersedes_approval(self) -> None:
        """Approve v1 → edit → v2 is a DRAFT. The latest version (what the
        workspace binds) is no longer approved, so the prior approval is
        invalidated. This is the exact gate requirement (8.10)."""
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        svc, repo = _make_service(resume=resume)
        now = datetime(2026, 7, 20, tzinfo=UTC)
        app = uuid4()
        v1 = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id,
            claims=(PackageClaimVersion("Python", evidence_ids=(uuid4(),)),), now=now,
        )
        v1_approved = svc.approve(
            application_id=app, candidate_id=cid, version_id=v1.id,
            actor_id=str(cid), now=now,
        )
        assert v1_approved.is_approved

        # Mutation: user edits → new version
        v2 = svc.apply_edits(
            application_id=app, candidate_id=cid, base_version_id=v1.id,
            edits=(PackageDiffEntry(section="summary", proposed_text="new text"),), now=now,
        )
        # Latest version is v2, which is a DRAFT (not approved)
        latest = svc.get_latest(app)
        assert latest is not None
        assert latest.id == v2.id
        assert latest.approval_state is PackageApprovalState.DRAFT
        assert not latest.is_approved, "mutation must invalidate the current approval"

        # v1 remains approved and immutable in history (append-only)
        v1_after = repo.find_package_version(app, v1.id)
        assert v1_after is not None
        assert v1_after.approval_state is PackageApprovalState.APPROVED


# ---------------------------------------------------------------------------
# (9) Vertical package slice (8.10)
# ---------------------------------------------------------------------------


class TestPackageSlice:
    """create a job-specific diff → reject unsupported claims → approve an
    exact payload → prove any input mutation invalidates approval."""

    def test_full_package_slice(self) -> None:
        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        ev = EvidenceItem(
            id=uuid4(), candidate_id=cid, kind=EvidenceKind.SKILL, name="Python",
            confirmation_status=ConfirmationStatus.CONFIRMED,
        )
        svc, repo = _make_service(resume=resume)
        app = uuid4()
        now = datetime(2026, 7, 20, tzinfo=UTC)

        # (a) Draft with a job-specific diff bound to exact inputs + a claim
        # bound to evidence.
        draft = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id,
            job_version_id=uuid4(), profile_version_id=uuid4(),
            claims=(PackageClaimVersion("5 years Python", evidence_ids=(ev.id,)),),
            cover_letter_text="Tailored for this role.",
            job_structured_data={"skills": ["Python", "Kubernetes"]},
            confirmed_evidence=[ev],
            now=now,
        )
        assert draft.payload_hash is not None
        # The Kubernetes gap is captured
        assert any(g.requirement_name == "Kubernetes" for g in draft.requirement_gaps)

        # (b) Reject an unsupported claim (no evidence) — construction raises.
        with pytest.raises(ValueError):
            PackageClaimVersion("invented skill", evidence_ids=())

        # (c) Approve the exact payload.
        approved = svc.approve(
            application_id=app, candidate_id=cid, version_id=draft.id,
            actor_id=str(cid), now=now,
        )
        frozen_hash = approved.payload_hash
        assert approved.is_approved

        # (d) Any input mutation (here: an edit) creates a new draft version and
        # invalidates the current approval.
        svc.apply_edits(
            application_id=app, candidate_id=cid, base_version_id=draft.id,
            edits=(PackageDiffEntry(section="summary", proposed_text="edited"),), now=now,
        )
        latest = svc.get_latest(app)
        assert latest is not None and not latest.is_approved
        # The approved version's frozen hash is preserved in history.
        approved_history = repo.find_package_version(app, approved.id)
        assert approved_history is not None
        assert approved_history.payload_hash == frozen_hash


# ---------------------------------------------------------------------------
# (10) Workspace binding store coherence (7.5 <-> Section 8)
# ---------------------------------------------------------------------------


class TestBindingStoreCoherence:
    """The PackageServiceBindingStore (consumed by the workspace) reads the
    latest version, so approval + invalidation-on-mutation are visible to the
    submission gate without a second write path."""

    def test_binding_reflects_approval_then_invalidation(self) -> None:
        from careerops.application.application_workspace import PackageServiceBindingStore

        cid = uuid4()
        resume = _confirmed_resume(candidate_id=cid)
        ev = uuid4()
        svc, _ = _make_service(resume=resume)
        store = PackageServiceBindingStore(svc)
        app = uuid4()
        now = datetime(2026, 7, 20, tzinfo=UTC)

        # No package yet -> no binding.
        assert store.get_binding(app) is None

        draft = svc.create_draft(
            application_id=app, candidate_id=cid, resume_version_id=resume.id,
            claims=(PackageClaimVersion("Python", evidence_ids=(ev,)),), now=now,
        )
        svc.approve(
            application_id=app, candidate_id=cid, version_id=draft.id,
            actor_id=str(cid), now=now,
        )
        binding = store.get_binding(app)
        assert binding is not None
        assert binding.approval_state == "approved"
        assert binding.package_version_id == draft.id
        assert binding.payload_hash == draft.payload_hash

        # Edit -> new draft version -> binding reflects DRAFT (invalidated).
        svc.apply_edits(
            application_id=app, candidate_id=cid, base_version_id=draft.id,
            edits=(PackageDiffEntry(section="summary", proposed_text="x"),), now=now,
        )
        invalidated = store.get_binding(app)
        assert invalidated is not None
        assert invalidated.approval_state == "draft", "mutation invalidates the binding"
        assert invalidated.package_version_id != draft.id
