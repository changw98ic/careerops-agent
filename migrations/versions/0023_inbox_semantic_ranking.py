"""persist review-only inbox semantic ranking metadata

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-27

The deterministic filter remains authoritative. These nullable/bounded fields
only persist the optional, review-only semantic ranking result so the UI and
trace can distinguish disabled, unavailable, and available outcomes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "filter_decisions",
        sa.Column(
            "semantic_ranking_status",
            sa.String(16),
            server_default="unavailable",
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "filter_decisions",
        sa.Column("semantic_ranking_score", sa.Numeric(5, 4), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "filter_decisions",
        sa.Column("semantic_ranking_reason", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "filter_decisions",
        sa.Column("semantic_model_version", sa.String(128), server_default="", nullable=False),
        schema="careerops",
    )
    op.create_check_constraint(
        "semantic_ranking_status_values",
        "filter_decisions",
        "semantic_ranking_status IN ('available', 'unavailable', 'disabled')",
        schema="careerops",
    )
    op.create_check_constraint(
        "semantic_ranking_score_range",
        "filter_decisions",
        "semantic_ranking_score IS NULL OR "
        "(semantic_ranking_score >= 0 AND semantic_ranking_score <= 1)",
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_constraint("semantic_ranking_score_range", "filter_decisions", schema="careerops")
    op.drop_constraint("semantic_ranking_status_values", "filter_decisions", schema="careerops")
    op.drop_column("filter_decisions", "semantic_model_version", schema="careerops")
    op.drop_column("filter_decisions", "semantic_ranking_reason", schema="careerops")
    op.drop_column("filter_decisions", "semantic_ranking_score", schema="careerops")
    op.drop_column("filter_decisions", "semantic_ranking_status", schema="careerops")
