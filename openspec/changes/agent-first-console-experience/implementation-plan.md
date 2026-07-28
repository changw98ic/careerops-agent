# Agent-first console experience — implementation plan

## Delivery contract

This plan turns the four delta specs into an executable, reviewable change. It is intentionally staged so the safe default remains useful before any real model or Ego pilot is enabled. This is currently a design-only plan: target implementation/test artifacts named below are deliverables for later phases, not claims that those runtime gates have already passed.

The change is not complete when the UI renders. It is complete only when:

1. the existing `agent_runs`/`agent_run_reviews` authority is extended without a competing persistence path;
2. the API, database, Temporal, browser, model, frontend, and accessibility contracts have executable evidence;
3. all unknown policy/capability/dependency/authorization states fail closed;
4. model, OAuth, external writes, auto-send, and model tools remain disabled by default;
5. three fresh adversarial groups have reviewed both this plan and the specs, all findings have been fixed, and every final lens score is at least 95/100.

No synthetic M1 pilot data may be created. `make verify-m1-full` remains an expected failure until approved real pilot data exists.

## Scoring rubric for each adversarial group

Each independent reviewer scores the artifact out of 100. A group passes only when every lens is at least 95 and no P0/P1 finding remains.

| Lens | Weight | Pass evidence |
| --- | ---: | --- |
| Security, privacy, and default-deny | 25 | egress/SSRF/auth/CSRF/idempotency/fencing/audit/cache/retention/budget contracts and negative tests |
| Product, UX, accessibility, and Ego alignment | 20 | first-run, action queue, AI preflight, empty/disabled/failure states, Chinese-first copy, keyboard/WCAG evidence |
| Architecture and feasibility | 20 | authority map, existing-state compatibility, API/Temporal contract, Ego deployment boundary |
| Data/migration/rollback | 15 | Alembic schema, constraints/grants, backfill, rolling compatibility, rollback-forward evidence |
| Verification and operability | 20 | scenario-to-test matrix, exact commands, CI/Compose/frontend/DB/Temporal/browser evidence, alerts/runbook |

The parent agent records the pre-fix score, finding, change made, and post-fix score in `review-evidence.md`. A structural OpenSpec pass is necessary but never counts as a 95 score by itself.

## Phase plan

### P0 — Baseline, authority, and contract freeze

Dependencies: none. Owner: backend/API lead with security reviewer.

Deliverables:

- Confirm current routes and owners: existing `/api/v1/agents/*`, `/api/v1/smart-intake/previews*`, `/api/v1/crawl-plans/*`, `/api/v1/matches/run`, resume-version service, and application package routes.
- Add a machine-readable contract module for canonical lifecycle fields, legacy mapping, capability states, action states, error codes, budgets, redaction rules, `ProviderPolicy`, `ConsentEnvelope`, and `EgressDecision`.
- Add `docs/agent-console/authorization-matrix.md` with the candidate/operator/worker/retention-service × resource × operation matrix, including read/write/deny, non-enumerating errors, CSRF, Origin, and fresh-confirmation rules.
- Add `docs/agent-console/egress-contract.md` with exact per-operation outbound fields, redaction version, provider policy fields, consent nonce/revocation, and the forced browser network boundary.
- Add `docs/agent-console/authority-map.md` mapping every old route/table/change to its authoritative adapter and explicitly excluding outbound actions.
- Add JSON Schemas at `docs/agent-console/provider-policy.schema.json`, `consent-envelope.schema.json`, and `egress-decision.schema.json`, plus the exact `migration-contract.md` and `budget-contract.md` annexes.
- Add `docs/agent-console/api-contract.md` as the endpoint-by-endpoint source for request/response JSON, status/error matrix, cursor signing/expiry, CSRF and idempotency binding, `no-store`, and the smart-intake/crawl/resume/package adapters.
- Add `docs/agent-console/temporal-contract.md` for every workflow/activity/signal/query payload, receipt, typed error, retry, lease/fencing SQL, heartbeat, cancellation, and worker identity rule.
- Add `docs/agent-console/sidecar-wire-contract.md` for exact `/ready` and `/v1/browser/run` HTTP bodies/statuses, signature/nonce headers, subresource interception, and bounded failures.
- Add `docs/agent-console/sidecar-wire.schema.json` and validate ready/not-ready, full/partial success, failure, timestamp/nonce, and HTTP status mappings against the prose wire contract.
- Add `docs/agent-console/scenario-register.json`, `scenario-evidence.schema.json`, `release-gate-evidence.schema.json`, and `retention-ci-contract.md`; these close all 106 scenario IDs, release-gate artifacts, artifact paths, gate owners, retention retry/alert rules, and fail-closed CI exit behavior.
- Freeze the authoritative destination table in `design.md` and use it as the review baseline.

