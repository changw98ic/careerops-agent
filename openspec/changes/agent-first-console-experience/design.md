## Context

The current Vue console has authenticated workspace routes, deterministic job matching, review-only Agent tabs, smart-intake components, Temporal infrastructure, a structured model gateway, and safety gates. Ego evaluation showed that these pieces are exposed as separate screens rather than one guided career loop: the dashboard has metrics and setup links but no prioritized work queue; the crawl-plan page can show “create a plan” without a creation CTA; Agent history has no trace or recovery detail; and the live environment truthfully reports `model_provider=disabled` while AI-shaped controls are visibly gated.

The implementation baseline is deliberately explicit:

- `src/careerops/domain/agent_runs.py` currently persists the legacy state values `pending`, `running`, `succeeded`, `failed`, `unavailable`, `abstained`, `stale`, `cancelled`, and `reviewed`.
- `src/careerops/application/agent_runtime.py` currently derives idempotency from `capability + input_hash` and executes the operation in the request path.
- `src/careerops/workflows/s5_workflows.py` contains crawl workflows; the worker does not yet register a durable Agent workflow.
- `migrations/versions/0025_agent_runs_and_browser_mode.py` already owns `agent_runs` and `agent_run_reviews`; this change must extend those tables rather than create a second run authority.
- The Compose topology does not yet ship an Ego runtime. Browser readiness must therefore fail closed while HTTP-only and deterministic paths remain usable.

The design preserves the repository safety invariants: model provider disabled by default, unknown policy actions denied, Redis/limiter failure closed, external writes and auto-send disabled, OAuth disabled, audit append-only through the security-definer function, candidate ownership resolved server-side, and all container images digest-pinned.

## Scope, dependencies, and authority

This change is an experience/orchestration layer over the earlier `llm-agent-career-loop` and `ai-assisted-smart-forms` changes. Existing capabilities are reused and modified through explicit adapters; no parallel persistence or executor is introduced.

| Concern | Authoritative owner | This change may add | This change must not do |
| --- | --- | --- | --- |
| Crawl plans/runs | Existing crawl-plan service, `CrawlRunWorkflow`, and crawl repositories | readiness, provenance/read-model projection, UI actions | call browser from an API handler or create a second crawl run table |
| Canonical jobs/inbox | Existing canonical-job and inbox projection contracts | source-state events and navigation context | write model scores into the deterministic decision authority |
| Deterministic matching | `MatchOrchestrator` and `filter_decisions` | explanation run attached to a job/version | let an LLM change a hard-filter decision or create evidence |
| Model preview | `smart-form-intake` preview/apply contract | shared preflight and state presentation | create a second generic preview store |
| Resume | `resume_versions` and existing resume service | review-only draft/diff metadata | mutate the base resume without an existing validated write |
| Interview preparation | Agent run/review contract and editable draft result | context continuity and clarification state | treat a draft as verified fact or external communication |
| Application package | Existing `/api/v1/applications/{id}/packages` contract | map approved draft fields into its request shape | auto-submit, send email, or enable external writes |
| Audit | `careerops.append_audit_event` security-definer function | bounded event taxonomy and read projection | direct API-role INSERT into the audit table |
| Browser execution | Explicit, digest-pinned Ego/browser sidecar contract | readiness and source-policy UI | assume the current Compose worker already contains Ego |

Mail follow-up, reply queue, mailbox ingestion, Gmail OAuth, and outbound communication are explicitly out of scope. Their existing review-only pages remain available, but this change does not create proactive actions that send, reply, or alter mail.

The delta precedence is explicit: `llm-agent-career-loop` remains authoritative for crawl execution, shared model schemas, Agent service inputs, and review-only result semantics; this change modifies only the page-managed context/action/run presentation and adds the missing durable orchestration contracts. `ai-assisted-smart-forms` remains authoritative for its short-lived preview storage and apply decision record; this change reuses it and adds only a preflight/capability presentation. Where this change adds a stricter security invariant (for example unknown-policy deny, no raw outbound field, or safe rendering), the stricter invariant wins. No earlier requirement is silently superseded; any conflicting implementation must be adapted or blocked before release.

The new action target allowlist is limited to `/profile`, `/resumes`, `/evidence`, `/crawl-plans`, `/crawl-runs`, `/crawl-runs/{id}`, `/jobs`, `/jobs/{id}`, `/inbox`, `/inbox/{id}`, `/ai-workbench`, and `/applications/{id}` with server-generated route parameters. It MUST NOT generate or invoke `sendApplicationEmail`, `sendReplyDraft`, `confirmSystemSend`, `syncMailNow`, or `confirmExternalSubmission` actions. Existing pages may expose their separately gated manual send controls, but this change neither adds nor auto-invokes them.

The server-side outbound dispatcher applies the finite matcher in
`docs/agent-console/authority-map.md` to the canonical HTTP method/path and
adapter name after one strict URL canonicalization. It denies the reply-send,
mail-sync, OAuth, system-send, and external-submission paths even if a model or
client supplies a plausible label. A route registry pass is not sufficient:
the same deny matcher runs at the adapter/dispatcher boundary, and an unknown
method, adapter, encoded path, or target is denied.

The authority registry is also explicit:

