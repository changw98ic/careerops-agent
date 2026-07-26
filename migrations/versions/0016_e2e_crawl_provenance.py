"""end-to-end career loop Section 5: crawl provenance columns on job_posting_versions.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-26

Additive migration for the crawl execution provenance (tasks 5.1, 5.6).
Forward-only safe: never drops, renames, or narrows an existing object. Both
new columns are nullable so existing rows remain valid.

Adds:
- ``job_posting_versions.crawl_run_id`` nullable FK -> ``careerops.crawl_runs.id``
  (SET NULL on delete). Links each ingested version to the crawl run that
  discovered it. Nullable for existing rows (pre-Section-5 data).
- ``job_posting_versions.plan_version_id`` nullable FK ->
  ``careerops.crawl_plan_versions.id`` (SET NULL on delete). Links each
  ingested version to the plan-version snapshot that governed the run.
  Nullable for existing rows.
- Least-privilege SELECT grant for the ``careerops_api`` runtime role on the
  new columns (read for provenance queries). No UPDATE/INSERT grant — the
  columns are set by the execution service at ingest time, not by arbitrary
  API callers.
- Real downgrade that drops only the two FK constraints + columns.

Iron Rules honored:
- 1 (additive migration): columns are nullable; existing data unaffected.
- 2 (provenance): every ingested version records crawl_run_id + plan_version_id.
- 8 (additive): no existing columns/constraints modified.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement: str) -> None:
    """Execute a role grant/revoke, honoring offline SQL rendering."""
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _role_exists("careerops_api"):
        op.execute(sa.text(statement))


def upgrade() -> None:
    # --- Provenance columns on job_posting_versions (tasks 5.1, 5.6).
    # Both nullable so existing rows (pre-Section-5) remain valid.
    # FK uses SET NULL on delete so a deleted run/plan does not cascade
    # into posting version loss.
    op.add_column(
        "job_posting_versions",
        sa.Column("crawl_run_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "job_posting_versions",
        sa.Column("plan_version_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.create_foreign_key(
        "fk_job_posting_versions_crawl_run_id_crawl_runs",
        "job_posting_versions",
        "crawl_runs",
        ["crawl_run_id"],
        ["id"],
        ondelete="SET NULL",
        source_schema="careerops",
        referent_schema="careerops",
    )
    op.create_foreign_key(
        "fk_job_posting_versions_plan_version_id_crawl_plan_versions",
        "job_posting_versions",
        "crawl_plan_versions",
        ["plan_version_id"],
        ["id"],
        ondelete="SET NULL",
        source_schema="careerops",
        referent_schema="careerops",
    )

    # --- Least-privilege grants (Iron Rule 1).
    # SELECT only — the execution service sets these at ingest time; the API
    # layer reads them for provenance queries but should not write them
    # directly.
    _apply(
        "GRANT SELECT (crawl_run_id, plan_version_id)"
        " ON careerops.job_posting_versions TO careerops_api"
    )


def downgrade() -> None:
    # Real downgrade: drop only the objects this migration created.
    _apply(
        "REVOKE SELECT (crawl_run_id, plan_version_id)"
        " ON careerops.job_posting_versions FROM careerops_api"
    )
    op.drop_constraint(
        "fk_job_posting_versions_plan_version_id_crawl_plan_versions",
        "job_posting_versions",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_job_posting_versions_crawl_run_id_crawl_runs",
        "job_posting_versions",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_column("job_posting_versions", "plan_version_id", schema="careerops")
    op.drop_column("job_posting_versions", "crawl_run_id", schema="careerops")
