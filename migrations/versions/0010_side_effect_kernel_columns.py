"""Side-effect kernel: add policy_decisions columns for PostgresSideEffectStore.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-24

The in-memory store held ``trusted_facts``, ``evidence_refs``, and
``untrusted_claims`` as transient fields. The PostgreSQL store persists them
on ``policy_decisions`` so the full authorization chain is durable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "policy_decisions",
        sa.Column(
            "trusted_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "policy_decisions",
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "policy_decisions",
        sa.Column(
            "untrusted_claims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="careerops",
    )


def downgrade() -> None:
    op.drop_column("policy_decisions", "untrusted_claims", schema="careerops")
    op.drop_column("policy_decisions", "evidence_refs", schema="careerops")
    op.drop_column("policy_decisions", "trusted_facts", schema="careerops")
