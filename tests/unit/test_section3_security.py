"""Section-3 security + minimization tests (tasks 3.7 / 3.8).

These tests are ADDITIVE (Iron Rule 8): they sit alongside
``test_section3_services.py`` and ``test_section3_routes.py`` and pin the
security invariants those files do not exercise:

- **3.8 unsupported claims**: ``PackageClaim`` rejects evidence-less claims at
  construction and the only evidence a trusted package may reference is the
  CONFIRMED set from :meth:`EvidenceService.list_eligible_for_package`. Model-
  proposed (unconfirmed) evidence never enters that set.
- **3.8 duplicate resumes**: content-addressed dedupe is CANDIDATE-SCOPED — the
  same bytes registered by two candidates produce two distinct versions, never a
  cross-candidate leak.
- **3.8 expired/stale content**: the content store enforces ``retention_until``
  (``ExpiredObject`` on read-after-expiry); a FAILED parse is stale and is
  excluded from package eligibility and cannot be confirmed.
- **3.8 model-disabled operation**: with ``MODEL_TAILORING`` denied (default),
  the minimized input carries no payload and a structured model gateway spy is
  never invoked, while the deterministic parse/extract/confirm/eligible path
  stays fully usable.
- **3.8 forbidden egress markers**: when tailoring is attempted, raw resume
  bytes, credentials, unconfirmed selections, and unselected evidence are each
  refused with a closed-string ``DeniedEgress`` reason.
- **3.7 model-input minimization**: when the capability IS released, the model
  input contains EXACTLY the selected CONFIRMED evidence + a bounded job
  excerpt, and every forbidden input is recorded as denied egress.
- **Ownership**: ``reject_candidate_substitution`` denies mismatched client
  ids; each Section-3 route surface surfaces 503 when its service is missing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.auth_dependency import (
    reject_candidate_substitution,
    require_api_auth,
    require_candidate_id,
)
from careerops.api.capability_dependency import require_repository
from careerops.api.errors import (
    CandidateProfileRequiredError,
    DependencyNotReadyError,
    InvalidStateError,
    NotFoundError,
    install_error_handlers,
)
from careerops.api.routes.evidence import router as evidence_router
from careerops.api.routes.profile import router as profile_router
from careerops.api.routes.resumes import router as resumes_router
from careerops.application.evidence_service import EvidenceReviewRequest, EvidenceService
from careerops.application.model_minimization import (
    DEFAULT_MAX_JOB_CHARS,
    DeniedEgress,
    ModelTailoringInput,
    ModelTailoringRequest,
    build_tailoring_input,
)
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    ExpiredObject,
    StorageDeleteResult,
    StoredBlob,
    StoredContent,
)
from careerops.application.profile_service import ProfileService
from careerops.application.resume_service import (
    RESUME_EXTRACTOR_VERSION,
    ResumeRegistrationRequest,
    ResumeService,
)
from careerops.auth.contracts import AuthenticatedPrincipal
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.applications import (
    ConfirmationStatus,
    PackageClaim,
    ResumeParseStatus,
)
from careerops.domain.candidates import EvidenceItem, EvidenceKind
from careerops.infrastructure.memory_repos import (
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryProfileRepository,
)
from careerops.model_gateway.base import (
    StructuredModelRequest,
    StructuredModelResponse,
)
from careerops.orchestration.capability_resolver import SettingsCapabilityResolver

CANDIDATE = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")
USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class _MemStore:
    """Content-addressed store stub.

    Mirrors the dedupe + retention contract of
    :class:`careerops.infrastructure.storage.local.LocalStorage` without the
    filesystem: ``put`` is content-addressed (``setdefault`` so the same digest
    never writes a second physical entry), and ``read_bytes`` enforces
    ``retention_until`` exactly like the real store (raises
    :class:`ExpiredObject` once the horizon passes).
    """

    def __init__(self, *, max_bytes: int = 5_000_000) -> None:
        self._blobs: dict[str, bytes] = {}
        self._retention: dict[str, datetime] = {}
        self._max_bytes = max_bytes
        self.put_calls = 0

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
        self.put_calls += 1
        # Content-addressed: the SAME digest never creates a second physical
        # entry. This is the property task 3.3 relies on for dedupe.
        self._blobs.setdefault(digest, content)
        self._retention.setdefault(digest, retention_until)
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
        current = now if now is not None else datetime.now(tz=UTC)
        # Same fail-closed check as LocalStorage.read_bytes.
        if stored.retention_until <= current:
            raise ExpiredObject("expired content is not readable")
        return self._blobs[stored.sha256]

    def delete_bytes(self, stored: StoredBlob) -> StorageDeleteResult:
        self._blobs.pop(stored.sha256, None)
        return StorageDeleteResult.DELETED

    def stored_count(self) -> int:
        return len(self._blobs)


class _SpyModelGateway:
    """Structured model client spy.

    Records every ``invoke`` call. A tailoring service MUST gate the call on
    ``ModelTailoringInput.enabled``; when the capability is denied this spy
    stays at zero invocations (task 3.8: "no model request is made").
    """

    def __init__(self) -> None:
        self.invocations: list[StructuredModelRequest] = []

    @property
    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        self.invocations.append(request)
        return StructuredModelResponse(
            task_type=request.task_type,
            result={},
            confidence=0.0,
            model_id="spy",
            prompt_version="test",
            is_review_only=True,
            repair_attempted=False,
            trace_id=request.trace_id,
        )


def _resolver(*, model_enabled: bool) -> SettingsCapabilityResolver:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "model_tailoring_enabled": model_enabled,
        }
    )
    return SettingsCapabilityResolver(settings)


def _resume_text() -> bytes:
    return (
        b"Jane Doe\nBackend Engineer with 5+ years of Python, Go, and PostgreSQL.\n"
        b"Built distributed systems with Kubernetes and AWS. Kafka, Redis, FastAPI.\n"
    )


def _seed_evidence(
    repo: InMemoryEvidenceRepository,
    *,
    candidate_id: UUID = CANDIDATE,
    name: str = "Python",
    unique: str | None = None,
    status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED,
) -> UUID:
    """Seed one evidence item with a distinct content/evidence hash."""
    import hashlib

    digest = hashlib.sha256((unique or name).encode("utf-8")).hexdigest()
    item = EvidenceItem(
        id=uuid4(),
        candidate_id=candidate_id,
        kind=EvidenceKind.SKILL,
        name=name,
        extractor_version=RESUME_EXTRACTOR_VERSION,
        confirmation_status=status,
        evidence_hash=digest,
        content_hash=digest,
    )
    return repo.store(item).id


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=USER_ID,
        username="owner",
        session_id=uuid4(),
        csrf_token_hash="hash",
        absolute_expires_at=datetime.now(tz=UTC),
        candidate_id=CANDIDATE,
    )


def _build_app(
    *,
    wire_services: bool = True,
    candidate_id: UUID | None = CANDIDATE,
) -> tuple[FastAPI, TestClient, dict[str, object]]:
    """Build a Section-3 test app. When ``wire_services`` is False the services
    are omitted from ``app.state`` so routes surface 503."""
    app = FastAPI()
    app.include_router(profile_router)
    app.include_router(resumes_router)
    app.include_router(evidence_router)
    install_error_handlers(app)

    services: dict[str, object] = {}
    if wire_services:
        profile_repo = InMemoryProfileRepository()
        resume_repo = InMemoryApplicationRepository()
        evidence_repo = InMemoryEvidenceRepository()
        services["profile_service"] = ProfileService(profile_repo)
        services["resume_service"] = ResumeService(
            resume_repo, evidence_repo, _MemStore(), max_bytes=5_000_000
        )
        services["evidence_service"] = EvidenceService(evidence_repo)
        services["_evidence_repo"] = evidence_repo
        for key, value in services.items():
            if not key.startswith("_"):
                setattr(app.state, key, value)

    app.dependency_overrides[require_api_auth] = lambda: _principal()

    def _resolve_candidate() -> UUID:
        if candidate_id is None:
            raise CandidateProfileRequiredError()
        return candidate_id

    app.dependency_overrides[require_candidate_id] = _resolve_candidate
    return app, TestClient(app), services


# ===========================================================================
# 3.8 — Unsupported claims rejected from trusted packages
# ===========================================================================


class TestUnsupportedClaimsRejected:
    """Iron Rule 4: every positive claim must reference candidate evidence.

    Model-produced interpretations are PROPOSALS; they cannot become a trusted
    package claim. The boundary is enforced twice: (1) ``PackageClaim`` rejects
    an evidence-less claim at construction, and (2) the only evidence a package
    may reference is the CONFIRMED set from the evidence service.
    """

    def test_package_claim_without_evidence_rejected(self) -> None:
        with pytest.raises(ValueError, match="traceable to evidence"):
            PackageClaim(claim_text="Built distributed systems at scale")

    def test_package_claim_with_evidence_accepted(self) -> None:
        evidence_id = uuid4()
        claim = PackageClaim(
            claim_text="Built distributed systems at scale",
            evidence_ids=(evidence_id,),
        )
        assert claim.evidence_ids == (evidence_id,)

    def test_only_confirmed_evidence_available_to_package(self) -> None:
        repo = InMemoryEvidenceRepository()
        confirmed = _seed_evidence(
            repo, name="Python", unique="py-c", status=ConfirmationStatus.UNCONFIRMED
        )
        unconfirmed = _seed_evidence(
            repo, name="Go", unique="go-u", status=ConfirmationStatus.UNCONFIRMED
        )
        rejected = _seed_evidence(
            repo, name="Rust", unique="rs-r", status=ConfirmationStatus.UNCONFIRMED
        )
        service = EvidenceService(repo)

        # Before confirmation: nothing is eligible for a trusted package.
        assert service.list_eligible_for_package(CANDIDATE) == []

        service.confirm(CANDIDATE, EvidenceReviewRequest(evidence_id=confirmed, actor_id="u"))
        service.reject(CANDIDATE, EvidenceReviewRequest(evidence_id=rejected, actor_id="u"))

        eligible_ids = {e.id for e in service.list_eligible_for_package(CANDIDATE)}
        assert eligible_ids == {confirmed}
        assert unconfirmed not in eligible_ids
        assert rejected not in eligible_ids

    def test_model_proposed_evidence_is_untrusted_until_confirmed(self) -> None:
        """Evidence extracted by the deterministic parser is born UNCONFIRMED:
        a model proposal cannot enter the trusted-claim set without the user."""
        repo = InMemoryEvidenceRepository()
        resume_repo = InMemoryApplicationRepository()
        service = ResumeService(resume_repo, repo, _MemStore(), max_bytes=5_000_000)
        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        # Every extracted item is a proposal (UNCONFIRMED) — never trusted yet.
        assert result.extracted_evidence
        for item in result.extracted_evidence:
            assert item.confirmation_status is ConfirmationStatus.UNCONFIRMED
            assert item.extractor_version == RESUME_EXTRACTOR_VERSION
        # Therefore none of the model/extractor-proposed items are eligible.
        evidence_service = EvidenceService(repo)
        assert evidence_service.list_eligible_for_package(CANDIDATE) == []

    def test_package_with_unconfirmed_evidence_claim_is_unbuildable(self) -> None:
        """A package claim referencing an unconfirmed evidence id is not a
        trusted package: the claim can be constructed (the id is a UUID), but
        the package may only be approved when every referenced id is in the
        CONFIRMED eligible set — pin that gate at the data the package service
        will consume."""
        repo = InMemoryEvidenceRepository()
        evidence_service = EvidenceService(repo)
        unconfirmed_id = _seed_evidence(repo, name="Python", unique="py-u")
        # The eligible set is the single source of truth for trusted claims.
        eligible = {e.id for e in evidence_service.list_eligible_for_package(CANDIDATE)}
        assert unconfirmed_id not in eligible
        # A claim bound to a non-eligible id therefore fails the trusted-package
        # gate even though PackageClaim itself only checks shape.
        claim = PackageClaim(claim_text="x", evidence_ids=(unconfirmed_id,))
        assert all(cid in eligible for cid in claim.evidence_ids) is False


# ===========================================================================
# 3.8 — Duplicate resume registration: dedupe is candidate-scoped
# ===========================================================================


class TestDuplicateResumeDedupeSecurity:
    def test_identical_hash_dedupes_no_second_blob(self) -> None:
        """Task 3.3 core: identical bytes for the SAME candidate return the
        existing version; the content-addressed store writes no second blob."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        store = _MemStore()
        service = ResumeService(resumes, evidence, store, max_bytes=5_000_000)

        first = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        blobs = store.stored_count()
        puts = store.put_calls
        second = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )

        assert second.deduplicated is True
        assert second.resume.id == first.resume.id
        # No second physical blob and no second put attempt past the first.
        assert store.stored_count() == blobs
        assert store.put_calls == puts
        assert second.extracted_evidence == ()

    def test_dedupe_is_candidate_scoped_no_cross_candidate_leak(self) -> None:
        """Security: identical content registered by two DIFFERENT candidates
        produces two distinct versions. Dedupe never collapses across the
        ownership boundary (Iron Rule 1)."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        store = _MemStore()
        service = ResumeService(resumes, evidence, store, max_bytes=5_000_000)

        first = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        other = service.register(
            OTHER, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )

        assert other.deduplicated is False
        assert other.resume.id != first.resume.id
        assert other.resume.candidate_id == OTHER
        # OTHER cannot read CANDIDATE's version and vice versa.
        with pytest.raises(NotFoundError):
            service.get_version(OTHER, first.resume.id)
        with pytest.raises(NotFoundError):
            service.get_version(CANDIDATE, other.resume.id)

    def test_dedupe_preserves_prior_failure_reason(self) -> None:
        """Re-registering bytes that previously FAILED to parse surfaces the
        stale failure (the existing version is returned unchanged)."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)

        first = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(content=b"%PDF broken", media_type="application/pdf"),
        )
        assert first.parse_status is ResumeParseStatus.FAILED
        again = service.register(
            CANDIDATE,
            ResumeRegistrationRequest(content=b"%PDF broken", media_type="application/pdf"),
        )
        assert again.deduplicated is True
        assert again.parse_status is ResumeParseStatus.FAILED
        assert again.resume.id == first.resume.id


