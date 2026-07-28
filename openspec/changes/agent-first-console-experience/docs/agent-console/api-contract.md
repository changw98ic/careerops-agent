# Agent console API contract

This annex freezes the HTTP contract for the change. It is normative alongside
the four capability specs. Existing domain endpoints remain authoritative; the
agent console may add a projection or an adapter, but it may not create a
second source of truth.

## 1. Wire conventions

- All paths are under `/api/v1`; all bodies are UTF-8 JSON. `Content-Type` is
  required on requests with a body and is `application/json` on success/error
  responses.
- IDs are UUID strings. Timestamps are RFC 3339 UTC strings with a `Z`
  suffix. Hashes are lower-case SHA-256 hex. Unknown fields are rejected with
  `400 INVALID_REQUEST`; clients must not depend on field order.
- `candidate_id` is never accepted as an authority input. The server resolves
  it from `careerops_session` and rejects a conflicting body/query value with
  `400 CANDIDATE_SCOPE_INPUT`.
- Mutations require `Origin` equal to the configured console origin,
  `X-CSRF-Token` equal to the non-HttpOnly `careerops_csrf` cookie,
  `Idempotency-Key` matching `^[A-Za-z0-9._~-]{1,128}$`, and an authenticated
  session cookie. A missing or mismatched `Origin` is `403 ORIGIN_DENIED`; a
  missing/mismatched token is `403 CSRF_DENIED`.
- The idempotency key is bound to actor, server candidate, resource, operation,
  and canonical body hash. A replay with the same hash returns the original
  response and `Idempotency-Replayed: true`; a different hash returns
  `409 IDEMPOTENCY_CONFLICT`. Records live for 24 hours.
- Collection mutations without a resource UUID bind the receipt to the fixed
  zero UUID `00000000-0000-0000-0000-000000000000` plus the resource type; the
  client cannot choose a different sentinel.
- All responses containing candidate content, source content, traces, previews,
  runs, packages, or audit data set `Cache-Control: no-store` and
  `Pragma: no-cache`. Error responses use the same headers whenever a request
  was authenticated. No response may put sensitive values in a URL.
- List cursors are opaque base64url (without padding) encodings of
  `{\"v\":1,\"sort\":\"created_at.desc,id.desc\",\"last\":{...},\"scope\":\"<candidate>\"}`
  signed with the server cursor key. A cursor with the wrong scope, version,
  signature, or age over 15 minutes returns `400 CURSOR_INVALID`; `limit` is
  an integer from 1 to 50, and the action queue hard-caps it at 3.
- Every response includes `trace_id`. Mutating success responses also include
  `receipt_id` and `idempotency_key`.

## 2. Common response types

```json
{
  "trace_id": "uuid",
  "code": "ERROR_CODE",
  "message": "stable Chinese-first safe message",
  "retryable": false,
  "fields": {"path": "validation detail"}
}
```

The `message` is safe for a candidate and never contains SQL, provider output,
URLs, credentials, raw source text, or another candidate's existence. The
following codes are stable: `UNAUTHENTICATED`, `NOT_FOUND`, `POLICY_DENIED`,
`DEPENDENCY_NOT_READY`, `PREREQUISITE_BLOCKED`, `CONTEXT_STALE`,
`PREFLIGHT_REQUIRED`, `CONSENT_REQUIRED`, `BUDGET_EXCEEDED`, `STALE`,
`NOT_RETRYABLE`, `STOP_NOT_ALLOWED`, `INVALID_REQUEST`, `CSRF_DENIED`,
`ORIGIN_DENIED`, `IDEMPOTENCY_CONFLICT`, `CURSOR_INVALID`, `AUDIT_FAILED`,
`RATE_LIMITED`, and `INTERNAL_SAFE_FAILURE`.

Every mutation receipt contains this common base shape. Each endpoint-specific
receipt may add only the bounded fields explicitly frozen below; it may not
remove any common field or invent an unbounded payload.

```json
{
  "trace_id": "uuid",
  "receipt_id": "uuid",
  "idempotency_key": "ui-action-01",
  "resource_type": "agent_action",
  "resource_id": "uuid",
  "state": "accepted",
  "accepted_at": "2026-07-28T00:00:00Z"
}
```

## 3. New agent-console endpoints

The following table is the endpoint-level contract. “Sensitive” means both
`no-store` headers are mandatory.

