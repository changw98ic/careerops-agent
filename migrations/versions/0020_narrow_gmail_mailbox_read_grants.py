"""narrow Gmail mailbox direct reads to due-count columns

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE_STATEMENTS = (
    "REVOKE SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs FROM careerops_mailbox",
    "GRANT SELECT (id, provider, status) ON careerops.gmail_accounts TO careerops_mailbox",
    "GRANT SELECT (gmail_account_id, status) ON careerops.gmail_sync_runs TO careerops_mailbox",
)

_DOWNGRADE_STATEMENTS = (
    "REVOKE SELECT (id, provider, status) ON careerops.gmail_accounts FROM careerops_mailbox",
    "REVOKE SELECT (gmail_account_id, status) ON careerops.gmail_sync_runs FROM careerops_mailbox",
    "GRANT SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs TO careerops_mailbox",
)


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
    _run_for_role("careerops_mailbox", _UPGRADE_STATEMENTS)


def downgrade() -> None:
    _run_for_role("careerops_mailbox", _DOWNGRADE_STATEMENTS)
