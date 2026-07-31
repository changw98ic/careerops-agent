"""repair runtime grants for canonical job ingestion and projection

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
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
    # Repair base-table permissions for databases whose capability roles were
    # bootstrapped after migration 0001. All grants are limited to operations
    # used by the canonical crawl -> match -> inbox path.
    _apply(
        "GRANT SELECT, INSERT, UPDATE ON careerops.candidates, "
        "careerops.companies TO careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT, UPDATE ON careerops.job_sources, "
        "careerops.job_postings, careerops.canonical_jobs, "
        "careerops.job_posting_assignments TO careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT ON careerops.job_posting_versions, "
        "careerops.job_merge_decisions, careerops.match_results "
        "TO careerops_api"
    )
    _apply(
        "GRANT SELECT ON careerops.evidence_items, careerops.profile_versions "
        "TO careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT, UPDATE ON careerops.crawl_plan_versions, "
        "careerops.crawl_runs, careerops.filter_decisions TO careerops_api"
    )
    _apply(
        "GRANT SELECT, INSERT, DELETE ON careerops.requirement_match_results "
        "TO careerops_api"
    )


def downgrade() -> None:
    # These statements restore the intended grants from their creating
    # migrations instead of removing permissions that predate this repair.
    _apply(
        "REVOKE UPDATE ON careerops.candidates, careerops.companies, "
        "careerops.job_sources, careerops.job_postings, "
        "careerops.canonical_jobs, careerops.job_posting_assignments "
        "FROM careerops_api"
    )
    _apply(
        "GRANT UPDATE (display_name, updated_at) ON careerops.candidates "
        "TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (name, official_domains, terms_status, updated_at) "
        "ON careerops.companies TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (state, verified_at, last_discovery_at, trust_status, "
        "terms_status, robots_status, adapter_version, enabled, last_run_at, "
        "last_run_metadata, executor_mode, updated_at) "
        "ON careerops.job_sources TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (source_state, last_seen_at, closed_at, updated_at) "
        "ON careerops.job_postings TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (aggregate_state, primary_posting_id, updated_at) "
        "ON careerops.canonical_jobs TO careerops_api"
    )
    _apply(
        "GRANT UPDATE (canonical_job_id, decision_id, assigned_at) "
        "ON careerops.job_posting_assignments TO careerops_api"
    )
