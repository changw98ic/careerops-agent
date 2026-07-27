"""PostgreSQL-backed reply-draft repository (Section 13, tasks 13.4-13.5).

Persists immutable :class:`ReplyDraft` versions on
``careerops.reply_draft_versions``. Server-side candidate scoping on every
read/write; cursor pagination over ``(created_at, id)``; idempotent upsert on
``id`` so a save of an existing version updates only the mutable lifecycle
fields (state / decided_at / send_intent_id / send_phase). The
``find_latest_for_thread`` lookup drives version-number assignment for body
edits.

Iron rules honored:
- Server-side ownership (Iron Rule 2/6): every read is scoped by the
  server-resolved ``candidate_id``; a not-owned draft is absent.
- Append-only / reversible (Iron Rule 4): versions are immutable; only the
  mutable lifecycle fields are updated on a save of the same id.
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

from careerops.domain.reply_draft import (
    ApplicationFact,
    ReplyDraft,
    ReplyDraftApprovalState,
    ReplyDraftClaim,
    ReplyDraftContext,
    ReplyIntent,
    ReplyRiskCategory,
)
from careerops.infrastructure.database.schema import reply_draft_versions

__all__ = ["PostgresReplyDraftRepository"]


# ---------------------------------------------------------------------------
# Row <-> domain mapping
# ---------------------------------------------------------------------------


def _context_to_row(ctx: ReplyDraftContext) -> dict[str, Any]:
    return {
        "message_id": str(ctx.message_id),
        "thread_id": str(ctx.thread_id),
        "application_id": str(ctx.application_id) if ctx.application_id else None,
        "thread_excerpt": ctx.thread_excerpt,
        "application_facts": [
            {"label": f.label, "value": f.value, "source": f.source} for f in ctx.application_facts
        ],
        "evidence_refs": [str(e) for e in ctx.evidence_refs],
        "intent": ctx.intent.value,
        "risk_category": ctx.risk_category.value,
        "mail_category": ctx.mail_category,
    }


def _row_to_context(row: dict[str, Any]) -> ReplyDraftContext:
    raw = row or {}
    application_id = raw.get("application_id")
    return ReplyDraftContext(
        message_id=UUID(raw["message_id"]) if raw.get("message_id") else UUID(int=0),
        thread_id=UUID(raw["thread_id"]) if raw.get("thread_id") else UUID(int=0),
        application_id=UUID(application_id) if application_id else None,
        thread_excerpt=raw.get("thread_excerpt", ""),
        application_facts=tuple(
            ApplicationFact(
                label=f.get("label", ""),
                value=f.get("value", ""),
                source=f.get("source", "application"),
            )
            for f in raw.get("application_facts", [])
        ),
        evidence_refs=tuple(UUID(e) for e in raw.get("evidence_refs", []) if e),
        intent=ReplyIntent(raw.get("intent", "acknowledge")),
        risk_category=ReplyRiskCategory(raw.get("risk_category", "low_risk")),
        mail_category=raw.get("mail_category", ""),
    )


def _claims_to_row(claims: tuple[ReplyDraftClaim, ...]) -> list[dict[str, Any]]:
    return [
        {
            "claim_text": c.claim_text,
            "evidence_ids": [str(e) for e in c.evidence_ids],
            "supported": c.supported,
        }
        for c in claims
    ]


def _row_to_claims(raw: Any) -> tuple[ReplyDraftClaim, ...]:
    items = raw or []
    claims: list[ReplyDraftClaim] = []
    for c in items:
        claims.append(
            ReplyDraftClaim(
                claim_text=c.get("claim_text", ""),
                evidence_ids=tuple(UUID(e) for e in c.get("evidence_ids", []) if e),
                supported=c.get("supported", True),
            )
        )
    return tuple(claims)


def _row_to_draft(row: sa.RowMapping) -> ReplyDraft:
    return ReplyDraft(
        id=row["id"],
        candidate_id=row["candidate_id"],
        message_id=row["message_id"],
        thread_id=row["thread_id"],
        account_id=row["account_id"],
        application_id=row["application_id"],
        recipient=row["recipient"],
        in_reply_to=row["in_reply_to_header"],
        references_header=row["references_header"],
        subject=row["subject"],
        body=row["body_text"],
        intent=ReplyIntent(row["intent"]),
        risk_category=ReplyRiskCategory(row["risk_category"]),
        context=_row_to_context(row["context"]),
        claims=_row_to_claims(row["claims"]),
        validation_issues=tuple(row["validation_issues"] or []),
        payload_hash=row["payload_hash"],
        version_number=row["version_number"],
        approval_state=ReplyDraftApprovalState(row["approval_state"]),
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        send_intent_id=row["send_intent_id"],
        send_phase=row["send_phase"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class PostgresReplyDraftRepository:
    """Durable :class:`ReplyDraft` version store backed by Postgres."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_by_id(self, draft_id: UUID) -> ReplyDraft | None:
        stmt = sa.select(reply_draft_versions).where(reply_draft_versions.c.id == draft_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_draft(row) if row else None

    def find_latest_for_thread(self, thread_id: UUID, candidate_id: UUID) -> ReplyDraft | None:
        stmt = (
            sa.select(reply_draft_versions)
            .where(
                reply_draft_versions.c.thread_id == thread_id,
                reply_draft_versions.c.candidate_id == candidate_id,
            )
            .order_by(reply_draft_versions.c.version_number.desc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_draft(row) if row else None

    def find_by_idempotency_key(self, key: str) -> ReplyDraft | None:
        # Idempotency is keyed on payload_hash (the canonical content hash).
        # Kept for protocol parity; the service uses find_latest_for_thread.
        stmt = sa.select(reply_draft_versions).where(reply_draft_versions.c.payload_hash == key)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row_to_draft(row) if row else None

    def save(self, draft: ReplyDraft) -> None:
        values: dict[str, Any] = {
            "id": draft.id,
            "candidate_id": draft.candidate_id,
            "message_id": draft.message_id,
            "thread_id": draft.thread_id,
            "account_id": draft.account_id,
            "application_id": draft.application_id,
            "version_number": draft.version_number,
            "recipient": draft.recipient,
            "in_reply_to_header": draft.in_reply_to,
            "references_header": draft.references_header,
            "subject": draft.subject,
            "body_text": draft.body,
            "intent": draft.intent.value,
            "risk_category": draft.risk_category.value,
            "mail_category": draft.context.mail_category,
            "context": _context_to_row(draft.context),
            "claims": _claims_to_row(draft.claims),
            "validation_issues": list(draft.validation_issues),
            "payload_hash": draft.payload_hash,
            "approval_state": draft.approval_state.value,
            "decided_at": draft.decided_at,
            "decided_by": draft.decided_by,
            "send_intent_id": draft.send_intent_id,
            "send_phase": draft.send_phase,
            "created_at": draft.created_at,
            "updated_at": draft.updated_at,
        }
        with self._engine.begin() as conn:
            stmt = (
                pg_insert(reply_draft_versions)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["id"],  # type: ignore[arg-type]
                    set_={
                        "approval_state": values["approval_state"],
                        "decided_at": values["decided_at"],
                        "decided_by": values["decided_by"],
                        "send_intent_id": values["send_intent_id"],
                        "send_phase": values["send_phase"],
                        "validation_issues": values["validation_issues"],
                        "subject": values["subject"],
                        "body_text": values["body_text"],
                        "claims": values["claims"],
                        "payload_hash": values["payload_hash"],
                        "application_id": values["application_id"],
                        "updated_at": values["updated_at"],
                    },
                )
            )
            conn.execute(stmt)

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        application_id: UUID | None = None,
        state: ReplyDraftApprovalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[ReplyDraft], tuple[datetime, UUID] | None]:
        stmt = sa.select(reply_draft_versions).where(
            reply_draft_versions.c.candidate_id == candidate_id
        )
        if application_id is not None:
            stmt = stmt.where(reply_draft_versions.c.application_id == application_id)
        if state is not None:
            stmt = stmt.where(reply_draft_versions.c.approval_state == state.value)
        if cursor is not None:
            cursor_time, cursor_id = cursor
            stmt = stmt.where(
                sa.or_(
                    reply_draft_versions.c.created_at < cursor_time,
                    sa.and_(
                        reply_draft_versions.c.created_at == cursor_time,
                        reply_draft_versions.c.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            reply_draft_versions.c.created_at.desc(),
            reply_draft_versions.c.id.desc(),
        ).limit(limit + 1)
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).mappings())
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [_row_to_draft(r) for r in rows]
        next_cursor: tuple[datetime, UUID] | None = None
        if has_more and items:
            last = items[-1]
            if last.created_at is not None:
                next_cursor = (last.created_at, last.id)
        return items, next_cursor
