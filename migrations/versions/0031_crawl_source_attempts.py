# ruff: noqa: E501
"""crawl_source_attempts expand migration (real-autonomous-career-loop Phase 5.1).

Adds the durable per-source attempt-outcome table. Every Tier 1 source attempt
finishes with exactly one of seven canonical outcomes
(:class:`careerops.domain.crawl_attempts.CrawlAttemptOutcome`), recorded here
rather than fragmented across ``crawl_runs.error_category`` (free-form text) and
``job_sources.last_run_metadata`` (which carries no run outcome today).

Forward-only additive: a brand-new table created alongside the existing
``job_sources`` / ``crawl_runs`` tables. The CHECK clause short names expand to
``ck_crawl_source_attempts_<short>`` via env.py's naming convention and match
the ``schema.py`` table byte-for-byte. A guarded destructive downgrade exists
so disposable migration round-trip databases can return to 0030; production
rollback is rollback-forward (never silently discard attempt history).
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

SCHEMA = "careerops"

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_CREATE_ATTEMPTS = f"""
CREATE TABLE {SCHEMA}.crawl_source_attempts (
  id uuid PRIMARY KEY,
  source_id uuid NOT NULL REFERENCES {SCHEMA}.job_sources(id) ON DELETE CASCADE,
  crawl_run_id uuid REFERENCES {SCHEMA}.crawl_runs(id) ON DELETE SET NULL,
  attempt_no integer NOT NULL,
  outcome varchar(32) NOT NULL,
  executor_mode varchar(16) NOT NULL DEFAULT 'http',
  action_count integer NOT NULL DEFAULT 0,
  evidence_summary jsonb NOT NULL DEFAULT '{{}}'::jsonb,
  started_at timestamptz,
  finished_at timestamptz,
  next_eligible_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_crawl_source_attempts_source_attempt_no UNIQUE (source_id, attempt_no),
  CONSTRAINT ck_crawl_source_attempts_outcome_values CHECK
    (outcome IN ('postings_found','verified_empty','not_job_source',
                 'transient_failure','auth_required','dynamic_or_unsupported','policy_denied')),
  CONSTRAINT ck_crawl_source_attempts_executor_mode_values CHECK
    (executor_mode IN ('http','ego')),
  CONSTRAINT ck_crawl_source_attempts_attempt_no_positive CHECK (attempt_no > 0),
  CONSTRAINT ck_crawl_source_attempts_action_count_nonnegative CHECK (action_count >= 0),
  CONSTRAINT ck_crawl_source_attempts_evidence_summary_object
    CHECK (jsonb_typeof(evidence_summary) = 'object')
);
CREATE INDEX ix_crawl_source_attempts_source_created
  ON {SCHEMA}.crawl_source_attempts(source_id, created_at);
CREATE INDEX ix_crawl_source_attempts_next_eligible
  ON {SCHEMA}.crawl_source_attempts(next_eligible_at);
"""

_GRANTS = f"""
REVOKE ALL ON {SCHEMA}.crawl_source_attempts FROM careerops_api;
GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.crawl_source_attempts TO careerops_api;
"""

# --- Guarded downgrade ---------------------------------------------------------

_DOWNGRADE = f"""
-- Production rollback is rollback-forward. A destructive teardown is only
-- allowed for an empty disposable schema (what migration round-trip checks
-- use). Never silently discard a recorded attempt outcome.
DO $$
DECLARE
  v_rows bigint;
BEGIN
  SELECT count(*) INTO v_rows FROM {SCHEMA}.crawl_source_attempts;
  IF v_rows > 0 THEN
    RAISE EXCEPTION
      'migration 0031 downgrade refused: % attempt rows exist; use rollback-forward', v_rows
      USING ERRCODE = '55000';
  END IF;
END;
$$;

DROP INDEX IF EXISTS {SCHEMA}.ix_crawl_source_attempts_next_eligible;
DROP INDEX IF EXISTS {SCHEMA}.ix_crawl_source_attempts_source_created;
DROP TABLE IF EXISTS {SCHEMA}.crawl_source_attempts;
"""


def upgrade() -> None:
    op.execute(sa.text(_CREATE_ATTEMPTS))
    op.execute(sa.text(_GRANTS))


def downgrade() -> None:
    op.execute(sa.text(_DOWNGRADE))
