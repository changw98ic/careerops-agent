## ADDED Requirements

### Requirement: Tier 1 broadly scans structured and discovered job sources
The system SHALL run a broad Tier 1 sweep across many configured and discovered sources. Tier 1 SHALL use the existing ATS, JsonLd, and Sitemap extractors when structured job data is available and SHALL send every extracted posting through the existing `ingest_posting` contract so canonical validation, deduplication, provenance, and inbox projection are reused.

#### Scenario: Structured source is ingested by Tier 1
- **WHEN** a source exposes valid ATS, JsonLd, or Sitemap job data
- **THEN** Tier 1 extracts and ingests the postings without invoking Tier 2

#### Scenario: Newly discovered source enters the broad sweep
- **WHEN** Tier 1 discovers a source with job-source evidence
- **THEN** the system records the source and schedules it for subsequent crawl attempts under a source-specific crawl budget

#### Scenario: Broad sweep processes a large source queue
- **WHEN** the Tier 1 queue contains at least 100 eligible source records
- **THEN** the system schedules every source independently and records an outcome for each source without requiring a source-specific parser

### Requirement: Every Tier 1 attempt records a reasoned outcome
Each Tier 1 source attempt SHALL finish with exactly one of these outcomes: `POSTINGS_FOUND`, `VERIFIED_EMPTY`, `NOT_JOB_SOURCE`, `TRANSIENT_FAILURE`, `AUTH_REQUIRED`, `DYNAMIC_OR_UNSUPPORTED`, or `POLICY_DENIED`. An empty extraction result alone SHALL NOT cause escalation to Tier 2.

#### Scenario: Source currently has no open postings
- **WHEN** the fetched source is confirmed to be a job source and explicitly indicates that no positions are open
- **THEN** Tier 1 records `VERIFIED_EMPTY` and schedules a later Tier 1 crawl without escalating

#### Scenario: Source is not a job source
- **WHEN** the fetched content has no job-source evidence and is classified as unrelated content
- **THEN** Tier 1 records `NOT_JOB_SOURCE` and does not escalate

#### Scenario: Source has a temporary technical failure
- **WHEN** a source attempt fails because of a timeout, DNS or TLS error, HTTP 429, or HTTP 5xx response
- **THEN** Tier 1 records `TRANSIENT_FAILURE`, applies bounded retry and cooldown, and does not use a logged-in session

#### Scenario: Source policy forbids crawling
- **WHEN** source policy, user policy, or an explicit crawl restriction disallows access
- **THEN** Tier 1 records `POLICY_DENIED` and neither Tier 1 nor Tier 2 continues

### Requirement: Tier 2 eligibility requires positive job-source and failure evidence
A source SHALL enter Tier 2 only when it has positive job-source evidence, the Tier 1 outcome is `AUTH_REQUIRED` or `DYNAMIC_OR_UNSUPPORTED`, source policy permits the intended access, and a finite per-source crawl budget is available. Positive job-source evidence SHALL come from a prior successful posting ingest, ATS or JsonLd job markup, job-listing links or page signals, a trusted recruitment-domain registry, or explicit user configuration. HTTP 403, CAPTCHA, an empty result, or model judgement alone SHALL NOT establish `AUTH_REQUIRED`.

#### Scenario: Public dynamic job source uses Tier 2 without login
- **WHEN** a confirmed job source returns `DYNAMIC_OR_UNSUPPORTED` and its content is publicly accessible
- **THEN** Tier 2 may render and navigate the public source without using the user's authenticated session

#### Scenario: Unconfirmed source is not escalated
- **WHEN** Tier 1 cannot extract postings but positive job-source evidence is absent
- **THEN** the source remains outside Tier 2 and is recorded for later public re-evaluation

#### Scenario: CAPTCHA is encountered
- **WHEN** either tier encounters a CAPTCHA or account-risk challenge
- **THEN** the crawl stops for that source and does not attempt to bypass the challenge or treat it as proof that login permission is required

### Requirement: Logged-in crawling requires source-specific user permission
When Tier 1 confirms that a job source requires authentication, the system SHALL pause that source and create one pending permission request that identifies the source, the evidence that login is required, the read-only job-discovery purpose, the requested crawl frequency, and the execution limits. Tier 2 SHALL NOT access or reuse the user's authenticated session for that source until the user grants permission.

#### Scenario: Login requirement triggers a permission request
- **WHEN** a confirmed job source redirects job content to login, presents a login wall over the job content, explicitly states that login is required, or returns an authentication-required response from its job API
- **THEN** the system records `AUTH_REQUIRED`, pauses the source, and creates one source-specific permission request

#### Scenario: User grants permission
- **WHEN** the user grants the pending permission request
- **THEN** Tier 2 may reuse the authenticated session only for the approved source and read-only job discovery within the disclosed limits

#### Scenario: Permission is absent or withdrawn
- **WHEN** permission is pending, denied, expired, or revoked
- **THEN** Tier 2 does not use an authenticated session and the source remains paused

#### Scenario: Session is absent or expired
- **WHEN** permission exists but no valid session is available
- **THEN** the system asks the user to complete login directly and does not collect or store the user's password

#### Scenario: Repeated crawl sees an existing pending request
- **WHEN** the same source still has an unresolved permission request
- **THEN** the system reuses the existing request instead of creating repeated prompts

### Requirement: Tier 2 execution has measurable limits
Each Tier 2 source run SHALL stop after 30 browser actions or 5 minutes, whichever occurs first, unless a stricter source policy applies. It SHALL also stop after three consecutive result pages produce no new canonical posting identity. Per-source scheduling SHALL enforce a configured minimum interval and cooldown. The scheduler SHALL also enforce finite configured limits for concurrent Tier 2 runs and total Tier 2 browser actions per day. Model response latency SHALL NOT be treated as rate limiting or anti-detection control.

#### Scenario: Step or time budget is exhausted
- **WHEN** a Tier 2 run reaches 30 browser actions or 5 minutes
- **THEN** it stops, records the budget-exhausted outcome, and preserves progress for a later scheduled run

#### Scenario: Pagination stops producing new postings
- **WHEN** three consecutive result pages contain no posting with a new canonical identity
- **THEN** Tier 2 stops pagination for that run

#### Scenario: Global Tier 2 budget is exhausted
- **WHEN** the configured concurrency limit or daily browser-action budget has been reached
- **THEN** the scheduler leaves additional eligible sources queued and does not start another Tier 2 run until capacity is available

### Requirement: LLM extraction is schema-bound and fails closed
The LLM extraction adapter SHALL convert rendered job content into the canonical schema required by `ingest_posting`. Every required value SHALL be supported by the rendered source content; unknown optional values SHALL remain empty rather than be invented. The adapter SHALL validate output before ingestion, permit at most one schema-repair attempt, and fail closed without ingesting a posting when validation still fails.

#### Scenario: Valid extracted posting is ingested
- **WHEN** LLM output conforms to the canonical posting schema and its required values are supported by the rendered source
- **THEN** the posting is ingested with source URL and `llm-extraction` provenance

#### Scenario: Invalid output is repaired once
- **WHEN** the first LLM output fails canonical schema validation
- **THEN** the adapter performs at most one repair attempt using the validation errors

#### Scenario: Invalid output remains invalid
- **WHEN** the repair attempt still fails validation or required values lack source support
- **THEN** the adapter records the extraction failure and does not call `ingest_posting`
