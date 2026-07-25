## ADDED Requirements

### Requirement: Recruitment mail is ingested from an explicitly authorized account
The system SHALL synchronize recruiting mail only from an account explicitly connected by the user and restricted to the approved Gmail read scope. The default deployment SHALL keep Google integration disabled. Main mailbox ingestion, broad mailbox labels, and unapproved scopes MUST remain unavailable.

#### Scenario: User connects a dedicated recruiting account
- **WHEN** the user completes the approved OAuth flow for a dedicated recruiting account
- **THEN** the system stores only protected credential references and account metadata, records the granted scope set, and enables read synchronization only when policy permits

#### Scenario: OAuth scope includes a forbidden write scope
- **WHEN** the OAuth response or configured scope set includes Gmail send, compose, modify, or full-mailbox scope
- **THEN** the system rejects the connection and does not store or use the credential

#### Scenario: Google integration is disabled
- **WHEN** the deployment has Google OAuth disabled
- **THEN** the system leaves synchronization unavailable and continues to support manual application tracking and user-managed email submission when separately released

#### Scenario: User revokes the recruiting account
- **WHEN** the user or provider revokes the connected account or its approved scope
- **THEN** the system marks the account revoked, stops future sync and provider use, invalidates pending provider actions for that account, and retains prior audit/history only according to retention policy

### Requirement: Mail synchronization is incremental, private, and idempotent
The system SHALL synchronize incrementally using provider history/watch state when available, persist provider message and thread identities, and ignore duplicate deliveries. Non-recruitment message bodies MUST NOT be persisted. Recruitment content SHALL follow classification, attachment quarantine, access control, and retention policy before being used for extraction or model processing.

#### Scenario: Same provider message is delivered twice
- **WHEN** a webhook or polling cycle presents a provider message ID already processed
- **THEN** the system acknowledges or skips the duplicate without creating a second message, extraction, or application event

#### Scenario: Non-recruitment message is ingested
- **WHEN** a message is classified as non-recruitment
- **THEN** the system stores only the minimum metadata required for deduplication/audit and discards the body content

#### Scenario: Sync fails after partial processing
- **WHEN** the worker stops after processing some messages in a synchronization batch
- **THEN** a retry resumes from durable provider state and preserves idempotent results without losing the cursor or duplicating records

### Requirement: Email threads are linked to applications using trusted evidence
The system SHALL link a recruiting thread to an application using stable provider thread/message identifiers, system-managed sent-message identifiers, verified recipient/sender domains, subject and job/source evidence, and existing user linkage. If more than one application is plausible, the system MUST create an unresolved-link review item rather than silently choosing one.

#### Scenario: Reply follows a system-managed application email
- **WHEN** an inbound message references a provider message or thread identifier recorded on a submitted application
- **THEN** the system links the message to that application with high-confidence provenance

#### Scenario: Thread has ambiguous company matches
- **WHEN** sender, subject, and content could match multiple active applications
- **THEN** the system leaves the thread unlinked or proposes candidates with evidence and asks the user to choose

#### Scenario: User confirms an ambiguous link
- **WHEN** the user selects the correct application for an unresolved thread
- **THEN** the system records the user decision and uses the link for future messages in that provider thread

### Requirement: Email understanding produces structured, evidence-backed proposals
The system SHALL classify recruitment messages into a controlled taxonomy and extract relevant fields such as outcome, next step, interview time, timezone, deadline, assessment, requested materials, compensation, authorization, and sender identity. Every extraction SHALL include confidence, rule/model version, bounded evidence references, and proposal status. Untrusted email content or model output MUST NOT directly alter policy, recipients, credentials, or application state.

#### Scenario: Interview invitation is detected
- **WHEN** a linked message contains a credible interview invitation and a time expression
- **THEN** the system proposes an interview event with the extracted time, timezone, evidence span, confidence, and a user-review action

#### Scenario: Rejection email is detected
- **WHEN** a linked message contains a credible rejection outcome
- **THEN** the system proposes a rejected application event with the source message and evidence, without silently changing the application state

#### Scenario: High-risk content is detected
- **WHEN** a message contains offer, salary, visa, identity, banking, tax, or withdrawal content
- **THEN** the system marks the extraction high-risk, blocks autonomous state/send actions, and routes it to a mandatory user review queue

#### Scenario: Message contains prompt injection
- **WHEN** email body, attachment text, signature, or HTML includes instructions to reveal secrets, change recipients, or bypass review
- **THEN** the system treats those instructions as untrusted content and prevents them from affecting tools, policy, state, or send targets

### Requirement: User can review and accept or reject mail-derived events
The system SHALL provide a review record for uncertain or state-changing mail interpretations. A review SHALL show the linked application, message metadata, bounded evidence, proposed event, confidence, and consequences. Acceptance or rejection SHALL be auditable and idempotent.

#### Scenario: User accepts a proposed interview event
- **WHEN** the user reviews the evidence and accepts the proposed interview event
- **THEN** the system appends an application event, updates the application state according to the legal transition table, and creates any configured scheduling/follow-up task

#### Scenario: User rejects a false classification
- **WHEN** the user marks a proposed event as incorrect
- **THEN** the system records the rejection, leaves the application state unchanged, and prevents the same proposal identity from being applied again

#### Scenario: Review request is repeated
- **WHEN** the client repeats an already completed review decision
- **THEN** the system returns the recorded result without applying a second state transition

### Requirement: Mail and application views show a unified timeline
The system SHALL provide a user-facing view of linked threads, message summaries, extracted events, pending reviews, reply drafts, and application state in chronological order. The UI SHALL distinguish confirmed facts, system suggestions, user decisions, and unresolved ambiguity.

#### Scenario: User opens a submitted application
- **WHEN** the application has inbound recruiting messages
- **THEN** the UI shows the latest thread summary, proposed/confirmed status, last sync time, pending action, and links to the supporting evidence

#### Scenario: Synchronization is unavailable
- **WHEN** the mail provider or sync worker is unavailable
- **THEN** the UI shows stale-data time and an actionable unavailable state without presenting old data as current
