"""add session_ref to crawl_source_permissions

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawl_source_permissions",
        sa.Column("session_ref", sa.Text(), nullable=True),
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_column(
        "crawl_source_permissions",
        "session_ref",
        schema="careerops",
    )
