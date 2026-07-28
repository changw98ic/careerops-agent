## MODIFIED Requirements

### Requirement: Existing Agent run persistence remains authoritative

The existing `agent_runs` and `agent_run_reviews` persistence contract is modified additively: the new canonical lifecycle/read-model fields, attempt/stage projections, and redacted endpoints SHALL adapt the existing rows and legacy wire state. No second Agent run/result authority may be introduced.

#### Scenario: Legacy run remains readable during rollout

- **WHEN** an old application reads a run after the additive migration
- **THEN** the legacy `state`/result response remains compatible while the new application can read canonical lifecycle fields, and no second run/result row is created

## ADDED Requirements

### Requirement: Durable Agent run state machine

The system SHALL persist a logical Agent run with a stable run ID, trace ID, candidate ownership, operation type, context digest, attempt number, canonical `execution_state`, `capability_state`, `review_state`, timestamps, and bounded error/review metadata. The canonical execution state SHALL be one of `queued`, `running`, `waiting_review`, `succeeded`, `failed`, `cancel_requested`, `cancelled`, `stale`, or `blocked`. During migration the response SHALL also expose the legacy `state` value and a deterministic mapping; unknown state values SHALL be blocked rather than treated as success.

#### Scenario: Run survives a page reload

- **WHEN** a candidate starts a review-only Agent operation and reloads the page while it is running
- **THEN** the run center reloads the same run ID and current state from the API rather than showing a new local spinner

#### Scenario: Worker loses a lease

- **WHEN** a worker lease expires before a run reaches a terminal state
- **THEN** the run becomes retryable or blocked according to the error policy, records the bounded reason and trace ID, and does not remain indefinitely in `running`

#### Scenario: Run input becomes stale

- **WHEN** a source digest no longer matches the run context
- **THEN** the run transitions to `stale`, explains the changed source, and requires a new context/preview before retry

#### Scenario: Legacy run row is read after migration

- **WHEN** a row contains legacy `pending`, `unavailable`, `abstained`, or `reviewed`
- **THEN** the API maps it to canonical fields as specified by the design, preserves `legacy_state`, and never exposes a false `succeeded` state

### Requirement: Trace-correlated stage timeline

The system SHALL expose an append-only, redacted stage timeline correlated by trace ID across API, Temporal, model gateway, and frontend. Each event MUST include a server-generated `stage_event_id`, monotonic `sequence`, `stage`, `status`, `attempt`, `schema_version`, `cause_code`, `terminal`, occurred time, duration when complete, retryability, source/run reference, a deduplication key, and a bounded message; prompts, raw model output, credentials, browser session material, and raw mailbox/resume bytes MUST NOT be persisted by default.

#### Scenario: Model provider disabled

- **WHEN** a run reaches a model-dependent stage while the provider is disabled
- **THEN** the timeline records `disabled_by_policy` or `blocked`, identifies the capability state, and presents the deterministic/manual fallback without claiming a model invocation

#### Scenario: Trace lookup

- **WHEN** a user opens a run detail page with a valid owned run ID
- **THEN** the page displays a copyable trace ID, stage timeline, input/source digests, model/provider state, and review decision metadata

#### Scenario: Unowned trace lookup

- **WHEN** a client requests a trace for a run outside the authenticated candidate scope
- **THEN** the API returns a non-enumerating authorization error and does not reveal whether the run exists

### Requirement: Retry, stop, and recovery semantics

The system SHALL expose retry and stop controls only when the state machine permits them. Retry MUST use a new attempt with idempotency and preserve lineage; stop MUST be cooperative, bounded, and visible; terminal historical events MUST remain append-only.

#### Scenario: Retry a transient failure

- **WHEN** a run fails with a retryable dependency error
- **THEN** the user can retry, a new attempt is linked to the original run, and the UI shows the next retry state and trace without duplicating authoritative records

#### Scenario: Stop a running run

- **WHEN** a candidate requests stop for a run in `running`
- **THEN** the run enters `cancel_requested`, workers stop at a safe checkpoint, and the UI eventually shows `cancelled` or a typed failure to stop

#### Scenario: Retry a stale run

