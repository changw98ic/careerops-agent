# ruff: noqa: E501
"""Agent console orchestration expand/contract migration.

The migration contains deliberate long SQL signatures and CHECK clauses;
keeping those expressions together makes the database contract auditable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

SCHEMA = "careerops"

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

_EXPAND_COLUMNS = f"""
ALTER TABLE {SCHEMA}.agent_runs
  ADD COLUMN IF NOT EXISTS execution_state text,
  ADD COLUMN IF NOT EXISTS capability_state text,
  ADD COLUMN IF NOT EXISTS review_state text,
  ADD COLUMN IF NOT EXISTS legacy_state text,
  ADD COLUMN IF NOT EXISTS context_id uuid,
  ADD COLUMN IF NOT EXISTS current_attempt integer;
"""

_EXPAND_CHECKS = f"""
ALTER TABLE {SCHEMA}.agent_runs
  ADD CONSTRAINT ck_agent_runs_execution_state CHECK
    (execution_state IS NULL OR execution_state IN
      ('queued','running','waiting_review','succeeded','failed',
       'cancel_requested','cancelled','stale','blocked')),
  ADD CONSTRAINT ck_agent_runs_capability_state CHECK
    (capability_state IS NULL OR capability_state IN
      ('enabled','disabled_by_policy','not_configured','dependency_not_ready',
       'blocked_by_prerequisite','stale','failed')),
  ADD CONSTRAINT ck_agent_runs_review_state CHECK
    (review_state IS NULL OR review_state IN
      ('not_required','pending','accepted','rejected','edited'));
"""

_UNIQUE_ID_CANDIDATE = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_id_candidate
  ON {SCHEMA}.agent_runs (id, candidate_id);
"""

_REVIEW_EXPAND = f"""
ALTER TABLE {SCHEMA}.agent_run_reviews
  ADD COLUMN IF NOT EXISTS idempotency_key varchar(128),
  ADD COLUMN IF NOT EXISTS field_decisions jsonb,
  ADD COLUMN IF NOT EXISTS request_hash varchar(64);
"""

_REVIEW_FK_AND_CHECKS = f"""
ALTER TABLE {SCHEMA}.agent_run_reviews
  DROP CONSTRAINT IF EXISTS agent_run_reviews_run_id_fkey,
  ADD CONSTRAINT fk_agent_run_reviews_run_candidate
    FOREIGN KEY (run_id,candidate_id)
    REFERENCES {SCHEMA}.agent_runs(id,candidate_id) ON DELETE RESTRICT,
  ADD CONSTRAINT ck_agent_run_reviews_request_hash
    CHECK (request_hash IS NULL OR char_length(request_hash) = 64),
  ADD CONSTRAINT ck_agent_run_reviews_field_decisions
    CHECK (field_decisions IS NULL OR jsonb_typeof(field_decisions) = 'array');
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_run_reviews_candidate_key
  ON {SCHEMA}.agent_run_reviews(candidate_id,run_id,idempotency_key)
  WHERE idempotency_key IS NOT NULL;
"""

_CREATE_CONTEXTS = f"""
CREATE TABLE {SCHEMA}.agent_contexts (
  id uuid PRIMARY KEY,
  candidate_id uuid NOT NULL REFERENCES {SCHEMA}.candidates(id) ON DELETE CASCADE,
  operation varchar(32) NOT NULL,
  schema_version varchar(64) NOT NULL,
  digest_algorithm varchar(16) NOT NULL CHECK (digest_algorithm = 'sha256'),
  source_version_snapshot jsonb NOT NULL CHECK (jsonb_typeof(source_version_snapshot)='object'),
  source_digests jsonb NOT NULL CHECK (jsonb_typeof(source_digests)='object'),
  allowed_operation varchar(32) NOT NULL,
  state varchar(16) NOT NULL DEFAULT 'active'
    CHECK (state IN ('active','stale','expired','revoked')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  invalidation_reason varchar(64),
  UNIQUE (id,candidate_id),
  CHECK (operation = allowed_operation),
  CHECK (expires_at > created_at)
);
"""

_FK_RUNS_CONTEXT = f"""
ALTER TABLE {SCHEMA}.agent_runs
  ADD CONSTRAINT fk_agent_runs_context_candidate
  FOREIGN KEY (context_id,candidate_id)
  REFERENCES {SCHEMA}.agent_contexts(id,candidate_id) ON DELETE SET NULL;
"""

