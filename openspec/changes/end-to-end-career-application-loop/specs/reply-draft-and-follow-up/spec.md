## ADDED Requirements

### Requirement: Follow-up rules create actionable reminders
The system SHALL allow versioned follow-up rules based on confirmed application events, message outcomes, deadlines, and user-configured waiting periods. Each reminder SHALL link to an application and rule version and SHALL support due, snoozed, cancelled, completed, and rescheduled states.

#### Scenario: No response after submission
- **WHEN** a confirmed submitted application reaches the active waiting threshold without a new linked message
- **THEN** the system creates one follow-up reminder with the next action and due time

#### Scenario: New reply arrives before reminder
- **WHEN** a linked recruiting reply arrives before an active follow-up is due
- **THEN** the system suppresses or re-evaluates the reminder and shows the new mail review action

#### Scenario: User cancels a follow-up
- **WHEN** the user cancels a reminder and provides an optional reason
- **THEN** the system records the cancellation and does not recreate it from the same rule version unless the user explicitly requests it

### Requirement: Reply drafts use constrained trusted context
The system SHALL generate reply drafts only for a linked application/thread and SHALL use the minimum relevant message excerpt, application facts, confirmed candidate evidence, and user-selected intent. A draft MUST preserve the trusted recipient and thread headers and MUST NOT introduce unsupported claims, commitments, or sensitive disclosures.

#### Scenario: User requests a scheduling reply
- **WHEN** the user selects a linked interview message and chooses a scheduling intent
- **THEN** the system creates a draft using the message context, user availability, timezone, and approved application facts, and shows the evidence/context used

#### Scenario: Draft contains an unsupported promise
- **WHEN** a generated draft includes an unverified skill, deadline commitment, salary statement, or authorization claim
- **THEN** the system marks the draft invalid or requires explicit user correction before it can enter approval

#### Scenario: User changes only the body
- **WHEN** the user edits the subject or body while keeping the trusted recipient and thread binding unchanged
- **THEN** the system creates a new draft payload hash and requires approval for that exact content

### Requirement: Reply review is mandatory before system-managed sending
The system SHALL display every reply's recipient, thread identity, subject, body, attachments, evidence references, application linkage, and risk category before the user approves sending. The user SHALL approve inside CareerOps; approval SHALL invoke the system-managed email delivery path and SHALL NOT delegate the final action to manual Gmail usage.

#### Scenario: User approves a low-risk acknowledgement
- **WHEN** the user reviews and approves a delivery confirmation or scheduling acknowledgement
- **THEN** the system submits it through the same controlled email delivery chain and shows pending/sent/reconciliation status

#### Scenario: User rejects a reply draft
- **WHEN** the user rejects a proposed reply
- **THEN** the system records the rejection, performs no provider write, and leaves the application state unchanged

#### Scenario: Recipient is edited
- **WHEN** the user attempts to change the recipient or target thread of an existing draft
- **THEN** the system rejects the in-place edit and requires a new proposal that re-enters recipient trust, policy, and approval checks

### Requirement: Automatic reply is disabled by default and risk-bounded
The system SHALL keep auto-send disabled by default. High-risk categories including offer, salary, visa, relocation, tax, background check, identity/bank, withdrawal, unknown, deadline commitments, resume/link delivery, and work authorization MUST require explicit user approval and MUST NOT be auto-sent by any model or rule. Any future low-risk auto-send MUST require the existing global kill switch, release qualification, account opt-in, policy allowlist, and reconciliation controls.

#### Scenario: High-risk email is classified
- **WHEN** a reply concerns salary, Offer, visa, identity, withdrawal, or another permanent-deny category
- **THEN** the system creates a draft/review task and refuses any automatic provider execution

#### Scenario: Auto-send configuration is present but capability is not released
- **WHEN** a user or environment supplies an auto-send category but release qualification or the global kill switch is not valid
- **THEN** the system denies automatic execution and retains only a reviewable draft

### Requirement: Follow-up and reply outcomes update the timeline safely
The system SHALL append draft creation, edits, approvals, denials, send attempts, receipts, reconciliation results, and reminder actions to the application timeline. A send receipt SHALL be required before a system-managed reply is recorded as sent.

#### Scenario: Reply is confirmed sent
- **WHEN** the provider receipt is reconciled successfully
- **THEN** the system appends a sent event with provider identifiers, payload hash, and follow-up consequence

#### Scenario: Reply result is ambiguous
- **WHEN** provider outcome cannot be determined
- **THEN** the system marks the reply reconciliation-required, disables blind retry, and presents a user task without claiming that the reply was sent
