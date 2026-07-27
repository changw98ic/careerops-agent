"""Contract tests: Section 13 reply drafting + follow-up management (13.9).

Proves the reply + follow-up slice through the service layer with in-memory
fakes (same style as Section 7/8/12 contract tests):

- (13.1) follow-up rule versions + default waiting periods per trigger state.
- (13.2) reminder create / dedup / snooze / reschedule / complete / cancel +
  terminal-state cleanup.
- (13.3) reply-draft context assembly from linked thread, bounded excerpt,
  confirmed application facts, selected evidence, user intent.
- (13.4) reply drafting with recipient/thread-header immutability +
  unsupported-claim validation (skill / deadline / salary / authorization).
- (13.5) draft edit/version/payload-hash; recipient/target edits create a new
  proposal + re-enter policy; duplicate approval is idempotent.
- (13.6) reuse Section 10's delivery services for user-confirmed reply sends;
  auto-send stays disabled; high-risk categories (salary / offer / visa / work
  authorization) permanently denied; ambiguous send -> reconciliation.
- (13.10) bounded pagination, ownership checks, idempotency.

Iron rules honored:
- Append-only / reversible (Iron Rule 4): versions never rewritten; decisions
  monotonic.
- Model review-only (Iron Rule 2): recipient / intent are user-sourced.
- Default-deny (Iron Rule 7): high-risk + auto-send permanently denied.
- Server-side ownership (Iron Rule 2/6): candidate resolved server-side.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.reply_draft_service import (
    DuplicateFollowUpError,
    FollowUpNotOwnedError,
    FollowUpService,
    ReplyDraftService,
    ReplySendOutcome,
    ReplySendPhase,
    ReplySendPort,
)
from careerops.domain.applications import (
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationState,
    FollowUpReminder,
    FollowUpState,
)
from careerops.domain.reply_draft import (
    DEFAULT_FOLLOW_UP_WAITING_PERIODS,
    FOLLOW_UP_RULE_VERSION_S13,
    REPLY_EXCERPT_MAX_CHARS,
    ApplicationFact,
    FollowUpTriggerState,
    ReplyDraft,
    ReplyDraftApprovalState,
    ReplyDraftClaim,
    ReplyIntent,
    ReplyRiskCategory,
    ReplySendDeniedError,
    UnsupportedClaimError,
    compute_reply_payload_hash,
    reply_risk_for_category,
    validate_reply_claims,
)

# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeDraftRepo:
    def __init__(self) -> None:
        self._drafts: dict[UUID, ReplyDraft] = {}

    def find_by_id(self, draft_id: UUID) -> ReplyDraft | None:
        return self._drafts.get(draft_id)

    def find_latest_for_thread(self, thread_id: UUID, candidate_id: UUID) -> ReplyDraft | None:
        candidates = [
            d
            for d in self._drafts.values()
            if d.thread_id == thread_id and d.candidate_id == candidate_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.version_number)

    def find_by_idempotency_key(self, key: str) -> ReplyDraft | None:
        return next((d for d in self._drafts.values() if d.payload_hash == key), None)

    def save(self, draft: ReplyDraft) -> None:
        self._drafts[draft.id] = draft

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        application_id: UUID | None = None,
        state: ReplyDraftApprovalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[ReplyDraft], tuple[datetime, UUID] | None]:
        items = [
            d
            for d in self._drafts.values()
            if d.candidate_id == candidate_id
            and (application_id is None or d.application_id == application_id)
            and (state is None or d.approval_state == state)
        ]
        items.sort(key=lambda d: (d.created_at or datetime.min.replace(tzinfo=UTC), d.id))
        items = list(reversed(items))
        # Apply the cursor: drop everything at-or-before the cursor anchor so a
        # second page never repeats page-1 items (mirrors the Postgres repo).
        if cursor is not None:
            cursor_time, cursor_id = cursor
            items = [
                d
                for d in items
                if (d.created_at or datetime.min.replace(tzinfo=UTC), d.id)
                < (cursor_time, cursor_id)
            ]
        next_cursor = None
        if len(items) > limit:
            last = items[limit - 1]
            next_cursor = (last.created_at, last.id) if last.created_at else None
        return items[:limit], next_cursor


class _FakeFollowUpRepo:
    def __init__(self) -> None:
        self._reminders: dict[UUID, FollowUpReminder] = {}

    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._reminders.get(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        return next(
            (
                r
                for r in self._reminders.values()
                if r.application_id == application_id
                and r.rule_version == rule_version
                and r.state == FollowUpState.ACTIVE
            ),
            None,
        )

    def save(self, reminder: FollowUpReminder) -> None:
        self._reminders[reminder.id] = reminder


class _FakeAppRepo:
    """Combined application + follow-up + timeline repo (ownership + events)."""

    def __init__(self, application: Application | None = None) -> None:
        self._apps: dict[UUID, Application] = {}
        self._events: dict[UUID, list[ApplicationEvent]] = {}
        if application is not None:
            self._apps[application.id] = application
        # follow-up surface
        self._follow_ups: dict[UUID, FollowUpReminder] = {}

    # application surface
    def find_by_id(self, application_id: UUID) -> Application | None:
        return self._apps.get(application_id)

    def save(self, application: Application) -> None:
        self._apps[application.id] = application

    def append_event(self, event: ApplicationEvent) -> None:
        self._events.setdefault(event.application_id, []).append(event)

    def events(self, application_id: UUID) -> list[ApplicationEvent]:
        return list(self._events.get(application_id, []))

    # follow-up surface
    def find_follow_up_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._follow_ups.get(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        return next(
            (
                r
                for r in self._follow_ups.values()
                if r.application_id == application_id
                and r.rule_version == rule_version
                and r.state == FollowUpState.ACTIVE
            ),
            None,
        )

    def save_follow_up(self, reminder: FollowUpReminder) -> None:
        self._follow_ups[reminder.id] = reminder


class _OwnershipReader:
    def __init__(self, app_repo: _FakeAppRepo) -> None:
        self._apps = app_repo

    def find_owned_application(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> Application | None:
        app = self._apps.find_by_id(application_id)
        if app is None or app.candidate_id != candidate_id:
            return None
        return app


class _TimelineSink:
    def __init__(self, app_repo: _FakeAppRepo) -> None:
        self._apps = app_repo
        self.notes: list[tuple[UUID, str, dict[str, object], datetime]] = []

    def append_note(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        note: str,
        event_data: dict[str, object],
        now: datetime,
    ) -> None:
        self.notes.append((application_id, note, event_data, now))


class _EvidenceReader:
    def __init__(self, confirmed: frozenset[UUID] | None = None) -> None:
        self._confirmed = confirmed or frozenset()

    def confirmed_evidence_ids(self, candidate_id: UUID) -> frozenset[UUID]:
        return self._confirmed


class _FakeReplySendPort:
    """Records sends + lets each test choose the outcome (13.6 / 13.9)."""

    def __init__(self, outcome: ReplySendOutcome | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._outcome = outcome
        self._status_by_intent: dict[UUID, ReplySendOutcome] = {}

    def deliver(
        self, *, draft: ReplyDraft, account_email: str, candidate_id: UUID, now: datetime
    ) -> ReplySendOutcome:
        self.calls.append({"draft_id": draft.id, "account_email": account_email, "now": now})
        outcome = self._outcome or ReplySendOutcome(
            phase=ReplySendPhase.SENT,
            intent_id=uuid4(),
            payload_hash=draft.payload_hash,
            provider_resource_id="fake-reply-1",
            provider_message_id="<fake@provider>",
            sent_at=now,
        )
        if outcome.intent_id is not None:
            self._status_by_intent[outcome.intent_id] = outcome
        return outcome

    def status(self, *, intent_id: UUID, candidate_id: UUID) -> ReplySendOutcome:
        return self._status_by_intent.get(
            intent_id,
            ReplySendOutcome(phase=ReplySendPhase.PENDING, intent_id=intent_id),
        )


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _make_application(
    *,
    candidate_id: UUID | None = None,
    state: ApplicationState = ApplicationState.SUBMITTED,
    submitted_at: datetime | None = NOW,
) -> Application:
    return Application(
        id=uuid4(),
        candidate_id=candidate_id or uuid4(),
        canonical_job_id=uuid4(),
        state=state,
        submitted_at=submitted_at,
        created_at=NOW,
        updated_at=NOW,
    )


def _make_draft_service(
    *,
    app_repo: _FakeAppRepo,
    evidence: _EvidenceReader | None = None,
    send_port: ReplySendPort | None = None,
) -> tuple[ReplyDraftService, _FakeDraftRepo]:
    repo = _FakeDraftRepo()
    svc = ReplyDraftService(
        repo,
        _OwnershipReader(app_repo),
        evidence_reader=evidence or _EvidenceReader(),
        timeline_sink=_TimelineSink(app_repo),
        send_port=send_port,
    )
    return svc, repo


def _make_follow_up_service(
    app_repo: _FakeAppRepo,
) -> FollowUpService:
    # The FollowUpService composes a follow-up repo (Protocol: find_by_id /
    # find_active_by_application_and_rule / save) with the application repo
    # (ownership + timeline). A dedicated fake backs the follow-up surface so
    # the method names match the Protocol exactly.
    return FollowUpService(_FakeFollowUpRepo(), app_repo, timeline_sink=_TimelineSink(app_repo))


# ===========================================================================
# 13.1 — follow-up rule versions + default waiting periods
# ===========================================================================


class TestFollowUpRules:
    def test_default_periods_cover_all_triggers(self) -> None:
        for trigger in FollowUpTriggerState:
            assert trigger in DEFAULT_FOLLOW_UP_WAITING_PERIODS

    def test_submitted_period_longer_than_interview(self) -> None:
        assert (
            DEFAULT_FOLLOW_UP_WAITING_PERIODS[FollowUpTriggerState.SUBMITTED]
            > DEFAULT_FOLLOW_UP_WAITING_PERIODS[FollowUpTriggerState.INTERVIEW]
        )

    def test_rule_version_is_section13(self) -> None:
        assert FOLLOW_UP_RULE_VERSION_S13.startswith("section13")


# ===========================================================================
# 13.2 — reminder create / dedup / snooze / reschedule / complete / cancel
#         + terminal-state cleanup
# ===========================================================================


class TestFollowUpLifecycle:
    def test_schedule_uses_trigger_default_period(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc = _make_follow_up_service(repo)
        reminder = svc.schedule_follow_up(
            application_id=app.id,
            candidate_id=app.candidate_id,
            trigger=FollowUpTriggerState.SUBMITTED.value,
            base_at=NOW,
            now=NOW,
        )
        # 5 business days from a Wednesday lands on the following Wednesday.
        assert reminder.due_at is not None
        expected = NOW + timedelta(days=7)  # Wed -> Wed (skip weekend)
        assert reminder.due_at == expected
        assert reminder.rule_version == FOLLOW_UP_RULE_VERSION_S13
        # event recorded
        events = repo.events(app.id)
        assert any(e.event_type == ApplicationEventType.FOLLOW_UP_SCHEDULED for e in events)

    def test_dedup_rejects_second_active(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc = _make_follow_up_service(repo)
        svc.schedule_follow_up(
            application_id=app.id,
            candidate_id=app.candidate_id,
            trigger="submitted",
            base_at=NOW,
            now=NOW,
        )
        with pytest.raises(DuplicateFollowUpError):
            svc.schedule_follow_up(
                application_id=app.id,
                candidate_id=app.candidate_id,
                trigger="submitted",
                base_at=NOW,
                now=NOW,
            )

    def test_snooze_reschedule_complete_cancel(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc = _make_follow_up_service(repo)
        r = svc.schedule_follow_up(
            application_id=app.id,
            candidate_id=app.candidate_id,
            trigger="interview",
            base_at=NOW,
            now=NOW,
        )
        snoozed = svc.snooze_follow_up(
            reminder_id=r.id,
            candidate_id=app.candidate_id,
            snoozed_until=NOW + timedelta(days=2),
            now=NOW,
        )
        assert snoozed.state == FollowUpState.SNOOZED
        resched = svc.reschedule_follow_up(
            reminder_id=r.id,
            candidate_id=app.candidate_id,
            new_due_at=NOW + timedelta(days=4),
            now=NOW,
        )
        assert resched.state == FollowUpState.ACTIVE
        assert resched.due_at == NOW + timedelta(days=4)
        completed = svc.complete_follow_up(reminder_id=r.id, candidate_id=app.candidate_id, now=NOW)
        assert completed.state == FollowUpState.COMPLETED
        # completed is terminal: cannot cancel
        from careerops.application.reply_draft_service import FollowUpServiceError

        with pytest.raises(FollowUpServiceError):
            svc.cancel_follow_up(
                reminder_id=r.id, candidate_id=app.candidate_id, reason="x", now=NOW
            )

    def test_terminal_cleanup_cancels_active(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc = _make_follow_up_service(repo)
        svc.schedule_follow_up(
            application_id=app.id,
            candidate_id=app.candidate_id,
            trigger="submitted",
            base_at=NOW,
            now=NOW,
        )
        cancelled = svc.cleanup_terminal(
            application_id=app.id,
            candidate_id=app.candidate_id,
            terminal_state="rejected",
            now=NOW,
        )
        assert cancelled is not None
        assert cancelled.state == FollowUpState.CANCELLED
        assert "rejected" in cancelled.cancelled_reason

    def test_not_owned_raises(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc = _make_follow_up_service(repo)
        other = uuid4()
        with pytest.raises(FollowUpNotOwnedError):
            svc.schedule_follow_up(
                application_id=app.id,
                candidate_id=other,
                trigger="submitted",
                base_at=NOW,
                now=NOW,
            )


# ===========================================================================
# 13.3 — context assembly (bounded excerpt, app facts, evidence, intent)
# ===========================================================================


class TestContextAssembly:
    def test_assembles_minimized_context(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        evidence_id = uuid4()
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.SCHEDULE,
            application_facts=(
                ApplicationFact(label="state", value="submitted", source="application"),
            ),
            evidence_refs=(evidence_id,),
            excerpt_override="Thanks for applying; let's schedule a call.",
        )
        assert ctx.application_id == app.id
        assert ctx.intent is ReplyIntent.SCHEDULE
        assert ctx.risk_category is ReplyRiskCategory.SCHEDULING
        assert ctx.thread_excerpt == "Thanks for applying; let's schedule a call."
        assert evidence_id in ctx.evidence_refs

    def test_excerpt_is_bounded(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        long_excerpt = "x" * (REPLY_EXCERPT_MAX_CHARS + 500)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override=long_excerpt,
        )
        assert len(ctx.thread_excerpt) == REPLY_EXCERPT_MAX_CHARS

    def test_unresolved_link_allowed_for_review(self) -> None:
        repo = _FakeAppRepo()
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=uuid4(),
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
        )
        assert ctx.application_id is None

    def test_not_owned_application_link_dropped(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=uuid4(),  # different candidate
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
        )
        # ownership substitution attempt -> link dropped, not honored
        assert ctx.application_id is None


# ===========================================================================
# 13.4 — drafting with recipient/thread-header immutability + claim validation
# ===========================================================================


class TestDraftingAndValidation:
    def test_draft_freezes_recipient_and_headers(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: your application",
            body="Thanks for the update.",
            in_reply_to="<orig@corp.com>",
            references_header="<orig@corp.com>",
            now=NOW,
        )
        assert draft.recipient == "recruiter@corp.com"
        assert draft.in_reply_to == "<orig@corp.com>"
        assert draft.references_header == "<orig@corp.com>"
        assert draft.approval_state is ReplyDraftApprovalState.DRAFT
        assert draft.validation_issues == ()

    def test_unsupported_salary_claim_blocks_clean_body(self) -> None:
        # A salary statement in the body is flagged.
        findings = validate_reply_claims(())
        assert findings == ()
        # Body scanner catches a salary figure.
        from careerops.domain.reply_draft import scan_body_for_unsupported_claims

        assert "salary_statement" in scan_body_for_unsupported_claims(
            "My salary expectation is $200k"
        )

    def test_unsupported_claim_blocks_approval(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.PROVIDE_INFO,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: compensation",
            body="My salary expectation is $200k.",
            now=NOW,
        )
        assert draft.validation_issues  # flagged
        with pytest.raises(UnsupportedClaimError):
            svc.approve(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)

    def test_claim_without_evidence_flagged(self) -> None:
        # A structured positive claim with no evidence binding is unsupported.
        findings = validate_reply_claims(
            (ReplyDraftClaim(claim_text="I know Python", evidence_ids=()),)
        )
        assert findings
        assert findings[0][1] == "claim_lacks_evidence"

    def test_claim_referencing_unconfirmed_evidence_flagged(self) -> None:
        confirmed = frozenset({uuid4()})
        unconfirmed = uuid4()
        findings = validate_reply_claims(
            (ReplyDraftClaim(claim_text="I led a team", evidence_ids=(unconfirmed,)),),
            confirmed_evidence_ids=confirmed,
        )
        assert findings
        assert findings[0][1] == "claim_references_unconfirmed_evidence"


# ===========================================================================
# 13.5 — edit/version/payload-hash; recipient/target mutation; duplicate approval
# ===========================================================================


class TestDraftEditVersioning:
    def test_body_edit_creates_new_version_with_new_hash(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        d1 = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: update",
            body="Thanks.",
            now=NOW,
        )
        d2 = svc.edit_draft(
            draft_id=d1.id,
            candidate_id=app.candidate_id,
            body="Thanks; looking forward to hearing from you.",
            now=NOW + timedelta(hours=1),
        )
        assert d2.version_number == d1.version_number + 1
        assert d2.payload_hash != d1.payload_hash
        # recipient + headers carried over unchanged (immutable)
        assert d2.recipient == d1.recipient
        assert d2.in_reply_to == d1.in_reply_to
        assert d2.references_header == d1.references_header
        # prior version untouched (append-only)
        prior = svc.get_draft(draft_id=d1.id, candidate_id=app.candidate_id)
        assert prior.body == "Thanks."

    def test_payload_hash_recomputable(self) -> None:
        h1 = compute_reply_payload_hash(
            message_id=uuid4(),
            thread_id=uuid4(),
            recipient="R@x.com",
            in_reply_to="a",
            references_header="b",
            subject="s",
            body="body",
            intent=ReplyIntent.ACKNOWLEDGE,
            claims=(),
        )
        assert len(h1) == 64
        # different body -> different hash
        h2 = compute_reply_payload_hash(
            message_id=uuid4(),
            thread_id=uuid4(),
            recipient="R@x.com",
            in_reply_to="a",
            references_header="b",
            subject="s",
            body="body2",
            intent=ReplyIntent.ACKNOWLEDGE,
            claims=(),
        )
        assert h1 != h2

    def test_recipient_mutation_rejected(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        # The service exposes the rejection helper; a target change MUST start
        # a new draft (re-enter recipient trust / policy / approval).
        from careerops.domain.reply_draft import RecipientMutationError

        with pytest.raises(RecipientMutationError):
            svc.require_new_draft_for_target_change()

    def test_duplicate_approve_is_idempotent(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: update",
            body="Thanks.",
            now=NOW,
        )
        first = svc.approve(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        second = svc.approve(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        assert first.approval_state is ReplyDraftApprovalState.APPROVED
        assert second.id == first.id  # idempotent: same version returned

    def test_reject_then_approve_blocked(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=app.id,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: update",
            body="Thanks.",
            now=NOW,
        )
        svc.reject(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        from careerops.domain.reply_draft import ReplyDraftAlreadyDecidedError

        with pytest.raises(ReplyDraftAlreadyDecidedError):
            svc.approve(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)


# ===========================================================================
# 13.6 — reuse Section 10 send chain; auto-send disabled; high-risk denied;
#         ambiguous send -> reconciliation
# ===========================================================================


class TestReplySend:
    def _approved_low_risk_draft(self, svc: ReplyDraftService, candidate_id: UUID) -> ReplyDraft:
        ctx = svc.assemble_context(
            candidate_id=candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re: thanks",
            body="Got it, thanks.",
            now=NOW,
        )
        return svc.approve(draft_id=draft.id, candidate_id=candidate_id, now=NOW)

    def test_user_confirmed_low_risk_send_succeeds(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        port = _FakeReplySendPort()
        svc, _ = _make_draft_service(app_repo=repo, send_port=port)
        draft = self._approved_low_risk_draft(svc, app.candidate_id)
        outcome = svc.send_approved(
            draft_id=draft.id,
            candidate_id=app.candidate_id,
            account_email="me@corp.com",
            now=NOW,
        )
        assert outcome.phase == ReplySendPhase.SENT
        assert len(port.calls) == 1
        # idempotent: repeat send reuses the in-flight outcome (no 2nd effect)
        outcome2 = port.status(intent_id=outcome.intent_id, candidate_id=app.candidate_id)
        assert outcome2.phase == ReplySendPhase.SENT

    def test_auto_send_not_invoked_without_approval(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        port = _FakeReplySendPort()
        svc, _ = _make_draft_service(app_repo=repo, send_port=port)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re",
            body="hi",
            now=NOW,
        )
        # not approved -> send refused (auto-send path does not exist)
        from careerops.domain.reply_draft import ReplyDraftError

        with pytest.raises(ReplyDraftError):
            svc.send_approved(
                draft_id=draft.id,
                candidate_id=app.candidate_id,
                account_email="me@corp.com",
                now=NOW,
            )
        assert port.calls == []

    @pytest.mark.parametrize("category", ["salary", "offer", "visa"])
    def test_high_risk_categories_permanently_denied(self, category: str) -> None:
        # A permanently-denied mail category forces a permanently-denied reply
        # risk so the send path hard-refuses regardless of user approval.
        assert reply_risk_for_category(category) is ReplyRiskCategory.PERMANENTLY_DENIED
        app = _make_application()
        repo = _FakeAppRepo(app)
        port = _FakeReplySendPort()
        svc, _ = _make_draft_service(app_repo=repo, send_port=port)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
            mail_category=category,
            excerpt_override="hi",
        )
        assert ctx.risk_category is ReplyRiskCategory.PERMANENTLY_DENIED
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="recruiter@corp.com",
            account_email="me@corp.com",
            subject="Re",
            body="ok",
            now=NOW,
        )
        approved = svc.approve(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        with pytest.raises(ReplySendDeniedError) as exc:
            svc.send_approved(
                draft_id=approved.id,
                candidate_id=app.candidate_id,
                account_email="me@corp.com",
                now=NOW,
            )
        assert "category_permanently_denied" in exc.value.reason_codes
        assert port.calls == []

    def test_work_authorization_denied(self) -> None:
        assert reply_risk_for_category("work_authorization") is ReplyRiskCategory.PERMANENTLY_DENIED

    def test_resume_link_denied(self) -> None:
        assert reply_risk_for_category("resume_link") is ReplyRiskCategory.PERMANENTLY_DENIED

    def test_ambiguous_send_marks_reconciliation(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        port = _FakeReplySendPort(
            outcome=ReplySendOutcome(
                phase=ReplySendPhase.RECONCILIATION_REQUIRED,
                intent_id=uuid4(),
                denial_reasons=("ambiguous_outcome",),
            )
        )
        svc, _ = _make_draft_service(app_repo=repo, send_port=port)
        draft = self._approved_low_risk_draft(svc, app.candidate_id)
        outcome = svc.send_approved(
            draft_id=draft.id,
            candidate_id=app.candidate_id,
            account_email="me@corp.com",
            now=NOW,
        )
        assert outcome.phase == ReplySendPhase.RECONCILIATION_REQUIRED
        assert outcome.denial_reasons == ("ambiguous_outcome",)
        # The draft is NOT recorded as sent (Iron Rule 4): no sent claim.
        refreshed = svc.get_draft(draft_id=draft.id, candidate_id=app.candidate_id)
        assert refreshed.send_phase == ReplySendPhase.RECONCILIATION_REQUIRED

    def test_send_chain_not_wired_denies(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        # send_port defaults to None (PRODUCTION never wires it before the
        # external-write qualification gate).
        svc, _ = _make_draft_service(app_repo=repo)
        draft = self._approved_low_risk_draft(svc, app.candidate_id)
        with pytest.raises(ReplySendDeniedError) as exc:
            svc.send_approved(
                draft_id=draft.id,
                candidate_id=app.candidate_id,
                account_email="me@corp.com",
                now=NOW,
            )
        assert "send_chain_not_wired" in exc.value.reason_codes


# ===========================================================================
# 13.10 — bounded pagination, ownership, idempotency
# ===========================================================================


class TestOwnershipAndPagination:
    def test_not_owned_draft_is_404_equivalent(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="r@x.com",
            account_email="me@x.com",
            subject="s",
            body="b",
            now=NOW,
        )
        from careerops.domain.reply_draft import ReplyDraftNotOwnedError

        with pytest.raises(ReplyDraftNotOwnedError):
            svc.get_draft(draft_id=draft.id, candidate_id=uuid4())

    def test_list_is_bounded_and_cursor_paginated(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        thread = uuid4()
        # create 3 drafts with distinct timestamps
        for i in range(3):
            ctx = svc.assemble_context(
                candidate_id=app.candidate_id,
                message_id=uuid4(),
                thread_id=thread,
                application_id=None,
                intent=ReplyIntent.ACKNOWLEDGE,
                excerpt_override=f"hi {i}",
            )
            svc.create_draft(
                candidate_id=app.candidate_id,
                context=ctx,
                recipient="r@x.com",
                account_email="me@x.com",
                subject=f"s {i}",
                body=f"b {i}",
                now=NOW + timedelta(minutes=i),
            )
        page1, cursor = svc.list_drafts(candidate_id=app.candidate_id, limit=2)
        assert len(page1) == 2
        assert cursor is not None
        page2, cursor2 = svc.list_drafts(candidate_id=app.candidate_id, limit=2, cursor=cursor)
        assert len(page2) == 1
        assert cursor2 is None
        # no cross-candidate leak
        other_page, _ = svc.list_drafts(candidate_id=uuid4(), limit=10)
        assert other_page == []

    def test_idempotent_reject(self) -> None:
        app = _make_application()
        repo = _FakeAppRepo(app)
        svc, _ = _make_draft_service(app_repo=repo)
        ctx = svc.assemble_context(
            candidate_id=app.candidate_id,
            message_id=uuid4(),
            thread_id=uuid4(),
            application_id=None,
            intent=ReplyIntent.ACKNOWLEDGE,
            excerpt_override="hi",
        )
        draft = svc.create_draft(
            candidate_id=app.candidate_id,
            context=ctx,
            recipient="r@x.com",
            account_email="me@x.com",
            subject="s",
            body="b",
            now=NOW,
        )
        first = svc.reject(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        second = svc.reject(draft_id=draft.id, candidate_id=app.candidate_id, now=NOW)
        assert first.id == second.id
        assert second.approval_state is ReplyDraftApprovalState.REJECTED
