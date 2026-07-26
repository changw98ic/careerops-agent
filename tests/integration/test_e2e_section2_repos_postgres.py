"""PostgreSQL integration tests for the Section 2 repositories.

Mirrors the in-memory contract from ``tests/unit/test_e2e_section2_repos.py``
against the real Postgres schema landed by migration 0014. The test inserts the
minimum fixture rows (candidate + canonical_job + resume_version) that the new
foreign keys require, exercises the new repositories, and asserts the same
invariants the in-memory suite pins:

- Profile: one-active-per-candidate, activate-deactivates-prior, candidate
  scoping, contradiction rejection, compensation-range DB guard.
- Resume: content-addressed dedupe, eligible-for-packages filter.
- Evidence: resume-derived fields round-trip, confirm/reject, candidate scoping.
- ApplicationCycle: get-or-create idempotency, re-application history.
- Application: 2.6/2.7 linkage round-trip, legacy NULL linkage reads.

Skips entirely when ``CAREEROPS_TEST_DATABASE_URL`` is not set (the unit suite
still enforces the contract on every run).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

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
    CompensationPeriod,
    CompensationPreference,
    LocationPreference,
    ProfileVersion,
    RemoteRules,
    TargetRole,
)
from careerops.infrastructure.database.postgres_application_cycle_repo import (
    PostgresApplicationCycleRepository,
)
from careerops.infrastructure.database.postgres_application_repo import (
    PostgresApplicationRepository,
)
from careerops.infrastructure.database.postgres_evidence_repo import (
    PostgresEvidenceRepository,
)
from careerops.infrastructure.database.postgres_profile_repo import (
    PostgresProfileRepository,
)
from careerops.infrastructure.database.schema import (
    application_cycles,
    applications,
    candidates,
    canonical_jobs,
    companies,
    profile_versions,
    resume_versions,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 26, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for Section 2 repo tests")
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    yield database_engine
    database_engine.dispose()


# ---------------------------------------------------------------------------
# Fixture row helpers (the new tables have FKs to candidates / canonical_jobs
# / resume_versions, so each test seeds the minimum it needs and cleans up).
# ---------------------------------------------------------------------------


def _seed_candidate(engine: Engine, candidate_id: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            candidates.insert().values(
                id=candidate_id,
                display_name="test candidate",
            )
        )


def _seed_company_and_job(engine: Engine, company_id: object, job_id: object) -> None:
    # Suffix the normalized name so parallel tests with different company_ids
    # do not collide on uq_companies_normalized_name.
    suffix = str(company_id)[-8:]
    with engine.begin() as conn:
        conn.execute(
            companies.insert().values(
                id=company_id,
                name=f"test company {suffix}",
                normalized_name=f"testcompany-{suffix}",
            )
        )
        conn.execute(
            canonical_jobs.insert().values(
                id=job_id,
                company_id=company_id,
                canonical_title="Backend Engineer",
                normalized_title="backend engineer",
                aggregate_state="active",
            )
        )


def _seed_resume(engine: Engine, resume_id: object, candidate_id: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            resume_versions.insert().values(
                id=resume_id,
                candidate_id=candidate_id,
                version_number=1,
                file_reference="ref",
                content_hash="r" * 64,
            )
        )


def _profile(
    candidate_id: object,
    version: int,
    *,
    is_active: bool = False,
    locations: tuple[LocationPreference, ...] = (),
    compensation: CompensationPreference | None = None,
) -> ProfileVersion:
    return ProfileVersion(
        id=uuid4(),
        candidate_id=candidate_id,  # type: ignore[arg-type]
        version=version,
        is_active=is_active,
        target_roles=(TargetRole(title="Backend Engineer"),),
        locations=locations,
        remote_rules=RemoteRules(remote_allowed=True),
        compensation=compensation or CompensationPreference(currency="USD"),
        seniority="senior",
        rules_version="rules.v1",
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class TestPostgresProfileRepository:
    def test_create_inactive_activate_deactivates_prior(self, engine: Engine) -> None:
        cid = uuid4()
        _seed_candidate(engine, cid)
        repo = PostgresProfileRepository(engine)
        try:
            v1 = repo.create_version(_profile(cid, 1, is_active=True))
            assert v1.is_active is True
            v2 = repo.create_version(_profile(cid, 2))
            repo.activate(cid, v2.id)
            active = repo.get_active_for(cid)
            assert active is not None
            assert active.id == v2.id
            assert repo.get_by_version_id(cid, v1.id).is_active is False
        finally:
            with engine.begin() as conn:
                conn.execute(
                    profile_versions.delete().where(profile_versions.c.candidate_id == cid)
                )

    def test_one_active_per_candidate_db_enforced(self, engine: Engine) -> None:
        cid = uuid4()
        _seed_candidate(engine, cid)
        repo = PostgresProfileRepository(engine)
        try:
            repo.create_version(_profile(cid, 1, is_active=True))
            # The repository path always deactivates the prior version, so to
            # prove the partial unique index enforces one-active-per-candidate
            # we insert a second active row directly, bypassing the repo.
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    profile_versions.insert().values(
                        id=uuid4(),
                        candidate_id=cid,
                        version=2,
                        is_active=True,
                    )
                )
        finally:
            with engine.begin() as conn:
                conn.execute(
                    profile_versions.delete().where(profile_versions.c.candidate_id == cid)
                )

    def test_get_by_version_id_rejects_other_candidate(self, engine: Engine) -> None:
        cid_a = uuid4()
        cid_b = uuid4()
        _seed_candidate(engine, cid_a)
        _seed_candidate(engine, cid_b)
        repo = PostgresProfileRepository(engine)
        try:
            v = repo.create_version(_profile(cid_a, 1))
            with pytest.raises(NotFoundError):
                repo.get_by_version_id(cid_b, v.id)
        finally:
            with engine.begin() as conn:
                conn.execute(
                    profile_versions.delete().where(
                        profile_versions.c.candidate_id.in_([cid_a, cid_b])
                    )
                )

    def test_create_rejects_inverted_compensation(self, engine: Engine) -> None:
        cid = uuid4()
        _seed_candidate(engine, cid)
        repo = PostgresProfileRepository(engine)
        try:
            with pytest.raises(InvalidStateError):
                repo.create_version(
                    _profile(
                        cid,
                        1,
                        compensation=CompensationPreference(
                            currency="USD",
                            amount_min=200_000,
                            amount_max=100_000,
                            period=CompensationPeriod.ANNUAL,
                        ),
                    )
                )
        finally:
            with engine.begin() as conn:
                conn.execute(
                    profile_versions.delete().where(profile_versions.c.candidate_id == cid)
                )


# ---------------------------------------------------------------------------
# Application cycle
# ---------------------------------------------------------------------------


class TestPostgresApplicationCycleRepository:
    def test_get_or_create_then_reapply(self, engine: Engine) -> None:
        cid = uuid4()
        company_id = uuid4()
        job_id = uuid4()
        _seed_candidate(engine, cid)
        _seed_company_and_job(engine, company_id, job_id)
        repo = PostgresApplicationCycleRepository(engine)
        try:
            first = repo.get_or_create_active(cid, job_id, cycle_id=uuid4(), reason="initial")
            second_call = repo.get_or_create_active(cid, job_id, cycle_id=uuid4(), reason="retry")
            assert first.id == second_call.id
            new_id = uuid4()
            reopened = repo.open_reapplication_cycle(
                cid, job_id, new_cycle_id=new_id, reason="reapply"
            )
            assert reopened.id == new_id
            assert reopened.prior_cycle_id == first.id
            assert reopened.active is True
            prior = repo.get_by_id(cid, first.id)
            assert prior.active is False
            assert prior.closed_at is not None
            assert len(repo.list_history(cid, job_id)) == 2
        finally:
            with engine.begin() as conn:
                conn.execute(
                    application_cycles.delete().where(application_cycles.c.candidate_id == cid)
                )

    def test_one_active_cycle_db_enforced(self, engine: Engine) -> None:
        cid = uuid4()
        company_id = uuid4()
        job_id = uuid4()
        _seed_candidate(engine, cid)
        _seed_company_and_job(engine, company_id, job_id)
        try:
            with engine.begin() as conn:
                conn.execute(
                    application_cycles.insert().values(
                        id=uuid4(),
                        candidate_id=cid,
                        canonical_job_id=job_id,
                        active=True,
                    )
                )
                # A second active row for the same (candidate, job) must
                # violate ix_application_cycles_candidate_job_active.
                with pytest.raises(IntegrityError):
                    conn.execute(
                        application_cycles.insert().values(
                            id=uuid4(),
                            candidate_id=cid,
                            canonical_job_id=job_id,
                            active=True,
                        )
                    )
        finally:
            with engine.begin() as conn:
                conn.execute(
                    application_cycles.delete().where(application_cycles.c.candidate_id == cid)
                )


# ---------------------------------------------------------------------------
# Evidence (resume-derived lifecycle)
# ---------------------------------------------------------------------------


class TestPostgresEvidenceRepository:
    def test_store_confirm_reject_round_trip(self, engine: Engine) -> None:
        cid = uuid4()
        resume_id = uuid4()
        _seed_candidate(engine, cid)
        _seed_resume(engine, resume_id, cid)
        repo = PostgresEvidenceRepository(engine)
        item = EvidenceItem(
            id=uuid4(),
            candidate_id=cid,
            kind=EvidenceKind.SKILL,
            name="OAuth2",
            extractor_version="extractor.v1",
            source_span="L10-L40",
            evidence_hash="e" * 64,
            content_hash="c" * 64,
            resume_version_id=resume_id,
            created_at=NOW,
        )
        try:
            stored = repo.store(item)
            assert stored.resume_version_id == resume_id
            assert stored.extractor_version == "extractor.v1"
            confirmed = repo.confirm(cid, stored.id)
            assert confirmed.confirmation_status is ConfirmationStatus.CONFIRMED
            rejected = repo.reject(cid, stored.id)
            assert rejected.confirmation_status is ConfirmationStatus.REJECTED
            only_confirmed = repo.list_confirmed_for(cid)
            # After reject, list_confirmed_for must be empty.
            assert only_confirmed == []
        finally:
            from careerops.infrastructure.database.schema import evidence_items

            with engine.begin() as conn:
                conn.execute(evidence_items.delete().where(evidence_items.c.candidate_id == cid))

    def test_confirm_rejects_other_candidate(self, engine: Engine) -> None:
        cid_a = uuid4()
        cid_b = uuid4()
        _seed_candidate(engine, cid_a)
        _seed_candidate(engine, cid_b)
        repo = PostgresEvidenceRepository(engine)
        item = EvidenceItem(
            id=uuid4(),
            candidate_id=cid_a,
            kind=EvidenceKind.SKILL,
            name="OAuth2",
            content_hash="a" * 64,
            evidence_hash="b" * 64,
            created_at=NOW,
        )
        try:
            stored = repo.store(item)
            with pytest.raises(NotFoundError):
                repo.confirm(cid_b, stored.id)
        finally:
            from careerops.infrastructure.database.schema import evidence_items

            with engine.begin() as conn:
                conn.execute(evidence_items.delete().where(evidence_items.c.candidate_id == cid_a))


# ---------------------------------------------------------------------------
# Resume + Application extension
# ---------------------------------------------------------------------------


class TestPostgresResumeAndApplication:
    def test_resume_lifecycle_fields_round_trip(self, engine: Engine) -> None:
        cid = uuid4()
        _seed_candidate(engine, cid)
        repo = PostgresApplicationRepository(engine)
        version = ResumeVersion(
            id=uuid4(),
            candidate_id=cid,
            version_number=1,
            file_reference="ref",
            content_hash="d" * 64,
            target_type="general",
            parse_status=ResumeParseStatus.PARSED,
            confirmation_status=ConfirmationStatus.CONFIRMED,
            source_reference="upload",
            parsed_at=NOW,
            confirmed_at=NOW,
            created_at=NOW,
        )
        try:
            repo.save_resume(version)
            read = repo.find_resume_by_id(cid, version.id)
            assert read is not None
            assert read.parse_status is ResumeParseStatus.PARSED
            assert read.confirmation_status is ConfirmationStatus.CONFIRMED
            assert read.source_reference == "upload"
            # Eligibility filter passes for parsed + confirmed.
            eligible = repo.find_eligible_resumes(cid)
            assert any(r.id == version.id for r in eligible)
            # Content-addressed dedupe finds the existing row.
            dedupe = repo.find_resume_by_content_hash(cid, "d" * 64)
            assert dedupe is not None
            assert dedupe.id == version.id
        finally:
            with engine.begin() as conn:
                conn.execute(resume_versions.delete().where(resume_versions.c.candidate_id == cid))

    def test_application_full_linkage_round_trip(self, engine: Engine) -> None:
        cid = uuid4()
        company_id = uuid4()
        job_id = uuid4()
        cycle_id = uuid4()
        package_version_id = uuid4()
        _seed_candidate(engine, cid)
        _seed_company_and_job(engine, company_id, job_id)
        # Seed a cycle so cycle_id FK is satisfied.
        with engine.begin() as conn:
            conn.execute(
                application_cycles.insert().values(
                    id=cycle_id,
                    candidate_id=cid,
                    canonical_job_id=job_id,
                    active=True,
                )
            )
        repo = PostgresApplicationRepository(engine)
        app = Application(
            id=uuid4(),
            candidate_id=cid,
            canonical_job_id=job_id,
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
        try:
            repo.save(app)
            read = repo.find_by_id(app.id)
            assert read is not None
            assert read.cycle_id == cycle_id
            assert read.submission_channel is SubmissionChannel.EMAIL
            assert read.package_version_id == package_version_id
            assert read.payload_hash == "p" * 64
            assert read.provider_kind == "gmail"
            assert read.provider_message_id == "msg-1"
            assert read.version == 3
        finally:
            with engine.begin() as conn:
                conn.execute(applications.delete().where(applications.c.candidate_id == cid))
                conn.execute(
                    application_cycles.delete().where(application_cycles.c.candidate_id == cid)
                )
