"""end-to-end career loop: database identity, candidate ownership, cycles.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-26

Additive foundation for the end-to-end-career-application-loop change
(Section 2). This migration is forward-only safe: it never drops, renames, or
narrows an existing object. Every new column is nullable or carries a safe
server default so existing rows remain valid; every new table is created
alongside the old projections.

Adds:
- 2.1 UNIQUE constraint on ``console_users.candidate_id`` so the single-user
  runtime enforces the user->candidate link at the DB. The column stays
  nullable (bootstrap creates both rows in one txn); once set it is unique.
- 2.2/2.3 ``profile_versions`` table with structured jsonb preference columns
  and ``jsonb_typeof`` shape guards that back service-level validation.
- 2.4 ``resume_versions`` lifecycle columns: parse_status, confirmation_status,
  source_reference, parsed_at, confirmed_at.
- 2.5 ``evidence_items`` resume-derived columns: source_span (bounded),
  extractor_version, confirmation_status, evidence_hash, resume_version_id.
- 2.6 ``application_cycles`` table (one active cycle per
  candidate/canonical_job via a partial unique index) + ``applications.cycle_id``.
- 2.7 ``applications`` channel/package/payload/provider linkage columns.
- 2.8 Least-privilege grants for the ``careerops_api`` runtime role and a real
  downgrade that drops only the objects this migration created.

The existing ``applications`` UQ(candidate_id, canonical_job_id) is INTENTIONALLY
kept as a safety net (design Decision 8); one-active-cycle is enforced at the
service layer in a later stage.

NOTE on constraint names: alembic's ``env.py`` binds ``target_metadata`` to the
schema.py MetaData, so the ``ck_%(table_name)s_%(constraint_name)s`` naming
convention is applied to every CheckConstraint. We therefore pass SHORT
constraint names here (e.g. ``"parse_status_values"``); the convention expands
them to ``ck_<table>_<short>`` exactly once, matching schema.py byte-for-byte
and staying well under PostgreSQL's 63-char identifier limit (long doubled
names get truncated + hashed, which would break ``drop_constraint``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
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

    Mirrors the pattern in 0005/0011: grants are skipped when the role does
    not exist (e.g. fresh dev DBs without the init script) so ``alembic
    upgrade head`` is portable; the statement is always rendered in
    ``--sql`` mode so the contract test can observe it.
    """
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _role_exists("careerops_api"):
        op.execute(sa.text(statement))


# ---------------------------------------------------------------------------
# New-table DDL helpers (kept inline to keep the migration self-contained)
# ---------------------------------------------------------------------------


