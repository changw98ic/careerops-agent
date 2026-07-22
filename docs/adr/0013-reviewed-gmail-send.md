# ADR 0013: Bound Gmail send to reviewed outbox execution and reconciliation

- Status: Accepted
- Date: 2026-07-20
- Builds on: ADR 0003 external side-effect authority, ADR 0004 Google OAuth scopes,
  ADR 0011 bounded autopilot authority and ADR 0012 dedicated BYO Gmail read-only sync.
- Narrows: Gmail send may exist only as a separate side-effect channel. ADR 0012 remains the
  read-only mailbox boundary and must not be widened to include send authority.

## Context

The job-search workflow needs to send approved application emails and follow-ups. That does not
mean the agent receives general mailbox authority. Sending email is an external write: it can
contact third parties, leak attachments, duplicate applications and create ambiguous provider
state after a timeout.

Google exposes `https://www.googleapis.com/auth/gmail.send` separately from
`https://www.googleapis.com/auth/gmail.readonly` in the
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes). The system therefore needs a separate
least-privilege credential reference, separate release evidence and a side-effect worker that can
execute only already-authorized immutable send payloads.

The product target is high autonomy with simple human review. The safe version is: agent prepares
recipient, subject, body and attachments; policy either requires per-intent approval or validates
an exact active Campaign Grant; the worker sends only that immutable payload; and reconciliation
stops duplicates when Gmail's result is ambiguous.

## Decision

### Credential and scope boundary

Gmail send uses a separate credential reference from Gmail read-only sync. A send-capable account
must prove:

- a dedicated recruiting Gmail account;
- BYO Google OAuth client;
- exact granted send scope `https://www.googleapis.com/auth/gmail.send`;
- a separate opaque credential handle for send execution;
- credential-store evidence SHA-256;
- account identity matched to the owner and candidate.

The send worker may request a short-lived send token through the same class of Unix-domain broker
client contract as Gmail read-only sync, but the broker server, refresh-token vault and OAuth
handshake are deployment dependencies. This repository must not store OAuth client secrets,
refresh tokens or access tokens in application tables, logs, API responses, docs examples or audit
payloads.

Read-only and send capabilities must not be collapsed into one broad mailbox role. A deployment
may use the same dedicated Gmail account for both scopes only after each capability has separate
scope evidence, credential references, release qualification and revocation handling.

### Authorization chain

No generic approval, model output, match score or "auto send" preference may authorize Gmail
send. A send intent becomes executable only when the database can prove the whole chain:

```text
ActionIntent
  -> immutable PayloadVersion
  -> PolicyDecision
  -> exact per-intent Approval or active CampaignGrant
  -> cap reservation
  -> transactional OutboxEvent
  -> isolated Gmail send worker
  -> ProviderAttempt
  -> ProviderReceipt or ReconciliationRequired
  -> append-only AuditEvent
```

The reviewed payload is immutable. In the current code path the canonical payload is
`gmail-send-payload.v1`, with channel `gmail:send` and adapter `gmail`, and it includes:

- owner, candidate, campaign, application and job snapshot identifiers;
- channel `gmail:send`;
- account id and credential reference version;
- recipient set, reply/thread reference when applicable, subject hash and body hash;
- attachment object keys, MIME types, filenames, sizes and hashes;
- idempotency key and provider reconciliation key;
- policy version, grant or approval version, release qualification version and expiry.

The worker must re-read and validate the latest database state before sending. It must reject
stale approval, stale payload, exhausted cap, revoked grant, inactive kill switch state, missing
release qualification, credential mismatch or any payload byte mismatch.

### Caps and kill switches

Gmail send is high risk. Before outbox eligibility, the system must enforce:

- global external-write switch;
- Gmail-send provider switch;
- campaign switch;
- account/credential switch;
- exact active Campaign Grant or per-intent approval;
- total campaign cap;
- daily campaign cap;
- per-recipient cap;
- same-company duplicate guard;
- release qualification current and unexpired.

Caps are consumed in the same transaction that creates the eligible outbox event. A policy
evaluation alone does not reserve capacity and cannot authorize a send.

### Execution and receipts

The Gmail send worker can execute only leased eligible outbox events whose event key is prefixed
`gmail-send:`. It runs with the dedicated `careerops_mail_sender` database role and cannot edit
payloads, approve proposals, create grants, change policy decisions, read Gmail inbox content,
mutate Gmail labels, access browser sessions or write business projections directly.

Runtime secret resolution is split across three local Unix-domain broker contracts:

- send tokens use `careerops.gmail.send-credential-broker.v1` and must return only the exact
  `https://www.googleapis.com/auth/gmail.send` scope;
- Sent/thread reconciliation uses the G011 read-only broker
  `careerops.gmail.credential-broker.v1` and must return only the exact
  `https://www.googleapis.com/auth/gmail.readonly` scope;
- attachments use `careerops.gmail-send.attachment-broker.v1` and must return bytes matching the
  reviewed attachment size and SHA-256.

The send account must bind to a G011 read-only Gmail account for the same owner/account before an
active account can support Sent/thread reconciliation. This binding is explicit
(`reconciliation_gmail_account_id`); it does not merge the send and read-only OAuth credential
references.

