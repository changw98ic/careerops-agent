"""Domain contracts for Gmail read synchronization and thread association.

Section 11 of the end-to-end-career-application-loop change (tasks 11.1-11.6).
This module ships ONLY the value types the service, repository and API
exchange. It reuses the existing M4 mail domain (:mod:`careerops.domain.email`)
for ``EmailAccount`` / ``EmailThread`` / ``EmailMessage`` / scope validation
and the existing pure sync helpers (:mod:`careerops.application.email_sync`)
for classification + privacy-filtered ingestion.

What this module adds (additive — Iron Rule 8):

- **Connection state (task 11.1)** — an explicit dedicated-account connection
  lifecycle (``MailConnectionState``) separate from the legacy
  ``EmailAccountStatus`` active/revoked/error tri-state. The connection state
  also records the granted read scope set and a protected credential
  reference id; ``validate_read_scope`` rejects any non-readonly scope BEFORE
  a credential is stored.

- **Sync cursor + sync run (task 11.3)** — incremental history/watch cursor
  storage (``MailSyncCursor``) and durable sync-run status
  (``MailSyncRun`` / ``MailSyncRunStatus``) so a worker crash resumes from
  durable provider state without losing the cursor or duplicating records.

- **Thread association (task 11.6)** — evidence-backed linking of an inbound
  recruiting thread to an application (``ThreadLinkStatus`` /
  ``ThreadAssociationEvidence`` / ``UnresolvedThreadLink``). Provider thread
  ids, system-managed sent-message linkage, trusted sender domains and
  subject/source evidence are evaluated in priority order; when more than one
  application is plausible an *unresolved* review item is created rather than
  silently choosing one.

Iron rules honored:
- Model review-only (Iron Rule 2): no type here lets model output pick a
  recipient, transition state, or write a trusted link.
- Append-only / reversible (Iron Rule 4): sync runs and link decisions are
  append-only; a confirmed link is recorded, never silently rewritten.
- Default-deny (Iron Rule 7): connection requires an explicitly granted
  readonly scope; the GMAIL_READ capability stays DENIED at the contract layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from careerops.domain.email import (
    ALLOWED_GMAIL_SCOPES,
    FORBIDDEN_SCOPES,
)

__all__ = [
    "MAIL_SYNC_POLICY_VERSION",
    "LinkConfidence",
    "LinkConfirmationDecision",
    "MailAccountSummary",
    "MailConnectionState",
    "MailSyncCursor",
    "MailSyncDedupKey",
    "MailSyncRun",
    "MailSyncRunStatus",
    "ReadScopeViolationError",
    "SyncDirection",
    "ThreadAssociationEvidence",
    "ThreadLinkSnapshot",
    "ThreadLinkStatus",
    "UnresolvedThreadLink",
    "validate_read_scope",
]


# Bumped whenever the connection / scope revalidation ruleset changes. Recorded
# on the connection summary so a stale connection re-validates its granted
# scope against the current rules at read time (task 11.1).
MAIL_SYNC_POLICY_VERSION = "section11.mail_sync.v1"


# ---------------------------------------------------------------------------
# Connection state (task 11.1)
# ---------------------------------------------------------------------------


class MailConnectionState(StrEnum):
    """Lifecycle of a dedicated recruiting-account connection.

    ``DISCONNECTED`` — no account connected for this candidate (default).
    ``CONNECTED`` — an account is connected with a validated readonly scope;
    synchronization is permitted only when the GMAIL_READ capability is also
    released (the capability is the contract-layer gate, default-deny).
    ``REVOKED`` — the user or provider revoked the account; future sync and
    provider use are stopped and pending provider actions invalidated
    (task 11.2). Prior audit/history is retained per policy.
    ``ERROR`` — a transient/recoverable sync error (e.g. watch expired); the
    account stays connected but sync is paused until the error clears.
    """

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    REVOKED = "revoked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MailAccountSummary:
    """Read model for the dedicated recruiting-account connection.

    ``granted_scopes`` is the validated scope set stored at connection time;
    it MUST always be a subset of ``ALLOWED_GMAIL_SCOPES``. The
    ``credential_reference_id`` points at an envelope-encrypted token envelope
    — no plaintext token ever lives in this type or in the API response.
    ``sync_available`` is false whenever the connection is not CONNECTED or
    the GMAIL_READ capability is denied (evaluated by the service, which has
    the resolver); the summary itself only carries the connection facts.
    """

    account_id: UUID
    candidate_id: UUID
    email_address: str
    connection_state: MailConnectionState
    granted_scopes: tuple[str, ...] = ()
    credential_reference_id: UUID | None = None
    history_id: str = ""
    watch_expiration: datetime | None = None
    last_sync_at: datetime | None = None
    last_error_code: str = ""
    policy_version: str = MAIL_SYNC_POLICY_VERSION

    @property
    def sync_available(self) -> bool:
        """True only when the connection is healthy enough to synchronize."""
        return self.connection_state is MailConnectionState.CONNECTED


class ReadScopeViolationError(Exception):
    """Raised when a granted scope set violates the readonly read-scope policy."""


def validate_read_scope(scopes: tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
    """Validate a granted OAuth scope set is exactly the approved readonly scope.

    Task 11.1: the system stores the credential ONLY when every granted scope
    is within ``ALLOWED_GMAIL_SCOPES`` and none are in ``FORBIDDEN_SCOPES``.
    Returns the canonical ordered tuple of granted scopes on success; raises
    :class:`ReadScopeViolationError` otherwise. This is a fail-closed check —
    a forbidden scope (send/compose/modify/full-mailbox) rejects the whole
    connection and the credential is never stored.
    """
    scope_set = frozenset(scopes)
    forbidden_found = scope_set & FORBIDDEN_SCOPES
    if forbidden_found:
        raise ReadScopeViolationError(
            "forbidden scope granted: " + ",".join(sorted(forbidden_found))
        )
    extra = scope_set - ALLOWED_GMAIL_SCOPES
    if extra:
        raise ReadScopeViolationError(
            "unapproved scope granted: " + ",".join(sorted(extra))
        )
    if not scope_set:
        raise ReadScopeViolationError("no granted scope")
    return tuple(sorted(scope_set))


# ---------------------------------------------------------------------------
# Sync cursor + sync run (task 11.3)
# ---------------------------------------------------------------------------


class SyncDirection(StrEnum):
    """Direction of an incremental sync step (used for audit/observability)."""

    FULL = "full"
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


class MailSyncRunStatus(StrEnum):
    """Durable status of one synchronization run (task 11.3).

    ``PENDING`` — the run was requested but not yet started by a worker.
    ``RUNNING`` — a worker claimed the run and is processing messages.
    ``COMPLETED`` — the run finished cleanly; the cursor advanced.
    ``PARTIAL`` — the run stopped after processing some messages (worker
    crash / lease lost); a retry resumes from durable provider state and
    preserves idempotent results (task 11.9 partial-sync-restart).
    ``FAILED`` — a terminal failure (revoked account, unrecoverable provider
    error). The cursor is NOT advanced.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            MailSyncRunStatus.COMPLETED,
            MailSyncRunStatus.FAILED,
        )