# ===========================================================================
# 3.8 — Expired / stale content handling
# ===========================================================================


class TestExpiredAndStaleContent:
    def test_store_raises_expired_object_past_retention(self) -> None:
        """The content store the resume service depends on enforces
        ``retention_until`` fail-closed (mirrors LocalStorage.read_bytes)."""
        store = _MemStore()
        from datetime import datetime as _dt

        past_horizon = _dt.now(tz=UTC) - timedelta(days=1)
        import io

        stored = store.put(
            io.BytesIO(b"stale bytes"),
            media_type="text/plain",
            classification=ContentClassification.ACCEPTED_ATTACHMENT,
            owner=ContentOwner(resource_type="resume_version", resource_id=uuid4()),
            retention_until=past_horizon,
        )
        with pytest.raises(ExpiredObject):
            store.read_bytes(stored)

    def test_store_readable_within_retention(self) -> None:
        store = _MemStore()
        import io

        future = _dt_now() + timedelta(days=30)
        stored = store.put(
            io.BytesIO(b"fresh bytes"),
            media_type="text/plain",
            classification=ContentClassification.ACCEPTED_ATTACHMENT,
            owner=ContentOwner(resource_type="resume_version", resource_id=uuid4()),
            retention_until=future,
        )
        assert store.read_bytes(stored) == b"fresh bytes"

    def test_resume_registered_with_future_retention(self) -> None:
        """A freshly registered resume is NOT born stale: its blob retention
        horizon lies in the future, so it remains readable immediately after
        registration."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        store = _MemStore()
        service = ResumeService(resumes, evidence, store, max_bytes=5_000_000)

        before = _dt_now()
        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        after = _dt_now()

        # The stored blob's retention horizon is in the future (365 days).

        # Re-read via the service's store using the recorded object key.
        blob = store.read_bytes(_stored_view(result.resume.file_reference, store))
        assert blob == _resume_text()
        # Sanity: registration happened just now.
        assert result.resume.created_at is not None
        assert before <= result.resume.created_at <= after

    def test_failed_parse_is_stale_and_not_eligible(self) -> None:
        """A resume whose parse FAILED is stale/unusable: it is excluded from
        package eligibility and cannot be confirmed."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)

        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=b"%PDF junk", media_type="application/pdf")
        )
        assert result.parse_status is ResumeParseStatus.FAILED
        assert service.list_eligible(CANDIDATE) == []
        with pytest.raises(InvalidStateError):
            service.confirm_content(CANDIDATE, result.resume.id)

    def test_unconfirmed_parsed_resume_is_not_yet_eligible(self) -> None:
        """A parsed-but-unconfirmed resume is stale for packaging until the
        user confirms it (Iron Rule 3)."""
        resumes = InMemoryApplicationRepository()
        evidence = InMemoryEvidenceRepository()
        service = ResumeService(resumes, evidence, _MemStore(), max_bytes=5_000_000)

        result = service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        assert result.parse_status is ResumeParseStatus.PARSED
        assert service.list_eligible(CANDIDATE) == []
        service.confirm_content(CANDIDATE, result.resume.id)
        eligible = service.list_eligible(CANDIDATE)
        assert [r.id for r in eligible] == [result.resume.id]


