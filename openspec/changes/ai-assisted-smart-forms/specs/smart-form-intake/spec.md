## ADDED Requirements

### Requirement: Smart intake exposes only the frozen MVP targets and input contract

The system SHALL support only `profile` and `interview_context` targets in this change. Both targets SHALL accept bounded text normalized to Unicode NFC with CRLF/CR converted to LF through a server-owned request schema. `profile` MAY reference an owned `profile_version_id` (or the server-resolved active version, or the sentinel `none`) for staleness. `interview_context` SHALL reference an existing shared `canonical_job_id`, its bound `job_version_id`, and an owned confirmed `resume_version_id`, and MAY reference an owned profile version and at most 50 owned evidence IDs. The server SHALL reject files, arbitrary URLs, raw resume bytes, typed mailbox/browser payloads, client-supplied prompts/schemas, unsupported targets, and references that do not belong to the authenticated candidate before model invocation.

#### Scenario: User requests a profile preview from text

- **WHEN** an authenticated candidate submits at most 12,000 normalized Unicode scalar values with target `profile` and a stable idempotency key
- **THEN** the system validates the input and selects the server-owned profile schema without activating or persisting a profile version

#### Scenario: User requests interview context with identity references

- **WHEN** an authenticated candidate submits at most 2,000 normalized Unicode scalar values with target `interview_context`, a canonical job ID, its job-version ID, an owned confirmed resume-version ID, and optional owned profile/evidence IDs
- **THEN** the system verifies the shared job binding and candidate-owned references, binds them to the preview for stale detection, and preserves the existing Agent start contract without treating them as trusted model facts

#### Scenario: Client submits an unsupported source

- **WHEN** a request contains a URL, file, raw resume content, arbitrary schema, unsupported target, or another candidate's context ID
- **THEN** the system rejects the request before model input assembly with a bounded error and creates no preview

### Requirement: Profile and interview previews use closed, bounded field schemas

The `profile` schema SHALL allow only `target_roles[]` (maximum 5; title maximum 120 characters, seniority maximum 80, notes maximum 240), `locations[]` (maximum 8; name maximum 120, `kind` exactly `preferred`, `radius_km` either null or an integer from 0–500), `include_keywords[]` (maximum 30 strings of maximum 80 characters), and `exclude_keywords[]` (maximum 30 strings of maximum 80 characters). The profile schema SHALL NOT include top-level `seniority`, `remote_rules`, `compensation`, `authorization`, or `hard_exclusions`. The `interview_context` schema SHALL allow only `user_context` of maximum 2,000 characters. Array order SHALL be the server's normalized proposal order; duplicate case-folded values SHALL be removed deterministically. Schemas SHALL reject unknown keys and `additionalProperties`.

#### Scenario: Model returns a schema-valid low-risk field

- **WHEN** the model proposes a target role, preferred location, keyword, or interview context within the target limits
- **THEN** the system retains it as a bounded proposal that can be reviewed and edited

#### Scenario: Model returns a protected or oversized field

- **WHEN** the model returns compensation, remote policy, authorization/visa, hard exclusions, consent, dates, permissions, application facts, `locations.kind` other than `preferred`, an unknown key, or an oversized value
- **THEN** the system marks it `blocked`/`unknown` or drops it, records a bounded reason, and never exposes it as an accepted field

### Requirement: Every preview field is attributable and explicitly untrusted

The system SHALL return each field with a typed value or null, confidence in the range 0..1, status (`proposed`, `unknown`, or `blocked`), a bounded reason when applicable, and at most three server-verifiable source references. The input digest SHALL be SHA-256 of the normalized text's UTF-8 bytes. A source reference SHALL contain that digest and a half-open `[start, end)` offset measured in Unicode scalar values after normalization; offsets SHALL be within the normalized text. For profile scalar values, the normalized case-folded value SHALL occur within at least one referenced normalized span; `interview_context.user_context` MAY reference the complete input because it is a user-context rewrite, not evidence. Model-authored references SHALL be validated before display and SHALL not create trusted evidence or policy facts.

