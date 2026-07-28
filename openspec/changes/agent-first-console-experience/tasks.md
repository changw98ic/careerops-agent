## 1. Contract, ownership, and safety foundation

- [ ] 1.1 Freeze the canonical `execution_state`, `capability_state`, `review_state`, legacy mapping, action states, reason codes, context envelope, preview fingerprint, stage event, and error schemas; add contract tests for every enum and unknown-value deny behavior.
- [ ] 1.2 Publish `docs/agent-console/authorization-matrix.md` with the actor × resource × operation authorization matrix for actions, contexts, previews, runs, stages, retries, stops, reviews, crawl plans/runs, resume versions, interview drafts, packages, exports, metrics, and operator trace access.
- [ ] 1.3 Freeze the common API envelope, exact endpoint paths, status/error codes, cursor rules, CSRF requirement, `Idempotency-Key` binding/24-hour TTL/conflict behavior, `no-store` headers, and Chinese-first safe messages.
- [ ] 1.4 Map each operation to its authoritative existing write endpoint: deterministic matching remains `filter_decisions`; resume apply uses the resume-version contract; interview remains an editable review draft; application uses the existing package API; external write/send remains denied.
- [ ] 1.5 Publish `docs/agent-console/egress-contract.md` defining `ProviderPolicy`, `ConsentEnvelope`, `EgressDecision`, exact per-operation field allowlists, consent revoke/recheck, provider policy/version, redaction, output sanitization, CSP, URL/route/action allowlists, and source/evidence revalidation tests.
- [ ] 1.6 Define the HTTP/browser SSRF sidecar contract: forced subrequest interceptor/proxy, source host allowlist, `http`/`https` only, DNS/IP validation bound to every connect/redirect, private/metadata/unsafe scheme denial, no cookies/authorization/OAuth/service workers, safe methods, byte/timeout/concurrency/rate budgets, fresh task spaces, and no browser credentials.
- [ ] 1.7 Define audit taxonomy and transaction boundaries through `careerops.append_audit_event`; cover cross-candidate denial, consent/provider decisions, context/action/run/stage/retry/stop/review/export/capability changes, read authorization, retention/legal holds, and bounded fields. The writer must use the business connection and audit failure must roll back the mutation.
- [ ] 1.8 Define per-candidate/provider/run model, browser, token, byte, concurrency, runtime, retry, and export budgets plus fail-closed behavior and negative tests.
- [ ] 1.9 Keep `docs/agent-console/provider-policy.schema.json`, `consent-envelope.schema.json`, and `egress-decision.schema.json` machine-valid; enforce operation-to-field-set closure and require non-null consent on every allowed model decision.
- [ ] 1.10 Keep `docs/agent-console/api-contract.md` as the single endpoint-level source for bodies, responses, status/error codes, cursor signing/expiry, CSRF, idempotency, ownership, and `no-store`; update existing-authority adapter rows when upstream APIs change.

## 2. PostgreSQL compatibility and persistence

- [ ] 2.1 Implement additive Alembic revision `0030_agent_console_orchestration` after repository head `0029_add_proposal_value_digests`; do not create a second `agent_runs` authority.
- [ ] 2.2 Extend `agent_runs`/`agent_run_reviews` with canonical lifecycle fields, legacy mapping, context/attempt references, field decisions, request hash, and idempotency constraints.
- [ ] 2.3 Add candidate-scoped `agent_contexts`, `agent_actions`, `agent_attempts`, and append-only `agent_stage_events` tables with FKs, check constraints, unique keys, indexes, redacted payload limits, and retention timestamps.
- [ ] 2.4 Add API/runtime/retention grants; prevent API-role direct audit/stage-event deletion or update; use controlled security-definer purge functions for retention.
- [ ] 2.5 Backfill legacy values deterministically, test old/new application compatibility, active-run reconciliation, migration idempotency, and rollback-forward without destructive downgrade.
- [ ] 2.6 Add database tests for candidate isolation, concurrent idempotency, lease fencing, append-only events, retention, foreign-key rejection, and safe partial crawl/run projection.
- [ ] 2.7 Implement the full `migration-contract.md` expand/backfill/contract sequence: nullable expand, exhaustive legacy mapping, composite FKs, typed CAS functions, exact grants/revokes, advisory-lock purge, legal-hold skip, retry/alert payload, and rollback-forward report.

## 3. Server context and proactive action projection

