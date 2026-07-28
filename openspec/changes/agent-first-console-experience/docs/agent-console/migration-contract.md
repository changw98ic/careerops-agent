# Migration 0030 contract

The migration file is `migrations/versions/0030_agent_console_orchestration.py`
with `revision = "0030_agent_console_orchestration"` and
`down_revision = "0029"`. The repository's current head is `0029`; the new
revision must not create a branch. This is an expand/contract migration:
production rollback is forward-only. A guarded downgrade is provided only for
an empty disposable schema so migration round-trip checks can remove every
0030-owned object without leaving dependencies for the older 0025 downgrade.
If any Agent row exists, the downgrade fails closed and the operator must use
the rollback-forward procedure below.

The SQL below is the normative shape. UUID/timestamp defaults use the existing
repository conventions. The implementation may choose SQLAlchemy/Alembic
helpers, but the resulting columns, constraints, indexes, functions, grants,
and transaction boundaries must be equivalent.

## 1. Expand phase DDL

### 1.1 Existing Agent rows

The current `agent_runs` and `agent_run_reviews` rows are preserved. The
expand transaction first adds nullable canonical columns and a compatibility
unique key; it does not use a `NOT NULL DEFAULT` table rewrite:

```sql
ALTER TABLE careerops.agent_runs
  ADD COLUMN IF NOT EXISTS execution_state text,
  ADD COLUMN IF NOT EXISTS capability_state text,
  ADD COLUMN IF NOT EXISTS review_state text,
  ADD COLUMN IF NOT EXISTS legacy_state text,
  ADD COLUMN IF NOT EXISTS context_id uuid,
  ADD COLUMN IF NOT EXISTS current_attempt integer;

ALTER TABLE careerops.agent_runs
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_runs_id_candidate
  ON careerops.agent_runs (id, candidate_id);

ALTER TABLE careerops.agent_run_reviews
  ADD COLUMN IF NOT EXISTS idempotency_key varchar(128),
  ADD COLUMN IF NOT EXISTS field_decisions jsonb,
  ADD COLUMN IF NOT EXISTS request_hash varchar(64);
```

The migration verifies that no existing `agent_runs.id` is duplicated across
candidate scope before creating the unique index. `agent_run_reviews` receives
a composite foreign key `(run_id,candidate_id)` after the old single-column
foreign key is replaced in the contract phase; a review for a mismatched run
and candidate is rejected by PostgreSQL.

The exact review-key conversion is:

