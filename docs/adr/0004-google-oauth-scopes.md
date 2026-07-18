# ADR 0004: Use BYO Google OAuth with staged scopes

- Status: Accepted
- Date: 2026-07-17

## Context

Gmail read scopes expose more than an application-level label, OAuth Testing refresh tokens are unsuitable for a long-running Watch integration, and a shared OAuth client would turn a single-user tool into a distributed data service.

## Decision

MVP supports only single-user self-hosting with a user-created Google Cloud project and OAuth web client. The repository, image and release artifacts contain no default Google client ID or secret. Production configuration requires operator attestations `DEPLOYMENT_MODE=single_user_byo` and `GOOGLE_OAUTH_PUBLISHING_STATUS=in_production`, followed by M7 evidence from the OAuth Console.

The account must be a dedicated job-search Gmail account. Main-mailbox label mode is not implemented. The app is External/In production personal-use; Testing may be used for short development only and cannot supply long-running Watch or release evidence.

Scopes are incremental:

| Stage | Capability | Scope boundary |
| --- | --- | --- |
| M4 | Gmail synchronization | `gmail.readonly` only |
| M5B-read | Availability | `calendar.freebusy` |
| M5B-write | Dedicated owned event | narrow owned-event scope selected and sandbox-verified |
| M6 | Send approved reply | `gmail.send` |

`gmail.compose` is never requested in MVP. Possessing a scope does not authorize an action; ADR 0003 remains authoritative.

Gmail Watch uses Pub/Sub Pull/StreamingPull, daily renewal, `history.list` and periodic reconciliation. No public webhook route is shipped. A future push route requires a separate ADR covering audience/identity, replay and DoS.

Refresh tokens use AES-256-GCM envelope encryption. Rows store ciphertext, nonce, key ID and scope inventory. The master key comes from an OS keychain or read-only mounted secret file and is excluded from the database, image, ordinary environment variables, logs and normal backups. API/UI/model components receive only opaque credential references.

Revocation deletes the credential, stops Watch and disables dependent workers. Scope expansion invalidates relevant qualification.

## Distribution trigger

Shipping a shared OAuth client, serving other users, exceeding personal-use limits or centrally storing other users' restricted-scope data requires OAuth verification and any applicable restricted-scope security assessment before release.

## Verification

- Startup rejects missing BYO/In production attestations outside development.
- Secret scans find no bundled client secret or plaintext token.
- Duplicate/drop/history-expiration/revoke tests pass.
- Release evidence includes a refresh/Watch observation across the seven-day Testing boundary.

References: [OAuth 2.0](https://developers.google.com/identity/protocols/oauth2), [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Gmail Watch](https://developers.google.com/workspace/gmail/api/guides/push), [personal-use verification](https://support.google.com/cloud/answer/13464323?hl=en), [User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).
