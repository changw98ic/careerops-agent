"""e2e inbox snooze records (Section 6)

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-26

Additive migration: adds ``inbox_snoozes`` table for persisting per-candidate
snooze decisions. A snooze hides a job from the inbox until ``snoozed_until``
passes.  Snooze is NOT an application state change -- it is a separate user
decision that does not touch the applications table.  No existing tables are
modified; downgrade drops only the new table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_snoozes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "canonical_job_id",
            name="uq_inbox_snoozes_candidate_job",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_inbox_snoozes_candidate_until",
        "inbox_snoozes",
        ["candidate_id", "snoozed_until"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_table("inbox_snoozes", schema="careerops")