A successful call records provider evidence such as Gmail message id and thread id when available,
plus the request hash, response hash, attempt metadata and audit correlation id. The receipt must
be append-only. Application state changes happen later through reconciliation/reducer logic, not
inside the send worker.

### Ambiguous-state handling

The system does not claim mathematical exactly-once across PostgreSQL and Gmail. It claims
effectively-once within the documented failure model.

Gmail does not provide a provider idempotency key for `users.messages.send`. The idempotency key
and reconciliation key are CareerOps-owned controls; they prevent blind resend by binding the
reviewed payload, local outbox state, receipt recording and Sent/thread reconciliation.

If a timeout, broken connection, process crash, partial response, unknown Gmail error or missing
receipt occurs after the worker might have called Gmail, automatic retry stops. The event moves to
`reconciliation_required`. Reconciliation must search by the bound provider reconciliation key,
payload hash, recipient, subject/body hash, optional thread and Sent-folder evidence. Only one of
these outcomes is allowed:

- confirm the original send and attach a receipt;
- prove no send occurred and create a fresh authorized attempt;
- keep the item blocked for review.

The worker must not blindly resend in an ambiguous state.

### Sent-folder reconciliation

Gmail send release qualification requires Sent/thread reconciliation. The read-only sync lane may
observe Sent-folder or thread evidence only under its own read-only credential boundary. It must
not inherit send authority. Reconciliation joins by owner/account/application, provider message or
thread identity, immutable payload hashes and request/audit ids.

### Defaults

Gmail send is disabled and unconfigured by default. A local stack without the send migration,
send-capable runtime role, broker servers, attachment broker, vault, BYO OAuth material, exact
`gmail.send` and `gmail.readonly` scopes, release qualification and controlled test-account
evidence must report Gmail send as
`not_configured`, `not_qualified` or `disabled`. It must not report "mailbox connected",
"email chain complete" or "sent" from drafts, review packets or synthetic fixtures.

## Current repository status

The repository contains the reviewed Gmail send control-plane and worker scaffolding:

- migration `0013` creates Gmail send accounts, drafts, reservations, review evidence,
  reconciliation jobs and narrow `careerops_mail_sender` functions;
- the internal API accepts only structured account, exact-payload draft, review and reservation
  requests, with `extra="forbid"` schemas;
- the worker/CLI path (`careerops-gmail-send status|run-once|reconcile-once`) is wired to the
  dedicated mail-sender database role and the broker contracts above;
- the default Compose and `.env.example` values leave
  `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`, `CAREEROPS_GMAIL_SEND_ENABLED=false`,
  `CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED=false` and
  `CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED=false`.

This is code and synthetic PostgreSQL qualification only. It is not live Gmail configuration, a
checked-in OAuth flow, a broker/vault implementation, live credential evidence, live send smoke
evidence or proof that the full email chain is complete.

## Explicit non-goals

This ADR does not implement or authorize:

- OAuth authorization handshake UI;
- deployment-owned broker server or refresh-token vault;
- checked-in Gmail credentials or real Gmail account configuration;
- live send smoke evidence;
- `gmail.compose`, `gmail.modify`, drafts, labels, trash/delete, settings, SMTP, IMAP or POP;
- generic recruiter replies;
- automatic offer, legal, salary, visa, relocation, calendar-attendee or document-request
  responses;
- using a Codex, Claude, browser or plugin Gmail connector as the product runtime provider;
- bypassing per-intent approval or Campaign Grant with a model decision.

## Consequences

- The agent can do most preparation work while the human review surface stays small: exact
  recipient, subject, body, attachments, job/application target, risks and grant/cap evidence.
- Gmail send becomes a narrow provider-write channel, not a general mailbox client.
- A timeout after send is treated as a reconciliation problem, not as permission to retry.
- Read-only mailbox sync and send stay independently auditable and revocable.
- The product cannot truthfully claim the email chain is complete until controlled Gmail send,
  receipt, Sent reconciliation and read-only inbound reconciliation have release evidence.

## Verification

Acceptance requires evidence that:

- only exact `gmail.send` scope is accepted for send credentials;
- read-only and send credentials are separate references with separate capability state;
- no token, refresh token or client secret is stored or returned;
- request schemas reject raw token, client secret, refresh token, broad mailbox scope, compose,
  modify, draft, label, SMTP, IMAP and POP fields;
- payload hash covers recipient, subject, body, attachments, account, application, approval/grant
  and release evidence;
- changing any reviewed payload byte invalidates eligibility;
- per-recipient and daily caps are enforced transactionally with outbox eligibility;
- revoked, expired, exhausted or superseded Campaign Grants cannot authorize send;
- global, campaign, provider and account kill switches block before provider call;
- before-call crashes retry safely, while after-call ambiguity moves to reconciliation;
- duplicate send under the same idempotency key converges to one confirmed provider effect;
- Sent/thread reconciliation can confirm a timeout-with-success without resending;
- default local and Compose stacks report Gmail send disabled/unconfigured with no live send
  claim.
