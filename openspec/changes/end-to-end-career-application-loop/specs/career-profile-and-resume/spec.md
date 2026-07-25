## ADDED Requirements

### Requirement: User has a versioned career profile and search preferences
The system SHALL allow the authenticated user to maintain one active career profile containing target roles, seniority, locations, remote/work authorization constraints, compensation preferences, preferred industries or company types, include keywords, exclude keywords, and explicit hard exclusions. Every saved profile SHALL have a version, updated timestamp, and stable identity so that matching results can be traced to the exact preference version that produced them.

#### Scenario: User saves valid search preferences
- **WHEN** the user submits a profile with at least one target role and valid location/remote constraints
- **THEN** the system stores a new immutable profile version, marks it active, and uses it for subsequent crawl filtering and matching

#### Scenario: Invalid preference cannot become active
- **WHEN** the user submits contradictory constraints, such as an excluded location being the only required location or an invalid compensation range
- **THEN** the system rejects the update with field-level errors and keeps the previous active profile unchanged

#### Scenario: Matching result records profile provenance
- **WHEN** a job is filtered or matched using the active profile
- **THEN** the result records the profile version identifier and rules version used for the decision

### Requirement: Resume files are immutable, selectable versions
The system SHALL allow the user to register resume versions without overwriting a prior version. Each version SHALL retain a content hash, source/reference, target type, creation time, parse status, and whether the user has confirmed the extracted content. A resume version with failed parsing or missing confirmation SHALL not be eligible for an application package.

#### Scenario: User registers a new resume
- **WHEN** the user uploads a supported resume file with a valid size and media type
- **THEN** the system stores a content-addressed reference, creates a new resume version, and starts deterministic parsing without modifying existing versions

#### Scenario: Identical resume is registered twice
- **WHEN** the user registers content whose hash already exists for the same candidate
- **THEN** the system returns the existing version or creates an explicitly linked duplicate record without storing a second physical blob

#### Scenario: Unconfirmed resume cannot be submitted
- **WHEN** the user tries to create an application package from a resume whose extraction is incomplete or unconfirmed
- **THEN** the system blocks package approval and explains which confirmation is required

### Requirement: Candidate evidence is explicit and traceable
The system SHALL represent skills, experience, achievements, education, authorization, and other positive candidate claims as evidence items linked to a user-selected source and bounded source span. Model-produced interpretations SHALL be proposals and SHALL NOT create trusted evidence or change user facts without user confirmation.

#### Scenario: Evidence is imported from a resume
- **WHEN** deterministic parsing extracts a candidate claim from a confirmed resume version
- **THEN** the system stores the claim with the resume version, source span, extractor version, and an unconfirmed or confirmed status

#### Scenario: Model proposes an unsupported claim
- **WHEN** a model suggests a skill or achievement that has no linked candidate evidence
- **THEN** the system marks it unsupported, excludes it from trusted application claims, and presents it for user review rather than saving it as fact

#### Scenario: User confirms or rejects a claim
- **WHEN** the user confirms or rejects an evidence proposal
- **THEN** the system records the actor, timestamp, source reference, and decision without rewriting the original resume content

### Requirement: Application packages are job-specific, evidence-bound drafts
The system SHALL create a separate application package for a specific application, job version, and resume version. A package MAY contain an ordered resume presentation, cover letter, application answers, and email body, but every positive claim SHALL reference candidate evidence. Generated or suggested changes SHALL be shown as a diff from the source material and SHALL require user confirmation before the package becomes approved.

#### Scenario: User requests job-specific tailoring
- **WHEN** the user selects a job and a confirmed resume version and requests tailoring
- **THEN** the system creates a draft package containing matched requirements, gaps, proposed edits, evidence references, and the exact job/profile/resume input identities

#### Scenario: Tailoring changes only presentation
- **WHEN** the tailoring service proposes reordering or rewriting a supported bullet
- **THEN** the system preserves the source fact, shows the proposed text and evidence reference, and does not silently mutate the base resume version

#### Scenario: Package contains an unsupported claim
- **WHEN** a package contains a claim without a valid candidate evidence reference
- **THEN** the system marks the package invalid and prevents approval or delivery

#### Scenario: User approves a package
- **WHEN** the user reviews the diff, evidence references, attachments, and target job and explicitly approves the package
- **THEN** the system freezes the package payload hash and records the approving user and approval time

### Requirement: Sensitive resume processing follows model and retention boundaries
The system SHALL keep model use optional and default-disabled. When a model is enabled, only the minimum job text and user-selected candidate evidence needed for the capability MAY leave the local boundary; raw resume files, credentials, unrelated private material, and unselected mailbox content MUST NOT be sent. Raw prompts and responses MUST remain disabled by default and follow bounded retention when explicitly enabled.

#### Scenario: Model provider is disabled
- **WHEN** the configured model provider is disabled
- **THEN** deterministic parsing, evidence management, filtering, and package validation remain usable and no model request is made

#### Scenario: Tailoring request contains unrelated private material
- **WHEN** a tailoring request would include unselected private content or a raw credential-bearing artifact
- **THEN** the system removes or rejects that content before model egress and records a denied-egress reason