- Matching hard-filter verdicts and requirement-level results remain `filter_decisions` through `/api/v1/matches/run`. The new Agent result is an explanation/review artifact in `agent_runs`; it cannot update hard verdicts, `semantic_ranking_*` fields, or evidence. Existing semantic-ranker writes must be disabled or adapted to this same review-only artifact before release.
- Resume source and confirmed versions remain `resume_versions` through `/api/v1/resume-versions` and `/api/v1/resumes`. A reviewed suggestion can create a new version only by calling that existing validated resume-version contract; no direct `agent_runs.result` copy is authoritative.
- Interview preparation remains a review-only result in `agent_runs.result` plus `agent_run_reviews`; smart-intake remains the authority for copying a user-authored interview context into the existing form. No second interview-draft table is introduced by this change.
- New package work targets `application_package_versions` through `/api/v1/applications/{application_id}/packages*`; legacy `application_packages` remains a compatibility read/write path for existing pages only, and the Agent adapter must not write both authorities in one operation.

New persistence uses candidate-scoped security-definer functions or controlled views for row authorization. Table/column grants alone are not considered row isolation; the API role cannot bypass candidate predicates or composite foreign keys.

The following annexes are normative and take precedence over abbreviated
examples in this design. A future implementation must update the annex and the
spec scenario evidence together; prose cannot weaken an annex:

| Annex | Frozen contract |
| --- | --- |
| `docs/agent-console/api-contract.md` | every new endpoint, existing-authority adapter, body/response, status/error, cursor, CSRF, idempotency, and `no-store` rule |
| `docs/agent-console/temporal-contract.md` | workflow input/result, five activity payloads/receipts, signals, query, lease/fencing SQL, retries, cancellation, and typed errors |
| `docs/agent-console/sidecar-wire-contract.md`, `sidecar-wire.schema.json` | `/ready`, `/v1/browser/run`, signature/nonce headers, exact result/error JSON, HTTP statuses, bounds, partial-success closure, and no-credential browser boundary |
| `docs/agent-console/migration-contract.md` | 0030 expand/backfill/contract DDL, composite keys, compatibility mapping, CAS functions, grants, audit transaction, retention, and rollback-forward |
| `docs/agent-console/provider-policy.schema.json`, `consent-envelope.schema.json`, `egress-decision.schema.json` | machine-validated policy, consent, and egress decision fields; an allowed decision must carry consent and operation fields are closed sets |
| `docs/agent-console/scenario-register.json`, `scenario-register.schema.json`, `scenario-evidence.schema.json`, `release-gate-evidence.schema.json`, `retention-ci-contract.md` | one record for each of the 106 scenarios, separate release-gate artifact schema, gate ownership, retention lock/retry/alert, and fail-closed CI evidence |

The first-value journey is a concrete route contract:

| Step | Route | Server context/API | Required accessible CTA and assertion |
| --- | --- | --- | --- |
| 0 | `/login` | session/bootstrap readiness | `登录` is labelled, error is associated, focus lands on the first invalid field |
| 1 | `/dashboard` | `GET /api/v1/agent-console/actions` | `开始建立职业画像` or the server-provided first action is the only primary CTA in the no-data state |
| 2 | `/profile` | profile version endpoint; `context_id` reference only | `保存职业画像` returns focus to the status/live region and does not fabricate metrics |
| 3 | `/resumes` | resume version/evidence readiness | `上传并确认简历` exposes parse/confirmation state and a route to evidence |
| 4 | `/evidence` | candidate-owned evidence list | `确认证据` announces completion and invalidates stale context if a source changed |
| 5 | `/crawl-plans` | existing plan version/activate endpoints | `创建职位抓取计划` opens the draft form; no plan is implied by an empty list |
| 6 | `/crawl-runs/:id` | existing crawl run read model | `查看运行进度` exposes trace/provenance/blocked/retry state, not a local spinner |
| 7 | `/inbox/:id` | canonical job/inbox contract; server context | `查看匹配依据` preserves job/version/context identifiers |
| 8 | `/ai-workbench?tab=matching` | deterministic matching plus optional explanation run | `查看确定性匹配` is usable when model is disabled; model explanation is labelled review-only |
| 9 | `/ai-workbench?tab=resume` | Agent resume-review route and context | `审查简历建议` shows preflight/source/unknowns and explicit review decision |
| 10 | `/ai-workbench?tab=interview` | Agent interview-preparation route and context | `生成面试准备草稿` is editable and separates verified facts from user goals |
| 11 | `/applications/:id` | existing package editor/approve/export contract | `审查申请材料` is the final review gate; `发送/提交` remains denied by this change |

## Goals / Non-Goals

**Goals:**

- Make the next useful user action visible without requiring the user to understand the module graph.
- Keep deterministic guidance useful when no model is configured.
- Give every Agent run a durable, redacted, page-visible trace and a recoverable lifecycle.
- Make model-assisted interaction a reviewable structured preview, never a silent form mutation or action.
- Preserve a continuous selected-job/profile/resume/evidence context across crawl, matching, resume review, interview preparation, and application preparation.
- Make disabled, blocked, stale, failed, empty, expired, rate-limited, and dependency-unavailable states explain the reason and the next safe action.
- Provide an executable API, database, Temporal, frontend, browser, and Ego evidence matrix without synthetic M1 pilot data.
- Treat WCAG 2.2 AA-oriented keyboard, focus, labeling, live-region, localization, zoom, contrast, and reduced-motion behavior as part of the feature contract.

**Non-Goals:**

- Enabling a model provider, OAuth, browser session reuse, external writes, auto-send, or model tool binding by default.
- Replacing existing authoritative domain APIs with a second persistence path.
- Building a general-purpose autonomous chat agent or push-notification system.
- Adding automatic application submission, Gmail sending, or unattended decisions.
- Fabricating pilot data to make the UI look populated.

## Decisions

### 1. Deterministic action projection first