#### Scenario: Model proposes a sourced role

- **WHEN** a schema-valid role is returned with offsets inside the normalized submitted text and the role value occurs in the referenced span
- **THEN** the preview shows the role as a proposal with bounded confidence and the validated source span

#### Scenario: Model fabricates a source span

- **WHEN** a returned source span is outside the input, uses a different digest, does not contain the profile value, or exceeds the reference limit
- **THEN** the system marks the field unknown/blocked or removes the reference and does not treat the field as evidence

### Requirement: Disabled and failed model states preserve manual workflows

The system SHALL keep `smart_intake_enabled=false` and the model provider disabled by default. Capability denial SHALL return `403 SMART_INTAKE_DISABLED` before input assembly. When the capability is released but `model_client.is_enabled` is false, the system SHALL return `200 state=unavailable` with zero provider calls. Provider timeout, 429, 5xx, or unavailable dependency SHALL return `200 state=unavailable`; explicit model abstention SHALL return `200 state=abstained`; schema or repair failure SHALL return `200 state=invalid`. All such responses SHALL contain no trusted fields. The existing manual Profile and Agent workflows SHALL remain usable in every such state.

#### Scenario: Capability is disabled

- **WHEN** a candidate requests a preview while smart intake is not released
- **THEN** the endpoint returns `403 SMART_INTAKE_DISABLED`, makes no model call, and the UI presents the ordinary manual form

#### Scenario: Provider is disabled after capability release

- **WHEN** a candidate requests a preview while the capability is released but `MODEL_PROVIDER=disabled`
- **THEN** the endpoint returns a safe `200` response with state `unavailable`, zero provider calls, and a manual fallback message

#### Scenario: Provider output cannot be validated

- **WHEN** the provider returns invalid JSON or schema-invalid output after the permitted repair attempt
- **THEN** the endpoint returns `state=invalid` without exposing partial output as a proposal

### Requirement: Preview creation, retrieval, and apply are candidate-scoped and idempotent

The system SHALL provide candidate-scoped create, retrieve, and apply endpoints. Preview creation SHALL require `idempotency_key` and use a canonical request fingerprint unique per `(candidate_id, target, idempotency_key)`. Canonicalization SHALL preserve array order, sort object keys, use normalized text, and hash UTF-8 JSON with no whitespace. A pending claim SHALL be inserted atomically before model invocation with a 30-second lease; a live duplicate SHALL return `409 SMART_PREVIEW_IN_PROGRESS`, while an expired claim MAY be reclaimed. An equivalent retry SHALL return the same preview, while a different request with the same key SHALL return `409 IDEMPOTENCY_KEY_REUSED`. Apply SHALL require `apply_idempotency_key` and a client `decision_set_hash`; the server SHALL recompute the hash over sorted closed field paths, decisions, typed values, and bounded reasons. A hash mismatch returns `409 IDEMPOTENCY_KEY_REUSED`; an equivalent retry returns the same draft and SHALL NOT duplicate decisions or model usage while preview values are retained. Once the preview is expired, revoked, or purged, the `410 SMART_PREVIEW_EXPIRED` tombstone takes precedence and no draft values are returned. Cross-candidate preview IDs SHALL return indistinguishable `404 SMART_PREVIEW_NOT_FOUND`.

#### Scenario: User double-clicks preview creation

- **WHEN** the same candidate submits the same target, input digest, context references, and idempotency key concurrently
- **THEN** the system claims the key before model invocation and returns one preview identity without duplicate provider usage

#### Scenario: User reuses a key for different input

- **WHEN** the same candidate submits a different fingerprint with an existing idempotency key
- **THEN** the system returns `409 IDEMPOTENCY_KEY_REUSED` and leaves the original preview unchanged