@dataclass(frozen=True, slots=True)
class MailSyncCursor:
    """Incremental history/watch cursor for one account (task 11.3).

    ``history_id`` is the provider history id; ``watch_expiration`` is the
    Pub/Sub watch expiration (None when polling). The cursor is durable: a
    worker crash leaves the cursor at the last committed point so a retry
    resumes without losing progress or duplicating records.
    """

    account_id: UUID
    history_id: str
    watch_expiration: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MailSyncRun:
    """One durable synchronization run (task 11.3).

    ``messages_processed`` counts newly-ingested messages; ``messages_skipped``
    counts duplicate deliveries (task 11.4). ``history_id_start`` /
    ``history_id_end`` bracket the cursor advance. ``error_code`` is a bounded
    machine code (empty on success). ``direction`` records whether this was a
    full, incremental or backfill step.
    """

    id: UUID
    account_id: UUID
    candidate_id: UUID
    status: MailSyncRunStatus
    direction: SyncDirection = SyncDirection.INCREMENTAL
    started_at: datetime | None = None
    completed_at: datetime | None = None
    messages_processed: int = 0
    messages_skipped: int = 0
    history_id_start: str = ""
    history_id_end: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        if self.messages_processed < 0 or self.messages_skipped < 0:
            raise ValueError("message counts must be non-negative")


@dataclass(frozen=True, slots=True)
class MailSyncDedupKey:
    """Idempotency key for a sync run (task 11.3 durable status).

    A repeat sync-now request for the same account while a run is already
    PENDING/RUNNING returns the existing run (idempotent) rather than
    starting a second one.
    """

    account_id: UUID
    requested_at: datetime


