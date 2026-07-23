"""add M2 matching tables: evidence_items, match_results, compensation_records

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
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
    op.create_table(
        "evidence_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("repository", sa.Text(), server_default="", nullable=False),
        sa.Column("commit_sha", sa.Text(), server_default="", nullable=False),
        sa.Column("path", sa.Text(), server_default="", nullable=False),
        sa.Column("symbol", sa.Text(), server_default="", nullable=False),
        sa.Column("content_hash", sa.String(64), server_default="", nullable=False),
        sa.Column("source_url", sa.Text(), server_default="", nullable=False),
        sa.Column("verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('skill', 'project', 'experience', 'certification', 'education')",
            name="ck_evidence_items_kind_values",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "repository",
            "commit_sha",
            "path",
            "symbol",
            "content_hash",
            name="uq_evidence_items_idempotency",
        ),
        schema="careerops",
    )

    op.create_table(
        "match_results",
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
        sa.Column("tier", sa.String(32), nullable=False),
        sa.Column("overall_score", sa.Numeric(5, 4), nullable=False),
        sa.Column(
            "requirement_matches",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "geographic_blocked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("remote_verdict", sa.String(32), server_default="unknown", nullable=False),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column("input_hash", sa.String(64), server_default="", nullable=False),
        sa.Column("output_hash", sa.String(64), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tier IN ('apply_now', 'strong_candidate', 'worth_exploring', "
            "'stretch', 'not_recommended', 'blocked')",
            name="ck_match_results_tier_values",
        ),
        sa.CheckConstraint(
            "remote_verdict IN ('eligible', 'not_eligible', 'review_required', 'unknown')",
            name="ck_match_results_remote_verdict_values",
        ),
        sa.CheckConstraint(
            "overall_score >= 0 AND overall_score <= 1",
            name="ck_match_results_score_range",
        ),
        schema="careerops",
    )

    op.create_index(
        "ix_match_results_candidate_job",
        "match_results",
        ["candidate_id", "canonical_job_id"],
        schema="careerops",
    )

    op.create_table(
        "compensation_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(8), server_default="", nullable=False),
        sa.Column("amount_min", sa.Numeric(14, 2)),
        sa.Column("amount_max", sa.Numeric(14, 2)),
        sa.Column("period", sa.String(16), server_default="", nullable=False),
        sa.Column("fx_rate", sa.Numeric(14, 8)),
        sa.Column("fx_effective_date", sa.Date()),
        sa.Column("normalized_amount_min", sa.Numeric(14, 2)),
        sa.Column("normalized_amount_max", sa.Numeric(14, 2)),
        sa.Column("normalized_currency", sa.String(8), server_default="CNY", nullable=False),
        sa.Column("score", sa.String(24), server_default="unknown", nullable=False),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "score IN ('unknown', 'high', 'competitive', 'moderate', 'below_market', 'neutral')",
            name="ck_compensation_records_score_values",
        ),
        schema="careerops",
    )

    _grant_for_api_role(
        "GRANT SELECT, INSERT ON careerops.evidence_items, "
        "careerops.match_results, careerops.compensation_records TO careerops_api"
    )


def downgrade() -> None:
    _grant_for_api_role(
        "REVOKE ALL ON careerops.evidence_items, "
        "careerops.match_results, careerops.compensation_records FROM careerops_api"
    )
    op.drop_table("compensation_records", schema="careerops")
    op.drop_index("ix_match_results_candidate_job", table_name="match_results", schema="careerops")
    op.drop_table("match_results", schema="careerops")
    op.drop_table("evidence_items", schema="careerops")