Acceptance evidence:

- Contract tests cover every enum and reject unknown values.
- An API review verifies no route creates a second run/crawl/preview authority.
- The disabled environment produces zero provider calls and no external writes.

### P1 — Additive PostgreSQL migration and compatibility

Dependencies: P0. Owner: database/API lead.

Deliverables:

- Create `migrations/versions/0030_agent_console_orchestration.py` after repository head `0029_add_proposal_value_digests`.
- Extend `agent_runs` and `agent_run_reviews`; add `agent_contexts`, `agent_actions`, `agent_attempts`, and append-only `agent_stage_events`.
- Add candidate FKs, state checks, unique idempotency/event keys, cursor indexes, retention timestamps, payload size checks, API/runtime/retention/legacy-compatibility grants, and no raw content columns.
- Add the `(id, candidate_id)` unique key and composite foreign keys, append-only event trigger/privileges, compatibility trigger plus dedicated legacy read/write functions for legacy `state`, CAS conditions for lifecycle/lease writes, and a no-row-level-authorization bypass.
- Backfill legacy `pending/unavailable/abstained/reviewed` values to canonical fields while preserving `legacy_state`; use composite `(run_id, candidate_id)` foreign keys for every child record.
- Use security-definer retention/purge and candidate-scoped audit-read functions; the API role cannot directly insert audit rows or update/delete stage events. Retention runs hourly, respects legal holds, and alerts on failure.
- Implement the complete `migration-contract.md` sequence: nullable expand, exhaustive backfill, explicit tighten, compatibility trigger/function, composite FKs, typed CAS functions, exact role grants/revokes, advisory-lock purge, three-attempt alert payload, and rollback-forward report. No `NOT NULL DEFAULT` rewrite may replace the expand/backfill steps.

Acceptance evidence:

- `CAREEROPS_TEST_DATABASE_URL=... make verify-db` passes with a disposable PostgreSQL.
- Upgrade from the current 0029 head (including 0025 Agent rows and 0026–0029 smart-intake rows), read old rows with the new application, and exercise rollback-forward reconciliation.
- Concurrent inserts with the same idempotency key produce one receipt; cross-candidate reads return non-enumerating 404.
- Old application/new schema and new application/old row compatibility tests prove recognized legacy writes are dual-mapped and unknown values are rejected.

### P2 — Context, action projection, and API mutations

Dependencies: P1. Owner: API/application lead.

Deliverables:

- Implement server-owned context creation/read/invalidation and digest/version checks.
- Implement deterministic action projection, source-event invalidation, queue version, 30-second polling/focus refresh, in-app-only deduplication, goal scope, and bounded attention budget.
- Implement the action endpoints and common request/error envelope from the spec.
- Require CSRF and `Idempotency-Key` on all mutations; bind keys to actor/resource/operation/body hash and return `409 IDEMPOTENCY_CONFLICT` on mismatch.
- Append bounded audit events through `careerops.append_audit_event` using the business transaction connection; audit/policy/budget failure rolls back the mutation. Read audit data through the candidate-scoped security-definer read contract.

Acceptance evidence:

- API contract tests cover success, 401/403/404/409/422/503, cursor bounds, stale queue version, idempotency races, and no-store headers.
- API contract tests are generated from every row in `api-contract.md`; they assert exact request/response fields, CSRF cookie/header equality, Origin, signed cursor scope/expiry, adapter compatibility, replay/conflict behavior, no-store headers, and audit rollback.
- Integration tests prove source events increment the queue once and do not leak candidate content.
- Read-only dashboard works with model disabled and no pilot data.