The dashboard action queue is a read model derived from authoritative state: profile readiness, resume confirmation, crawl-plan readiness, stale runs, unmatched jobs, pending evidence, pending reviews, and application follow-ups that are already represented by an existing review-only contract. It returns at most three actions per candidate and includes a stable action key, deterministic rank, priority, reason codes, prerequisites, source timestamps, a server-generated source-event key, and a safe route or draft action. The model is not needed to calculate urgency or invent a task.

Source-state changes emit a bounded invalidation event in the same transaction as the authoritative change. The projection increments `queue_version` once per unique event key. The first release uses in-app refresh: the visible dashboard polls at most once every 30 seconds and refreshes on route focus; it does not send email, browser, or operating-system notifications. Multiple tabs/devices deduplicate by `(candidate_id, event_key, delivery_surface)` and the server stores the last delivered queue version. A candidate goal scope and in-app quiet-hours policy may suppress presentation, but never hide the action from its source page.

Alternative rejected: using an LLM to decide what the user should do. That would make prioritization nondeterministic, harder to audit, and unavailable in the safe default environment.

### 2. One server-scoped context envelope

The frontend stores only a selected context reference. The API resolves the authenticated candidate and validates job/version/profile/resume/evidence ownership. The envelope contains IDs, immutable version numbers, content digests, freshness timestamps, an explicit `allowed_operation`, and selection provenance; raw resumes, raw mailbox data, credentials, arbitrary URLs, and model content are excluded. The envelope carries `context_id`, `schema_version`, `created_at`, `expires_at`, `digest_algorithm`, `source_version_snapshot`, and `invalidation_reason`. Any source change invalidates the envelope or marks it stale.

### 3. Orthogonal Agent lifecycle fields with legacy compatibility

The existing `agent_runs.state` column and response value remain readable during an expand/contract window. New contracts add three orthogonal values:

- `execution_state`: `queued`, `running`, `waiting_review`, `succeeded`, `failed`, `cancel_requested`, `cancelled`, `stale`, or `blocked`.
- `capability_state`: `enabled`, `disabled_by_policy`, `not_configured`, `dependency_not_ready`, `blocked_by_prerequisite`, `stale`, or `failed`.
- `review_state`: `not_required`, `pending`, `accepted`, `rejected`, or `edited`.

The compatibility mapping is deterministic: `pending → queued`; `running → running`; `succeeded → succeeded`; `failed → failed`; `unavailable → blocked` plus its capability reason; `abstained → waiting_review` with outcome `abstained`; `cancelled → cancelled`; and `reviewed → succeeded` plus the persisted review decision, or `waiting_review` if the legacy review row is missing. Responses expose both `state` (legacy) and the canonical fields until all consumers migrate. Retry is allowed only from `failed` with a retryable error or from `stale` after a new context; stop is allowed from `queued` or `running`; review is allowed from `waiting_review` or `succeeded` with a reviewable result. Unknown state values are blocked and never treated as success.

The state transition contract is:

```text
queued -> running -> waiting_review -> succeeded
queued/running -> failed
queued/running -> cancel_requested -> cancelled
any non-terminal state -> stale when an input digest changes
failed -> queued only through a new attempt when retryable
blocked -> queued only after the blocking capability/prerequisite is resolved
```

`reviewed` is a review field, not an execution state. `unavailable` and `abstained` are preserved as bounded outcome/capability metadata during migration.

### 4. Frozen API and error contract

All candidate scope is resolved from the authenticated session; a client-supplied candidate ID is ignored or rejected. Mutating requests require a valid CSRF token and an `Idempotency-Key` (1–128 ASCII characters). The server binds the key to actor, resource, operation, and canonical request-body hash. Keys are retained for 24 hours; a reused key with a different hash returns `409 IDEMPOTENCY_CONFLICT`. Every sensitive response sets `Cache-Control: no-store` and `Pragma: no-cache`.

The target API surface is:

| Method and path | Purpose | Success | Typed failures |
| --- | --- | --- | --- |
| `GET /api/v1/agent-console/actions?cursor=&limit=3` | candidate action queue | `200` with `queue_version` | `401`, `503 DEPENDENCY_NOT_READY` |
| `POST /api/v1/agent-console/actions/{action_key}/accept` | accept/navigation | `200` or `201` | `404`, `409 IDEMPOTENCY_CONFLICT`, `422 PREREQUISITE_BLOCKED` |
| `POST /api/v1/agent-console/actions/{action_key}/snooze` | bounded snooze | `200` | `400 SNOOZE_OUT_OF_RANGE`, `409` |
| `POST /api/v1/agent-console/actions/{action_key}/dismiss` | explicit dismissal | `200` | `409` |
| `POST /api/v1/agent-console/actions/{action_key}/complete` | record source completion | `200` | `409 ACTION_VERSION_CONFLICT` |
| `POST /api/v1/agent-console/contexts` | create validated context | `201` | `403`, `409 SOURCE_VERSION_CONFLICT`, `422` |
| `GET /api/v1/agent-console/contexts/{context_id}` | read context metadata | `200` | non-enumerating `404` |
| `GET /api/v1/agents/runs` | list candidate runs | `200` with cursor | `401`, typed dependency state |
| `GET /api/v1/agents/runs/{run_id}` | read run | `200` | non-enumerating `404` |
| `GET /api/v1/agents/runs/{run_id}/stages` | read redacted stages | `200` with sequence order | non-enumerating `404` |
| `POST /api/v1/agents/runs/{run_id}/retry` | create next attempt | `202` | `409 NOT_RETRYABLE`, `409 IDEMPOTENCY_CONFLICT`, `422 CONTEXT_STALE` |
| `POST /api/v1/agents/runs/{run_id}/stop` | cooperative stop | `202` | `409 STOP_NOT_ALLOWED` |
| `POST /api/v1/agents/runs/{run_id}/review` | field-level review/apply | `200` | `409 PREVIEW_STALE`, `409 IDEMPOTENCY_CONFLICT`, `422 EVIDENCE_REQUIRED` |
| `GET /api/v1/capabilities/agent?operation=` | capability/readiness | `200` | unknown operation returns blocked state |
| `POST /api/v1/agent-console/preflight` | no-provider-call egress/capability/consent preview | `200` with server `consent_id`/`preflight_id` | `403 POLICY_DENIED`, `409 CONTEXT_STALE`, `422 PREREQUISITE_MISSING`, `429 BUDGET_EXHAUSTED` |
| `GET /api/v1/crawl-runs` | existing candidate crawl run list | `200` with cursor | `401`, `400 CURSOR_INVALID` |
| `GET /api/v1/crawl-runs/{run_id}` | existing crawl detail + trace/provenance projection | `200` | non-enumerating `404` |
| `GET /api/v1/crawl-runs/{run_id}/stages` | redacted crawl stage timeline | `200` with sequence order | non-enumerating `404` |
| `POST /api/v1/crawl-runs/{run_id}/retry` | retry an eligible crawl attempt through `CrawlRunWorkflow` | `202` receipt | `404`, `409 NOT_RETRYABLE`, `422 SOURCE_POLICY_DENIED` |
| `POST /api/v1/crawl-runs/{run_id}/stop` | cooperative crawl stop | `202` receipt | `404`, `409 STOP_NOT_ALLOWED` |
| existing `POST /api/v1/agents/job-matching`, `/resume-review`, `/interview-preparation` | start review-only operation | `202`/compatible legacy response | typed prerequisite/capability errors |
| existing `/api/v1/smart-intake/previews*` | short-lived smart-form preview/apply | existing contract | existing typed preview errors |
| existing `/api/v1/crawl-plans/*` | plan/version/activate/run-now | existing contract plus readiness fields | source/policy/dependency errors |