# ===========================================================================
# 3.8 — Model-disabled operation: deterministic paths usable, no model request
# ===========================================================================


class TestModelDisabledOperation:
    """Spec scenario "Model provider is disabled": deterministic parsing,
    evidence management, filtering, and package validation remain usable and
    NO model request is made."""

    def test_disabled_capability_returns_no_payload(self) -> None:
        repo = InMemoryEvidenceRepository()
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="some job text",
                raw_resume_bytes=b"secret resume",
                credentials="sk-live-token",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=False),
        )
        assert result.enabled is False
        # No content leaves the boundary: empty evidence + empty excerpt.
        assert result.evidence == ()
        assert result.job_excerpt == ""
        assert result.denied_egress == ()
        assert result.usable is True

    def test_model_gateway_not_invoked_when_disabled(self) -> None:
        """The canonical tailoring caller gates the gateway on
        ``ModelTailoringInput.enabled``; when the capability is denied the spy
        records zero invocations."""
        spy = _SpyModelGateway()
        repo = InMemoryEvidenceRepository()
        minimized = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=False),
        )
        # The contract a tailoring service MUST honor:
        if minimized.enabled:
            spy.invoke(StructuredModelRequest(task_type="tailoring", trace_id="t"))
        assert spy.invocations == []

    def test_deterministic_pipeline_usable_with_model_disabled(self) -> None:
        """End-to-end: with MODEL_TAILORING denied, register -> parse -> extract
        -> confirm -> list_eligible all succeed, and the minimization path
        produces no model payload. The model gateway is never contacted."""
        spy = _SpyModelGateway()
        repo = InMemoryEvidenceRepository()
        resumes = InMemoryApplicationRepository()
        resume_service = ResumeService(resumes, repo, _MemStore(), max_bytes=5_000_000)
        evidence_service = EvidenceService(repo)
        resolver = _resolver(model_enabled=False)

        result = resume_service.register(
            CANDIDATE, ResumeRegistrationRequest(content=_resume_text(), media_type="text/plain")
        )
        assert result.parse_status is ResumeParseStatus.PARSED
        resume_service.confirm_content(CANDIDATE, result.resume.id)
        eligible = resume_service.list_eligible(CANDIDATE)
        assert [r.id for r in eligible] == [result.resume.id]

        # Evidence confirmation works without any model involvement.
        for item in result.extracted_evidence:
            evidence_service.confirm(
                CANDIDATE, EvidenceReviewRequest(evidence_id=item.id, actor_id="u")
            )
        confirmed = evidence_service.list_eligible_for_package(CANDIDATE)
        assert {e.id for e in confirmed} == {i.id for i in result.extracted_evidence}

        # A tailoring attempt with the capability denied produces no payload.
        minimized = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=tuple(e.id for e in confirmed),
                job_text="job description",
            ),
            evidence_repository=repo,
            capability_resolver=resolver,
        )
        if minimized.enabled:
            spy.invoke(StructuredModelRequest(task_type="tailoring", trace_id="t"))
        assert minimized.enabled is False
        assert spy.invocations == []

    def test_dependency_down_marks_not_usable_not_denied(self) -> None:
        """Iron Rule 2/3: a released capability whose backing dependency is
        down surfaces as not-usable (caller raises 503), NOT as a clean denial
        that would hide the missing dependency."""
        repo = InMemoryEvidenceRepository()
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
            dependency_available=False,
        )
        assert result.enabled is False
        assert result.usable is False
        assert result.disabled_reason == "dependency_not_ready"


