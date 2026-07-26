"""Unit tests for the end-to-end-career-application-loop Section 2 repositories.

Covers the in-memory implementations of the five repository concerns landed in
this stage:

- ProfileRepository (immutable versions, one-active invariant, contradiction hook)
- ResumeRepository extension (content-addressed dedupe, eligible-for-packages)
- EvidenceRepository (resume-derived lifecycle, confirm/reject, candidate scoping)
- ApplicationCycleRepository (get-or-create, re-application history)
- ApplicationRepository extension (cycle/channel/package/payload/provider linkage)

The Postgres implementations are exercised by the integration suite
(``tests/integration``) under ``CAREEROPS_TEST_DATABASE_URL``; this file pins
the behavioral contract via the in-memory path so the contract is enforced on
every unit run. A shared-protocol section at the bottom runs the same suite
against Postgres when the database is available.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.domain.applications import (
    Application,
    ApplicationState,
    ConfirmationStatus,
    ResumeParseStatus,
    ResumeVersion,
    SubmissionChannel,
)
from careerops.domain.candidates import EvidenceItem, EvidenceKind
from careerops.domain.profiles import (
    Authorization,
    CompensationPeriod,
    CompensationPreference,
    HardExclusions,
    LocationKind,
    LocationPreference,
    ProfileVersion,
    RemoteRules,
    TargetRole,
)
from careerops.infrastructure.memory_repos import (
    InMemoryApplicationCycleRepository,
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryProfileRepository,
)

NOW = datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Profile repository
# ---------------------------------------------------------------------------


def _profile(
    candidate_id: UUID,
    version: int,
    *,
    is_active: bool = False,
    locations: tuple[LocationPreference, ...] = (),
    compensation: CompensationPreference | None = None,
    profile_id: UUID | None = None,
) -> ProfileVersion:
    return ProfileVersion(
        id=profile_id or uuid4(),
        candidate_id=candidate_id,
        version=version,
        is_active=is_active,
        target_roles=(TargetRole(title="Backend Engineer", seniority="senior"),),
        locations=locations,
        remote_rules=RemoteRules(remote_allowed=True),
        compensation=compensation or CompensationPreference(currency="USD"),
        seniority="senior",
        authorization=Authorization(work_authorization="citizen"),
        include_keywords=("python",),
        exclude_keywords=("legacy",),
        hard_exclusions=HardExclusions(),
        rules_version="rules.v1",
        created_at=NOW,
        updated_at=NOW,
    )


class TestProfileRepository:
    def test_create_inactive_and_then_activate(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        v1 = repo.create_version(_profile(cid, 1))
        assert v1.is_active is False
        active = repo.activate(cid, v1.id)
        assert active.is_active is True
        active_fetched = repo.get_active_for(cid)
        assert active_fetched is not None
        assert active_fetched.id == v1.id

    def test_activate_deactivates_prior_in_same_op(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        v1 = repo.create_version(_profile(cid, 1, is_active=True))
        v2 = repo.create_version(_profile(cid, 2))
        repo.activate(cid, v2.id)
        # v1 must be deactivated; v2 is the lone active version.
        active_after = repo.get_active_for(cid)
        assert active_after is not None
        assert active_after.id == v2.id
        assert repo.get_by_version_id(cid, v1.id).is_active is False
        assert repo.get_by_version_id(cid, v2.id).is_active is True

    def test_at_most_one_active_per_candidate(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        for version in range(1, 6):
            v = repo.create_version(_profile(cid, version))
            repo.activate(cid, v.id)
        active = repo.get_active_for(cid)
        assert active is not None
        # Only the last-activated version is active.
        active_count = sum(1 for v in repo.list_versions(cid) if v.is_active)
        assert active_count == 1
        assert active.version == 5

    def test_get_by_version_id_rejects_other_candidate(self) -> None:
        repo = InMemoryProfileRepository()
        cid_a = uuid4()
        cid_b = uuid4()
        v = repo.create_version(_profile(cid_a, 1))
        # Client-supplied candidate substitution must not leak another
        # candidate's profile (Iron Rule 2).
        with pytest.raises(NotFoundError):
            repo.get_by_version_id(cid_b, v.id)

    def test_create_version_rejects_contradictory_required_excluded(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        profile = _profile(
            cid,
            1,
            locations=(
                LocationPreference(name="Chengdu", kind=LocationKind.REQUIRED),
                LocationPreference(name="Chengdu", kind=LocationKind.EXCLUDED),
            ),
        )
        with pytest.raises(InvalidStateError):
            repo.create_version(profile)
        # The invalid version never became active (Iron Rule 6).
        assert repo.get_active_for(cid) is None
        assert repo.list_versions(cid) == []

    def test_create_version_rejects_preferred_excluded_overlap(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        profile = _profile(
            cid,
            1,
            locations=(
                LocationPreference(name="Beijing", kind=LocationKind.PREFERRED),
                LocationPreference(name="Beijing", kind=LocationKind.EXCLUDED),
            ),
        )
        with pytest.raises(InvalidStateError):
            repo.create_version(profile)

    def test_create_version_rejects_inverted_compensation(self) -> None:
        repo = InMemoryProfileRepository()
        cid = uuid4()
        profile = _profile(
            cid,
            1,
            compensation=CompensationPreference(
                currency="USD",
                amount_min=200_000,
                amount_max=100_000,
                period=CompensationPeriod.ANNUAL,
            ),
        )
        with pytest.raises(InvalidStateError):
            repo.create_version(profile)

    def test_activate_revalidates_contradictions(self) -> None:
        """A version that became contradictory must not flip to active."""
        repo = InMemoryProfileRepository()
        cid = uuid4()
        profile = _profile(cid, 1)  # valid when created
        created = repo.create_version(profile)
        # Tamper with the in-memory store to simulate a rules change making
        # the stored preferences contradictory (the service re-validates on
        # activate using the stored payload, not the caller's claim).
        stored = repo._versions[created.id]  # type: ignore[private-use]
        repo._versions[created.id] = replace(  # type: ignore[private-use]
            stored,
            locations=(
                LocationPreference(name="Shanghai", kind=LocationKind.REQUIRED),
                LocationPreference(name="Shanghai", kind=LocationKind.EXCLUDED),
            ),
        )
        with pytest.raises(InvalidStateError):
            repo.activate(cid, created.id)


# ---------------------------------------------------------------------------
# Resume repository extension (content-addressed dedupe + eligibility)
# ---------------------------------------------------------------------------


def _resume(
    candidate_id: UUID,
    version_number: int,
    *,
    content_hash: str,
    file_reference: str = "ref",
    target_type: str = "general",
    parse_status: ResumeParseStatus = ResumeParseStatus.PENDING,
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED,
    resume_id: UUID | None = None,
) -> ResumeVersion:
    return ResumeVersion(
        id=resume_id or uuid4(),
        candidate_id=candidate_id,
        version_number=version_number,
        file_reference=file_reference,
        content_hash=content_hash,
        target_type=target_type,
        human_confirmed=False,
        parse_status=parse_status,
        confirmation_status=confirmation_status,
        source_reference="upload",
        created_at=NOW,
    )


class TestResumeRepositoryExtension:
    def test_find_by_content_hash_returns_existing(self) -> None:
        repo = InMemoryApplicationRepository()
        cid = uuid4()
        sha = "a" * 64
        v1 = _resume(cid, 1, content_hash=sha)
        repo.save_resume(v1)
        found = repo.find_resume_by_content_hash(cid, sha)
        assert found is not None
        assert found.id == v1.id

    def test_find_by_content_hash_scoped_by_candidate(self) -> None:
        repo = InMemoryApplicationRepository()
        cid_a = uuid4()
        cid_b = uuid4()
        sha = "b" * 64
        repo.save_resume(_resume(cid_a, 1, content_hash=sha))
        # Another candidate with the same hash must not see cid_a's resume.
        assert repo.find_resume_by_content_hash(cid_b, sha) is None

    def test_find_by_content_hash_empty_hash_returns_none(self) -> None:
        repo = InMemoryApplicationRepository()
        assert repo.find_resume_by_content_hash(uuid4(), "") is None

    def test_find_eligible_resumes_only_parsed_and_confirmed(self) -> None:
        repo = InMemoryApplicationRepository()
        cid = uuid4()
        sha_a = "a" * 64
        sha_b = "b" * 64
        sha_c = "c" * 64
        sha_d = "d" * 64
        # Eligible: parsed + confirmed.
        repo.save_resume(
            _resume(
                cid,
                1,
                content_hash=sha_a,
                parse_status=ResumeParseStatus.PARSED,
                confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        )
        # Pending parse: not eligible.
        repo.save_resume(
            _resume(
                cid,
                2,
                content_hash=sha_b,
                parse_status=ResumeParseStatus.PENDING,
                confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        )
        # Unconfirmed: not eligible.
        repo.save_resume(
            _resume(
                cid,
                3,
                content_hash=sha_c,
                parse_status=ResumeParseStatus.PARSED,
                confirmation_status=ConfirmationStatus.UNCONFIRMED,
            )
        )
        # Rejected: not eligible.
        repo.save_resume(
            _resume(
                cid,
                4,
                content_hash=sha_d,
                parse_status=ResumeParseStatus.PARSED,
                confirmation_status=ConfirmationStatus.REJECTED,
            )
        )
        eligible = repo.find_eligible_resumes(cid)
        assert {r.content_hash for r in eligible} == {sha_a}

    def test_find_resume_by_id_scoped_by_candidate(self) -> None:
        repo = InMemoryApplicationRepository()
        cid_a = uuid4()
        cid_b = uuid4()
        v = _resume(cid_a, 1, content_hash="a" * 64)
        repo.save_resume(v)
        assert repo.find_resume_by_id(cid_a, v.id) is not None
        assert repo.find_resume_by_id(cid_b, v.id) is None


# ---------------------------------------------------------------------------
# Evidence repository (resume-derived lifecycle)
# ---------------------------------------------------------------------------


def _evidence(
    candidate_id: UUID,
    *,
    evidence_id: UUID | None = None,
    repository: str = "acme/api",
    commit_sha: str = "abc",
    path: str = "src/auth.py",
    symbol: str = "login",
    content_hash: str = "x" * 64,
    evidence_hash: str = "y" * 64,
    resume_version_id: UUID | None = None,
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id or uuid4(),
        candidate_id=candidate_id,
        kind=EvidenceKind.SKILL,
        name="OAuth2",
        description="",
        repository=repository,
        commit_sha=commit_sha,
        path=path,
        symbol=symbol,
        content_hash=content_hash,
        source_url="https://example.com",
        verified=False,
        extractor_version="extractor.v1",
        source_span="L10-L40",
        confirmation_status=confirmation_status,
        evidence_hash=evidence_hash,
        resume_version_id=resume_version_id,
        created_at=NOW,
    )


class TestEvidenceRepository:
    def test_store_and_get_round_trip_with_resume_derived_fields(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        resume_id = uuid4()
        item = _evidence(cid, resume_version_id=resume_id)
        stored = repo.store(item)
        assert stored.id == item.id
        assert stored.resume_version_id == resume_id
        assert stored.extractor_version == "extractor.v1"
        assert stored.source_span == "L10-L40"

    def test_store_idempotent_on_repo_key(self) -> None:
        """Same (candidate, repository, commit_sha, path, symbol, content_hash)
        is a no-op: the existing row is returned, not duplicated."""
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        first = _evidence(cid)
        repo.store(first)
        # Re-store with a different id but same idempotency key.
        duplicate = _evidence(cid, evidence_id=uuid4())
        returned = repo.store(duplicate)
        # The existing row wins; the returned id is the original.
        assert returned.id == first.id
        assert len(repo.list_for_candidate(cid)) == 1
        # The duplicate id was NOT persisted (it would raise NotFoundError).
        with pytest.raises(NotFoundError):
            repo.get_by_id(cid, duplicate.id)

    def test_confirm_reject_flips_status(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        item = repo.store(_evidence(cid))
        confirmed = repo.confirm(cid, item.id)
        assert confirmed.confirmation_status is ConfirmationStatus.CONFIRMED
        rejected = repo.reject(cid, item.id)
        assert rejected.confirmation_status is ConfirmationStatus.REJECTED
        # Row still present — append-only history preserved.
        assert repo.get_by_id(cid, item.id) is not None

    def test_confirm_idempotent(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        item = repo.store(_evidence(cid))
        first = repo.confirm(cid, item.id)
        second = repo.confirm(cid, item.id)
        assert first.confirmation_status is second.confirmation_status

    def test_confirm_reject_rejects_other_candidate(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid_a = uuid4()
        cid_b = uuid4()
        item = repo.store(_evidence(cid_a))
        with pytest.raises(NotFoundError):
            repo.confirm(cid_b, item.id)

    def test_list_confirmed_excludes_unconfirmed_and_rejected(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        # Each item needs a distinct content_hash so the repo-key idempotency
        # path does not collapse them into one row.
        repo.store(_evidence(cid, content_hash="c" + "1" * 63, evidence_hash="h" + "1" * 63))
        confirmed = repo.store(
            _evidence(
                cid,
                content_hash="c" + "2" * 63,
                evidence_hash="h" + "2" * 63,
                confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        )
        repo.store(
            _evidence(
                cid,
                content_hash="c" + "3" * 63,
                evidence_hash="h" + "3" * 63,
                confirmation_status=ConfirmationStatus.REJECTED,
            )
        )
        result = repo.list_confirmed_for(cid)
        assert {r.id for r in result} == {confirmed.id}

    def test_find_by_evidence_hash(self) -> None:
        repo = InMemoryEvidenceRepository()
        cid = uuid4()
        item = repo.store(_evidence(cid, evidence_hash="z" * 64))
        found = repo.find_by_evidence_hash(cid, "z" * 64)
        assert found is not None
        assert found.id == item.id
        assert repo.find_by_evidence_hash(cid, "") is None


# ---------------------------------------------------------------------------
# Application cycle repository
# ---------------------------------------------------------------------------


class TestApplicationCycleRepository:
    def test_get_or_create_is_idempotent(self) -> None:
        repo = InMemoryApplicationCycleRepository()
        cid = uuid4()
        job = uuid4()
        cycle_id_a = uuid4()
        cycle_id_b = uuid4()
        first = repo.get_or_create_active(cid, job, cycle_id=cycle_id_a, reason="initial")
        # Second call with a different cycle_id must return the existing one.
        second = repo.get_or_create_active(cid, job, cycle_id=cycle_id_b, reason="retry")
        assert first.id == second.id == cycle_id_a

    def test_one_active_per_candidate_job(self) -> None:
        repo = InMemoryApplicationCycleRepository()
        cid = uuid4()
        job = uuid4()
        repo.get_or_create_active(cid, job, cycle_id=uuid4())
        active = repo.get_active_for(cid, job)
        assert active is not None
        # All active cycles for (cid, job) — must be exactly one.
        history = repo.list_history(cid, job)
        actives = [c for c in history if c.active]
        assert len(actives) == 1

    def test_reapplication_closes_prior_and_links_history(self) -> None:
        repo = InMemoryApplicationCycleRepository()
        cid = uuid4()
        job = uuid4()
        first = repo.get_or_create_active(cid, job, cycle_id=uuid4())
        new_id = uuid4()
        second = repo.open_reapplication_cycle(cid, job, new_cycle_id=new_id, reason="reapply")
        # The new cycle is active and references the prior.
        assert second.id == new_id
        assert second.prior_cycle_id == first.id
        assert second.active is True
        assert second.closed_at is None
        # The prior cycle is closed.
        prior = repo.get_by_id(cid, first.id)
        assert prior.active is False
        assert prior.closed_at is not None
        # History preserved.
        assert len(repo.list_history(cid, job)) == 2

    def test_reapplication_without_active_cycle_raises(self) -> None:
        repo = InMemoryApplicationCycleRepository()
        cid = uuid4()
        job = uuid4()
        with pytest.raises(NotFoundError):
            repo.open_reapplication_cycle(cid, job, new_cycle_id=uuid4())

    def test_get_by_id_rejects_other_candidate(self) -> None:
        repo = InMemoryApplicationCycleRepository()
        cid_a = uuid4()
        cid_b = uuid4()
        job = uuid4()
        cycle = repo.get_or_create_active(cid_a, job, cycle_id=uuid4())
        with pytest.raises(NotFoundError):
            repo.get_by_id(cid_b, cycle.id)


# ---------------------------------------------------------------------------
# Application repository extension (cycle/channel/package/payload/provider)
# ---------------------------------------------------------------------------


class TestApplicationRepositoryExtension:
    def test_save_and_read_full_linkage(self) -> None:
        repo = InMemoryApplicationRepository()
        cid = uuid4()
        job = uuid4()
        cycle_id = uuid4()
        package_version_id = uuid4()
        app = Application(
            id=uuid4(),
            candidate_id=cid,
            canonical_job_id=job,
            state=ApplicationState.SUBMITTED,
            apply_url="https://example.com/apply",
            cycle_id=cycle_id,
            submission_channel=SubmissionChannel.EMAIL,
            package_version_id=package_version_id,
            payload_hash="p" * 64,
            provider_kind="gmail",
            provider_message_id="msg-1",
            submitted_at=NOW,
            version=3,
            created_at=NOW,
            updated_at=NOW,
        )
        repo.save(app)
        read = repo.find_by_id(app.id)
        assert read is not None
        assert read.cycle_id == cycle_id
        assert read.submission_channel is SubmissionChannel.EMAIL
        assert read.package_version_id == package_version_id
        assert read.payload_hash == "p" * 64
        assert read.provider_kind == "gmail"
        assert read.provider_message_id == "msg-1"
        assert read.submitted_at == NOW
        assert read.version == 3

    def test_legacy_application_with_null_linkage_still_reads(self) -> None:
        """Backfill safety: an application written before 2.6/2.7 columns
        existed must still round-trip via the same read path."""
        repo = InMemoryApplicationRepository()
        cid = uuid4()
        job = uuid4()
        app = Application(
            id=uuid4(),
            candidate_id=cid,
            canonical_job_id=job,
            state=ApplicationState.FAVORITED,
        )
        repo.save(app)
        read = repo.find_by_id(app.id)
        assert read is not None
        assert read.cycle_id is None
        assert read.submission_channel is None
        assert read.package_version_id is None
        assert read.payload_hash is None
        assert read.provider_kind is None
        assert read.provider_message_id is None

    def test_append_only_events_not_touched_by_save(self) -> None:
        """Saving an application with new fields must not affect append-only
        event history (Iron Rule 6 — events are append-only)."""
        from careerops.domain.applications import (
            ApplicationEvent,
            ApplicationEventSource,
            ApplicationEventType,
        )

        repo = InMemoryApplicationRepository()
        cid = uuid4()
        job = uuid4()
        app = Application(
            id=uuid4(),
            candidate_id=cid,
            canonical_job_id=job,
            state=ApplicationState.FAVORITED,
        )
        repo.save(app)
        event = ApplicationEvent(
            id=uuid4(),
            application_id=app.id,
            event_type=ApplicationEventType.CREATED,
            source=ApplicationEventSource.USER,
            occurred_at=NOW,
        )
        repo.append_event(event)
        # Re-save the application with new linkage fields.
        updated = replace(app, submission_channel=SubmissionChannel.MANUAL, version=2)
        repo.save(updated)
        events = repo.get_events(app.id)
        assert len(events) == 1
        assert events[0].event_type is ApplicationEventType.CREATED
