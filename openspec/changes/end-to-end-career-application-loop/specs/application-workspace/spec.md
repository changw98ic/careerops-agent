## ADDED Requirements

### Requirement: Application is the central lifecycle record
The system SHALL create at most one active application for a candidate and canonical job pair unless the user explicitly starts a documented re-application cycle. An application SHALL link the candidate, canonical job, selected source posting when relevant, current state, creation/update times, and an append-only event history.

#### Scenario: User creates an application from a favorite job
- **WHEN** the user starts an application for a job that has no active application
- **THEN** the system creates one application in the initial favorite state and records a created event with the initiating user; entering the preparation workspace is a separate legal transition to the preparation state

#### Scenario: User clicks apply twice
- **WHEN** the same user repeats an application creation request for the same candidate and job
- **THEN** the system returns the existing application and does not create a second active application

#### Scenario: User explicitly re-applies
- **WHEN** the user requests a new application cycle after a prior terminal application and provides a reason
- **THEN** the system creates a new cycle linked to the prior application and preserves both histories

### Requirement: Application states follow a validated lifecycle
The system SHALL enforce legal application transitions such as favorited, preparing, submitted, interviewing, offer, rejected, withdrawn, and on-hold. Every transition SHALL include source, actor, timestamp, reason or evidence when applicable, and an immutable event record. A model or inbound email classifier MUST NOT directly perform a state transition.

#### Scenario: User moves a preparation to submitted
- **WHEN** the user confirms a successful system-managed send or confirms a manual external submission with required evidence
- **THEN** the system transitions the application to submitted and records the channel, timestamp, and submission reference

#### Scenario: Illegal transition is requested
- **WHEN** a caller attempts to move a rejected application directly to interviewing without a new application cycle or reviewed evidence
- **THEN** the system rejects the transition and leaves the application and event history unchanged

#### Scenario: Email classifier proposes an interview
- **WHEN** the email intelligence service identifies an interview invitation
- **THEN** the system creates a proposed application event with evidence and requires the configured user review before changing the application state

### Requirement: Application has an explicit submission channel
The system SHALL represent the selected channel as `email`, `external_form`, or `manual`. The channel selection SHALL be derived from trusted job/source evidence and user choice. The system SHALL show why a channel is eligible or unavailable before the user can prepare a submission.

#### Scenario: Job has a verified recruiting email
- **WHEN** a source-backed recruiting contact passes domain and trust checks
- **THEN** the application workspace offers email submission and displays the source evidence

#### Scenario: Job has only an external form
- **WHEN** the job has a trusted official apply URL but no verified recruiting email
- **THEN** the workspace offers external-form submission and does not invent an email recipient

#### Scenario: User selects an unavailable channel
- **WHEN** the user attempts to select email without an eligible verified contact
- **THEN** the system blocks the selection and offers the supported channel or a manual handling path

### Requirement: Submission package is bound to the application
The system SHALL allow the user to select one confirmed resume version and one approved application package for a submission. The workspace SHALL show the job version, package payload hash, attachments, recipient/channel, and approval status. Changing a job, recipient, resume, attachment, or body SHALL invalidate the prior package approval.

#### Scenario: User reviews an application package
- **WHEN** the user opens the submission review
- **THEN** the UI shows the exact job evidence, resume version, generated or edited materials, attachments, target channel, and unresolved validation errors

#### Scenario: Package input changes after approval
- **WHEN** the user changes the selected resume or any payload byte after package approval
- **THEN** the system creates a new package version or invalidates the old approval and requires review again

### Requirement: External-form submission is user-confirmed and auditable
For an external-form channel, the system SHALL open or expose the trusted application URL and provide a manual completion checklist. The system SHALL not claim that the application was submitted until the user explicitly confirms completion and provides the minimum required evidence.

#### Scenario: User completes an external form
- **WHEN** the user returns to CareerOps and confirms submission with the official URL and submission time
- **THEN** the system records a manually confirmed submitted event and links the evidence to the application

#### Scenario: User abandons an external form
- **WHEN** the user leaves the external form without confirming completion
- **THEN** the application remains in preparation and the system does not create a submitted event

### Requirement: Application timeline is the user-facing source of truth
The system SHALL present a chronological timeline containing job decisions, package versions, submission attempts, inbound email events, user decisions, reminders, and follow-up actions. System suggestions SHALL be visibly distinguished from user-confirmed facts.

#### Scenario: User opens an application with new mail
- **WHEN** a linked recruiting thread contains a new unreviewed message
- **THEN** the timeline shows the message summary, proposed event, evidence, confidence, and the next review action

#### Scenario: User filters the application list
- **WHEN** the user selects submitted, awaiting reply, interviewing, rejected, offer, or needs review
- **THEN** the system returns applications based on current lifecycle state and pending review items without losing the timeline history

### Requirement: Follow-up tasks are derived and user-controllable
The system SHALL support one or more versioned follow-up rules that create reminders from confirmed submission events and configurable elapsed time. Users SHALL be able to snooze, reschedule, complete, or cancel reminders. Terminal application states SHALL cancel or resolve active reminders according to policy.

#### Scenario: Submission creates a follow-up reminder
- **WHEN** an application enters submitted and the active follow-up rule applies
- **THEN** the system creates one idempotent reminder with due time, rule version, and a link to the application

#### Scenario: User snoozes a reminder
- **WHEN** the user snoozes an active reminder until a valid future time
- **THEN** the system records the snooze event and does not create duplicate active reminders for the same rule version

#### Scenario: Application is rejected
- **WHEN** the application enters a terminal rejected state
- **THEN** the system cancels or resolves active follow-ups and records the reason