| Method and path | Request | Success | Required errors | Cache/auth |
| --- | --- | --- | --- | --- |
| `GET /api/v1/agent-console/actions?cursor=&limit=3` | query only; no candidate query | `200 ActionPage` | `401`, `400 CURSOR_INVALID`, `503 DEPENDENCY_NOT_READY` | sensitive/session |
| `POST /api/v1/agent-console/actions/{action_key}/accept` | `AcceptAction` | `200 ActionReceipt` | `404`, `409 STALE`, `422 PREREQUISITE_BLOCKED`, `403` | mutation |
| `POST /api/v1/agent-console/actions/{action_key}/snooze` | `SnoozeAction` | `200 ActionReceipt` | `404`, `409 STALE`, `422 INVALID_REQUEST` | mutation |
| `POST /api/v1/agent-console/actions/{action_key}/dismiss` | `DismissAction` | `200 ActionReceipt` | `404`, `409 STALE` | mutation |
| `POST /api/v1/agent-console/actions/{action_key}/complete` | `CompleteAction` | `200 ActionReceipt` | `404`, `409 STALE`, `422 PREREQUISITE_BLOCKED` | mutation |
| `POST /api/v1/agent-console/contexts` | `CreateContext` | `201 Context` | `403`, `404`, `409 CONTEXT_STALE`, `422` | sensitive/mutation |
| `GET /api/v1/agent-console/contexts/{context_id}` | path ID | `200 Context` | `404` | sensitive/session |
| `GET /api/v1/capabilities/agent?operation=` | operation enum | `200 Capability` | `400`, `403`, `503` | redacted/session |
| `POST /api/v1/agent-console/preflight` | `PreflightRequest` | `200 Preflight` | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `422`, `503` | sensitive/mutation |
| `POST /api/v1/agents/job-matching` | `StartRun` | `202 RunReceipt` | `403 PREFLIGHT_REQUIRED`, `403 POLICY_DENIED`, `409`, `422`, `503` | sensitive/mutation |
| `POST /api/v1/agents/resume-review` | `StartRun` | `202 RunReceipt` | `403 PREFLIGHT_REQUIRED`, `403 POLICY_DENIED`, `409`, `422`, `503` | sensitive/mutation |
| `POST /api/v1/agents/interview-preparation` | `StartRun` | `202 RunReceipt` | `403 PREFLIGHT_REQUIRED`, `403 POLICY_DENIED`, `409`, `422`, `503` | sensitive/mutation |
| `GET /api/v1/agents/runs?cursor=&limit=&capability=` | list query | `200 RunPage` | `400`, `401` | sensitive/session |
| `GET /api/v1/agents/runs/{run_id}` | path ID | `200 Run` | `404` | sensitive/session |
| `GET /api/v1/agents/runs/{run_id}/stages?cursor=&limit=` | list query | `200 StagePage` | `404`, `400` | sensitive/session |
| `GET /api/v1/agents/runs/{run_id}/reviews` | path ID | `200 ReviewPage` | `404`, `503` | sensitive/session |
| `POST /api/v1/agents/runs/{run_id}/retry` | `RetryRun` | `202 RunReceipt` | `404`, `409 NOT_RETRYABLE`, `422`, `403` | sensitive/mutation |
| `POST /api/v1/agents/runs/{run_id}/stop` | `StopRun` | `202 RunReceipt` | `404`, `409 STOP_NOT_ALLOWED` | sensitive/mutation |
| `POST /api/v1/agents/runs/{run_id}/review` | `ReviewRun` | `200 ReviewReceipt` | `404`, `409 STALE`, `422` | sensitive/mutation |
| `GET /api/v1/crawl-runs` | `cursor`, `limit`, optional `plan_version_id` | `200 CrawlRunPage` | `401`, `400 CURSOR_INVALID` | sensitive/session |
| `GET /api/v1/crawl-runs/{run_id}` | path ID | `200 CrawlRun` | `404 NOT_FOUND` | sensitive/session |
| `GET /api/v1/crawl-runs/{run_id}/stages?cursor=&limit=` | query only | `200 CrawlStagePage` | `404`, `400 CURSOR_INVALID` | sensitive/session |
| `POST /api/v1/crawl-runs/{run_id}/retry` | `RetryCrawlRun` | `202 RunReceipt` | `404`, `409 NOT_RETRYABLE`, `422 SOURCE_POLICY_DENIED` | sensitive/mutation |
| `POST /api/v1/crawl-runs/{run_id}/stop` | `StopCrawlRun` | `202 RunReceipt` | `404`, `409 STOP_NOT_ALLOWED` | sensitive/mutation |

There are two explicit operation types. `model_operation` is one of
`job_matching`, `resume_review`, `interview_preparation`, or
`smart_form_intake` and is used by provider policy, consent, preflight, and
egress. `agent_run_operation` is the strict subset
`job_matching|resume_review|interview_preparation` and is accepted by Agent
workflow/start/retry routes. `smart_form_intake` remains preview-only through
the existing adapter and cannot start an Agent run.
Accordingly, `GET /api/v1/capabilities/agent?operation=` and
`POST /api/v1/agent-console/preflight` accept `model_operation`, while
`POST /api/v1/agents/job-matching`, `POST /api/v1/agents/resume-review`,
`POST /api/v1/agents/interview-preparation`, workflow input, and Agent retry
accept only `agent_run_operation`.
The URL slugs are explicit compatibility aliases: `resume-review` maps to
`resume_review`, `interview-preparation` maps to
`interview_preparation`, and `job-matching` (if exposed) maps to
`job_matching`; no other slug is accepted.