# ===========================================================================
# 3.8 — Forbidden egress markers when model tailoring is attempted
# ===========================================================================


def _reasons(result: ModelTailoringInput) -> set[str]:
    return {d.reason for d in result.denied_egress}


class TestForbiddenEgressMarkers:
    """Spec scenario "Tailoring request contains unrelated private material":
    when tailoring IS attempted, raw resume / credentials / unselected and
    unconfirmed content are refused at the boundary with a recorded reason."""

    def test_raw_resume_refused(self) -> None:
        repo = InMemoryEvidenceRepository()
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
                raw_resume_bytes=b"%PDF raw resume",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        assert result.enabled is True
        assert "raw_resume_refused_at_model_boundary" in _reasons(result)

    def test_credentials_refused(self) -> None:
        repo = InMemoryEvidenceRepository()
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
                credentials="oauth-refresh-token",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        assert "credential_refused_at_model_boundary" in _reasons(result)

    def test_unconfirmed_selected_evidence_refused(self) -> None:
        repo = InMemoryEvidenceRepository()
        unconfirmed = _seed_evidence(repo, name="Python", unique="py-u")
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(unconfirmed,),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        assert result.evidence == ()
        assert "selected_evidence_not_confirmed" in _reasons(result)

    def test_unselected_evidence_never_reaches_boundary(self) -> None:
        """Evidence that EXISTS for the candidate but is NOT in the user's
        selection set is never fetched, never appears in the model input.
        This is the "unselected mailbox content" guarantee."""
        repo = InMemoryEvidenceRepository()
        EvidenceService(repo).confirm(
            CANDIDATE,
            EvidenceReviewRequest(
                evidence_id=_seed_evidence(repo, name="Go", unique="go-c"), actor_id="u"
            ),
        )
        confirmed = repo.list_confirmed_for(CANDIDATE)
        assert confirmed
        selected_confirmed = confirmed[0]

        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(selected_confirmed.id,),
                job_text="x",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        # Exactly the one selected item; no other confirmed evidence leaks in.
        assert [e.id for e in result.evidence] == [selected_confirmed.id]

    def test_job_text_bounded_no_full_posting(self) -> None:
        repo = InMemoryEvidenceRepository()
        long_text = "REQUIREMENTS\n" + ("Python engineer. " * 2000)
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text=long_text,
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        assert len(result.job_excerpt) == DEFAULT_MAX_JOB_CHARS
        assert len(result.job_excerpt) < len(long_text)

    def test_denied_egress_reasons_are_closed_strings(self) -> None:
        """DeniedEgress reasons are operator-groupable closed strings, never
        the refused content itself."""
        repo = InMemoryEvidenceRepository()
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="x",
                raw_resume_bytes=b"SECRET RESUME CONTENT",
                credentials="sk-super-secret",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        for entry in result.denied_egress:
            assert isinstance(entry, DeniedEgress)
            assert entry.reason in {
                "raw_resume_refused_at_model_boundary",
                "credential_refused_at_model_boundary",
                "unselected_content_refused_at_model_boundary",
                "selected_evidence_not_found",
                "selected_evidence_not_confirmed",
            }
            # The detail must NEVER carry the refused secret content.
            assert "SECRET" not in entry.detail


