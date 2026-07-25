## ADDED Requirements

### Requirement: User can register and control trusted job sources
The system SHALL allow the user to create, edit, pause, resume, and remove crawl sources. A source SHALL include a source type, company or organization identity when known, canonical URL or board identifier, trust status, terms/robots status, enabled state, and source-specific adapter configuration. Unsupported source types SHALL remain disabled until an adapter and policy contract exist.

#### Scenario: User adds an official ATS source
- **WHEN** the user registers a valid Greenhouse, Lever, Ashby, or official company careers source
- **THEN** the system validates the source identity and URL, creates the source in enabled or explicitly paused state, and displays its policy status

#### Scenario: User pauses a source
- **WHEN** the user pauses an enabled source
- **THEN** the system prevents new runs from starting for that source while retaining prior postings, evidence, and run history

#### Scenario: Unsupported source is submitted
- **WHEN** the user submits a source type without a registered adapter or policy
- **THEN** the system refuses activation and explains that the source is not supported

### Requirement: User can define a versioned crawl plan
The system SHALL allow the user to define a crawl plan containing source selection, job themes, include/exclude keywords, role families, locations, remote rules, seniority, compensation bounds, content scope, per-run limits, and a schedule with timezone. Each change SHALL create a new plan version and SHALL NOT alter the interpretation of prior crawl runs.

#### Scenario: User creates a scheduled plan
- **WHEN** the user selects enabled sources, a valid interval, a timezone, and a content scope
- **THEN** the system creates an active plan version with a computed next-run time and displays the effective filters

#### Scenario: User changes plan scope
- **WHEN** the user changes the role keywords or location range of an active plan
- **THEN** the system creates a new plan version, preserves the old version for provenance, and applies the new version only to future runs

#### Scenario: Schedule is invalid
- **WHEN** the user supplies an interval, time window, or timezone that cannot be evaluated safely
- **THEN** the system rejects the plan and does not schedule a run

### Requirement: Crawl runs are explicit, bounded, and idempotent
The system SHALL support manual and scheduled runs. Every run SHALL bind to one plan version, source set, run identifier, start/end time, limits, result counters, and terminal status. Replaying the same run identifier or source observation SHALL not create duplicate postings or duplicate content versions.

#### Scenario: User runs a plan manually
- **WHEN** the user requests an immediate run for an active plan
- **THEN** the system creates a run record, applies the plan snapshot, and reports progress and terminal results independently of the browser request

#### Scenario: Scheduled run overlaps an existing run
- **WHEN** a scheduled trigger fires while the same source and plan version are already running
- **THEN** the system coalesces or skips the duplicate trigger and records the reason without starting an unbounded second crawl

#### Scenario: Crawl run is retried
- **WHEN** a worker retries an activity after a timeout or restart
- **THEN** the system reuses the run/source idempotency identity and does not duplicate source postings or evidence records

### Requirement: Crawl policy fails closed on unsafe or disallowed access
The system SHALL enforce allowed schemes, SSRF protection, DNS/redirect checks, terms and robots policy, per-domain rate limits, concurrency limits, response limits, and stop/backoff behavior. It MUST NOT bypass login walls, CAPTCHA, Cloudflare controls, robots restrictions, or explicit source blocks. Unknown policy states SHALL deny or limit access according to a documented safe fallback.

#### Scenario: Source terms are blocked
- **WHEN** a run evaluates a source whose terms or robots status is explicitly blocked
- **THEN** the system denies the fetch, records the policy decision, and continues other independent sources without bypassing the block

#### Scenario: URL resolves to a private or metadata address
- **WHEN** a URL, redirect, or resolved connection target is private, loopback, link-local, reserved, or a cloud metadata endpoint
- **THEN** the system denies the request before external content is accepted and records a non-sensitive denial reason

#### Scenario: Domain is rate limited
- **WHEN** a source returns rate-limit signals or reaches its configured budget
- **THEN** the system stops or backs off that domain, records the next eligible time, and does not fail open by increasing concurrency

### Requirement: User can inspect crawl operations and outcomes
The system SHALL provide a source/plan view showing enabled state, policy status, schedule, last and next run, counts of discovered/updated/closed/failed postings, failure reasons, and a way to inspect run evidence. Operational failures SHALL be actionable without exposing credentials or raw sensitive content.

#### Scenario: User inspects a failed run
- **WHEN** a crawl run ends with source or parser failures
- **THEN** the UI shows the affected source, safe error category, retry/backoff state, and a supported next action

#### Scenario: User inspects a successful run
- **WHEN** a crawl run completes successfully
- **THEN** the UI shows the plan version, source count, new/updated/closed counts, and links to the resulting job inbox items