### 3.1 Exact JSON bodies and responses

```json
// AcceptAction, CompleteAction
{"expected_queue_version": 12, "client_event_id": "uuid"}

// SnoozeAction
{"expected_queue_version": 12, "until": "2026-07-29T00:00:00Z", "client_event_id": "uuid"}

// DismissAction
{"expected_queue_version": 12, "reason_code": "NOT_RELEVANT", "client_event_id": "uuid"}

// CreateContext
{
  "operation": "resume_review",
  "job_id": "uuid",
  "job_version": 4,
  "profile_version_id": "uuid",
  "resume_version_id": "uuid",
  "evidence_claim_ids": ["uuid"],
  "source_refs": [{"type": "job", "id": "uuid", "version": 4}]
}

// PreflightRequest
{"context_id": "uuid", "operation": "resume_review"}

// StartRun
{"context_id": "uuid", "operation": "resume_review", "preflight_id": "uuid"}

// RetryRun
{"expected_state": "failed", "context_id": "uuid", "preflight_id": "uuid"}

// StopRun
{"expected_state": "running", "reason_code": "USER_STOPPED"}

// ReviewRun
{
  "expected_run_revision": 7,
  "decision": "accepted",
  "field_decisions": [{"path": "summary", "decision": "keep"}],
  "preview_id": "uuid",
  "context_digest": "sha256",
  "note": "保留这条建议"
}
```

`Context` is:

```json
{
  "context_id": "uuid", "operation": "resume_review", "schema_version": "agent-context-v1",
  "source_version_snapshot": {"job": 4, "profile": 2, "resume": 8, "evidence": 3},
  "source_digests": {"job": "sha256", "profile": "sha256", "resume": "sha256"},
  "allowed_operation": "resume_review", "created_at": "timestamp",
  "expires_at": "timestamp", "state": "active", "invalidation_reason": null
}
```

`ActionPage`, `Capability`, `Preflight`, `Run`, and `StagePage` are frozen as
follows. The implementation may add only fields explicitly marked `metadata`
and must keep them redacted and bounded.

`ActionPage.items[].state` and `ActionReceipt.state` are the closed enum
`proposed | accepted | snoozed | dismissed | completed | expired | blocked`.
`open`, `pending`, or an unknown value is invalid and cannot be serialized.

```json
// ActionPage
{"queue_version":12, "generated_at":"timestamp", "user_goal_scope":"job_search",
 "notification_policy":{"surface":"in_app","quiet_hours":false}, "items":[{
  "action_key": "resume-review:job:uuid:resume:8", "kind": "resume_review",
  "title": "检查这份简历与岗位的匹配度", "reason_code": "NEW_CONTEXT",
  "priority": "high", "target_route": "/ai-workbench?tab=resume",
  "deterministic_rank":1, "prerequisites":{"status":"ready","missing":[]},
  "context_id": "uuid", "source_refs": [{"type": "job", "id": "uuid"}],
  "source_event_key":"uuid", "source_freshness":"timestamp",
  "state": "proposed", "created_at": "timestamp", "expires_at": null
}], "next_cursor": null, "trace_id": "uuid"}

// Capability
{"operation": "resume_review", "state": "disabled_by_policy",
 "reason_code": "MODEL_PROVIDER_DISABLED", "may_start": false,
 "requires_preflight": true, "retryable": false, "trace_id": "uuid"}

// Preflight
{"preflight_id":"uuid", "operation":"resume_review", "context_id":"uuid",
 "decision":"blocked", "capability_state":"disabled_by_policy",
 "reason_code":"MODEL_PROVIDER_DISABLED", "field_set_hash":null,
 "redaction_version":"egress-redaction-v1", "allowed_fields":[],
 "budget":{"run_calls_remaining":0,"candidate_calls_remaining":0,
   "input_tokens_remaining":0,"output_tokens_remaining":0},
 "consent_scope":{"candidate_id":"omitted","source_digests":[],
   "expires_at":null}, "consent_id":null, "consent_issued":false,
 "provider_policy_version":null, "expires_at":"timestamp", "trace_id":"uuid"}

For `decision="ready"`, `capability_state="enabled"`,
`field_set_hash` is a 64-character SHA-256, `allowed_fields` is the exact
closed redacted field list, the budget values are positive/remaining as
applicable, `consent_scope.source_digests` is non-empty,
`consent_scope.expires_at` is exactly 15 minutes after issuance, and
`consent_id` is a UUID with `consent_issued=true`. For every blocked decision,
`consent_id` is null and no provider call is permitted.

// Run
{"run_id": "uuid", "candidate_id": "omitted", "operation": "resume_review",
 "execution_state": "waiting_review", "capability_state": "enabled",
 "review_state": "pending", "context_id": "uuid", "trace_id": "uuid",
 "current_attempt": 1, "progress": {"completed": 2, "total": 3},
 "latest_stage": "persist_agent_stage", "failure": null,
 "created_at": "timestamp", "started_at": "timestamp", "finished_at": null}

// StagePage
{"items": [{"stage_event_id": "uuid", "sequence": 1, "attempt": 1,
  "schema_version": "stage-v1", "stage": "invoke_model_stage",
  "status": "blocked", "terminal": true, "retryable": false,
  "provider_state": "disabled", "duration_ms": 0, "cause_code": "POLICY_DENIED",
  "source_refs": [], "message": "模型能力已关闭", "occurred_at": "timestamp"}],
 "next_cursor": null, "trace_id": "uuid"}
```

