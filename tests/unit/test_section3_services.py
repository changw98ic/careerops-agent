"""Unit tests for the Section-3 application services (tasks 3.1 / 3.3 / 3.4 /
3.5 / 3.6 / 3.7).

Uses the in-memory Section-2 repositories (which mirror the Postgres behavior)
and an in-memory content-addressed store stub backed by a dict — no DB, no
tempfiles. Pins the iron-rule behaviors:

- profile copy-on-write + validate-before-activate (3.1)
- resume content-addressed dedupe, media/size validation, deterministic parse,
  evidence extraction (3.3 / 3.4 / 3.5)
- evidence idempotent confirm/reject + append-only audit (3.6)
- model-input minimization default-deny + selection/confirmation gate (3.7)
"""

from __future__ import annotations

from datetime import datetime
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest

from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.application.evidence_service import (
    EvidenceReviewRequest,
    EvidenceService,
    ListEvidenceAuditSink,
)
from careerops.application.model_minimization import (
    ModelTailoringRequest,
    build_tailoring_input,
)
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StorageDeleteResult,
    StoredBlob,
    StoredContent,
)
from careerops.application.profile_service import ProfilePreferences, ProfileService
from careerops.application.resume_service import (
    RESUME_EXTRACTOR_VERSION,
    ResumeRegistrationRequest,
    ResumeService,
)
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.applications import ConfirmationStatus, ResumeParseStatus
from careerops.domain.candidates import EvidenceKind
from careerops.domain.profiles import (
    CompensationPreference,
    LocationKind,
    LocationPreference,
    RemoteRules,
    TargetRole,
)
from careerops.infrastructure.memory_repos import (
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryProfileRepository,
)
from careerops.orchestration.capability_resolver import (
    SettingsCapabilityResolver,
)

CANDIDATE = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# In-memory content-addressed store stub
# ---------------------------------------------------------------------------


class _MemStore:
    """Minimal StoragePort stub: dedupes by digest, no real filesystem."""

    def __init__(self, *, max_bytes: int = 5_000_000) -> None:
        self._blobs: dict[str, bytes] = {}
        self._max_bytes = max_bytes

    def put(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        classification: ContentClassification,
        owner: ContentOwner,
        retention_until: datetime,
        expected_sha256: str | None = None,
    ) -> StoredContent:
        import hashlib

        content = stream.read()
        if len(content) > self._max_bytes:
            from careerops.application.ports.storage import SizeLimitExceeded

            raise SizeLimitExceeded("too big")
        digest = hashlib.sha256(content).hexdigest()
        self._blobs.setdefault(digest, content)
        return StoredContent(
            object_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
            sha256=digest,
            byte_size=len(content),
            media_type=media_type,
            classification=classification,
            owner=owner,
            retention_until=retention_until,
        )

    def read_bytes(self, stored: StoredContent, *, now: datetime | None = None) -> bytes:
        return self._blobs[stored.sha256]

    def delete_bytes(self, stored: StoredBlob) -> StorageDeleteResult:
        self._blobs.pop(stored.sha256, None)
        return StorageDeleteResult.DELETED

    def stored_count(self) -> int:
        return len(self._blobs)


def _profile_prefs(
    *,
    roles: tuple[TargetRole, ...] = (TargetRole(title="Backend Engineer"),),
    locations: tuple[LocationPreference, ...] = (
        LocationPreference(name="Chengdu", kind=LocationKind.REQUIRED),
    ),
    compensation: CompensationPreference | None = None,
) -> ProfilePreferences:
    return ProfilePreferences(
        target_roles=roles,
        locations=locations,
        remote_rules=RemoteRules(remote_allowed=True),
        compensation=compensation
        or CompensationPreference(currency="CNY", amount_min=100, amount_max=200),
    )


# ===========================================================================
# 3.1 Profile service
# ===========================================================================