### P3 — Durable Agent workflow and traces

Dependencies: P1 and P2. Owner: Temporal/application lead.

Deliverables:

- Register `AgentRunWorkflow` on `careerops-agent` with workflow ID `agent-run:{candidate_id}:{logical_run_id}`.
- Keep model/network/database/browser work in activities; add `stop` and `refresh_context` signals and redacted status query.
- Implement 10-second heartbeat, 90-second model activity timeout, 30-second cancellation acknowledgement, at most two retries with exponential backoff/jitter, lease epoch fencing, poison-input blocking, and reconciliation.
- Freeze activity input/output JSON, lease acquire/renew/CAS SQL conditions, worker identity, cancel epoch, workflow reuse policy, signal/query behavior, and the `careerops-agent` worker registration. The old `careerops-m0` worker must not claim Agent runs.
- Implement the exact payload/receipt/error tables and SQL in `docs/agent-console/temporal-contract.md`; a contract test must reject an old worker ID, stale lease epoch, post-cancel completion, duplicate event key, or retry that skips preflight/consent.
- Emit append-only stage events with event ID, sequence, attempt, schema, cause, terminal bit, redaction, provider state, duration, retryability, and retention.
- Extend existing Agent list/detail/review routes compatibly and add stages/retry/stop with candidate scope and no-store.
- Build reload-safe Run Center with trace copy, progress, clarification, recovery, live region, and no fake completion.

Acceptance evidence:

- `make verify-temporal` plus workflow replay, cancel, duplicate completion, worker restart, stale input, and old lease tests.
- One retry key creates one new attempt; a fenced worker cannot write success.
- Trace detail never contains raw prompt, output, source bytes, credential, cookie, or mailbox content.

### P4 — Truthful model preflight, preview, apply, and output safety

Dependencies: P0–P3. Owner: model gateway/frontend lead. Model provider remains disabled in default CI.

Deliverables:

- Reuse smart-intake preview persistence where the surface is the same; do not create a generic second store.
- Implement capability resolution with provider host/model/region/retention/training policy, consent, release flag, prerequisite, budget, and dependency checks.
- Recheck `ConsentEnvelope`, `ProviderPolicy`, field-set hash, revocation, and atomic budgets when a queued run is claimed and immediately before provider connection; Redis/limiter failure denies.
- Wire the provider host allowlist into the existing model factory/config path; an empty allowlist or unknown provider policy is a configuration error that denies before network connection, not an “allow all” default.
- Implement preflight showing goal, sources, exact allowlisted/redacted fields, budgets, provider state, expected output, expiry, and remove/cancel controls.
- Validate typed output, evidence, qualitative uncertainty, schema/prompt versions, context digest, expiry, fingerprint, field decisions, and authoritative destination before apply.
- Add safe text renderer, CSP, URL/route/action allowlists, evidence revalidation, no tools, one bounded repair attempt, and no raw content in logs/caches/storage.
- Implement rate-limit, timeout, invalid-output, prompt-injection, unsupported-claim, user-cancel, clarification, and manual fallback states.

Acceptance evidence:

- Fake provider tests show only allowlisted fields are sent and source removal changes the fingerprint.
- Disabled provider tests prove zero provider network calls and deterministic/manual fallback.
- XSS/unsafe URL/route and cache/storage scans pass; provider policy unknown denies before connection.

### P5 — Crawl-to-inbox and Ego/browser sidecar

Dependencies: P1–P3 and existing crawl authority. Owner: crawl/worker lead.

Deliverables:

- Add crawl-plan empty-state CTA, draft/version/activate contract, source allowlist/policy/schedule/scope preview, and readiness state.
- Reuse `CrawlRunWorkflow`, normalized posting, canonical job, and inbox projection; add provenance/read-model counts and idempotent partial-success handling.
- Choose a dedicated browser-worker sidecar. The configured image must use an immutable `@sha256:` reference; the sidecar health/policy bundle is a hard readiness gate.
- The sidecar must enforce a forced egress proxy/network interceptor for every page subrequest, use a fresh isolated task space, reject tab/session reuse, and bind DNS validation to the actual connection; a CLI-only executor is not accepted as the browser contract.
- Implement the exact `/ready` and `/v1/browser/run` request/response/error wire shapes, mTLS/signature/nonce rules, HTTP statuses, digest checks, and fail-closed absent-sidecar behavior in `docs/agent-console/sidecar-wire-contract.md`.
- Enforce DNS/IP/private-address/redirect/scheme/header/method/size/timeout/rate/concurrency controls at the HTTP/browser boundary. No cookies, OAuth, or Authorization headers enter the sidecar.
- Add sidecar-ready and sidecar-absent Compose/CI evidence; absence yields `dependency_not_ready` and preserves permitted HTTP/manual paths.

Acceptance evidence:

- `make verify-compose` and the dedicated browser harness prove health, image digest, policy denial, no credentials, and cleanup.
- Tests cover SSRF redirect attacks, private/metadata IPs, browser interruption, replay, duplicate postings, zero results, and partial results.

### P6 — Career-loop pages and accessibility

Dependencies: P2–P5. Owner: frontend/product lead.

Deliverables:

- Add dashboard action queue/context rail, first-run login/bootstrap/no-data path, crawl-plan creation, run center, matching explanation, resume review, interview preparation, and package review continuity.
- Implement the exact route journey `/login → /dashboard → /profile → /resumes → /evidence → /crawl-plans → /crawl-runs/:id → /inbox/:id → /ai-workbench?tab=matching → /ai-workbench?tab=resume → /ai-workbench?tab=interview → /applications/:id`, preserving only server-issued `context_id` references.
- Keep deterministic matching and existing package/resume APIs authoritative; show model explanation/drafts as review-only.
- Exclude mail/reply/outbound actions from the new queue and surface external-write denial truthfully.
- Apply `lang="zh-CN"`, Chinese-first copy, programmatic labels, error associations, live regions, focus recovery, keyboard-only completion, visible focus, non-color status, 200% zoom, contrast, and reduced-motion behavior.

Acceptance evidence:

- Frontend component tests and `make verify-frontend` pass, including bundle gate.
- Ego/browser evidence walks the no-data path immediately; the populated path (login → profile → resume → evidence → crawl plan → inbox/match → resume review → interview → package review) runs only with approved real pilot evidence, never synthetic data.
- Keyboard and accessibility checks cover every changed form/control and all disabled/stale/failure states.
- Ego assertions explicitly cover `aria-live="polite"`, first-error focus, dialog focus trap/restore, Tab/Enter/Space behavior, visible focus, 200% zoom, contrast/non-color state, reduced motion, and Chinese copy on each changed route.

### P7 — Verification, rollout, and operational handoff

Dependencies: P0–P6. Owner: release/operations lead.

Deliverables:

- Maintain the scenario-to-test/evidence matrix and link evidence files/CI jobs.
- Run a plan/spec consistency check that parses `scenario-register.json`, counts exactly 106 unique scenario IDs (AO 22, AR 20, LL 28, CL 36), verifies every `#### Scenario` heading including modified-contract IDs, validates all JSON Schemas, and fails on an unowned scenario, duplicate ID, wrong gate, or missing evidence path. A route-set diff check must also parse the finite method/path rows in `api-contract.md` and `authorization-matrix.md`, reject shorthand-only rows or a route present in only one annex, and reject the forbidden `POST /api/v1/crawl-plans` root mutation.
- Run `make verify`, `make verify-db`, `make verify-frontend`, `make verify-compose`, `make verify-temporal`, `make security`, OpenSpec strict validation, and `git diff --check`.
- Add rollout flags, denial/budget/rate/queue lag alerts, retention monitoring, stop/rollback runbook, and compatibility-window monitoring.
- Add CI jobs or make targets for Agent Temporal tests, migration rollback-forward, sidecar-ready/absent, browser/accessibility evidence, model egress capture, and no-store/cache/XSS checks; upload evidence artifacts without secrets.
- Add the retention/CI controls in `retention-ci-contract.md`: advisory-lock purge, three retries (1s/4s/16s + jitter), legal-hold skip, safe alert payload, secret scan, scenario/release-gate schema validation, and non-zero exit on any missing assertion/artifact/score.
- Gate each named scenario artifact on both the command exit code and artifact existence; UI/Ego records must be `artifacts/ego/<scenario-id>.json` with required screenshots, while API/DB/Temporal/model/browser-boundary/operations records must be `artifacts/agent-console/<scenario-id>.json`. Release-blocking records must additionally validate against `release-gate-evidence.schema.json` at `artifacts/agent-console/<gate>-<commit>.json`. Missing records, expected screenshots, API receipts, migration output, or Temporal replay output is a failure, not an unverified warning.
- Default deployment keeps `MODEL_PROVIDER=disabled`, `CAREEROPS_EXTERNAL_WRITES_ENABLED` unset/false, `CAREEROPS_AUTO_SEND_ENABLED` unset/false, and `CAREEROPS_GOOGLE_OAUTH_ENABLED` unset/false.
- Complete three fresh independent adversarial groups for both spec and plan; fix each finding, rerun validation, and record final scores in `review-evidence.md`.