The `candidate_id` omission in `Run` is deliberate. `source_refs` contain IDs
only and are filtered through ownership; raw job/resume/evidence content never
appears in the run or action response.

An action mutation returns this exact `ActionReceipt` (the common receipt
fields are retained and the action fields are required):

```json
{
  "trace_id":"uuid", "receipt_id":"uuid", "idempotency_key":"action-01",
  "resource_type":"agent_action", "resource_id":"uuid",
  "action_key":"resume-review:job:uuid:resume:8", "state":"accepted",
  "queue_version":12, "outcome":{"route":"/ai-workbench?tab=resume","run_id":null},
  "audit_event_id":"uuid", "idempotency_replayed":false,
  "accepted_at":"timestamp"
}
```

### 3.2 Crawl run detail, stages, retry, and stop

The crawl route is an adapter over the existing `CrawlRunWorkflow` and
`crawl_runs`/provenance repositories. It does not create `agent_runs` or a
second crawl table. `CrawlRun` is:

```json
{
  "run_id":"uuid", "plan_version_id":"uuid", "state":"running",
  "execution_state":"running", "trace_id":"uuid",
  "source_counts":{"requested":4,"succeeded":3,"blocked":1},
  "normalization":{"observed":120,"deduplicated":18,"persisted":102},
  "policy_decisions":[{"source_id":"source-1","decision":"allow","reason_code":null}],
  "next_retry_at":null, "created_at":"timestamp", "started_at":"timestamp",
  "finished_at":null, "failure":{"code":null,"retryable":false}
}
```

`CrawlStagePage` is:

```json
{"items":[{"stage_event_id":"uuid","sequence":2,"source_id":"source-1",
  "stage":"normalize_postings","status":"completed","terminal":false,
  "counts":{"input":40,"output":35},"cause_code":null,
  "retryable":false,"duration_ms":1200,"occurred_at":"timestamp"}],
 "next_cursor":null,"trace_id":"uuid"}
```

```json
// RetryCrawlRun
{"expected_state":"failed|stale","plan_version_id":"uuid","reason_code":"USER_RETRY"}

// StopCrawlRun
{"expected_state":"queued|running","reason_code":"USER_STOPPED"}
```

Both mutation bodies require the common `Origin`, `X-CSRF-Token`, and
`Idempotency-Key` headers. A successful retry/stop returns the common receipt
with `resource_type="crawl_run"`, the same logical `run_id`, and a new
attempt number for retry. The route performs an atomic expected-state check;
same-key replays return the original receipt and a changed body is
`409 IDEMPOTENCY_CONFLICT`. A stopped run cannot later be reported as success
by a fenced worker. The exact status/error matrix is:

| Endpoint | `200/202` | `404` | `409` | `422/503` |
| --- | --- | --- | --- | --- |
| `GET /api/v1/crawl-runs`, `GET /api/v1/crawl-runs/{run_id}`, `GET /api/v1/crawl-runs/{run_id}/stages` | owned redacted result | unowned/unknown `NOT_FOUND` | n/a | `503 DEPENDENCY_NOT_READY` only when the read model is unavailable |
| `POST /api/v1/crawl-runs/{run_id}/retry` | `202` receipt | `NOT_FOUND` | `NOT_RETRYABLE`, `STALE`, `IDEMPOTENCY_CONFLICT` | `SOURCE_POLICY_DENIED`, `DEPENDENCY_NOT_READY` |
| `POST /api/v1/crawl-runs/{run_id}/stop` | `202` receipt | `NOT_FOUND` | `STOP_NOT_ALLOWED`, `IDEMPOTENCY_CONFLICT` | `AUDIT_FAILED`, `DEPENDENCY_NOT_READY` |

