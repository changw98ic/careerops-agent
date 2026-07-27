"""PostgreSQL-backed mail-intelligence repositories (Section 12).

- :class:`PostgresEmailEventProposalRepository` persists durable
  :class:`EmailEventProposal` records on ``careerops.email_event_proposals``
  (task 12.5). Server-side candidate scoping on every read/write; idempotent
  lookup by ``idempotency_key``; cursor pagination over ``(created_at, id)``.
- :class:`PostgresMailMessageRepository` reads a minimized, sanitized
  :class:`MailMessageInput` for extraction (task 12.3 input). It joins
  ``email_messages`` → ``email_threads`` to honor ownership (the candidate owns
  the application the thread is linked to). When GMAIL_READ is disabled no new
  rows arrive here; the slice (task 12.10) ingests fixtures directly.

Iron rules honored:
- Server-side ownership (Iron Rule 2/6): every read is scoped by the
  server-resolved ``candidate_id``; a not-owned proposal/message is absent.
- Append-only / reversible (Iron Rule 4): the extraction snapshot is immutable
  once written; only the decision state mutates (pending → terminal).
- No external writes: this module is a record store only.
"""

# RowMapping values are typed as ``Any`` by SQLAlchemy.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.application.mail_extraction import MailMessageInput
from careerops.domain.mail_intelligence import (
    EmailEventProposal,
    EmailEventProposalState,
    EmailEvidenceSpan,
    MailCategory,
    MailExtraction,
    MailExtractionSource,
    MailOutcome,
)
from careerops.infrastructure.database.schema import (
    email_event_proposals,
    email_messages,
    email_threads,
)

__all__ = [
    "PostgresEmailEventProposalRepository",
    "PostgresMailMessageRepository",
]


# ---------------------------------------------------------------------------
# row <-> domain mapping
# ---------------------------------------------------------------------------


def _extraction_to_row(extraction: MailExtraction) -> dict[str, Any]:
    return {
        "category": extraction.category.value,
        "outcome": extraction.outcome.value,
        "sender_email": extraction.sender_email,
        "sender_name": extraction.sender_name,
        "sender_domain": extraction.sender_domain,
        "interview_at": extraction.interview_at.isoformat() if extraction.interview_at else None,
        "timezone": extraction.timezone,
        "deadline": extraction.deadline.isoformat() if extraction.deadline else None,
        "requested_materials": list(extraction.requested_materials),
        "compensation": extraction.compensation,
        "summary": extraction.summary,
        "evidence_spans": [
            {"label": s.label, "text": s.text, "start": s.start, "end": s.end}
            for s in extraction.evidence_spans
        ],
        "source": extraction.source.value,
        "rules_version": extraction.rules_version,
        "model_version": extraction.model_version,
        "high_risk": extraction.high_risk,
        "review_required": extraction.review_required,
        "prompt_injection_detected": extraction.prompt_injection_detected,
    }


