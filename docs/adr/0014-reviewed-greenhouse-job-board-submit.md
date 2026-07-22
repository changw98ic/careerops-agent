# ADR 0014: Bound Greenhouse submission to reviewed Job Board API execution

- Status: Accepted
- Date: 2026-07-21
- Builds on: ADR 0003 external side-effect authority, ADR 0011 bounded autopilot
  authority and ADR 0013 reviewed Gmail send.
- Narrows: the first real ATS write channel is the documented Greenhouse Job Board API. Unknown
  browser forms, custom employer domains and undocumented Greenhouse endpoints remain manual.

## Context

CareerOps already discovers public Greenhouse jobs and can prepare reviewed application material,
but a draft or PDF is not a submitted application. A real submission can create a candidate record,
disclose personal data, consume a campaign cap and become ambiguous if the request times out.

Official Greenhouse references:

- [Job Board API](https://developers.greenhouse.io/job-board.html)
- [Create a job board API key for an integration](https://support.greenhouse.io/hc/en-us/articles/13446638483355-Create-a-job-board-API-key-for-an-integration)

Greenhouse documents public job and question reads at
`GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?questions=true`.
Its application write is
`POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}` and requires a Job
Board API key over Basic Auth. Greenhouse also states that required-field validation is the
client's responsibility. The key is normally controlled by the organization operating the job
board, so this channel is useful only when the user has explicit authorization and a deployment
credential. It is not a universal applicant credential and must not be fabricated or scraped.

## Decision

### Provider and destination boundary

The adapter uses only the fixed HTTPS origin `boards-api.greenhouse.io`. Board token and numeric
job-post id are validated path segments and are included in the immutable destination identity.
The client does not accept a caller-supplied scheme, host, port, redirect target or arbitrary URL.

The adapter first reads the public job with `questions=true`, classifies the returned form, and
binds the exact response hash and normalized field schema to the reviewed submission. Before POST,
the broker re-reads the schema and requires the hash to match. A changed job, closed job, changed
question, unknown response shape or cross-board destination stops before the provider write.

`boards.greenhouse.io`, `job-boards.greenhouse.io`, embedded employer pages and custom employer
domains remain discovery destinations only. CareerOps does not replay their browser requests or
call undocumented form endpoints. Login, MFA and CAPTCHA are never bypassed.

### Credential boundary

CareerOps stores only an opaque versioned credential reference, credential profile hash and board
binding. The Greenhouse credential broker owns all provider egress for this channel. The worker
sends the broker a reviewed, hash-bound command envelope; it does not receive, parse, log or
construct Basic Auth from the long-lived Job Board API key.

The broker re-reads the public schema from the fixed `boards-api.greenhouse.io` origin, requires
the schema hash to match the reviewed packet, constructs Basic Auth in memory and performs the
single POST to the same fixed origin. It returns only a bounded non-secret execution result and
request/response evidence hashes. The broker response must match the expected owner, board,
credential profile and reviewed submission identity, and must not contain a Job Board API key,
refresh token, client secret, private key or unrelated provider credential.

The API key is never returned to a worker, persisted in application tables, audit records, logs,
receipts, command-line arguments, exception messages or API responses. Basic Auth is constructed
only inside the broker for the fixed provider request and is never forwarded across redirects.

### Form policy and human review

The adapter is allowlist-based. The v1 reviewed payload may contain only fields that the normalized
public form declares and the release policy explicitly permits:

- `first_name`;
- `last_name`;
- `email`;
- reviewed `phone`;
- reviewed `resume`;
- reviewed `cover_letter`.

All custom questions are manual in v1, even when they appear non-sensitive. Location fields,
location tuples, website fields, education, employment history, referral source and external
attachment URLs are also manual in v1.

The following conditions always stop for human handling and cannot be authorized by a generic
approval, model output or Campaign Grant:

- government-compliance, EEO, demographic, disability, veteran or diversity questions;
- GDPR or other legal consent and retention decisions;
- work authorization, visa/sponsorship, export-control, criminal-history or other legal
  attestations;
- identity documents, payment, signatures, background checks or health data;
- assessments, tests, scheduling, account creation, login, MFA or CAPTCHA;
- custom questions, location, location tuple, website, education, employment history, referral
  source or external attachment URL fields;
- hidden fields;
- unknown field names/types, ambiguous labels, custom attachments or schema drift.

The user reviews one immutable packet containing the board/job, public job evidence, normalized
questions, exact answers, material hashes, sensitive-field classification, credential profile,
grant/cap state and release qualification. Review does not create provider authority by itself;
the database must prove the complete chain.

### Authorization and execution chain

```text
Greenhouse public job/schema evidence
  -> ActionIntent and immutable PayloadVersion
  -> exact owner/campaign PolicyDecision
  -> exact active CampaignGrant and per-intent Authorization
  -> exact reviewed Greenhouse packet
  -> current review-required ReleaseQualification
  -> transactional cap reservation and OutboxEvent
  -> dedicated Greenhouse sender
  -> SideEffectAttempt
  -> ProviderReceipt or ReconciliationRequired
  -> append-only AuditEvent
```

The database, not the caller, derives release decision evidence from a qualification id. It binds
owner, campaign, grant, authorization, intent, payload, board, job id, schema hash, answer hash,
material hashes, credential profile and adapter release. Grant revocation, expiry, release
revocation, credential disablement and global/campaign/provider kill switches are rechecked after
the relevant transaction locks and before a side-effect attempt is created.

Grant state uses the shared lock-order contract introduced by G015: acquire the exact
`careerops:autopilot-grant-state:{grant_id}` advisory lock before campaign/grant row locks, re-read
active state, and only then proceed. Cap reservation remains transactional with outbox eligibility.

### Idempotency, receipt and ambiguity

The local submission identity covers owner, campaign, intent/payload, board, job id, schema hash,
answer hash, material hashes and credential profile. Replaying the same reviewed identity converges
on the same reservation and outbox event.

Greenhouse does not document an applicant-supplied exactly-once key, a success response schema,
a provider application id in the POST response or a Job Board API readback that proves whether a
submission succeeded. A clean 2xx therefore records only an immutable `accepted_unverified`
receipt containing the bounded HTTP status plus request/response hashes and immediately moves the
item to `reconciliation_required`. It does not mark the application confirmed and it is not
permission to retry. A definite pre-write validation or provider rejection records a rejected
attempt without automatic mutation of the reviewed payload.

Any timeout, broken connection, redirect, 5xx, invalid or oversized 2xx response, process crash,
receipt-persistence failure or unknown error after POST might have started is ambiguous. Both
`accepted_unverified` and ambiguous outcomes move to `reconciliation_required`; the worker must
not retry either. Reconciliation can attach independently authorized employer-side evidence, such
as a separately qualified least-privilege administrative read or recruiting webhook, under a new
reviewed command. A human may also record reviewed evidence that no application exists and create
a fresh authorization. Gmail messages are useful clues but are not ATS-authoritative proof.
Without such evidence, the item stays blocked. This channel claims no universal exactly-once or
provider-confirmation guarantee. `confirmed` is allowed only after independent employer-authority
evidence or reviewed human evidence, never from the Job Board POST response alone.

### Runtime and role defaults

The channel is disabled and unconfigured by default. Activation requires all of:

- global external writes and the separate automatic submit switch enabled;
- Greenhouse submit enabled and release attested;
- a current review-required release qualification;
- absolute credential and attachment broker sockets;
- a dedicated least-privilege Greenhouse sender database role;
- exact campaign authorization, reviewed packet, caps and inactive kill switches.

Production additionally requires an explicit in-production attestation. Default API, workflow,
mailbox and generic outbox roles cannot resolve the API key or execute Greenhouse provider
functions. The Greenhouse sender cannot create grants, approve packets, edit payloads or browse
unrelated application data.

The checked-in `greenhouse-submit` Compose profile is packaging for that disabled worker path. It
uses the dedicated `greenhouse_sender` role and opaque Unix broker socket mount only; it does not
mount API keys, provider secrets, raw object storage or arbitrary Greenhouse data into the worker.
The CLI can report status or run the worker loop, but it still requires a deployment-owned
authorized broker/key profile before any live POST can occur.

## Explicit non-goals

This decision does not authorize:

- obtaining, guessing, scraping or sharing an employer's Job Board API key;
- browser automation against Greenhouse-hosted or employer-hosted forms;
- undocumented Greenhouse endpoints, Harvest administrator actions or candidate impersonation;
- bypassing robots, terms, login, MFA, CAPTCHA, spam controls or employer restrictions;
- autonomous answers to legal, compliance, EEO, demographic or assessment questions;
- automatic retry after an ambiguous provider write;
- claiming a live submission when only fixtures, review packets or disabled configuration exist.

## Consequences

- CareerOps gains one narrow real-provider application channel with auditable authorization,
  attempts, receipts and recovery.
- Most ordinary preparation remains autonomous; the human reviews the final immutable packet and
  bounded exceptions.
- The channel is not available for most public Greenhouse boards unless the user has an explicitly
  authorized Job Board API credential. Those jobs continue to a manual handoff rather than an
  undocumented browser shortcut.
- Additional candidate-facing browser adapters require separate policy, qualification and release
  evidence.

## Verification

Acceptance requires unit and real-PostgreSQL evidence for fixed-origin networking, redirect
rejection, bounded broker envelopes, secret redaction, exact destination/schema/payload/material
binding, every hard-stop category, transactional caps, grant/release/credential revocation,
kill-switch behavior, role grants, idempotent local replay, `accepted_unverified` receipt,
ambiguity stop, reconciliation-before-retry, migration upgrade/downgrade and default-disabled
Compose/runtime, including the dedicated `greenhouse-submit` profile and socket-only worker mount.
No test may treat a Job Board POST response alone as confirmed.
