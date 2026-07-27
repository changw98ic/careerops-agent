## Why

The repository now has separate building blocks for HTTP crawling, Ego browser scripts, a structured LLM gateway, deterministic resume analysis, and advisory job matching, but they are not one user-manageable product flow. The missing product capability is a page-managed loop that turns a crawl plan into reviewable job matches, resume feedback, and interview preparation while preserving evidence, traceability, and human control.

This change makes that gap explicit and gives the missing LLM/browser integration an independently verifiable delivery contract instead of treating isolated adapters or CLI smoke tests as a completed Agent experience.

## What Changes

- Add a browser-backed crawl activity that can use Ego for JavaScript-heavy career sites and permitted social sources, normalizes results with the existing posting contract, and persists provenance and run state.
- Connect page-triggered crawl runs to Temporal execution and the inbox projection; keep HTTP/ATS adapters as the deterministic path and use Ego only where the source contract permits it.
- Add a shared review-only Agent runtime for model requests, structured schemas, bounded inputs, capability checks, fallback behavior, token/latency traces, and evidence-bound outputs.
- Add a resume review Agent that analyzes a selected resume version against confirmed evidence and a target job, producing suggestions/diffs without mutating the base resume or inventing trusted claims.
- Make the job matching Agent available in the page workflow after deterministic hard filters, with model/rules provenance, evidence links, confidence, abstention, and user correction.
- Add an interview preparation Agent that produces a bounded, job-specific preparation pack, questions, and STAR prompts from the job, confirmed resume evidence, and user-selected context; outputs remain drafts requiring review.
- Add page/API management for Agent runs, provider status, prompts/results metadata, review decisions, retries, stale inputs, and trace-correlated metrics.
- Add adversarial, contract, integration, and real-provider qualification evidence. Do not enable external writes, auto-send, OAuth, model tools, or unattended application actions through this change.

## Capabilities

### New Capabilities

- `browser-backed-crawl`: Ego/HTTP source execution, normalization, Temporal triggering, provenance, idempotency, and inbox projection.
- `llm-agent-runtime`: Shared model capability resolution, minimized egress, schema validation, review-only semantics, fallback, usage metrics, and trace correlation.
- `resume-review-agent`: Evidence-bound LLM resume review and job-specific presentation suggestions.
- `job-matching-agent`: Deterministic-first and LLM-assisted job matching exposed through the product workflow.
- `interview-preparation-agent`: Job-specific preparation packs, questions, and STAR prompts with review and evidence boundaries.
- `agent-workspace`: Page/API management of crawl and Agent runs, results, review actions, failures, and operational evidence.

### Modified Capabilities

None. Existing application, crawl, inbox, profile, and safety contracts remain authoritative; this change adds the missing integrated capabilities on top of them.

## Impact

- Affected backend areas: `infrastructure/temporal`, crawl sinks/adapters, `application` Agent services, `model_gateway`, inbox projection, observability, and API dependencies.
- Affected page areas: crawl-plan/run views, inbox match details, resume review, interview preparation, Agent run/review states, and safe error/empty states.
- New persisted metadata may include Agent run identity, input hashes, schema/prompt/model versions, evidence references, review decisions, and bounded outcome summaries; raw prompts, raw model responses, credentials, and browser session material remain excluded by default.
- The worker deployment must provide an explicit, bounded Ego/browser runtime or sidecar. Environments without it must remain usable through HTTP-only sources and deterministic/model-disabled fallbacks.
