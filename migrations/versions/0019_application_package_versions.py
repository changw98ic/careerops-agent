"""application package versions (Section 8)

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-26

Additive migration: introduces ``application_package_versions`` — immutable,
job-specific package versions (design Decision 4). One application owns N
versions; user edits (or optional review-only model suggestions) create a NEW
version rather than mutating the prior one, so an approved payload is never
overwritten in place. The approved version binds the exact job version, profile
version, confirmed resume, evidence refs, attachment hashes and a payload hash;
any later edit produces a new draft version, superseding (not mutating) the old
approval.

This table is additive and does NOT touch the legacy ``application_packages``
table (M3 single-package-per-app), which remains for backfill/back-compat. No
existing tables are modified; downgrade drops only the new table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_package_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "resume_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.resume_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.job_posting_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "profile_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.profile_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("cover_letter_text", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "answers",
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
            "attachments",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "diff",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requirement_gaps",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Text(), server_default="", nullable=False),
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
            "application_id",
            "version_number",
            name="uq_application_package_versions_app_version",
        ),
        sa.CheckConstraint(
            "approval_state IN ('draft', 'pending_review', 'approved', 'rejected')",
            name="ck_application_package_versions_approval_state",
        ),
        sa.CheckConstraint(
            "payload_hash IS NULL OR char_length(payload_hash) = 64",
            name="ck_application_package_versions_payload_hash_length",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_application_package_versions_app",
        "application_package_versions",
        ["application_id", "version_number"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_table("application_package_versions", schema="careerops")
