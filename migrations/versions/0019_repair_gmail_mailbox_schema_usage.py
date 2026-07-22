"""repair Gmail mailbox capability schema usage

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE_GRANTS = ("GRANT USAGE ON SCHEMA careerops TO careerops_mailbox",)

_DOWNGRADE_REVOKES = ("REVOKE USAGE ON SCHEMA careerops FROM careerops_mailbox",)


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _run_for_role(role_name: str, statements: tuple[str, ...]) -> None:
    if op.get_context().as_sql:
        for statement in statements:
            op.execute(sa.text(statement))
        return
    if _role_exists(role_name):
        for statement in statements:
            op.execute(sa.text(statement))


def upgrade() -> None:
    _run_for_role("careerops_mailbox", _UPGRADE_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_mailbox", _DOWNGRADE_REVOKES)