# ---------------------------------------------------------------------------
# Thread association (task 11.6)
# ---------------------------------------------------------------------------


class ThreadLinkStatus(StrEnum):
    """Association state of a recruiting thread to an application.

    ``UNLINKED`` — no evidence yet; the thread is not associated.
    ``LINKED`` — a single high-confidence application match was found via
    provider ids / sent-message linkage / trusted domains; still reviewable
    but usable for timeline projection.
    ``UNRESOLVED`` — sender/subject/content could match multiple active
    applications; the system creates a review item and asks the user to
    choose rather than silently picking one (task 11.6).
    ``CONFIRMED`` — the user explicitly confirmed the link (or an UNRESOLVED
    item was resolved); future messages in the same provider thread reuse it.
    """

    UNLINKED = "unlinked"
    LINKED = "linked"
    UNRESOLVED = "unresolved"
    CONFIRMED = "confirmed"


class LinkConfidence(StrEnum):
    """Confidence band for an automatic thread→application link.

    Deterministic, evidence-backed bands. ``PROVIDER_ID`` (a provider
    thread/message id recorded on a submitted application) is the strongest;
    ``SENT_MESSAGE`` (a system-managed sent-message receipt in the same
    thread) is next; ``TRUSTED_DOMAIN`` + ``SUBJECT_SOURCE`` are weaker and
    only produce LINKED (never CONFIRMED) until the user reviews.
    """

    PROVIDER_ID = "provider_id"
    SENT_MESSAGE = "sent_message"
    TRUSTED_DOMAIN = "trusted_domain"
    SUBJECT_SOURCE = "subject_source"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ThreadAssociationEvidence:
    """Evidence used to (attempt to) link a thread to an application.

    ``provider_thread_id`` / ``provider_message_ids`` are the stable provider
    identities. ``sent_message_application_ids`` are applications whose
    system-managed send recorded a message/thread id matching this thread
    (strongest signal after a direct provider-id hit). ``trusted_sender_domain``
    is the verified sender domain. ``subject_tokens`` / ``source_job_ids`` are
    subject/source-derived candidate signals. All fields are trusted business
    state resolved by the service; none come from model output.
    """

    provider_thread_id: str
    provider_message_ids: tuple[str, ...] = ()
    sent_message_application_ids: tuple[UUID, ...] = ()
    trusted_sender_domain: str = ""
    subject_tokens: tuple[str, ...] = ()
    source_job_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadLinkSnapshot:
    """The current association of one thread (read model).

    ``application_id`` is set when status is LINKED/CONFIRMED; it is None for
    UNLINKED and UNRESOLVED. ``candidate_application_ids`` carries the
    plausible applications for an UNRESOLVED thread so the UI can present the
    choice. ``evidence_refs`` are bounded references safe to surface.
    """

    thread_id: UUID
    account_id: UUID
    candidate_id: UUID
    status: ThreadLinkStatus
    application_id: UUID | None = None
    candidate_application_ids: tuple[UUID, ...] = ()
    confidence: LinkConfidence = LinkConfidence.NONE
    evidence_refs: dict[str, str] = field(default_factory=lambda: {})
    provider_thread_id: str = ""
    subject: str = ""
    resolved_at: datetime | None = None
    # The review-item id for an UNRESOLVED link — the value the user references
    # to confirm a resolution. Stable across re-evaluation so a repeat
    # association for the same thread reuses the same review item.
    link_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class UnresolvedThreadLink:
    """A thread whose application link needs user resolution (task 11.6).

    The user picks one of ``candidate_application_ids`` (or none). The
    decision is recorded as a :class:`LinkConfirmationDecision`.
    """

    link_id: UUID
    thread_id: UUID
    candidate_id: UUID
    provider_thread_id: str
    subject: str = ""
    candidate_application_ids: tuple[UUID, ...] = ()
    evidence_refs: dict[str, str] = field(default_factory=lambda: {})
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LinkConfirmationDecision:
    """A user's resolution of an unresolved thread link (task 11.6).

    ``confirmed_application_id`` is the user's pick; ``None`` means the user
    explicitly left the thread unlinked (a valid resolution). Repeat
    decisions on the same link id return the recorded result without
    re-evaluating evidence.
    """

    link_id: UUID
    candidate_id: UUID
    confirmed_application_id: UUID | None
    decided_at: datetime
