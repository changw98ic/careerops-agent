## ADDED Requirements

### Requirement: Deterministic eligibility precedes semantic matching
The job-matching workflow SHALL run active-profile hard filters, source-state checks, and evidence-constrained requirement matching before any LLM ranking. A job denied by a hard constraint SHALL NOT become recommended because of a model score.

#### Scenario: Job passes deterministic gates
- **WHEN** a persisted job satisfies the active profile's hard constraints and has a valid evidence context
- **THEN** the system may create a bounded semantic-matching Agent run for that job

#### Scenario: Job fails a hard gate
- **WHEN** a job violates location, authorization, source, compensation, role, or explicit-exclusion rules
- **THEN** the system records the deterministic reason and does not send the job to the LLM matching path

### Requirement: Matching output is explainable and evidence-bound
The matching Agent SHALL return a structured score/tier, requirement coverage, gaps, transferable skills, seniority fit, remote compatibility when evidenced, recommendation, confidence, job evidence references, candidate evidence references, model/rules versions, and review-only status. It SHALL not create candidate evidence or transition an application.

#### Scenario: Model produces a match
- **WHEN** the model returns schema-valid output for an eligible job
- **THEN** the inbox and job detail show the bounded recommendation with evidence links, confidence, model version, and an explicit advisory label

#### Scenario: Model claims unsupported fit
- **WHEN** the model asserts a skill or eligibility fact with no job or candidate evidence
- **THEN** the system marks the field unknown/unsupported, excludes it from trusted policy facts, and presents the result for review

### Requirement: Matching is available from the page workflow
The page/API SHALL allow the user to request semantic matching for one job or a bounded eligible batch and SHALL expose pending, running, succeeded, unavailable, failed, abstained, stale, and reviewed states. Repeated requests for unchanged inputs SHALL be idempotent.

#### Scenario: User requests one-job matching
- **WHEN** the user requests matching for a recommended job with unchanged input hashes
- **THEN** the system returns or reuses one Agent run and displays its state without blocking the page request

#### Scenario: User requests a bounded batch
- **WHEN** the user requests matching for a batch within the configured job and token limits
- **THEN** the system creates a bounded batch run, reports progress, and does not invoke the provider for excluded jobs

### Requirement: Matching degrades explicitly when the model is unavailable
The system SHALL preserve deterministic filter and requirement-match results when the model is disabled, unavailable, rate-limited, or invalid. The UI SHALL distinguish `semantic_unavailable` or `abstained` from a genuine weak match.

#### Scenario: Model is disabled
- **WHEN** model tailoring is not released
- **THEN** the inbox remains usable with deterministic results and shows that semantic ranking was not performed

#### Scenario: Model call fails after deterministic evaluation
- **WHEN** a matching call fails or exceeds its budget
- **THEN** the system stores the failure class, keeps the deterministic result, and offers a safe retry without duplicating the run

### Requirement: User corrections are durable matching evidence
The system SHALL let the user correct a match recommendation or its evidence interpretation. Corrections SHALL be stored with actor, time, input/result identity, and correction reason, and SHALL remain distinguishable from model output.

#### Scenario: User downgrades a recommendation
- **WHEN** the user changes a model recommendation from apply to skip and supplies or selects a reason
- **THEN** the system records the correction and updates the page projection without changing the model result or candidate evidence

#### Scenario: Matching input becomes stale
- **WHEN** the job, profile, or confirmed evidence version changes
- **THEN** the prior semantic result is marked stale and cannot drive a new application action without regeneration or explicit review