- [ ] 3.1 Implement server-owned context creation/read/invalidation with `context_id`, schema/digest metadata, immutable source version snapshots, expiry, allowed operation, and candidate ownership checks.
- [ ] 3.2 Implement the deterministic action projection with at most three cards, stable server keys, queue version, source event key, rank/reasons, goal scope, policy, freshness, expiry, and safe route allowlists.
- [ ] 3.3 Emit one queue invalidation event per material source change in the authoritative transaction; implement 30-second visible polling/focus refresh and multi-tab/device delivery deduplication.
- [ ] 3.4 Implement accept/snooze/dismiss/complete endpoints with queue-version checks, bounded snooze, CSRF, idempotency, audit, and no external writes.
- [ ] 3.5 Add API/repository/integration tests for cross-candidate denial, stale context, source-change reintroduction, duplicate refresh, action races, quiet-hours/goal scope, empty state, and model-disabled fallback.

## 4. Durable Agent workflow and run center

- [ ] 4.1 Implement `AgentRunWorkflow` on `careerops-agent` with stable workflow/run/attempt IDs, activity-only model/network/database work, status query, stop/refresh signals, heartbeat, timeout, retry, and cancellation policies.
- [ ] 4.2 Publish the Agent workflow/activity JSON contracts and lease/fencing protocol (worker identity, acquire/renew/CAS, lease epoch, cancel epoch, retry budget, dead-letter/reconciliation), then implement transition guards, stale-source detection, post-cancel completion rejection, poison-input blocking, and cancellation timeout.
- [ ] 4.3 Implement bounded append-only stage events with event ID, sequence, attempt, schema, cause, terminal bit, deduplication key, redaction, provider state, duration, retryability, and retention.
- [ ] 4.4 Implement candidate-scoped list/detail/stage/readiness endpoints and compatible existing start/review routes with cursor pagination, non-enumerating errors, `no-store`, and typed empty/blocked/unavailable/failed responses.
- [ ] 4.5 Implement retry/stop/review idempotency and field-decision validation; preserve attempt lineage and prevent duplicate authoritative writes.
- [ ] 4.6 Add frontend run center with reload-safe state, timeline, trace copy, provider/capability state, progress/clarification, retry/stop controls, stale-source explanation, recovery CTAs, and live announcements.
- [ ] 4.7 Implement exactly the workflow/activity/signal/query payloads, lease SQL, fencing predicates, heartbeat/cancellation deadlines, and typed error/retry table in `temporal-contract.md`; add replay and old-worker rejection evidence.

## 5. Truthful model preview/apply and safe rendering

- [ ] 5.1 Implement backend capability resolution for policy, provider configuration, release flag, dependencies, prerequisites, consent, source freshness, rate limits, and failure; disabled mode performs zero provider calls.
- [ ] 5.2 Reuse the smart-intake preview store/contract where applicable; freeze typed preview fields, evidence/source refs, unknowns, qualitative confidence, warning, model/schema/prompt versions, digest, expiry, and fingerprint.
- [ ] 5.3 Implement preflight source selection/removal, exact redacted payload summary, budget display, clarification state, user cancel, bounded progress, and manual/deterministic fallback.
- [ ] 5.4 Implement apply validation for expiry, digest, ownership, evidence revalidation, field decisions, CSRF, idempotency, schema, and authoritative resume/interview/package destinations.
- [ ] 5.5 Harden prompt envelopes and output handling for untrusted job/resume/mail/browser/user content, bounded inputs/outputs, one repair attempt, no tools, safe text/HTML/URL/route rendering, CSP, and unsupported-claim rejection.
- [ ] 5.6 Add fake-provider, disabled-provider, unknown-policy, consent-withdrawal, timeout, 429, malformed-output, prompt-injection, unsafe-markup, oversized-input, stale-preview, cache, retention, and privacy telemetry tests.

## 6. Crawl-to-inbox and Ego boundary

- [ ] 6.1 Add crawl-plan creation/empty-state CTA, source registration, allowlist/policy status, schedule, profile prerequisite, scope preview, draft version, CSRF/idempotency, and explicit activation.
- [ ] 6.2 Connect activated plans to the existing Temporal crawl workflow/read model; record provenance, counts, normalization/deduplication, policy decisions, retry time, and trace without duplicate crawl persistence.
- [ ] 6.3 Implement the digest-pinned Ego/browser sidecar readiness contract, signed bounded requests, source policy, no credentials, health/readiness, retention, and Compose/CI sidecar-ready/absent checks.
- [ ] 6.4 Enforce SSRF/redirect/DNS/private-IP/unsafe-scheme/header/method/size/rate/concurrency controls in HTTP and browser execution; preserve allowed HTTP fallback only when the source policy permits it.
- [ ] 6.5 Project normalized postings into existing canonical job/inbox contracts with idempotent replay, partial-success semantics, zero-result handling, duplicate suppression, and no synthetic pilot data.
- [ ] 6.6 Add tests for source blocked, browser unavailable, redirect attack, worker interruption, replay, partial result, policy-denied fallback, and provenance retention.
- [ ] 6.7 Implement the exact `/ready` and `/v1/browser/run` bodies/statuses/signature/nonce checks from `sidecar-wire-contract.md`, including subresource interception and task-space reuse denial.

