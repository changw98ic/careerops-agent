"""add M3 tables: contacts, applications, lifecycle events, resumes, packages, follow-ups

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
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
    # --- contacts ---
    op.create_table(
        "contacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.companies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), server_default="", nullable=False),
        sa.Column("role", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "publicly_listed",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("domain_match", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("confidence", sa.String(16), server_default="high", nullable=False),
        sa.Column(
            "allowed_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"display\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("company_id", "email", name="uq_contacts_company_email"),
        sa.CheckConstraint(
            "source IN ('job_page', 'careers_page', 'ats_listing', "
            "'official_recruiting_page', 'established_thread')",
            name="ck_contacts_source_values",
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_contacts_confidence_values",
        ),
        sa.CheckConstraint("publicly_listed = true", name="ck_contacts_publicly_listed_required"),
        schema="careerops",
    )

    # --- applications ---
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(24), server_default="favorited", nullable=False),
        sa.Column("apply_url", sa.Text(), server_default="", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("follow_up_due_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
            "candidate_id", "canonical_job_id", name="uq_applications_candidate_job"
        ),
        sa.CheckConstraint(
            "state IN ('favorited', 'ignored', 'preparing', 'submitted', "
            "'interviewing', 'offer', 'rejected', 'withdrawn', 'on_hold')",
            name="ck_applications_state_values",
        ),
        sa.CheckConstraint("version > 0", name="ck_applications_version_positive"),
        schema="careerops",
    )

    op.create_index(
        "ix_applications_candidate_state",
        "applications",
        ["candidate_id", "state"],
        schema="careerops",
    )

    # --- application_lifecycle_events (append-only) ---
    op.create_table(
        "application_lifecycle_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_state", sa.String(24)),
        sa.Column("to_state", sa.String(24)),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.Text(), server_default="", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('created', 'state_changed', 'submitted_manually', "
            "'note_added', 'package_attached', 'follow_up_scheduled', "
            "'follow_up_cancelled', 'follow_up_snoozed', 'follow_up_rescheduled')",
            name="ck_application_lifecycle_events_event_type_values",
        ),
        sa.CheckConstraint(
            "source IN ('user', 'system', 'workflow')",
            name="ck_application_lifecycle_events_source_values",
        ),
        schema="careerops",
    )

    op.create_index(
        "ix_application_lifecycle_events_app_occurred",
        "application_lifecycle_events",
        ["application_id", "occurred_at"],
        schema="careerops",
    )

    # --- resume_versions ---
    op.create_table(
        "resume_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("file_reference", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("target_type", sa.Text(), server_default="general", nullable=False),
        sa.Column(
            "human_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "candidate_id", "version_number", name="uq_resume_versions_candidate_version"
        ),
        sa.CheckConstraint("version_number > 0", name="ck_resume_versions_version_number_positive"),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name="ck_resume_versions_content_hash_length"
        ),
        schema="careerops",
    )

    # --- application_packages ---
    op.create_table(
        "application_packages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "resume_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.resume_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cover_letter_text", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "answers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "claims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
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
            "approval_state IN ('draft', 'pending_review', 'approved', 'rejected')",
            name="ck_application_packages_approval_state_values",
        ),
        schema="careerops",
    )

    # --- follow_up_reminders ---
    op.create_table(
        "follow_up_reminders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), server_default="active", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
        sa.Column("cancelled_reason", sa.Text(), server_default="", nullable=False),
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
            "state IN ('active', 'snoozed', 'cancelled', 'completed')",
            name="ck_follow_up_reminders_state_values",
        ),
        schema="careerops",
    )

    op.create_index(
        "ix_follow_up_reminders_app_rule_active",
        "follow_up_reminders",
        ["application_id", "rule_version"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        schema="careerops",
    )

    # --- Grants ---
    _grant_for_api_role(
        "GRANT SELECT, INSERT ON careerops.contacts, "
        "careerops.applications, careerops.application_lifecycle_events, "
        "careerops.resume_versions, careerops.application_packages, "
        "careerops.follow_up_reminders TO careerops_api"
    )
    _grant_for_api_role(
        "GRANT UPDATE ON careerops.applications, careerops.follow_up_reminders TO careerops_api"
    )

    # --- Append-only guard ---
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_application_lifecycle_events_append_only "
            "BEFORE UPDATE OR DELETE ON careerops.application_lifecycle_events "
            "FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
        )
    )


def downgrade() -> None:
    _grant_for_api_role(
        "REVOKE ALL ON careerops.contacts, "
        "careerops.applications, careerops.application_lifecycle_events, "
        "careerops.resume_versions, careerops.application_packages, "
        "careerops.follow_up_reminders FROM careerops_api"
    )
    op.drop_index(
        "ix_follow_up_reminders_app_rule_active",
        table_name="follow_up_reminders",
        schema="careerops",
    )
    op.drop_table("follow_up_reminders", schema="careerops")
    op.drop_table("application_packages", schema="careerops")
    op.drop_table("resume_versions", schema="careerops")
    op.drop_index(
        "ix_application_lifecycle_events_app_occurred",
        table_name="application_lifecycle_events",
        schema="careerops",
    )
    op.drop_table("application_lifecycle_events", schema="careerops")
    op.drop_index("ix_applications_candidate_state", table_name="applications", schema="careerops")
    op.drop_table("applications", schema="careerops")
    op.drop_table("contacts", schema="careerops")
