# Agent Temporal contract

This annex freezes the durable execution wire contract. Temporal is an
orchestrator only: workflow code is deterministic and JSON-only; all network,
model, browser, and database effects happen in activities behind the policy
and repository boundaries.

## 1. Workflow identity and input/output

Task queue: `careerops-agent`. Workflow type: `AgentRunWorkflow`. Workflow ID:
`agent-run:{candidate_id}:{logical_run_id}`. `logical_run_id` is a server UUID;
the API returns `ATTACH_EXISTING` for the same candidate/logical run and
`REJECT_DUPLICATE` for a terminal run with a different request.

The exact workflow input is:

```json
{
  "candidate_id": "uuid",
  "logical_run_id": "uuid",
  "context_id": "uuid",
  "operation": "job_matching",
  "context_digest": "sha256",
  "policy_version": "policy-v1",
  "consent_id": "uuid",
  "trace_id": "uuid",
  "request_fingerprint": "sha256"
}
```

`operation` is the `agent_run_operation` subset: `job_matching`, `resume_review`,
or `interview_preparation`. `consent_id` is required for an enabled model stage;
it is absent only for deterministic-only runs. The workflow rejects unknown
fields, wrong types, a non-UUID ID, an expired context, or a mismatched
request fingerprint before its first activity.

The workflow result is:

```json
{
  "run_id": "uuid", "candidate_id": "uuid", "logical_run_id": "uuid",
  "execution_state": "succeeded", "capability_state": "enabled",
  "review_state": "pending", "current_attempt": 1,
  "last_event_key": "uuid", "trace_id": "uuid"
}
```

No workflow result contains raw prompts, provider responses, browser HTML,
cookies, credentials, resume bytes, mailbox text, or a candidate ID in a
candidate-facing list response. The query projection removes
`candidate_id` after server-side authorization.

Wire notation in this annex is exact: `uuid` means an RFC 4122 UUID string,
`sha256` means lower-case 64-character hexadecimal, `timestamp` means an RFC
3339 UTC string ending in `Z`, and every `enum` below is replaced by the finite
literal set shown in that row. These are type names, not open placeholders.

## 2. Activity contracts

Every activity receives an envelope with exactly these fields:

```json
{
  "run_id": "uuid", "candidate_id": "uuid", "attempt_id": "uuid",
  "lease_epoch": 3, "cancel_epoch": 0, "trace_id": "uuid",
  "input_digest": "sha256"
}
```

The activity-specific payload is nested under `payload`; it is redacted before
Temporal history persistence. Each activity returns the following receipt
shape, with `output` restricted to its row below:

```json
{
  "attempt_id": "uuid", "lease_epoch": 3, "cancel_epoch": 0,
  "stage": "resolve_agent_context", "event_key": "uuid", "sequence": 1,
  "outcome": "completed", "next_state": "running", "output": {},
  "trace_id": "uuid"
}
```

| Activity | Exact `payload` input | Exact `output` keys | Side effects and timeout |
| --- | --- | --- | --- |
| `resolve_agent_context` | `{"context_id":"00000000-0000-0000-0000-000000000001","operation":"resume_review","expected_digest":"<64 lower-case hex>"}` | `context_id`, `source_version_snapshot`, `source_digests`, `allowed_operation`, `state`, `expires_at` | candidate-scoped read only; 30s, no retry on `CONTEXT_STALE` |
| `run_deterministic_stage` | `{"stage":"normalize","context_digest":"<64 lower-case hex>","input_refs":[{"type":"job","id":"00000000-0000-0000-0000-000000000002","version":1}]}`; `stage` is one of `normalize`, `filter`, `rank`, `prepare`, and ref type is one of `job`, `evidence`, `profile` | `stage`, `result_digest`, `result_refs`, `verdict`, `unknowns` | deterministic repository read; 60s, at most 2 retries with jitter |
| `invoke_model_stage` | `{"operation":"resume_review","context_digest":"<64 lower-case hex>","consent_id":"00000000-0000-0000-0000-000000000003","preflight_id":"00000000-0000-0000-0000-000000000004","field_set_hash":"<64 lower-case hex>","provider_policy_version":"policy-v1"}`; operation is one of `job_matching`, `resume_review`, `interview_preparation` | `provider_id`, `model_id`, `response_digest`, `schema_version`, `prompt_version`, `field_set_hash`, `usage`, `proposal_digest`, `uncertainties` | mandatory preflight, fake/provider gateway only; 90s, at most 1 retry and never after consent/policy drift |
| `persist_agent_stage` | `{"stage":"invoke_model_stage","event_key":"00000000-0000-0000-0000-000000000005","sequence":1,"outcome":"completed","redacted_payload":{},"result_digest":"<64 lower-case hex>"}`; stage is the finite activity stage name and outcome is `completed`, `blocked`, or `failed` | `stage_event_id`, `event_key`, `sequence`, `stored_state`, `audit_receipt_id` | one DB transaction with audit; 30s, retry only with same receipt/CAS |
| `reconcile_agent_run` | `{"expected_attempt_id":"00000000-0000-0000-0000-000000000006","expected_lease_epoch":3,"expected_cancel_epoch":0,"terminal_state":"succeeded"}`; terminal state is one of `succeeded`, `failed`, `cancelled`, `stale`, `blocked` | `execution_state`, `capability_state`, `review_state`, `current_attempt`, `reconciled_event_key` | CAS terminal projection and audit; 30s, no blind retry |