### Release-blocking evidence gates

These gates make the score evidence-based rather than prose-based:

| Gate | Required assertion | Blocking failure |
| --- | --- | --- |
| `MODEL-02` | fake provider captures exact allowlisted outbound JSON; consent revoke/policy drift denies at worker claim and connect | any disallowed field or stale consent sent |
| `CRAWL-02` | browser proxy intercepts subresources; DNS/redirect/IDN/private-IP/IPv4-mapped IPv6 checks bind to connect | any internal or unallowlisted request reaches the sidecar |
| `AUTH-02` | actor/resource matrix, Origin/CSRF, composite ownership, idempotency race, fixed field decisions | cross-candidate read/write or replay succeeds |
| `DB-02` | 0025 Agent rows + 0026–0029 smart-intake rows upgrade to 0030 with dual-write/CAS/composite FKs and the DDL annex | old/new rolling compatibility or constraint test fails |
| `AUDIT-02` | business mutation and append-audit share one connection/transaction; audit failure rolls back; read is scoped | business mutation commits without its audit decision |
| `TEMP-02` | lease acquire/renew/CAS, cancel epoch, old-worker rejection, dead-letter, retry budget | fenced or cancelled worker writes authoritative success |
| `PRIV-02` | hourly purge, legal hold, failure alert, no-store, no persistent raw content, provider redaction | raw source/cache/retention leak or unmonitored purge failure |

## Verification matrix

| Evidence ID | Contract covered | Primary artifact/command |
| --- | --- | --- |
| API-01 | action/context/run endpoint schemas, auth, CSRF, idempotency, errors | `tests/contract/test_agent_console_api.py` + `make verify` |
| DB-01 | 0030 upgrade from 0029, constraints, composite FKs, grants, isolation, retention | `tests/integration/test_agent_console_migration.py` + `make verify-db` |
| TEMP-01 | workflow replay, heartbeat, cancel, retry, fencing | `tests/unit/test_agent_workflow.py`, `make verify-temporal` |
| MODEL-01 | disabled/fake provider, egress, consent, schema, injection, rate limit | `tests/unit/test_model_egress_policy.py`, model contract suite |
| CRAWL-01 | plan/readiness, SSRF, Ego absent/ready, replay/projection | crawl integration/browser harness + `make verify-compose` |
| UX-01 | dashboard/empty/disabled/failure/action/context flow | frontend component tests + Ego screenshots/DOM evidence |
| A11Y-01 | labels, keyboard, focus, live regions, zoom, contrast, motion | frontend accessibility suite + keyboard Ego run |
| OPS-01 | alerts, budgets, retention, stop/rollback, no synthetic data | runbook review + CI artifacts + operational smoke |

