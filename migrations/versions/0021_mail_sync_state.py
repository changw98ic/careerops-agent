"""mail sync state (Section 11)

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-26

Additive migration for Gmail read synchronization + thread association
(dedicated account, read-only). Introduces:

1. ``email_accounts`` additive columns (task 11.1):
   - ``candidate_id`` — server-side ownership scope (the legacy table was
     unscoped; Iron Rule 2 requires the dedicated account to be owned by a
     candidate). Nullable so existing rows backfill cleanly.
   - ``granted_scopes`` — the validated readonly scope set (JSONB array; always
     a subset of the gmail.readonly allowlist). A forbidden scope rejects the
     connection at the service layer BEFORE the credential is stored.
   - ``connection_state`` — explicit lifecycle (disconnected/connected/revoked/
     error) separate from the legacy ``status`` active/revoked/error tri-state.
   - ``last_error_code`` — bounded machine code for ERROR/REVOKED states.

2. ``email_sync_runs`` (task 11.3) — durable sync-run status so a worker crash
   resumes from durable provider state without losing the cursor or duplicating
   records (partial-sync-restart, task 11.9).

3. ``email_sync_cursors`` (task 11.3) — incremental history/watch cursor, one
   row per account. ``advance_cursor`` is the durable resume point.

4. ``email_thread_links`` (task 11.6) — evidence-backed thread→application
   association. Provider ids / sent-message linkage / trusted domains /
   subject-source evidence populate the snapshot; an ambiguous match is left
   ``unresolved`` for user confirmation rather than silently choosing one.

No existing tables are modified beyond the additive columns; downgrade drops
only the new tables and the new columns. The legacy M4 unique constraints
(``uq_email_messages_account_provider``, ``uq_email_threads_account_provider``)
already enforce provider message/thread id uniqueness (task 11.4).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Additive columns on email_accounts (task 11.1).
    op.add_column(
        "email_accounts",
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "email_accounts",
        sa.Column(
            "granted_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "email_accounts",
        sa.Column(
            "connection_state",
            sa.String(16),
            server_default="disconnected",
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "email_accounts",
        sa.Column("last_error_code", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.create_check_constraint(
        "ck_email_accounts_connection_state",
        "email_accounts",
        "connection_state IN ('disconnected', 'connected', 'revoked', 'error')",
        schema="careerops",
    )
    op.create_index(
        "ix_email_accounts_candidate",
        "email_accounts",
        ["candidate_id"],
        schema="careerops",
    )

    # 2. Durable sync-run status (task 11.3).
    op.create_table(
        "email_sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("direction", sa.String(16), server_default="incremental", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("messages_processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("messages_skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("history_id_start", sa.Text(), server_default="", nullable=False),
        sa.Column("history_id_end", sa.Text(), server_default="", nullable=False),
        sa.Column("error_code", sa.Text(), server_default="", nullable=False),
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
            "status IN ('pending', 'running', 'completed', 'partial', 'failed')",
            name="ck_email_sync_runs_status",
        ),
        sa.CheckConstraint(
            "direction IN ('full', 'incremental', 'backfill')",
            name="ck_email_sync_runs_direction",
        ),
        sa.CheckConstraint("messages_processed >= 0", name="ck_email_sync_runs_processed"),
        sa.CheckConstraint("messages_skipped >= 0", name="ck_email_sync_runs_skipped"),
        schema="careerops",
    )
    op.create_index(
        "ix_email_sync_runs_account_created",
        "email_sync_runs",
        ["account_id", "created_at"],
        schema="careerops",
    )

    # 3. Incremental history/watch cursor (task 11.3).
    op.create_table(
        "email_sync_cursors",
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_accounts.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("history_id", sa.Text(), server_default="", nullable=False),
        sa.Column("watch_expiration", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="careerops",
    )

    # 4. Thread association links (task 11.6).
    op.create_table(
        "email_thread_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.email_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(16), server_default="unlinked", nullable=False),
        sa.Column("confidence", sa.String(24), server_default="none", nullable=False),
        sa.Column(
            "candidate_application_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
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
        sa.UniqueConstraint("thread_id", name="uq_email_thread_links_thread"),
        sa.CheckConstraint(
            "status IN ('unlinked', 'linked', 'unresolved', 'confirmed')",
            name="ck_email_thread_links_status",
        ),
        sa.CheckConstraint(
            "confidence IN ('provider_id', 'sent_message', 'trusted_domain', "
            "'subject_source', 'none')",
            name="ck_email_thread_links_confidence",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_email_thread_links_candidate_status",
        "email_thread_links",
        ["candidate_id", "status"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_table("email_thread_links", schema="careerops")
    op.drop_table("email_sync_cursors", schema="careerops")
    op.drop_table("email_sync_runs", schema="careerops")
    op.drop_index("ix_email_accounts_candidate", table_name="email_accounts", schema="careerops")
    op.drop_constraint(
        "ck_email_accounts_ck_email_accounts_connection_state",
        "email_accounts",
        schema="careerops",
    )
    op.drop_column("email_accounts", "last_error_code", schema="careerops")
    op.drop_column("email_accounts", "connection_state", schema="careerops")
    op.drop_column("email_accounts", "granted_scopes", schema="careerops")
    op.drop_column("email_accounts", "candidate_id", schema="careerops")