_CREATE_ACTIONS = f"""
CREATE TABLE {SCHEMA}.agent_actions (
  id uuid PRIMARY KEY,
  candidate_id uuid NOT NULL REFERENCES {SCHEMA}.candidates(id) ON DELETE CASCADE,
  action_key varchar(160) NOT NULL,
  source_event_key uuid NOT NULL,
  queue_version bigint NOT NULL CHECK (queue_version > 0),
  state varchar(16) NOT NULL CHECK (state IN ('proposed','accepted','snoozed','dismissed','completed','expired','blocked')),
  kind varchar(40) NOT NULL,
  reason_code varchar(64) NOT NULL,
  deterministic_rank integer NOT NULL CHECK (deterministic_rank >= 0),
  source_refs jsonb NOT NULL CHECK (jsonb_typeof(source_refs)='array'),
  context_id uuid,
  snooze_until timestamptz,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (candidate_id,action_key,source_event_key),
  FOREIGN KEY (context_id,candidate_id)
    REFERENCES {SCHEMA}.agent_contexts(id,candidate_id) ON DELETE RESTRICT
);
"""

_CREATE_ATTEMPTS = f"""
CREATE TABLE {SCHEMA}.agent_attempts (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL,
  candidate_id uuid NOT NULL REFERENCES {SCHEMA}.candidates(id) ON DELETE CASCADE,
  attempt_no integer NOT NULL CHECK (attempt_no > 0),
  workflow_id varchar(220) NOT NULL,
  worker_id uuid,
  lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
  cancel_epoch bigint NOT NULL DEFAULT 0 CHECK (cancel_epoch >= 0),
  state varchar(24) NOT NULL CHECK (state IN ('queued','running','waiting_review','succeeded','failed','cancel_requested','cancelled','stale','blocked')),
  retry_budget integer NOT NULL CHECK (retry_budget >= 0),
  dead_lettered_at timestamptz,
  dead_letter_reason varchar(64),
  lease_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (run_id,attempt_no),
  UNIQUE (id,run_id,candidate_id),
  FOREIGN KEY (run_id,candidate_id)
    REFERENCES {SCHEMA}.agent_runs(id,candidate_id) ON DELETE RESTRICT
);
"""

_CREATE_STAGE_EVENTS = f"""
CREATE TABLE {SCHEMA}.agent_stage_events (
  id uuid PRIMARY KEY,
  attempt_id uuid NOT NULL,
  run_id uuid NOT NULL,
  candidate_id uuid NOT NULL REFERENCES {SCHEMA}.candidates(id) ON DELETE CASCADE,
  event_key uuid NOT NULL,
  sequence integer NOT NULL CHECK (sequence > 0),
  schema_version varchar(64) NOT NULL,
  stage varchar(64) NOT NULL,
  status varchar(20) NOT NULL CHECK (status IN ('started','completed','blocked','failed','cancelled')),
  cause varchar(64),
  terminal boolean NOT NULL DEFAULT false,
  provider_state varchar(32),
  retryable boolean NOT NULL DEFAULT false,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
  redacted_payload jsonb NOT NULL CHECK (jsonb_typeof(redacted_payload)='object'),
  retention_until timestamptz NOT NULL,
  UNIQUE (event_key),
  UNIQUE (attempt_id,sequence),
  FOREIGN KEY (attempt_id,run_id,candidate_id)
    REFERENCES {SCHEMA}.agent_attempts(id,run_id,candidate_id) ON DELETE RESTRICT
);
"""

_CREATE_IDEMPOTENCY_RECEIPTS = f"""
CREATE TABLE {SCHEMA}.agent_idempotency_receipts (
  id uuid PRIMARY KEY,
  actor_id varchar(128) NOT NULL,
  candidate_id uuid NOT NULL REFERENCES {SCHEMA}.candidates(id) ON DELETE CASCADE,
  resource_type varchar(64) NOT NULL,
  resource_id uuid NOT NULL,
  operation varchar(64) NOT NULL,
  idempotency_key varchar(128) NOT NULL,
  canonical_body_hash varchar(64) NOT NULL CHECK (char_length(canonical_body_hash)=64),
  response_status smallint NOT NULL CHECK (response_status BETWEEN 200 AND 499),
  receipt jsonb NOT NULL CHECK (jsonb_typeof(receipt)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  UNIQUE (actor_id,candidate_id,resource_type,resource_id,operation,idempotency_key),
  CHECK (resource_id <> '00000000-0000-0000-0000-000000000000'::uuid OR resource_type IN ('agent_context','agent_action_queue','crawl_plan'))
);
"""

