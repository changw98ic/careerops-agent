"""Gmail send receipt store: durable provider receipts.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-24

Adds the ``gmail_send_receipts`` table so that ``GmailSideEffectProvider``
can persist its reconciliation-keyed receipt map across process restarts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "careerops.gmail_send_receipts"


def upgrade() -> None:
    op.create_table(
        "gmail_send_receipts",
        sa.Column("reconciliation_key", sa.Text(), primary_key=True),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_table("gmail_send_receipts", schema="careerops")
