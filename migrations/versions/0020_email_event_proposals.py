"""email event proposals (Section 12)

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-26

Additive migration: introduces ``email_event_proposals`` — durable,
reviewable mail-derived event proposals (design Decision 4 / Section 12).
One message owns at most one active proposal per extraction content hash
(``idempotency_key``); a re-run of deterministic + model extraction for the
same message returns the existing proposal. The proposal links a message /
thread to at most one application (nullable ``application_id`` for the
unresolved-link case, task 12.10). The extraction is stored as an immutable
JSONB snapshot; only the decision state is mutable (``pending`` → terminal).

Server-side candidate scoping is enforced via the non-null ``candidate_id``
(the server-resolved owner). No existing tables are modified; downgrade drops
only the new table and its indexes.

This migration does NOT enable any live OAuth, external-write, or auto-send
flag (task 17.6). The table is a review-only record store; acceptance delegates
to the existing USER-sourced application transition service and never lets a
proposal write :class:`ApplicationState` directly (Iron Rule 2).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_event_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
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
            nullable=True,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("proposed_state", sa.String(24), nullable=True),
        sa.Column(
            "extraction",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
        sa.Column(
            "high_risk",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "review_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "prompt_injection_detected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("extraction_source", sa.String(8), server_default="rules", nullable=False),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column("model_version", sa.Text(), server_default="", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), server_default="", nullable=False),
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
        sa.UniqueConstraint("idempotency_key", name="uq_email_event_proposals_idempotency"),
        sa.CheckConstraint(
            "state IN ('pending', 'accepted', 'rejected', 'superseded')",
            name="ck_email_event_proposals_state_values",
        ),
        sa.CheckConstraint(
            "extraction_source IN ('rules', 'model')",
            name="ck_email_event_proposals_extraction_source_values",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_email_event_proposals_confidence_range",
        ),
        sa.CheckConstraint(
            "(state IN ('accepted', 'rejected') AND decided_at IS NOT NULL) OR "
            "(state IN ('pending', 'superseded') AND decided_at IS NULL)",
            name="ck_email_event_proposals_decision_timestamp_consistent",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_email_event_proposals_candidate_state",
        "email_event_proposals",
        ["candidate_id", "state"],
        schema="careerops",
    )
    op.create_index(
        "ix_email_event_proposals_message",
        "email_event_proposals",
        ["message_id"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_event_proposals_message",
        table_name="email_event_proposals",
        schema="careerops",
    )
    op.drop_index(
        "ix_email_event_proposals_candidate_state",
        table_name="email_event_proposals",
        schema="careerops",
    )
    op.drop_table("email_event_proposals", schema="careerops")
