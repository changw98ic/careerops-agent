## MODIFIED Requirements

### Requirement: Existing Agent workspace navigation remains candidate-scoped and review-only

The existing Agent workspace contract is modified only to consume the server-owned context/action/run read models defined below. Existing Agent tabs and routes SHALL remain candidate-scoped, review-only, and safe when the model provider is disabled; the new queue SHALL not create a second Agent execution or navigation authority.

#### Scenario: Existing workspace opens with the safe default

- **WHEN** a candidate opens the existing Agent workbench while the model provider is disabled
- **THEN** the existing route remains usable for deterministic/manual work, shows the truthful disabled state, and does not create a second run authority

## ADDED Requirements

### Requirement: Candidate-scoped action queue

The system SHALL expose a candidate-scoped action queue derived from authoritative CareerOps state and SHALL return no more than three active actions at a time. Each action MUST include a server-generated stable `action_key`, priority, deterministic rank, title, reason code, prerequisite status, source references, source freshness, target route or safe draft action, a source event key, and an `expires_at` or re-evaluation policy. The response MUST include `queue_version`, `generated_at`, `user_goal_scope`, and the in-app `notification_policy`.

#### Scenario: New candidate with no setup

- **WHEN** an authenticated candidate has no active profile version and no confirmed resume
- **THEN** the queue returns setup actions in dependency order, explains that profile creation precedes crawl/matching, and does not claim that jobs or model results already exist

#### Scenario: Queue has stale and review work

- **WHEN** a candidate has a stale crawl run, a pending evidence review, and a ready interview draft
- **THEN** the queue ranks the stale/review items using deterministic reason codes, includes timestamps, and limits the visible set to three actions without dropping the items from their source pages

#### Scenario: Model provider is disabled

- **WHEN** `model_provider=disabled`
- **THEN** deterministic setup, crawl, review, and navigation actions remain available while model-dependent actions are marked unavailable with a manual or deterministic alternative

#### Scenario: Cross-candidate access attempt

- **WHEN** a client supplies an action or candidate identifier that is not owned by the authenticated principal
- **THEN** the API denies the request without returning action content and records a bounded audit reason

#### Scenario: Queue response has a deterministic contract

- **WHEN** an authenticated candidate reads the queue
- **THEN** the response contains `queue_version`, `generated_at`, `source_event_key`, `deterministic_rank`, `user_goal_scope`, `notification_policy`, and a server-allowlisted route for every returned action; the client cannot supply any of these authority fields

### Requirement: Action lifecycle and safe navigation

The system SHALL support `proposed`, `accepted`, `snoozed`, `dismissed`, `completed`, `expired`, and `blocked` action states. Accepting an action MUST be idempotent and MUST NOT perform an external write; it may navigate to an existing route or create a review-only draft request through an existing contract.

#### Scenario: Accept a navigation action twice

- **WHEN** the same authenticated client accepts the same action twice with the same idempotency key
- **THEN** the second request returns the original outcome and does not create a duplicate run, draft, or audit mutation

#### Scenario: Snooze with a bounded duration

- **WHEN** a candidate snoozes an action
- **THEN** the API stores the selected bounded snooze time, excludes the action until re-evaluation, and shows when it will return

#### Scenario: Action prerequisite is not satisfied

- **WHEN** a candidate accepts an action whose prerequisite became invalid
- **THEN** the API returns a typed blocked response with the missing prerequisite, preserves the source data, and offers the next valid action

### Requirement: Shared career context

The system SHALL provide a server-validated context envelope containing the authenticated candidate, selected canonical job and version, active profile version, confirmed resume version, selected evidence IDs, immutable source digests, and freshness timestamps. The frontend SHALL store only a reference or non-sensitive selection state, not raw resume, mailbox, or model content in persistent browser storage.

#### Scenario: Continue from matching to interview preparation

- **WHEN** the candidate selects a job, profile, resume, and evidence set in matching and opens interview preparation
- **THEN** the next page loads the same validated context, displays the selected sources, and requires a fresh selection if any digest or ownership check is stale

#### Scenario: Source changes after context creation

- **WHEN** a selected resume or profile version changes after a context envelope is created
- **THEN** the envelope becomes stale, existing preview/run results are not silently reused, and the page explains which source must be reselected

#### Scenario: Context contains an untrusted user note

- **WHEN** a user adds free-form context to a model-assisted operation
- **THEN** the system labels it as untrusted context, isolates it from system instructions, and excludes it from trusted evidence claims