The detail/stage reads are `no-store`; the stage payload is redacted and
contains no HTML, screenshot, cookies, authorization headers, or raw source.
The contract suite must cover a page reload, sequence ordering, source policy
denial, browser absence, duplicate retry, stop-vs-finish race, and a
cross-candidate 404.

## 3.3 Existing adapter payloads and statuses

The following adapter contracts are frozen against the existing Pydantic
models. The Agent console passes these bodies unchanged after candidate scope,
Origin/CSRF, idempotency, and capability checks; it does not invent a generic
write API.

### Smart intake

`GET /api/v1/smart-intake/previews?cursor=&limit=1..50` returns the owned
short-lived preview list as
`{items:[SmartIntakePreviewResponse],total,next_cursor,trace_id}` with
`200`; an invalid signed cursor is `400 CURSOR_INVALID`, an unauthenticated
request is `401`, and an unavailable preview repository/limiter is `503
DEPENDENCY_NOT_READY`. The response is `no-store` and never accepts a
candidate query parameter.

`POST /api/v1/smart-intake/previews` accepts exactly:

```json
{
  "target":"profile|interview_context",
  "input":{"kind":"text","text":"1..12000 characters"},
  "idempotency_key":"1..128 characters",
  "context_refs":{"profile_version_id":"uuid"},
  "interview_refs":{"canonical_job_id":"uuid","job_version_id":"uuid",
    "resume_version_id":"uuid","profile_version_id":"uuid|null","evidence_ids":["uuid"]}
}
```

`context_refs` is used only for `profile`; `interview_refs` is required for
`interview_context` and its input is at most 2,000 characters. The exact
`200 SmartIntakePreviewResponse` is
`{preview_id,candidate_id,target,state,input_digest,context_digest,fields,expires_at,model_id,prompt_version,draft_patch,decision_set_hash}`;
each field is `{path,value,value_type,confidence,status,reason,source_refs}`.
`GET /api/v1/smart-intake/capability` returns
`{released,provider_enabled,manual_fallback:true}` with `200`; missing
capability wiring is `503 DEPENDENCY_NOT_READY`.

`POST /api/v1/smart-intake/previews/{preview_id}/apply` accepts exactly
`{apply_idempotency_key,context_digest,decision_set_hash,decisions}` where each
decision is `{path,decision,value,reason}` and decision is
`accept|edit|reject|unknown`. It returns the same preview response with `200`.
Unowned/unknown preview is `404`, stale/expired/revoked is `409 STALE`, policy
or release denial is `403 POLICY_DENIED`, validation is `422`, and missing
service/rate limiter is `503`. Both preview GET and POST responses are
`no-store`; apply is a mutation and requires the common headers.

### Deterministic matching and evidence adapters

These routes remain deterministic, candidate-scoped authorities. They are
listed here so the Agent console cannot create a parallel matching/evidence
write path. Every response is `no-store`; candidate IDs in a path or body are
checked against the authenticated session and never establish authority.

| Route | Exact request/response | Statuses and controls |
| --- | --- | --- |
| `GET /api/v1/candidates?limit=1..200` | no body; `200 {items:[{id,display_name,evidence_count}],total}` | `401`, `400`; session candidate only |
| `GET /api/v1/candidates/{candidate_id}/evidence` | no body; `200 [{id,candidate_id,kind,name,description,repository,commit_sha,path,symbol,verified}]` | `401`, non-matching path `404`; substitution check |
| `POST /api/v1/evidence/import` | `{candidate_id,kind,name,description,repository,commit_sha,path,symbol,source_url}`; `201 EvidenceItem` | `401`, `403`, `422`, `503`; Origin, CSRF, idempotency, substitution check, audit |
| `GET /api/v1/jobs/{job_id}/remote-eligibility` | no body; `200 {canonical_job_id,verdict,confidence,evidence_spans,reason,rules_version}` | `404`, `503`; owned job/read-only |
| `GET /api/v1/jobs/{job_id}/compensation` | no body; `200 {id,canonical_job_id,currency,amount_min,amount_max,period,normalized_amount_min,normalized_amount_max,normalized_currency,score}` | `404`, `503`; owned job/read-only |
| `GET /api/v1/matches?candidate_id=&canonical_job_id=&cursor=&limit=1..200` | no body; `200 {items:[MatchResult],total,cursor}` | `400`, `401`; body/query candidate substitution is denied |
| `POST /api/v1/matches/run` | `{candidate_id,canonical_job_id}`; `201 MatchResult` | `401`, `403`, `404`, `422`, `503`; Origin, CSRF, idempotency, deterministic authority, audit |