Error bodies use the flat `{"trace_id","code","message","retryable","fields"}` envelope frozen in `docs/agent-console/api-contract.md`; user-facing `message` is Chinese-first and never contains secrets, raw provider text, or cross-candidate existence information. Cursor pagination is server-owned and candidate-scoped. Operators, if later introduced, require an explicit scope and receive the same redaction contract; an authenticated candidate is never an operator by inference.

The complete per-endpoint wire contract, including exact bodies, response
fields, cursor encoding/expiry, CSRF cookie/header rules, `no-store` policy,
and the existing smart-intake/crawl/resume/package adapters is
`docs/agent-console/api-contract.md`. Any route implementation that diverges
from that annex is a contract failure, not an optional compatibility detail.

The following bounded payloads are the minimum wire contract. Names and shapes are frozen before implementation; server-generated IDs, fingerprints, queue versions, trace IDs, and timestamps are never accepted as client authority.

```json
{
  "queue_version": 12,
  "generated_at": "2026-07-28T08:00:00Z",
  "user_goal_scope": "job_search",
  "notification_policy": {"surface": "in_app", "quiet_hours": false},
  "items": [{
    "action_key": "resume-review:job:00000000-0000-0000-0000-000000000010:resume:8",
    "kind": "resume_review",
    "title": "检查这份简历与岗位的匹配度",
    "reason_code": "NEW_CONTEXT",
    "priority": "high",
    "target_route": "/ai-workbench?tab=resume",
    "deterministic_rank": 1,
    "prerequisites": {"status": "ready", "missing": []},
    "context_id": "00000000-0000-0000-0000-000000000001",
    "source_refs": [{"type": "job", "id": "00000000-0000-0000-0000-000000000010"}],
    "source_event_key": "00000000-0000-0000-0000-000000000002",
    "source_freshness": "2026-07-28T07:59:00Z",
    "state": "proposed",
    "created_at": "2026-07-28T08:00:00Z",
    "expires_at": null
  }],
  "next_cursor": null,
  "trace_id": "00000000-0000-0000-0000-000000000003"
}
```

An action mutation accepts only `expected_queue_version`, optional server-issued
`context_id`, and the operation-specific bounded input (for example `until`
from the server-provided snooze choices). A successful receipt is the exact
`ActionReceipt` in `api-contract.md`: `trace_id`, `receipt_id`,
`idempotency_key`, `resource_type`, `resource_id`, `action_key`, the
seven-value action `state`, integer `queue_version`, `outcome`,
`audit_event_id`, `idempotency_replayed`, and `accepted_at`. A context create
request accepts only candidate-owned IDs/versions, selected evidence IDs,
`allowed_operation`, and bounded untrusted user context; the response adds
server-generated `context_id`, `schema_version`, `digest_algorithm`, source
digest/version snapshot, `created_at`, `expires_at`, and `invalidation_reason`.

Run detail exposes `{"run_id","legacy_state","execution_state","capability_state","review_state","context_id","trace_id","current_attempt","progress","latest_stage","failure":{"code","retryable"},"created_at","started_at","finished_at"}`. `result_summary` is bounded and redacted when present as explicitly bounded metadata; raw prompt/output/source bytes are never part of the response. Stage detail exposes `stage_event_id`, `sequence`, `attempt`, `schema_version`, `stage`, `status`, `cause_code`, `terminal`, `occurred_at`, `duration_ms`, `retryable`, `provider_state`, `source_refs`, and a bounded message. Review/apply accepts a list of `{path, decision, edited_value?, evidence_refs[]}` plus `preview_id`, `context_digest`, and the request body hash is server-computed; it cannot accept arbitrary `edited_result` keys.

### 5.1 ProviderPolicy, EgressDecision, and ConsentEnvelope

