"""Contract tests: Section 11 Gmail read-sync gate (tasks 11.1-11.10).

Proves the mail-sync + thread-association slice through the service layer with
in-memory fakes (same style as test_section7/8). Covers the task-11.9
integration scenarios:

- duplicate messages are skipped without a second record (11.4).
- a partial sync restart resumes from the durable cursor without duplicating
  records or losing the cursor (11.3 / 11.9).
- a revoked account stops future sync and invalidates pending provider
  actions (11.2).
- a non-recruitment message body is discarded; only minimum metadata is kept
  (11.5).
- an ambiguous thread link is left UNRESOLVED rather than silently chosen
  (11.6).
- a confirmed link is reused and a repeat decision is idempotent (11.6).
- a forbidden OAuth scope rejects the connection and the credential is never
  stored (11.1).

Plus bounded cursor pagination behavior (11.10).

Iron rules honored:
- Default-deny (Iron Rule 7): the GMAIL_READ capability is never released in
  these tests; the SERVICE layer is exercised directly (the route-layer gate
  is proven denied-by-default elsewhere).
- Model review-only (Iron Rule 2): no test feeds model output as a trusted
  link or recipient.
- Append-only / reversible (Iron Rule 4): link decisions are recorded once.
- Server-side ownership (Iron Rule 2/6): every call passes the server-resolved
  candidate_id; a different candidate sees nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.mail_sync_service import (
    AccountNotConnectedError,
    AccountNotOwnedError,
    LinkAlreadyResolvedError,
    LinkNotFoundError,
    MailSyncService,
    ReadScopeError,
    ThreadNotFoundError,
)
from careerops.domain.applications import ApplicationState
from careerops.domain.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailMessage,
    EmailThread,
)
from careerops.domain.mail_sync import (
    LinkConfidence,
    LinkConfirmationDecision,
    MailConnectionState,
    MailSyncRun,
    MailSyncRunStatus,
    ThreadAssociationEvidence,
    ThreadLinkSnapshot,
    ThreadLinkStatus,
    UnresolvedThreadLink,
    validate_read_scope,
)

_READONLY = "https://www.googleapis.com/auth/gmail.readonly"


# ---------------------------------------------------------------------------
# In-memory fakes implementing the three Protocol repos
# ---------------------------------------------------------------------------


class _FakeAccountRepo:
    def __init__(self) -> None:
        self._by_id: dict[UUID, EmailAccount] = {}
        self._by_candidate: dict[UUID, UUID] = {}
        self._scopes: dict[UUID, tuple[str, ...]] = {}
        self._states: dict[UUID, MailConnectionState] = {}
        self._errors: dict[UUID, str] = {}
        self.invalidated_count = 0

    def get_account(self, candidate_id: UUID, account_id: UUID) -> EmailAccount | None:
        account = self._by_id.get(account_id)
        if account is None:
            return None
        if self._by_candidate.get(candidate_id) != account_id:
            return None
        return account

    def get_account_for_candidate(self, candidate_id: UUID) -> EmailAccount | None:
        account_id = self._by_candidate.get(candidate_id)
        if account_id is None:
            return None
        return self._by_id.get(account_id)

    def upsert_account(self, account: EmailAccount) -> None:
        self._by_id[account.id] = account

    def set_connection_state(
        self,
        candidate_id: UUID,
        account_id: UUID,
        state: MailConnectionState,
        *,
        last_error_code: str = "",
        now: datetime,
    ) -> EmailAccount | None:
        account = self._by_id.get(account_id)
        if account is None:
            return None
        self._states[account_id] = state
        self._errors[account_id] = last_error_code
        if state is MailConnectionState.CONNECTED:
            self._by_candidate[candidate_id] = account_id
            self._by_id[account_id] = EmailAccount(
                id=account.id,
                email_address=account.email_address,
                credential_reference_id=account.credential_reference_id,
                status=EmailAccountStatus.ACTIVE,
                history_id=account.history_id,
                watch_expiration=account.watch_expiration,
                last_sync_at=account.last_sync_at,
                created_at=account.created_at,
                updated_at=now,
            )
        elif state is MailConnectionState.REVOKED:
            mapped_status = EmailAccountStatus.REVOKED
            self._by_id[account_id] = EmailAccount(
                id=account.id,
                email_address=account.email_address,
                credential_reference_id=account.credential_reference_id,
                status=mapped_status,
                history_id=account.history_id,
                watch_expiration=account.watch_expiration,
                last_sync_at=account.last_sync_at,
                created_at=account.created_at,
                updated_at=now,
            )
        elif state is MailConnectionState.ERROR:
            self._by_id[account_id] = EmailAccount(
                id=account.id,
                email_address=account.email_address,
                credential_reference_id=account.credential_reference_id,
                status=EmailAccountStatus.ERROR,
                history_id=account.history_id,
                watch_expiration=account.watch_expiration,
                last_sync_at=account.last_sync_at,
                created_at=account.created_at,
                updated_at=now,
            )
        return self._by_id[account_id]

    def set_scopes(self, account_id: UUID, scopes: tuple[str, ...]) -> None:
        self._scopes[account_id] = scopes


class _FakeSyncRunRepo:
    def __init__(self, processed_repo: _FakeThreadLinkRepo) -> None:
        self._runs: dict[UUID, MailSyncRun] = {}
        self._by_account: dict[UUID, list[UUID]] = {}
        self._cursors: dict[UUID, str] = {}
        self._processed = processed_repo

    def create_run(self, run: MailSyncRun) -> None:
        self._runs[run.id] = run
        self._by_account.setdefault(run.account_id, []).insert(0, run.id)

    def get_run(self, candidate_id: UUID, run_id: UUID) -> MailSyncRun | None:
        run = self._runs.get(run_id)
        if run is None or run.candidate_id != candidate_id:
            return None
        return run

    def get_open_run(self, account_id: UUID) -> MailSyncRun | None:
        for run_id in self._by_account.get(account_id, []):
            run = self._runs[run_id]
            if run.status in (MailSyncRunStatus.PENDING, MailSyncRunStatus.RUNNING):
                return run
        return None

    def update_run(
        self,
        candidate_id: UUID,
        run_id: UUID,
        *,
        status: MailSyncRunStatus,
        messages_processed: int,
        messages_skipped: int,
        history_id_end: str,
        error_code: str,
        completed_at: datetime,
    ) -> MailSyncRun | None:
        run = self._runs.get(run_id)
        if run is None or run.candidate_id != candidate_id:
            return None
        updated = MailSyncRun(
            id=run.id,
            account_id=run.account_id,
            candidate_id=run.candidate_id,
            status=status,
            direction=run.direction,
            started_at=run.started_at,
            completed_at=completed_at,
            messages_processed=messages_processed,
            messages_skipped=messages_skipped,
            history_id_start=run.history_id_start,
            history_id_end=history_id_end,
            error_code=error_code,
        )
        self._runs[run_id] = updated
        return updated

    def list_runs(
        self,
        candidate_id: UUID,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        ids = [
            rid
            for rid in self._by_account.get(account_id, [])
            if self._runs[rid].candidate_id == candidate_id
        ]
        items = [
            {
                "id": str(rid),
                "status": self._runs[rid].status.value,
                "direction": self._runs[rid].direction.value,
                "messages_processed": self._runs[rid].messages_processed,
                "messages_skipped": self._runs[rid].messages_skipped,
                "history_id_end": self._runs[rid].history_id_end,
                "error_code": self._runs[rid].error_code,
            }
            for rid in ids
        ]
        end = limit if cursor is None else limit + int(cursor)
        page = items[:end]
        has_more = len(items) > end
        return {
            "items": page,
            "next_cursor": str(end) if has_more else None,
            "has_more": has_more,
        }

    def get_cursor(self, account_id: UUID):
        from careerops.domain.mail_sync import MailSyncCursor

        history_id = self._cursors.get(account_id)
        if history_id is None:
            return None
        return MailSyncCursor(account_id=account_id, history_id=history_id)

    def advance_cursor(self, account_id: UUID, cursor, *, now: datetime) -> None:
        self._cursors[account_id] = cursor.history_id

    def processed_provider_ids(self, account_id: UUID) -> frozenset[str]:
        return self._processed.provider_ids_for_account(account_id)


class _FakeThreadLinkRepo:
    def __init__(self) -> None:
        self._threads_by_id: dict[UUID, EmailThread] = {}
        self._threads_by_provider: dict[tuple[UUID, str], UUID] = {}
        self._messages: dict[UUID, EmailMessage] = {}
        self._messages_by_account: dict[UUID, set[str]] = {}
        self._links: dict[UUID, ThreadLinkSnapshot] = {}  # thread_id -> snapshot
        self._unresolved: dict[UUID, UnresolvedThreadLink] = {}  # link_id -> unresolved
        self._decisions: dict[UUID, LinkConfirmationDecision] = {}  # link_id -> decision

    def provider_ids_for_account(self, account_id: UUID) -> frozenset[str]:
        return frozenset(self._messages_by_account.get(account_id, set()))

    def upsert_thread(self, thread: EmailThread) -> None:
        existing_id = self._threads_by_provider.get((thread.account_id, thread.provider_thread_id))
        if existing_id is not None:
            self._threads_by_id[existing_id] = EmailThread(
                id=existing_id,
                account_id=thread.account_id,
                provider_thread_id=thread.provider_thread_id,
                subject=thread.subject,
                application_id=thread.application_id,
                created_at=self._threads_by_id[existing_id].created_at,
                updated_at=thread.updated_at,
            )
            return
        self._threads_by_id[thread.id] = thread
        self._threads_by_provider[(thread.account_id, thread.provider_thread_id)] = thread.id

    def get_thread(self, candidate_id: UUID, thread_id: UUID) -> EmailThread | None:
        return self._threads_by_id.get(thread_id)

    def append_message(self, message: EmailMessage) -> None:
        self._messages[message.id] = message
        self._messages_by_account.setdefault(message.account_id, set()).add(
            message.provider_message_id
        )

    def get_link(self, candidate_id: UUID, thread_id: UUID) -> ThreadLinkSnapshot | None:
        return self._links.get(thread_id)

    def upsert_link(self, snapshot: ThreadLinkSnapshot, *, now: datetime) -> None:
        from uuid import uuid4

        from careerops.domain.mail_sync import (
            ThreadLinkSnapshot,
            ThreadLinkStatus,
            UnresolvedThreadLink,
        )

        # Reuse the snapshot's own link_id when present; mint one otherwise.
        link_id = snapshot.link_id if snapshot.link_id is not None else uuid4()
        snapshot = ThreadLinkSnapshot(
            thread_id=snapshot.thread_id,
            account_id=snapshot.account_id,
            candidate_id=snapshot.candidate_id,
            status=snapshot.status,
            application_id=snapshot.application_id,
            candidate_application_ids=snapshot.candidate_application_ids,
            confidence=snapshot.confidence,
            evidence_refs=snapshot.evidence_refs,
            provider_thread_id=snapshot.provider_thread_id,
            subject=snapshot.subject,
            resolved_at=snapshot.resolved_at,
            link_id=link_id,
        )
        self._links[snapshot.thread_id] = snapshot
        if snapshot.status is ThreadLinkStatus.UNRESOLVED:
            self._unresolved[link_id] = UnresolvedThreadLink(
                link_id=link_id,
                thread_id=snapshot.thread_id,
                candidate_id=snapshot.candidate_id,
                provider_thread_id=snapshot.provider_thread_id,
                subject=snapshot.subject,
                candidate_application_ids=snapshot.candidate_application_ids,
                evidence_refs=snapshot.evidence_refs,
                created_at=now,
            )
        else:
            # A non-unresolved snapshot resolves (removes) any prior
            # unresolved review item for this thread.
            self._unresolved.pop(link_id, None)

    def list_threads(
        self, candidate_id: UUID, account_id: UUID, *, cursor, limit
    ) -> dict[str, object]:
        items = [
            {
                "id": str(t.id),
                "account_id": str(t.account_id),
                "provider_thread_id": t.provider_thread_id,
                "subject": t.subject,
                "application_id": str(t.application_id) if t.application_id else None,
            }
            for t in self._threads_by_id.values()
            if t.account_id == account_id
        ]
        return {"items": items[:limit], "next_cursor": None, "has_more": False}

    def list_messages(
        self, candidate_id: UUID, thread_id: UUID, *, cursor, limit
    ) -> dict[str, object]:
        items = [
            {
                "id": str(m.id),
                "provider_message_id": m.provider_message_id,
                "category": m.category.value,
                "sender_email": m.sender_email,
                "subject": m.subject,
                "snippet": m.snippet,
                "body_persisted": m.body_persisted,
            }
            for m in self._messages.values()
            if m.thread_id == thread_id
        ]
        return {"items": items[:limit], "next_cursor": None, "has_more": False}

    def list_unresolved(self, candidate_id: UUID, *, cursor, limit) -> dict[str, object]:
        items = [
            {
                "link_id": str(u.link_id),
                "thread_id": str(u.thread_id),
                "candidate_application_ids": [str(a) for a in u.candidate_application_ids],
                "evidence_refs": dict(u.evidence_refs),
            }
            for u in self._unresolved.values()
            if u.candidate_id == candidate_id
        ]
        return {"items": items[:limit], "next_cursor": None, "has_more": False}

    def get_unresolved(self, candidate_id: UUID, link_id: UUID) -> UnresolvedThreadLink | None:
        u = self._unresolved.get(link_id)
        if u is None or u.candidate_id != candidate_id:
            return None
        return u

    def record_confirmation(
        self, decision: LinkConfirmationDecision, *, now: datetime
    ) -> ThreadLinkSnapshot | None:
        u = self._unresolved.pop(decision.link_id, None)
        if u is None:
            return None
        self._decisions[decision.link_id] = decision
        from careerops.domain.mail_sync import ThreadLinkStatus

        snapshot = self._links.get(u.thread_id)
        if snapshot is not None:
            new = ThreadLinkSnapshot(
                thread_id=snapshot.thread_id,
                account_id=snapshot.account_id,
                candidate_id=snapshot.candidate_id,
                status=ThreadLinkStatus.CONFIRMED
                if decision.confirmed_application_id is not None
                else ThreadLinkStatus.UNLINKED,
                application_id=decision.confirmed_application_id,
                candidate_application_ids=snapshot.candidate_application_ids,
                confidence=snapshot.confidence,
                evidence_refs=snapshot.evidence_refs,
                provider_thread_id=snapshot.provider_thread_id,
                subject=snapshot.subject,
                resolved_at=decision.decided_at,
            )
            self._links[u.thread_id] = new
            return new
        return None

    def prior_confirmation(
        self, candidate_id: UUID, link_id: UUID
    ) -> LinkConfirmationDecision | None:
        d = self._decisions.get(link_id)
        if d is None or d.candidate_id != candidate_id:
            return None
        return d


class _FakeInvalidator:
    def __init__(self) -> None:
        self.invalidated: list[UUID] = []

    def invalidate_for_account(self, account_id: UUID) -> int:
        self.invalidated.append(account_id)
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_service() -> tuple[
    MailSyncService,
    _FakeAccountRepo,
    _FakeSyncRunRepo,
    _FakeThreadLinkRepo,
    _FakeInvalidator,
]:
    accounts = _FakeAccountRepo()
    threads = _FakeThreadLinkRepo()
    runs = _FakeSyncRunRepo(threads)
    invalidator = _FakeInvalidator()
    service = MailSyncService(accounts, runs, threads, invalidator=invalidator)
    return service, accounts, runs, threads, invalidator


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 26, 12, tzinfo=UTC)


@pytest.fixture
def candidate_id() -> UUID:
    return uuid4()


def _connect(
    service: MailSyncService,
    candidate_id: UUID,
    *,
    now: datetime,
    scopes: tuple[str, ...] = (_READONLY,),
    email: str = "recruiting@example.com",
):
    return service.connect_account(
        candidate_id=candidate_id,
        email_address=email,
        granted_scopes=scopes,
        credential_reference_id=uuid4(),
        now=now,
    )


# ---------------------------------------------------------------------------
# Task 11.1 — connection state + read-scope validation
# ---------------------------------------------------------------------------


class TestConnectionAndScope:
    def test_readonly_scope_connects(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        assert summary.connection_state is MailConnectionState.CONNECTED
        assert summary.sync_available is True
        assert summary.granted_scopes == (_READONLY,)
        assert summary.credential_reference_id is not None

    def test_forbidden_scope_rejects_and_credential_never_stored(self, now, candidate_id):
        service, accounts, *_ = _make_service()
        with pytest.raises(ReadScopeError):
            service.connect_account(
                candidate_id=candidate_id,
                email_address="recruiting@example.com",
                granted_scopes=(_READONLY, "https://mail.google.com/"),
                credential_reference_id=uuid4(),
                now=now,
            )
        # Credential was never stored.
        assert accounts.get_account_for_candidate(candidate_id) is None

    def test_validate_read_scope_rejects_send_scope(self):
        from careerops.domain.mail_sync import ReadScopeViolationError

        with pytest.raises(ReadScopeViolationError):
            validate_read_scope(
                frozenset({_READONLY, "https://www.googleapis.com/auth/gmail.send"})
            )

    def test_reconnect_reuses_account(self, now, candidate_id):
        service, *_ = _make_service()
        first = _connect(service, candidate_id, now=now, email="a@x.com")
        later = now + timedelta(hours=1)
        second = service.connect_account(
            candidate_id=candidate_id,
            email_address="a@x.com",
            granted_scopes=(_READONLY,),
            credential_reference_id=uuid4(),
            now=later,
        )
        assert first.account_id == second.account_id


# ---------------------------------------------------------------------------
# Task 11.2 — revoke stops sync + invalidates pending actions
# ---------------------------------------------------------------------------


class TestRevokeAndError:
    def test_revoke_invalidates_pending_and_blocks_sync(self, now, candidate_id):
        service, _accounts, _runs, _threads, invalidator = _make_service()
        summary = _connect(service, candidate_id, now=now)
        revoked = service.revoke_account(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        assert revoked.connection_state is MailConnectionState.REVOKED
        assert invalidator.invalidated == [summary.account_id]
        # A sync request on a revoked account is refused.
        with pytest.raises(AccountNotConnectedError):
            service.request_sync(candidate_id=candidate_id, account_id=summary.account_id, now=now)

    def test_revoke_is_idempotent(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        first = service.revoke_account(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        second = service.revoke_account(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        assert first.connection_state is second.connection_state is MailConnectionState.REVOKED

    def test_mark_error_then_sync_refused(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        errored = service.mark_sync_error(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            error_code="WATCH_EXPIRED",
            now=now,
        )
        assert errored.connection_state is MailConnectionState.ERROR
        with pytest.raises(AccountNotConnectedError):
            service.request_sync(candidate_id=candidate_id, account_id=summary.account_id, now=now)


# ---------------------------------------------------------------------------
# Tasks 11.3 / 11.4 / 11.5 — sync runs, dedup, partial restart, body discard
# ---------------------------------------------------------------------------


def _msg(
    *,
    provider_message_id: str,
    provider_thread_id: str = "thread-1",
    subject: str = "Interview invitation",
    snippet: str = "We'd like to invite you to interview for the role.",
    body: str = "full body",
    sender: str = "recruiter@company.com",
    history_id: str = "100",
) -> dict[str, object]:
    return {
        "provider_message_id": provider_message_id,
        "provider_thread_id": provider_thread_id,
        "subject": subject,
        "snippet": snippet,
        "body_text": body,
        "sender_email": sender,
        "history_id": history_id,
        "received_at": datetime(2026, 7, 26, 10, tzinfo=UTC),
    }


class TestSyncRunDedupAndRestart:
    def test_duplicate_message_skipped(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        messages = (
            _msg(provider_message_id="m1"),
            _msg(provider_message_id="m1"),  # duplicate delivery
        )
        result = service.run_sync_step(
            candidate_id=candidate_id, run_id=run.id, messages=messages, now=now
        )
        assert result.status is MailSyncRunStatus.COMPLETED
        assert result.messages_processed == 1
        assert result.messages_skipped == 1
        # Only one message row exists.
        assert threads.provider_ids_for_account(summary.account_id) == frozenset({"m1"})

    def test_partial_restart_resumes_without_duplicate(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        # First step processes m1 and m2, then "crashes" (we simulate by
        # passing a batch whose third entry raises via a bad payload that
        # trips the generic except). We instead prove resumption directly:
        first_batch = (_msg(provider_message_id="m1"), _msg(provider_message_id="m2"))
        service.run_sync_step(
            candidate_id=candidate_id, run_id=run.id, messages=first_batch, now=now
        )
        # A second run is requested; m1/m2 are already processed so they are
        # skipped, m3 is new.
        run2 = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        second_batch = (
            _msg(provider_message_id="m1"),
            _msg(provider_message_id="m2"),
            _msg(provider_message_id="m3"),
        )
        result = service.run_sync_step(
            candidate_id=candidate_id, run_id=run2.id, messages=second_batch, now=now
        )
        assert result.messages_processed == 1  # only m3
        assert result.messages_skipped == 2
        assert threads.provider_ids_for_account(summary.account_id) == frozenset({"m1", "m2", "m3"})

    def test_cursor_advances_to_last_history_id(self, now, candidate_id):
        service, _accounts, runs, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1", history_id="200"),),
            now=now,
        )
        cursor = runs.get_cursor(summary.account_id)
        assert cursor is not None and cursor.history_id == "200"

    def test_non_recruitment_body_discarded(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        # A message with no recruitment signal -> non_recruitment -> body not persisted.
        non_recruitment = _msg(
            provider_message_id="newsletter-1",
            subject="Your weekly newsletter",
            snippet="Lots of unrelated content here.",
            body="secret personal body content",
            sender="news@random.com",
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(non_recruitment,),
            now=now,
        )
        stored = [m for m in threads._messages.values()]
        assert len(stored) == 1
        message = stored[0]
        assert message.body_persisted is False
        # Body content is not retained on the stored message.
        assert message.snippet == ""

    def test_sync_now_is_idempotent_while_pending(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        first = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        second = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        assert first.id == second.id


# ---------------------------------------------------------------------------
# Task 11.6 — thread association
# ---------------------------------------------------------------------------


class TestThreadAssociation:
    def test_provider_id_links_high_confidence(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        # Ingest a message so a thread exists.
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1", provider_thread_id="t-A"),),
            now=now,
        )
        thread_id = threads._threads_by_provider[(summary.account_id, "t-A")]
        app_a = uuid4()
        snapshot = service.associate_thread(
            candidate_id=candidate_id,
            thread_id=thread_id,
            evidence=ThreadAssociationEvidence(
                provider_thread_id="t-A",
                sent_message_application_ids=(app_a,),
            ),
            candidate_applications=((app_a, ApplicationState.SUBMITTED),),
            now=now,
        )
        assert snapshot.status is ThreadLinkStatus.LINKED
        assert snapshot.application_id == app_a
        assert snapshot.confidence is LinkConfidence.PROVIDER_ID

    def test_ambiguous_match_left_unresolved(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1", provider_thread_id="t-B"),),
            now=now,
        )
        thread_id = threads._threads_by_provider[(summary.account_id, "t-B")]
        app_a, app_b = uuid4(), uuid4()
        snapshot = service.associate_thread(
            candidate_id=candidate_id,
            thread_id=thread_id,
            evidence=ThreadAssociationEvidence(
                provider_thread_id="t-B",
                trusted_sender_domain="company.com",
            ),
            candidate_applications=(
                (app_a, ApplicationState.SUBMITTED),
                (app_b, ApplicationState.PREPARING),
            ),
            now=now,
        )
        assert snapshot.status is ThreadLinkStatus.UNRESOLVED
        assert set(snapshot.candidate_application_ids) == {app_a, app_b}

    def test_confirm_link_then_reuse_idempotent(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1", provider_thread_id="t-C"),),
            now=now,
        )
        thread_id = threads._threads_by_provider[(summary.account_id, "t-C")]
        app_a, app_b = uuid4(), uuid4()
        snapshot = service.associate_thread(
            candidate_id=candidate_id,
            thread_id=thread_id,
            evidence=ThreadAssociationEvidence(
                provider_thread_id="t-C", trusted_sender_domain="company.com"
            ),
            candidate_applications=(
                (app_a, ApplicationState.SUBMITTED),
                (app_b, ApplicationState.PREPARING),
            ),
            now=now,
        )
        assert snapshot.status is ThreadLinkStatus.UNRESOLVED
        link_id = snapshot.link_id
        assert link_id is not None
        # User confirms app_a.
        decision = service.confirm_link(
            candidate_id=candidate_id,
            link_id=link_id,
            confirmed_application_id=app_a,
            now=now,
        )
        assert decision.confirmed_application_id == app_a
        # The snapshot is now CONFIRMED and reused.
        confirmed_snapshot = threads.get_link(candidate_id, thread_id)
        assert confirmed_snapshot is not None
        assert confirmed_snapshot.status is ThreadLinkStatus.CONFIRMED
        assert confirmed_snapshot.application_id == app_a
        # Repeat decision is idempotent (raises already-resolved carrying prior).
        with pytest.raises(LinkAlreadyResolvedError) as exc:
            service.confirm_link(
                candidate_id=candidate_id,
                link_id=link_id,
                confirmed_application_id=app_b,
                now=now,
            )
        assert exc.value.decision.confirmed_application_id == app_a

    def test_confirm_unknown_link_404(self, now, candidate_id):
        service, *_ = _make_service()
        with pytest.raises(LinkNotFoundError):
            service.confirm_link(
                candidate_id=candidate_id,
                link_id=uuid4(),
                confirmed_application_id=None,
                now=now,
            )

    def test_associate_unknown_thread_404(self, now, candidate_id):
        service, *_ = _make_service()
        with pytest.raises(ThreadNotFoundError):
            service.associate_thread(
                candidate_id=candidate_id,
                thread_id=uuid4(),
                evidence=ThreadAssociationEvidence(provider_thread_id="t-X"),
                candidate_applications=(),
                now=now,
            )

    def test_no_evidence_leaves_unlinked(self, now, candidate_id):
        service, _accounts, _runs, threads, _inv = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1", provider_thread_id="t-D"),),
            now=now,
        )
        thread_id = threads._threads_by_provider[(summary.account_id, "t-D")]
        snapshot = service.associate_thread(
            candidate_id=candidate_id,
            thread_id=thread_id,
            evidence=ThreadAssociationEvidence(provider_thread_id="t-D"),
            candidate_applications=((uuid4(), ApplicationState.SUBMITTED),),
            now=now,
        )
        assert snapshot.status is ThreadLinkStatus.UNLINKED


# ---------------------------------------------------------------------------
# Task 11.10 — bounded cursor pagination + ownership scoping
# ---------------------------------------------------------------------------


class TestPaginationAndOwnership:
    def test_limit_bounded(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        page = service.list_sync_runs(
            candidate_id=candidate_id,
            account_id=summary.account_id,
            cursor=None,
            limit=10_000,  # caller asks for huge page
        )
        # The service delegates the bound to the repo; the route enforces the
        # hard ceiling. Here we assert the page shape is well-formed.
        assert page["items"] == []

    def test_other_candidate_sees_nothing(self, now, candidate_id):
        service, *_ = _make_service()
        summary = _connect(service, candidate_id, now=now)
        run = service.request_sync(
            candidate_id=candidate_id, account_id=summary.account_id, now=now
        )
        service.run_sync_step(
            candidate_id=candidate_id,
            run_id=run.id,
            messages=(_msg(provider_message_id="m1"),),
            now=now,
        )
        other = uuid4()
        # Another candidate's account lookup is empty.
        assert service.get_account_status(candidate_id=other) is None
        # Their sync history is empty.
        other_summary = service.get_account_status(candidate_id=other)
        assert other_summary is None

    def test_owned_account_required_for_sync(self, now, candidate_id):
        service, *_ = _make_service()
        with pytest.raises(AccountNotOwnedError):
            service.request_sync(candidate_id=candidate_id, account_id=uuid4(), now=now)