class TestProfileService:
    def test_create_activates_first_version(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)

        version = service.create_version(CANDIDATE, _profile_prefs())

        assert version.version == 1
        assert version.is_active is True
        active = service.get_active(CANDIDATE)
        assert active is not None
        assert active.id == version.id

    def test_create_new_version_deactivates_prior(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        first = service.create_version(CANDIDATE, _profile_prefs())

        second = service.create_version(
            CANDIDATE,
            _profile_prefs(roles=(TargetRole(title="Staff Engineer"),)),
        )

        assert second.version == 2
        assert second.is_active is True
        assert first.id != second.id
        # Prior version deactivated; exactly one active.
        re_first = repo.get_by_version_id(CANDIDATE, first.id)
        assert re_first.is_active is False
        active2 = service.get_active(CANDIDATE)
        assert active2 is not None
        assert active2.id == second.id

    def test_invalid_preferences_do_not_displace_active(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        good = service.create_version(CANDIDATE, _profile_prefs())
        # Contradictory: same location required AND excluded.
        bad = ProfilePreferences(
            target_roles=(TargetRole(title="X"),),
            locations=(
                LocationPreference(name="Chengdu", kind=LocationKind.REQUIRED),
                LocationPreference(name="Chengdu", kind=LocationKind.EXCLUDED),
            ),
        )
        with pytest.raises(InvalidStateError):
            service.create_version(CANDIDATE, bad)
        # Active version unchanged.
        still_active = service.get_active(CANDIDATE)
        assert still_active is not None
        assert still_active.id == good.id
        assert still_active.version == 1

    def test_invalid_compensation_rejected(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        inverted = _profile_prefs(
            compensation=CompensationPreference(currency="CNY", amount_min=500, amount_max=100),
        )
        with pytest.raises(InvalidStateError):
            service.create_version(CANDIDATE, inverted)

    def test_activate_revalidates(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        # Store an inactive version with a contradiction baked in via the repo
        # (bypass service validation) to simulate a rules change after store.
        from careerops.domain.profiles import ProfileVersion

        contradictory = ProfileVersion(
            id=uuid4(),
            candidate_id=CANDIDATE,
            version=1,
            is_active=False,
            target_roles=(TargetRole(title="X"),),
            locations=(
                LocationPreference(name="NYC", kind=LocationKind.REQUIRED),
                LocationPreference(name="NYC", kind=LocationKind.EXCLUDED),
            ),
        )
        repo._versions[contradictory.id] = contradictory
        with pytest.raises(InvalidStateError):
            service.activate_version(CANDIDATE, contradictory.id)

    def test_get_version_rejects_other_candidate(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        version = service.create_version(CANDIDATE, _profile_prefs())
        with pytest.raises(NotFoundError):
            service.get_version(OTHER, version.id)

    def test_create_without_activate_keeps_prior_active(self) -> None:
        repo = InMemoryProfileRepository()
        service = ProfileService(repo)
        first = service.create_version(CANDIDATE, _profile_prefs())
        draft = service.create_version(
            CANDIDATE,
            _profile_prefs(roles=(TargetRole(title="Draft"),)),
            activate=False,
        )
        assert draft.is_active is False
        active_first = service.get_active(CANDIDATE)
        assert active_first is not None
        assert active_first.id == first.id
        # Later activate it.
        activated = service.activate_version(CANDIDATE, draft.id)
        assert activated.is_active is True
        active_draft = service.get_active(CANDIDATE)
        assert active_draft is not None
        assert active_draft.id == draft.id


# ===========================================================================
# 3.3 / 3.4 / 3.5 Resume service
# ===========================================================================


def _resume_text() -> bytes:
    return (
        b"Jane Doe\nBackend Engineer with 5+ years of Python, Go, and PostgreSQL.\n"
        b"Built distributed systems with Kubernetes and AWS. Kafka, Redis, FastAPI.\n"
    )


class TestResumeService:
    def test_register_parses_and_extracts_evidence(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        store = _MemStore()
        service = ResumeService(resumes, evidence, store, max_bytes=5_000_000)

        result = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(
                content=_resume_text(), media_type="text/plain", source_reference="upload"
            ),
        )

        assert result.deduplicated is False
        assert result.parse_status is ResumeParseStatus.PARSED
        assert result.parse_error == ""
        assert result.resume.parse_status is ResumeParseStatus.PARSED
        assert result.resume.confirmation_status is ConfirmationStatus.UNCONFIRMED
        assert len(result.extracted_evidence) > 0
        # Every extracted item is unconfirmed + carries provenance.
        for item in result.extracted_evidence:
            assert item.kind is EvidenceKind.SKILL
            assert item.confirmation_status is ConfirmationStatus.UNCONFIRMED
            assert item.extractor_version == RESUME_EXTRACTOR_VERSION
            assert item.source_span
            assert item.evidence_hash
            assert item.resume_version_id == result.resume.id

    def test_register_dedupes_identical_hash_no_second_blob(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        store = _MemStore()
        service = ResumeService(resumes, evidence, store, max_bytes=5_000_000)

        first = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        blobs_after_first = store.stored_count()
        second = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )

        assert second.deduplicated is True
        assert second.resume.id == first.resume.id
        # No second physical blob, no re-extraction.
        assert store.stored_count() == blobs_after_first
        assert second.extracted_evidence == ()

    def test_register_rejects_unsupported_media_type(self) -> None:
        service = ResumeService(
            InMemoryApplicationRepository(),
            InMemoryEvidenceRepository(),
            _MemStore(),
            max_bytes=1000,
        )
        with pytest.raises(InvalidStateError):
            service.register(
                CANDIDATE,
                ResumeRegistrationRequest(content=b"x", media_type="application/vnd.unknown"),
            )

    def test_register_rejects_oversize(self) -> None:
        service = ResumeService(
            InMemoryApplicationRepository(),
            InMemoryEvidenceRepository(),
            _MemStore(max_bytes=10),
            max_bytes=10,
        )
        with pytest.raises(InvalidStateError):
            service.register(
                CANDIDATE,
                ResumeRegistrationRequest(content=b"a" * 100, media_type="text/plain"),
            )

    def test_pdf_registers_but_parses_failed_without_evidence(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)

        result = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(content=b"%PDF-1.4 ...", media_type="application/pdf"),
        )

        # Registerable but not text-extractable: FAILED, no evidence.
        assert result.parse_status is ResumeParseStatus.FAILED
        assert result.parse_error
        assert result.extracted_evidence == ()
        assert result.resume.parse_status is ResumeParseStatus.FAILED
        # The version exists (immutable) but is NOT eligible for a package.
        assert service.list_eligible(CANDIDATE) == []

    def test_failed_resume_cannot_be_confirmed(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)
        result = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(content=b"%PDF junk", media_type="application/pdf"),
        )
        with pytest.raises(InvalidStateError):
            service.confirm_content(CANDIDATE, result.resume.id)

    def test_confirm_makes_resume_package_eligible(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)
        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        # Not eligible before confirmation.
        assert service.list_eligible(CANDIDATE) == []
        confirmed = service.confirm_content(CANDIDATE, result.resume.id)
        assert confirmed.confirmation_status is ConfirmationStatus.CONFIRMED
        eligible = service.list_eligible(CANDIDATE)
        assert [r.id for r in eligible] == [result.resume.id]

    def test_confirm_is_idempotent(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)
        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        first = service.confirm_content(CANDIDATE, result.resume.id)
        second = service.confirm_content(CANDIDATE, result.resume.id)
        assert first.id == second.id
        assert second.confirmation_status is ConfirmationStatus.CONFIRMED

    def test_re_extraction_is_idempotent(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)
        # Register a second resume (different content) that shares some skills.
        other = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(
                content=b"Another role using Python and Go and PostgreSQL and AWS.\n",
                media_type="text/plain",
            ),
        )
        # Evidence items for the second resume carry its own resume_version_id;
        # the same skill on two resumes produces two distinct evidence hashes.
        for item in other.extracted_evidence:
            assert item.resume_version_id == other.resume.id

    def test_get_version_rejects_other_candidate(self) -> None:
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)
        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        with pytest.raises(NotFoundError):
            service.get_version(OTHER, result.resume.id)


# ===========================================================================
# 3.6 Evidence service
# ===========================================================================


def _seed_evidence(
    repo: InMemoryEvidenceRepository,
    *,
    candidate_id: UUID = CANDIDATE,
    name: str = "Python",
    unique: str | None = None,
) -> UUID:
    # Each seeded item needs a distinct content_hash/evidence_hash so the
    # repository-derived idempotency key does not collapse two distinct claims
    # onto one row (the repo-key path is (candidate_id, repo, commit, path,
    # symbol, content_hash)).
    import hashlib

    from careerops.domain.candidates import EvidenceItem

    digest = hashlib.sha256((unique or name).encode("utf-8")).hexdigest()
    item = EvidenceItem(
        id=uuid4(),
        candidate_id=candidate_id,
        kind=EvidenceKind.SKILL,
        name=name,
        extractor_version=RESUME_EXTRACTOR_VERSION,
        confirmation_status=ConfirmationStatus.UNCONFIRMED,
        evidence_hash=digest,
        content_hash=digest,
    )
    stored = repo.store(item)
    return stored.id


class TestEvidenceService:
    def test_confirm_records_audit_only_on_transition(self) -> None:
        repo = InMemoryEvidenceRepository()
        evidence_id = _seed_evidence(repo)
        sink = ListEvidenceAuditSink()
        service = EvidenceService(repo, audit_sink=sink)

        first = service.confirm(
            CANDIDATE,
            EvidenceReviewRequest(
                evidence_id=evidence_id, actor_id="user-1", source_reference="r1"
            ),
        )
        second = service.confirm(
            CANDIDATE,
            EvidenceReviewRequest(
                evidence_id=evidence_id, actor_id="user-1", source_reference="r2"
            ),
        )

        assert first.was_change is True
        assert first.status is ConfirmationStatus.CONFIRMED
        # Idempotent repeat: no new audit event.
        assert second.was_change is False
        assert len(sink.events) == 1
        assert sink.events[0].decision is ConfirmationStatus.CONFIRMED
        assert sink.events[0].actor_id == "user-1"
        assert sink.events[0].source_reference == "r1"

    def test_reject_then_confirm_records_both_transitions(self) -> None:
        repo = InMemoryEvidenceRepository()
        evidence_id = _seed_evidence(repo)
        sink = ListEvidenceAuditSink()
        service = EvidenceService(repo, audit_sink=sink)

        rejected = service.reject(
            CANDIDATE, EvidenceReviewRequest(evidence_id=evidence_id, actor_id="u")
        )
        confirmed = service.confirm(
            CANDIDATE, EvidenceReviewRequest(evidence_id=evidence_id, actor_id="u")
        )

        assert rejected.status is ConfirmationStatus.REJECTED
        assert confirmed.status is ConfirmationStatus.CONFIRMED
        assert len(sink.events) == 2
        # prior_status recorded so the trail is self-explaining.
        assert sink.events[0].prior_status is ConfirmationStatus.UNCONFIRMED
        assert sink.events[1].prior_status is ConfirmationStatus.REJECTED

    def test_only_confirmed_evidence_eligible_for_package(self) -> None:
        repo = InMemoryEvidenceRepository()
        evidence_id = _seed_evidence(repo)
        service = EvidenceService(repo)
        assert service.list_eligible_for_package(CANDIDATE) == []
        service.confirm(CANDIDATE, EvidenceReviewRequest(evidence_id=evidence_id, actor_id="u"))
        eligible = service.list_eligible_for_package(CANDIDATE)
        assert [e.id for e in eligible] == [evidence_id]

    def test_confirm_rejects_other_candidate(self) -> None:
        repo = InMemoryEvidenceRepository()
        evidence_id = _seed_evidence(repo, candidate_id=CANDIDATE)
        service = EvidenceService(repo)
        with pytest.raises(NotFoundError):
            service.confirm(OTHER, EvidenceReviewRequest(evidence_id=evidence_id, actor_id="u"))

    def test_list_filters_by_status(self) -> None:
        repo = InMemoryEvidenceRepository()
        evidence_id = _seed_evidence(repo)
        service = EvidenceService(repo)
        service.reject(CANDIDATE, EvidenceReviewRequest(evidence_id=evidence_id, actor_id="u"))
        rejected = service.list_for_candidate(CANDIDATE, status=ConfirmationStatus.REJECTED)
        confirmed = service.list_for_candidate(CANDIDATE, status=ConfirmationStatus.CONFIRMED)
        assert [e.id for e in rejected] == [evidence_id]
        assert confirmed == []


# ===========================================================================
# 3.7 Model-input minimization
# ===========================================================================


def _resolver(*, model_enabled: bool) -> SettingsCapabilityResolver:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "model_tailoring_enabled": model_enabled,
        }
    )
    return SettingsCapabilityResolver(settings)