The contract names two intentionally different enums: `model_operation` is
`job_matching|resume_review|interview_preparation|smart_form_intake` for
provider policy, consent, preflight, and egress; `agent_run_operation` is the
three-value subset accepted by Agent workflow/start/retry. Smart intake keeps
its existing preview/apply lifecycle and never becomes a durable Agent
workflow operation.

The model gateway resolves three server-owned records before a request is queued and again immediately before the provider activity runs:

| Record | Required fields | Deny condition |
| --- | --- | --- |
| `ProviderPolicy` | `policy_id`, `version`, `provider_id`, `model_id`, allowlisted TLS host, `verified_tls=true`, region, retention days, `training_allowed=false`, known/supported deletion policy with SLA, approved operations/fields, effective/expiry time, approval source | any missing/expired/unknown field, host, certificate, region, retention, training/deletion policy, or operation |
| `ConsentEnvelope` | server UUID `consent_id`, candidate, operation, source version/digest set, field-set hash, provider policy version, granted/expiry time (15 minutes), revocation time, single-use nonce | missing, expired, revoked, mismatched candidate/operation/source/field set, nonce replay |
| `EgressDecision` | server UUID, allow/deny, reason code, policy/consent IDs, field-set hash, redaction version, request ID, server time | any deny reason, budget failure, policy change, or worker recheck mismatch |

The allowed outbound fields are fixed per operation. `job_matching` may send normalized job title, location, requirement identifiers/text, candidate target titles, normalized skills, and confirmed evidence claim text/version IDs. `resume_review` may send the same job fields plus confirmed evidence claims and presentation metadata; it never sends PDF/raw resume bytes, name, email, phone, address, compensation, authorization, or visa fields by default. `interview_preparation` may additionally send the candidate's explicitly selected practice goals as untrusted text. `smart_form_intake` may send only the user-entered intake text and selected non-sensitive form target. Mailbox content, cookies, browser HTML, arbitrary URLs, credentials, and unconfirmed evidence are not allowed for any operation in this change.

Consent is per operation and source set, not a blanket account switch. Revocation is checked before queue, when a queued run is claimed, and immediately before provider connection. A changed field set or provider policy invalidates the consent and fingerprint. The fake provider test MUST capture the exact outbound JSON and assert that disallowed fields are absent, not merely assert that a PDF was not sent.

### 5.2 BrowserNetworkBoundary

The browser sidecar MUST run behind a forced egress proxy or network interceptor that observes every top-level navigation, redirect, image, iframe, stylesheet, script, WebSocket, `fetch`, and XHR request. A request is admitted only after scheme, IDN/punycode-canonical host, explicit port (`80` or `443` only), allowlisted source family, DNS answer, and connected socket address pass the same policy. DNS answers are pinned to the connection or revalidated at connect time; CNAME chains, IPv4-mapped IPv6, rebinding, and redirect hops are tested. Page JavaScript cannot bypass the interceptor. WebSocket upgrades, uploads, form POSTs, downloads, and arbitrary headers are denied even though a WebSocket handshake uses HTTP GET.

Each run starts a fresh isolated task space with no reused tab, cookie jar, service worker, cache, local storage, session storage, upload, OAuth/session material, `Authorization` header, or arbitrary user header. Only `GET`/`HEAD` are allowed; redirects are limited to three; response size is capped at 2 MiB; fetch timeout is 15 seconds; source concurrency is two; and the source rate budget is 60 requests/minute. Loopback, link-local, private, multicast, IPv6-local, cloud metadata, unsafe schemes, unallowlisted hosts, and non-default ports are denied for every subrequest. If the sidecar cannot prove this boundary, readiness is `dependency_not_ready` and browser execution is not available.

The HTTP executor uses the same mandatory allowlist. A missing/`None` host allowlist is a configuration error that denies the source; it is never interpreted as “allow all”. The final normalized posting may be retained according to source policy, but raw HTML, screenshots, page source, downloaded files, and browser errors are deleted at task completion or their configured retention deadline.

The sidecar protocol is `GET /ready` plus `POST /v1/browser/run`. Readiness returns `image_digest`, `policy_bundle_digest`, `egress_proxy_mode`, `supported_schemes`, and `key_id`; any missing or non-digest value is not ready. A run request contains only `run_id`, `attempt_id`, `source_id`, canonical start URL, allowlist version, task-space nonce, byte/time/request budgets, and an output schema version. The worker signs the canonical request with an active key ID, RFC 3339 timestamp, and unique UUID replay nonce; the sidecar verifies mTLS, signature, a ±60-second clock window, and nonce uniqueness, and returns only normalized postings/provenance/counts or a typed policy/dependency error. Keys support a 24-hour overlap rotation, are never logged, and are rejected after expiry. The request/response contract is tested without a real external source.

### 5.3 ActorResourceAuthorizationMatrix

Candidate scope is resolved from the session, never from a request body. The matrix is normative:

| Actor | Read | Mutate | Explicit deny |
| --- | --- | --- | --- |
| Candidate | own actions, contexts, previews, runs, stages, source versions, packages, and own aggregate usage | own action lifecycle, context create, plan draft/activate/run, Agent start/retry/stop/review, preview apply, package edit/approve/export with CSRF/idempotency/fresh confirmation | any other candidate resource, operator-only data, raw provider/source logs, external send/write |
| Scoped operator (future) | only approved operational metadata and redacted aggregates within an explicit tenant/candidate scope | capability flag/retention/stop controls requiring operator audit and change approval | raw candidate content, arbitrary candidate impersonation, provider secret access |
| Agent worker | only the candidate/run/attempt/resource IDs in its signed workflow input, with current lease epoch | stage/event/result writes for its assigned attempt through repository contracts | changing ownership, policy/capability, consent, authoritative profile/job/package data, external side effects |
| Retention service | retention metadata and due records | purge only through the controlled security-definer function, respecting legal holds | arbitrary reads, audit deletion, source reconstruction |

