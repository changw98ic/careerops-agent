"""LangGraph checkpointer: create ``langgraph`` schema + grant.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-24

PostgresSaver needs its own schema for the checkpoint tables. We create a
dedicated ``langgraph`` schema and grant the runtime role usage + DDL so
``PostgresSaver.setup()`` can install its tables at application startup.
The tables themselves are created by the checkpointer library's ``setup()``
method; this migration only provisions the schema and grants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LANGGRAPH_GRANTS: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA langgraph TO careerops_api",
    "GRANT CREATE ON SCHEMA langgraph TO careerops_api",
)


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement: str) -> None:
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _role_exists("careerops_api"):
        op.execute(sa.text(statement))


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA IF NOT EXISTS langgraph"))
    for statement in _LANGGRAPH_GRANTS:
        _apply(statement)


def downgrade() -> None:
    if _role_exists("careerops_api"):
        op.execute(sa.text("REVOKE CREATE ON SCHEMA langgraph FROM careerops_api"))
        op.execute(sa.text("REVOKE USAGE ON SCHEMA langgraph FROM careerops_api"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS langgraph CASCADE"))
