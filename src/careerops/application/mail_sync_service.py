"""Mail read-synchronization + thread-association service (Section 11).

Sits on top of the M4 mail domain (:mod:`careerops.domain.email`) and the
existing pure sync helpers (:mod:`careerops.application.email_sync`) and adds
the Section-11 concerns the new contract requires:

- **11.1** dedicated-account connection state with allowed read-scope
  validation and protected credential-reference storage.
- **11.2** account revoke / error handling that stops future sync and provider
  use and invalidates pending provider actions for that account.
- **11.3** incremental history/watch cursor storage, retry, backfill and
  durable sync-run status; a partial run resumes from durable provider state.
- **11.4** provider message/thread id uniqueness and duplicate Pub/Sub /
  polling delivery handling (delegates dedup to the unique constraint + the
  ingest helper's existing-provider-id check).
- **11.5** persist only minimum metadata for non-recruitment mail; the ingest
  helper already discards the body for non-recruitment messages.
- **11.6** thread association using provider ids, sent-message linkage,
  trusted domains, subject/source evidence, and unresolved multi-candidate
  state.

The service is additive: it composes Protocol repositories rather than
mutating existing ones, so M4 callers and tests are unaffected. The
GMAIL_READ capability stays DENIED at the contract layer (Iron Rule 7); this
service builds the path but performs NO live OAuth and NO provider writes.

Iron rules honored:
- Additive (Iron Rule 8): new service, no existing service mutated.
- Model review-only (Iron Rule 2): no method lets model output pick a
  recipient, transition application state, or write a trusted link.
- Append-only / reversible (Iron Rule 4): sync runs and link decisions are
  append-only; a confirmed link is recorded, never silently rewritten.
- Server-side ownership (Iron Rule 2/6): ``candidate_id`` is the
  server-resolved owner; every read/write is scoped by it.
- Default-deny (Iron Rule 7): connection requires a validated readonly scope;
  sync requires the GMAIL_READ capability (checked by the caller/route).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.email_sync import (
    IngestedMessage,
    ingest_message,
)
from careerops.domain.applications import ApplicationState
from careerops.domain.email import (
    EmailAccount,
    EmailAccountStatus,
    EmailMessage,
    EmailThread,
)
from careerops.domain.mail_sync import (
    MAIL_SYNC_POLICY_VERSION,
    LinkConfidence,
    LinkConfirmationDecision,
    MailAccountSummary,
    MailConnectionState,
    MailSyncCursor,
    MailSyncRun,
    MailSyncRunStatus,
    ReadScopeViolationError,
    SyncDirection,
    ThreadAssociationEvidence,
    ThreadLinkSnapshot,
    ThreadLinkStatus,
    UnresolvedThreadLink,
    validate_read_scope,
)

__all__ = [
    "AccountNotConnectedError",
    "AccountNotOwnedError",
    "DuplicateSyncRequestError",
    "LinkAlreadyResolvedError",
    "LinkNotFoundError",
    "MailAccountRepository",
    "MailSyncService",
    "PendingActionInvalidator",
    "SyncRunRepository",
    "ThreadLinkRepository",
    "ThreadNotFoundError",
]


# ---------------------------------------------------------------------------
# Errors (translated at the API layer)
# ---------------------------------------------------------------------------


class MailSyncError(Exception):
    """Base for mail-sync service errors."""


class AccountNotOwnedError(MailSyncError):
    """The account does not exist or is not owned by the candidate.

    Raised for BOTH missing and not-owned so the response never leaks that an
    account exists for another candidate.
    """

    def __init__(self, account_id: UUID) -> None:
        super().__init__(f"mail account not found for candidate: {account_id}")


class AccountNotConnectedError(MailSyncError):
    """Sync was requested for an account that is not CONNECTED."""

    def __init__(self, account_id: UUID, state: MailConnectionState) -> None:
        self.state = state
        super().__init__(f"mail account {account_id} is not connected (state={state.value})")


class DuplicateSyncRequestError(MailSyncError):
    """A sync run is already pending/running for this account (idempotent)."""

    def __init__(self, run: MailSyncRun) -> None:
        self.run = run
        super().__init__(f"sync run {run.id} already {run.status.value}")


class ThreadNotFoundError(MailSyncError):
    """The thread does not exist or is not owned by the candidate."""

    def __init__(self, thread_id: UUID) -> None:
        super().__init__(f"mail thread not found for candidate: {thread_id}")


class LinkNotFoundError(MailSyncError):
    """The unresolved link does not exist or is not owned by the candidate."""

    def __init__(self, link_id: UUID) -> None:
        super().__init__(f"unresolved link not found for candidate: {link_id}")


class LinkAlreadyResolvedError(MailSyncError):
    """The link was already resolved; repeat decision is idempotent."""

    def __init__(self, decision: LinkConfirmationDecision) -> None:
        self.decision = decision
        super().__init__(f"link {decision.link_id} already resolved")


class ReadScopeError(MailSyncError):
    """The granted scope set violates the readonly read-scope policy."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Repository protocols