Every listed mutation validates `Origin` against the configured console origin, uses SameSite cookies, rejects missing/mismatched Origin, rejects wildcard CORS, and sets frame/CSP protections. CSRF is required for action accept/snooze/dismiss/complete, context creation, plan creation/activation/run-now, Agent start/retry/stop/review, preview apply, package edit/approve/export, and any future state-changing route. Package activation/approval/export uses a server-generated confirmation nonce bound to actor/resource/body hash, expires after 10 minutes, is single-use, and requires a current authenticated session. This does not authorize external writes; those remain denied.

### 5.4 MigrationDualWriteAndAuditTransaction

Revision `0030_agent_console_orchestration` follows the repository head `0029_add_proposal_value_digests`. It adds a unique `(id, candidate_id)` key to `agent_runs` so `agent_run_reviews` and all new child tables can use composite `(run_id, candidate_id)` foreign keys. Every child row carries `candidate_id`; a mismatched run/resource pair is rejected by the database, not only by application code. `agent_stage_events` has unique `(attempt_id, sequence)` and `event_key`, a bounded payload check, and no API-role `UPDATE`/`DELETE` privilege. `agent_actions` has a unique `(candidate_id, action_key, source_event_key)` key; attempts have unique `(run_id, attempt_no)` and a CAS-protected `lease_epoch`.

The rolling migration has four phases: (1) expand with nullable canonical fields, composite key, trigger/function, and additive child tables; (2) backfill and verify the legacy mapping; (3) dual-read canonical fields with legacy fallback and dual-write both representations through one repository transaction; (4) retire legacy writers only after the compatibility window. During the window, a database compatibility function maps a recognized legacy `state` update to canonical fields and rejects unknown values; new code writes both fields. A new application refuses to start against a schema before 0030, while the old application is first switched to the dedicated legacy read/write compatibility functions and then loses direct table access. If that adapter is not installed, rollout stops. Active runs are reconciled before the old writer is removed. Rollback-forward disables new scheduling and preserves all historical columns/tables; it never drops or rewrites audit/stage history.

Audit append receives the business transaction connection and calls `careerops.append_audit_event` in that same transaction. If the audit function, policy decision, budget counter, or append fails, the business mutation rolls back and returns a safe denial; an audit record in a separate committed transaction is not acceptable. Audit reads use a candidate-scoped security-definer read function/view with redaction and authorization, not a direct API-role table select. Audit payloads include `consent_id`, egress field-set hash, policy version, actor/session/request IDs, resource, old/new state, reason, and server time. A scheduled purge runs hourly, records success/failure metrics and alerts, respects a `legal_hold_until/legal_hold_reason` field, and has a tested failure-retry path.

### 5. Two-phase model interaction

Every model-assisted operation has `preflight`, `preview`, and `apply` phases. Before a provider request, the UI shows the operation goal, selected source versions, exact allowed fields/byte budget, what will be sent after redaction, provider/capability state, expected output type, expiry, and the controls to remove a source or cancel. If a required source is missing, the UI asks a deterministic clarification question and makes no provider call. A provider clarification response becomes a bounded `needs_user_input` state, never an implicit retry loop.

Preview returns typed fields, source references, confidence semantics, unknowns, warnings, model/provider identity, prompt/schema versions, context digest, expiry, and request fingerprint. Apply requires an unexpired preview, matching context digest, explicit field-level decisions, CSRF, idempotency, and a human review record. The result is copied into an existing form or draft API; the model never writes directly to an authoritative record. Progress is sourced from the durable run state, and stop/retry controls are shown only when supported. A local spinner is never presented as completed model work.

### 6. Temporal and worker contract

Durable Agent execution uses a new `AgentRunWorkflow` registered on task queue `careerops-agent`. Its workflow ID is `agent-run:{candidate_id}:{logical_run_id}`. The workflow accepts only JSON-safe IDs, digests, versions, and policy decisions; it never calls a model, HTTP endpoint, browser, database driver, or clock directly. Activities are `resolve_agent_context`, `run_deterministic_stage`, `invoke_model_stage`, `persist_agent_stage`, and `reconcile_agent_run`. The API creates the run and starts the workflow asynchronously.

The workflow exposes a `stop` signal, a `refresh_context` signal, and a redacted status query. Activities heartbeat every 10 seconds, have a 90-second model activity timeout and a 30-second cancellation acknowledgement budget, and use at most two retries with exponential backoff and jitter. Each attempt owns a monotonically increasing `lease_epoch`; a worker may persist a stage only when its epoch is current. An old worker, duplicate activity completion, or a post-cancel completion is rejected and audited. A poison input, exhausted retry budget, or cancellation timeout becomes `blocked`/`failed` with a typed reason instead of looping forever.

The workflow input is the exact `{candidate_id, logical_run_id, context_id,
operation, context_digest, policy_version, consent_id, trace_id,
request_fingerprint}` envelope; `operation` is the three-value
`agent_run_operation` subset and `consent_id` is absent only for a
deterministic-only run. Activity receipts are the exact
`{attempt_id, lease_epoch, cancel_epoch, stage, event_key, sequence, outcome,
next_state, output, trace_id}` envelope. They are accepted only by a
compare-and-set equivalent to
`WHERE attempt_id=:attempt AND lease_epoch=:epoch AND
cancel_epoch=:cancel_epoch AND state IN ('running','waiting_review')`.
Lease renew is the same CAS on an unexpired lease; stop increments
`cancel_epoch` before sending the signal. Workflow reuse is
`REJECT_DUPLICATE` for a terminal logical run and `ATTACH_EXISTING` only when
the same candidate/run/idempotency receipt is returned. These conditions are
part of `TEMP-02`, not implementation discretion.

