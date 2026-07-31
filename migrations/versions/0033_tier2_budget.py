"""tier2_budget: daily counter + lease slots for Tier 2 browser budget.

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tier2_budget_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column(
            "consumed",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint("consumed >= 0", name="consumed_nonnegative"),
        schema="careerops",
    )
    op.create_table(
        "tier2_budget_leases",
        sa.Column("lease_id", sa.String(128), primary_key=True),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_tier2_budget_leases_source",
        "tier2_budget_leases",
        ["source_id"],
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_index("ix_tier2_budget_leases_source", table_name="tier2_budget_leases", schema="careerops")
    op.drop_table("tier2_budget_leases", schema="careerops")
    op.drop_table("tier2_budget_daily", schema="careerops")