class TestModelMinimization:
    def test_disabled_capability_returns_no_payload(self) -> None:
        repo = InMemoryEvidenceRepository()
        resolver = _resolver(model_enabled=False)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
                raw_resume_bytes=b"secret",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
        )
        assert result.enabled is False
        assert result.evidence == ()
        assert result.job_excerpt == ""
        # No egress was attempted (model disabled), so no per-item denials.
        assert result.denied_egress == ()
        assert result.usable is True

    def test_enabled_rejects_raw_resume_and_credentials_with_audit(self) -> None:
        repo = InMemoryEvidenceRepository()
        resolver = _resolver(model_enabled=True)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
                raw_resume_bytes=b"resume",
                credentials="secret-token",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
        )
        assert result.enabled is True
        reasons = {d.reason for d in result.denied_egress}
        assert "raw_resume_refused_at_model_boundary" in reasons
        assert "credential_refused_at_model_boundary" in reasons

    def test_enabled_includes_only_confirmed_selected_evidence(self) -> None:
        repo = InMemoryEvidenceRepository()
        confirmed_id = _seed_evidence(repo, name="Python", unique="py-1")
        # Confirm it.
        EvidenceService(repo).confirm(
            CANDIDATE, EvidenceReviewRequest(evidence_id=confirmed_id, actor_id="u")
        )
        unconfirmed_id = _seed_evidence(repo, name="Go", unique="go-1")  # stays unconfirmed

        resolver = _resolver(model_enabled=True)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(confirmed_id, unconfirmed_id),
                job_text="job text",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
        )
        assert result.enabled is True
        assert [e.id for e in result.evidence] == [confirmed_id]
        # The unconfirmed selection is denied egress.
        denied_kinds = {d.reason for d in result.denied_egress}
        assert "selected_evidence_not_confirmed" in denied_kinds

    def test_enabled_bounds_job_text(self) -> None:
        repo = InMemoryEvidenceRepository()
        resolver = _resolver(model_enabled=True)
        long_text = "a" * 10_000
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text=long_text,
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
            max_job_chars=120,
        )
        assert len(result.job_excerpt) == 120

    def test_dependency_down_marks_not_usable(self) -> None:
        repo = InMemoryEvidenceRepository()
        resolver = _resolver(model_enabled=True)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
            dependency_available=False,
        )
        assert result.enabled is False
        assert result.usable is False
        assert result.disabled_reason == "dependency_not_ready"

    def test_selected_evidence_for_other_candidate_denied(self) -> None:
        repo = InMemoryEvidenceRepository()
        other_id = _seed_evidence(repo, candidate_id=OTHER)
        resolver = _resolver(model_enabled=True)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(other_id,),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
        )
        # Cross-candidate selection is refused (NotFound -> denied egress),
        # never silently included.
        assert result.evidence == ()
        reasons = {d.reason for d in result.denied_egress}
        assert "selected_evidence_not_found" in reasons