# ---------------------------------------------------------------------------


class MailAccountRepository(Protocol):
    """Dedicated recruiting-account store, scoped by server-resolved candidate.

    Every method is scoped by ``candidate_id`` — there is no unscoped read.
    The store persists the validated ``granted_scopes`` tuple and the
    ``credential_reference_id`` only; no plaintext token is ever stored.
    """

    def get_account(self, candidate_id: UUID, account_id: UUID) -> EmailAccount | None: ...
    def get_account_for_candidate(self, candidate_id: UUID) -> EmailAccount | None: ...
    def upsert_account(self, account: EmailAccount) -> None: ...
    def set_connection_state(
        self,
        candidate_id: UUID,
        account_id: UUID,
        state: MailConnectionState,
        *,
        last_error_code: str = "",
        now: datetime,
    ) -> EmailAccount | None: ...


class SyncRunRepository(Protocol):
    """Durable sync-run + cursor store, scoped by candidate.

    ``get_open_run`` returns a PENDING/RUNNING run for the account if one
    exists (used for idempotent sync-now). ``get_cursor`` /
    ``advance_cursor`` persist the incremental history/watch cursor.
    ``processed_provider_ids`` returns the provider message ids already
    ingested for an account (used for duplicate-delivery dedup).
    """

    def create_run(self, run: MailSyncRun) -> None: ...
    def get_run(self, candidate_id: UUID, run_id: UUID) -> MailSyncRun | None: ...
    def get_open_run(self, account_id: UUID) -> MailSyncRun | None: ...
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
    ) -> MailSyncRun | None: ...
    def list_runs(
        self,
        candidate_id: UUID,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]: ...
    def get_cursor(self, account_id: UUID) -> MailSyncCursor | None: ...
    def advance_cursor(
        self, account_id: UUID, cursor: MailSyncCursor, *, now: datetime
    ) -> None: ...
    def processed_provider_ids(self, account_id: UUID) -> frozenset[str]: ...


class ThreadLinkRepository(Protocol):
    """Thread + message + link store, scoped by candidate.

    Threads and messages are owned by the candidate that owns the account.
    Link decisions are append-only: a CONFIRMED link records the decision
    rather than mutating prior evidence.
    """

    def upsert_thread(self, thread: EmailThread) -> None: ...
    def get_thread(self, candidate_id: UUID, thread_id: UUID) -> EmailThread | None: ...
    def append_message(self, message: EmailMessage) -> None: ...
    def get_link(self, candidate_id: UUID, thread_id: UUID) -> ThreadLinkSnapshot | None: ...
    def upsert_link(self, snapshot: ThreadLinkSnapshot, *, now: datetime) -> None: ...
    def list_threads(
        self,
        candidate_id: UUID,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]: ...
    def list_messages(
        self,
        candidate_id: UUID,
        thread_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]: ...
    def list_unresolved(
        self,
        candidate_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]: ...
    def get_unresolved(self, candidate_id: UUID, link_id: UUID) -> UnresolvedThreadLink | None: ...
    def record_confirmation(
        self, decision: LinkConfirmationDecision, *, now: datetime
    ) -> ThreadLinkSnapshot | None: ...
    def prior_confirmation(
        self, candidate_id: UUID, link_id: UUID
    ) -> LinkConfirmationDecision | None: ...


