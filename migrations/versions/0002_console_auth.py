"""add single-user console authentication state

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_API_GRANTS = (
    "GRANT SELECT ON careerops.console_users, careerops.bootstrap_tokens, "
    "careerops.console_sessions TO careerops_api",
    "GRANT INSERT (id, username, password_hash, password_algorithm, password_parameters, "
    "password_changed_at, created_at, updated_at) ON careerops.console_users TO careerops_api",
    "GRANT INSERT (id, token_hash, issued_at, expires_at, created_at) "
    "ON careerops.bootstrap_tokens TO careerops_api",
    "GRANT INSERT (id, user_id, state, token_hash, csrf_token_hash, client_fingerprint, "
    "created_at, last_seen_at, idle_expires_at, absolute_expires_at) "
    "ON careerops.console_sessions TO careerops_api",
    "GRANT UPDATE (password_hash, password_algorithm, password_parameters, "
    "password_changed_at, disabled_at, updated_at) "
    "ON careerops.console_users TO careerops_api",
    "GRANT UPDATE (used_at, used_by_user_id, revoked_at) "
    "ON careerops.bootstrap_tokens TO careerops_api",
    "GRANT UPDATE (state, last_seen_at, idle_expires_at, revoked_at, "
    "rotated_to_session_id) ON careerops.console_sessions TO careerops_api",
)
_API_REVOKES = (
    ("REVOKE ALL ON careerops.console_sessions FROM careerops_api", "console_sessions"),
    ("REVOKE ALL ON careerops.bootstrap_tokens FROM careerops_api", "bootstrap_tokens"),
    ("REVOKE ALL ON careerops.console_users FROM careerops_api", "console_users"),
)


def _api_role_exists() -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api')")
        )
    )


def _grant_api_auth_capability() -> None:
    if op.get_context().as_sql:
        for statement in _API_GRANTS:
            op.execute(sa.text(statement))
        return
    if _api_role_exists():
        for statement in _API_GRANTS:
            op.execute(sa.text(statement))


def _revoke_api_auth_capability() -> None:
    if op.get_context().as_sql:
        for statement, _table in _API_REVOKES:
            op.execute(sa.text(statement))
        return
    if _api_role_exists():
        # 0039 dropped the auth tables; downgrades to <0039 must tolerate
        # their absence (REVOKE on a missing relation fails).
        for statement, table in _API_REVOKES:
            if sa.inspect(op.get_bind()).has_table(table, schema="careerops"):
                op.execute(sa.text(statement))


def upgrade() -> None:
    op.create_table(
        "console_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton_key", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "password_algorithm", sa.String(length=16), server_default="argon2id", nullable=False
        ),
        sa.Column(
            "password_parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("singleton_key = 1", name="ck_console_users_singleton_key_value"),
        sa.CheckConstraint(
            "username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
            name="ck_console_users_username_canonical",
        ),
        sa.CheckConstraint(
            "password_algorithm = 'argon2id'",
            name="ck_console_users_password_algorithm_value",
        ),
        sa.CheckConstraint(
            "password_hash LIKE '$argon2id$%'",
            name="ck_console_users_password_hash_format",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_console_users"),
        sa.UniqueConstraint("singleton_key", name="uq_console_users_singleton_key"),
        sa.UniqueConstraint("username", name="uq_console_users_username"),
        schema="careerops",
    )
    op.create_table(
        "bootstrap_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_bootstrap_tokens_token_hash_format",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_bootstrap_tokens_expiry_after_issue",
        ),
        sa.CheckConstraint(
            "(used_at IS NULL AND used_by_user_id IS NULL) OR "
            "(used_at IS NOT NULL AND used_by_user_id IS NOT NULL)",
            name="ck_bootstrap_tokens_use_identity_consistent",
        ),
        sa.CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_bootstrap_tokens_use_or_revoke",
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR (used_at >= issued_at AND used_at < expires_at)",
            name="ck_bootstrap_tokens_use_within_lifetime",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_bootstrap_tokens_revocation_after_issue",
        ),
        sa.ForeignKeyConstraint(
            ["used_by_user_id"],
            ["careerops.console_users.id"],
            name="fk_bootstrap_tokens_used_by_user_id_console_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bootstrap_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_bootstrap_tokens_token_hash"),
        schema="careerops",
    )
    op.create_index(
        "ix_bootstrap_tokens_active_expiry",
        "bootstrap_tokens",
        ["expires_at"],
        unique=False,
        schema="careerops",
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_table(
        "console_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("client_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_to_session_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "state IN ('preauth', 'authenticated', 'revoked')",
            name="ck_console_sessions_state_values",
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_console_sessions_token_hash_format",
        ),
        sa.CheckConstraint(
            "csrf_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_console_sessions_csrf_hash_format",
        ),
        sa.CheckConstraint(
            "client_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_console_sessions_client_fingerprint_format",
        ),
        sa.CheckConstraint(
            "(state = 'preauth' AND user_id IS NULL AND revoked_at IS NULL "
            "AND rotated_to_session_id IS NULL) OR "
            "(state = 'authenticated' AND user_id IS NOT NULL AND revoked_at IS NULL "
            "AND rotated_to_session_id IS NULL) OR "
            "(state = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_console_sessions_state_consistent",
        ),
        sa.CheckConstraint(
            "created_at <= last_seen_at AND last_seen_at < idle_expires_at "
            "AND idle_expires_at <= absolute_expires_at",
            name="ck_console_sessions_lifetime_ordered",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_console_sessions_revocation_after_creation",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["careerops.console_users.id"],
            name="fk_console_sessions_user_id_console_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_to_session_id"],
            ["careerops.console_sessions.id"],
            name="fk_console_sessions_rotated_to_session",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_console_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_console_sessions_token_hash"),
        schema="careerops",
    )
    op.create_index(
        "ix_console_sessions_user_state",
        "console_sessions",
        ["user_id", "state"],
        unique=False,
        schema="careerops",
    )
    op.create_index(
        "ix_console_sessions_expiry",
        "console_sessions",
        ["idle_expires_at", "absolute_expires_at"],
        unique=False,
        schema="careerops",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    _grant_api_auth_capability()


def downgrade() -> None:
    _revoke_api_auth_capability()
    # if_exists: 0039 dropped these tables, so downgrades from >=0039 must
    # tolerate the indexes/tables already being gone.
    op.drop_index(
        "ix_console_sessions_expiry",
        table_name="console_sessions",
        schema="careerops",
        if_exists=True,
    )
    op.drop_index(
        "ix_console_sessions_user_state",
        table_name="console_sessions",
        schema="careerops",
        if_exists=True,
    )
    op.drop_table("console_sessions", schema="careerops", if_exists=True)
    op.drop_index(
        "ix_bootstrap_tokens_active_expiry",
        table_name="bootstrap_tokens",
        schema="careerops",
        if_exists=True,
    )
    op.drop_table("bootstrap_tokens", schema="careerops", if_exists=True)
    op.drop_table("console_users", schema="careerops", if_exists=True)
