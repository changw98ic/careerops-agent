## ADDED Requirements

### Requirement: User can create a job-specific interview preparation run
The system SHALL allow an authenticated user to create an interview-preparation Agent run for a selected job version, confirmed resume/evidence set, active profile version, and optional user-provided interview context. The run SHALL capture immutable input identities and a bounded preparation budget.

#### Scenario: User starts preparation
- **WHEN** the user selects a job and an eligible confirmed resume/evidence set and requests preparation
- **THEN** the system creates an idempotent pending Agent run and does not send an external message or schedule an interview

#### Scenario: Required input is missing
- **WHEN** the job, resume, ownership, or confirmed evidence context is missing or stale
- **THEN** the system rejects or marks the run unavailable without model egress and explains the missing input

### Requirement: Preparation output contains structured, bounded coaching material
The interview-preparation Agent SHALL produce a structured preparation pack containing job-specific focus areas, likely question categories, bounded technical/behavioral questions, evidence-linked answer prompts, STAR story prompts, uncertainty/gap notes, and a review-only status. It SHALL not claim that an interview is scheduled or invent candidate history.

#### Scenario: Preparation succeeds
- **WHEN** the model returns schema-valid output from the selected job and confirmed evidence
- **THEN** the page displays the preparation pack with job evidence, candidate evidence, model/schema versions, and review status

#### Scenario: No evidence supports an answer
- **WHEN** a suggested answer would require an unsupported achievement, metric, employer fact, or authorization claim
- **THEN** the system marks the prompt as needing user input or unknown and does not present the claim as a candidate fact

### Requirement: User can edit and save preparation material as a versioned draft
The page/API SHALL allow the user to edit, accept, reject, and annotate preparation questions and STAR prompts. User-edited content SHALL be stored as a new version linked to the source Agent result and SHALL not alter the original model output or resume evidence.

#### Scenario: User adds a STAR story
- **WHEN** the user supplies a personal situation/action/result for a generated STAR prompt
- **THEN** the system stores it as user-authored preparation content with the selected evidence links and correction metadata

#### Scenario: User rejects a question
- **WHEN** the user rejects a generated question or marks it irrelevant
- **THEN** the system records the decision and removes it from the active preparation view without deleting the original result

### Requirement: Preparation results are stale-aware and retryable
The system SHALL mark a preparation pack stale when the selected job, profile, resume, evidence, or user context changes. Regeneration SHALL create a new version and preserve the prior result and review history.

#### Scenario: Interview preparation input changes
- **WHEN** the target job receives a new version or the user changes the selected resume/evidence
- **THEN** the existing pack is marked stale and the page requires regeneration or explicit acknowledgement before presenting it as current

#### Scenario: Provider is unavailable
- **WHEN** the model provider is disabled or unavailable
- **THEN** the page offers a deterministic preparation outline from job requirements and confirmed evidence, labels the missing semantic generation, and allows a later retry