`MatchResult` is exactly
`{id,candidate_id,canonical_job_id,tier,overall_score,geographic_blocked,remote_verdict,requirement_matches,rules_version,input_hash,output_hash}`;
each `requirement_matches` item is
`{requirement_name,level,confidence,reason,evidence_ids}`. The matching
adapter cannot call the model provider or update hard filter verdicts; the
Agent result is a separate review/explanation artifact.

### Crawl plan and existing crawl run adapters

`POST /api/v1/crawl-plans/versions` accepts exactly
`{sources[],themes[],include_keywords[],exclude_keywords[],role_families[],locations[],remote_rules,seniority[],compensation,content_scope,interval_seconds,timezone,per_run_limits,activate}`.
Location is `{name,kind,radius_km}`; remote rules is
`{remote_allowed,hybrid_allowed,onsite_required,timezone}`; compensation is
`{currency,amount_min,amount_max,period,equity}`; per-run limits is
`{max_postings_per_source,max_sources,timeout_seconds}`. The `candidate_id`
field is forbidden. Success is `201 CrawlPlanVersionResponse` with
`{id,version,is_active,sources,themes,include_keywords,exclude_keywords,role_families,locations,remote_rules,seniority,compensation,content_scope,interval_seconds,timezone,per_run_limits,rules_version,created_at}`.

The exact route/status matrix is:

| Route | Request | Success | Errors |
| --- | --- | --- | --- |
| `GET /api/v1/crawl-plans` | none | `200 CrawlPlanHeadResponse` (`active,next_run_at,state`) | `401`, `503` |
| `GET /api/v1/crawl-plans/versions?limit=1..200` | query | `200 {items,total}` | `400`, `401`, `503` |
| `GET /api/v1/crawl-plans/versions/{version_id}` | path UUID | `200 CrawlPlanVersionResponse` | `404`, `503` |
| `POST /api/v1/crawl-plans/versions` | body above | `201 CrawlPlanVersionResponse` | `403`, `409 INVALID_STATE`, `422`, `503` |
| `POST /api/v1/crawl-plans/versions/{version_id}/activate` | empty JSON object | `200 CrawlPlanVersionResponse` | `404`, `409 INVALID_STATE`, `503` |
| `POST /api/v1/crawl-plans/pause` | empty JSON object | `200 CrawlPlanHeadResponse` | `409 INVALID_STATE`, `503` |
| `POST /api/v1/crawl-plans/resume` | empty JSON object | `200 CrawlPlanHeadResponse` | `404`, `409 INVALID_STATE`, `503` |
| `POST /api/v1/crawl-plans/run-now` | empty JSON object | `200 RunResponse` (`id,plan_version_id,run_identity,state,source_set,error_category,created_at,started_at,ended_at,next_eligible_at`) | `409 INVALID_STATE`, `503 DEPENDENCY_NOT_READY` while leaving queued state visible |
| `GET /api/v1/crawl-runs?limit=1..200&plan_version_id=uuid` | query | `200 CrawlRunPage` | `400`, `401`, `404`, `503` |
| `GET /api/v1/crawl-runs/{run_id}` | path UUID | `200 RunResponse` including `limits` and `counters` | `404`, `503` |

`CrawlRunPage` is exactly
`{items:[CrawlRun],total,next_cursor,trace_id}`; `next_cursor` uses the signed
cursor contract even when the existing domain repository currently returns a
bounded `total`. Every plan/run mutation in this table requires `Origin`, `X-CSRF-Token`, and
`Idempotency-Key`; the adapter stores the common receipt and audit event even
when the existing domain response has no receipt field. Existing domain
errors are mapped to the stable envelope without exposing database/provider
details. Crawl-run retry/stop/stages use section 3.2 and are additive to the
existing read-only routes.

### Application package adapter

`POST /api/v1/applications/{application_id}/packages` accepts
`{resume_version_id,job_version_id|null,profile_version_id|null,cover_letter_text,notes,answers,claims,attachments}`;
claims are `{claim_text,evidence_ids[]}` and attachments are
`{name,content_hash,media_type,size_bytes}`. The exact response is
`PackageVersionResponse` with
`{id,application_id,version_number,resume_version_id,job_version_id,profile_version_id,cover_letter_text,notes,answers,claims,attachments,diff,requirement_gaps,payload_hash,approval_state,approved_at,approved_by}`.
`GET /api/v1/applications/{application_id}/packages`,
`GET /api/v1/applications/{application_id}/packages/latest`,
`POST /api/v1/applications/{application_id}/packages/{version_id}/approve`,
and `POST /api/v1/applications/{application_id}/packages/{version_id}/edits`
preserve this response model. Edits are
`{edits:[{section,original_text,proposed_text,evidence_ids[],source}]}`;
approve has an empty body plus the ten-minute confirmation nonce. Unknown or
unowned application/version is `404`, stale/evidence/resume conflict is
`409/422`, and missing package service is `503`. No package adapter route can
send or submit externally.

