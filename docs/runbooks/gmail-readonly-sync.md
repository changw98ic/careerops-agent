# Gmail read-only sync runbook

## Purpose and boundary

G011 defines a bounded Gmail read-only signal source for the job-search workflow. It is designed
for a dedicated BYO Gmail account and exact `https://www.googleapis.com/auth/gmail.readonly`
authority. It can discover recruiter/application signals and create reviewed proposals. It must
not send, compose, draft, modify, label, delete, or otherwise mutate Gmail.

This runbook is operational guidance for the intended path. It is not evidence that Gmail is
connected. The checked-in runtime is fail-closed by default: the CLI, worker, Gmail read-only
HTTP client, Unix credential-broker client, local broker host and owner-scoped DB repositories
exist, but they do not become a configured Gmail integration until BYO OAuth material, opaque
handle registration and real polling smoke have been installed and verified.

For host-side Desktop OAuth client import, loopback/PKCE authorization, in-process
Security.framework Keychain storage, broker socket ownership and opaque-handle registration, use
[Gmail OAuth broker runbook](gmail-oauth-broker.md). Chrome login alone is not a CareerOps
credential; browser sessions and plugin Gmail connectors must not substitute for the OAuth broker.

Official Google references:

- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [OAuth 2.0 Scopes for Google APIs](https://developers.google.com/identity/protocols/oauth2/scopes)
- [users.messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
- [users.messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)
- [users.history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)
- [Configure push notifications in Gmail API](https://developers.google.com/workspace/gmail/api/guides/push)

## Current implementation status

Implemented or specified:

- authenticated internal operator API contract for Gmail read-only accounts;
- dedicated-account, BYO, exact-scope request boundary;
- opaque credential-handle contract;
- Unix-domain credential-broker client protocol that requests only `gmail.readonly` envelopes;
- host-side Gmail broker CLI/server for local loopback OAuth, in-process macOS
  Security.framework Keychain storage and Unix-socket token envelopes;
- Gmail REST client restricted to `getProfile`, `messages.list`, `messages.get` in metadata
  format and `history.list`;
- polling mailbox worker, `careerops-gmail-readonly` CLI, Makefile status/sync targets and
  disabled Compose profile;
- owner-scoped PostgreSQL migration/repository/functions for accounts, sync runs, redacted
  metadata-only message signals, reviewed proposals, command receipts and revoke/reset actions;
- metadata classifier/redactor that persists hashes and redacted excerpts, not raw bodies;
- proposal review contract with `canonical_job_id` and `job_posting_id`;
- reset-history contract that queues recovery/full sync without accepting an arbitrary Gmail
  history id;
- default unavailable provider when Gmail is not configured.

Not implemented:

- browser-native Google consent is still operator-confirmed and not automated past the consent
  boundary;
- real Gmail API polling smoke against a user account;
- Watch/PubSub;
- send/compose/draft/modify/delete/label operations.

Current local evidence: the readonly grant for the configured dedicated Gmail account succeeded. Gmail read-only
sync is still not fully configured until the broker handle is registered in the application
control plane and a real bounded polling smoke has persisted only redacted/hash evidence. Do not
report Gmail as "configured" or "打通" until those runtime pieces have evidence.

## Required configuration evidence

Before enabling an account, collect and store evidence for:

- dedicated Gmail account for recruiting/job-search only;
- BYO OAuth client;
- Google OAuth publishing status: `testing` or `in_production`;
- exact granted scope: `https://www.googleapis.com/auth/gmail.readonly`;
- credential-store evidence SHA-256 as `credential_store_evidence_sha256`;
- opaque credential handle resolvable only through the local Unix broker;
- owner user id and candidate id binding.

Use a read-only broker profile that is separate from any Gmail send broker profile. Strict
deployments should use a separate Google Cloud project or OAuth client profile for this
`gmail.readonly` capability so read-only release evidence, revocation and audit state cannot be
confused with send authority. Import a Google Desktop OAuth client JSON into the host broker, move
the cleartext source JSON to the operator's Trash after successful import, run the local
loopback/PKCE authorization flow with `--account-subject`, and store client secrets plus refresh
tokens through in-process macOS Security.framework Keychain calls or an equivalent OS-backed
vault, never in this repository. The Keychain path must stay inside the broker process rather than
crossing a command-line boundary with client secrets or token JSON. The operator must explicitly
accept the Google consent screen for the expected account and exact `gmail.readonly` scope before
recording approval; URL generation, Chrome login or callback receipt alone is not consent
evidence. If the OAuth consent screen remains in Testing, treat the credential as short-lived
development evidence: Google documents that External Testing apps receive refresh tokens that
expire after 7 days from authorization/re-authorization unless only basic profile/OIDC scopes are
requested.

Never paste or store:

- OAuth client secret;
- refresh token;
- access token;
- Gmail password;
- broad `https://mail.google.com/` scope;
- `gmail.send`, `gmail.compose`, `gmail.modify`, `gmail.insert`, settings, SMTP, IMAP, or POP
  credentials.

## Operator API flow

### Register account

Register only through the authenticated internal API. Required request fields:

```json
{
  "command_id": "uuid",
  "candidate_id": "uuid",
  "credential_handle": "opaque-broker-handle",
  "account_subject": "jobs-only@example.com",
  "dedicated": true,
  "oauth_client_mode": "byo",
  "publishing_status": "testing",
  "credential_store_evidence_sha256": "64 lowercase hex"
}
```

The request must not contain `scope`, `token`, `client_secret`, `refresh_token`, `secret_handle`,
`label_ids`, `query`, `watch_topic_name`, or any send/compose/modify authority.

### Request sync

Manual sync queues a polling run:

```json
{
  "command_id": "uuid",
  "reason": "manual"
}
```

Allowed reasons are `manual`, `history_expired`, `scheduled`, and `recovery`. The worker resolves
the opaque handle through the Unix broker and uses Gmail read-only endpoints only.

### Review proposals

A proposal review requires:

```json
{
  "command_id": "uuid",
  "decision": "approve",
  "snapshot_sha256": "64 lowercase hex",
  "canonical_job_id": "uuid",
  "job_posting_id": "uuid",
  "reason": "matches tracked application"
}
```

Use `reject` when the signal is unrelated, ambiguous, unsafe, duplicated, or outside the
candidate/job context. Approval may make downstream reconciliation eligible; it does not send
email or mutate Gmail.

### Reset history

If incremental Gmail history is expired or Gmail returns the practical 404 recovery path for an
invalid/too-old `startHistoryId`, the account moves to `sync_required`. The operator then submits:

```json
{
  "command_id": "uuid",
  "snapshot_sha256": "64 lowercase hex",
  "reason": "gmail history expired; queue full sync"
}
```

The reset request must not accept or persist a caller-provided `provider_history_id`. Recovery
clears stale cursor state and queues a full polling sync.

### Revoke

Revocation records the account as revoked and blocks future sync:

```json
{
  "command_id": "uuid",
  "reason": "operator disconnected Gmail"
}
```

The application stores revocation evidence. The credential broker or external vault is
responsible for invalidating OAuth material because the application does not store refresh
tokens.

After application revocation, revoke or delete the matching broker-side OAuth material from
Keychain or the external vault. A revoked CareerOps account with an unreclaimed broker refresh
token is operationally incomplete even though CareerOps will refuse future sync.

## Worker behavior

In the default local stack, the worker is disabled because `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`:

```bash
make gmail-readonly-status
```

Expected JSON includes `"enabled":false` and `"disabled_reason":"GOOGLE_OAUTH_DISABLED"`.
Setting `CAREEROPS_GOOGLE_OAUTH_ENABLED=true` is not sufficient by itself; the mailbox role also
needs a real absolute broker socket, BYO OAuth material, an application-registered opaque handle,
a disposable or production-appropriate mailbox database login, and smoke evidence before any
account can be called configured.

The direct CLI equivalent is:

```bash
uv run careerops-gmail-readonly status --json
uv run careerops-gmail-readonly run-once --limit 1 --max-results 10 --json
```

The Makefile `gmail-readonly-status`, `gmail-readonly-sync` and `gmail-readonly-worker` targets
build `CAREEROPS_DATABASE_URL` through `careerops-database-url` using the mailbox login parts
from environment variables, and stop before worker launch if URL encoding fails. They then launch
the CLI through `env -i`. The final worker environment is limited to `PATH`,
`UV_PROJECT_ENVIRONMENT`, `CAREEROPS_ENVIRONMENT`, `CAREEROPS_CONSOLE_COOKIE_SECURE`, encoded
`CAREEROPS_DATABASE_URL`, the readonly broker socket and disabled-by-default gates/Google
production attestation. The long-running worker defaults to
`CAREEROPS_GMAIL_READONLY_POLL_SECONDS=5`, `CAREEROPS_GMAIL_READONLY_LIMIT=10` and
`CAREEROPS_GMAIL_READONLY_MAX_RESULTS=100`; override these variables for a tighter smoke pass.

The default broker socket is `/run/careerops-gmail/broker.sock`. The path must be absolute. The
deployment should own the parent directory with restrictive permissions such as mode `0700`, and
the socket should allow only the broker process and the mailbox runtime role to connect, for
example owner-only mode `0600` or an equivalent service-manager ACL. Do not mount the send broker
socket into the read-only worker.

Docker Compose does not create a Gmail broker, Security.framework Keychain/vault host or OAuth
credential material. Keep host broker services separate from Docker core workers: run the broker
on the host, or provide deployment-owned broker services and mount only the read-only socket into
the mailbox worker. A container with `careerops-gmail-readonly` available is not a configured
Gmail integration by itself.

For local development, the Makefile shares repo-local broker sockets between the host broker and
host worker commands:

```bash
make gmail-oauth-serve
make gmail-readonly-status
make gmail-readonly-sync
make gmail-readonly-worker
```

If `.env` is loaded through Make, keep JSON-like environment arrays unquoted, for example:

```dotenv
CAREEROPS_CONSOLE_ALLOWED_HOSTS=["127.0.0.1:8000","localhost:8000"]
CAREEROPS_CONSOLE_ALLOWED_ORIGINS=["http://127.0.0.1:8000","http://localhost:8000"]
```

Do not wrap those arrays in an extra shell string quote when Make exports the environment.

The read-only broker client sends this protocol request for each opaque handle:

```json
{
  "version": "careerops.gmail.credential-broker.v1",
  "provider": "gmail",
  "opaque_handle": "gmail:readonly:opaque-handle",
  "account_subject": "jobs-only@example.com",
  "scope": "https://www.googleapis.com/auth/gmail.readonly"
}
```

The broker must return a short-lived access-token envelope for the same account and exact scope.
It must not return refresh tokens, client secrets, client ids, token URIs or private keys.

Initial polling:

1. Call `users.messages.list`.
2. For selected message ids, call `users.messages.get`.
3. Classify recruiting relevance from metadata and snippet in memory; do not fetch raw bodies.
4. Persist only redacted excerpts, SHA-256 hashes, classification, confidence, provenance, and
   proposal payload hashes.
5. Queue proposals for relevant or ambiguous signals.

Incremental polling:

1. Read the stored history cursor.
2. Call `users.history.list`.
3. Fetch changed messages with `users.messages.get` when needed.
4. Persist new redacted/hash-only signals.
5. Advance cursor only after a complete successful page/run.

Recovery:

- Do not advance the cursor after partial pages, incomplete fetches, broker errors, Google API
  errors, or classification failures.
- On history expiry or 404 for an invalid/old cursor, mark the account `sync_required`.
- Wait for operator reset before full sync recovery.

## Data-handling rules

Allowed to persist:

- owner id, candidate id, account id, account subject;
- opaque credential handle and evidence hash;
- Gmail history cursor/page-state metadata;
- provider message/thread/history ids for provenance and deduplication;
- message/thread/sender/subject/snippet hashes;
- redacted excerpt;
- classification, relevance, confidence, provenance JSON;
- signal/proposal payload hashes;
- proposal review decision and selected job ids.

Not allowed to persist:

- raw access tokens, refresh tokens, client secrets, or passwords;
- raw email body;
- raw attachment content;
- raw sender, subject, or snippet; provider message/thread/history ids are the explicit minimum
  provenance exception;
- Gmail labels or query filters from an operator request;
- send/compose/modify request payloads.

## Role and owner-scope checks

Every API and repository method must bind authenticated `owner_user_id`. A valid account id alone
is insufficient.

Expected role split:

- API role: owner-scoped account summaries, command receipts, and proposal decisions only.
- Worker role: queued sync claiming, broker access, Gmail read-only calls, redacted/hash signal
  writes, proposal creation.
- Read-only role: reporting summaries without credential handles.

When investigating access bugs, check the owner id first. Cross-owner account, run, signal, or
proposal reads must return not found or unavailable without leaking the resource.

## Explicitly unsupported operations

Do not enable or document these read-only worker capabilities as available:

- Watch/PubSub, `users.watch`, Cloud Pub/Sub setup, push webhook, or watch renewal;
- production vault internals beyond the bounded opaque-handle protocol; host-side Desktop OAuth,
  Keychain storage and local broker operation are documented separately in
  [Gmail OAuth broker runbook](gmail-oauth-broker.md);
- Gmail send, drafts, compose, modify, trash/delete, labels, settings, SMTP, IMAP, POP;
- automated recruiter replies;
- application submission through Gmail.

If an operator asks whether Gmail is configured, answer from evidence. In the default local stack,
the correct answer is "unconfigured/unavailable."

## Verification checklist

Before claiming Gmail read-only sync is configured:

- API schema rejects secret/scope/watch/query/label/history-id inputs.
- API responses do not include credential handles, secret handles, tokens, or client secrets.
- Stored credential evidence hash is 64 lowercase hex.
- Granted scope evidence is exactly `https://www.googleapis.com/auth/gmail.readonly`.
- Broker can resolve the opaque handle without exposing the token to logs.
- Broker socket path and permissions restrict access to the mailbox runtime boundary.
- Initial polling can list/get messages from the dedicated test account.
- Incremental polling can process `users.history.list`.
- A 404/history-expired path moves the account to `sync_required`.
- Operator reset queues a full sync without caller-provided history id.
- Persisted message evidence is redacted/hash-only.
- Proposal approval/rejection records snapshot hash and selected job ids.
- Revoke blocks future sync.
