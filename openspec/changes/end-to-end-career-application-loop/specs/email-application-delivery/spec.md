## ADDED Requirements

### Requirement: System-managed email delivery requires explicit user confirmation
The system SHALL allow the user to confirm an email submission inside CareerOps and SHALL then execute the send through the approved backend provider path. The user MUST NOT be required to open Gmail or manually send the message. A send MUST NOT occur without a current explicit user confirmation bound to the exact recipient, subject, body, attachment set, application, account, and payload hash.

#### Scenario: User confirms an initial application email
- **WHEN** the user reviews the recipient, subject, body, attachments, application, and evidence in CareerOps and clicks the final send confirmation
- **THEN** the system creates or reuses an idempotent send intent and begins the controlled provider execution path without requiring a manual Gmail action

#### Scenario: User only previews the email
- **WHEN** the user opens or edits an email preview but does not confirm sending
- **THEN** the system stores at most a draft and performs no provider write

#### Scenario: Confirmed payload is changed
- **WHEN** any recipient, subject, body, attachment, account, or application binding changes after confirmation
- **THEN** the prior confirmation is invalidated and the system requires a new confirmation before sending

### Requirement: Email recipient eligibility is evidence-bound
The system SHALL send only to a recipient derived from a trusted recruiting contact or a verified reply target linked to the application. The recipient SHALL pass domain, source, and application-linkage checks. The system MUST NOT guess employee addresses, derive recipients solely from model output, or send to an untrusted address.

#### Scenario: Verified recruiting address is available
- **WHEN** a public recruiting address is linked to the job source and its domain matches the company evidence
- **THEN** the system displays the evidence and allows the user to prepare an email submission

#### Scenario: Recipient is guessed or domain-mismatched
- **WHEN** a recipient has no source evidence, is inferred from a name pattern, or does not match the trusted company domain
- **THEN** the system denies delivery and records the denial reason without attempting a provider call

#### Scenario: Reply target comes from a linked thread
- **WHEN** a reply is generated for an existing linked recruiting thread
- **THEN** the system derives the recipient and threading headers from trusted provider metadata and prevents model output from changing them

### Requirement: Email submission uses a complete immutable payload
The system SHALL bind each send intent to the application, account, recipient, subject, normalized body, attachment hashes, thread headers when applicable, evidence references, policy version, and payload hash. The user review representation SHALL match the bytes and targets passed to the provider.

#### Scenario: User approves an application package email
- **WHEN** the selected approved package contains a resume and optional cover letter
- **THEN** the system computes attachment and payload hashes, displays them through the review record, and binds them to the send intent

#### Scenario: Attachment changes after review
- **WHEN** an attachment is replaced, removed, or changes content hash
- **THEN** the system creates a new payload version and requires the user to review and confirm again

### Requirement: Attachments pass safe material validation before delivery
The system SHALL allow only attachments that are selected from an approved application package or explicitly user-selected evidence and that pass declared/detected media-type, size, content-hash, quarantine, and retention checks. Unsupported, mismatched, executable, macro-bearing, archive, password-protected, or expired attachments MUST be denied before any provider call.

#### Scenario: Confirmed resume passes validation
- **WHEN** the user selects a confirmed resume version whose bytes, media type, size, and retention state pass validation
- **THEN** the system binds its content hash to the payload and makes it eligible for user review

#### Scenario: Attachment fails MIME or quarantine checks
- **WHEN** declared and detected types differ or the attachment is unsupported, oversized, quarantined, expired, or unsafe
- **THEN** the system denies the package/send intent and performs no provider write

#### Scenario: Attachment changes after validation
- **WHEN** the selected attachment bytes or content hash change after the review payload is built
- **THEN** the system invalidates the payload version and requires validation and user confirmation again

### Requirement: External email writes pass the full authorization chain
The system SHALL route every external email write through policy evaluation, approval/confirmation, transactional outbox, isolated side-effect worker, provider attempt, reconciliation, provider receipt, and append-only audit. Unknown action kinds, missing evidence, expired approval, unavailable credential, invalid qualification, or disabled capability MUST deny.

#### Scenario: Capability is disabled by default
- **WHEN** external writes, Google integration, or auto-send is disabled in configuration
- **THEN** the system refuses provider execution while keeping preview, package preparation, and application tracking usable

#### Scenario: User confirmation passes policy
- **WHEN** the current confirmation is valid, the recipient and payload are allowlisted, the account is available, and the capability is released
- **THEN** the system enqueues exactly one eligible intent for the side-effect worker and records the policy and approval identities

#### Scenario: Policy dependency fails
- **WHEN** policy, database, Redis lease, credential resolution, or outbox authorization cannot be evaluated
- **THEN** the system fails closed, does not call the provider, and exposes an actionable pending/denied state to the user

### Requirement: Send execution is idempotent and reconciles ambiguity
The system SHALL use a stable idempotency key and provider reconciliation key for each logical send. A timeout, connection loss, worker crash, or unknown provider result MUST enter reconciliation before any retry. The system MUST not blindly send a second copy.

#### Scenario: Worker crashes before provider call
- **WHEN** the side-effect worker crashes before executing the provider request
- **THEN** the intent can be safely retried with the same idempotency identity and produces at most one provider effect

#### Scenario: Provider succeeds before local receipt commit
- **WHEN** the provider accepts the email but the worker loses the response or fails before committing the receipt
- **THEN** the system searches using the reconciliation key, records confirmed sent or ambiguous, and disables blind retry

#### Scenario: Duplicate confirmation is submitted
- **WHEN** the user clicks send twice or repeats the same confirmation request
- **THEN** the system returns the existing intent/receipt state and does not create a second provider effect

### Requirement: Successful delivery updates the application with provider evidence
The system SHALL mark an application submitted only after a provider receipt confirms the system-managed email, and SHALL store provider message/thread identifiers, send time, channel, payload hash, and audit reference. Failed or ambiguous delivery SHALL remain visibly unresolved and SHALL NOT be reported as submitted.

#### Scenario: Provider receipt is confirmed
- **WHEN** the provider returns a verified message identifier and the receipt is persisted
- **THEN** the system records a submitted event linked to the provider identifiers and shows the next follow-up action

#### Scenario: Delivery is ambiguous
- **WHEN** reconciliation cannot determine whether the provider accepted the email
- **THEN** the application remains in a delivery-review state, automatic retry is disabled, and the user receives a reconciliation task