_BACKFILL = f"""
UPDATE {SCHEMA}.agent_runs
SET legacy_state = state,
    execution_state = CASE state
      WHEN 'pending' THEN 'queued'
      WHEN 'running' THEN 'running'
      WHEN 'succeeded' THEN 'succeeded'
      WHEN 'failed' THEN 'failed'
      WHEN 'unavailable' THEN 'blocked'
      WHEN 'abstained' THEN 'waiting_review'
      WHEN 'stale' THEN 'stale'
      WHEN 'cancelled' THEN 'cancelled'
      WHEN 'reviewed' THEN CASE WHEN review_decision IS NULL THEN 'waiting_review' ELSE 'succeeded' END
      ELSE NULL END,
    capability_state = CASE state
      WHEN 'unavailable' THEN 'dependency_not_ready'
      WHEN 'failed' THEN 'failed'
      WHEN 'stale' THEN 'stale'
      ELSE 'enabled' END,
    review_state = CASE
      WHEN state = 'reviewed' THEN COALESCE(review_decision, 'pending')
      WHEN state = 'abstained' THEN 'pending'
      ELSE 'not_required' END,
    current_attempt = COALESCE(current_attempt, 0)
WHERE execution_state IS NULL;
"""

_PREFLIGHT_COUNT = f"""
SELECT count(*) FROM {SCHEMA}.agent_runs
WHERE state NOT IN ('pending','running','succeeded','failed','unavailable','abstained','stale','cancelled','reviewed');
"""

_VERIFY_NONNULL = f"""
SELECT count(*) FROM {SCHEMA}.agent_runs
WHERE execution_state IS NULL OR capability_state IS NULL
  OR review_state IS NULL OR legacy_state IS NULL;
"""

_TIGHTEN_COLUMNS = f"""
ALTER TABLE {SCHEMA}.agent_runs
  ALTER COLUMN execution_state SET DEFAULT 'queued',
  ALTER COLUMN execution_state SET NOT NULL,
  ALTER COLUMN capability_state SET DEFAULT 'enabled',
  ALTER COLUMN capability_state SET NOT NULL,
  ALTER COLUMN review_state SET DEFAULT 'not_required',
  ALTER COLUMN review_state SET NOT NULL,
  ALTER COLUMN current_attempt SET DEFAULT 0,
  ALTER COLUMN current_attempt SET NOT NULL;
"""

# --- Stage events append-only trigger ------------------------------------------

_APPEND_ONLY_TRIGGER = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.reject_agent_stage_event_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = {SCHEMA}, pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'agent stage events are append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS agent_stage_events_append_only
    ON {SCHEMA}.agent_stage_events;
CREATE TRIGGER agent_stage_events_append_only
BEFORE UPDATE OR DELETE ON {SCHEMA}.agent_stage_events
FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_agent_stage_event_mutation();
"""

# --- Legacy compatibility trigger -----------------------------------------------

_NORMALIZE_LEGACY_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.normalize_agent_legacy_state()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
BEGIN
  -- Map recognized legacy state values to canonical fields
  NEW.legacy_state := NEW.state;
  NEW.execution_state := CASE NEW.state
    WHEN 'pending' THEN 'queued'
    WHEN 'running' THEN 'running'
    WHEN 'succeeded' THEN 'succeeded'
    WHEN 'failed' THEN 'failed'
    WHEN 'unavailable' THEN 'blocked'
    WHEN 'abstained' THEN 'waiting_review'
    WHEN 'stale' THEN 'stale'
    WHEN 'cancelled' THEN 'cancelled'
    WHEN 'reviewed' THEN CASE WHEN NEW.review_decision IS NULL THEN 'waiting_review' ELSE 'succeeded' END
    ELSE NULL END;
  NEW.capability_state := CASE NEW.state
    WHEN 'unavailable' THEN 'dependency_not_ready'
    WHEN 'failed' THEN 'failed'
    WHEN 'stale' THEN 'stale'
    ELSE 'enabled' END;
  NEW.review_state := CASE
    WHEN NEW.state = 'reviewed' THEN COALESCE(NEW.review_decision, 'pending')
    WHEN NEW.state = 'abstained' THEN 'pending'
    ELSE 'not_required' END;
  NEW.current_attempt := COALESCE(NEW.current_attempt, 0);

  IF NEW.execution_state IS NULL THEN
    RAISE EXCEPTION 'unrecognized legacy state: %', NEW.state
      USING ERRCODE = '22023';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_runs_normalize_legacy
    ON {SCHEMA}.agent_runs;
CREATE TRIGGER agent_runs_normalize_legacy
BEFORE INSERT OR UPDATE OF state ON {SCHEMA}.agent_runs
FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.normalize_agent_legacy_state();
"""