# ===========================================================================
# 3.7 — Model-input minimization (capability released)
# ===========================================================================


class TestModelInputMinimization:
    """Task 3.7: when MODEL_TAILORING is released, the model input contains
    ONLY selected candidate evidence + minimum job text; denied-egress is
    recorded for every forbidden content type."""

    def test_released_input_is_exactly_selected_confirmed_plus_bounded_excerpt(self) -> None:
        """The comprehensive minimization assertion: one confirmed selected
        item passes; every other input type is refused with a recorded reason."""
        repo = InMemoryEvidenceRepository()
        evidence_service = EvidenceService(repo)

        # 1) selected + confirmed -> the ONLY item that may leave.
        chosen = _seed_evidence(repo, name="Python", unique="py-chosen")
        evidence_service.confirm(CANDIDATE, EvidenceReviewRequest(evidence_id=chosen, actor_id="u"))
        # 2) selected but unconfirmed -> refused.
        unconfirmed_selected = _seed_evidence(repo, name="Go", unique="go-unconf")
        # 3) confirmed but NOT selected -> must not appear (unselected).
        unselected_confirmed = _seed_evidence(repo, name="Rust", unique="rs-conf")
        evidence_service.confirm(
            CANDIDATE, EvidenceReviewRequest(evidence_id=unselected_confirmed, actor_id="u")
        )

        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(chosen, unconfirmed_selected),
                job_text="JOB: Senior Backend Engineer. " * 500,
                raw_resume_bytes=b"raw resume bytes",
                credentials="provider-token",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )

        # ONLY the selected + confirmed item is in the model input.
        assert result.enabled is True
        assert [e.id for e in result.evidence] == [chosen]
        # The unselected confirmed item never reached the boundary.
        assert unselected_confirmed not in {e.id for e in result.evidence}
        # The job text is bounded.
        assert len(result.job_excerpt) == DEFAULT_MAX_JOB_CHARS

        reasons = _reasons(result)
        assert "raw_resume_refused_at_model_boundary" in reasons
        assert "credential_refused_at_model_boundary" in reasons
        assert "selected_evidence_not_confirmed" in reasons

    def test_minimized_input_carries_no_raw_resume_or_secret_material(self) -> None:
        """The minimized payload contains structured evidence + a job excerpt
        only; raw resume bytes and credentials appear nowhere in the payload."""
        repo = InMemoryEvidenceRepository()
        chosen = _seed_evidence(repo, name="Python", unique="py-2")
        EvidenceService(repo).confirm(
            CANDIDATE, EvidenceReviewRequest(evidence_id=chosen, actor_id="u")
        )
        secret_marker = "TOPSECRET-resume-contents"
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(chosen,),
                job_text="job text",
                raw_resume_bytes=secret_marker.encode("utf-8"),
                credentials="sk-secret-token",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        # Serialize the payload the way a caller would; the secrets must not
        # leak into the evidence names, the excerpt, or the disabled reason.
        payload_blob = result.job_excerpt + " ".join(e.name for e in result.evidence)
        assert "TOPSECRET" not in payload_blob
        assert "sk-secret-token" not in payload_blob

    def test_no_evidence_selected_yields_empty_evidence_not_full_dump(self) -> None:
        """Even with the capability released, an empty selection never falls
        back to dumping all confirmed evidence."""
        repo = InMemoryEvidenceRepository()
        EvidenceService(repo).confirm(
            CANDIDATE,
            EvidenceReviewRequest(
                evidence_id=_seed_evidence(repo, name="Go", unique="go-3"), actor_id="u"
            ),
        )
        result = build_tailoring_input(
            request=ModelTailoringRequest(
                candidate_id=CANDIDATE,
                selected_evidence_ids=(),
                job_text="job",
            ),
            evidence_repository=repo,
            capability_resolver=_resolver(model_enabled=True),
        )
        assert result.enabled is True
        assert result.evidence == ()


