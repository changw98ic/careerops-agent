"""e2e inbox filter decisions + requirement match results (Section 6)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-26

Additive migration: adds ``filter_decisions`` and ``requirement_match_results``
tables for persisting deterministic hard-filter outcomes and requirement-level
match results. No existing tables are modified; downgrade drops only the new
tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "filter_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.profile_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "blocking_reasons",
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "canonical_job_id",
            "candidate_id",
            "profile_version_id",
            name="uq_filter_decisions_job_candidate_profile",
        ),
        sa.CheckConstraint(
            "verdict IN ('recommended', 'excluded')",
            name="verdict_values",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blocking_reasons) = 'array'",
            name="blocking_reasons_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'object'",
            name="evidence_refs_object",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_filter_decisions_candidate_verdict",
        "filter_decisions",
        ["candidate_id", "verdict"],
        schema="careerops",
    )

    op.create_table(
        "requirement_match_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "filter_decision_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.filter_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_name", sa.Text(), nullable=False),
        sa.Column("match_level", sa.String(24), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column("model_version", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_level IN ('strong', 'partial', 'transferable', 'unsupported')",
            name="match_level_values",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        sa.CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="evidence_ids_array"),
        schema="careerops",
    )
    op.create_index(
        "ix_requirement_match_results_decision",
        "requirement_match_results",
        ["filter_decision_id"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_table("requirement_match_results", schema="careerops")
    op.drop_table("filter_decisions", schema="careerops")
