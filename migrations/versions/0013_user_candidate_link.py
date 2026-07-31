"""Link console_users to candidates.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-25

Adds a nullable ``candidate_id`` FK on ``console_users`` so that the
single-console-owner identity can be associated with a candidate profile.
The bootstrap flow now creates both rows in one transaction.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "console_users",
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.create_foreign_key(
        "fk_console_users_candidate_id_candidates",
        "console_users",
        "candidates",
        ["candidate_id"],
        ["id"],
        source_schema="careerops",
        referent_schema="careerops",
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # 0039 dropped the auth tables; downgrades from >=0039 must tolerate their
    # absence (both ops fail on a missing relation).
    if sa.inspect(op.get_bind()).has_table("console_users", schema="careerops"):
        op.drop_constraint(
            "fk_console_users_candidate_id_candidates",
            "console_users",
            schema="careerops",
            type_="foreignkey",
        )
        op.drop_column("console_users", "candidate_id", schema="careerops")