### Requirement: Proactive attention budget

The system SHALL apply a configurable per-candidate attention budget to proactive action surfaces. A budgeted action MUST be explainable, dismissible, snoozable, and suppressed after completion or explicit dismissal until its source state materially changes.

#### Scenario: Repeated page refresh

- **WHEN** the candidate refreshes the dashboard without any source-state change
- **THEN** the queue does not reorder or re-notify the same action solely because of the refresh

#### Scenario: Material source change

- **WHEN** a new crawl result, review request, or stale deadline changes the source state
- **THEN** the system may reintroduce a relevant action with a new reason/event reference and does not silently resurrect a manually dismissed action without explaining the change

### Requirement: Event-invalidated pull refresh and notification deduplication

The system SHALL create one bounded queue-invalidation event in the same transaction as each material authoritative source change. A unique `source_event_key` SHALL increment `queue_version` once, and the visible dashboard SHALL refresh on route focus or by event-invalidated polling no more often than once per 30 seconds. The first release SHALL use in-app delivery only; it SHALL NOT send email, browser, or operating-system notifications. Delivery deduplication SHALL be keyed by candidate, event, and surface across tabs and devices. This is pull refresh, not push notification.

#### Scenario: Source state changes while the dashboard is open

- **WHEN** a confirmed source change creates a new queue-invalidation event
- **THEN** the next permitted refresh returns a greater `queue_version`, the affected action appears within 30 seconds, and the response explains the update reason

#### Scenario: Two tabs receive the same event

- **WHEN** two authenticated tabs refresh after the same source event
- **THEN** both tabs see the same queue version and action identity, but the server records at most one in-app delivery for that candidate/event/surface

#### Scenario: User goal and quiet-hours policy suppress attention

- **WHEN** the candidate has configured a goal scope or in-app quiet-hours policy
- **THEN** ranking and presentation honor that policy without deleting the action from its source page, and the response exposes the policy state rather than silently hiding work

### Requirement: Safe action mutation contract

The action API SHALL use `GET /api/v1/agent-console/actions?cursor=&limit=3` for reads and these four exact mutation paths: `POST /api/v1/agent-console/actions/{action_key}/accept`, `POST /api/v1/agent-console/actions/{action_key}/snooze`, `POST /api/v1/agent-console/actions/{action_key}/dismiss`, and `POST /api/v1/agent-console/actions/{action_key}/complete`. Every mutation SHALL require server-resolved candidate ownership, CSRF, an `Idempotency-Key`, the current `queue_version`, and an atomic request-hash check. Reuse with a different request returns `409 IDEMPOTENCY_CONFLICT`; unknown action, policy, capability, or prerequisite state returns a non-success blocked response. All action mutations SHALL append a bounded audit event through the existing security-definer audit function and SHALL never perform an external write.

#### Scenario: Accepted action races with a source change

- **WHEN** an action is accepted using an old `queue_version` after its prerequisite changed
- **THEN** the API returns `409 ACTION_VERSION_CONFLICT` or `422 PREREQUISITE_BLOCKED`, does not create a run/draft, and provides the next valid route

#### Scenario: Idempotency key is reused with different content

- **WHEN** the same actor reuses an action mutation key with a different body or action
- **THEN** the API returns `409 IDEMPOTENCY_CONFLICT`, creates no second state change, and records the bounded security decision

#### Scenario: Mutation has a missing or foreign Origin

- **WHEN** a browser submits an action mutation without the configured console Origin or with a foreign Origin
- **THEN** the API rejects it before state change, requires CSRF for a valid Origin, returns a safe error, and appends the denial through the audit function

### Requirement: Accessible and truthful action surface

The action queue and all changed action controls SHALL use `lang="zh-CN"`, Chinese-first labels, programmatic names, keyboard-visible focus, `aria-describedby` for errors, and a live region for queue-version changes. State SHALL not be conveyed by color alone, and the queue SHALL remain usable at 200% zoom and with reduced motion enabled.

#### Scenario: Keyboard user accepts an action

- **WHEN** a keyboard-only user opens the dashboard and moves through the action cards
- **THEN** each card and control has a unique accessible name, focus is visible, the user can accept/snooze/dismiss without a pointer, and focus returns to the changed card after the response

#### Scenario: Queue request fails

- **WHEN** the action endpoint returns a typed dependency or authorization error
- **THEN** the live region announces the Chinese explanation and one safe next action, and the UI does not announce an action as accepted
