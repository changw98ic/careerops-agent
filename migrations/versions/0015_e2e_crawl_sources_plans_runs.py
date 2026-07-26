"""end-to-end career loop Section 4: crawl sources control + versioned plans + runs.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-26

Additive migration for the crawl-plan-management spec (tasks 4.1-4.4). Forward-
only safe: never drops, renames, or narrows an existing object. Every new
column is nullable or carries a safe server default so existing rows remain
valid; every new table is created alongside the old projections.

Adds:
- 4.2/4.3 ``job_sources`` control columns: trust_status / terms_status /
  robots_status / adapter_version / enabled / last_run_at / last_run_metadata.
  Sources DECLARE SCOPE ONLY; the crawl policy layer in ``crawl_policy.py``
  stays authoritative and fails closed on ``blocked`` (Iron Rule 6).
- 4.4 ``crawl_plan_versions`` table: immutable copy-on-write plan snapshots
  with structured jsonb preference fields, schedule (bounded interval_seconds +
  IANA timezone) and per-run limits. Partial unique index enforces one active
  version per owner (design Decision 2).
- 4.1 ``crawl_runs`` table: run records bound to a plan-version snapshot +
  source set + run identity. ``run_identity`` uniqueness is the idempotency
  contract (Iron Rule 4). State CHECK mirrors ``CrawlRunState`` values.
- Least-privilege grants for the ``careerops_api`` runtime role and a real
  downgrade that drops only the objects this migration created.

NOTE on constraint names: alembic's ``env.py`` binds ``target_metadata`` to the
schema.py MetaData, so the ``ck_%(table_name)s_%(constraint_name)s`` naming
convention is applied to every CheckConstraint. We therefore pass SHORT
constraint names here (e.g. ``"trust_status_values"``); the convention expands
them to ``ck_<table>_<short>`` exactly once, matching schema.py byte-for-byte
and staying well under PostgreSQL's 63-char identifier limit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
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
    """Execute a role grant/revoke, honoring offline SQL rendering.

    Mirrors the pattern in 0014: grants are skipped when the role does not
    exist (e.g. fresh dev DBs without the init script) so ``alembic upgrade
    head`` is portable; the statement is always rendered in ``--sql`` mode so
    the contract test can observe it.
    """
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _role_exists("careerops_api"):
        op.execute(sa.text(statement))


# ---------------------------------------------------------------------------
# New-table DDL helpers (kept inline to keep the migration self-contained)
# ---------------------------------------------------------------------------


def _create_crawl_plan_versions() -> None:
    op.create_table(
        "crawl_plan_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "themes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "include_keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exclude_keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "role_families",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "locations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "remote_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "seniority",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "compensation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("content_scope", sa.Text(), server_default="", nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column(
            "per_run_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("owner_id", "version", name="uq_crawl_plan_versions_owner_version"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("interval_seconds >= 0", name="interval_seconds_nonnegative"),
        sa.CheckConstraint("jsonb_typeof(sources) = 'array'", name="sources_array"),
        sa.CheckConstraint("jsonb_typeof(themes) = 'array'", name="themes_array"),
        sa.CheckConstraint(
            "jsonb_typeof(include_keywords) = 'array'", name="include_keywords_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exclude_keywords) = 'array'", name="exclude_keywords_array"
        ),
        sa.CheckConstraint("jsonb_typeof(role_families) = 'array'", name="role_families_array"),
        sa.CheckConstraint("jsonb_typeof(locations) = 'array'", name="locations_array"),
        sa.CheckConstraint("jsonb_typeof(remote_rules) = 'object'", name="remote_rules_object"),
        sa.CheckConstraint("jsonb_typeof(seniority) = 'array'", name="seniority_array"),
        sa.CheckConstraint("jsonb_typeof(compensation) = 'object'", name="compensation_object"),
        sa.CheckConstraint("jsonb_typeof(per_run_limits) = 'object'", name="per_run_limits_object"),
        schema="careerops",
    )
    # Exactly one active plan version per owner.
    op.create_index(
        "ix_crawl_plan_versions_owner_active",
        "crawl_plan_versions",
        ["owner_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        schema="careerops",
    )


def _create_crawl_runs() -> None:
    op.create_table(
        "crawl_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawl_plan_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_identity", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "source_set",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(24),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_category", sa.Text(), server_default="", nullable=False),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'timeout')",
            name="state_values",
        ),
        sa.CheckConstraint("jsonb_typeof(source_set) = 'array'", name="source_set_array"),
        sa.CheckConstraint("jsonb_typeof(limits) = 'object'", name="limits_object"),
        sa.CheckConstraint("jsonb_typeof(counters) = 'object'", name="counters_object"),
        schema="careerops",
    )
    op.create_index(
        "ix_crawl_runs_plan_version_state",
        "crawl_runs",
        ["plan_version_id", "state"],
        schema="careerops",
    )
    op.create_index(
        "ix_crawl_runs_created_at",
        "crawl_runs",
        ["created_at"],
        schema="careerops",
    )


def upgrade() -> None:
    # --- 4.2/4.3 job_sources additive control columns. Existing rows pick up
    # the safe server defaults (unknown status, disabled, empty metadata) so
    # the migration never narrows an existing row.
    op.add_column(
        "job_sources",
        sa.Column("trust_status", sa.String(16), server_default="unknown", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column("terms_status", sa.String(16), server_default="unknown", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column("robots_status", sa.String(16), server_default="unknown", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column("adapter_version", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "job_sources",
        sa.Column(
            "last_run_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="careerops",
    )
    op.create_check_constraint(
        "trust_status_values",
        "job_sources",
        "trust_status IN ('unknown', 'allowed', 'blocked')",
        schema="careerops",
    )
    op.create_check_constraint(
        "terms_status_values",
        "job_sources",
        "terms_status IN ('unknown', 'allowed', 'blocked')",
        schema="careerops",
    )
    op.create_check_constraint(
        "robots_status_values",
        "job_sources",
        "robots_status IN ('unknown', 'allowed', 'blocked')",
        schema="careerops",
    )

    # --- 4.4 crawl_plan_versions + 4.1 crawl_runs
    _create_crawl_plan_versions()
    _create_crawl_runs()

    # --- Least-privilege grants for the runtime role (Iron Rule 1).
    # ``careerops_api`` is the role the runtime login assumes via SET ROLE;
    # granting here makes the new tables/columns usable under the runtime
    # role, not just superuser. job_sources gains UPDATE on the new control
    # columns + SELECT (it was already readable but UPDATE on the control
    # columns is new in this migration).
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.crawl_plan_versions TO careerops_api")
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.crawl_runs TO careerops_api")
    _apply(
        "GRANT UPDATE (trust_status, terms_status, robots_status, adapter_version, "
        "enabled, last_run_at, last_run_metadata)"
        " ON careerops.job_sources TO careerops_api"
    )


def downgrade() -> None:
    # Real downgrade: drop ONLY the objects this migration created. Existing
    # tables/columns/constraints are untouched (additive invariant). Check
    # constraints are dropped by their short name; the naming convention
    # expands identically to the upgrade path so the resolved DB name matches.
    _apply(
        "REVOKE UPDATE (trust_status, terms_status, robots_status, adapter_version, "
        "enabled, last_run_at, last_run_metadata)"
        " ON careerops.job_sources FROM careerops_api"
    )
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.crawl_runs FROM careerops_api")
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.crawl_plan_versions FROM careerops_api")

    op.drop_index("ix_crawl_runs_created_at", table_name="crawl_runs", schema="careerops")
    op.drop_index("ix_crawl_runs_plan_version_state", table_name="crawl_runs", schema="careerops")
    op.drop_table("crawl_runs", schema="careerops")

    op.drop_index(
        "ix_crawl_plan_versions_owner_active",
        table_name="crawl_plan_versions",
        schema="careerops",
    )
    op.drop_table("crawl_plan_versions", schema="careerops")

    op.drop_constraint("robots_status_values", "job_sources", schema="careerops", type_="check")
    op.drop_constraint("terms_status_values", "job_sources", schema="careerops", type_="check")
    op.drop_constraint("trust_status_values", "job_sources", schema="careerops", type_="check")
    op.drop_column("job_sources", "last_run_metadata", schema="careerops")
    op.drop_column("job_sources", "last_run_at", schema="careerops")
    op.drop_column("job_sources", "enabled", schema="careerops")
    op.drop_column("job_sources", "adapter_version", schema="careerops")
    op.drop_column("job_sources", "robots_status", schema="careerops")
    op.drop_column("job_sources", "terms_status", schema="careerops")
    op.drop_column("job_sources", "trust_status", schema="careerops")