### Review list adapter

`GET /api/v1/agents/runs/{run_id}/reviews` returns
`{items:[{review_id,run_id,decision,actor_id,field_decisions:[{path,decision,edited_value,evidence_refs[]}],note,created_at}],trace_id}`.
`actor_id` is redacted to the candidate-facing label; operator identities are
never exposed. An owned run with no reviews returns `200 items=[]`; unowned or
unknown run is `404`; dependency failure is `503`; the response is
`no-store`. `POST /review` uses the exact `ReviewRun` body from section 3 and
the same typed statuses.

## 4. Existing authority adapters

These routes are not reimplemented by the console. The adapter must preserve
the existing response schema and add only the standard `trace_id`/receipt
envelope where the existing contract already supports it.

| Existing route | Adapter rule and exact mutation precondition |
| --- | --- |
| `GET /api/v1/smart-intake/capability`, `GET /api/v1/smart-intake/previews`, `POST /api/v1/smart-intake/previews`, `GET /api/v1/smart-intake/previews/{preview_id}`, `POST /api/v1/smart-intake/previews/{preview_id}/apply` | Short-lived review-only preview remains the only smart-form authority. Apply accepts the existing field-decision body, requires the preview's `context_digest`, `X-CSRF-Token`, and `Idempotency-Key`, and returns a local draft patch/decision receipt; it never writes a canonical profile/resume directly. |
| `GET /api/v1/crawl-plans`, `GET /api/v1/crawl-plans/versions`, `GET /api/v1/crawl-plans/versions/{version_id}`, `POST /api/v1/crawl-plans/versions`, `POST /api/v1/crawl-plans/versions/{version_id}/activate`, `POST /api/v1/crawl-plans/pause`, `POST /api/v1/crawl-plans/resume`, `POST /api/v1/crawl-plans/run-now` | Existing plan/version repository and `CrawlRunWorkflow` remain authoritative. There is no `POST /api/v1/crawl-plans` root mutation. All listed state-changing calls use the common mutation headers and return the existing typed response plus receipt; no Agent action can directly insert a crawl run. |
| `GET /api/v1/crawl-runs`, `GET /api/v1/crawl-runs/{run_id}`, `GET /api/v1/crawl-runs/{run_id}/stages`, `POST /api/v1/crawl-runs/{run_id}/retry`, `POST /api/v1/crawl-runs/{run_id}/stop` | Existing crawl read model and `CrawlRunWorkflow` remain authoritative. The console may link to it using `target_route` and must preserve its owner scope, no-store behavior, and typed receipt controls. |
| `GET /api/v1/profile`, `GET /api/v1/profile/versions`, `GET /api/v1/profile/versions/{version_id}`, `POST /api/v1/profile`, `POST /api/v1/profile/versions/{version_id}/activate`, `GET /api/v1/resumes`, `GET /api/v1/resumes/eligible`, `GET /api/v1/resumes/{version_id}`, `POST /api/v1/resumes`, `POST /api/v1/resumes/{version_id}/confirm`, `GET /api/v1/resumes/{version_id}/evidence`, `GET /api/v1/evidence`, `GET /api/v1/evidence/{evidence_id}`, `POST /api/v1/evidence/{evidence_id}/confirm`, `POST /api/v1/evidence/{evidence_id}/reject` | Resume/profile/evidence version APIs remain the write authority. Agent review results are proposals referencing immutable version IDs; apply uses the existing validated version contract and a human decision. The exact auth and mutation controls for each path are enumerated in `authorization-matrix.md`. |
| `GET /api/v1/candidates/{candidate_id}/evidence`, `POST /api/v1/evidence/import`, `GET /api/v1/matches`, `POST /api/v1/matches/run` | Deterministic matching/evidence APIs remain authoritative. Candidate IDs in a path/body are checked against the session and never become authority inputs; matching writes remain reviewable deterministic artifacts and do not become model/provider writes. |
| `GET /api/v1/candidates`, `GET /api/v1/jobs/{job_id}/remote-eligibility`, `GET /api/v1/jobs/{job_id}/compensation` | Candidate/job read adapters remain candidate-scoped and `no-store`; missing or unowned resources are non-enumerating `404`, and repository failure is `503 DEPENDENCY_NOT_READY`. |
| `GET /api/v1/companies`, `GET /api/v1/jobs`, `GET /api/v1/jobs/{job_id}` | Existing job/company read authority. Responses are candidate/session scoped, `no-store`, and return `401/404/503` without exposing another candidate's saved state. |
| `GET /api/v1/inbox`, `GET /api/v1/inbox/{job_id}`, `GET /api/v1/inbox/{job_id}/excluded-reasons`, `POST /api/v1/inbox/{job_id}/favorite`, `POST /api/v1/inbox/{job_id}/ignore`, `POST /api/v1/inbox/{job_id}/snooze` | Existing inbox projection remains authoritative for normalized postings and deterministic filtering. Reads are `no-store`; mutations require Origin, CSRF, idempotency, ownership, and audit. Agent actions may link to these routes but cannot write a second inbox table. |
| `GET /api/v1/applications`, `POST /api/v1/applications`, `POST /api/v1/applications/{application_id}/transition`, `GET /api/v1/applications/{application_id}/events`, `POST /api/v1/applications/{application_id}/prepare`, `GET /api/v1/applications/{application_id}/channels`, `POST /api/v1/applications/{application_id}/channel`, `POST /api/v1/applications/{application_id}/package`, `GET /api/v1/applications/{application_id}/timeline`, `POST /api/v1/applications/{application_id}/state` | Existing application workspace remains authoritative for local application state and package binding. Each route uses ownership; reads are `no-store`; mutations require Origin, CSRF, idempotency, and audit. The Agent cannot transition or bind a package without the existing authority checks. |
| `POST /api/v1/resume-versions`, `POST /api/v1/application-packages`, `POST /api/v1/follow-ups` | Legacy compatibility writes remain explicit adapters during the migration window. They must use the same candidate ownership, idempotency, validation, and audit rules; no Agent action may bypass the canonical profile/resume/package contracts. |
| `GET /api/v1/applications/{application_id}/email-draft`, `POST /api/v1/applications/{application_id}/send-email`, `POST /api/v1/applications/{application_id}/submit`, `POST /api/v1/applications/{application_id}/confirm-external-submission` | Draft/read may remain an owned local presentation; send and external submission are denied in this change before network/state side effects with `403 EXTERNAL_WRITE_DENIED` (or the existing separate approved capability), and cannot appear in the Agent action queue. |
| `GET /api/v1/applications/{application_id}/packages`, `GET /api/v1/applications/{application_id}/packages/latest`, `POST /api/v1/applications/{application_id}/packages`, `POST /api/v1/applications/{application_id}/packages/{version_id}/approve`, `POST /api/v1/applications/{application_id}/packages/{version_id}/edits` | Application package/version APIs remain the only package authority. Approval/edit requires CSRF, idempotency, ownership, and the ten-minute confirmation nonce. Local copy/download is not a new HTTP endpoint; it uses an owned response, explicit confirmation, and an export/copy audit event. No outbound send is added. |
| `POST /api/v1/agents/job-matching`, `POST /api/v1/agents/resume-review`, `POST /api/v1/agents/interview-preparation`, `GET /api/v1/agents/runs`, `GET /api/v1/agents/runs/{run_id}`, `POST /api/v1/agents/runs/{run_id}/review`, `GET /api/v1/agents/runs/{run_id}/reviews` | Existing Agent routes are compatibility adapters to the canonical run/context contract. A start request without a successful current preflight returns `403 PREFLIGHT_REQUIRED`; old state values are mapped by `design.md` and never treated as success when unknown. |