```sql
ALTER TABLE careerops.agent_run_reviews
  DROP CONSTRAINT IF EXISTS agent_run_reviews_run_id_fkey,
  ADD CONSTRAINT fk_agent_run_reviews_run_candidate
    FOREIGN KEY (run_id,candidate_id)
    REFERENCES careerops.agent_runs(id,candidate_id) ON DELETE RESTRICT,
  ADD CONSTRAINT ck_agent_run_reviews_request_hash
    CHECK (request_hash IS NULL OR char_length(request_hash) = 64),
  ADD CONSTRAINT ck_agent_run_reviews_field_decisions
    CHECK (field_decisions IS NULL OR jsonb_typeof(field_decisions) = 'array');
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_run_reviews_candidate_key
  ON careerops.agent_run_reviews(candidate_id,run_id,idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

### 1.2 New tables

```sql
CREATE TABLE careerops.agent_contexts (
  id uuid PRIMARY KEY,
  candidate_id uuid NOT NULL REFERENCES careerops.candidates(id) ON DELETE CASCADE,
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

ALTER TABLE careerops.agent_runs
  ADD CONSTRAINT fk_agent_runs_context_candidate
  FOREIGN KEY (context_id,candidate_id)
  REFERENCES careerops.agent_contexts(id,candidate_id) ON DELETE SET NULL;

CREATE TABLE careerops.agent_actions (
  id uuid PRIMARY KEY,
  candidate_id uuid NOT NULL REFERENCES careerops.candidates(id) ON DELETE CASCADE,
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
    REFERENCES careerops.agent_contexts(id,candidate_id) ON DELETE RESTRICT
);

CREATE TABLE careerops.agent_attempts (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL,
  candidate_id uuid NOT NULL REFERENCES careerops.candidates(id) ON DELETE CASCADE,
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
    REFERENCES careerops.agent_runs(id,candidate_id) ON DELETE RESTRICT
);

CREATE TABLE careerops.agent_stage_events (
  id uuid PRIMARY KEY,
  attempt_id uuid NOT NULL,
  run_id uuid NOT NULL,
  candidate_id uuid NOT NULL REFERENCES careerops.candidates(id) ON DELETE CASCADE,
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
    REFERENCES careerops.agent_attempts(id,run_id,candidate_id) ON DELETE RESTRICT
);

CREATE TABLE careerops.agent_idempotency_receipts (
  id uuid PRIMARY KEY,
  actor_id varchar(128) NOT NULL,
  candidate_id uuid NOT NULL REFERENCES careerops.candidates(id) ON DELETE CASCADE,
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
```

All JSONB payloads are additionally limited to 64 KiB by a check or trigger;
the stage-event append function rejects raw prompt/response keys and stores
only IDs, digests, bounded summaries, token counts, and typed codes. Candidate
and parent composite indexes are mandatory for every new table.

## 2. Backfill and contract phase

Backfill runs in bounded batches under the migration service role and records a
checkpoint. The exact mapping is:

| Legacy `agent_runs.state` | `execution_state` | `capability_state` | `review_state` |
| --- | --- | --- | --- |
| `pending` | `queued` | `enabled` | `not_required` |
| `running` | `running` | `enabled` | `not_required` |
| `succeeded` | `succeeded` | `enabled` | `not_required` |
| `failed` | `failed` | `failed` | `not_required` |
| `unavailable` | `blocked` | `dependency_not_ready` | `not_required` |
| `abstained` | `waiting_review` | `enabled` | `pending` |
| `stale` | `stale` | `stale` | `not_required` |
| `cancelled` | `cancelled` | `enabled` | `not_required` |
| `reviewed` | `succeeded` when a review decision exists; otherwise `waiting_review` | `enabled` | `accepted`/`rejected`/`edited` from the review row; otherwise `pending` |

The backfill statement is idempotent and preserves the original value:

```sql
UPDATE careerops.agent_runs
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
```

The migration must execute a preflight query that counts states outside the
nine recognized values, abort when the count is non-zero, and verify zero null
canonical fields before the contract phase. The explicit `ELSE NULL` is only a
temporary expand value; it is never accepted by the tighten check.

After the verification checkpoint, the contract phase tightens the nullable
expand fields in one short transaction:

```sql
ALTER TABLE careerops.agent_runs
  ALTER COLUMN execution_state SET DEFAULT 'queued',
  ALTER COLUMN execution_state SET NOT NULL,
  ALTER COLUMN capability_state SET DEFAULT 'enabled',
  ALTER COLUMN capability_state SET NOT NULL,
  ALTER COLUMN review_state SET DEFAULT 'not_required',
  ALTER COLUMN review_state SET NOT NULL,
  ALTER COLUMN current_attempt SET DEFAULT 0,
  ALTER COLUMN current_attempt SET NOT NULL;
```

During the rolling window, a `careerops.normalize_agent_legacy_state()`
security-definer function maps only the table values, and a compatibility
trigger dual-writes recognized legacy `state` and canonical fields in one
transaction. Unknown values raise SQLSTATE `22023`; they cannot become
success. New writers update both representations. New application code refuses
to start against a schema before 0030. Before direct table grants are revoked,
the old application is switched to the dedicated compatibility functions
`careerops.read_agent_legacy_state(uuid,uuid)` and
`careerops.write_agent_legacy_state(uuid,uuid,varchar,jsonb)`, which only read
or update recognized legacy state through the same mapping/trigger. Old code
may therefore continue during the window without direct access to canonical
tables or the audit table; a rollout that has not installed this adapter is
blocked rather than pretending compatibility.

After all workers read canonical fields, the trigger is retained for the
compatibility window and legacy writers are disabled by feature flag. Only a
separate forward migration may remove it. Active runs are reconciled before
the old writer is retired.

## 3. CAS functions, privileges, and audit

The API role does not receive direct `INSERT/UPDATE/DELETE` on
`agent_stage_events` or the audit table. The migration creates these
security-definer functions with a fixed `search_path`:

- `careerops.acquire_agent_attempt(uuid,uuid,uuid,uuid,timestamptz)` returning
  `(lease_epoch bigint,cancel_epoch bigint,lease_expires_at timestamptz)`;
- `careerops.renew_agent_attempt(uuid,uuid,uuid,uuid,bigint,bigint,timestamptz)`
  returning `(lease_epoch bigint,lease_expires_at timestamptz)`;
- `careerops.append_agent_stage_event(uuid,uuid,uuid,uuid,uuid,bigint,bigint,uuid,integer,varchar,varchar,varchar,varchar,boolean,varchar,boolean,timestamptz,integer,jsonb,timestamptz)`
  returning `(stage_event_id uuid,event_key uuid,sequence integer)`;
- `careerops.reconcile_agent_run(uuid,uuid,uuid,uuid,bigint,bigint,varchar,varchar,varchar,timestamptz)`
  returning `(execution_state varchar,capability_state varchar,review_state varchar)`;
- `careerops.reserve_agent_idempotency(varchar,uuid,varchar,uuid,varchar,varchar,varchar,timestamptz)`
  returning `(receipt_id uuid,replayed boolean,existing_body_hash varchar,existing_receipt jsonb)`;
- `careerops.finalize_agent_idempotency(uuid,smallint,jsonb)` returning
  `(receipt_id uuid)`;
- `careerops.purge_agent_console(timestamptz)` returning
  `(purged bigint,skipped_legal_hold bigint,failed bigint)`;
- `careerops.read_agent_legacy_state(uuid,uuid)` returning the bounded legacy
  projection `(state varchar,result jsonb)`;
- `careerops.write_agent_legacy_state(uuid,uuid,varchar,jsonb)` returning
  `(execution_state varchar,capability_state varchar,review_state varchar)`.

The positional parameters are frozen, in order: acquire is
`run_id,candidate_id,attempt_id,worker_id,now`; renew is
`attempt_id,run_id,candidate_id,worker_id,lease_epoch,cancel_epoch,now`;
append is `stage_event_id,attempt_id,run_id,candidate_id,worker_id,lease_epoch,
cancel_epoch,event_key,sequence,schema_version,stage,status,cause,terminal,
provider_state,retryable,occurred_at,duration_ms,redacted_payload,
retention_until`; reconcile is
`run_id,candidate_id,attempt_id,worker_id,lease_epoch,cancel_epoch,
execution_state,capability_state,review_state,finished_at`. Every parent ID,
worker ID, epoch, event key, and candidate ID is checked against the same row
inside the function transaction. Idempotency reserve is
`actor_id,candidate_id,resource_type,resource_id,operation,idempotency_key,
canonical_body_hash,expires_at`; finalize is `receipt_id,response_status,receipt`.

Acquire/renew/append require `worker_id`, `lease_epoch`, `cancel_epoch`, an
unexpired lease, and the composite parent scope. Terminal reconciliation uses
the same predicates and affected-row check specified in
`temporal-contract.md`. A stale or cancelled worker receives
`FENCED_OR_CANCELLED` and cannot write a terminal success.

Required privilege shape after migration:

```sql
REVOKE ALL ON careerops.agent_runs, careerops.agent_run_reviews,
  careerops.agent_contexts, careerops.agent_actions, careerops.agent_attempts,
  careerops.agent_stage_events, careerops.agent_idempotency_receipts FROM careerops_api;
REVOKE INSERT, UPDATE, DELETE ON careerops.audit_events FROM careerops_api;
GRANT SELECT ON careerops.v_agent_runs_candidate,
  careerops.v_agent_contexts_candidate, careerops.v_agent_actions_candidate,
  careerops.v_agent_attempts_candidate, careerops.v_agent_stage_events_candidate
  TO careerops_api;
GRANT EXECUTE ON FUNCTION careerops.acquire_agent_attempt(uuid,uuid,uuid,uuid,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION careerops.renew_agent_attempt(uuid,uuid,uuid,uuid,bigint,bigint,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION careerops.append_agent_stage_event(uuid,uuid,uuid,uuid,uuid,bigint,bigint,uuid,integer,varchar,varchar,varchar,varchar,boolean,varchar,boolean,timestamptz,integer,jsonb,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION careerops.reconcile_agent_run(uuid,uuid,uuid,uuid,bigint,bigint,varchar,varchar,varchar,timestamptz) TO careerops_worker;
GRANT EXECUTE ON FUNCTION careerops.reserve_agent_idempotency(varchar,uuid,varchar,uuid,varchar,varchar,varchar,timestamptz) TO careerops_api;
GRANT EXECUTE ON FUNCTION careerops.finalize_agent_idempotency(uuid,smallint,jsonb) TO careerops_api;
GRANT EXECUTE ON FUNCTION careerops.purge_agent_console(timestamptz) TO careerops_retention;
GRANT EXECUTE ON FUNCTION careerops.read_agent_legacy_state(uuid,uuid) TO careerops_legacy_agent;
GRANT EXECUTE ON FUNCTION careerops.write_agent_legacy_state(uuid,uuid,varchar,jsonb) TO careerops_legacy_agent;
```

The function bodies use a fixed `search_path = careerops, pg_catalog`, check
the caller role, perform the predicates from `temporal-contract.md`, and
return zero rows/raise a typed error on a fence miss. No wildcard `EXECUTE` or
base-table mutation or read grant is allowed. The five candidate views accept
the authenticated candidate UUID through a security-definer function or a
transaction-local setting established by the API; a client cannot select the
candidate predicate.

Stage events are append-only at both privilege and trigger layers:

```sql
CREATE OR REPLACE FUNCTION careerops.reject_agent_stage_event_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = careerops, pg_catalog AS $$
BEGIN
  RAISE EXCEPTION 'agent stage events are append-only' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER agent_stage_events_append_only
BEFORE UPDATE OR DELETE ON careerops.agent_stage_events
FOR EACH ROW EXECUTE FUNCTION careerops.reject_agent_stage_event_mutation();
```

The `agent_idempotency_receipts` table is claimed in the same transaction as
every action/context/run/crawl/package adapter mutation. A same-hash row
returns its stored receipt; a different hash raises `IDEMPOTENCY_CONFLICT`.
Receipt rows expire after 24 hours through the retention function and never
authorize a different candidate/resource.

Candidate reads use security-definer functions/views with an authenticated
candidate parameter. API table grants alone are not row isolation.

Every mutation calls `careerops.append_audit_event` on the same database
connection and transaction as the business write. If audit append, policy,
budget, or the business write fails, all effects roll back. Audit events are
append-only and include actor/session/request/trace IDs, candidate/resource,
old/new state, policy/consent/field-set digests, reason, and server time.

## 4. Retention, legal hold, and rollback-forward

`purge_agent_console(now)` takes a PostgreSQL advisory lock; selects due rows
with `FOR UPDATE SKIP LOCKED`; skips rows with
`legal_hold_until > now`; deletes/ redacts only rows owned by the retention
role; and returns `{purged, skipped_legal_hold, failed}` counters. It never
deletes audit rows. The hourly job retries three times with exponential
backoff, emits an alert payload containing `trace_id`, failed count, oldest due
timestamp, and migration revision, then exits non-zero if still failing.

Trace/stage data is retained 90 days, preview metadata 30 days, and audit
events 365 days unless a legal/operational hold is recorded. Purge is tested
for lock contention, retry, legal hold, partial failure, idempotent rerun, and
no API-role bypass.

Production never performs a destructive downgrade. Rollback-forward disables
new scheduling, lets running attempts stop/reconcile, restores the previous
read projection from `legacy_state` where needed, and preserves all
stage/audit history. The Alembic downgrade first checks that all Agent tables
are empty; only then does it remove 0030 views, functions, tables, constraints,
and compatibility columns and restore the 0025 foreign key and grants. A
non-empty database fails closed with a rollback-forward error. A rollback
report records the revision, stopped workflow IDs, pending attempts, data
checksums, and operator approval. `DB-01` must run upgrade from 0029,
old/new rolling compatibility, backfill, constraint/privilege checks, purge,
and the rollback-forward procedure on disposable PostgreSQL.
