## ADDED Requirements

### Requirement: A crawl schedule self-drives on a per-source interval
The system SHALL register a Temporal Schedule that starts the existing `CrawlScheduledWorkflow` on each source's configured interval, consuming the previously-dead `interval_seconds` field. Public structured sources SHALL crawl roughly hourly; anti-bot sources SHALL crawl on a slower, human-like cadence (2–4 hours). The system SHALL advance crawl without a user click.

#### Scenario: Public source crawls hourly
- **WHEN** a public structured source has an ~1h interval
- **THEN** a Temporal Schedule starts `CrawlScheduledWorkflow` on that cadence

#### Scenario: Anti-bot source crawls slowly
- **WHEN** an anti-bot source has a 2–4h interval
- **THEN** it is crawled on the slower cadence to lower detection risk

### Requirement: The Gmail send token is refreshed on two layers
The system SHALL refresh the Gmail access token on two layers: (1) a scheduled refresh roughly hourly, and (2) a just-before-send check that refreshes if the token is near expiry. Both layers SHALL share one refresh routine and one token store; the send-time layer SHALL skip refresh if one just occurred, to avoid redundant calls.

#### Scenario: Token is refreshed on schedule
- **WHEN** the hourly refresh schedule fires
- **THEN** the access token is renewed via `refresh_access_token` and stored in the shared token store

#### Scenario: Token is refreshed just before send
- **WHEN** a send is about to execute and the token is near expiry
- **THEN** the send-time layer refreshes the token (or skips if recently refreshed) before sending

### Requirement: An inbound-mail sync schedule self-drives once reading is wired
Once the Gmail read path is wired (see real-provider-integration), the system SHALL register a Temporal Schedule that pulls new inbound mail on a ~5–10 minute polling cadence, so the loop closes (send out, read replies back) without manual action.

#### Scenario: Inbound mail is polled
- **WHEN** the mail-sync schedule fires
- **THEN** new mail is fetched incrementally and handed to `MailSyncService`

### Requirement: Triggered work is durable and resumable
Each scheduled workflow SHALL be durable and resumable across worker restarts. A worker restart SHALL NOT lose in-flight scheduled work or duplicate already-completed side effects.

#### Scenario: Worker restarts mid-run
- **WHEN** a worker restarts while a scheduled workflow is in flight
- **THEN** the workflow resumes from its last durable state without re-executing completed side effects

### Requirement: Crawl completion chains into downstream stages
Upon successful crawl completion, the system SHALL trigger downstream stages (matching and inbox projection) so the loop progresses end-to-end without a manual step between crawl and match.

#### Scenario: New postings flow into matching
- **WHEN** a crawl run completes with new postings
- **THEN** matching and inbox projection are triggered without waiting for a user action