# --- CAS functions -------------------------------------------------------------

_ACQUIRE_LEASE_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.acquire_agent_attempt(
  p_run_id uuid,
  p_candidate_id uuid,
  p_attempt_id uuid,
  p_worker_id uuid,
  p_now timestamptz
) RETURNS TABLE(lease_epoch bigint, cancel_epoch bigint, lease_expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_lease_epoch bigint;
  v_cancel_epoch bigint;
  v_expires timestamptz;
BEGIN
  -- Verify parent run exists with matching candidate
  SELECT ar.cancel_epoch INTO v_cancel_epoch
  FROM agent_runs ar
  WHERE ar.id = p_run_id AND ar.candidate_id = p_candidate_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'run not found or candidate mismatch'
      USING ERRCODE = 'no_data_found';
  END IF;

  v_cancel_epoch := COALESCE(v_cancel_epoch, 0);
  v_lease_epoch := 1;
  v_expires := p_now + interval '5 minutes';

  INSERT INTO agent_attempts (id, run_id, candidate_id, attempt_no, workflow_id, worker_id,
    lease_epoch, cancel_epoch, state, retry_budget, lease_expires_at, created_at)
  SELECT p_attempt_id, p_run_id, p_candidate_id,
    COALESCE((SELECT max(attempt_no) FROM agent_attempts WHERE run_id = p_run_id), 0) + 1,
    '', p_worker_id, v_lease_epoch, v_cancel_epoch, 'queued', 3, v_expires, p_now
  ON CONFLICT DO NOTHING;

  RETURN QUERY SELECT v_lease_epoch, v_cancel_epoch, v_expires;
END;
$$;
"""

_RENEW_LEASE_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.renew_agent_attempt(
  p_attempt_id uuid,
  p_run_id uuid,
  p_candidate_id uuid,
  p_worker_id uuid,
  p_lease_epoch bigint,
  p_cancel_epoch bigint,
  p_now timestamptz
) RETURNS TABLE(lease_epoch bigint, lease_expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_updated int;
  v_new_expires timestamptz;
BEGIN
  v_new_expires := p_now + interval '5 minutes';

  UPDATE agent_attempts
  SET lease_epoch = lease_epoch + 1,
      lease_expires_at = v_new_expires
  WHERE id = p_attempt_id
    AND run_id = p_run_id
    AND candidate_id = p_candidate_id
    AND worker_id = p_worker_id
    AND lease_epoch = p_lease_epoch
    AND cancel_epoch = p_cancel_epoch
    AND lease_expires_at > p_now;

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  IF v_updated = 0 THEN
    RAISE EXCEPTION 'lease renewal failed: fenced or expired'
      USING ERRCODE = '40001';
  END IF;

  RETURN QUERY SELECT p_lease_epoch + 1, v_new_expires;
END;
$$;
"""

_APPEND_STAGE_EVENT_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.append_agent_stage_event(
  p_stage_event_id uuid,
  p_attempt_id uuid,
  p_run_id uuid,
  p_candidate_id uuid,
  p_worker_id uuid,
  p_lease_epoch bigint,
  p_cancel_epoch bigint,
  p_event_key uuid,
  p_sequence integer,
  p_schema_version varchar,
  p_stage varchar,
  p_status varchar,
  p_cause varchar,
  p_terminal boolean,
  p_provider_state varchar,
  p_retryable boolean,
  p_occurred_at timestamptz,
  p_duration_ms integer,
  p_redacted_payload jsonb,
  p_retention_until timestamptz
) RETURNS TABLE(stage_event_id uuid, event_key uuid, sequence integer)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_valid int;
BEGIN
  -- CAS: verify attempt lease is still held
  SELECT 1 INTO v_valid
  FROM agent_attempts
  WHERE id = p_attempt_id
    AND run_id = p_run_id
    AND candidate_id = p_candidate_id
    AND worker_id = p_worker_id
    AND lease_epoch = p_lease_epoch
    AND cancel_epoch = p_cancel_epoch
    AND lease_expires_at > p_occurred_at;

  IF v_valid IS NULL THEN
    RAISE EXCEPTION 'FENCED_OR_CANCELLED: attempt lease invalid'
      USING ERRCODE = '40001';
  END IF;

  INSERT INTO agent_stage_events (id, attempt_id, run_id, candidate_id, event_key,
    sequence, schema_version, stage, status, cause, terminal, provider_state,
    retryable, occurred_at, duration_ms, redacted_payload, retention_until)
  VALUES (p_stage_event_id, p_attempt_id, p_run_id, p_candidate_id, p_event_key,
    p_sequence, p_schema_version, p_stage, p_status, p_cause, p_terminal,
    p_provider_state, p_retryable, p_occurred_at, p_duration_ms,
    p_redacted_payload, p_retention_until);

  RETURN QUERY SELECT p_stage_event_id, p_event_key, p_sequence;
END;
$$;
"""

_RECONCILE_RUN_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.reconcile_agent_run(
  p_run_id uuid,
  p_candidate_id uuid,
  p_attempt_id uuid,
  p_worker_id uuid,
  p_lease_epoch bigint,
  p_cancel_epoch bigint,
  p_execution_state varchar,
  p_capability_state varchar,
  p_review_state varchar,
  p_finished_at timestamptz
) RETURNS TABLE(execution_state varchar, capability_state varchar, review_state varchar)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_valid int;
  v_updated int;
BEGIN
  -- Verify lease
  SELECT 1 INTO v_valid
  FROM agent_attempts a
  WHERE a.id = p_attempt_id
    AND a.run_id = p_run_id
    AND a.candidate_id = p_candidate_id
    AND a.worker_id = p_worker_id
    AND a.lease_epoch = p_lease_epoch
    AND a.cancel_epoch = p_cancel_epoch
    AND a.lease_expires_at > now();

  IF v_valid IS NULL THEN
    RAISE EXCEPTION 'FENCED_OR_CANCELLED: attempt lease invalid'
      USING ERRCODE = '40001';
  END IF;

  -- Disable legacy trigger during reconciliation to avoid double-mapping
  ALTER TABLE agent_runs DISABLE TRIGGER agent_runs_normalize_legacy;

  UPDATE agent_runs
  SET execution_state = p_execution_state,
      capability_state = p_capability_state,
      review_state = p_review_state,
      legacy_state = p_execution_state,
      finished_at = COALESCE(p_finished_at, finished_at)
  WHERE id = p_run_id AND candidate_id = p_candidate_id;

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  IF v_updated = 0 THEN
    RAISE EXCEPTION 'run not found for reconciliation'
      USING ERRCODE = 'no_data_found';
  END IF;

  UPDATE agent_attempts
  SET state = p_execution_state,
      finished_at = p_finished_at
  WHERE id = p_attempt_id;

  ALTER TABLE agent_runs ENABLE TRIGGER agent_runs_normalize_legacy;

  RETURN QUERY SELECT p_execution_state, p_capability_state, p_review_state;
END;
$$;
"""

_RESERVE_IDEMPOTENCY_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.reserve_agent_idempotency(
  p_actor_id varchar,
  p_candidate_id uuid,
  p_resource_type varchar,
  p_resource_id uuid,
  p_operation varchar,
  p_idempotency_key varchar,
  p_canonical_body_hash varchar,
  p_expires_at timestamptz
) RETURNS TABLE(receipt_id uuid, replayed boolean, existing_body_hash varchar, existing_receipt jsonb)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_existing RECORD;
BEGIN
  SELECT id, canonical_body_hash, receipt
  INTO v_existing
  FROM agent_idempotency_receipts
  WHERE actor_id = p_actor_id
    AND candidate_id = p_candidate_id
    AND resource_type = p_resource_type
    AND resource_id = p_resource_id
    AND operation = p_operation
    AND idempotency_key = p_idempotency_key;

  IF FOUND THEN
    IF v_existing.canonical_body_hash = p_canonical_body_hash THEN
      RETURN QUERY SELECT v_existing.id, true, v_existing.canonical_body_hash, v_existing.receipt;
      RETURN;
    ELSE
      RAISE EXCEPTION 'IDEMPOTENCY_CONFLICT: same key, different body hash'
        USING ERRCODE = '23505';
    END IF;
  END IF;

  INSERT INTO agent_idempotency_receipts (id, actor_id, candidate_id, resource_type,
    resource_id, operation, idempotency_key, canonical_body_hash, response_status,
    receipt, expires_at)
  VALUES (gen_random_uuid(), p_actor_id, p_candidate_id, p_resource_type,
    p_resource_id, p_operation, p_idempotency_key, p_canonical_body_hash,
    202, jsonb_build_object('pending', true), p_expires_at)
  RETURNING id INTO v_existing.id;

  RETURN QUERY SELECT v_existing.id, false, NULL::varchar, NULL::jsonb;
END;
$$;
"""

_FINALIZE_IDEMPOTENCY_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.finalize_agent_idempotency(
  p_receipt_id uuid,
  p_response_status smallint,
  p_receipt jsonb
) RETURNS TABLE(receipt_id uuid)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
BEGIN
  UPDATE agent_idempotency_receipts
  SET response_status = p_response_status,
      receipt = p_receipt
  WHERE id = p_receipt_id;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'receipt not found: %', p_receipt_id
      USING ERRCODE = 'no_data_found';
  END IF;

  RETURN QUERY SELECT p_receipt_id;
END;
$$;
"""

_PURGE_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.purge_agent_console(
  p_now timestamptz
) RETURNS TABLE(purged bigint, skipped_legal_hold bigint, failed bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_purged bigint := 0;
  v_skipped bigint := 0;
  v_failed bigint := 0;
  v_advisory_key bigint := 7283910456;
BEGIN
  -- Acquire advisory lock to prevent concurrent purges
  IF NOT pg_try_advisory_lock(v_advisory_key) THEN
    RAISE EXCEPTION 'purge already running'
      USING ERRCODE = '55000';
  END IF;

  -- Expire stale idempotency receipts
  DELETE FROM agent_idempotency_receipts
  WHERE expires_at < p_now;

  GET DIAGNOSTICS v_purged = ROW_COUNT;

  -- Stage events retention (90 days)
  WITH due AS (
    SELECT se.id
    FROM agent_stage_events se
    WHERE se.retention_until < p_now
    LIMIT 5000
    FOR UPDATE SKIP LOCKED
  )
  DELETE FROM agent_stage_events se
  USING due WHERE se.id = due.id;

  GET DIAGNOSTICS v_purged = ROW_COUNT;
  v_purged := v_purged;

  PERFORM pg_advisory_unlock(v_advisory_key);
  RETURN QUERY SELECT v_purged, v_skipped, v_failed;
END;
$$;
"""

# --- Legacy read/write adapters ------------------------------------------------

_READ_LEGACY_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.read_agent_legacy_state(
  p_run_id uuid,
  p_candidate_id uuid
) RETURNS TABLE(state varchar, result jsonb)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
BEGIN
  RETURN QUERY
  SELECT ar.legacy_state, ar.result
  FROM agent_runs ar
  WHERE ar.id = p_run_id AND ar.candidate_id = p_candidate_id;
END;
$$;
"""

_WRITE_LEGACY_FN = f"""
CREATE OR REPLACE FUNCTION {SCHEMA}.write_agent_legacy_state(
  p_run_id uuid,
  p_candidate_id uuid,
  p_state varchar,
  p_result jsonb
) RETURNS TABLE(execution_state varchar, capability_state varchar, review_state varchar)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = {SCHEMA}, pg_catalog AS $$
DECLARE
  v_exec varchar;
  v_cap varchar;
  v_rev varchar;
BEGIN
  -- Disable trigger to avoid double-mapping; we do explicit canonical mapping
  ALTER TABLE agent_runs DISABLE TRIGGER agent_runs_normalize_legacy;

  UPDATE agent_runs
  SET state = p_state,
      result = COALESCE(p_result, result),
      legacy_state = p_state,
      execution_state = CASE p_state
        WHEN 'pending' THEN 'queued'
        WHEN 'running' THEN 'running'
        WHEN 'succeeded' THEN 'succeeded'
        WHEN 'failed' THEN 'failed'
        WHEN 'unavailable' THEN 'blocked'
        WHEN 'abstained' THEN 'waiting_review'
        WHEN 'stale' THEN 'stale'
        WHEN 'cancelled' THEN 'cancelled'
        WHEN 'reviewed' THEN CASE WHEN review_decision IS NULL THEN 'waiting_review' ELSE 'succeeded' END
        ELSE NULL END,
      capability_state = CASE p_state
        WHEN 'unavailable' THEN 'dependency_not_ready'
        WHEN 'failed' THEN 'failed'
        WHEN 'stale' THEN 'stale'
        ELSE 'enabled' END,
      review_state = CASE
        WHEN p_state = 'reviewed' THEN COALESCE(review_decision, 'pending')
        WHEN p_state = 'abstained' THEN 'pending'
        ELSE 'not_required' END
  WHERE id = p_run_id AND candidate_id = p_candidate_id
  RETURNING execution_state, capability_state, review_state
  INTO v_exec, v_cap, v_rev;

  ALTER TABLE agent_runs ENABLE TRIGGER agent_runs_normalize_legacy;

  IF v_exec IS NULL THEN
    RAISE EXCEPTION 'run not found'
      USING ERRCODE = 'no_data_found';
  END IF;

  RETURN QUERY SELECT v_exec, v_cap, v_rev;
END;
$$;
"""

# --- Candidate views -----------------------------------------------------------

_V_AGENT_RUNS = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_agent_runs_candidate
WITH (security_barrier = true) AS
SELECT * FROM {SCHEMA}.agent_runs;
"""

_V_AGENT_CONTEXTS = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_agent_contexts_candidate
WITH (security_barrier = true) AS
SELECT * FROM {SCHEMA}.agent_contexts;
"""

_V_AGENT_ACTIONS = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_agent_actions_candidate
WITH (security_barrier = true) AS
SELECT * FROM {SCHEMA}.agent_actions;
"""

_V_AGENT_ATTEMPTS = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_agent_attempts_candidate
WITH (security_barrier = true) AS
SELECT * FROM {SCHEMA}.agent_attempts;
"""

_V_AGENT_STAGE_EVENTS = f"""
CREATE OR REPLACE VIEW {SCHEMA}.v_agent_stage_events_candidate
WITH (security_barrier = true) AS
SELECT * FROM {SCHEMA}.agent_stage_events;
"""

# --- Grants --------------------------------------------------------------------

_GRANTS = f"""
REVOKE ALL ON {SCHEMA}.agent_runs, {SCHEMA}.agent_run_reviews,
  {SCHEMA}.agent_contexts, {SCHEMA}.agent_actions, {SCHEMA}.agent_attempts,
  {SCHEMA}.agent_stage_events, {SCHEMA}.agent_idempotency_receipts FROM careerops_api;
REVOKE INSERT, UPDATE, DELETE ON {SCHEMA}.audit_events FROM careerops_api;
GRANT SELECT ON {SCHEMA}.v_agent_runs_candidate,
  {SCHEMA}.v_agent_contexts_candidate, {SCHEMA}.v_agent_actions_candidate,
  {SCHEMA}.v_agent_attempts_candidate, {SCHEMA}.v_agent_stage_events_candidate
  TO careerops_api;
GRANT EXECUTE ON FUNCTION {SCHEMA}.acquire_agent_attempt(uuid,uuid,uuid,uuid,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION {SCHEMA}.renew_agent_attempt(uuid,uuid,uuid,uuid,bigint,bigint,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION {SCHEMA}.append_agent_stage_event(uuid,uuid,uuid,uuid,uuid,bigint,bigint,uuid,integer,varchar,varchar,varchar,varchar,boolean,varchar,boolean,timestamptz,integer,jsonb,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION {SCHEMA}.reconcile_agent_run(uuid,uuid,uuid,uuid,bigint,bigint,varchar,varchar,varchar,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION {SCHEMA}.reserve_agent_idempotency(varchar,uuid,varchar,uuid,varchar,varchar,varchar,timestamptz) TO careerops_api;
GRANT EXECUTE ON FUNCTION {SCHEMA}.finalize_agent_idempotency(uuid,smallint,jsonb) TO careerops_api;
GRANT EXECUTE ON FUNCTION {SCHEMA}.purge_agent_console(timestamptz) TO careerops_retention;
GRANT EXECUTE ON FUNCTION {SCHEMA}.read_agent_legacy_state(uuid,uuid) TO careerops_legacy_agent;
GRANT EXECUTE ON FUNCTION {SCHEMA}.write_agent_legacy_state(uuid,uuid,varchar,jsonb) TO careerops_legacy_agent;
"""


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # 1. Expand phase: add nullable columns, constraints, indexes
    # -----------------------------------------------------------------------
    op.execute(sa.text(_EXPAND_COLUMNS))
    op.execute(sa.text(_EXPAND_CHECKS))
    op.execute(sa.text(_UNIQUE_ID_CANDIDATE))

    # agent_run_reviews expansion
    op.execute(sa.text(_REVIEW_EXPAND))

    # -----------------------------------------------------------------------
    # 2. New tables
    # -----------------------------------------------------------------------
    op.execute(sa.text(_CREATE_CONTEXTS))

    # FK from agent_runs.context_id to agent_contexts
    op.execute(sa.text(_FK_RUNS_CONTEXT))

    op.execute(sa.text(_CREATE_ACTIONS))
    op.execute(sa.text(_CREATE_ATTEMPTS))
    op.execute(sa.text(_CREATE_STAGE_EVENTS))
    op.execute(sa.text(_CREATE_IDEMPOTENCY_RECEIPTS))

    # Composite FK for reviews (after agent_runs unique index exists)
    op.execute(sa.text(_REVIEW_FK_AND_CHECKS))

    # -----------------------------------------------------------------------
    # 3. Backfill phase
    # -----------------------------------------------------------------------
    # Skip runtime checks in offline SQL mode (no database connection).
    if not op.get_context().as_sql:
        # Preflight: abort if any unrecognized legacy states exist
        result = op.get_bind().execute(sa.text(_PREFLIGHT_COUNT))
        bad_count = result.scalar()
        if bad_count and bad_count > 0:
            raise RuntimeError(
                f"Migration 0030 preflight: {bad_count} agent_runs rows have "
                "unrecognized state values; aborting."
            )

    op.execute(sa.text(_BACKFILL))

    # Verify no null canonical fields remain (skip in offline mode)
    if not op.get_context().as_sql:
        result = op.get_bind().execute(sa.text(_VERIFY_NONNULL))
        null_count = result.scalar()
        if null_count and null_count > 0:
            raise RuntimeError(
                f"Migration 0030 verify: {null_count} agent_runs rows still have "
                "NULL canonical fields after backfill."
            )

    # Contract: tighten nullable columns to NOT NULL
    op.execute(sa.text(_TIGHTEN_COLUMNS))

    # -----------------------------------------------------------------------
    # 4. Triggers and functions
    # -----------------------------------------------------------------------
    op.execute(sa.text(_APPEND_ONLY_TRIGGER))
    op.execute(sa.text(_NORMALIZE_LEGACY_FN))
    op.execute(sa.text(_ACQUIRE_LEASE_FN))
    op.execute(sa.text(_RENEW_LEASE_FN))
    op.execute(sa.text(_APPEND_STAGE_EVENT_FN))
    op.execute(sa.text(_RECONCILE_RUN_FN))
    op.execute(sa.text(_RESERVE_IDEMPOTENCY_FN))
    op.execute(sa.text(_FINALIZE_IDEMPOTENCY_FN))
    op.execute(sa.text(_PURGE_FN))
    op.execute(sa.text(_READ_LEGACY_FN))
    op.execute(sa.text(_WRITE_LEGACY_FN))

    # -----------------------------------------------------------------------
    # 5. Views and grants
    # -----------------------------------------------------------------------
    op.execute(sa.text(_V_AGENT_RUNS))
    op.execute(sa.text(_V_AGENT_CONTEXTS))
    op.execute(sa.text(_V_AGENT_ACTIONS))
    op.execute(sa.text(_V_AGENT_ATTEMPTS))
    op.execute(sa.text(_V_AGENT_STAGE_EVENTS))
    op.execute(sa.text(_GRANTS))


def downgrade() -> None:
    # Downgrade is a no-op per contract: rollback is forward-only.
    # Destructive downgrade would break running attempts and orphan audit data.
    pass
