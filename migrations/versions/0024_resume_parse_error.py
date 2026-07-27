"""persist bounded resume parse errors

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-27

Parse failures are a normal, user-visible lifecycle outcome. Store only the
bounded sanitized reason; never persist raw resume bytes or provider output.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resume_versions",
        sa.Column("parse_error", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_column("resume_versions", "parse_error", schema="careerops")