## 7. Career-loop pages and accessibility

- [ ] 7.1 Add dashboard action queue/context rail and cross-module navigation for profile, resume, evidence, crawl, inbox, matching, resume review, interview preparation, and application package review.
- [ ] 7.2 Add first-run login/bootstrap/no-data path, accurate setup-complete messaging, Chinese-first localization, and no fabricated metrics.
- [ ] 7.3 Add deterministic-first matching explanations; keep `filter_decisions` authoritative and attach optional model explanation only as review-only output.
- [ ] 7.4 Add resume review diffs, evidence links, immutable review records, validated resume-version apply path, and stale/unsupported-claim blocking.
- [ ] 7.5 Add interview preparation continuity with verified facts/user goals separated, editable draft output, clarification, deterministic fallback, and review boundary.
- [ ] 7.6 Connect approved inputs to the existing package editor/approve/export contract with field-level evidence checks, idempotency, safe text export, and permanent external-write/auto-send denial.
- [ ] 7.7 Implement shared empty/blocked/unavailable/stale/failed/loading/status components; add labels, `aria-describedby`, focus management, live regions, keyboard paths, visible focus, non-color state, 200% zoom, contrast, and reduced motion.
- [ ] 7.8 Add component/browser tests for all changed controls and the full keyboard path from login/no-data dashboard through package review, including model-disabled and dependency-failure states.

## 8. Verification, rollout, and three adversarial groups

- [ ] 8.1 Add a requirement-to-test/evidence matrix mapping every scenario ID to exactly one primary test and evidence artifact across API, database, Temporal, frontend, Ego/browser, security, and accessibility; update it before adding any new scenario.
- [ ] 8.1a Validate `scenario-register.json` contains exactly 106 unique IDs and that each spec scenario resolves to one gate, command, owner, route/viewport when applicable, and `scenario-evidence.schema.json` artifact path; validate the seven release gates (`MODEL-02`, `CRAWL-02`, `AUTH-02`, `DB-02`, `AUDIT-02`, `TEMP-02`, `PRIV-02`) with `release-gate-evidence.schema.json` and `<gate>-<commit>.json` paths.
- [ ] 8.1b Implement `retention-ci-contract.md`: fail-closed gate exit codes, artifact/secret rules, retention advisory lock, three-attempt alerting, legal-hold behavior, and no-synthetic-data assertion.
- [ ] 8.2 Run `openspec validate agent-first-console-experience --type change --strict --json`, `git diff --check`, and docs-plan consistency checks after every review fix.
- [ ] 8.3 Run `make verify`, `make verify-db`, `make verify-frontend`, `make verify-compose`, `make verify-temporal`, `make security`, and the relevant contract/trace/browser suites with model provider, OAuth, external writes, and auto-send disabled.
- [ ] 8.4 Run migration upgrade/compatibility/rollback-forward, concurrent idempotency, Temporal replay/cancel/fencing, no-store/cache, SSRF, output-XSS, prompt-injection, and Ego-ready/absent qualification evidence.
- [ ] 8.5 Add feature flags, rollout dashboards, budget/rate/denial alerts, stop/rollback runbook, retention job monitoring, and a no-data/read-only smoke path; keep `make verify-m1-full` unchanged when pilot data is absent.
- [ ] 8.6 Capture Ego evidence for login/bootstrap, dashboard action queue, profile preview, crawl-plan creation, run timeline, matching, resume review, interview preparation, package review, disabled model, stale/failure recovery, and accessibility keyboard paths.
- [ ] 8.7 Complete adversarial group 1 (security, product/accessibility, architecture/feasibility), fix all findings, and record pre/post scores.
- [ ] 8.8 Complete adversarial group 2 with fresh independent reviewers and the same three lenses, fix all findings, and record pre/post scores.
- [ ] 8.9 Complete adversarial group 3 with fresh independent reviewers and the same three lenses, fix all findings, and record pre/post scores; each final lens score MUST be at least 95/100.
- [ ] 8.10 Record remaining assumptions, unimplemented runtime evidence, final scores, and explicit “not pilot-ready” gates if any required external dependency remains unavailable.