#### Scenario: Another candidate reads a preview

- **WHEN** a request references a preview owned by another candidate
- **THEN** the system returns `404 SMART_PREVIEW_NOT_FOUND` without revealing state, target, timestamps, or field data

### Requirement: Previews are immutable, short-lived, and stale-aware

The system SHALL bind each preview to a candidate, input digest, context digest, source/version IDs, schema/prompt/model versions, and capability state. The server SHALL recompute and compare the context digest during apply. The context digest SHALL include target, active/base profile ID and rules version (or `none`), and for interview context the canonical job ID, job-version content hash, confirmed resume-version content hash, selected profile rules version, sorted confirmed evidence IDs/hashes, schema version, and policy version. A preview SHALL expire 30 minutes after creation; a non-sensitive candidate-owned tombstone SHALL remain after value purge so expired, revoked, or purged previews return `410 SMART_PREVIEW_EXPIRED` without field values, and a changed input/context/policy version SHALL return `409 STALE_SMART_INTAKE_PREVIEW`. Preview values SHALL be purged within 24 hours after expiry by the retention role while decision metadata remains bounded and append-only.

#### Scenario: Profile changes before apply

- **WHEN** the active profile or referenced context version changes after preview creation
- **THEN** the apply request returns `409 STALE_SMART_INTAKE_PREVIEW` and does not mutate the profile

#### Scenario: User applies an expired preview

- **WHEN** the preview is older than its 30-minute review window or has been purged/revoked
- **THEN** the apply request returns `410 SMART_PREVIEW_EXPIRED` without returning the old field values

### Requirement: Applying a preview records decisions and returns only a local draft patch

The apply operation SHALL accept decisions only for fields present in the immutable preview, with `accept`, `edit`, `reject`, or `unknown` decisions. Decision keys SHALL use only `target_roles[i].title|seniority|notes`, `locations[i].name|radius_km`, `include_keywords[i]`, `exclude_keywords[i]`, or `user_context`, where `i` is the server-normalized proposal index. Values SHALL match the target schema and `reason` SHALL be at most 240 characters. It SHALL reject attempts to confirm blocked or unknown fields as trusted facts. It SHALL record actor, decision, decision-set hash, preview identity, bounded reason, the submitted value digest, and the immutable proposal value digest, then return a non-persisted `draft_patch`. The frontend SHALL compare each indexed field and array with the local baseline captured at preview creation; if a local value differs, it SHALL leave that value unchanged and report a conflict. If the user later starts an Agent run, `user_context` SHALL remain fenced as untrusted user input and SHALL NOT become evidence, a policy fact, or an instruction with tool authority. The existing profile save or Agent start operation SHALL remain a separate user action and final authority.

#### Scenario: User accepts and edits profile fields

- **WHEN** the user accepts one role, edits one location, and rejects one keyword
- **THEN** the apply response contains only the accepted/edited low-risk patch and the existing profile save endpoint performs final validation and version creation

#### Scenario: User applies interview context

- **WHEN** the user accepts or edits the proposed `user_context`
- **THEN** the response updates only the local AgentWorkbench context draft and does not start interview preparation until the user separately clicks the existing start action

#### Scenario: Client submits an unreturned field

- **WHEN** the apply request includes a key absent from the preview, a value outside the target schema, or attempts to promote a blocked field
- **THEN** the system rejects the request and leaves the preview, target form, and target resource unchanged

### Requirement: Smart intake cannot authorize protected fields or side effects

The system SHALL never use a smart preview as the sole authority for compensation, remote policy, work authorization, visa sponsorship, hard exclusions, identity/contact data, consent, dates, permissions, evidence confirmation, profile activation, crawl execution, browser navigation, package approval, mail, application state, application submission, or any external write. The smart-intake route SHALL not call those action services. For `interview_context`, the existing Agent start operation SHALL still require and independently validate `canonical_job_id`, bound `job_version_id`, owned confirmed `resume_version_id`, optional profile version, and confirmed evidence IDs; smart intake cannot make any of those inputs eligible.

