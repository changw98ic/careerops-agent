## MODIFIED Requirements

### Requirement: Existing crawl, matching, resume, and package contracts remain authoritative

Existing crawl-plan/run, canonical-job/inbox, deterministic matching, resume-version, and application-package contracts are modified only by the route/context/readiness adapters in this change. The new Agent experience SHALL not replace their persistence or side-effect boundaries, and every apply/export path SHALL call the existing validated contract.

#### Scenario: Existing authority survives the new route

- **WHEN** a candidate uses a new context-preserving CTA to reach crawl, matching, resume, or package review
- **THEN** the target calls the existing authoritative contract, preserves its validation/ownership/side-effect policy, and does not create a parallel persistence record

## ADDED Requirements

### Requirement: Crawl plan creation and readiness

The console SHALL provide a clear creation path for a crawl plan, including source registration, policy status, schedule, profile prerequisites, preview of scope, and explicit activation. A plan MUST remain a draft until the user confirms its sources and schedule.

#### Scenario: No plan exists

- **WHEN** an authenticated candidate opens the crawl-plan page with no plan
- **THEN** the page displays a primary create-plan action, explains required profile/source prerequisites, and does not imply that a plan is active

#### Scenario: Source is blocked by policy

- **WHEN** a proposed source is not allowlisted or violates a crawl policy
- **THEN** the source is visibly blocked with the reason and cannot be activated, while allowed sources remain reviewable

#### Scenario: Activate a valid plan

- **WHEN** the candidate confirms a valid draft plan
- **THEN** the system creates an immutable plan version, records the actor and policy decision, and exposes the next run/monitor action without starting an external side effect outside the approved crawl contract

#### Scenario: First-run profile is incomplete

- **WHEN** an authenticated candidate has no profile, confirmed resume, or evidence
- **THEN** the dashboard and crawl-plan empty state present one ordered first-value path `profile → resume → evidence → crawl plan`, with one primary CTA and no fabricated metrics

### Requirement: Crawl run to inbox projection

The system SHALL connect an activated crawl plan to a durable run and project normalized, provenance-bearing job postings into the existing inbox/job contracts. Each run MUST expose source counts, deduplication/normalization counts, policy decisions, errors, next retry/run time, and trace correlation.

#### Scenario: HTTP source succeeds

- **WHEN** an allowed HTTP/ATS source returns valid postings
- **THEN** the run records provenance and counts, persists canonical job versions through the existing contract, and makes new candidates visible in the inbox with freshness metadata

#### Scenario: JavaScript source requires browser execution

- **WHEN** a permitted source is explicitly marked as browser-backed and HTTP extraction is insufficient
- **THEN** the run uses the bounded Ego/browser activity with source-specific policy and timeout, records browser execution evidence, and falls back or fails closed if the browser dependency is unavailable

#### Scenario: Crawl run is interrupted

- **WHEN** a worker or browser activity is interrupted
- **THEN** the run is resumable or retryable with bounded idempotency and does not duplicate canonical postings or silently mark the run successful

#### Scenario: Source redirects to a private address

- **WHEN** an HTTP or browser source redirects, resolves, or reconnects to loopback, private, link-local, metadata, multicast, an unallowlisted host, or an unsafe scheme
- **THEN** the fetch is blocked before content processing, the redirect is revalidated at every hop, no cookies/authorization/session credentials are sent, and the run records a typed policy denial

#### Scenario: Browser dependency is absent

- **WHEN** the permitted source requires the Ego sidecar but its immutable image, health check, or policy bundle is not ready
- **THEN** the run is `dependency_not_ready`/blocked, the UI gives the HTTP/manual alternative where policy permits, and it never silently invokes a different browser or unauthorized HTTP source

#### Scenario: Browser page requests a private subresource

- **WHEN** an allowed page's JavaScript, iframe, image, WebSocket, `fetch`, or XHR requests a private/metadata address, unallowlisted host, unsafe scheme, or non-default port
- **THEN** the forced sidecar egress boundary blocks the subrequest before connect, does not reuse cookies/service workers/tabs, records a bounded policy denial, and leaves the run non-successful

#### Scenario: Browser task space is reused

- **WHEN** a crawl request attempts to reuse an existing Ego tab, cookie jar, local storage, or service worker
- **THEN** readiness denies the browser capability, a fresh isolated task space is required, and no previous candidate/source session data is exposed

### Requirement: Deterministic-first matching with optional explanation

