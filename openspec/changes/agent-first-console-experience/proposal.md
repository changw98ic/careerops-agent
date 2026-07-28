## Why

Ego evaluation of the current console found a usable review-only shell, but not a user-directed Agent experience: the dashboard exposes metrics instead of prioritized next actions, crawl plans have no clear creation path, Agent runs expose no trace or recovery context, and the model provider is disabled while several screens still present AI-shaped entry points. The product needs one explicit contract for proactive guidance, truthful model states, cross-module context, human approval, and observable execution before more Agent integrations are implemented.

This is an orchestration and experience delta over the existing `llm-agent-career-loop` and `ai-assisted-smart-forms` changes. It does not create a second crawl executor, model runtime, resume authority, matching authority, or application writer. The current implementation truth is part of the acceptance boundary: Agent runs still use the legacy `pending/unavailable/abstained/reviewed` values, Agent execution is currently request-bound, the Temporal worker does not yet register an Agent workflow, and the Compose topology does not yet ship an Ego runtime. The plan below therefore distinguishes existing compatibility from work that must be implemented before pilot release.

## What Changes

- Add a dashboard action queue that turns deterministic system state into a bounded set of prioritized next actions with reasons, prerequisites, evidence freshness, estimated impact, and safe CTAs.
- Add a shared Agent context contract so crawl, matching, resume review, interview preparation, and application preparation can continue from the same selected job, profile, resume, evidence, and last run.
- Add a page-managed Agent run center with lifecycle state, trace ID, stage timeline, input/output digests, model/provider state, retry/stop semantics, review decisions, and actionable failure recovery.
- Define a truthful AI interaction pattern: request → structured preview → evidence/unknowns/confidence → user edit → explicit apply/review; no silent writes, invented facts, external sends, or model claims while the provider is disabled.
- Make every gated/disabled capability explain why it is unavailable, what remains usable, how to enable it safely, and what the user should do next.
- Add a crawl-to-application journey contract covering crawl plan creation, run monitoring, job shortlist, matching, resume review, interview preparation, application package review, and manual approval boundaries.
- Unify Chinese-first copy, accessibility labels, keyboard/focus behavior, empty/error states, and review-only status across the console.
- Add contract, API, frontend, browser, trace, safety, and adversarial acceptance evidence, including an executable API/DB/Temporal/Ego/browser matrix; keep model provider, OAuth, external writes, auto-send, and model tool binding disabled by default.
- Define default-deny egress, SSRF, output rendering, object authorization, CSRF, idempotency, worker fencing, audit, retention, cache, and per-candidate/provider budget invariants as normative acceptance requirements.

## Capabilities

### New Capabilities

- `agent-action-orchestration`: Prioritized, bounded next actions and shared context across the console.
- `agent-run-observability`: Page/API-visible Agent run lifecycle, traces, recovery, and review evidence.
- `truthful-llm-interaction`: Structured AI preview, provenance, uncertainty, consent, fallback, and disabled-state behavior.
- `career-loop-experience`: A page-managed crawl-to-application journey with explicit human approval gates.

### Modified Capabilities

- `agent-workspace`: adds the action/context/run read models and review-only navigation contract without replacing the existing Agent routes.
- `llm-agent-runtime`: adds capability resolution, model egress consent, budget, retention, and output-safety requirements.
- `browser-backed-crawl`: adds the page-managed readiness/provenance boundary; the existing crawl executor and SSRF policy remain authoritative.
- `smart-form-intake`: reuses its short-lived preview/apply contract instead of introducing a second preview store.
- `resume-review-agent`, `job-matching-agent`, and `interview-preparation-agent`: adds cross-module context and authoritative destination rules.
- Existing profile, resume, evidence, application, auth, audit, and side-effect contracts remain authoritative and are not relaxed.

## Impact

- Frontend: `Dashboard.vue`, `AgentWorkbench.vue`, `SmartIntakePanel.vue`, profile/resume/evidence/crawl/application/mail/review views, shared shell/status/action components, localization and accessibility attributes.
- Backend/API: candidate-scoped action and context projections, Agent run read models, trace metadata, capability/readiness responses, review decisions, and bounded retry/stop endpoints.
- Temporal/worker: stage transitions and resumable run state for crawl and Agent work; Agent execution moves to a named workflow/activity contract, while no new external-side-effect authority is introduced.
- PostgreSQL: additive context/action/attempt/stage projections around existing `agent_runs` and `agent_run_reviews`; state migration is expand/contract and rollback-forward only.
- Browser runtime: an optional digest-pinned Ego/browser sidecar contract; absent readiness blocks browser work and leaves HTTP/deterministic paths available.
- Model gateway: provider capability reporting, structured outputs, evidence references, redaction, token/latency metrics, and disabled fallback.
- Verification: existing `make verify`, `make verify-db`, `make verify-frontend`, security checks, disposable PostgreSQL/Temporal evidence, Ego browser journeys, and three independent adversarial review rounds for both the spec and implementation plan.