#### Scenario: Model detects salary or authorization text

- **WHEN** pasted text contains salary, visa, authorization, or hard-exclusion information
- **THEN** the corresponding existing manual control remains visible and the preview marks the value unknown/blocked rather than changing a policy fact

#### Scenario: User applies a ready preview

- **WHEN** the user applies any accepted preview fields
- **THEN** no crawl, Ego/browser, Agent-start, package, email, OAuth, application-submit, or external form operation is initiated

### Requirement: Smart intake obeys privacy, CSRF, origin, rate-limit, and untrusted-content boundaries

Create and apply SHALL require the existing authenticated session, CSRF protections, and allowlisted Host+Origin validation, and production SHALL not use an auth-less fallback. Pasted text SHALL be bounded and treated as untrusted content with no model tools. The `smart_intake_preview` limiter SHALL allow at most 10 preview creations per candidate-hash per 10 minutes and one in-flight claim; Redis/limiter failure SHALL deny. Raw prompts, raw provider responses, credentials, raw resume bytes, unrelated mailbox/browser data, and field values MUST NOT be written to logs, traces, analytics, local storage, or unrestricted audit JSON by default. Successful, error, and GET responses SHALL be `Cache-Control: no-store`; metrics SHALL use bounded labels without user content, job titles, email addresses, or trace IDs.

#### Scenario: Pasted text contains prompt injection

- **WHEN** input asks the model to call a tool, reveal a secret, change a recipient, or override policy
- **THEN** the model receives it only as untrusted content with no tools, output remains schema-allowlisted, and no policy or side effect changes

#### Scenario: Redis limiter is unavailable

- **WHEN** the candidate-hash limiter cannot make a reliable decision
- **THEN** preview creation is denied closed and the manual form remains available

### Requirement: The frontend provides a consistent manual-first review interaction

The frontend SHALL provide a reusable smart-intake interaction with input, loading, provider/capability state, field-level accept/edit/reject controls, confidence/source/status indicators, expiry/stale errors, and a manual fallback. Profile and AgentWorkbench SHALL integrate it through thin adapters. Existing manual fields SHALL remain visible, dirty manual values SHALL not be overwritten silently, and the interaction SHALL be keyboard accessible and usable on narrow mobile viewports.

#### Scenario: User reviews a ready profile preview on mobile

- **WHEN** a candidate opens a ready preview at a narrow viewport
- **THEN** every field decision remains readable and operable, manual controls remain reachable, and the primary action says review/apply rather than save/submit

#### Scenario: User dismisses AI assistance

- **WHEN** the candidate closes the preview, rejects all fields, or receives an unavailable state
- **THEN** manually entered values remain intact, no target resource changes, and the ordinary form path remains available

### Requirement: Smart-intake decisions are auditable and capability-reversible

The system SHALL append an audit/decision record for preview outcome and each field-level decision through the shared `careerops.append_audit_event` SECURITY DEFINER path in the same transaction, including candidate, actor, target, preview ID, input/context digests, schema/model/capability versions, trace correlation, and bounded reasons without raw content. The API role SHALL not insert directly into the audit table. Disabling smart intake SHALL revoke and purge unapplied preview values, retain non-sensitive tombstones and bounded decision metadata according to retention, block new previews, and leave manual Profile and Agent workflows usable.

#### Scenario: User corrects a proposal

- **WHEN** the user edits a proposed role or interview context before apply
- **THEN** the record distinguishes the model proposal from the user-authored correction and preserves both identities without rewriting source material

#### Scenario: Capability is rolled back

- **WHEN** an operator disables smart intake after previews exist
- **THEN** new preview creation is denied with a safe capability reason, unapplied values are revoked/purged, prior bounded decision metadata remains auditable, and manual workflows continue to function