The system SHALL run deterministic hard filters and evidence checks before any optional model explanation/ranking. Model-assisted matching MUST be clearly labeled, cite allowed evidence, expose unknowns/confidence, and remain review-only.

#### Scenario: Model disabled

- **WHEN** the model provider is disabled
- **THEN** deterministic matching still returns its rule-level result and reasons, while model ranking/explanation is marked unavailable with no fabricated score

#### Scenario: Match has an evidence gap

- **WHEN** a job requirement cannot be supported by confirmed candidate evidence
- **THEN** the result marks the requirement as missing/unsupported and routes the user to evidence/profile review instead of inventing a match

#### Scenario: Model explanation is stored

- **WHEN** optional model matching completes
- **THEN** the deterministic `filter_decisions` result remains authoritative, the explanation is attached to a review-only run with source/version digests, and no model score changes job eligibility or creates evidence

### Requirement: Resume review and interview preparation continuity

The system SHALL allow a candidate to continue from a selected job and confirmed resume into resume review and interview preparation using the shared context. Both operations MUST produce review-only drafts, preserve source references and unknowns, and never mutate the base resume or contact an external party.

#### Scenario: Start resume review

- **WHEN** the candidate has a target job, job version, confirmed resume, and valid context
- **THEN** the system creates a traceable review run with structured suggestions/diffs, evidence links, and an explicit human review state

#### Scenario: Start interview preparation

- **WHEN** the candidate continues with the same context and adds untrusted practice goals
- **THEN** the system generates a bounded preparation draft or deterministic fallback, labels user context separately from verified facts, and keeps the draft editable

#### Scenario: Missing context

- **WHEN** one required source is absent or stale
- **THEN** the page identifies the missing source and offers a route to fix it before enabling generation

#### Scenario: Resume review has an authoritative destination

- **WHEN** a candidate accepts a resume suggestion
- **THEN** the system records the field-level review decision and only creates a new validated resume version through the existing resume-version contract; the base version and evidence remain unchanged until that contract succeeds

#### Scenario: Interview draft includes untrusted practice goals

- **WHEN** a candidate asks for preparation using free-form practice goals
- **THEN** verified facts, job facts, and user goals are displayed as separate sources, unsupported claims remain unknown, and the draft cannot be treated as evidence

### Requirement: Application package review boundary

The system SHALL allow a candidate to assemble a job-specific application package from approved resume/evidence inputs and model-reviewed drafts, but SHALL require explicit approval before any package is considered send-ready. This change MUST NOT enable automatic submission, email send, or external write.

#### Scenario: Package contains an unevidenced claim

- **WHEN** a draft claim lacks evidence or conflicts with an authoritative source
- **THEN** approval is blocked with a field-level reason and the user can edit, remove, or attach valid evidence

#### Scenario: External writes are disabled

- **WHEN** the candidate attempts to send or submit from the review-only console
- **THEN** the action is denied with a clear policy state and the approved draft remains available for manual export/copy through the existing safe contract

#### Scenario: Package approval and export are replayed

- **WHEN** package approve or manual export is submitted twice with the same idempotency key
- **THEN** one authoritative package receipt is returned, no duplicate version/export event is created, and no external send occurs

#### Scenario: Package export contains unsafe content

- **WHEN** an approved draft contains an unsupported claim, unsafe URL, raw markup, or content from another candidate
- **THEN** field-level validation blocks approval/export, identifies the safe correction, and records a bounded audit decision

### Requirement: End-to-end progress and recovery

The console SHALL show the current loop stage, completed stages, blocked prerequisites, pending reviews, and the next safe action across dashboard, inbox, Agent workspace, run history, and application workspace.

#### Scenario: Candidate completes a stage

- **WHEN** a profile, crawl, match, review, or preparation stage reaches its authoritative success state
- **THEN** the next dependent stage becomes discoverable through the action queue and context-preserving CTA without requiring the candidate to reconstruct the workflow

#### Scenario: Stage fails

- **WHEN** a stage fails
- **THEN** the loop shows a typed reason, retryability, trace, affected inputs, and a recovery CTA; it does not advance the candidate as if the stage completed

### Requirement: First-run authentication, bootstrap, and accessible localization

The console SHALL provide a Chinese-first, accessible first-run path for login and bootstrap. An already-provisioned account or closed bootstrap SHALL be described as setup complete with a login CTA, not as an invalid/expired token. Login, bootstrap, dashboard, crawl-plan, Agent workbench, run detail, and application review states SHALL satisfy the shared labels, focus, live-region, zoom, contrast, reduced-motion, and non-color status contract.

