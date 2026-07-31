"""Source-specific crawl-permission service (real-autonomous-career-loop Phase 6.2).

Manages the crawl-permission lifecycle for a source:

1. ``request_permission`` -- called when a Tier 1 attempt yields
   ``AUTH_REQUIRED``: pauses the source (state -> PAUSED, enabled=False),
   creates at most one pending request per source (ON CONFLICT DO NOTHING),
   and returns the existing unresolved request on repeats.
2. ``grant`` / ``deny`` / ``revoke`` / ``expire`` -- enforce the legal
   state-machine transitions defined in
   :data:`careerops.domain.crawl_attempts.ALLOWED_PERMISSION_TRANSITIONS`.
3. ``list_for_source`` -- returns all permission requests for a source.

Every mutation is audit-logged via the injected ``PermissionAuditSink``.

Design decisions:
- The service pauses the source on request (task 6.2) so no further Tier 1
  runs hit the login-walled page until the user acts.
- The ``CrawlPermissionRepository`` enforces "at most one PENDING per source"
  at the DB level (partial unique index); the service checks
  ``get_unresolved_for_source`` first to return the existing request instead
  of attempting a duplicate insert.
- Grant can set an ``expires_at`` deadline; the caller is responsible for
  calling ``expire`` when the deadline lapses (Phase 7 or a sweep).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from careerops.domain.crawl_attempts import (
    CrawlPermissionRepository,
    CrawlPermissionState,
    CrawlSourcePermission,
    is_permission_transition_allowed,
)
from careerops.domain.crawl_plans import CrawlSourceRepository, CrawlSourceState

__all__ = [
    "CrawlPermissionService",
    "PermissionAuditEvent",
    "PermissionAuditSink",
    "PermissionDecision",
]

_log = logging.getLogger(__name__)

# Default grant validity period.  Phase 7 / a sweep calls ``expire`` when
# this lapses.
_DEFAULT_GRANT_TTL = timedelta(days=90)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class PermissionDecision(StrEnum):
    """The decision the user (or system) made on a permission request."""

    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class PermissionAuditEvent:
    """One append-only audit entry for a permission decision."""

    permission_id: UUID
    source_id: UUID
    owner_id: UUID
    decision: PermissionDecision
    prior_state: CrawlPermissionState
    new_state: CrawlPermissionState
    reason: str = ""
    occurred_at: datetime | None = None


@runtime_checkable
class PermissionAuditSink(Protocol):
    """Append-only audit sink for permission decisions."""

    def record(self, event: PermissionAuditEvent) -> None: ...


@runtime_checkable
class PermissionAttentionSink(Protocol):
    """Persist and resolve user attention created by a permission request."""

    def pending(
        self,
        permission: CrawlSourcePermission,
        *,
        source_name: str,
    ) -> None: ...

    def resolved(
        self,
        permission: CrawlSourcePermission,
        *,
        decision: PermissionDecision,
    ) -> None: ...


class ListPermissionAuditSink:
    """In-memory audit sink for test assertions."""

    def __init__(self) -> None:
        self.events: list[PermissionAuditEvent] = []

    def record(self, event: PermissionAuditEvent) -> None:
        self.events.append(event)


class LoggingPermissionAuditSink:
    """Structured-log audit sink -- append-only by nature of immutable logs."""

    def record(self, event: PermissionAuditEvent) -> None:
        _log.info(
            "permission decision: permission=%s source=%s decision=%s %s->%s reason=%s",
            event.permission_id,
            event.source_id,
            event.decision.value,
            event.prior_state.value,
            event.new_state.value,
            event.reason or "(none)",
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CrawlPermissionService:
    """Source-specific crawl-permission lifecycle manager.

    Composes the ``CrawlPermissionRepository`` (permission CRUD + state
    machine), the ``CrawlSourceRepository`` (to pause the source on
    request), and a ``PermissionAuditSink`` (append-only audit).
    """

    def __init__(
        self,
        permission_repository: CrawlPermissionRepository,
        source_repository: CrawlSourceRepository,
        *,
        audit_sink: PermissionAuditSink | None = None,
        attention_sink: PermissionAttentionSink | None = None,
        grant_ttl: timedelta = _DEFAULT_GRANT_TTL,
    ) -> None:
        self._permissions = permission_repository
        self._sources = source_repository
        self._audit = audit_sink or LoggingPermissionAuditSink()
        self._attention = attention_sink
        self._grant_ttl = grant_ttl

    # -- Request ---------------------------------------------------------------

    def request_permission(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        login_evidence: str = "",
        domain_scope: str = "",
        disclosed_terms: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Pause the source and create (or reuse) a pending permission request.

        If the source already has an unresolved PENDING request, the existing
        one is returned (idempotent -- no duplicate prompt).

        Steps:
        1. Verify the source exists and belongs to the owner.
        2. Check for an existing unresolved (PENDING) request.
        3. If none, pause the source (state -> PAUSED, enabled=False) and
           create a new PENDING permission row.
        4. Audit-log the request.
        """
        ts = now or datetime.now(tz=UTC)

        # 1. Verify source exists.
        source = self._sources.get_by_id(owner_id, source_id)

        # 2. Check for existing unresolved request.
        existing = self._permissions.get_unresolved_for_source(owner_id, source_id)
        if existing is not None:
            return existing  # Reuse -- no duplicate prompt.

        # 3. Pause the source and create the request.
        if source.state is not CrawlSourceState.PAUSED or source.enabled:
            self._sources.update_state(
                owner_id,
                source_id,
                state=CrawlSourceState.PAUSED,
                enabled=False,
                now=ts,
            )

        terms: dict[str, object] = dict(disclosed_terms or {})
        if login_evidence:
            terms.setdefault("login_evidence", login_evidence)
        terms.setdefault("purpose", "read-only job listing extraction")
        terms.setdefault("frequency", "periodic (configurable per source)")
        terms.setdefault("max_browser_actions", 30)
        terms.setdefault("max_duration_seconds", 300)
        terms.setdefault("max_consecutive_empty_pages", 3)

        permission = CrawlSourcePermission(
            id=uuid4(),
            source_id=source_id,
            owner_id=owner_id,
            state=CrawlPermissionState.PENDING,
            domain_scope=domain_scope,
            disclosed_terms=terms,
            requested_at=ts,
            created_at=ts,
            updated_at=ts,
        )
        saved = self._permissions.save(owner_id, permission)

        # 4. Audit.
        self._audit.record(
            PermissionAuditEvent(
                permission_id=saved.id,
                source_id=source_id,
                owner_id=owner_id,
                decision=PermissionDecision.REQUESTED,
                prior_state=CrawlPermissionState.PENDING,
                new_state=CrawlPermissionState.PENDING,
                reason=login_evidence,
                occurred_at=ts,
            )
        )
        if self._attention is not None:
            self._attention.pending(saved, source_name=source.source_identifier)
        return saved

    # -- Decisions -------------------------------------------------------------

    def grant(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        session_ref: str = "",
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Grant a pending permission request.

        Transitions PENDING -> GRANTED, sets ``expires_at`` to the configured
        grant TTL, and re-enables + un-pauses the source so Tier 2 can use
        the authenticated session (Phase 7).

        Args:
            session_ref: opaque reference to the authenticated session
                (Phase 6.6). Stored on the permission row so the
                SessionChecker can validate it later.
        """
        ts = now or datetime.now(tz=UTC)
        current = self._permissions.get_by_id(owner_id, permission_id)
        _assert_transition(current.state, CrawlPermissionState.GRANTED)
        expected_session_ref = f"careerops-login-{current.source_id}"
        if session_ref and session_ref != expected_session_ref:
            raise ValueError("session reference does not match the approved source")

        updated = self._permissions.transition(
            owner_id, permission_id, to=CrawlPermissionState.GRANTED, now=ts
        )
        # Set expires_at on the granted permission.
        expires_at = ts + self._grant_ttl
        updated = CrawlSourcePermission(
            id=updated.id,
            source_id=updated.source_id,
            owner_id=updated.owner_id,
            state=updated.state,
            domain_scope=updated.domain_scope,
            disclosed_terms=updated.disclosed_terms,
            requested_at=updated.requested_at,
            granted_at=updated.granted_at,
            denied_at=updated.denied_at,
            revoked_at=updated.revoked_at,
            expired_at=updated.expired_at,
            expires_at=expires_at,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            session_ref=expected_session_ref,
        )
        updated = self._permissions.save(owner_id, updated)

        # Re-enable the source so Phase 7 can attach the authenticated session.
        try:
            self._sources.update_state(
                owner_id,
                updated.source_id,
                state=CrawlSourceState.ACTIVE,
                enabled=True,
                now=ts,
            )
        except Exception:
            _log.warning(
                "failed to re-enable source %s after grant; source stays paused",
                updated.source_id,
            )

        self._audit.record(
            PermissionAuditEvent(
                permission_id=permission_id,
                source_id=updated.source_id,
                owner_id=owner_id,
                decision=PermissionDecision.GRANTED,
                prior_state=CrawlPermissionState.PENDING,
                new_state=CrawlPermissionState.GRANTED,
                occurred_at=ts,
            )
        )
        if self._attention is not None:
            self._attention.resolved(updated, decision=PermissionDecision.GRANTED)
        return updated

    def deny(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Deny a pending permission request.

        Transitions PENDING -> DENIED (terminal).  The source stays paused
        until a new request is made and granted.
        """
        ts = now or datetime.now(tz=UTC)
        current = self._permissions.get_by_id(owner_id, permission_id)
        _assert_transition(current.state, CrawlPermissionState.DENIED)

        updated = self._permissions.transition(
            owner_id, permission_id, to=CrawlPermissionState.DENIED, now=ts
        )
        self._audit.record(
            PermissionAuditEvent(
                permission_id=permission_id,
                source_id=updated.source_id,
                owner_id=owner_id,
                decision=PermissionDecision.DENIED,
                prior_state=current.state,
                new_state=CrawlPermissionState.DENIED,
                reason=reason,
                occurred_at=ts,
            )
        )
        if self._attention is not None:
            self._attention.resolved(updated, decision=PermissionDecision.DENIED)
        return updated

    def revoke(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Revoke a granted permission.

        Transitions GRANTED -> REVOKED (terminal).  Pauses the source and
        disables it so no authenticated Tier 2 session can be attached.
        """
        ts = now or datetime.now(tz=UTC)
        current = self._permissions.get_by_id(owner_id, permission_id)
        _assert_transition(current.state, CrawlPermissionState.REVOKED)

        updated = self._permissions.transition(
            owner_id, permission_id, to=CrawlPermissionState.REVOKED, now=ts
        )
        # Pause the source -- permission withdrawn.
        try:
            self._sources.update_state(
                owner_id,
                updated.source_id,
                state=CrawlSourceState.PAUSED,
                enabled=False,
                now=ts,
            )
        except Exception:
            _log.warning(
                "failed to pause source %s after revoke; source stays as-is",
                updated.source_id,
            )

        self._audit.record(
            PermissionAuditEvent(
                permission_id=permission_id,
                source_id=updated.source_id,
                owner_id=owner_id,
                decision=PermissionDecision.REVOKED,
                prior_state=CrawlPermissionState.GRANTED,
                new_state=CrawlPermissionState.REVOKED,
                reason=reason,
                occurred_at=ts,
            )
        )
        if self._attention is not None:
            self._attention.resolved(updated, decision=PermissionDecision.REVOKED)
        return updated

    def expire(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        """Expire a granted permission whose ``expires_at`` has lapsed.

        Transitions GRANTED -> EXPIRED (terminal).  Pauses the source.
        Callers (Phase 7 sweep or cron) should call this when
        ``expires_at < now``.
        """
        ts = now or datetime.now(tz=UTC)
        current = self._permissions.get_by_id(owner_id, permission_id)
        _assert_transition(current.state, CrawlPermissionState.EXPIRED)

        updated = self._permissions.transition(
            owner_id, permission_id, to=CrawlPermissionState.EXPIRED, now=ts
        )
        # Pause the source -- permission lapsed.
        try:
            self._sources.update_state(
                owner_id,
                updated.source_id,
                state=CrawlSourceState.PAUSED,
                enabled=False,
                now=ts,
            )
        except Exception:
            _log.warning(
                "failed to pause source %s after expire; source stays as-is",
                updated.source_id,
            )

        self._audit.record(
            PermissionAuditEvent(
                permission_id=permission_id,
                source_id=updated.source_id,
                owner_id=owner_id,
                decision=PermissionDecision.EXPIRED,
                prior_state=CrawlPermissionState.GRANTED,
                new_state=CrawlPermissionState.EXPIRED,
                occurred_at=ts,
            )
        )
        if self._attention is not None:
            self._attention.resolved(updated, decision=PermissionDecision.EXPIRED)
        return updated

    # -- Queries ---------------------------------------------------------------

    def list_for_source(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        limit: int = 50,
    ) -> list[CrawlSourcePermission]:
        """Return all permission requests for a source, newest first."""
        return self._permissions.list_for_source(owner_id, source_id, limit=limit)

    def get_permission(
        self,
        owner_id: UUID,
        permission_id: UUID,
    ) -> CrawlSourcePermission:
        """Return a single permission by id (ownership-scoped)."""
        return self._permissions.get_by_id(owner_id, permission_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_transition(
    current: CrawlPermissionState, target: CrawlPermissionState
) -> None:
    """Raise ``ValueError`` if the transition is illegal."""
    if not is_permission_transition_allowed(current, target):
        raise ValueError(
            f"illegal crawl permission transition: {current.value} -> {target.value}"
        )