The plan/spec consistency gate is `uv run python scripts/verify_agent_console_spec.py --change openspec/changes/agent-first-console-experience`; it parses the four spec files, counts the modified/added requirements and 106 scenarios, resolves register IDs and artifact basenames, validates the JSON Schemas, and fails if any required evidence command/path is absent. The same checker performs the route-set diff: it rejects API/authorization drift, shorthand-only mutation rows, and the forbidden root crawl-plan mutation. A release CI invocation adds `--require-artifacts --commit "$GITHUB_SHA"`; that mode requires all 106 scenario JSON artifacts, all seven release-gate artifacts, exact commit/path binding, and every registered screenshot file. This checker is now part of the change's documentation gate; runtime implementation and evidence production remain future P7 deliverables in the current design-only state.

The file names above are target evidence locations. If an existing test module is the correct owner, the implementation must update the matrix rather than silently omit the evidence.

## Scenario evidence registration

Scenario IDs are deterministic and reviewable: use the two-letter capability prefix (`AO`, `AR`, `LL`, `CL`), the requirement number in file order, and the scenario number within that requirement, for example `AO-R1-S1`. The implementation must add the ID to the test name, browser evidence name, and review record. The following register assigns every current scenario to a primary evidence family; a scenario may also have secondary evidence, but it may not have no owner.

| ID range | Source | Primary evidence owner |
| --- | --- | --- |
| `AO-M1-S1` | Modified existing Agent workspace navigation | `UX-01` (A11Y-01 secondary) |
| `AO-R1-S1..S5` | Candidate-scoped action queue | `API-01`, action projection integration |
| `AO-R2-S1..S3` | Action lifecycle and safe navigation | `API-01`, idempotency/CSRF tests |
| `AO-R3-S1..S3` | Shared career context | `DB-01`, context API tests |
| `AO-R4-S1..S2` | Proactive attention budget | `UX-01`, queue projection tests |
| `AO-R5-S1..S3` | Event-driven refresh/deduplication | `API-01`, multi-tab browser evidence |
| `AO-R6-S1..S3` | Safe action mutation contract | `API-01`, audit/atomicity tests |
| `AO-R7-S1..S2` | Accessible action surface | `A11Y-01`, keyboard browser evidence |
| `AR-M1-S1` | Modified existing Agent run persistence | `DB-01`, `TEMP-01` |
| `AR-R1-S1..S4` | Durable Agent run state | `TEMP-01`, compatibility integration |
| `AR-R2-S1..S3` | Trace-correlated stages | `TEMP-01`, `OPS-01` redaction tests |
| `AR-R3-S1..S5` | Retry/stop/recovery | `TEMP-01`, fencing/cancel tests |
| `AR-R4-S1..S2` | Operational read model | `API-01`, `A11Y-01` |
| `AR-R5-S1..S2` | Temporal/endpoint/database compatibility | `DB-01`, `TEMP-01` |
| `AR-R6-S1..S3` | Redacted audit/retention/metrics | `DB-01`, `OPS-01` |
| `LL-M1-S1` | Modified existing smart-intake authority | `MODEL-01`, `API-01` |
| `LL-R1-S1..S4` | Capability resolution | `MODEL-01`, disabled-provider browser evidence |
| `LL-R2-S1..S4` | Structured preview/apply | `MODEL-01`, smart-intake contract |
| `LL-R3-S1..S3` | Untrusted content isolation | `MODEL-01`, XSS/prompt-injection tests |
| `LL-R4-S1..S4` | Provider egress/consent | `MODEL-01`, privacy evidence |
| `LL-R5-S1..S4` | Fallback/error UX | `MODEL-01`, `UX-01` |
| `LL-R6-S1..S2` | Privacy/usage telemetry | `MODEL-01`, `OPS-01` |
| `LL-R7-S1..S2` | Safe output/evidence revalidation | `MODEL-01`, XSS/browser evidence |
| `LL-R8-S1..S2` | Human-friendly progress/clarification | `UX-01`, `TEMP-01` |
| `LL-R9-S1..S2` | Mandatory preflight | `MODEL-01`, `AUTH-02` |
| `CL-M1-S1` | Modified existing crawl/matching/resume/package authority | `API-01`, `DB-01` |
| `CL-R1-S1..S4` | Crawl plan/readiness | `CRAWL-01`, `UX-01` |
| `CL-R2-S1..S7` | Crawl-to-inbox projection | `CRAWL-01`, `DB-01`, `CRAWL-02` |
| `CL-R3-S1..S3` | Deterministic matching | `CRAWL-01`, `MODEL-01` |
| `CL-R4-S1..S5` | Resume/interview continuity | `UX-01`, `MODEL-01`, API contract |
| `CL-R5-S1..S4` | Application package boundary | `API-01`, `DB-01`, `OPS-01` |
| `CL-R6-S1..S2` | End-to-end progress/recovery | `UX-01`, `TEMP-01` |
| `CL-R7-S1..S2` | Login/bootstrap/accessibility | `A11Y-01`, Ego evidence |
| `CL-R8-S1..S2` | Mail/outbound boundary | `API-01`, security negative test |
| `CL-R9-S1..S6` | Executable route journey/accessibility | `UX-01`, `A11Y-01`, Ego evidence |

