## ADDED Requirements

### Requirement: The page manages crawl and Agent run lifecycles
The authenticated workspace SHALL provide list/detail views and API endpoints for crawl runs and Agent runs. It SHALL show queued, running, succeeded, failed, unavailable, abstained, stale, cancelled, and reviewed states with safe timestamps, bounded error categories, input identity summaries, and next actions.

#### Scenario: User opens an Agent run
- **WHEN** the user views a resume-review, matching, or interview-preparation run they own
- **THEN** the page shows its state, input versions/hashes, model/schema/prompt versions, evidence links, bounded outcome, and review actions without showing credentials or raw transcripts

#### Scenario: User views another candidate's run
- **WHEN** a request references a run owned by another candidate
- **THEN** the API returns the same not-found/denied behavior as other candidate-scoped resources and reveals no run metadata

### Requirement: Page actions are idempotent and review-gated
Start, retry, cancel, accept, reject, edit, and correct actions SHALL use stable idempotency keys, authenticated ownership, CSRF protection where applicable, and server-side state validation. A user action SHALL update a proposal/draft/review record and SHALL NOT directly invoke an external provider.

#### Scenario: User double-clicks start
- **WHEN** the same start command is submitted more than once for unchanged inputs
- **THEN** the page receives one run identity and no duplicate workflow, model result, or usage record is created

#### Scenario: User approves a generated suggestion
- **WHEN** the user accepts a resume, match, or preparation suggestion
- **THEN** the system records the review decision and creates only the permitted internal version/projection; no Gmail, browser, form, or other external write occurs

### Requirement: The workspace exposes safe operational evidence
The page SHALL show crawl and Agent duration, source/model outcomes, retry/backoff, token aggregate summaries, trace correlation, correction counts, and stale/unavailable reasons using bounded labels. It MUST NOT render raw prompts, raw model responses, keys, browser session data, or unrestricted external content by default.

#### Scenario: User investigates a failed model run
- **WHEN** an Agent run fails, abstains, or is rate-limited
- **THEN** the page shows a safe failure category, trace ID, latency/budget information, and a retry or fallback action without exposing provider payloads

#### Scenario: Operator reads metrics
- **WHEN** an operator scrapes `/metrics`
- **THEN** metrics include bounded run/outcome/usage aggregates and never use user content, prompt text, job titles, email addresses, or trace IDs as high-cardinality metric labels

### Requirement: Capability rollback preserves user work
The workspace SHALL honor independent capability states for browser crawl, model matching, resume review, and interview preparation. Disabling a capability SHALL block new work, preserve prior results/reviews/history, and leave deterministic/manual workflows available where defined.

#### Scenario: Model capability is disabled after results exist
- **WHEN** an operator or user disables a model capability
- **THEN** new Agent runs are denied with a safe reason, prior results remain readable and marked historical, and resume parsing/inbox hard filters/manual preparation continue to work

#### Scenario: Browser capability is disabled after a crawl
- **WHEN** Ego is unavailable or the browser capability is rolled back
- **THEN** new browser runs stop before navigation, prior postings and provenance remain readable, and permitted HTTP sources can continue independently