The exact activity payloads and outputs, worker identity binding, lease
acquire/renew/append SQL, signal payloads, redacted query response, retry
classification, heartbeat, and cancellation deadlines are frozen in
`docs/agent-console/temporal-contract.md`.

Existing `CrawlRunWorkflow` remains the crawl authority. The Agent workflow may consume its normalized job/version output through an activity, but it does not duplicate crawl persistence or call Ego from workflow code.

### 7. PostgreSQL migration and rollback-forward

Migration `0030_agent_console_orchestration` is additive and follows repository head `0029_add_proposal_value_digests` (which already includes `0025_agent_runs_and_browser_mode` and the smart-intake revisions). It extends `agent_runs` with canonical lifecycle/capability/review columns, `legacy_state`, `context_id`, and `current_attempt`; extends `agent_run_reviews` with an idempotency key, field decisions, and request hash; and adds:

- `agent_contexts`: candidate FK, selected IDs/versions, source digests, operation, schema version, lifecycle timestamps, and invalidation reason.
- `agent_actions`: candidate FK, server-generated action key, queue version, source event key, deterministic rank, lifecycle state, snooze/expiry, and bounded reason/source references.
- `agent_attempts`: run FK, attempt number, workflow ID, lease epoch, state, retry budget, and timestamps with a unique `(run_id, attempt_no)` constraint.
- `agent_stage_events`: attempt FK, unique `(attempt_id, sequence)` and event key, redacted payload, status, cause, retryability, provider state, duration, and retention timestamp.

All tables have candidate-scoped indexes and foreign keys. The API role has only the required row-scoped read/write privileges; append-only stage events cannot be updated/deleted by the API role. Retention uses a controlled security-definer purge function and a retention role, not ad hoc deletes. The migration backfills canonical lifecycle fields using the mapping above, preserves the old state, and is tested against an old application/new schema and new application/old rows. Rollback is forward-only: stop new workflow scheduling, dual-read legacy fields, reconcile active runs, and keep historical events/audit data. No destructive downgrade is permitted.

The complete normative SQL shape, nullable expand order, exhaustive legacy
backfill, trigger contract, composite foreign keys, typed CAS functions,
privileges, advisory-lock purge, alert/retry payload, and rollback-forward
report are in `docs/agent-console/migration-contract.md`. The implementation
must not substitute a single `NOT NULL DEFAULT` rewrite for the documented
expand/backfill/tighten sequence.

### 8. Security and privacy invariants

Unknown policy, capability, feature flag, provider, dependency, authorization, lifecycle, or browser-readiness states resolve to deny/blocked. The following are acceptance requirements, not implementation suggestions:

- **Model egress:** only an explicitly allowlisted provider host/model may receive data over verified TLS. The server, not the browser, selects the provider. Each operation has an allowlisted field set and requires per-operation consent; raw PDF bytes, credentials, mailbox contents, arbitrary URLs, and unconfirmed evidence are excluded. The server redacts secrets and direct identifiers before send, records provider/region/retention/training policy, and denies when any policy is unknown. SDK debug logs, request bodies, provider caches, browser storage, and service-worker caches are disabled for source content.
- **HTTP/browser egress:** only `http`/`https` and source-allowlisted hosts are accepted. `file:`, `data:`, `javascript:`, loopback, link-local, private, multicast, IPv6-local, and cloud-metadata addresses are denied. DNS is resolved and validated before connect and on every redirect; redirects are limited to three and revalidated against the allowlist. Cookies, `Authorization`, OAuth/session reuse, arbitrary headers, unsafe methods, uploads, and arbitrary ports are denied. Each source has byte, timeout, concurrency, rate, and total-run budgets; the browser sidecar receives no user credentials by default.
- **Output safety:** model/browser/job content is untrusted. HTML is escaped or rendered as safe text; raw HTML, scripts, event handlers, unsafe URLs, arbitrary routes, and model-supplied actions are rejected. Route/action identifiers come from a server allowlist. Evidence references and source spans are revalidated against the candidate/version before display or apply. Content Security Policy and XSS tests cover source text and model output.
- **Authorization and CSRF:** every endpoint is checked against an actor × resource × operation matrix. Candidate operations require ownership of action, context, run, stage, preview, source version, package, and export. Cross-candidate requests return non-enumerating `404`. Every state-changing endpoint, including accept/snooze/dismiss/complete, context creation, plan activation/run-now, retry/stop/review, preview apply, package edit/approve, and export/copy, requires CSRF and idempotency. Package approval/export requires a fresh confirmation; external write remains denied.
- **Idempotency:** the canonical request hash includes actor, resource, operation, and normalized body. Unique constraints and atomic insert/update protect races. Key reuse with a changed request is a conflict; a retry with the same key returns the original receipt. No retry may replay an external side effect because external writes are outside this change.
- **Fencing and budgets:** a stale lease epoch cannot complete a stage. Per-run model calls are capped at five; each model request is capped at 32,000 input tokens, 2,048 output tokens, and 90 seconds. Defaults are at most 10 provider calls/hour **in aggregate across all providers for one candidate** and 100,000 input/20,000 output tokens/day per candidate, two browser tasks concurrently, 2 MiB per HTTP response, 15 seconds per fetch, three redirects, and 60 requests/minute/source. Model calls, retries, browser bytes, and exports consume atomic counters; a Redis/limiter error denies or blocks the operation, never fails open. Exceeding a budget blocks/queues work and explains when it can resume; it never fails open.
- **Audit and retention:** policy denials, cross-candidate attempts, context create/invalidate, action lifecycle, provider allow/deny, consent, preview view/apply/reject, run/attempt/stage transitions, retry/stop races, export/copy, capability changes, and sensitive-source access emit bounded events containing actor/session/request IDs, candidate/resource IDs, old/new state, policy/provider version, reason, and server time. Events are appended through `careerops.append_audit_event`, are read-authorized, and are retained for 365 days. Preview metadata is retained 30 days and redacted traces 90 days unless a documented legal/operational hold exists.
- **Cache and clipboard:** sensitive responses are `no-store`; raw content is not placed in local/session storage, IndexedDB, URL query strings, service-worker caches, or trace logs. Copy/export surfaces show the destination and a privacy warning, require confirmation, and record the event. The UI does not promise that clipboard contents can be erased.