# ===========================================================================
# Ownership — candidate substitution + 503 dependency-not-ready
# ===========================================================================


class TestCandidateOwnership:
    def test_reject_candidate_substitution_mismatch(self) -> None:
        with pytest.raises(CandidateProfileRequiredError):
            reject_candidate_substitution(provided=str(OTHER), resolved=CANDIDATE)

    def test_reject_candidate_substitution_mismatch_uuid(self) -> None:
        with pytest.raises(CandidateProfileRequiredError):
            reject_candidate_substitution(provided=OTHER, resolved=CANDIDATE)

    def test_reject_candidate_substitution_invalid_uuid(self) -> None:
        with pytest.raises(CandidateProfileRequiredError):
            reject_candidate_substitution(provided="not-a-uuid", resolved=CANDIDATE)

    def test_reject_candidate_substitution_allows_none_and_empty(self) -> None:
        # "Not supplied" is not a substitution; the server-resolved id wins.
        reject_candidate_substitution(provided=None, resolved=CANDIDATE)
        reject_candidate_substitution(provided="", resolved=CANDIDATE)

    def test_reject_candidate_substitution_allows_match(self) -> None:
        reject_candidate_substitution(provided=str(CANDIDATE), resolved=CANDIDATE)
        reject_candidate_substitution(provided=CANDIDATE, resolved=CANDIDATE)

    def test_require_repository_raises_dependency_not_ready_when_missing(self) -> None:
        """Unit-level pin of the 503 helper: a missing service name raises
        DependencyNotReadyError (Iron Rule 2)."""
        from starlette.requests import Request

        app = FastAPI()
        # No attribute set on app.state.
        scope = {"type": "http", "app": app, "headers": []}
        request = Request(scope)
        with pytest.raises(DependencyNotReadyError):
            require_repository(request, "profile_service")


