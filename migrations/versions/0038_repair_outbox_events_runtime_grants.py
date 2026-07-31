"""repair runtime grants for outbox_events

Revision ID: 0038
Revises: 0037

The ``outbox_events`` table (the side-effect outbox, created back in revision
0030) was never granted to the runtime capability roles. The ``drain_outbox``
activity -- which the Phase 2 trigger loop now fires on a 30s Temporal schedule
via ``OutboxDrainWorkflow`` -- runs as ``careerops_runtime`` (a member of
``careerops_api``) and failed with ``permission denied for table outbox_events``
the first time it was ever invoked on a cadence. Migrations 0036/0037 repaired
runtime grants for the crawl-to-inbox and job-projection tables but missed this
one. Enqueue (INSERT) and claim/publish (SELECT, UPDATE) both run as the
runtime role, so all three privileges are granted here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement_template: str) -> None:
    """Apply the GRANT/REVOKE to each capability role that actually exists.

    Mirrors the 0036/0037 repair pattern: some environments bootstrapped the
    capability roles after migration 0001, so we guard each role rather than
    assuming it is present.
    """
    if op.get_context().as_sql:
        return
    bind = op.get_bind()
    for role in ("careerops_api", "careerops_worker"):
        if _role_exists(role):
            bind.execute(sa.text(statement_template.replace("__ROLE__", role)))


def upgrade() -> None:
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.outbox_events TO __ROLE__")


def downgrade() -> None:
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.outbox_events FROM __ROLE__")