### 9. Ego/browser deployment boundary

The first release uses an optional dedicated browser-worker sidecar, not an implicit CLI in the API or Temporal process. Its image reference must contain an immutable `@sha256:` digest, and readiness fails closed if the digest, binary, policy bundle, or health endpoint is missing. The sidecar accepts only a signed, candidate-scoped, source-allowlisted request from the workflow activity and returns bounded normalized content/provenance; it does not receive OAuth/session credentials and never persists screenshots or page source beyond the configured retention window. Compose and CI must verify both sidecar-ready and sidecar-absent behavior. Sidecar absence marks browser work `dependency_not_ready` while HTTP-only sources and deterministic UI remain usable.

The exact sidecar HTTP bodies/statuses, mTLS/signature/nonce headers,
canonical signed string, response/error schema, subresource interception and
budget limits are frozen in `docs/agent-console/sidecar-wire-contract.md`.

### 10. Chinese-first and accessible state surfaces

All changed pages set `lang="zh-CN"`, use Chinese-first user copy, and keep trace IDs/error codes as secondary technical text. Every form control has a programmatic label; validation errors use `aria-describedby` and focus the first invalid field; run changes use a polite live region; dialogs restore focus; keyboard users can complete the full action/context/review flow; focus is visible; status is not conveyed by color alone; layouts remain usable at 200% zoom; contrast meets the project WCAG 2.2 AA target; and reduced-motion preferences disable nonessential animation. Login/bootstrap, empty dashboard, crawl-plan empty state, Agent workbench, run detail, disabled model, stale, failure, and recovery states are included in the same checks.

## Risks / Trade-offs

- **[Risk] Action queue becomes noisy or prescriptive.** → Cap to three actions, use deterministic reason codes, expose dismiss/snooze, apply an attention budget, and measure completion/dismissal rates without adding push notifications.
- **[Risk] Context envelope references data that changes mid-run.** → Store immutable version/digest pairs, validate at every stage, mark stale, and require a fresh preview instead of silently rebasing.
- **[Risk] Model output contains prompt injection or unsupported claims.** → Isolate untrusted content, use schema validation, evidence allowlists, bounded fields, explicit unknowns, no tool binding, safe rendering, and human review.
- **[Risk] Trace data leaks prompts or personal data.** → Persist hashes, IDs, bounded summaries, token counts, latency, and redacted error codes only; enforce retention and `no-store` responses.
- **[Risk] Retry duplicates a side effect.** → Keep external writes out of the run graph; use idempotency keys, lease fencing, and attempt lineage for all durable writes.
- **[Risk] Ego is unavailable or cannot run in the worker environment.** → Detect readiness before a browser run, return `dependency_not_ready`, retain HTTP-only sources, and never silently scrape through a different path.
- **[Risk] New read models drift from authoritative records.** → Build projections from existing APIs/events, include source versions, and add consistency checks in integration tests.
- **[Risk] Accessibility is treated as polish.** → Keep it in the spec, task IDs, component tests, keyboard browser evidence, and release gate.

## Migration and rollout

1. Freeze contracts, security matrix, state mapping, and fixture-free tests behind disabled flags.
2. Apply `0030_agent_console_orchestration` after the current `0029` head, backfill compatibility fields, and verify expand/contract with the existing 0025 rows and intervening smart-intake revisions.
3. Add context/action projections and read-only endpoints; verify existing pages remain unchanged.
4. Add dashboard action cards and the shared context rail; initially make actions navigation-only.
5. Add Agent workflow/trace/retry/stop semantics and run center; keep model disabled.
6. Reuse the smart-intake preview store and add preflight/consent, structured output, safe rendering, and manual fallback.
7. Add crawl-plan creation/readiness and connect only to the existing crawl workflow and inbox projection.
8. Add matching explanation, resume review, interview preparation, and package review in order; no mail/outbound actions.
9. Run all gates plus three independent adversarial review groups for both this spec and the implementation plan. Pilot with model disabled, then a fake provider, then an explicitly approved provider configuration.

Rollback disables new action/context/run UI, stops scheduling new Agent workflows, lets active work reach a safe terminal state or reconciliation state, retains audit/trace metadata, and leaves authoritative profile/job/application data untouched. If a migration defect is found, use the forward compatibility path; never drop historical run, stage, or audit data.

## Open Questions

- Which provider and deployment region will be approved for the first real model pilot, and what provider retention/training policy will be accepted?
- Which exact source families may use the Ego sidecar in the first release, and what source-specific rate/robots evidence is required?
- What candidate-configured daily attention budget should override the safe defaults while preserving the three-card cap?
- Should a later release add push notifications, or remain in-app only until notification consent and retention are separately specified?