- **WHEN** a run is `stale`
- **THEN** retry is disabled until the candidate refreshes the context, and the page offers the exact source refresh action

#### Scenario: Cancellation is not acknowledged

- **WHEN** a cancellation request is not acknowledged within 30 seconds
- **THEN** the run becomes `failed` with `CANCEL_TIMEOUT`, exposes the trace and recovery action, and never remains indefinitely in `cancel_requested`

#### Scenario: Old worker submits after fencing

- **WHEN** a worker with an expired lease epoch submits a stage completion
- **THEN** the repository rejects the write, preserves the newer stage event, and appends a bounded audit event without changing the run to success

### Requirement: Operational read model

The system SHALL provide candidate-scoped list and detail endpoints for runs, stages, review decisions, capability status, and bounded usage/latency metrics. Responses MUST use `Cache-Control: no-store` for sensitive run details and MUST distinguish empty, blocked, unavailable, failed, and loading states.

#### Scenario: No runs exist

- **WHEN** a candidate has no Agent runs
- **THEN** the run center shows an empty state with a safe start action and does not present an error or fabricate history

#### Scenario: Dependency unavailable

- **WHEN** Temporal, Redis, storage, or a required repository is not ready
- **THEN** the API returns a typed dependency state and the page shows the dependency and a retry/manual alternative without failing open

### Requirement: Temporal, endpoint, and database compatibility contract

The system SHALL execute Agent work through the named `AgentRunWorkflow` on the `careerops-agent` task queue and SHALL expose run list/detail/stage/retry/stop/review operations with candidate-scoped authorization, CSRF on mutations, `Idempotency-Key` on mutations, cursor pagination, and `Cache-Control: no-store` for sensitive data. The workflow SHALL use activity boundaries for model/network/database work, stable workflow IDs, attempt lineage, heartbeat, cancellation, and lease fencing. Existing `agent_runs` and `agent_run_reviews` remain the persistence authority; additive migration `0030_agent_console_orchestration` follows repository head `0029_add_proposal_value_digests` and adds canonical lifecycle columns and attempt/stage/context projections with composite candidate foreign keys, unique constraints, API-role grants, and retention handling.

#### Scenario: Duplicate retry request

- **WHEN** the same candidate retries a run twice with the same idempotency key
- **THEN** the API returns one attempt receipt, creates one new attempt at most, and preserves the original run/stage lineage

#### Scenario: Unknown lifecycle or dependency state

- **WHEN** the API or worker cannot resolve a lifecycle, policy, capability, or dependency state
- **THEN** the run is `blocked`, the mutation is denied, a safe error code is returned, and no model/browser/external write is attempted

### Requirement: Redacted audit, retention, and metrics

The system SHALL audit context create/invalidate, action changes, provider allow/deny, consent, preview view/apply/reject, run/attempt/stage transitions, retry/stop races, capability changes, cross-candidate failures, and export/copy decisions with actor/session/request IDs, resource IDs, old/new state, policy version, reason, and server time. Audit append is through `careerops.append_audit_event`; raw prompts, outputs, source bytes, and credentials are excluded. Preview metadata expires after 30 days, redacted trace events after 90 days, and audit events after 365 days unless a documented hold applies. Metrics are aggregated by candidate-owned scope and never expose another candidate's counts or content.

#### Scenario: Sensitive run detail is cached

- **WHEN** a browser or intermediary requests run detail, stage events, preview metadata, or context metadata
- **THEN** the response includes `Cache-Control: no-store`, sensitive content is absent from persistent browser storage and service-worker caches, and no raw source appears in a trace or metric

#### Scenario: Audit append fails during a mutation

- **WHEN** a run/action/review mutation cannot append its required audit event through the same database transaction
- **THEN** the business mutation rolls back, the API returns a safe denial, and no partially committed lifecycle or review state is visible

#### Scenario: Retention purge encounters a legal hold

- **WHEN** the hourly retention job reaches a preview/trace row with an active legal hold
- **THEN** it retains the held row, records the hold reason and next review time in bounded metadata, purges eligible rows, and alerts if the purge transaction fails
