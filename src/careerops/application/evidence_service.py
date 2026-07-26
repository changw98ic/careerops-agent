"""Candidate-evidence application service (Section 3, tasks 3.5 + 3.6).

Owns two concerns that sit above the Section-2
:class:`PostgresEvidenceRepository`:

1. **Evidence lifecycle review (task 3.6)**: idempotent confirm/reject with an
   append-only audit event recording the actor, timestamp, and source
   reference. Repeat decisions return the existing state WITHOUT recording a
   second audit event (spec: "User confirms or rejects a claim → records the
   actor, timestamp, source reference, and decision without rewriting the
   original resume content").
2. **Confirmation gating (Iron Rule 4)**: only ``CONFIRMED`` evidence is
   eligible for application-package claims. The service exposes
   :meth:`list_eligible_for_package` so a later package service consumes one
   source of truth for that gate.

Iron rules honored:

- **Server-side candidate ownership** (Iron Rule 1): every method takes the
  server-resolved ``candidate_id``.
- **Idempotent + audit (Iron Rule 7)**: the repository is already idempotent
  (``confirm`` of an already-confirmed row is a no-op); the service layers the
  "record an audit event only on an actual transition" rule on top so the
  audit trail is append-only and free of duplicate entries.
- **Default-deny for packages**: :meth:`list_eligible_for_package` returns
  CONFIRMED evidence only — the single chokepoint a package service must use.

The audit sink is a Protocol so tests can substitute an in-memory list-backed
sink and so a durable sink can be wired in a later stage without changing the
service contract. The default is :class:`LoggingEvidenceAuditSink` (structured
JSON log on ``careerops.evidence.audit``); no durable sink is wired yet. The
seam is intentionally here so a future hash-chained audit table can be slotted
in without touching the service or its callers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from careerops.api.errors import NotFoundError
from careerops.domain.applications import ConfirmationStatus
from careerops.domain.candidates import EvidenceItem

__all__ = [
    "EvidenceAuditEvent",
    "EvidenceAuditSink",
    "EvidenceDecision",
    "EvidenceRepositoryProtocol",
    "EvidenceService",
    "ListEvidenceAuditSink",
    "LoggingEvidenceAuditSink",
]

_log = logging.getLogger("careerops.evidence.audit")


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    """The decision recorded for one evidence item.

    ``status`` is the resulting :class:`ConfirmationStatus`. ``was_change`` is
    True only when this call actually transitioned the status — repeat
    decisions return the existing state with ``was_change=False`` so the caller
    can tell idempotent repeats from first-time decisions (and so the audit
    trail records exactly one event per real transition).
    """

    evidence: EvidenceItem
    status: ConfirmationStatus
    was_change: bool


@dataclass(frozen=True, slots=True)
class EvidenceAuditEvent:
    """Append-only audit record for an evidence decision.

    Captures the spec-mandated fields: actor (``actor_id`` + ``actor_type``),
    timestamp (``occurred_at``), source reference (``source_reference``), the
    target evidence id, and the decision. ``prior_status`` records the state
    the item transitioned from so the trail is self-explaining.
    """

    evidence_id: UUID
    candidate_id: UUID
    decision: ConfirmationStatus
    prior_status: ConfirmationStatus
    actor_id: str
    actor_type: str
    source_reference: str
    occurred_at: datetime
    resume_version_id: UUID | None = None


class EvidenceAuditSink(Protocol):
    """Append-only audit sink for evidence decisions (Iron Rule 7)."""

    def record(self, event: EvidenceAuditEvent) -> None: ...


class ListEvidenceAuditSink:
    """In-memory append-only audit sink — collects events for test assertions."""

    def __init__(self) -> None:
        self.events: list[EvidenceAuditEvent] = []

    def record(self, event: EvidenceAuditEvent) -> None:
        self.events.append(event)


class LoggingEvidenceAuditSink:
    """Structured-log audit sink — append-only by nature of immutable logs.

    Used as the default when no durable audit store is wired (e.g. an
    in-memory-only runtime). Each event is emitted as a single JSON line on the
    ``careerops.evidence.audit`` logger so an operator can correlate decisions
    without a DB round-trip.
    """

    def record(self, event: EvidenceAuditEvent) -> None:
        payload = {
            "evidence_id": str(event.evidence_id),
            "candidate_id": str(event.candidate_id),
            "decision": event.decision.value,
            "prior_status": event.prior_status.value,
            "actor_id": event.actor_id,
            "actor_type": event.actor_type,
            "source_reference": event.source_reference,
            "occurred_at": event.occurred_at.isoformat(),
            "resume_version_id": str(event.resume_version_id) if event.resume_version_id else None,
        }
        _log.info("evidence decision: %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


class EvidenceRepositoryProtocol(Protocol):
    """Repository seam consumed by :class:`EvidenceService`.

    Satisfied by :class:`PostgresEvidenceRepository` and
    :class:`InMemoryEvidenceRepository`. Every read/write scopes by the
    server-resolved ``candidate_id``.
    """

    def get_by_id(self, candidate_id: UUID, evidence_id: UUID) -> EvidenceItem: ...

    def list_for_candidate(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]: ...

    def list_confirmed_for(self, candidate_id: UUID, *, limit: int = 200) -> list[EvidenceItem]: ...

    def confirm(
        self, candidate_id: UUID, evidence_id: UUID, *, now: datetime | None = None
    ) -> EvidenceItem: ...

    def reject(
        self, candidate_id: UUID, evidence_id: UUID, *, now: datetime | None = None
    ) -> EvidenceItem: ...

    def store(self, item: EvidenceItem) -> EvidenceItem: ...


@dataclass(frozen=True, slots=True)
class EvidenceReviewRequest:
    """Input for a confirm/reject decision (task 3.6 route body)."""

    evidence_id: UUID
    actor_id: str
    actor_type: str = "user"
    source_reference: str = ""
    now: datetime | None = None


class EvidenceService:
    """Application service for resume-derived candidate evidence.

    Confirm/reject is idempotent at the service boundary: a repeat decision
    returns the existing state (``was_change=False``) WITHOUT recording a fresh
    audit event. Only an actual status transition appends to the audit trail.
    """

    def __init__(
        self,
        repository: EvidenceRepositoryProtocol,
        audit_sink: EvidenceAuditSink | None = None,
    ) -> None:
        self._repo = repository
        self._audit = audit_sink or LoggingEvidenceAuditSink()

    # -- reads --------------------------------------------------------------

    def get(self, candidate_id: UUID, evidence_id: UUID) -> EvidenceItem:
        """Return one evidence item, scoped by candidate ownership (404 otherwise)."""
        return self._repo.get_by_id(candidate_id, evidence_id)

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        status: ConfirmationStatus | None = None,
        limit: int = 200,
    ) -> list[EvidenceItem]:
        """List evidence for the candidate, optionally filtered by status."""
        if status is ConfirmationStatus.CONFIRMED:
            return self._repo.list_confirmed_for(candidate_id, limit=limit)
        items = self._repo.list_for_candidate(candidate_id, limit=limit)
        if status is None:
            return items
        return [i for i in items if i.confirmation_status is status]

    def list_eligible_for_package(
        self, candidate_id: UUID, *, limit: int = 200
    ) -> list[EvidenceItem]:
        """CONFIRMED evidence only — the single gate package claims must use.

        Iron Rule 4: every positive claim in an approved package must reference
        CONFIRMED candidate evidence. A package service MUST consume this method
        (or the repository's ``list_confirmed_for`` directly) rather than
        re-implementing the filter.
        """
        return self._repo.list_confirmed_for(candidate_id, limit=limit)

    # -- writes -------------------------------------------------------------

    def confirm(self, candidate_id: UUID, request: EvidenceReviewRequest) -> EvidenceDecision:
        """Mark an evidence item CONFIRMED; idempotent with append-only audit.

        Raises :class:`NotFoundError` (404) if the evidence id does not belong
        to the candidate. A repeat confirm returns the existing item with
        ``was_change=False`` and records NO new audit event.
        """
        return self._apply_decision(candidate_id, request, ConfirmationStatus.CONFIRMED)

    def reject(self, candidate_id: UUID, request: EvidenceReviewRequest) -> EvidenceDecision:
        """Mark an evidence item REJECTED; idempotent with append-only audit."""
        return self._apply_decision(candidate_id, request, ConfirmationStatus.REJECTED)

    # -- internals ----------------------------------------------------------

    def _apply_decision(
        self,
        candidate_id: UUID,
        request: EvidenceReviewRequest,
        target: ConfirmationStatus,
    ) -> EvidenceDecision:
        # Verify ownership + read the prior status BEFORE mutating. The repo's
        # own get_by_id raises NotFoundError for a cross-candidate id, so a
        # client-supplied evidence_id for another candidate never leaks.
        existing = self._repo.get_by_id(candidate_id, request.evidence_id)
        if existing.confirmation_status is target:
            # Idempotent repeat: existing state, no audit event.
            return EvidenceDecision(evidence=existing, status=target, was_change=False)

        if target is ConfirmationStatus.CONFIRMED:
            updated = self._repo.confirm(candidate_id, request.evidence_id, now=request.now)
        else:
            updated = self._repo.reject(candidate_id, request.evidence_id, now=request.now)

        # Append exactly one audit event for the real transition.
        self._audit.record(
            EvidenceAuditEvent(
                evidence_id=updated.id,
                candidate_id=candidate_id,
                decision=target,
                prior_status=existing.confirmation_status,
                actor_id=request.actor_id,
                actor_type=request.actor_type,
                source_reference=request.source_reference,
                occurred_at=_now_or(request.now),
                resume_version_id=updated.resume_version_id,
            )
        )
        return EvidenceDecision(evidence=updated, status=target, was_change=True)


def _now_or(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(tz=UTC)


__all__ += ["NotFoundError"]