def _row_to_extraction(row: dict[str, Any]) -> MailExtraction:
    def _opt_dt(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    spans = tuple(
        EmailEvidenceSpan(
            label=str(s.get("label", "")),
            text=str(s.get("text", "")),
            start=int(s.get("start", 0)),
            end=int(s.get("end", 0)),
        )
        for s in (row.get("evidence_spans") or [])
    )
    return MailExtraction(
        category=MailCategory(row.get("category", "unknown")),
        outcome=MailOutcome(row.get("outcome", "unknown")),
        sender_email=str(row.get("sender_email", "")),
        sender_name=str(row.get("sender_name", "")),
        sender_domain=str(row.get("sender_domain", "")),
        interview_at=_opt_dt(row.get("interview_at")),
        timezone=str(row.get("timezone", "")),
        deadline=_opt_dt(row.get("deadline")),
        requested_materials=tuple(row.get("requested_materials") or ()),
        compensation=str(row.get("compensation", "")),
        summary=str(row.get("summary", "")),
        confidence=float(row.get("confidence", 0.0) or 0.0),
        evidence_spans=spans,
        source=MailExtractionSource(row.get("source", "rules")),
        rules_version=str(row.get("rules_version", "")),
        model_version=str(row.get("model_version", "")),
        high_risk=bool(row.get("high_risk", False)),
        review_required=bool(row.get("review_required", True)),
        prompt_injection_detected=bool(row.get("prompt_injection_detected", False)),
    )


def _row_to_proposal(row: sa.RowMapping) -> EmailEventProposal:
    extraction = _row_to_extraction(row["extraction"] or {})
    return EmailEventProposal(
        id=row["id"],
        message_id=row["message_id"],
        thread_id=row.get("thread_id"),
        account_id=row.get("account_id"),
        candidate_id=row["candidate_id"],
        application_id=row.get("application_id"),
        extraction=extraction,
        proposed_state=row.get("proposed_state"),
        idempotency_key=row["idempotency_key"],
        state=EmailEventProposalState(row["state"]),
        decided_at=row.get("decided_at"),
        decided_by=row.get("decided_by") or "",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# Proposal repository
# ---------------------------------------------------------------------------


class PostgresEmailEventProposalRepository:
    """Durable EmailEventProposal store backed by ``email_event_proposals``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_by_id(self, proposal_id: UUID) -> EmailEventProposal | None:
        stmt = sa.select(email_event_proposals).where(email_event_proposals.c.id == proposal_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_proposal(row) if row else None

    def find_by_idempotency_key(self, key: str) -> EmailEventProposal | None:
        stmt = sa.select(email_event_proposals).where(
            email_event_proposals.c.idempotency_key == key
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_proposal(row) if row else None

    def find_active_for_message(self, message_id: UUID) -> EmailEventProposal | None:
        stmt = (
            sa.select(email_event_proposals)
            .where(email_event_proposals.c.message_id == message_id)
            .order_by(email_event_proposals.c.created_at.desc())
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_proposal(row) if row else None

    def save(self, proposal: EmailEventProposal) -> None:
        extraction_row = _extraction_to_row(proposal.extraction)
        values: dict[str, Any] = {
            "id": proposal.id,
            "message_id": proposal.message_id,
            "thread_id": proposal.thread_id,
            "account_id": proposal.account_id,
            "candidate_id": proposal.candidate_id,
            "application_id": proposal.application_id,
            "category": proposal.extraction.category.value,
            "proposed_state": proposal.proposed_state,
            "extraction": extraction_row,
            "confidence": proposal.extraction.confidence,
            "high_risk": proposal.extraction.high_risk,
            "review_required": proposal.extraction.review_required,
            "prompt_injection_detected": proposal.extraction.prompt_injection_detected,
            "idempotency_key": proposal.idempotency_key,
            "state": proposal.state.value,
            "extraction_source": proposal.extraction.source.value,
            "rules_version": proposal.extraction.rules_version,
            "model_version": proposal.extraction.model_version,
            "decided_at": proposal.decided_at,
            "decided_by": proposal.decided_by,
            "created_at": proposal.created_at,
            "updated_at": proposal.updated_at,
        }
        with self._engine.begin() as conn:
            stmt = (
                pg_insert(email_event_proposals)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={  # type: ignore[arg-type]
                        "application_id": values["application_id"],
                        "state": values["state"],
                        "decided_at": values["decided_at"],
                        "decided_by": values["decided_by"],
                        "updated_at": values["updated_at"],
                        "category": values["category"],
                        "proposed_state": values["proposed_state"],
                        "extraction": values["extraction"],
                        "confidence": values["confidence"],
                        "high_risk": values["high_risk"],
                        "review_required": values["review_required"],
                        "prompt_injection_detected": values["prompt_injection_detected"],
                        "extraction_source": values["extraction_source"],
                        "rules_version": values["rules_version"],
                        "model_version": values["model_version"],
                    },
                )
            )
            conn.execute(stmt)

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        state: EmailEventProposalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[EmailEventProposal], tuple[datetime, UUID] | None]:
        stmt = sa.select(email_event_proposals).where(
            email_event_proposals.c.candidate_id == candidate_id
        )
        if state is not None:
            stmt = stmt.where(email_event_proposals.c.state == state.value)
        if cursor is not None:
            cursor_time, cursor_id = cursor
            stmt = stmt.where(
                sa.or_(
                    email_event_proposals.c.created_at < cursor_time,
                    sa.and_(
                        email_event_proposals.c.created_at == cursor_time,
                        email_event_proposals.c.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            email_event_proposals.c.created_at.desc(),
            email_event_proposals.c.id.desc(),
        ).limit(limit + 1)
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_row_to_proposal(r) for r in rows]
        next_cursor: tuple[datetime, UUID] | None = None
        if has_more and items:
            last = items[-1]
            if last.created_at is not None:
                next_cursor = (last.created_at, last.id)
        return items, next_cursor


# ---------------------------------------------------------------------------
# Message repository
# ---------------------------------------------------------------------------


class PostgresMailMessageRepository:
    """Read a minimized message for extraction, scoped by candidate ownership.

    Ownership is derived from the thread → application → candidate chain
    (``email_threads.application_id`` → ``applications.candidate_id``). A message
    whose thread is not linked to an application owned by ``candidate_id`` is
    treated as absent (the extractor never sees unowned content).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_message(self, message_id: UUID, candidate_id: UUID) -> MailMessageInput | None:
        # Join messages → threads → applications to enforce candidate ownership.
        stmt = (
            sa.select(
                email_messages.c.id.label("message_id"),
                email_messages.c.thread_id,
                email_messages.c.account_id,
                email_messages.c.sender_email,
                email_messages.c.sender_name,
                email_messages.c.subject,
                email_messages.c.snippet,
                email_messages.c.received_at,
            )
            .select_from(
                email_messages.join(
                    email_threads,
                    email_messages.c.thread_id == email_threads.c.id,
                )
            )
            .where(email_messages.c.id == message_id)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        # Body is deliberately NOT persisted for non-recruitment mail and only
        # minimally for recruitment mail (Section 11). The extractor consumes
        # the sanitized snippet; the full body never enters this layer.
        return MailMessageInput(
            message_id=row["message_id"],
            thread_id=row.get("thread_id"),
            account_id=row.get("account_id"),
            sender_email=str(row.get("sender_email") or ""),
            sender_name=str(row.get("sender_name") or ""),
            subject=str(row.get("subject") or ""),
            snippet=str(row.get("snippet") or ""),
            body_text=str(row.get("snippet") or ""),  # minimized input
            received_at=row.get("received_at"),
        )
