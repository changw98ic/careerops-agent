## MODIFIED Requirements

### Requirement: Existing smart-intake preview/apply remains the short-lived authority

The existing `smart-form-intake` preview/apply contract is modified only by the shared capability/preflight, egress, rendering, and audit requirements in this change. Its short-lived preview store and explicit apply decision remain authoritative; this change SHALL not create a second generic preview store or silently widen its targets.

#### Scenario: Existing smart intake keeps its short-lived authority

- **WHEN** a profile or interview-context preview is created through the existing smart-intake route
- **THEN** it uses the existing preview identity/expiry/apply decision contract, while the new preflight and egress checks add gates without creating a parallel preview store

## ADDED Requirements

### Requirement: Explicit capability resolution

The system SHALL resolve model capability state from backend policy, provider configuration, dependency readiness, feature release, operation prerequisites, provider allowlist, consent, rate budget, and output policy. The response SHALL identify whether the operation is `enabled`, `disabled_by_policy`, `not_configured`, `dependency_not_ready`, `blocked_by_prerequisite`, `stale`, or `failed`. Unknown policy, provider, release, or dependency states SHALL resolve to blocked/denied, never enabled.

#### Scenario: Safe default environment

- **WHEN** the model provider is disabled or external writes are disabled
- **THEN** the API and UI state that fact plainly, do not call an external model, and expose the manual/deterministic path

#### Scenario: Frontend/backend release mismatch

- **WHEN** the frontend release flag is enabled but the backend capability is not released
- **THEN** the operation remains unavailable, the UI explains that backend release is required, and no request is sent to the model gateway

#### Scenario: Missing operation prerequisite

- **WHEN** a resume review lacks a confirmed resume or target job
- **THEN** the UI identifies each missing input, links to the source page, and keeps the generate action unavailable without implying a provider failure

#### Scenario: Provider policy is incomplete

- **WHEN** the configured provider host, model, region, retention, training policy, or operation consent is missing or unknown
- **THEN** the capability is blocked, no provider request is made, and the UI gives the deterministic/manual alternative

### Requirement: Structured preview before apply

Every model-assisted form or Agent preparation operation SHALL expose a preview contract with typed proposed fields, source spans or evidence references where applicable, confidence, unknowns, warnings, schema/prompt versions, model/provider identity, context digest, expiry, and request fingerprint. Applying a preview MUST require explicit user decisions and MUST call the authoritative existing write/draft contract.

#### Scenario: Preview contains an unsupported claim

- **WHEN** the model proposes a claim without an allowed evidence reference
- **THEN** the field is marked unsupported/unknown, cannot become trusted evidence by default, and the user can reject or edit it before apply

#### Scenario: User edits a proposed field

- **WHEN** the user changes a proposed value before applying
- **THEN** the preview records the user edit separately from the model proposal and the authoritative write receives the edited value only after validation

#### Scenario: Preview expires

- **WHEN** the preview is expired or its context digest no longer matches
- **THEN** apply is rejected as stale, no authoritative record changes, and the UI offers regenerate/refresh

#### Scenario: Preflight shows model egress

- **WHEN** a candidate is about to request a provider preview
- **THEN** the UI shows the operation goal, selected source versions, allowlisted fields after redaction, byte/token budget, provider capability, expected output, expiry, and controls to remove a source or cancel before any provider request

### Requirement: Untrusted content isolation

The system SHALL treat job descriptions, resumes, email content, browser content, and free-form user notes as untrusted data. Model prompts MUST isolate such content from system instructions, model outputs MUST be schema-validated and bounded, and the model MUST have no tool binding or authority to perform external writes.

#### Scenario: Prompt injection in a job description

- **WHEN** a job description contains instructions to ignore policy or call tools
- **THEN** the content is passed as untrusted data, the model response cannot change policy/capability state, and any unsupported result is shown for review only

#### Scenario: Oversized input

- **WHEN** input exceeds the operation's byte, token, or field budget
- **THEN** the request is rejected or safely truncated according to the operation contract, the user sees what was omitted, and no unbounded provider request is made

#### Scenario: Output requests a tool or policy change

- **WHEN** untrusted content or provider output asks to call a tool, change policy, reveal a secret, or perform an external action
- **THEN** the request is treated as unsupported content, no tool or policy change occurs, and the user sees a review-only warning

### Requirement: Controlled provider egress and consent

The model gateway SHALL send only server-allowlisted, explicitly consented fields over verified TLS to an allowlisted provider/model. The server SHALL redact direct identifiers and secrets, exclude raw resume/PDF bytes, mailbox contents, credentials, arbitrary URLs, and unconfirmed evidence by default, and record only provider/model/region/retention policy metadata. SDK logs, HTTP caches, browser storage, service-worker caches, raw prompts, and raw responses MUST NOT retain source content.

#### Scenario: Candidate removes a source before preview

- **WHEN** the candidate removes a resume/evidence source in preflight
- **THEN** the request fingerprint and displayed source set change, the removed source is not sent, and apply requires a preview generated from the new context

#### Scenario: Provider host is not allowlisted

- **WHEN** a runtime configuration points to a provider host outside the server allowlist
- **THEN** the capability is denied before network connection, a bounded audit event records the policy decision, and the manual/deterministic path remains available

#### Scenario: Consent is revoked while a run is queued

