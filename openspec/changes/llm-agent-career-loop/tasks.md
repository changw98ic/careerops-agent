## 1. Contracts, storage, and capability gates

- [ ] 1.1 Freeze the browser-source, Agent-run, Agent-result, review-decision, correction, and stale-input schemas from the six capability specs.
- [ ] 1.2 Add additive PostgreSQL migrations for Agent runs/results, input identities, evidence references, review decisions, browser execution metadata, and bounded usage/outcome projections.
- [ ] 1.3 Implement candidate-scoped repositories with stable idempotency keys, ownership checks, versioned results, and stale-input detection.
- [ ] 1.4 Add capability resolver entries and readiness checks for browser crawl, model matching, resume review, and interview preparation with safe defaults.
- [ ] 1.5 Add contract tests for default-deny behavior, ownership, idempotency, state transitions, stale inputs, and rollback preservation.

## 2. Browser-backed crawl and durable execution

- [ ] 2.1 Define the source-executor port and adapt the existing guarded HTTP fetch path without changing its SSRF, robots, rate-limit, and response-limit contracts.
- [ ] 2.2 Implement the bounded Ego worker/sidecar bridge with URL preflight, navigation/scroll/response budgets, lifecycle cleanup, safe readiness reporting, and provenance output.
- [ ] 2.3 Normalize Ego page output through the existing source adapters into `CrawledPostingRecord` without persisting browser session material or raw unrestricted pages.
- [ ] 2.4 Extend the crawl execution service to select the declared executor mode, record safe browser failures/backoff, and never silently switch modes after a denial.
- [ ] 2.5 Wire `/crawl-plans/run-now` to start or reuse the idempotent `CrawlRunWorkflow` after creating the pending run.
- [ ] 2.6 Add a projection activity that runs deterministic inbox filtering/evidence matching after durable posting persistence and reports projection status separately from crawl status.
- [ ] 2.7 Add restart/retry integration coverage for HTTP and Ego harnesses, duplicate postings/versions, partial source failure, worker unavailability, and workflow replay.
- [ ] 2.8 Update crawl-plan/run APIs and pages to show executor mode, browser readiness, queued/running/terminal state, bounded errors, backoff, and links to inbox results.

## 3. Shared LLM Agent runtime

- [ ] 3.1 Define capability-specific request/response schemas and prompt versions for resume review, job matching, and interview preparation.
- [ ] 3.2 Implement a candidate-scoped Agent-run service that persists pending/running/succeeded/failed/unavailable/abstained/stale/reviewed states and input hashes.
- [ ] 3.3 Inject one configured `StructuredModelClient` into the inbox and Agent services; retain `DisabledModelAdapter` as the default path.
- [ ] 3.4 Implement bounded input builders, untrusted-content envelopes, evidence allowlists, credential/private-content rejection, and raw prompt/response suppression.
- [ ] 3.5 Enforce schema validation, one repair attempt, timeout/token budgets, 429 handling, explicit abstention, and deterministic fallback for every Agent capability.
- [ ] 3.6 Add aggregate token/latency/outcome metrics and trace correlation without high-cardinality user-content labels.
- [ ] 3.7 Add fake-provider contract tests and a redacted real-provider smoke command that proves structured output and `review_only=true` without storing credentials or content.

## 4. Job matching Agent

- [ ] 4.1 Integrate semantic ranking into `InboxProjectionService` after hard filters and evidence matching, preserving deterministic decisions when the model is unavailable.
- [ ] 4.2 Add single-job and bounded-batch matching commands with stable input-hash idempotency and asynchronous Agent-run status.
- [ ] 4.3 Persist requirement coverage, job/candidate evidence references, model/rules versions, confidence, abstention, and user corrections.
- [ ] 4.4 Add inbox/job-detail pages for matching status, advisory result, gaps, evidence, model metadata, correction, retry, and stale handling.
- [ ] 4.5 Add tests for hard-gate precedence, prompt injection, unsupported claims, disabled/unavailable/invalid model output, duplicate requests, and stale results.

## 5. Resume review Agent

- [ ] 5.1 Implement resume-review input assembly from one eligible resume version, one job version, active profile, and selected confirmed evidence.
- [ ] 5.2 Implement schema-validated review findings and presentation diffs with source evidence references and unsupported-claim handling.
- [ ] 5.3 Persist review results without mutating the base resume or creating trusted evidence; create a new version for every user acceptance or edit.
- [ ] 5.4 Add resume-review API/page views for start, progress, findings, evidence, diff, accept/reject/edit, retry, and stale status.
- [ ] 5.5 Add tests for ownership, unparseable/unconfirmed resumes, unsupported achievements, evidence binding, base immutability, input staleness, and model fallback.

