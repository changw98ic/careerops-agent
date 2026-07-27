## Context

The current repository has several useful but disconnected slices:

- HTTP/ATS adapters and `RealCrawlActivitySink` can normalize and persist source postings.
- `ego-browser` is used by standalone scripts for JavaScript-heavy pages and social sources, but is not an execution dependency of the Temporal crawl path.
- `StructuredModelClient` and the Anthropic-compatible MiMo adapter are implemented, while the safe default remains `disabled`.
- Deterministic resume parsing/evidence extraction and an advisory `LLMJobMatcher` exist, but the page-level inbox service does not receive the model client.
- The LangGraph review graph is compiled with a demo crawler that returns no jobs, and the run-now API creates a pending run without starting its workflow.
- There is no interview-preparation Agent or page/API contract for one.

The change must close these seams without weakening the repository's default-deny, evidence-first, review-only, no-tools, no-auto-send, and append-only audit invariants. Temporal remains the durable execution boundary; LangGraph remains the review/state-flow boundary; PostgreSQL remains the business source of truth.

## Goals / Non-Goals

**Goals:**

- Make a page-triggered crawl run execute through Temporal, use HTTP or Ego according to source capability, persist normalized postings with provenance, and project them into the inbox.
- Provide one shared model-runtime contract for resume review, job matching, and interview preparation, including minimized egress, schema validation, bounded retries, fallback, trace correlation, and aggregate usage metrics.
- Make each Agent result a durable, reviewable proposal tied to input hashes, model/schema versions, confirmed evidence, and the authenticated candidate.
- Expose crawl, Agent, review, correction, retry, and failure states through API and server-rendered page surfaces.
- Keep model-disabled mode useful: deterministic parsing, hard filtering, evidence matching, package validation, and manual interview preparation remain available.
- Produce contract, integration, adversarial, and provider-qualification evidence that distinguishes instrumentation from real pilot quality.

**Non-Goals:**

- Enabling Gmail OAuth, external writes, automatic sending, unattended applications, third-party form automation, or social-account actions.
- Giving Ego or any model credentials, tools, policy authority, recipient authority, or direct database mutation access.
- Replacing deterministic hard filters, evidence confirmation, or package approval with model confidence.
- Building a general multi-tenant Agent platform, arbitrary prompt editor, autonomous browser agent, or unrestricted memory store.
- Claiming model quality or release qualification without a real pilot, independent review, privacy/legal review, and custody evidence.

## Decisions

### 1. One crawl execution port, two bounded source executors

Introduce a source-execution port used by the existing crawl execution service. The HTTP executor delegates to the existing SSRF/robots/rate-limit guarded fetcher. The browser executor delegates to an explicit Ego bridge running in a worker or sidecar and returns the same bounded response/provenance shape. A source declares its executor mode; it cannot silently fall back from a denied or unavailable browser policy to an unapproved HTTP scrape.

Alternative considered: invoke `ego-browser` from arbitrary application routes or workflow code. Rejected because it makes browser lifecycle, credentials, timeout, and audit boundaries implicit and breaks Temporal determinism.

### 2. API creates the run and starts an idempotent Temporal workflow

The run-now command keeps the existing stable run identity, then starts `CrawlRunWorkflow` with the server-resolved owner and run ID. A repeated command returns the existing workflow/run state. The API does not wait for crawl completion; the page polls or receives bounded status updates. A worker that is unavailable leaves a visible queued/dependency state rather than pretending the run executed.

Alternative considered: execute a crawl inside the request handler. Rejected because browser and network work can outlive request timeouts and would not have durable retry/reconciliation semantics.

### 3. Persist first, project second, enrich on explicit Agent jobs

The crawl activity normalizes and idempotently persists postings and versions. A projection activity then updates canonical jobs/inbox decisions. Deterministic hard filters and evidence matching run before semantic ranking. LLM matching is an explicit Agent job for eligible/recommended items, either requested by the user or triggered by a configured bounded batch policy; it never bypasses the hard filter.

Resume review and interview preparation are user-triggered Agent jobs because they consume selected private evidence and can be expensive. Each job captures immutable input identities and can become stale when the job, profile, resume, or evidence version changes.

Alternative considered: call the model inline for every crawled posting. Rejected because it increases cost, leaks more content by default, complicates retries, and makes a crawl failure indistinguishable from an enrichment failure.

### 4. Shared model runtime with capability-specific schemas

