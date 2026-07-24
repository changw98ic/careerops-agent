"""add M4 tables: email_accounts, email_threads, email_messages, etc.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _api_role_exists() -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api')")
        )
    )


def _grant_for_api_role(statement: str) -> None:
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _api_role_exists():
        op.execute(sa.text(statement))


def upgrade() -> None:
    # --- email_accounts ---
    op.create_table(
        "email_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email_address", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "credential_reference_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.oauth_credential_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("history_id", sa.Text(), server_default="", nullable=False),
        sa.Column("watch_expiration", sa.DateTime(timezone=True)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'error')",
            name="ck_email_accounts_status_values",
        ),
        schema="careerops",
    )

    # --- email_threads ---
    op.create_table(
        "email_threads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_thread_id", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
        ),
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
            "account_id", "provider_thread_id", name="uq_email_threads_account_provider"
        ),
        schema="careerops",
    )

    # --- email_messages ---
    op.create_table(
        "email_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_threads.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("category", sa.String(24), server_default="unknown", nullable=False),
        sa.Column("state", sa.String(24), server_default="synced", nullable=False),
        sa.Column("sender_email", sa.Text(), server_default="", nullable=False),
        sa.Column("sender_name", sa.Text(), server_default="", nullable=False),
        sa.Column("subject", sa.Text(), server_default="", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("snippet", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "body_persisted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id", "provider_message_id", name="uq_email_messages_account_provider"
        ),
        sa.CheckConstraint(
            "category IN ('recruitment', 'non_recruitment', 'unknown')",
            name="ck_email_messages_category_values",
        ),
        sa.CheckConstraint(
            "state IN ('synced', 'classified', 'extracted', 'drafted', 'archived')",
            name="ck_email_messages_state_values",
        ),
        sa.CheckConstraint(
            "(category = 'non_recruitment' AND body_persisted = false) OR "
            "category <> 'non_recruitment'",
            name="ck_email_messages_non_recruitment_body_not_persisted",
        ),
        schema="careerops",
    )

    op.create_index(
        "ix_email_messages_thread_received",
        "email_messages",
        ["thread_id", "received_at"],
        schema="careerops",
    )

    # --- email_extractions (append-only) ---
    op.create_table(
        "email_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("extraction_type", sa.String(32), nullable=False),
        sa.Column(
            "extracted_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
        sa.Column("model_version", sa.Text(), server_default="", nullable=False),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_email_extractions_confidence_range",
        ),
        schema="careerops",
    )

    # --- reply_drafts ---
    op.create_table(
        "reply_drafts",
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
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("to_address", sa.Text(), server_default="", nullable=False),
        sa.Column("subject", sa.Text(), server_default="", nullable=False),
        sa.Column("body_text", sa.Text(), server_default="", nullable=False),
        sa.Column("references_header", sa.Text(), server_default="", nullable=False),
        sa.Column("in_reply_to_header", sa.Text(), server_default="", nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired')",
            name="ck_reply_drafts_status_values",
        ),
        sa.CheckConstraint(
            "char_length(payload_hash) = 64",
            name="ck_reply_drafts_payload_hash_length",
        ),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_at IS NOT NULL) OR "
            "(status <> 'approved' AND approved_at IS NULL)",
            name="ck_reply_drafts_approval_timestamp_consistent",
        ),
        schema="careerops",
    )

    # --- attachment_quarantine ---
    op.create_table(
        "attachment_quarantine",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), server_default="", nullable=False),
        sa.Column("declared_mime", sa.Text(), server_default="", nullable=False),
        sa.Column("detected_mime", sa.Text(), server_default="", nullable=False),
        sa.Column("byte_size", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="quarantined", nullable=False),
        sa.Column("deny_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("content_hash", sa.String(64), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('quarantined', 'cleared', 'denied', 'expired')",
            name="ck_attachment_quarantine_status_values",
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_attachment_quarantine_byte_size_nonnegative"),
        sa.CheckConstraint(
            "(status IN ('cleared', 'denied', 'expired') AND resolved_at IS NOT NULL) OR "
            "(status = 'quarantined' AND resolved_at IS NULL)",
            name="ck_attachment_quarantine_resolution_timestamp_consistent",
        ),
        schema="careerops",
    )

    # --- Grants ---
    _grant_for_api_role(
        "GRANT SELECT, INSERT ON careerops.email_accounts, "
        "careerops.email_threads, careerops.email_messages, "
        "careerops.email_extractions, careerops.reply_drafts, "
        "careerops.attachment_quarantine TO careerops_api"
    )
    _grant_for_api_role(
        "GRANT UPDATE ON careerops.email_accounts, "
        "careerops.email_threads, careerops.email_messages, "
        "careerops.reply_drafts, careerops.attachment_quarantine TO careerops_api"
    )

    # --- Append-only guard ---
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_email_extractions_append_only "
            "BEFORE UPDATE OR DELETE ON careerops.email_extractions "
            "FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
        )
    )


def downgrade() -> None:
    _grant_for_api_role(
        "REVOKE ALL ON careerops.email_accounts, "
        "careerops.email_threads, careerops.email_messages, "
        "careerops.email_extractions, careerops.reply_drafts, "
        "careerops.attachment_quarantine FROM careerops_api"
    )
    op.drop_table("attachment_quarantine", schema="careerops")
    op.drop_table("reply_drafts", schema="careerops")
    op.drop_table("email_extractions", schema="careerops")
    op.drop_index(
        "ix_email_messages_thread_received",
        table_name="email_messages",
        schema="careerops",
    )
    op.drop_table("email_messages", schema="careerops")
    op.drop_table("email_threads", schema="careerops")
    op.drop_table("email_accounts", schema="careerops")