## 6. Interview preparation Agent

- [ ] 6.1 Freeze the preparation-pack, question, STAR-prompt, uncertainty, and user-edit schemas with bounded size limits.
- [ ] 6.2 Implement job-specific preparation generation from selected job facts, confirmed resume evidence, profile context, and optional user context only.
- [ ] 6.3 Implement deterministic preparation fallback for model-disabled/unavailable mode using requirements and confirmed evidence.
- [ ] 6.4 Add API/page views for preparation run creation, pack display, question/STAR editing, accept/reject, versioning, retry, and stale handling.
- [ ] 6.5 Add tests for unsupported claims, injected job content, missing evidence, user-authored STAR corrections, ownership, idempotency, and provider failure.

## 7. Unified Agent workspace and operations

- [ ] 7.1 Add authenticated, candidate-scoped endpoints for crawl and Agent run lists/details, status polling, retries, cancellation where safe, review decisions, and corrections.
- [ ] 7.2 Add page navigation and empty/loading/error states for crawl, inbox matching, resume review, interview preparation, Agent run history, and review queues.
- [ ] 7.3 Render safe provenance summaries, input versions, model/schema/prompt versions, trace IDs, evidence links, bounded explanations, and next actions without raw transcripts or secrets.
- [ ] 7.4 Add CSRF/idempotency/state-transition tests for double-clicks, replayed decisions, cross-candidate IDs, stale approvals, and capability rollback.
- [ ] 7.5 Update runbooks and release evidence to distinguish code-level integration, offline harness evidence, real provider smoke, pilot quality, and independent review.

## 8. Ten independent adversarial verification passes

- [ ] 8.1 Pass 1 — attack Ego/browser content with prompt injection, fake tool instructions, recipient changes, secret-exfiltration requests, and hidden Unicode controls; prove zero tool/policy/send effects.
- [ ] 8.2 Pass 2 — attack crawl URLs with private IPs, metadata endpoints, redirect chains, DNS rebinding, unsafe schemes, oversized responses, and browser navigation escapes; prove denial before acceptance.
- [ ] 8.3 Pass 3 — attack model egress with API keys, cookies, raw resume bytes, attachments, unrelated mailbox content, and unselected evidence; prove redaction/denial and no secret logging.
- [ ] 8.4 Pass 4 — attack the model boundary with tool calls, policy assertions, forged evidence, guessed recipients, and model-only authorization; prove review-only and default-deny behavior.
- [ ] 8.5 Pass 5 — attack evidence binding with fabricated metrics, employers, skills, salary, visa, and STAR stories; prove unsupported output cannot become trusted facts or approved package content.
- [ ] 8.6 Pass 6 — attack idempotency with double-clicked starts, replayed Temporal activities, duplicate model requests, repeated reviews, and concurrent corrections; prove one logical run/result and no duplicate side effects.
- [ ] 8.7 Pass 7 — inject provider failures: disabled mode, timeout, 401/403, 404, 429, 5xx, malformed JSON, invalid schema, and repair failure; prove deterministic fallback and explicit abstention.
- [ ] 8.8 Pass 8 — inject browser/worker failures: missing Ego binary, expired session, crash mid-navigation, worker restart, partial source failure, and orphaned run; prove bounded recovery and truthful UI state.
- [ ] 8.9 Pass 9 — attack API/page boundaries with cross-candidate IDs, CSRF, stale inputs, forged review actions, raw prompt access, and unsafe HTML; prove ownership, state, and output escaping.
- [ ] 8.10 Pass 10 — attack observability and retention with high-cardinality content, prompt/response leakage, trace misuse, oversized metadata, and rollback; prove bounded metrics, retention compliance, and preserved history.

## 9. Qualification and release gate

- [ ] 9.1 Run `make verify`, `make verify-db`, security scanning, browser-harness tests, and all ten adversarial passes with recorded artifacts.
- [ ] 9.2 Run a real read-only pilot using user-configured Mimo/Ego capabilities without enabling OAuth, external writes, auto-send, or unattended application actions.
- [ ] 9.3 Obtain independent review of the pilot samples, corrections, privacy/legal handling, retention/custody, and Agent quality metrics.
- [ ] 9.4 Update release qualification only from current evidence; do not mark D0 or the full release gate complete when pilot or independent evidence is absent.