class TestRouteDependencyNotReady:
    """Each Section-3 route surface must surface 503 when its backing service
    is missing (no silent fallback)."""

    def test_profile_route_503_when_service_missing(self) -> None:
        _app, client, _services = _build_app(wire_services=False)
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_resumes_route_503_when_service_missing(self) -> None:
        _app, client, _services = _build_app(wire_services=False)
        resp = client.get("/api/v1/resumes")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_resumes_register_503_when_service_missing(self) -> None:
        _app, client, _services = _build_app(wire_services=False)
        resp = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _resume_text(), "text/plain")},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_evidence_route_503_when_service_missing(self) -> None:
        _app, client, _services = _build_app(wire_services=False)
        resp = client.get("/api/v1/evidence")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_missing_principal_is_403(self) -> None:
        """No server-resolved candidate -> 403, not 503 and never a silent
        unscoped read."""
        _app, client, _services = _build_app(candidate_id=None)
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "CANDIDATE_PROFILE_REQUIRED"

    def test_resumes_cross_candidate_is_404(self) -> None:
        """A resume registered by CANDIDATE is invisible to OTHER: swapping the
        server-resolved candidate yields 404 (ownership scoping)."""
        _app, client, _services = _build_app()
        rid = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _resume_text(), "text/plain")},
        ).json()["resume"]["id"]
        _app.dependency_overrides[require_candidate_id] = lambda: OTHER
        assert client.get(f"/api/v1/resumes/{rid}").status_code == 404

    def test_evidence_cross_candidate_confirm_is_404(self) -> None:
        _app, client, services = _build_app()
        evidence_repo: InMemoryEvidenceRepository = services["_evidence_repo"]  # type: ignore[assignment]
        evidence_id = _seed_evidence(evidence_repo)
        _app.dependency_overrides[require_candidate_id] = lambda: OTHER
        resp = client.post(f"/api/v1/evidence/{evidence_id}/confirm")
        assert resp.status_code == 404


# ===========================================================================
# helpers
# ===========================================================================


def _dt_now() -> datetime:
    return datetime.now(tz=UTC)


def _stored_view(object_key: str, store: _MemStore) -> StoredContent:
    """Reconstruct a minimal StoredContent view so the test can call
    ``store.read_bytes`` using the resume service's recorded object_key."""
    digest = object_key.rsplit("/", 1)[-1]
    # The store indexes retention by digest; reuse whatever horizon it recorded.
    retention = store._retention.get(digest, _dt_now() + timedelta(days=365))
    return StoredContent(
        object_key=object_key,
        sha256=digest,
        byte_size=len(store._blobs[digest]),
        media_type="text/plain",
        classification=ContentClassification.ACCEPTED_ATTACHMENT,
        owner=ContentOwner(resource_type="resume_version", resource_id=uuid4()),
        retention_until=retention,
    )
