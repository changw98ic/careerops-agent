"""repair runtime grants for the real crawl-to-inbox pipeline

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement: str) -> None:
    if op.get_context().as_sql or _role_exists("careerops_api"):
        op.execute(sa.text(statement))


def upgrade() -> None:
    # Crawl source registration needs full row insertion plus the existing
    # bounded updates.  Re-granting is idempotent and repairs databases where
    # the base migration ran before the capability role existed.
    _apply(
        "GRANT SELECT, INSERT, UPDATE ON careerops.job_sources "
        "TO careerops_api"
    )
    # Migration 0017 created these tables but omitted runtime grants entirely.
    _apply(
        "GRANT SELECT, INSERT, UPDATE ON careerops.filter_decisions "
        "TO careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT, DELETE ON careerops.requirement_match_results "
        "TO careerops_api"
    )


def downgrade() -> None:
    _apply(
        "REVOKE SELECT, INSERT, DELETE ON careerops.requirement_match_results "
        "FROM careerops_api"
    )
    _apply(
        "REVOKE SELECT, INSERT, UPDATE ON careerops.filter_decisions "
        "FROM careerops_api"
    )
    _apply(
        "REVOKE UPDATE ON careerops.job_sources "
        "FROM careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT ON careerops.job_sources TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (state, verified_at, last_discovery_at, trust_status, "
        "terms_status, robots_status, adapter_version, enabled, last_run_at, "
        "last_run_metadata, executor_mode, updated_at) "
        "ON careerops.job_sources TO careerops_api"
    )