#### Scenario: Bootstrap is already closed

- **WHEN** an unauthenticated user opens bootstrap after an account already exists or bootstrap is closed
- **THEN** the page explains that initial setup is complete, provides the login route, uses Chinese-first copy, and does not expose token validity details or account existence beyond the safe message

#### Scenario: Keyboard user completes first value

- **WHEN** a keyboard-only user starts at login and proceeds through the no-data dashboard
- **THEN** every control has a programmatic label, errors are associated with fields, focus moves to the next required action, and the user can reach profile/resume/evidence/crawl-plan without a pointer

### Requirement: Existing mail and outbound boundaries remain explicit

The career-loop projection SHALL exclude mail-follow-up, reply-queue, Gmail OAuth, external writes, auto-send, and unattended application actions. Existing mail/reply pages remain review-only and are not presented as completed Agent capabilities in the action queue. The new frontend action dispatcher and backend route allowlist SHALL reject `sendApplicationEmail`, `sendReplyDraft`, `confirmSystemSend`, `syncMailNow`, and `confirmExternalSubmission` before navigation/service dispatch. Unknown side-effect policy states SHALL block the action.

#### Scenario: User asks the loop to send

- **WHEN** a candidate attempts to send a package, email, or reply from any new action/context surface
- **THEN** the API denies the request with the external-write policy state, records the decision, and leaves the reviewable draft intact

#### Scenario: Action projection sees an outbound surface

- **WHEN** a source page exposes `sendApplicationEmail`, `sendReplyDraft`, `confirmSystemSend`, `syncMailNow`, or `confirmExternalSubmission`
- **THEN** the new action queue excludes those action keys, the new context router cannot invoke them, and the existing separately gated manual surface remains unchanged

### Requirement: Executable route journey and accessible page contract

The console SHALL implement the ordered first-value route journey `/login → /dashboard → /profile → /resumes → /evidence → /crawl-plans → /crawl-runs/:id → /inbox/:id → /ai-workbench?tab=matching → /ai-workbench?tab=resume → /ai-workbench?tab=interview → /applications/:id`. Each transition SHALL carry only a server-issued `context_id` reference, use a server-allowlisted route and CTA, and expose the current stage/reason/next action. Changed pages SHALL use `lang="zh-CN"`, programmatic labels, `aria-describedby` errors, `aria-live="polite"` for async state, visible focus, keyboard operation, focus restore for dialogs, non-color state, 200% zoom, contrast, and reduced-motion behavior.

#### Scenario: Context-preserving route transition

- **WHEN** a candidate accepts the dashboard action to continue from one stage to the next
- **THEN** the target route receives the same server-validated context reference, displays the selected source/version, and rejects/requires refresh if the digest is stale

#### Scenario: Keyboard-only first-value path

- **WHEN** a keyboard-only candidate moves from login through the empty dashboard and profile setup
- **THEN** each primary CTA has a stable accessible name, Tab/Enter/Space completes the path, the first invalid field receives focus, and async completion is announced without a pointer

#### Scenario: Disabled model in the continuous journey

- **WHEN** the candidate reaches matching, resume review, or interview preparation with the provider disabled
- **THEN** deterministic/manual actions remain enabled, the page says “当前未启用大模型”, no model result/score is implied, and exactly one safe next action is presented

#### Scenario: Bootstrap is closed or already provisioned

- **WHEN** the user opens `/bootstrap` after setup is complete or bootstrap is closed
- **THEN** the page displays Chinese-first setup-complete guidance and a labelled `返回登录` CTA, does not expose token validity/account existence details, and routes safely to `/login`

#### Scenario: Agent tab query and context are restored

- **WHEN** a candidate opens `/ai-workbench?tab=resume&context_id=00000000-0000-0000-0000-000000000001` or `/ai-workbench?tab=interview&context_id=00000000-0000-0000-0000-000000000001`
- **THEN** the router selects only the allowlisted tab, loads the server-validated context, and falls back to `matching` with a safe error for an unknown tab or stale context

#### Scenario: Crawl run reaches its detail view

- **WHEN** an activated crawl plan returns a durable run ID
- **THEN** the UI navigates to `/crawl-runs/{id}`, displays the API-backed state/trace/provenance, and exposes a labelled recovery action; a success toast alone is not considered completion
