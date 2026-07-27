## ADDED Requirements

### Requirement: A crawl source selects an authorized execution mode
Each enabled crawl source SHALL declare whether it uses the guarded HTTP executor or the bounded Ego browser executor. The system SHALL reject activation when the selected executor, adapter, source policy, or required browser capability is unavailable.

#### Scenario: HTTP source is enabled
- **WHEN** a user enables a source with a registered HTTP adapter and an allowed policy state
- **THEN** the crawl plan accepts the source and the run uses the guarded HTTP executor

#### Scenario: Browser source is unavailable
- **WHEN** a user selects Ego for a source but the browser capability is unavailable or not ready
- **THEN** the system refuses activation or marks the source unavailable with a safe reason and does not silently switch execution modes

### Requirement: Ego execution is isolated, bounded, and treated as untrusted input
The browser executor SHALL run only inside a worker or explicitly managed browser bridge, enforce URL/policy/timeout/response/concurrency limits, and return bounded page content plus provenance. Browser output SHALL be untrusted content and SHALL not expose browser credentials, tools, or application mutation access.

#### Scenario: JavaScript career page is crawled
- **WHEN** an enabled source permits browser execution and the worker opens its configured URL through Ego
- **THEN** the executor returns bounded normalized content, final URL, capture time, response identity, and parser version without returning session material

#### Scenario: Browser page contains an instruction to act
- **WHEN** a page asks the browser or Agent to reveal secrets, change a target, bypass review, or perform an external action
- **THEN** the instruction is retained only as untrusted source content or omitted by the parser and cannot invoke tools, change policy, or create an external side effect

#### Scenario: Browser operation exceeds its budget
- **WHEN** a browser navigation, scroll, response, or source concurrency budget is exceeded
- **THEN** the source stops with a bounded error/backoff outcome and the run remains safe for independent sources

### Requirement: A manual crawl request starts durable execution
The authenticated run-now command SHALL create or reuse an idempotent run identity and start the corresponding Temporal workflow without waiting for crawl completion. The workflow SHALL execute the bound plan-version snapshot and report queued, running, and terminal states through the existing run record.

#### Scenario: User starts a crawl from the page
- **WHEN** the user requests run-now for an active plan with eligible sources
- **THEN** the API creates or reuses one pending run and starts the workflow with the server-resolved owner and run ID

#### Scenario: User repeats run-now
- **WHEN** the same plan/source set already has a pending or running run
- **THEN** the API returns the existing run identity and does not start a duplicate workflow

#### Scenario: Temporal worker is unavailable
- **WHEN** the run record is created but no worker accepts the workflow
- **THEN** the page shows a queued/dependency state and never reports the run as completed or failed solely because the request returned

### Requirement: Crawled postings are normalized and projected into the inbox
Every successful source observation SHALL be normalized through the existing posting contract, persisted idempotently with source/run/plan/parser/capture/content provenance, and made available to the inbox projection only after durable persistence.

#### Scenario: New browser posting is discovered
- **WHEN** the browser executor returns a valid posting with a new source identity or content hash
- **THEN** the system stores the posting/version with provenance and creates or updates the corresponding canonical job/inbox projection

#### Scenario: A crawl is retried
- **WHEN** a worker retries a source or resumes a run with the same run/source/external identity and content hash
- **THEN** the system creates no duplicate posting, version, inbox decision, or evidence record

#### Scenario: One source fails while others succeed
- **WHEN** a source fails due to policy, browser, network, or parser conditions during a multi-source run
- **THEN** the run records a bounded source failure/backoff and continues permitted independent sources without marking the failed source as successful