def _create_profile_versions() -> None:
    op.create_table(
        "profile_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
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
            "target_roles",
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
            "compensation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("seniority", sa.Text(), server_default="", nullable=False),
        # ``authorization_rules`` (not ``authorization``): AUTHORIZATION is a
        # PostgreSQL keyword and unquoted use inside CHECK expressions is
        # ambiguous; renaming avoids the footgun and mirrors remote_rules /
        # hard_exclusions (all jsonb-rules objects).
        sa.Column(
            "authorization_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
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
            "hard_exclusions",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "candidate_id", "version", name="uq_profile_versions_candidate_version"
        ),
        # Shape guards backing service-level validation (task 2.3). Short names
        # expand to ck_profile_versions_<short> via the naming convention.
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("jsonb_typeof(target_roles) = 'array'", name="target_roles_array"),
        sa.CheckConstraint("jsonb_typeof(locations) = 'array'", name="locations_array"),
        sa.CheckConstraint("jsonb_typeof(remote_rules) = 'object'", name="remote_rules_object"),
        sa.CheckConstraint("jsonb_typeof(compensation) = 'object'", name="compensation_object"),
        sa.CheckConstraint(
            "jsonb_typeof(authorization_rules) = 'object'", name="authorization_rules_object"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(include_keywords) = 'array'", name="include_keywords_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exclude_keywords) = 'array'", name="exclude_keywords_array"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(hard_exclusions) = 'object'", name="hard_exclusions_object"
        ),
        # Compensation range guard: when both bounds are JSON numbers, min <= max.
        # Non-numeric or missing bounds defer to service-level validation.
        sa.CheckConstraint(
            "jsonb_typeof(compensation->'amount_min') IS DISTINCT FROM 'number' "
            "OR jsonb_typeof(compensation->'amount_max') IS DISTINCT FROM 'number' "
            "OR (compensation->'amount_min')::numeric <= (compensation->'amount_max')::numeric",
            name="compensation_range",
        ),
        schema="careerops",
    )
    # Exactly one active profile version per candidate (partial unique index).
    op.create_index(
        "ix_profile_versions_candidate_active",
        "profile_versions",
        ["candidate_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        schema="careerops",
    )


def _create_application_cycles() -> None:
    op.create_table(
        "application_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("prior_cycle_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        # Self-reference for re-application history. PostgreSQL handles inline
        # self-referencing FKs, so (unlike cross-table cycles) no use_alter is
        # needed — and use_alter=True silently dropped the constraint under
        # op.create_table, which broke alembic check.
        sa.ForeignKeyConstraint(
            ["prior_cycle_id"],
            ["careerops.application_cycles.id"],
            name="fk_application_cycles_prior_cycle",
            ondelete="SET NULL",
        ),
        # An active cycle has no closed_at; an inactive cycle must be closed.
        sa.CheckConstraint(
            "(active = true AND closed_at IS NULL) OR (active = false AND closed_at IS NOT NULL)",
            name="active_closed_consistent",
        ),
        schema="careerops",
    )
    # Exactly one active cycle per (candidate, canonical_job).
    op.create_index(
        "ix_application_cycles_candidate_job_active",
        "application_cycles",
        ["candidate_id", "canonical_job_id"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        schema="careerops",
    )


def upgrade() -> None:
    # --- 2.1 user->candidate uniqueness (nullable stays; singleton already
    # guarantees one row, this is defense in depth for the link itself).
    op.create_unique_constraint(
        "uq_console_users_candidate_id",
        "console_users",
        ["candidate_id"],
        schema="careerops",
    )

    # --- 2.2/2.3 profile versions + shape guards
    _create_profile_versions()

    # --- 2.4 resume_versions lifecycle columns (additive; defaults keep
    # existing rows valid: pending parse, unconfirmed).
    op.add_column(
        "resume_versions",
        sa.Column(
            "parse_status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "resume_versions",
        sa.Column(
            "confirmation_status",
            sa.String(16),
            server_default="unconfirmed",
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "resume_versions",
        sa.Column("source_reference", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "resume_versions",
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "resume_versions",
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        schema="careerops",
    )
    op.create_check_constraint(
        "parse_status_values",
        "resume_versions",
        "parse_status IN ('pending', 'parsed', 'failed')",
        schema="careerops",
    )
    op.create_check_constraint(
        "confirmation_status_values",
        "resume_versions",
        "confirmation_status IN ('unconfirmed', 'confirmed', 'rejected')",
        schema="careerops",
    )

    # --- 2.5 evidence_items resume-derived columns (additive; the existing
    # repository/commit/path idempotency path keeps its semantics, the new
    # source_span/extractor_version path serves resume-extracted claims).
    op.add_column(
        "evidence_items",
        sa.Column("extractor_version", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "evidence_items",
        sa.Column("source_span", sa.Text(), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "evidence_items",
        sa.Column(
            "confirmation_status",
            sa.String(16),
            server_default="unconfirmed",
            nullable=False,
        ),
        schema="careerops",
    )
    op.add_column(
        "evidence_items",
        sa.Column("evidence_hash", sa.String(64), server_default="", nullable=False),
        schema="careerops",
    )
    op.add_column(
        "evidence_items",
        sa.Column(
            "resume_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.resume_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="careerops",
    )
    op.create_check_constraint(
        "confirmation_status_values",
        "evidence_items",
        "confirmation_status IN ('unconfirmed', 'confirmed', 'rejected')",
        schema="careerops",
    )
    op.create_check_constraint(
        "source_span_bounded",
        "evidence_items",
        "char_length(source_span) <= 8192",
        schema="careerops",
    )
    op.create_check_constraint(
        "evidence_hash_length",
        "evidence_items",
        "evidence_hash = '' OR char_length(evidence_hash) = 64",
        schema="careerops",
    )

    # --- 2.6 application_cycles + applications.cycle_id
    _create_application_cycles()
    op.add_column(
        "applications",
        sa.Column(
            "cycle_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.application_cycles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="careerops",
    )

    # --- 2.7 applications channel / package version / payload / provider linkage
    op.add_column(
        "applications",
        sa.Column("submission_channel", sa.String(16), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "applications",
        sa.Column("package_version_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "applications",
        sa.Column("payload_hash", sa.String(64), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "applications",
        sa.Column("provider_kind", sa.String(24), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "applications",
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        schema="careerops",
    )
    op.create_check_constraint(
        "submission_channel_values",
        "applications",
        "submission_channel IS NULL OR submission_channel IN ('email', 'external_form', 'manual')",
        schema="careerops",
    )
    op.create_check_constraint(
        "payload_hash_length",
        "applications",
        "payload_hash IS NULL OR char_length(payload_hash) = 64",
        schema="careerops",
    )

    # --- 2.8 Least-privilege grants for the runtime role.
    # ``careerops_api`` is the role the runtime login (``careerops_runtime``)
    # assumes via SET ROLE; granting here makes the new tables usable under
    # the runtime role, not just superuser (iron rule 4).
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.profile_versions TO careerops_api")
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.application_cycles TO careerops_api")
    # console_users.candidate_id was added in 0013 without an INSERT/UPDATE
    # grant; the runtime needs write access to enforce server-side candidate
    # ownership (bootstrap links user->candidate in one txn).
    _apply("GRANT INSERT (candidate_id) ON careerops.console_users TO careerops_api")
    _apply("GRANT UPDATE (candidate_id) ON careerops.console_users TO careerops_api")


def downgrade() -> None:
    # Real downgrade: drop ONLY the objects this migration created. Existing
    # tables/columns/constraints are untouched (additive invariant). Check
    # constraints are dropped by their short name; the naming convention
    # expands identically to the upgrade path so the resolved DB name matches.
    _apply("REVOKE UPDATE (candidate_id) ON careerops.console_users FROM careerops_api")
    _apply("REVOKE INSERT (candidate_id) ON careerops.console_users FROM careerops_api")
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.application_cycles FROM careerops_api")
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.profile_versions FROM careerops_api")

    op.drop_constraint("payload_hash_length", "applications", schema="careerops", type_="check")
    op.drop_constraint(
        "submission_channel_values", "applications", schema="careerops", type_="check"
    )
    op.drop_column("applications", "provider_message_id", schema="careerops")
    op.drop_column("applications", "provider_kind", schema="careerops")
    op.drop_column("applications", "payload_hash", schema="careerops")
    op.drop_column("applications", "package_version_id", schema="careerops")
    op.drop_column("applications", "submission_channel", schema="careerops")
    op.drop_column("applications", "cycle_id", schema="careerops")

    op.drop_index(
        "ix_application_cycles_candidate_job_active",
        table_name="application_cycles",
        schema="careerops",
    )
    op.drop_table("application_cycles", schema="careerops")

    op.drop_constraint("evidence_hash_length", "evidence_items", schema="careerops", type_="check")
    op.drop_constraint("source_span_bounded", "evidence_items", schema="careerops", type_="check")
    op.drop_constraint(
        "confirmation_status_values",
        "evidence_items",
        schema="careerops",
        type_="check",
    )
    op.drop_column("evidence_items", "resume_version_id", schema="careerops")
    op.drop_column("evidence_items", "evidence_hash", schema="careerops")
    op.drop_column("evidence_items", "confirmation_status", schema="careerops")
    op.drop_column("evidence_items", "source_span", schema="careerops")
    op.drop_column("evidence_items", "extractor_version", schema="careerops")

    op.drop_constraint(
        "confirmation_status_values",
        "resume_versions",
        schema="careerops",
        type_="check",
    )
    op.drop_constraint("parse_status_values", "resume_versions", schema="careerops", type_="check")
    op.drop_column("resume_versions", "confirmed_at", schema="careerops")
    op.drop_column("resume_versions", "parsed_at", schema="careerops")
    op.drop_column("resume_versions", "source_reference", schema="careerops")
    op.drop_column("resume_versions", "confirmation_status", schema="careerops")
    op.drop_column("resume_versions", "parse_status", schema="careerops")

    op.drop_index(
        "ix_profile_versions_candidate_active",
        table_name="profile_versions",
        schema="careerops",
    )
    op.drop_table("profile_versions", schema="careerops")

    op.drop_constraint(
        "uq_console_users_candidate_id",
        "console_users",
        schema="careerops",
        type_="unique",
    )
