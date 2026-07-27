"""Contract tests: Section 7 application workspace gate (tasks 7.10-7.11).

Proves the application-preparation slice through the domain service layer with
in-memory repos:

- Duplicate application creation is idempotent and cycle-bound (7.2 / 7.10).
- Illegal state transitions are rejected; model output cannot transition state
  (7.1 / 7.3 / 7.10).
- Channel eligibility is derived from trusted job evidence; unavailable
  channels are blocked (7.4).
- External-form/manual confirmation requires evidence; an abandoned form leaves
  the application in PREPARING with no submitted event (7.7 / 7.10).
- Submitted evidence (apply URL + timestamp) is recorded on the timeline (7.6).
- Stale package approval: rebinding with a new payload hash supersedes the old
  binding (7.5 / 7.10).
- Vertical slice (7.11): favorite → prepare → package binding → external-form
  confirmation → timeline projection, all deterministic, no external writes.

Iron rules honored:
- Idempotent (Iron Rule 3): get-or-create returns the existing application.
- Append-only / reversible (Iron Rule 4): timeline is a projection; history is
  never rewritten.
- Model review-only (Iron Rule 2): no service method lets model output select a
  channel or transition state.
- Default-deny (Iron Rule 7): EMAIL is not auto-eligible; submission needs
  evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.application.application_workspace import (
    ApplicationNotOwnedError,
    ApplicationWorkspaceService,
    ChannelUnavailableError,
    ExternalFormEvidenceError,
)
from careerops.domain.applications import (
    Application,
    ApplicationCycle,
    ApplicationEvent,
    ApplicationState,
    IllegalTransitionError,
    SubmissionChannel,
)

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeApplicationRepo:
    """In-memory ApplicationRepository (find/save/append_event/get_events)."""

    def __init__(self) -> None:
        self._apps: dict[UUID, Application] = {}
        self._by_pair: dict[tuple[UUID, UUID], UUID] = {}
        self.events: list[ApplicationEvent] = []

    def find_by_id(self, application_id: UUID) -> Application | None:
        return self._apps.get(application_id)

    def find_by_candidate_and_job(
        self, candidate_id: UUID, canonical_job_id: UUID
    ) -> Application | None:
        app_id = self._by_pair.get((candidate_id, canonical_job_id))
        return self._apps.get(app_id) if app_id else None

    def save(self, application: Application) -> None:
        self._apps[application.id] = application
        self._by_pair[(application.candidate_id, application.canonical_job_id)] = application.id

    def append_event(self, event: ApplicationEvent) -> None:
        self.events.append(event)

    def get_events(self, application_id: UUID) -> list[ApplicationEvent]:
        return [e for e in self.events if e.application_id == application_id]


class _FakeCycleRepo:
    """In-memory cycle repo mirroring the Postgres get_or_create_active contract."""

    def __init__(self) -> None:
        self._active: dict[tuple[UUID, UUID], ApplicationCycle] = {}

    def get_or_create_active(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle:
        key = (candidate_id, canonical_job_id)
        existing = self._active.get(key)
        if existing is not None:
            return existing

        cycle = ApplicationCycle(
            id=cycle_id,
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            active=True,
            created_at=now,
        )
        self._active[key] = cycle
        return cycle


def _make_service(
    *, with_cycle: bool = True
) -> tuple[ApplicationWorkspaceService, _FakeApplicationRepo, _FakeCycleRepo]:
    repo = _FakeApplicationRepo()
    cycle = _FakeCycleRepo()
    svc = ApplicationWorkspaceService(
        repo,  # type: ignore[arg-type]
        cycle_repo=cycle if with_cycle else None,
    )
    return svc, repo, cycle


# ---------------------------------------------------------------------------
# (1) Idempotent get-or-create + cycle binding (7.2 / 7.10)
# ---------------------------------------------------------------------------


class TestGetOrCreateIdempotency:
    def test_repeat_create_returns_same_application(self) -> None:
        svc, repo, _ = _make_service()
        cid, job = uuid4(), uuid4()
        now = datetime.now(tz=UTC)

        a1 = svc.get_or_create_application(
            candidate_id=cid, canonical_job_id=job, apply_url="https://acme.com/j/1", now=now
        )
        a2 = svc.get_or_create_application(
            candidate_id=cid, canonical_job_id=job, apply_url="https://acme.com/j/1", now=now
        )
        assert a1.id == a2.id, "repeat get-or-create must not duplicate"
        assert len(repo._apps) == 1
        assert a1.state is ApplicationState.FAVORITED

    def test_new_application_is_bound_to_a_cycle(self) -> None:
        svc, _, cycle = _make_service()
        cid, job = uuid4(), uuid4()
        now = datetime.now(tz=UTC)

        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=job, now=now)
        assert app.cycle_id is not None, "new application must bind to a cycle"
        assert (cid, job) in cycle._active

    def test_other_candidate_gets_distinct_application(self) -> None:
        """Server-side ownership: a different candidate gets a different app."""
        svc, repo, _ = _make_service()
        now = datetime.now(tz=UTC)
        job = uuid4()
        a1 = svc.get_or_create_application(candidate_id=uuid4(), canonical_job_id=job, now=now)
        a2 = svc.get_or_create_application(candidate_id=uuid4(), canonical_job_id=job, now=now)
        assert a1.id != a2.id
        assert len(repo._apps) == 2

    def test_read_enforces_ownership(self) -> None:
        svc, _, _ = _make_service()
        cid, other = uuid4(), uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        # Another candidate cannot read it
        with pytest.raises(ApplicationNotOwnedError):
            svc.get_application(application_id=app.id, candidate_id=other)
        # Unknown id -> same error (no existence leak)
        with pytest.raises(ApplicationNotOwnedError):
            svc.get_application(application_id=uuid4(), candidate_id=cid)


# ---------------------------------------------------------------------------
# (2) Illegal transitions + model cannot transition (7.1 / 7.3 / 7.10)
# ---------------------------------------------------------------------------


class TestIllegalTransitions:
    def test_rejected_to_interviewing_rejected(self) -> None:
        svc, repo, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        # Force the app into REJECTED via the repo (terminal) to test the gate.
        rejected = Application(
            id=app.id,
            candidate_id=cid,
            canonical_job_id=app.canonical_job_id,
            state=ApplicationState.REJECTED,
            version=5,
            created_at=now,
            updated_at=now,
        )
        repo.save(rejected)
        with pytest.raises(IllegalTransitionError):
            svc.apply_user_transition(
                application_id=app.id,
                candidate_id=cid,
                to_state=ApplicationState.INTERVIEWING,
                now=now,
            )

    def test_submission_requires_evidence_not_bare_transition(self) -> None:
        """A bare transition to SUBMITTED is refused — submission must carry evidence."""
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        from careerops.application.application_workspace import WorkspaceError

        with pytest.raises(WorkspaceError):
            svc.apply_user_transition(
                application_id=app.id,
                candidate_id=cid,
                to_state=ApplicationState.SUBMITTED,
                now=now,
            )

    def test_prepare_from_submitted_rejected(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        # Now confirm an external submission to reach SUBMITTED
        svc.confirm_external_submission(
            application_id=app.id,
            candidate_id=cid,
            apply_url="https://acme.com/j/1",
            submitted_at=now,
            now=now,
        )
        # PREPARING->SUBMITTED already happened; preparing again is illegal
        with pytest.raises(IllegalTransitionError):
            svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)

    def test_favorite_to_preparing_succeeds(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        prepared = svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        assert prepared.state is ApplicationState.PREPARING


# ---------------------------------------------------------------------------
# (3) Channel eligibility (7.4)
# ---------------------------------------------------------------------------


class TestChannelEligibility:
    def test_external_form_needs_apply_url(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        # No apply URL
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        snap = svc.evaluate_channels(application_id=app.id, candidate_id=cid)
        assert not snap.is_eligible(SubmissionChannel.EMAIL), "email deferred (no contact)"
        assert not snap.is_eligible(SubmissionChannel.EXTERNAL_FORM), "no apply URL"
        assert snap.is_eligible(SubmissionChannel.MANUAL)

    def test_select_unavailable_channel_blocked(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        with pytest.raises(ChannelUnavailableError):
            svc.select_channel(
                application_id=app.id,
                candidate_id=cid,
                channel=SubmissionChannel.EXTERNAL_FORM,
                now=now,
            )

    def test_select_manual_channel_succeeds(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        updated = svc.select_channel(
            application_id=app.id,
            candidate_id=cid,
            channel=SubmissionChannel.MANUAL,
            now=now,
        )
        assert updated.submission_channel is SubmissionChannel.MANUAL


# ---------------------------------------------------------------------------
# (4) External-form abandonment + (5) submitted evidence (7.7 / 7.10)
# ---------------------------------------------------------------------------


class TestExternalFormSubmission:
    def test_abandoned_form_stays_preparing(self) -> None:
        """If the user never confirms, the application stays PREPARING and no
        submitted event is created (no automatic submitted claim)."""
        svc, repo, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        # Simulate abandonment: no confirm call.
        current = repo.find_by_id(app.id)
        assert current is not None
        assert current.state is ApplicationState.PREPARING
        events = repo.get_events(app.id)
        assert not any(e.event_type.value == "submitted_manually" for e in events), (
            "abandoned external form must not create a submitted event"
        )

    def test_external_form_requires_apply_url(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        with pytest.raises(ExternalFormEvidenceError):
            svc.confirm_external_submission(
                application_id=app.id,
                candidate_id=cid,
                apply_url="",
                submitted_at=now,
                now=now,
            )

    def test_confirmation_records_evidence_and_transitions(self) -> None:
        svc, repo, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(
            candidate_id=cid,
            canonical_job_id=uuid4(),
            apply_url="https://acme.com/jobs/42",
            now=now,
        )
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        submitted_at = datetime(2026, 7, 26, 12, tzinfo=UTC)
        updated = svc.confirm_external_submission(
            application_id=app.id,
            candidate_id=cid,
            apply_url="https://acme.com/jobs/42",
            submitted_at=submitted_at,
            now=now,
        )
        assert updated.state is ApplicationState.SUBMITTED
        assert updated.submitted_at == submitted_at
        assert updated.submission_channel is SubmissionChannel.EXTERNAL_FORM
        # The submitted event carries the evidence
        sub_events = [
            e for e in repo.get_events(app.id) if e.event_type.value == "submitted_manually"
        ]
        assert len(sub_events) == 1
        assert sub_events[0].event_data["apply_url"] == "https://acme.com/jobs/42"
        assert sub_events[0].event_data["submitted_at"] == submitted_at.isoformat()

    def test_confirmation_only_from_preparing(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        # Still FAVORITED -> cannot confirm submission
        with pytest.raises(IllegalTransitionError):
            svc.confirm_external_submission(
                application_id=app.id,
                candidate_id=cid,
                apply_url="https://acme.com/j/1",
                submitted_at=now,
                now=now,
            )


# ---------------------------------------------------------------------------
# (6) Timeline projection (7.6)
# ---------------------------------------------------------------------------


class TestTimelineProjection:
    def test_timeline_is_chronological_and_complete(self) -> None:
        svc, _, _ = _make_service()
        cid = uuid4()
        t0 = datetime(2026, 7, 1, tzinfo=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=t0)
        t1 = datetime(2026, 7, 2, tzinfo=UTC)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=t1)
        t2 = datetime(2026, 7, 3, tzinfo=UTC)
        svc.confirm_external_submission(
            application_id=app.id,
            candidate_id=cid,
            apply_url="https://acme.com/j/1",
            submitted_at=t2,
            now=t2,
        )
        timeline = svc.get_timeline(application_id=app.id, candidate_id=cid)
        # created, state_changed, submitted = 3 entries
        assert len(timeline) == 3
        assert [e.occurred_at for e in timeline] == sorted(e.occurred_at for e in timeline)
        # Kinds projected
        kinds = {e.kind.value for e in timeline}
        assert "decision" in kinds
        assert "submission" in kinds
        # Submitted entry carries evidence refs
        sub = next(e for e in timeline if e.kind.value == "submission")
        assert sub.evidence_refs["apply_url"] == "https://acme.com/j/1"
        assert sub.to_state == "submitted"

    def test_timeline_distinguishes_confirmed_facts(self) -> None:
        """All event-derived entries are CONFIRMED (proposals arrive in later gates)."""
        svc, _, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        svc.prepare_application(application_id=app.id, candidate_id=cid, now=now)
        timeline = svc.get_timeline(application_id=app.id, candidate_id=cid)
        assert all(e.status.value == "confirmed" for e in timeline)


# ---------------------------------------------------------------------------
# (7) Stale package approval / binding (7.5 / 7.10)
# ---------------------------------------------------------------------------


class TestPackageBinding:
    def test_bind_records_payload_hash_and_version(self) -> None:
        svc, repo, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        pkg_v1 = uuid4()
        updated = svc.bind_package(
            application_id=app.id,
            candidate_id=cid,
            package_version_id=pkg_v1,
            payload_hash="a" * 64,
            job_version_id=uuid4(),
            resume_version_id=uuid4(),
            evidence_refs=(uuid4(),),
            now=now,
        )
        assert updated.package_version_id == pkg_v1
        assert updated.payload_hash == "a" * 64
        attached = [e for e in repo.get_events(app.id) if e.event_type.value == "package_attached"]
        assert len(attached) == 1
        assert attached[0].event_data["payload_hash"] == "a" * 64

    def test_rebind_with_new_payload_supersedes_old(self) -> None:
        """Changing the bound payload creates a new binding version; the old
        payload hash no longer matches (invalidation-on-mutation)."""
        svc, repo, _ = _make_service()
        cid = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(candidate_id=cid, canonical_job_id=uuid4(), now=now)
        pkg_v1 = uuid4()
        svc.bind_package(
            application_id=app.id,
            candidate_id=cid,
            package_version_id=pkg_v1,
            payload_hash="a" * 64,
            now=now,
        )
        pkg_v2 = uuid4()
        updated = svc.bind_package(
            application_id=app.id,
            candidate_id=cid,
            package_version_id=pkg_v2,
            payload_hash="b" * 64,
            now=now,
        )
        # The current binding reflects the NEW payload; the old is invalidated.
        assert updated.payload_hash == "b" * 64
        assert updated.package_version_id == pkg_v2
        attached = [e for e in repo.get_events(app.id) if e.event_type.value == "package_attached"]
        assert len(attached) == 2, "both binds are append-only history"
        assert attached[-1].event_data["payload_hash"] == "b" * 64


# ---------------------------------------------------------------------------
# (8) Vertical slice (7.11)
# ---------------------------------------------------------------------------


class TestApplicationWorkspaceSlice:
    """favorite → preparation → package binding → external-form confirmation
    → timeline projection, using deterministic fixtures, no external writes."""

    def test_full_slice_external_form(self) -> None:
        svc, repo, _ = _make_service()
        candidate_id = uuid4()
        canonical_job_id = uuid4()
        apply_url = "https://careers.acme.com/jobs/senior-engineer/apply"
        t_create = datetime(2026, 7, 20, 9, tzinfo=UTC)
        t_prepare = datetime(2026, 7, 20, 10, tzinfo=UTC)
        t_bind = datetime(2026, 7, 20, 11, tzinfo=UTC)
        t_submit = datetime(2026, 7, 21, 14, tzinfo=UTC)

        # 1. Favorite (create)
        app = svc.get_or_create_application(
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            apply_url=apply_url,
            now=t_create,
        )
        assert app.state is ApplicationState.FAVORITED
        assert app.cycle_id is not None

        # 2. Prepare
        app = svc.prepare_application(
            application_id=app.id, candidate_id=candidate_id, now=t_prepare
        )
        assert app.state is ApplicationState.PREPARING

        # 3. Channel eligibility — external_form is available (apply URL present)
        snap = svc.evaluate_channels(application_id=app.id, candidate_id=candidate_id)
        assert snap.is_eligible(SubmissionChannel.EXTERNAL_FORM)
        app = svc.select_channel(
            application_id=app.id,
            candidate_id=candidate_id,
            channel=SubmissionChannel.EXTERNAL_FORM,
            now=t_prepare,
        )
        assert app.submission_channel is SubmissionChannel.EXTERNAL_FORM

        # 4. Package binding (Section 8 produces the package; here we bind it)
        app = svc.bind_package(
            application_id=app.id,
            candidate_id=candidate_id,
            package_version_id=uuid4(),
            payload_hash="c" * 64,
            job_version_id=uuid4(),
            resume_version_id=uuid4(),
            now=t_bind,
        )
        assert app.payload_hash == "c" * 64

        # 5. External-form confirmation with evidence
        app = svc.confirm_external_submission(
            application_id=app.id,
            candidate_id=candidate_id,
            apply_url=apply_url,
            submitted_at=t_submit,
            now=t_submit,
        )
        assert app.state is ApplicationState.SUBMITTED
        assert app.submitted_at == t_submit

        # 6. Timeline projection reflects the whole journey
        timeline = svc.get_timeline(application_id=app.id, candidate_id=candidate_id)
        kinds = [e.kind.value for e in timeline]
        assert "decision" in kinds  # created + prepare
        assert "package_event" in kinds
        assert "submission" in kinds
        assert [e.occurred_at for e in timeline] == sorted(e.occurred_at for e in timeline)
        # Append-only: history was never rewritten
        assert len(repo.events) == len({e.id for e in repo.events})

    def test_full_slice_manual_tracking(self) -> None:
        """Manual channel: no apply URL needed; user records the submission."""
        svc, _, _ = _make_service()
        candidate_id = uuid4()
        now = datetime.now(tz=UTC)
        app = svc.get_or_create_application(
            candidate_id=candidate_id, canonical_job_id=uuid4(), now=now
        )
        svc.prepare_application(application_id=app.id, candidate_id=candidate_id, now=now)
        # MANUAL is always eligible
        snap = svc.evaluate_channels(application_id=app.id, candidate_id=candidate_id)
        assert snap.is_eligible(SubmissionChannel.MANUAL)
        app = svc.confirm_external_submission(
            application_id=app.id,
            candidate_id=candidate_id,
            apply_url="",
            submitted_at=now,
            channel=SubmissionChannel.MANUAL,
            now=now,
        )
        assert app.state is ApplicationState.SUBMITTED
        assert app.submission_channel is SubmissionChannel.MANUAL