The output value types are closed as follows:

```json
// run_deterministic_stage.output
{"stage":"normalize", "result_digest":"<64 lower-case hex>",
 "result_refs":[{"type":"job","id":"uuid","version":1,"digest":"<64 lower-case hex>"}],
 "verdict":"pass|fail|unknown", "unknowns":["bounded reason"]}

// invoke_model_stage.output
{"provider_id":"provider-key", "model_id":"model-key",
 "response_digest":"<64 lower-case hex>", "schema_version":"schema-v1",
 "prompt_version":"prompt-v1", "field_set_hash":"<64 lower-case hex>",
 "usage":{"input_tokens":0,"output_tokens":0,"latency_ms":0},
 "proposal_digest":"<64 lower-case hex>", "uncertainties":["bounded reason"]}

// persist_agent_stage.output
{"stage_event_id":"uuid", "event_key":"uuid", "sequence":1,
 "stored_state":"completed|blocked|failed", "audit_receipt_id":"uuid"}

// reconcile_agent_run.output
{"execution_state":"queued|running|waiting_review|succeeded|failed|cancel_requested|cancelled|stale|blocked",
 "capability_state":"enabled|disabled_by_policy|not_configured|dependency_not_ready|blocked_by_prerequisite|stale|failed",
 "review_state":"not_required|pending|accepted|rejected|edited", "current_attempt":1,
 "reconciled_event_key":"uuid"}
```

`result_refs` contains IDs/version/digests only; `usage` is non-negative and
bounded by the budget contract; `unknowns`/`uncertainties` are arrays of at
most 20 safe reason codes/messages, never raw model/source text.

Activity payloads carry IDs/digests, not source content. `invoke_model_stage`
gets its allowlisted field envelope from the server-side context resolver; an
activity cannot manufacture a context or provider policy.

## 3. Lease, fencing, and cancellation SQL

The database is the fencing authority. Worker identity is a server-issued
UUID bound to the worker's mTLS identity and task queue; a client cannot supply
it. A lease lasts 30 seconds and is renewed every 10 seconds. Acquire is the
only operation that increments `lease_epoch`:

```sql
UPDATE careerops.agent_attempts
SET worker_id = :worker_id,
    lease_epoch = lease_epoch + 1,
    lease_expires_at = :now + interval '30 seconds',
  state = 'running'
WHERE id = :attempt_id
  AND run_id = :run_id
  AND candidate_id = :candidate_id
  AND state IN ('queued', 'running')
  AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
RETURNING lease_epoch, cancel_epoch;
```

Renew and every stage append use all fencing predicates:

```sql
UPDATE careerops.agent_attempts
SET lease_expires_at = :now + interval '30 seconds'
WHERE id = :attempt_id AND run_id = :run_id
  AND worker_id = :worker_id AND lease_epoch = :lease_epoch
  AND cancel_epoch = :cancel_epoch AND state = 'running'
  AND lease_expires_at > :now;

INSERT INTO careerops.agent_stage_events (
  id, attempt_id, run_id, candidate_id, event_key, sequence,
  schema_version, stage, status, cause, terminal, provider_state,
  retryable, occurred_at, duration_ms, redacted_payload, retention_until
)
SELECT
  :stage_event_id, :attempt_id, :run_id, :candidate_id, :event_key, :sequence,
  :schema_version, :stage, :status, :cause, :terminal, :provider_state,
  :retryable, :occurred_at, :duration_ms, :redacted_payload, :retention_until
WHERE EXISTS (
  SELECT 1 FROM careerops.agent_attempts
  WHERE id = :attempt_id AND run_id = :run_id
    AND worker_id = :worker_id AND lease_epoch = :lease_epoch
    AND cancel_epoch = :cancel_epoch AND state IN ('running','waiting_review')
    AND lease_expires_at > :now
);
```

The insert and the existence check are implemented by one security-definer
function/transaction; an affected-row count of zero returns
`FENCED_OR_CANCELLED` and cannot be interpreted as success. `persist_agent_stage`
uses a unique `(attempt_id, sequence)` and `event_key`; a duplicate identical
receipt is a replay, while a different payload is `409 EVENT_KEY_CONFLICT`.

`retry_budget` is the remaining retry count, initialized to `2` for a new
attempt. A retry consumes one unit in the same transaction that creates the
next attempt; a failed reservation leaves the old value unchanged. When the
value is zero, the workflow persists `dead_lettered_at`,
`dead_letter_reason=RETRY_BUDGET_EXHAUSTED`, emits a terminal failed stage
event, and raises a non-retryable error. No in-memory counter is authoritative.

Stop is a transaction that first verifies the candidate and expected state,
increments `cancel_epoch`, sets `execution_state=cancel_requested`, and
appends audit; only then does the workflow receive `stop`.

## 4. Signals and queries

Signal names and payloads are fixed:

```json
// stop
{"reason_code":"USER_STOPPED|POLICY_REVOKED|OPERATOR_STOPPED","request_id":"uuid"}

// refresh_context
{"context_id":"uuid","context_digest":"sha256","source_event_key":"uuid"}
```

`stop` is idempotent. A late stop after a terminal state returns a terminal
receipt and does not reopen the run. `refresh_context` is accepted only while
`queued`, `running`, or `waiting_review`; it causes a new context resolution
and invalidates pending model consent if any source digest changed.

The redacted `status` query returns:

```json
{
  "run_id":"uuid", "execution_state":"running", "capability_state":"enabled",
  "review_state":"not_required", "current_attempt":1,
  "latest_stage":"run_deterministic_stage", "completed":1, "total":3,
  "retryable":false, "failure_code":null, "trace_id":"uuid"
}
```

The query never returns worker ID, provider request, raw error, prompt,
response, or lease token. The workflow query is read-only and does not refresh
leases or perform a database write.

## 5. Errors, retries, and recovery

Typed non-retryable errors are `CONTEXT_STALE`, `POLICY_DENIED`,
`CONSENT_REVOKED`, `BUDGET_EXCEEDED`, `FENCED_OR_CANCELLED`,
`OUTPUT_SCHEMA_INVALID`, `SOURCE_NOT_AUTHORIZED`, `DUPLICATE_TERMINAL`, and
`AUDIT_FAILED`. Retryable errors are only `DEPENDENCY_NOT_READY`, transient
database unavailability, and provider `429/5xx` after the gateway has
rechecked policy. Backoff is exponential with full jitter, maximum two
activity attempts unless the table above says one; no retry can bypass a
preflight or consent check.

Heartbeat details are redacted and sent every 10 seconds. A missing heartbeat
for 30 seconds lets another worker acquire the lease. Cancellation is allowed
30 seconds for graceful activity completion; after that, the old epoch is
fenced and the run is `cancelled` or `failed` according to the persisted
receipt, never `succeeded`.

Workflow history retention is 30 days and contains only digests, IDs, bounded
status, and typed codes. Stage events are retained according to the migration
contract. The `TEMP-02` gate must prove replay determinism, old-worker fencing,
lease expiry takeover, cancel race, duplicate receipt, dead-letter alert, and
retry-budget accounting.
