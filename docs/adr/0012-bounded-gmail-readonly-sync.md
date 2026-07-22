# ADR 0012: Bound Gmail read-only sync to dedicated BYO accounts and reviewed proposals

- Status: Accepted
- Date: 2026-07-20
- Refines: ADR 0004 for the bounded, polling-only implementation of its Gmail read-only stage.
  ADR 0004 remains authoritative for Google OAuth scope and release decisions.
- Extends: ADR 0003 side-effect authority by defining a read-only Gmail signal source that can
  create review proposals but cannot send, compose, modify, label, delete, or otherwise mutate
  Gmail.

## Context

The job-search workflow needs mailbox evidence for recruiter replies, application
acknowledgements, interviews, assessments, offers, rejections, and reconciliation hints. That
evidence is useful only if the system can read recruiting signals without becoming a general
mail client or a hidden external-write channel.

Google's Gmail API exposes a restricted `gmail.readonly` OAuth scope for viewing Gmail messages
and settings, while broader scopes such as `gmail.send`, `gmail.compose`, `gmail.modify`, and
`https://mail.google.com/` permit writes or broader authority. Google documents the scope list in
[Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) and
[OAuth 2.0 Scopes for Google APIs](https://developers.google.com/identity/protocols/oauth2/scopes).

Google's message list endpoint returns message ids and thread ids, and message details require
`users.messages.get`; see [users.messages.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
and [users.messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get).
Incremental sync can use [users.history.list](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list).
Gmail also supports Watch/Pub/Sub push notifications, but that requires Cloud Pub/Sub setup and
watch renewal; see [Configure push notifications in Gmail API](https://developers.google.com/workspace/gmail/api/guides/push).

The product direction is not "connect my normal mailbox and let the agent do anything." The
bounded design is a dedicated recruiting Gmail account, brought by the user, with exact read-only
authority and a review queue before any downstream workflow state changes.

## Decision

### Account and OAuth boundary

Gmail read-only sync is allowed only for a dedicated Gmail account used for the job-search
workflow. Registration requires the operator to attest:

- the account is dedicated to recruiting/job-search workflow use;
- the OAuth client is BYO, not a platform-owned shared OAuth client;
- the OAuth publishing status is recorded as `testing` or `in_production`;
- credential-store evidence is recorded as a lowercase SHA-256 digest;
- the only Gmail scope is exactly `https://www.googleapis.com/auth/gmail.readonly`.

The API accepts an opaque `credential_handle`, not a token, refresh token, or client secret. The
handle is resolved through a Unix-domain credential broker owned by the local
runtime boundary. Application code stores the opaque handle and evidence hash only; it must not
store OAuth client secrets or refresh tokens in application tables, logs, API responses, runbooks,
or audit payloads.

The runtime is default-unconfigured. A local deployment without an installed Unix broker server,
BYO OAuth credential, mailbox database role/login and explicit `CAREEROPS_GOOGLE_OAUTH_ENABLED`
configuration must return unavailable rather than pretend Gmail is connected. The repository may
ship the broker client, Gmail read-only HTTP client, polling worker and database contract without
claiming that any Gmail account is configured.

### Sync algorithm

The first supported implementation is polling only:

1. Initial sync lists messages with `users.messages.list`.
2. For selected message ids, the worker calls `users.messages.get`.
3. The worker keeps message metadata and snippet classification inputs in memory only; it does
   not fetch or classify raw message bodies.
4. The worker persists only redacted excerpts, hashes, classification, provenance, and proposal
   payload hashes.
5. Subsequent sync uses `users.history.list` from the stored Gmail history cursor when possible.
6. If Gmail returns a history-expired condition, including the practical `404` path for an invalid
   or too-old `startHistoryId`, the account moves to `sync_required`.
7. An authenticated operator can call reset-history. Reset does not accept a caller-provided
   Gmail history id. It clears the stale cursor and queues a full sync.

The worker persists provider message id, thread id, and history id as the minimum provider
identity needed for provenance, deduplication, and reconciliation. It must not persist raw message
bodies, sender addresses, subjects, snippets, labels, or attachment contents unless the field is
explicitly represented as a redacted excerpt or hash. Attachment fetching is out of scope unless
a later ADR defines a separate quarantine and review boundary.

### Owner scope, roles, and persistence

Every Gmail account, sync run, message signal, proposal, review decision, and command receipt is
owner-scoped. Queries must bind the authenticated owner id, not just the Gmail account id.

Runtime roles are split:

- API/operator role can read owner-visible summaries and write owner-scoped commands/review
  decisions through narrow repository methods.
- Worker role can claim queued sync work, use the credential broker, call read-only Gmail
  endpoints, write redacted/hash-only signals, and queue proposals.
- Read-only/reporting role can inspect summaries without access to opaque credential handles.

The database contract is append-heavy. Message signals and review decisions are audit evidence;
they are not mutable scratch rows. Reconciliation must create new evidence instead of rewriting
historical signal content.

### Proposal review

Gmail signals are not applied directly to candidate/application state. Relevant or ambiguous
signals create reviewed proposals. A proposal contains:

- redacted excerpt and hash-bound signal evidence;
- classification, relevance, confidence, provenance, and proposal kind;
- immutable payload SHA-256;
- optional reviewer-selected `canonical_job_id` and `job_posting_id` during approval.

Approval or rejection requires authenticated owner identity, CSRF-protected operator action, the
proposal id, current payload/snapshot hash, and an idempotency key. Approval makes the proposal
eligible for downstream reconciliation or follow-up drafting. It does not send email. It does not
modify Gmail. It does not submit applications.

### Revoke

Revoke marks the owner-scoped Gmail account revoked, prevents future sync runs, and records a
command receipt. It does not delete historical redacted/hash evidence that remains inside the
retention policy. The credential broker or external vault remains responsible for invalidating
or deleting OAuth material; the application records only the revocation state and audit evidence.

## Explicit non-goals

The following are not implemented by this ADR:

- Gmail Watch/PubSub, `users.watch`, Cloud Pub/Sub topic/subscription setup, watch renewal, or
  push webhook handling.
- OAuth authorization handshake UI.
- Refresh-token vault implementation.
- The deployment-owned Unix credential broker server and refresh-token vault. The repository
  implements only the bounded broker client/protocol.
- OAuth credentials, broker socket material or a smoke-tested real Gmail account in the checked-in
  local stack.
- Gmail send, compose, draft creation, label modification, message modification, trash/delete,
  settings changes, SMTP, IMAP, or POP.
- Any automated reply to recruiters.
- Any claim that a real Gmail account has been connected in local, staging, or production.

The API and worker must fail closed when these dependencies are absent. "Configured Gmail
read-only sync" requires separate evidence that the broker, BYO OAuth credential, restricted
scope, owner-scoped DB repository, and polling worker are all installed and passing smoke tests.

## Consequences

- Mailbox evidence can feed the job-search workflow without granting email-write authority.
- Dedicated BYO accounts reduce accidental ingestion of unrelated personal mail.
- Exact `gmail.readonly` keeps the authority narrow but still requires restricted-scope security
  discipline under Google's OAuth policies.
- Polling is simpler and auditable for the initial release; Watch/PubSub remains a later
  performance optimization, not a hidden dependency.
- Reset-history is operator-controlled recovery, not a caller-selected cursor override.
- The product can truthfully say Gmail is "unconfigured" until real credential/broker/operator
  evidence exists.

## Verification

Acceptance requires tests and runbook evidence that:

- request schemas reject token/client-secret/refresh-token/scope/watch/query/label inputs;
- responses never expose opaque handles or secret handles;
- only exact `gmail.readonly` is accepted;
- owner id is bound on every read and command;
- history-expired or 404 recovery moves to `sync_required`;
- reset-history queues a full sync without accepting a caller-supplied history id;
- persisted signal rows contain redacted/hash-only data;
- proposal approval binds snapshot/payload hash and selected job ids;
- revoke blocks future sync;
- a default local stack with no broker/OAuth credential reports Gmail read-only unavailable.