All three Agents use `StructuredModelClient`, capability-specific request builders, and JSON Schemas. The runtime records only aggregate token counts, latency, outcome, model/schema/prompt versions, and safe trace IDs. Raw prompt/response content is not persisted or logged by default. The client has no `tools` field and every result is `review_only`.

The provider remains `disabled` unless the user supplies the configured endpoint, key, and model and explicitly releases the model capability. Invalid, unavailable, or over-budget calls return an explicit unavailable/abstained result and preserve deterministic output.

Alternative considered: let each Agent call a provider SDK directly. Rejected because it duplicates egress/security behavior and makes provider usage and redaction un-auditable.

### 5. Evidence references are the authority boundary

Resume review may propose presentation edits, but every positive claim must reference confirmed candidate evidence. Job matching may propose a score or recommendation, but it cannot create evidence or change application state. Interview preparation may synthesize questions and answer prompts only from the selected job facts and confirmed evidence; unsupported claims are marked unknown or omitted. User corrections create new versions and remain distinguishable from model output.

### 6. Page management is a state machine, not a chat surface

The UI exposes list/detail/review actions for crawl runs and Agent runs. It shows queued/running/succeeded/failed/abstained/stale/reviewed states, input provenance, evidence links, bounded explanations, and safe retry actions. It does not expose arbitrary system prompts, credentials, raw model transcripts, or a button that turns a suggestion into an external side effect.

### 7. Capability rollout is additive and reversible

New database tables and API routes are additive. Capabilities are released independently: browser crawl, model matching, resume review, and interview preparation. Disabling one capability preserves crawl history, job records, resume versions, evidence, Agent metadata, review decisions, and manual workflows. Rollback is configuration/capability based and does not delete history.

## Risks / Trade-offs

- **[Risk] Ego is unavailable, unauthenticated, or cannot run in the worker environment.** → Detect it before a browser run, return `dependency_unavailable`, retain HTTP-only sources, and never silently scrape through a different path.
- **[Risk] Browser output contains prompt injection or sensitive content.** → Treat all browser output as untrusted, apply bounded extraction/redaction before model egress, allow no tools, and retain only normalized fields/provenance according to retention policy.
- **[Risk] Model quality is poor or claims are fabricated.** → Require schemas, evidence references, review-only output, abstention, user confirmation, and independent pilot evaluation before qualification.
- **[Risk] A long crawl creates many model calls and cost spikes.** → Apply per-run and per-Agent budgets, deduplicate by input hash, make enrichment explicit/bounded, and expose aggregate token metrics.
- **[Risk] Source/job/resume changes make Agent results stale.** → Store all input identities/hashes and block approval/use of stale results until regenerated or explicitly reviewed.
- **[Risk] Temporal retries duplicate crawl or Agent effects.** → Use stable workflow/run/Agent idempotency keys and keep model calls and writes in activities with durable result records.
- **[Risk] UI reports queued as completed.** → Use distinct state contracts and only show success after durable terminal evidence is stored.

## Migration Plan

1. Add additive schemas for browser source configuration, Agent runs/results, review decisions, input identities, and bounded usage/outcome projections.
2. Add the browser executor behind a capability resolver and a worker/sidecar readiness check; keep all existing HTTP sources working unchanged.
3. Wire run-now to start the existing Temporal workflow and add the projection activity; verify idempotency and restart behavior before enabling UI controls.
4. Inject one shared model client into the inbox and Agent services; implement job matching first, then resume review and interview preparation behind independent gates.
5. Add API/page views and review actions; run contract, database, browser-harness, model-fake, and adversarial suites.
6. Perform a real-provider smoke/qualification pilot only with user-provided configuration and no external-write flags. Record evidence separately from code-level tests.

Rollback disables the relevant capability and stops new workflows/Agent jobs. Existing runs are reconciled to a safe terminal/dependency state; persisted history and review decisions remain readable. No rollback step deletes source content, resume versions, evidence, or audit records.

## Open Questions

- Which exact Ego deployment contract is authoritative for the worker: local CLI, long-lived sidecar, or a dedicated browser-worker service?
- Which source classes may use Ego in the first release, and what login/session or retention rules apply to each?
- Should automatic job matching run after every successful projection or only after an explicit page action in the first pilot?
- What interview-preparation output schema and user-owned STAR library format should be frozen before implementation?
- Which real-provider quality thresholds and independent reviewer artifacts will authorize model capability release?
