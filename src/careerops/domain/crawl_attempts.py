"""Canonical domain types for per-source crawl attempts and permissions.

real-autonomous-career-loop Phase 5 (tasks 5.1-5.3).

Two durable state machines back the durable source-discovery / attempt-outcome
contract (design: ``llm-job-extraction`` spec):

- :class:`CrawlAttemptOutcome` — the seven Tier 1 attempt outcomes every source
  attempt SHALL finish with exactly one of. This replaces the fragmented,
  non-canonical outcome signals that today live only inside
  ``crawl_runs.error_category`` (a free-form Text column stuffed with a mix of
  policy decisions, backoff reasons, and hand-rolled strings) and inside
  ``job_sources.last_run_metadata`` (which carries no run outcome at all).
  Task 5.3 forbids encoding this state machine only inside ``last_run_metadata``;
  the canonical value set lives here and is enforced by a CHECK column on the
  new ``crawl_source_attempts`` table.

- :class:`CrawlPermissionState` — the per-source crawl-permission lifecycle
  (pending / granted / denied / revoked / expired). This is distinct from the
  declared policy inputs (``CrawlPolicyStatus`` trust / terms / robots on
  ``job_sources``) which the crawl_policy layer consumes; it is the
  user-grantable consent request lifecycle. Hard constraint from the spec
  (tasks 5.2, 6.6, 10.5): the persisted permission stores NO password and NO
  raw session material — only opaque references + expiry checks arrive in
  Phase 6/7. The :class:`CrawlSourcePermission` dataclass therefore carries
  decision timestamps and disclosed limits but no credential fields.

The transition tables mirror the :data:`careerops.domain.crawl.ALLOWED_TRANSITIONS`
pattern: terminal states (``POLICY_DENIED`` outcome; ``DENIED`` / ``REVOKED`` /
``EXPIRED`` permission states) admit no further moves, so a finished/denied
record is never silently resurrected. Re-requesting a revoked/expired permission
is a NEW row created by the Phase-6 permission service (task 6.2), not a
transition on the terminal row — exactly as a finished crawl run cannot move
back to ``PENDING``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from careerops.domain.crawl_plans import CrawlExecutorMode

__all__ = [
    "ALLOWED_PERMISSION_TRANSITIONS",
    "CRAWL_ATTEMPT_OUTCOME_VALUES",
    "CrawlAttemptOutcome",
    "CrawlAttemptRepository",
    "CrawlPermissionRepository",
    "CrawlPermissionState",
    "CrawlPermissionStateValues",
    "CrawlSourceAttempt",
    "CrawlSourcePermission",
    "PERMISSION_TERMINAL_STATES",
    "SUCCESSFUL_CHAIN_OUTCOMES",
    "TERMINAL_STOP_OUTCOMES",
    "TIER2_ELIGIBLE_OUTCOMES",
    "is_permission_transition_allowed",
]


# ---------------------------------------------------------------------------
# Tier 1 attempt outcome
# ---------------------------------------------------------------------------


class CrawlAttemptOutcome(StrEnum):
    """The canonical outcome of a single Tier 1 source attempt.

    Exactly one of these SHALL be recorded for every scheduled source
    (``llm-job-extraction`` spec: "Every Tier 1 attempt records a reasoned
    outcome"). The classifier (Phase 5.6) maps existing fragments
    (``CrawlSourceResult`` postings/status, policy decision, backoff reason,
    captcha signal) onto this closure; the values are persisted as a CHECK
    column on ``crawl_source_attempts.outcome``.
    """

    POSTINGS_FOUND = "postings_found"
    VERIFIED_EMPTY = "verified_empty"
    NOT_JOB_SOURCE = "not_job_source"
    TRANSIENT_FAILURE = "transient_failure"
    AUTH_REQUIRED = "auth_required"
    DYNAMIC_OR_UNSUPPORTED = "dynamic_or_unsupported"
    POLICY_DENIED = "policy_denied"


# The frozen value set, surfaced so the migration CHECK clause and the
# repository mapper stay byte-for-byte in sync with the enum without a
# round-trip import cycle.
CRAWL_ATTEMPT_OUTCOME_VALUES: tuple[str, ...] = tuple(
    member.value for member in CrawlAttemptOutcome
)

# Outcomes that MAY make a confirmed job source eligible for Tier 2 escalation
# — and ONLY when positive job-source evidence is also present (spec:
# "Tier 2 eligibility requires positive job-source and failure evidence").
# The classifier (5.6) must not let empty/403/CAPTCHA/model-judgement collapse
# into AUTH_REQUIRED.
TIER2_ELIGIBLE_OUTCOMES: frozenset[CrawlAttemptOutcome] = frozenset(
    {CrawlAttemptOutcome.AUTH_REQUIRED, CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED}
)

# ``POLICY_DENIED`` is a terminal stop: neither Tier 1 nor Tier 2 continues
# until the underlying policy changes (spec scenario "Source policy forbids
# crawling"). Recorded separately so the retry/cooldown layer (5.7) can treat
# it as non-retryable.
TERMINAL_STOP_OUTCOMES: frozenset[CrawlAttemptOutcome] = frozenset(
    {CrawlAttemptOutcome.POLICY_DENIED}
)

# Outcomes that chain into matching and inbox projection (Phase 8.3).
# Only ``POSTINGS_FOUND`` triggers the matching chain.  All other outcomes
# (including ``VERIFIED_EMPTY``, ``TRANSIENT_FAILURE``, ``AUTH_REQUIRED``,
# ``DYNAMIC_OR_UNSUPPORTED``, ``NOT_JOB_SOURCE``, ``POLICY_DENIED``) are
# filtered out and never reach the inbox.
SUCCESSFUL_CHAIN_OUTCOMES: frozenset[CrawlAttemptOutcome] = frozenset(
    {CrawlAttemptOutcome.POSTINGS_FOUND}
)


# ---------------------------------------------------------------------------
# Source-specific crawl permission state
# ---------------------------------------------------------------------------


class CrawlPermissionStateValues(StrEnum):
    """String values for ``crawl_source_permissions.state``.

    Exposed as a separate enum (rather than reusing ``CrawlPermissionState``)
    so the migration CHECK clause and the row mapper can reference the raw
    value closure without depending on the dataclass-bearing domain enum.
    Mirrors how ``CRAWL_ATTEMPT_OUTCOME_VALUES`` is derived.
    """

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CrawlPermissionState(StrEnum):
    """Per-source crawl-permission lifecycle.

    ``GRANTED`` is the ONLY state that authorizes an authenticated Tier 2
    session (spec: "Permission is absent or withdrawn -> Tier 2 does not use an
    authenticated session and the source remains paused"). Terminal states
    (``DENIED`` / ``REVOKED`` / ``EXPIRED``) admit no further moves; a fresh
    request is a new row created by the permission service (task 6.2), not a
    transition on the terminal row.
    """

    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


# Legal permission-state transitions: from_state -> allowed to_states. Mirrors
# :data:`careerops.domain.crawl.ALLOWED_TRANSITIONS`: terminal states admit no
# further moves. A grant ends only by revocation or expiry; a pending request
# resolves to granted/denied or lapses to expired.
ALLOWED_PERMISSION_TRANSITIONS: dict[CrawlPermissionState, frozenset[CrawlPermissionState]] = {
    CrawlPermissionState.PENDING: frozenset(
        {CrawlPermissionState.GRANTED, CrawlPermissionState.DENIED, CrawlPermissionState.EXPIRED}
    ),
    CrawlPermissionState.GRANTED: frozenset(
        {CrawlPermissionState.REVOKED, CrawlPermissionState.EXPIRED}
    ),
    CrawlPermissionState.DENIED: frozenset(),
    CrawlPermissionState.REVOKED: frozenset(),
    CrawlPermissionState.EXPIRED: frozenset(),
}

# Terminal permission states — no authenticated session may be attached while a
# source's latest permission is in any of these (spec scenario 10.5).
PERMISSION_TERMINAL_STATES: frozenset[CrawlPermissionState] = frozenset(
    {CrawlPermissionState.DENIED, CrawlPermissionState.REVOKED, CrawlPermissionState.EXPIRED}
)


def is_permission_transition_allowed(
    current: CrawlPermissionState, target: CrawlPermissionState
) -> bool:
    """Return True if ``current -> target`` is a legal permission transition.

    The repository :meth:`CrawlPermissionRepository.transition` calls this
    before persisting; an illegal move raises so a denied/revoked/expired
    permission is never silently resurrected.
    """
    return target in ALLOWED_PERMISSION_TRANSITIONS.get(current, frozenset())


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlSourceAttempt:
    """One durable per-source attempt outcome row (``crawl_source_attempts``).

    ``owner_id`` is the server-resolved candidate (the table has no owner
    column in the single-user runtime; ownership is transitive via
    ``job_sources.company_id``). ``crawl_run_id`` is nullable because a Tier 1
    probe may classify a source without binding to a full crawl run.
    ``evidence_summary`` is a BOUNDED safe summary (status code, signal flags,
    counts, next-eligible hint) — never raw page content, credentials, or PII
    (same rule as ``job_sources.last_run_metadata``).
    """

    id: UUID
    source_id: UUID
    owner_id: UUID
    attempt_no: int
    outcome: CrawlAttemptOutcome
    crawl_run_id: UUID | None = None
    executor_mode: CrawlExecutorMode = CrawlExecutorMode.HTTP
    action_count: int = 0
    evidence_summary: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_eligible_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CrawlSourcePermission:
    """A source-specific crawl-permission request (``crawl_source_permissions``).

    Stores the consent lifecycle ONLY: scope, disclosed purpose/frequency/limits,
    and decision timestamps. It deliberately holds NO password and NO raw session
    material (tasks 5.2, 6.6, 10.5) — opaque session references arrive in Phase
    6/7 on a separate surface. ``expires_at`` is the configured deadline for a
    grant; ``expired_at`` is the timestamp set when the row actually lapsed.
    """

    id: UUID
    source_id: UUID
    owner_id: UUID
    state: CrawlPermissionState = CrawlPermissionState.PENDING
    domain_scope: str = ""
    disclosed_terms: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    requested_at: datetime | None = None
    granted_at: datetime | None = None
    denied_at: datetime | None = None
    revoked_at: datetime | None = None
    expired_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Repository Protocols
#
# Server-side candidate ownership (Iron Rule 2): every method takes the
# server-resolved ``owner_id``. Concrete implementations live in
# :mod:`careerops.infrastructure.database.postgres_crawl_repo` (Postgres) and
# the in-memory mirror in :mod:`careerops.infrastructure.memory_repos`.
# ---------------------------------------------------------------------------


@runtime_checkable
class CrawlAttemptRepository(Protocol):
    """Append-only per-source attempt-outcome store."""

    def record(self, owner_id: UUID, attempt: CrawlSourceAttempt) -> CrawlSourceAttempt:
        """Persist a new attempt outcome row.

        ``attempt_no`` is the monotonic per-source sequence; the caller SHOULD
        obtain it via :meth:`next_attempt_no` so retries do not collide on the
        ``uq_attempts_source_attempt_no`` unique key. Returns the persisted row.
        """
        ...

    def get_by_id(self, owner_id: UUID, attempt_id: UUID) -> CrawlSourceAttempt:
        """Return the attempt, scoped transitively via its source's owner.

        Raises :class:`NotFoundError` if the attempt does not exist or its
        source belongs to a different owner.
        """
        ...

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourceAttempt]:
        """Return attempts for a source, newest first."""
        ...

    def latest_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourceAttempt | None:
        """Return the most recent attempt for a source, or None."""
        ...

    def next_attempt_no(self, owner_id: UUID, source_id: UUID) -> int:
        """Return the next monotonic attempt_no for a source (>=1)."""
        ...


@runtime_checkable
class CrawlPermissionRepository(Protocol):
    """Per-source crawl-permission request store with a state machine."""

    def save(
        self, owner_id: UUID, permission: CrawlSourcePermission
    ) -> CrawlSourcePermission:
        """Insert or update (idempotent on ``id``). Returns the persisted row."""
        ...

    def get_by_id(self, owner_id: UUID, permission_id: UUID) -> CrawlSourcePermission:
        """Return the permission, scoped transitively via its source's owner.

        Raises :class:`NotFoundError` if it does not exist or belongs to a
        different owner.
        """
        ...

    def get_unresolved_for_source(
        self, owner_id: UUID, source_id: UUID
    ) -> CrawlSourcePermission | None:
        """Return the pending permission request for a source, or None.

        Used by the Phase-6 permission service to reuse an existing unresolved
        request instead of creating repeated prompts (spec scenario "Repeated
        crawl sees an existing pending request").
        """
        ...

    def list_for_source(
        self, owner_id: UUID, source_id: UUID, *, limit: int = 50
    ) -> list[CrawlSourcePermission]:
        """Return permission requests for a source, newest first."""
        ...

    def transition(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        to: CrawlPermissionState,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Move a permission to ``to``, validating against
        :data:`ALLOWED_PERMISSION_TRANSITIONS` and stamping the matching
        decision timestamp (``granted_at`` / ``denied_at`` / ``revoked_at`` /
        ``expired_at``). Raises :class:`NotFoundError` if the row is missing
        and :class:`ValueError` if the transition is illegal.
        """
        ...