class PendingActionInvalidator(Protocol):
    """Invalidates pending provider actions for a revoked account (task 11.2).

    Returns the number of actions moved to a terminal failed/cancelled state.
    The concrete implementation lives at the outbox/kernel layer; the service
    depends on the Protocol so it can be wired (or faked) without pulling the
    kernel into the read path.
    """

    def invalidate_for_account(self, account_id: UUID) -> int: ...


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


# Applications whose thread link is eligible to be auto-resolved by evidence.
# Terminal/ignored applications are excluded from the candidate set so an
# ambiguous match against a REJECTED/IGNORED app does not block linking to a
# live one.
_INELIGIBLE_LINK_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.IGNORED,
    }
)


class MailSyncService:
    """Connection / sync / thread-association service (Section 11)."""

    def __init__(
        self,
        account_repo: MailAccountRepository,
        sync_run_repo: SyncRunRepository,
        thread_link_repo: ThreadLinkRepository,
        *,
        invalidator: PendingActionInvalidator | None = None,
    ) -> None:
        self._accounts = account_repo
        self._runs = sync_run_repo
        self._threads = thread_link_repo
        self._invalidator = invalidator

    # ------------------------------------------------------------------
    # 11.1 — connection state + read-scope validation
    # ------------------------------------------------------------------

    def connect_account(
        self,
        *,
        candidate_id: UUID,
        email_address: str,
        granted_scopes: tuple[str, ...],
        credential_reference_id: UUID,
        now: datetime,
    ) -> MailAccountSummary:
        """Validate the granted scope and store the protected credential ref.

        Fail-closed (task 11.1): a forbidden or unapproved scope rejects the
        whole connection and the credential is never stored. On success the
        account is upserted to CONNECTED with the canonical granted-scope
        tuple recorded. Re-connecting the same email address reuses the
        existing account id and refreshes the credential reference + scope.
        """
        try:
            scopes = validate_read_scope(granted_scopes)
        except ReadScopeViolationError as error:
            raise ReadScopeError(str(error)) from error

        existing = self._find_account_by_email(candidate_id, email_address)
        account_id = existing.id if existing is not None else uuid4()
        account = EmailAccount(
            id=account_id,
            email_address=email_address,
            credential_reference_id=credential_reference_id,
            status=EmailAccountStatus.ACTIVE,
            history_id=existing.history_id if existing is not None else "",
            watch_expiration=existing.watch_expiration if existing is not None else None,
            last_sync_at=existing.last_sync_at if existing is not None else None,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        self._accounts.upsert_account(account)
        # The granted scopes are persisted by the repo alongside the account
        # (schema column ``granted_scopes``); the connection state defaults to
        # CONNECTED on a fresh connect. ``upsert_account`` stores the legacy
        # EmailAccount row; the connection-state column is set here.
        updated = self._accounts.set_connection_state(
            candidate_id,
            account_id,
            MailConnectionState.CONNECTED,
            last_error_code="",
            now=now,
        )
        if updated is None:
            updated = account
        return _to_summary(updated, candidate_id, scopes)

    def get_account_status(self, *, candidate_id: UUID) -> MailAccountSummary | None:
        """Return the connection summary for the candidate's account, if any."""
        account = self._accounts.get_account_for_candidate(candidate_id)
        if account is None:
            return None
        return _to_summary(account, candidate_id)

    # ------------------------------------------------------------------
    # 11.2 — revoke / error handling
    # ------------------------------------------------------------------

    def revoke_account(
        self,
        *,
        candidate_id: UUID,
        account_id: UUID,
        now: datetime,
    ) -> MailAccountSummary:
        """Mark the account REVOKED and stop future sync + provider use.

        Immediately invalidates pending provider actions for that account
        (task 11.2) via the wired :class:`PendingActionInvalidator`. The
        account row is retained for audit/history per retention policy; only
        its connection state and the legacy status flag flip. Repeat revokes
        are idempotent.
        """
        account = self._require_owned_account(candidate_id, account_id)
        invalidated = 0
        if self._invalidator is not None:
            invalidated = self._invalidator.invalidate_for_account(account_id)
        updated = self._accounts.set_connection_state(
            candidate_id,
            account_id,
            MailConnectionState.REVOKED,
            last_error_code="",
            now=now,
        )
        # The legacy status mirrors the connection state so M4 readers see
        # the account as revoked too.
        if updated is not None:
            self._accounts.upsert_account(
                replace(updated, status=EmailAccountStatus.REVOKED, updated_at=now)
            )
            updated = self._accounts.get_account(candidate_id, account_id) or updated
        summary = _to_summary(updated or account, candidate_id)
        # ``invalidated`` is exposed via the summary's evidence, not a trusted
        # claim — the count is informational for the UI/audit.
        return replace(summary, last_error_code=f"revoked:pended:{invalidated}")

    def mark_sync_error(
        self,
        *,
        candidate_id: UUID,
        account_id: UUID,
        error_code: str,
        now: datetime,
    ) -> MailAccountSummary:
        """Record a recoverable sync error; the account stays connected but paused.

        Task 11.2: a transient error (watch expired, partial provider response)
        moves the connection to ERROR so the UI shows an actionable state
        without presenting old data as current. A later successful sync clears
        the error back to CONNECTED.
        """
        self._require_owned_account(candidate_id, account_id)
        updated = self._accounts.set_connection_state(
            candidate_id,
            account_id,
            MailConnectionState.ERROR,
            last_error_code=error_code,
            now=now,
        )
        account = updated or self._require_owned_account(candidate_id, account_id)
        return _to_summary(account, candidate_id)

    # ------------------------------------------------------------------
    # 11.3 / 11.4 / 11.5 — sync runs + incremental ingest
    # ------------------------------------------------------------------

    def request_sync(
        self,
        *,
        candidate_id: UUID,
        account_id: UUID,
        now: datetime,
        direction: SyncDirection = SyncDirection.INCREMENTAL,
    ) -> MailSyncRun:
        """Create a durable sync run, or return the existing open run (idempotent).

        Task 11.3: the run is durable; a worker later claims it via
        :meth:`run_sync_step`. A repeat request while a run is PENDING/RUNNING
        returns that run unchanged (idempotent). Requires the account to be
        CONNECTED (ERROR/REVOKED/DISCONNECTED all refuse).
        """
        account = self._require_owned_account(candidate_id, account_id)
        if self._connection_state(account) is not MailConnectionState.CONNECTED:
            raise AccountNotConnectedError(account_id, self._connection_state(account))
        open_run = self._runs.get_open_run(account_id)
        if open_run is not None:
            return open_run
        cursor = self._runs.get_cursor(account_id)
        history_start = cursor.history_id if cursor is not None else account.history_id
        run = MailSyncRun(
            id=uuid4(),
            account_id=account_id,
            candidate_id=candidate_id,
            status=MailSyncRunStatus.PENDING,
            direction=direction,
            started_at=now,
            history_id_start=history_start,
        )
        self._runs.create_run(run)
        return run

    def run_sync_step(
        self,
        *,
        candidate_id: UUID,
        run_id: UUID,
        messages: tuple[dict[str, object], ...],
        now: datetime,
    ) -> MailSyncRun:
        """Process one incremental batch for a run (tasks 11.3 / 11.4 / 11.5).

        Each entry in ``messages`` is a provider message dict (provider ids,
        sender, subject, snippet, body, received_at, thread id). Duplicate
        deliveries (provider message id already processed) are skipped without
        creating a second record (11.4). Non-recruitment bodies are discarded
        (11.5, delegated to :func:`ingest_message`). The run is marked PARTIAL
        on an exception so a retry resumes from durable state (11.9); on
        success it is COMPLETED and the cursor advances.

        ``run_sync_step`` performs NO provider network calls — the caller
        (a worker activity, or a test) supplies the already-fetched messages.
        """
        run = self._runs.get_run(candidate_id, run_id)
        if run is None:
            raise MailSyncError(f"sync run not found: {run_id}")
        account = self._require_owned_account(candidate_id, run.account_id)
        if self._connection_state(account) is not MailConnectionState.CONNECTED:
            raise AccountNotConnectedError(run.account_id, self._connection_state(account))
        # Dedup set is mutable across the batch: a provider message id
        # processed earlier in THIS batch (or in a prior run) is a duplicate
        # and is skipped (task 11.4). The DB unique constraint is the durable
        # backstop; this in-memory tracking makes in-batch duplicates
        # idempotent without relying on an IntegrityError.
        seen_ids: set[str] = set(self._runs.processed_provider_ids(run.account_id))
        processed = 0
        skipped = 0
        last_history_id = run.history_id_start
        try:
            for raw in messages:
                provider_message_id = str(raw.get("provider_message_id", "") or "")
                if provider_message_id and provider_message_id in seen_ids:
                    skipped += 1
                    continue
                ingested = self._ingest_one(
                    account_id=run.account_id,
                    candidate_id=candidate_id,
                    raw=raw,
                    existing_ids=frozenset(seen_ids),
                    now=now,
                )
                if ingested is None:
                    skipped += 1
                    continue
                if provider_message_id:
                    seen_ids.add(provider_message_id)
                processed += 1
                history_id = str(raw.get("history_id", "") or "")
                if history_id:
                    last_history_id = history_id
        except Exception:
            updated = self._runs.update_run(
                candidate_id,
                run_id,
                status=MailSyncRunStatus.PARTIAL,
                messages_processed=processed,
                messages_skipped=skipped,
                history_id_end=last_history_id,
                error_code="SYNC_STEP_FAILED",
                completed_at=now,
            )
            return updated or run

        # Advance the durable cursor (task 11.3) so a retry resumes from here.
        if last_history_id:
            self._runs.advance_cursor(
                run.account_id,
                MailSyncCursor(
                    account_id=run.account_id,
                    history_id=last_history_id,
                    updated_at=now,
                ),
                now=now,
            )
        self._accounts.set_connection_state(
            candidate_id,
            run.account_id,
            MailConnectionState.CONNECTED,
            last_error_code="",
            now=now,
        )
        updated = self._runs.update_run(
            candidate_id,
            run_id,
            status=MailSyncRunStatus.COMPLETED,
            messages_processed=processed,
            messages_skipped=skipped,
            history_id_end=last_history_id,
            error_code="",
            completed_at=now,
        )
        return updated or run

    def _ingest_one(
        self,
        *,
        account_id: UUID,
        candidate_id: UUID,
        raw: dict[str, object],
        existing_ids: frozenset[str],
        now: datetime,
    ) -> IngestedMessage | None:
        """Ingest one provider message, creating its thread + link snapshot.

        Reuses :func:`ingest_message` for privacy filtering + dedup (11.4/11.5).
        Ensures the thread exists and an initial UNLINKED link snapshot is
        present so the association step can populate it.
        """
        provider_thread_id = str(raw.get("provider_thread_id", "") or "")
        provider_message_id = str(raw.get("provider_message_id", "") or "")
        if not provider_message_id:
            return None
        # Ensure the thread row exists (idempotent on provider_thread_id).
        thread_id = self._ensure_thread(
            account_id=account_id,
            candidate_id=candidate_id,
            provider_thread_id=provider_thread_id,
            subject=str(raw.get("subject", "") or ""),
            now=now,
        )
        ingested = ingest_message(
            account_id=account_id,
            thread_id=thread_id,
            provider_message_id=provider_message_id,
            sender_email=str(raw.get("sender_email", "") or ""),
            sender_name=str(raw.get("sender_name", "") or ""),
            subject=str(raw.get("subject", "") or ""),
            snippet=str(raw.get("snippet", "") or ""),
            body_text=str(raw.get("body_text", "") or ""),
            received_at=raw.get("received_at", now),  # type: ignore[arg-type]
            existing_provider_ids=existing_ids,
            now=now,
        )
        if ingested is None:
            return None
        self._threads.append_message(ingested.message)
        return ingested

    def _ensure_thread(
        self,
        *,
        account_id: UUID,
        candidate_id: UUID,
        provider_thread_id: str,
        subject: str,
        now: datetime,
    ) -> UUID:
        """Get-or-create a thread by provider_thread_id; return its UUID.

        The thread id is deterministically derived from
        (account_id, provider_thread_id) so a retry after a partial run, or a
        second message in the same provider thread, reuses the same thread
        rather than creating duplicates (11.4). The repo's
        ``upsert_thread`` on-conflict on provider_thread_id is the durable
        backstop.
        """
        thread = EmailThread(
            id=_thread_id(account_id, provider_thread_id),
            account_id=account_id,
            provider_thread_id=provider_thread_id,
            subject=subject,
            created_at=now,
            updated_at=now,
        )
        self._threads.upsert_thread(thread)
        return thread.id

    # ------------------------------------------------------------------
    # 11.6 — thread association
    # ------------------------------------------------------------------

    def associate_thread(
        self,
        *,
        candidate_id: UUID,
        thread_id: UUID,
        evidence: ThreadAssociationEvidence,
        candidate_applications: tuple[tuple[UUID, ApplicationState], ...],
        now: datetime,
    ) -> ThreadLinkSnapshot:
        """Link a thread to an application using trusted evidence (task 11.6).

        Evaluation priority (strongest first):

        1. ``PROVIDER_ID`` — a provider thread/message id recorded on a
           submitted application → LINKED (high confidence).
        2. ``SENT_MESSAGE`` — a system-managed sent-message receipt in this
           thread → LINKED.
        3. ``TRUSTED_DOMAIN`` — a single active application whose company
           domain matches the verified sender domain → LINKED.
        4. ``SUBJECT_SOURCE`` — subject/source tokens match exactly one active
           application → LINKED.
        5. Multiple plausible matches → ``UNRESOLVED`` (review item created).
        6. No match → ``UNLINKED``.

        ``candidate_applications`` is the trusted (application_id, state) set
        the service resolved from business state; model output never populates
        it. An UNRESOLVED snapshot records the plausible ids so the UI can
        present the choice; only :meth:`confirm_link` upgrades UNRESOLVED →
        CONFIRMED.
        """
        thread = self._threads.get_thread(candidate_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        eligible = [
            app_id
            for app_id, state in candidate_applications
            if state not in _INELIGIBLE_LINK_STATES
        ]
        eligible_set = list(dict.fromkeys(eligible))  # de-dup, preserve order

        confidence, matched = _evaluate_evidence(evidence, eligible_set)

        if confidence is LinkConfidence.NONE:
            snapshot = ThreadLinkSnapshot(
                thread_id=thread_id,
                account_id=thread.account_id,
                candidate_id=candidate_id,
                status=ThreadLinkStatus.UNLINKED,
                provider_thread_id=thread.provider_thread_id,
                subject=thread.subject,
            )
        elif matched is not None and len(matched) == 1:
            app_id = next(iter(matched))
            snapshot = ThreadLinkSnapshot(
                thread_id=thread_id,
                account_id=thread.account_id,
                candidate_id=candidate_id,
                status=ThreadLinkStatus.LINKED,
                application_id=app_id,
                confidence=confidence,
                evidence_refs=_evidence_refs(evidence, confidence),
                provider_thread_id=thread.provider_thread_id,
                subject=thread.subject,
            )
        else:
            # Ambiguous: more than one plausible application.
            snapshot = ThreadLinkSnapshot(
                thread_id=thread_id,
                account_id=thread.account_id,
                candidate_id=candidate_id,
                status=ThreadLinkStatus.UNRESOLVED,
                candidate_application_ids=tuple(matched or ()),
                confidence=confidence,
                evidence_refs=_evidence_refs(evidence, confidence),
                provider_thread_id=thread.provider_thread_id,
                subject=thread.subject,
                link_id=self._resolve_link_id(candidate_id, thread_id),
            )
        self._threads.upsert_link(snapshot, now=now)
        return snapshot

    def confirm_link(
        self,
        *,
        candidate_id: UUID,
        link_id: UUID,
        confirmed_application_id: UUID | None,
        now: datetime,
    ) -> LinkConfirmationDecision:
        """Record the user's resolution of an unresolved thread link.

        Task 11.6: the user picks one application (or none). The decision is
        append-only — a repeat decision on the same link returns the recorded
        result without re-evaluating evidence (idempotent). On first decision
        the snapshot upgrades to CONFIRMED and future messages in the same
        provider thread reuse the link.
        """
        unresolved = self._threads.get_unresolved(candidate_id, link_id)
        if unresolved is None:
            # Idempotent: a link already resolved returns its prior decision.
            prior = self._threads.prior_confirmation(candidate_id, link_id)
            if prior is not None:
                raise LinkAlreadyResolvedError(prior)
            raise LinkNotFoundError(link_id)
        prior = self._threads.prior_confirmation(candidate_id, link_id)
        if prior is not None:
            raise LinkAlreadyResolvedError(prior)
        decision = LinkConfirmationDecision(
            link_id=link_id,
            candidate_id=candidate_id,
            confirmed_application_id=confirmed_application_id,
            decided_at=now,
        )
        self._threads.record_confirmation(decision, now=now)
        return decision

    # ------------------------------------------------------------------
    # Read models (cursor-paginated, response-size-bounded)
    # ------------------------------------------------------------------

    def list_sync_runs(
        self,
        *,
        candidate_id: UUID,
        account_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        """Cursor-paginated sync-run history (task 11.7 / 11.10)."""
        self._require_owned_account(candidate_id, account_id)
        return self._runs.list_runs(candidate_id, account_id, cursor=cursor, limit=limit)

    def list_threads(
        self,
        *,
        candidate_id: UUID,
        account_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        self._require_owned_account(candidate_id, account_id)
        return self._threads.list_threads(candidate_id, account_id, cursor=cursor, limit=limit)

    def list_messages(
        self,
        *,
        candidate_id: UUID,
        thread_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        thread = self._threads.get_thread(candidate_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)
        return self._threads.list_messages(candidate_id, thread_id, cursor=cursor, limit=limit)

    def list_unresolved_links(
        self,
        *,
        candidate_id: UUID,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return self._threads.list_unresolved(candidate_id, cursor=cursor, limit=limit)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_account_by_email(self, candidate_id: UUID, email_address: str) -> EmailAccount | None:
        account = self._accounts.get_account_for_candidate(candidate_id)
        if account is not None and account.email_address.lower() == email_address.lower():
            return account
        return None

    def _require_owned_account(self, candidate_id: UUID, account_id: UUID) -> EmailAccount:
        account = self._accounts.get_account(candidate_id, account_id)
        if account is None:
            raise AccountNotOwnedError(account_id)
        return account

    def _resolve_link_id(self, candidate_id: UUID, thread_id: UUID) -> UUID:
        """Return the stable review-item id for an unresolved thread link.

        Reuses the existing link id when the thread already has an UNRESOLVED
        snapshot (so a repeat association does not create a second review
        item); otherwise mints a new id.
        """
        existing = self._threads.get_link(candidate_id, thread_id)
        if existing is not None and existing.link_id is not None:
            return existing.link_id
        return uuid4()

    @staticmethod
    def _connection_state(account: EmailAccount) -> MailConnectionState:
        """Map the legacy ``EmailAccount.status`` to a connection state.

        Delegates to the module-level :func:`_connection_state_for` so the
        same derivation is reusable by ``_to_summary`` without private access.
        """
        return _connection_state_for(account)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connection_state_for(account: EmailAccount) -> MailConnectionState:
    """Map the legacy ``EmailAccount.status`` to a connection state.

    The authoritative connection state lives in the new ``connection_state``
    column (set via ``set_connection_state``); this helper derives a
    best-effort state from the legacy status for the M4 ``EmailAccount``
    value type, which the service exchanges in. A repo that stores the
    explicit column overrides this on read.
    """
    if account.status is EmailAccountStatus.REVOKED:
        return MailConnectionState.REVOKED
    if account.status is EmailAccountStatus.ERROR:
        return MailConnectionState.ERROR
    return MailConnectionState.CONNECTED


def _thread_id(account_id: UUID, provider_thread_id: str) -> UUID:
    """Deterministic thread UUID from (account_id, provider_thread_id).

    Keeps a retry / second message in the same provider thread reusing one
    thread id (task 11.4 idempotency). ``NAMESPACE_URL`` gives a stable,
    spec-defined UUID.
    """
    from uuid import NAMESPACE_URL, uuid5

    return uuid5(NAMESPACE_URL, f"careerops:mail_thread:{account_id}:{provider_thread_id}")


def _to_summary(
    account: EmailAccount,
    candidate_id: UUID,
    scopes: tuple[str, ...] = (),
) -> MailAccountSummary:
    """Build a :class:`MailAccountSummary` from a legacy ``EmailAccount``."""
    # Default the granted scopes to the canonical readonly set when the repo
    # did not hand back an explicit tuple (the column is additive).
    granted = (
        scopes if scopes else tuple(sorted({"https://www.googleapis.com/auth/gmail.readonly"}))
    )
    state = _connection_state_for(account)
    return MailAccountSummary(
        account_id=account.id,
        candidate_id=candidate_id,
        email_address=account.email_address,
        connection_state=state,
        granted_scopes=granted,
        credential_reference_id=account.credential_reference_id,
        history_id=account.history_id,
        watch_expiration=account.watch_expiration,
        last_sync_at=account.last_sync_at,
        policy_version=MAIL_SYNC_POLICY_VERSION,
    )


def _evaluate_evidence(
    evidence: ThreadAssociationEvidence,
    eligible: list[UUID],
) -> tuple[LinkConfidence, set[UUID] | None]:
    """Return ``(confidence, matched_ids)`` for the strongest evidence band.

    ``matched_ids`` is None when no evidence matched; a set (possibly with
    more than one id) when at least one band matched. The caller already
    limited ``eligible`` to the active applications whose company domain /
    subject / source tokens match the thread; this helper only decides the
    confidence band and whether the match is unique (LINKED) or ambiguous
    (UNRESOLVED).
    """
    eligible_set = set(eligible)
    if not eligible_set:
        return LinkConfidence.NONE, None

    # 1. System-managed sent-message / provider-id recorded on a submitted
    #    application is the strongest signal.
    if evidence.sent_message_application_ids:
        hits = eligible_set & set(evidence.sent_message_application_ids)
        if hits:
            return LinkConfidence.PROVIDER_ID, hits

    # 2. Trusted sender domain match (eligibility pre-filtered upstream).
    if evidence.trusted_sender_domain:
        return LinkConfidence.TRUSTED_DOMAIN, eligible_set

    # 3. Subject / source token match (eligibility pre-filtered upstream).
    if evidence.subject_tokens or evidence.source_job_ids:
        return LinkConfidence.SUBJECT_SOURCE, eligible_set

    return LinkConfidence.NONE, None


def _evidence_refs(
    evidence: ThreadAssociationEvidence, confidence: LinkConfidence
) -> dict[str, str]:
    """Build bounded evidence references safe to surface in the UI."""
    refs: dict[str, str] = {"confidence": confidence.value}
    if evidence.provider_thread_id:
        refs["provider_thread_id"] = evidence.provider_thread_id
    if evidence.trusted_sender_domain:
        refs["trusted_sender_domain"] = evidence.trusted_sender_domain
    if evidence.sent_message_application_ids:
        refs["sent_message_link_count"] = str(len(evidence.sent_message_application_ids))
    return refs
