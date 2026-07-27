"""reply draft versions (Section 13)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-26

Additive migration: introduces ``reply_draft_versions`` — immutable,
candidate-owned reply-draft versions (Section 13, tasks 13.4-13.5). One
candidate owns N reply drafts; body edits create a NEW version (fresh payload
hash) sharing the same trusted recipient + thread headers, while a recipient /
target-thread edit is rejected and must start a fresh draft that re-enters
recipient trust, policy and approval (Iron Rule 2 / 4).

The table is server-side-candidate-scoped (``candidate_id`` is required, never
client-supplied) and does NOT touch the legacy M4 ``reply_drafts`` table,
which remains for backfill/back-compat. No existing tables are modified;
downgrade drops only the new table + its indexes.

Column notes:

- ``candidate_id`` — server-resolved owner; Iron Rule 2/6 ownership scope.
- ``message_id`` / ``thread_id`` — the linked inbound thread anchor.
- ``application_id`` — nullable so an unresolved-link draft can still be
  created for review (task 13.3 / 13.9); SET NULL on application delete so a
  deleted application never cascades into losing the audit trail.
- ``recipient`` / ``in_reply_to`` / ``references_header`` — the immutable
  trusted reply target (task 13.4).
- ``payload_hash`` — sha256 (64 hex) over the exact reply inputs; frozen at
  approval so any byte change is detectable (task 13.5).
- ``risk_category`` — derived deterministically; high-risk / permanently-denied
  categories are never system-sent (task 13.6).
- ``validation_issues`` — the deterministic unsupported-claim findings that
  block approval until corrected (task 13.4).
- ``context`` — the bounded, minimized trusted context (thread excerpt,
  application facts, evidence refs) as JSONB.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reply_draft_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_threads.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        # Immutable trusted reply target (task 13.4).
        sa.Column("recipient", sa.Text(), server_default="", nullable=False),
        sa.Column("in_reply_to_header", sa.Text(), server_default="", nullable=False),
        sa.Column("references_header", sa.Text(), server_default="", nullable=False),
        # Editable content (body edits -> new version).
        sa.Column("subject", sa.Text(), server_default="", nullable=False),
        sa.Column("body_text", sa.Text(), server_default="", nullable=False),
        sa.Column("intent", sa.String(24), server_default="acknowledge", nullable=False),
        sa.Column("risk_category", sa.String(24), server_default="low_risk", nullable=False),
        sa.Column("mail_category", sa.Text(), server_default="", nullable=False),
        # Context + claims + validation findings (JSONB).
        sa.Column(
            "context",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "claims",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "validation_issues",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), server_default="", nullable=False),
        sa.Column("send_intent_id", sa.Uuid(), nullable=True),
        sa.Column("send_phase", sa.String(24), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "thread_id",
            "candidate_id",
            "version_number",
            name="uq_reply_draft_versions_thread_candidate_version",
        ),
        sa.CheckConstraint(
            "approval_state IN "
            "('draft', 'pending_review', 'approved', 'rejected', 'expired', 'superseded')",
            name="ck_reply_draft_versions_approval_state",
        ),
        sa.CheckConstraint(
            "char_length(payload_hash) = 64",
            name="ck_reply_draft_versions_payload_hash_length",
        ),
        sa.CheckConstraint(
            "(approval_state IN ('approved', 'rejected') AND decided_at IS NOT NULL) "
            "OR (approval_state NOT IN ('approved', 'rejected') AND decided_at IS NULL)",
            name="ck_reply_draft_versions_decision_timestamp_consistent",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_reply_draft_versions_candidate_state",
        "reply_draft_versions",
        ["candidate_id", "approval_state"],
        schema="careerops",
    )
    op.create_index(
        "ix_reply_draft_versions_application",
        "reply_draft_versions",
        ["application_id"],
        schema="careerops",
    )
    op.create_index(
        "ix_reply_draft_versions_thread",
        "reply_draft_versions",
        ["thread_id", "candidate_id"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reply_draft_versions_thread", table_name="reply_draft_versions", schema="careerops"
    )
    op.drop_index(
        "ix_reply_draft_versions_application",
        table_name="reply_draft_versions",
        schema="careerops",
    )
    op.drop_index(
        "ix_reply_draft_versions_candidate_state",
        table_name="reply_draft_versions",
        schema="careerops",
    )
    op.drop_table("reply_draft_versions", schema="careerops")
