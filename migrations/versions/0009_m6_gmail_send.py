"""M6 Gmail Send: send_attempts, send_receipts, reconciliation_records.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-23

M6 adds Gmail send capability through the M5A authorization chain:
- send_attempts: tracks each send attempt bound to an action_intent, with
  idempotency key (account + inbound message + intent + body hash) and
  reconciliation key for deduplication.
- send_receipts: provider-issued receipts with message ID and thread ID.
- reconciliation_records: tracks ambiguous outcomes that require manual
  resolution; auto_retry_disabled is always true (safety invariant).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "careerops"


def upgrade() -> None:
    # -- send_attempts -------------------------------------------------------
    op.create_table(
        "send_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.action_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_email", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("in_reply_to", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "references_list",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("category", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("reconciliation_key", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'succeeded', 'failed', "
            "'reconciliation_required')",
            name="send_attempts_status_values",
        ),
        sa.CheckConstraint(
            "category IN ('delivery_confirmation', 'recruiter_contact_ack', "
            "'assessment_receipt_ack', 'confirmed_time_ack', 'thanks_no_questions', "
            "'scheduling_options', 'follow_up', 'resume_or_link', "
            "'work_authorization', 'deadline_commitment', "
            "'salary', 'offer', 'visa', 'relocation', 'tax', "
            "'background_check', 'identity_or_bank', 'withdrawal', 'unknown')",
            name="send_attempts_category_values",
        ),
        sa.CheckConstraint(
            "char_length(body_hash) = 64",
            name="send_attempts_body_hash_length",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) = 64",
            name="send_attempts_idempotency_key_length",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_send_attempts_intent_status",
        "send_attempts",
        ["action_intent_id", "status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_send_attempts_reconciliation_key",
        "send_attempts",
        ["reconciliation_key"],
        schema=SCHEMA,
    )

    # -- send_receipts -------------------------------------------------------
    op.create_table(
        "send_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "send_attempt_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.send_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("reconciliation_key", sa.Text(), nullable=False, unique=True),
        sa.Column("final_state", sa.String(24), nullable=False),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True)),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "final_state IN ('succeeded', 'failed', 'revoked')",
            name="send_receipts_final_state_values",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_send_receipts_attempt",
        "send_receipts",
        ["send_attempt_id"],
        schema=SCHEMA,
    )

    # -- reconciliation_records ----------------------------------------------
    op.create_table(
        "reconciliation_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "send_attempt_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.send_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.action_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reconciliation_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("window_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column(
            "attempts_within_window",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "auto_retry_disabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("escalated_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_note", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed_sent', 'confirmed_not_sent', "
            "'ambiguous', 'escalated_manual')",
            name="reconciliation_records_status_values",
        ),
        sa.CheckConstraint(
            "auto_retry_disabled = true",
            name="reconciliation_auto_retry_always_disabled",
        ),
        sa.CheckConstraint(
            "window_minutes > 0",
            name="reconciliation_window_positive",
        ),
        sa.CheckConstraint(
            "(resolved_at IS NOT NULL AND resolution_note IS NOT NULL) OR "
            "(resolved_at IS NULL AND resolution_note IS NULL)",
            name="reconciliation_resolution_consistent",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_reconciliation_records_intent_status",
        "reconciliation_records",
        ["action_intent_id", "status"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_reconciliation_records_unresolved",
        "reconciliation_records",
        ["status"],
        schema=SCHEMA,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("reconciliation_records", schema=SCHEMA)
    op.drop_table("send_receipts", schema=SCHEMA)
    op.drop_table("send_attempts", schema=SCHEMA)
