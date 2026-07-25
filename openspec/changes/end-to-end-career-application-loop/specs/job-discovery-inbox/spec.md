## ADDED Requirements

### Requirement: Discovered postings retain source provenance and immutable versions
The system SHALL normalize supported source observations into source postings and user-visible canonical jobs. Each posting SHALL retain source identity, external identifier, canonical URL, company/source identity, captured time, content hash, parser version, crawl run, and raw-content reference subject to retention. A content change SHALL create a new immutable version rather than overwrite history.

#### Scenario: Same source posting is recrawled unchanged
- **WHEN** a source observation has the same source identity and content hash as the latest version
- **THEN** the system updates observation metadata idempotently without creating a duplicate posting or version

#### Scenario: Job content changes
- **WHEN** a known source posting has a new normalized content hash
- **THEN** the system creates a new posting version linked to the same posting and preserves the prior version and evidence references

#### Scenario: Source posting disappears once
- **WHEN** a previously seen posting is absent from one crawl
- **THEN** the system does not immediately close the canonical job and records the observation according to the adapter's closure policy

### Requirement: Deduplication is precision-first and reversible
The system SHALL deduplicate by source identity, canonical URL, verified company/title/location identity, and stable fingerprints before proposing semantic merges. Semantic similarity SHALL never silently merge jobs. Every merge or split decision SHALL retain algorithm version, evidence, actor, timestamp, and rollback identity.

#### Scenario: Two source postings have the same canonical URL
- **WHEN** two postings resolve to the same trusted canonical URL
- **THEN** the system associates them with one canonical job while preserving both source postings and their independent source states

#### Scenario: Semantic match is uncertain
- **WHEN** two postings are similar but do not satisfy deterministic identity rules
- **THEN** the system presents a merge proposal with evidence and leaves both jobs separate until a permitted reviewer decision

#### Scenario: User reverses a merge
- **WHEN** the user rejects or rolls back a previous merge decision
- **THEN** the system restores the prior canonical associations without deleting source history or audit evidence

### Requirement: The inbox applies hard filters before advisory ranking
The system SHALL apply user-configured hard constraints such as location, remote eligibility, work authorization, role family, compensation, source status, and explicit exclusions before ranking. A job failing a hard constraint SHALL not appear in the recommended set, but SHALL remain inspectable with the blocking reason when the user chooses to view excluded results.

#### Scenario: Job fails a hard location rule
- **WHEN** a posting's verified location is outside the active profile's allowed locations
- **THEN** the system excludes it from recommendations and shows the deterministic blocking rule in the excluded view

#### Scenario: Job has unknown remote eligibility
- **WHEN** a user requires remote eligibility and the job evidence is unknown
- **THEN** the system treats the job as not recommended, labels the uncertainty, and does not infer eligibility from a model score

#### Scenario: User changes preferences
- **WHEN** the user activates a new profile version
- **THEN** future inbox calculations use the new version while prior match results retain their original profile and rules provenance

### Requirement: Match results are explainable, advisory, and evidence-bound
The system SHALL provide requirement-level match results with match level, confidence, reason, candidate evidence references, job evidence references, rules/model versions, and review-only status. Model output SHALL not authorize an application, alter policy facts, or create candidate evidence directly.

#### Scenario: Requirement has supporting evidence
- **WHEN** a job requirement can be matched to confirmed candidate evidence
- **THEN** the result shows the requirement, supporting evidence, confidence, and explanation in the job detail and application workspace

#### Scenario: Model output is unavailable
- **WHEN** the configured model provider is disabled, unavailable, or returns invalid structured output
- **THEN** deterministic hard filters remain available and the system marks semantic matching unavailable rather than inventing a score

#### Scenario: Prompt injection appears in job content
- **WHEN** job content contains instructions attempting to change recipients, policy, or system behavior
- **THEN** the content is treated as untrusted evidence, cannot affect policy or tools, and is either ignored or shown as an injection warning

### Requirement: User can act on a job from the inbox
The system SHALL let the user inspect source evidence, favorite, ignore, snooze, create an application, or mark a job as manually handled. These actions SHALL be idempotent and SHALL create an application event or user decision record where applicable.

#### Scenario: User favorites a job
- **WHEN** the user favorites a recommended job
- **THEN** the system creates or reuses one application record in the initial favorite state and shows the next preparation action

#### Scenario: User ignores a job
- **WHEN** the user ignores a job with an optional reason
- **THEN** the system records the decision and removes the job from the active recommendation queue without deleting source evidence

#### Scenario: User opens a stale job
- **WHEN** all trusted postings for a canonical job are closed or the source evidence is expired
- **THEN** the UI clearly labels the job as stale/closed and prevents a new submission unless the user explicitly confirms a manual override
