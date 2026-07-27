## ADDED Requirements

### Requirement: User can request a review for selected resume and job inputs
The system SHALL allow an authenticated user to start a resume-review Agent run for one immutable resume version, one immutable job version, an active profile version, and an explicitly selected evidence set. The run SHALL capture input identities/hashes before model execution.

#### Scenario: User starts resume review
- **WHEN** the user selects a parsed resume, a job, and confirmed evidence and requests review
- **THEN** the system creates an idempotent Agent run with the input versions, capability, schema, and pending state

#### Scenario: Resume is not eligible
- **WHEN** the selected resume is unparseable, unconfirmed, missing, or owned by another candidate
- **THEN** the system rejects the request without model egress and explains the required correction

### Requirement: Resume review is evidence-bound and review-only
The resume-review Agent SHALL identify strengths, gaps, ambiguities, and presentation suggestions, but every positive claim SHALL reference confirmed candidate evidence or be marked unsupported/unknown. The result SHALL never mutate the base resume, create trusted evidence, change application state, or approve a package.

#### Scenario: Model suggests a supported improvement
- **WHEN** the model proposes reordering or rewriting a resume bullet supported by confirmed evidence
- **THEN** the result includes the source evidence reference, before/after presentation diff, and review-only status

#### Scenario: Model invents an achievement
- **WHEN** the model proposes a skill, metric, employer fact, or achievement without confirmed evidence
- **THEN** the system marks it unsupported or omits it and blocks it from trusted package claims until the user supplies and confirms evidence

### Requirement: User can review and version resume suggestions
The page/API SHALL show resume-review findings, evidence links, uncertainty, and diffs. Accepting or editing a suggestion SHALL create a new proposal/package presentation version and preserve the immutable source resume and original Agent output.

#### Scenario: User accepts a suggestion
- **WHEN** the user accepts a supported presentation suggestion
- **THEN** the system records the decision and creates a new version linked to the same source resume and evidence without overwriting the source

#### Scenario: User edits a suggestion
- **WHEN** the user changes proposed wording or rejects a finding
- **THEN** the system records the user correction separately from model output and revalidates evidence before the result can enter an application package

### Requirement: Stale resume reviews cannot silently be reused
The system SHALL mark a resume-review result stale when its job version, profile version, resume version, or referenced evidence changes. A stale result SHALL require regeneration or explicit user review before package use.

#### Scenario: Job content changes after review
- **WHEN** the target job receives a new immutable version after a review completes
- **THEN** the result is marked stale and the page prevents it from being treated as current tailoring advice

#### Scenario: User retries a stale review
- **WHEN** the user requests regeneration for the same logical inputs
- **THEN** the system creates a new idempotent Agent run/version and preserves the prior result for audit and comparison
