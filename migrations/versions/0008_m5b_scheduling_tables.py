"""M5B calendar scheduling: schedule_proposals, calendar_events, interview_records.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-22

M5B adds calendar scheduling and interview management tables:
- schedule_proposals: tracks the scheduling lifecycle from draft to confirmed,
  binding candidate slots, recruiter confirmation, user action, and the M5A
  authorization chain (action_intent_id, payload_hash).
- calendar_events: events created in the dedicated CareerOps Interviews calendar,
  with a unique reconciliation_key preventing duplicates.
- interview_records: deterministic interview records with evidence summary.
- conflict_reviews: race condition detection between FreeBusy and insert;
  confirmation_email_blocked is always true (safety invariant).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "careerops"


def upgrade() -> None:
    # -- schedule_proposals ------------------------------------------------
    op.create_table(
        "schedule_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_slots",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("selected_slot", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("rules_version", sa.Text(), nullable=False),
        sa.Column(
            "scheduling_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "calendar_ids_checked",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("recruiter_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("user_action_at", sa.DateTime(timezone=True)),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.action_intents.id", ondelete="SET NULL"),
        ),
        sa.Column("payload_hash", sa.String(64)),
        sa.Column("freebusy_queried_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conflict_reason", sa.Text()),
        sa.Column("created_by", sa.Text(), nullable=False),
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
            "status IN ('draft', 'pending_recruiter', 'recruiter_confirmed', "
            "'pending_user_action', 'executing', 'confirmed', 'conflict_review', "
            "'cancelled', 'expired')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "payload_hash IS NULL OR char_length(payload_hash) = 64",
            name="payload_hash_length",
        ),
        sa.CheckConstraint(
            "(status = 'confirmed' AND action_intent_id IS NOT NULL) OR status <> 'confirmed'",
            name="confirmed_requires_intent",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_schedule_proposals_application_status",
        "schedule_proposals",
        ["application_id", "status"],
        schema=SCHEMA,
    )

    # -- calendar_events ---------------------------------------------------
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "schedule_proposal_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.action_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_event_id", sa.Text()),
        sa.Column("calendar_id", sa.Text(), nullable=False),
        sa.Column("reconciliation_key", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "slot_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("meeting_link", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("conflict_detected_at", sa.DateTime(timezone=True)),
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
            "status IN ('pending', 'created', 'conflict_detected', 'cancelled')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "(status = 'conflict_detected' AND conflict_detected_at IS NOT NULL) OR "
            "status <> 'conflict_detected'",
            name="conflict_timestamp_consistent",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_calendar_events_proposal_status",
        "calendar_events",
        ["schedule_proposal_id", "status"],
        schema=SCHEMA,
    )

    # -- interview_records -------------------------------------------------
    op.create_table(
        "interview_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "application_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.applications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "calendar_event_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.calendar_events.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "schedule_proposal_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), server_default="scheduled", nullable=False),
        sa.Column("interviewer_name", sa.Text(), server_default="", nullable=False),
        sa.Column("interviewer_email", sa.Text(), server_default="", nullable=False),
        sa.Column("interview_type", sa.Text(), server_default="video", nullable=False),
        sa.Column("meeting_link", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "evidence_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
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
        sa.CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'rescheduled')",
            name="status_values",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_interview_records_application_status",
        "interview_records",
        ["application_id", "status"],
        schema=SCHEMA,
    )

    # -- conflict_reviews --------------------------------------------------
    op.create_table(
        "conflict_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "schedule_proposal_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "calendar_event_id",
            sa.Uuid(),
            sa.ForeignKey(f"{SCHEMA}.calendar_events.id", ondelete="SET NULL"),
        ),
        sa.Column("conflict_type", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text()),
        sa.Column(
            "confirmation_email_blocked",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "conflict_type IN ('freebusy_race', 'duplicate_event', 'buffer_violation', "
            "'reconciliation_failure')",
            name="conflict_type_values",
        ),
        sa.CheckConstraint(
            "confirmation_email_blocked = true",
            name="confirmation_email_always_blocked",
        ),
        sa.CheckConstraint(
            "(resolved_at IS NOT NULL AND resolution IS NOT NULL) OR "
            "(resolved_at IS NULL AND resolution IS NULL)",
            name="resolution_consistent",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_conflict_reviews_proposal_unresolved",
        "conflict_reviews",
        ["schedule_proposal_id"],
        schema=SCHEMA,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("conflict_reviews", schema=SCHEMA)
    op.drop_table("interview_records", schema=SCHEMA)
    op.drop_table("calendar_events", schema=SCHEMA)
    op.drop_table("schedule_proposals", schema=SCHEMA)