There are currently exactly 106 scenarios: AO 22, AR 20, LL 28, and CL 36, including one modified-contract scenario per spec. Every scenario evidence record MUST include the assigned ID, route when applicable, viewport (`1440×900` and `375×812` for UI scenarios), precondition/data state, operation sequence, API/DB/Temporal expected state, accessible-name/DOM assertions for UI scenarios, and the exact artifact path from `docs/agent-console/scenario-register.json`: UI/A11y primary evidence uses `artifacts/ego/<scenario-id>.json`; API/DB/Temporal/model/browser-boundary/operations primary evidence uses `artifacts/agent-console/<scenario-id>.json`. UI/A11y records also require at least one screenshot under `artifacts/ego/`. The evidence command must fail when a required artifact is absent. If a later review adds a scenario, it must extend this table before implementation begins; no scenario may be silently unowned.

The UI evidence contract has these route-specific assertions:

| Route | Required assertions | Artifact |
| --- | --- | --- |
| `/login` | `lang=zh-CN`, labelled username/password, `aria-describedby` error, first invalid focus, login CTA | `artifacts/ego/CL-R7-S2.json` |
| `/bootstrap` | setup-complete branch, labelled `返回登录`, no token/account leak, safe `/login` navigation | `artifacts/ego/CL-R9-S4.json` |
| `/dashboard` | action queue, one no-data primary CTA, queue-version live announcement, focus after accept/snooze | `artifacts/ego/AO-R7-S1.json` |
| `/crawl-plans` and `/crawl-runs/:id` | `创建职位抓取计划`, durable run redirect, trace/provenance/recovery state | `artifacts/ego/CL-R1-S1.json` and `CL-R9-S6.json` |
| `/ai-workbench?tab=matching|resume|interview` | query tab allowlist, context_id restore, disabled/preflight/progress/cancel/review state, no raw output | `artifacts/ego/CL-R9-S5.json` |
| `/applications/:id` | package review/apply/export boundary, send actions absent/denied, focus/live region | `artifacts/ego/CL-R8-S1.json` |

The browser runner must assert DOM accessible names, state text, route/query, API status, and focus target; a screenshot alone cannot pass an interaction scenario.

## Rollback and stop procedure

1. Disable action/context/run release flags and stop scheduling new Agent workflows.
2. Keep API reads for historical traces under the existing ownership/redaction contract.
3. Signal active workflows to stop; reconcile unacknowledged cancellations to `CANCEL_TIMEOUT`/blocked within the 30-second bound.
4. Disable model/browser capabilities independently; retain deterministic matching, HTTP-only permitted crawl, manual forms, and package review.
5. If schema behavior is faulty, deploy the forward compatibility migration and continue dual-read of legacy fields; do not drop run/stage/audit history.
6. Record the incident, policy decision, trace IDs, affected release flag, and recovery evidence through the append-only audit path.

## Remaining release blockers

The change is not pilot-ready if any of these remain unresolved: no immutable Ego image/readiness evidence for a browser-backed source; no disposable PostgreSQL migration evidence; no Compose health evidence; no frontend bundle gate; any provider/OAuth/external-write flag enabled by default; any unknown state treated as success; any raw source in logs/traces/browser storage; any cross-candidate read; or any final adversarial lens below 95.
