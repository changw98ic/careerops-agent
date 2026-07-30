## ADDED Requirements

### Requirement: A real model provider is active by default for the single user
The system SHALL configure a real, Anthropic-compatible model provider by default for the single-user runtime, replacing the disabled default. The model gateway SHALL remain the single egress point and all LLM call sites SHALL route through `StructuredModelClient`.

#### Scenario: Out-of-the-box model availability
- **WHEN** the single-user runtime starts with provider credentials present
- **THEN** the model client reports enabled and agent services (intake, matching, resume review, extraction) do not return `unavailable` due to a disabled provider

#### Scenario: Provider configuration is absent
- **WHEN** no provider credentials are configured at startup
- **THEN** the runtime fails fast with an actionable error rather than silently using the disabled adapter

### Requirement: Real Gmail send is wired into the runtime
The system SHALL inject the real Gmail side-effect provider (send) into the runtime, replacing the fake provider, whenever auto-send and external-writes are enabled and a usable send token exists. Provider selection SHALL NOT depend on a dead `google_oauth_enabled` flag.

#### Scenario: Application send reaches a real Gmail account
- **WHEN** a send is executed through the side-effect chain
- **THEN** the message is delivered through the real Gmail provider and a provider receipt is recorded

#### Scenario: Send token is refreshed at startup
- **WHEN** the runtime builds the send provider
- **THEN** it refreshes the access token from the stored refresh token before constructing the sender

### Requirement: Real Gmail read pulls inbound mail into the sync path
The system SHALL provide a real Gmail read component that fetches new inbound mail incrementally via the Gmail history list, resuming from the stored history-id cursor, and hands fetched messages to `MailSyncService.run_sync_step`. Because `MailSyncService` performs no provider network calls, the read component is the first hop that connects Gmail to mail intelligence.

#### Scenario: New inbound mail is fetched incrementally
- **WHEN** the mail-sync trigger runs
- **THEN** the read component fetches only new messages since the last cursor and hands them to `MailSyncService`

#### Scenario: Read uses the granted gmail.readonly scope
- **WHEN** the read component authenticates
- **THEN** it uses the read scope already granted alongside send, without a separate authorization

### Requirement: Capability resolver releases read, send, and external writes for the single user
The capability resolver SHALL treat Gmail read, Gmail send, and external writes as released for the single user rather than default-denied.

#### Scenario: External write path is usable
- **WHEN** a capability decision is evaluated for a released external-effect path with its dependencies ready
- **THEN** the resolver returns released instead of denying as `CAPABILITY_NOT_RELEASED`