The outbound dispatcher is not an Agent endpoint. If an adapter attempts to
call it, the server applies the finite method/path/action matcher in
`docs/agent-console/authority-map.md` after strict canonicalization. Reply
send, mail sync, OAuth, system-send, external submission, unknown adapter, and
unknown route all return `403 EXTERNAL_WRITE_DENIED`/`OAUTH_DENIED` before any
network or state-changing call. A model output can never select the
dispatcher target.

## 5. Status, ownership, and negative contract

All owned resources use `200/201/202` as shown above. An unowned or unknown
resource returns the same `404 NOT_FOUND` envelope, so ownership cannot be
enumerated. A policy/dependency denial is `403` or `503` only after ownership
has been established. A server-side audit failure returns `503 AUDIT_FAILED`
and the mutation is rolled back. A stale context or optimistic version is
`409`, never silently merged. A browser-side or model-side error is reduced to
the typed `cause_code`; raw provider/browser output is never returned.

The contract test suite must assert every row in this annex, including absent
`Origin`, wrong CSRF cookie/header, candidate-scope injection, cursor replay,
same/different idempotency replay, no-store headers, audit rollback, and
cross-candidate non-enumerating 404 behavior.

This change adds no candidate-facing audit, Prometheus metrics, or package
export HTTP endpoint. Audit and aggregate metrics remain internal
security-definer/read-model or existing `/metrics` surfaces; candidate UI
copy/download is a local, explicitly confirmed presentation of an already
owned package response and records an append-only export/copy audit event. If
a future release adds an HTTP export/audit endpoint, it must add an exact row
here and in the authorization matrix before implementation.