- **WHEN** the candidate revokes the operation consent or the provider policy/field set changes after queueing but before the worker connects
- **THEN** the worker rechecks the server-owned consent/policy envelope, blocks the run before network connection, records the consent/policy version and deny reason, and performs no provider call

#### Scenario: Fake provider captures the outbound payload

- **WHEN** a fake provider is used for a permitted resume-review operation
- **THEN** the captured JSON contains only the operation's exact allowlisted fields and version IDs, excludes disallowed PII/raw bytes/URLs, and includes the server-generated egress field-set hash

### Requirement: Truthful fallback and error UX

The system SHALL provide a manual or deterministic fallback for every model-assisted surface that is not enabled. It SHALL distinguish disabled, unavailable, rate-limited, invalid, stale, and provider-error states, include a trace/reference where available, and never display a fabricated confidence or model result.

#### Scenario: Model gateway times out

- **WHEN** a provider request exceeds its bounded timeout
- **THEN** the operation records a retryable timeout, preserves the manual form/draft, and offers retry without duplicating the preview or authoritative write

#### Scenario: Provider returns invalid structure

- **WHEN** the model output fails schema validation after the allowed repair attempt
- **THEN** the result is rejected as invalid, no fields are applied, and the UI explains that the model output could not be trusted

#### Scenario: Provider rate limit

- **WHEN** the provider returns a rate-limit response or the candidate budget is exhausted
- **THEN** the run is marked retryable/blocked with a next-eligible time, the UI explains the limit and manual path, and no tight retry loop occurs

#### Scenario: User cancels a provider run

- **WHEN** the user requests cancellation while a provider activity is running
- **THEN** the durable run shows `cancel_requested`, the activity stops at a safe checkpoint, and the UI shows `cancelled` or a typed cancellation failure rather than a completed result

### Requirement: Privacy and usage telemetry

The system SHALL record bounded model metadata including task type, provider/model identifier, prompt/schema version, token counts, latency, outcome, trace ID, and redacted error category. It SHALL exclude raw prompts/responses and sensitive source bytes by default and SHALL enforce configured retention.

#### Scenario: Successful preview telemetry

- **WHEN** a preview completes
- **THEN** usage and latency metadata are available to the owned run/trace and aggregate metrics without exposing raw content

#### Scenario: Provider disabled telemetry

- **WHEN** no provider call is made because capability is disabled
- **THEN** telemetry records a disabled outcome with zero provider usage and does not imply a model invocation

### Requirement: Safe output rendering and evidence revalidation

The system SHALL render model, browser, job, email, and user-authored content as untrusted text unless a server-owned safe renderer explicitly permits formatting. Raw HTML, scripts, event handlers, unsafe URLs, arbitrary route names, and model-supplied actions MUST be rejected. Every evidence reference or source span SHALL be revalidated against the authenticated candidate and immutable source version immediately before display and apply. Sensitive responses SHALL be `no-store` and excluded from persistent browser storage.

#### Scenario: Model returns unsafe markup or route

- **WHEN** a provider result contains HTML, a `javascript:` URL, or a route/action not in the server allowlist
- **THEN** the unsafe value is removed or marked unsupported, no navigation/action occurs, and the user sees a bounded warning

#### Scenario: Evidence changed after preview

- **WHEN** an evidence item is revoked or its source version changes after a preview was generated
- **THEN** the preview becomes stale, the evidence chip is invalidated, and apply cannot write the claim

### Requirement: Human-friendly progress and clarification

The UI SHALL distinguish preflight, queued, running, waiting for clarification, waiting for review, succeeded, failed, cancelled, blocked, and stale using durable server state. A model-dependent surface SHALL expose one safe next action, bounded progress guidance, supported cancel/retry controls, and qualitative uncertainty when numeric confidence is not calibrated.

#### Scenario: Provider asks for missing context

- **WHEN** a provider result cannot proceed without a missing user goal or source selection
- **THEN** the UI asks one bounded clarification question, preserves the draft, makes no silent provider retry, and lets the candidate edit the context before resubmitting

#### Scenario: Model is disabled on an AI-shaped page

- **WHEN** `model_provider=disabled` on the Agent workbench or smart-intake surface
- **THEN** the UI states “当前未启用大模型”, identifies the deterministic/manual alternative, and offers exactly one safe next action without a fake spinner or confidence score

### Requirement: Mandatory preflight before every model capability

Every model-assisted operation, including smart intake, resume review, interview preparation, and optional matching explanation, SHALL first call `POST /api/v1/agent-console/preflight` with the server-issued context reference and operation. Preflight SHALL make no provider call and SHALL return the capability state, exact outbound field set, redaction version, budget, provider policy version, required consent scope, and a single-use `preflight_id`/`consent_id`. Preview/start requests without a valid unexpired preflight and consent SHALL be denied before model input assembly.

#### Scenario: Direct preview bypasses preflight

- **WHEN** a client calls a preview or Agent-start endpoint without a valid server-issued preflight and consent for the same context/field set
- **THEN** the API returns `403 PREFLIGHT_REQUIRED`, makes no provider call, and leaves the manual/deterministic path available

#### Scenario: Preflight is reviewed before provider use

- **WHEN** the candidate removes a source or changes the selected operation after preflight
- **THEN** the previous `preflight_id`/consent is invalidated, the displayed field set changes, and a new preflight is required
