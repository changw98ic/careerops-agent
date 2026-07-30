# ruff: noqa: E501
"""crawl_source_permissions expand migration (real-autonomous-career-loop Phase 5.2).

Adds the source-specific crawl-permission table. This is the user-grantable
consent request lifecycle (pending / granted / denied / revoked / expired),
distinct from the declared policy inputs (trust / terms / robots status on
``job_sources``) which the crawl_policy layer consumes.

HARD CONSTRAINT (tasks 5.2, 6.6, 10.5): this table stores NO password and NO
raw session material — only the scope, disclosed purpose/frequency/limits,
decision timestamps, and expiry. Opaque session references arrive in Phase 6/7
on a separate surface. A partial unique index enforces "at most one pending
request per source" at the DB so the permission service cannot create duplicate
prompts under concurrency.

Forward-only additive: a brand-new table alongside ``job_sources``. CHECK
short names expand to ``ck_crawl_source_permissions_<short>`` and match
``schema.py`` byte-for-byte. Guarded destructive downgrade for disposable
round-trip databases only.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

SCHEMA = "careerops"

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_CREATE_PERMISSIONS = f"""
CREATE TABLE {SCHEMA}.crawl_source_permissions (
  id uuid PRIMARY KEY,
  source_id uuid NOT NULL REFERENCES {SCHEMA}.job_sources(id) ON DELETE CASCADE,
  domain_scope text NOT NULL DEFAULT '',
  state varchar(16) NOT NULL DEFAULT 'pending',
  disclosed_terms jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  requested_at timestamptz,
  granted_at timestamptz,
  denied_at timestamptz,
  revoked_at timestamptz,
  expired_at timestamptz,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_crawl_source_permissions_state_values
    CHECK (state IN ('pending','granted','denied','revoked','expired')),
  CONSTRAINT ck_crawl_source_permissions_disclosed_terms_object
    CHECK (jsonb_typeof(disclosed_terms) = 'object')
);
CREATE INDEX ix_crawl_source_permissions_source_state
  ON {SCHEMA}.crawl_source_permissions(source_id, state);
-- At most one PENDING request per source (spec: "reuses the existing request
-- instead of creating repeated prompts").
CREATE UNIQUE INDEX uq_crawl_source_permissions_pending_source
  ON {SCHEMA}.crawl_source_permissions(source_id)
  WHERE state = 'pending';
"""

_GRANTS = f"""
REVOKE ALL ON {SCHEMA}.crawl_source_permissions FROM careerops_api;
GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.crawl_source_permissions TO careerops_api;
"""

# --- Guarded downgrade ---------------------------------------------------------

_DOWNGRADE = f"""
-- Production rollback is rollback-forward. A destructive teardown is only
-- allowed for an empty disposable schema (what migration round-trip checks
-- use). Never silently discard a user permission decision.
DO $$
DECLARE
  v_rows bigint;
BEGIN
  SELECT count(*) INTO v_rows FROM {SCHEMA}.crawl_source_permissions;
  IF v_rows > 0 THEN
    RAISE EXCEPTION
      'migration 0032 downgrade refused: % permission rows exist; use rollback-forward', v_rows
      USING ERRCODE = '55000';
  END IF;
END;
$$;

DROP INDEX IF EXISTS {SCHEMA}.uq_crawl_source_permissions_pending_source;
DROP INDEX IF EXISTS {SCHEMA}.ix_crawl_source_permissions_source_state;
DROP TABLE IF EXISTS {SCHEMA}.crawl_source_permissions;
"""


def upgrade() -> None:
    op.execute(sa.text(_CREATE_PERMISSIONS))
    op.execute(sa.text(_GRANTS))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE))
