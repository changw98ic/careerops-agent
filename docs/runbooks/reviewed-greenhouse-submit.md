# Reviewed Greenhouse submit runbook

## Purpose and boundary

G013 defines Greenhouse Job Board API submission as a reviewed, broker-executed application
channel. It is narrow by design: public GETs can discover jobs and form schema, but POST is allowed
only when the target employer has explicitly created or authorized a Job Board API key for this
integration.

This runbook is operational guidance for the intended G013 path. It is not proof that Greenhouse
submission is configured. The checked-in repository must continue to fail closed unless the
deployment has an employer-authorized key profile, a credential broker that owns provider egress,
release qualification, caps, kill switches, revocation evidence and controlled live reconciliation
evidence.

Do not report "Greenhouse submit is live", "application submitted", "confirmed", "full chain
complete" or "universal auto apply" from this runbook alone.

## Official API facts

- Greenhouse Job Board API documentation:
  <https://developers.greenhouse.io/job-board.html>
- Greenhouse support article for creating a Job Board API key:
  <https://support.greenhouse.io/hc/en-us/articles/13446638483355-Create-a-job-board-API-key-for-an-integration>
- Public Job Board GET endpoints do not require authentication.
- The application POST endpoint is
  `POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}` and requires Basic
  Auth using a Job Board API key.
- Greenhouse says required-field validation is the client's responsibility. A 2xx response is
  therefore accepted by CareerOps only as `accepted_unverified`, never as confirmed.

## Current implementation status

Implemented or specified boundary:

- fixed provider origin `https://boards-api.greenhouse.io`;
- public schema read with `questions=true`;
- exact target binding for board token, job post id, schema SHA-256 and material SHA-256;
- default-disabled runtime configuration;
- separate `careerops_greenhouse_sender` database capability role;
- append-only account, review evidence, command receipt and reconciliation tables;
- transactional cap reservation and outbox eligibility;
- limited `careerops.cli.greenhouse_submit` CLI commands: `status`, `run-once` and `worker`;
- disabled-by-default `greenhouse-submit` Compose profile using the dedicated sender role;
- `accepted_unverified` receipt state for every bounded 2xx;
- mandatory reconciliation before any confirmed application state;
- no retry after 2xx, timeout, redirect, 5xx, oversized response, receipt persistence failure or
  any other ambiguous post-start outcome.

Not claimed by this runbook:

- live employer-authorized Job Board API key;
- live broker smoke test;
- universal applicant API access;
- browser automation for Greenhouse-hosted or employer-hosted forms;
- Harvest administrator API authority;
- confirmed application status from Job Board POST alone;
- automatic submission for custom questions, location, website or legal/compliance answers.

## Credential and egress boundary

The Greenhouse credential broker owns provider egress. Workers send only a reviewed command
envelope containing opaque credential reference, owner, board, job, schema hash, payload hash,
material hashes and review/release/cap evidence. The broker must:

- re-fetch the public schema from `boards-api.greenhouse.io`;
- require the fetched schema SHA-256 to equal the reviewed schema SHA-256;
- construct Basic Auth in memory from the employer-authorized Job Board API key;
- POST once to `boards-api.greenhouse.io`;
- reject redirects, alternate hosts, caller-supplied URLs and non-HTTPS transport;
- return only bounded non-secret outcome evidence.

The broker must never return the long-lived API key, a Basic Auth header, refresh token, client
secret, private key or unrelated provider credential to the worker, database, API, logs, receipts
or audit events.

## Runtime switches

Default configuration is fail-closed:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CAREEROPS_EXTERNAL_WRITES_ENABLED` | `false` | Provider writes are blocked globally. |
| `CAREEROPS_AUTO_SUBMIT_ENABLED` | `false` | Automated application submission is blocked. |
| `CAREEROPS_GREENHOUSE_SUBMIT_ENABLED` | `false` | Greenhouse submit remains disabled. |
| `CAREEROPS_GREENHOUSE_SUBMIT_RELEASE_ATTESTED` | `false` | Release qualification is absent. |
| `CAREEROPS_GREENHOUSE_SUBMIT_IN_PRODUCTION_ATTESTED` | `false` | Production use is not attested. |
| `CAREEROPS_GREENHOUSE_SUBMIT_CREDENTIAL_BROKER_SOCKET` | `/run/careerops-greenhouse/broker.sock` | Credential-broker Unix socket path. |
| `CAREEROPS_GREENHOUSE_SUBMIT_ATTACHMENT_BROKER_SOCKET` | `/run/careerops-greenhouse/attachments.sock` | Attachment-broker Unix socket path. |

Activation is all-or-nothing. Setting only one Greenhouse switch is a configuration error:
external writes, auto-submit, Greenhouse submit enablement, release attestation and an absolute
credential and attachment broker socket must be present together. Production additionally requires
in-production attestation.

The checked-in `greenhouse-submit` Compose profile runs the CLI worker with the
`greenhouse_sender` database role and mounts only the opaque Unix broker socket directory. It does
not mount API keys, secret files or object storage. The CLI and Compose profile do not prove live
readiness: a deployment must still provide its own authorized credential broker/key profile, broker
smoke, least-privilege database role evidence and employer-authoritative reconciliation evidence
before making a live claim.

The checked-in repository does not provide the broker server or an employer credential. The
credential source is deployment-owned: the target employer must explicitly create or authorize the
Job Board API key for this CareerOps integration, bind it to one account profile and board token,
and expose only an opaque broker handle to CareerOps. Public board discovery can run without that
key, but any POST path must stop at `not_configured` until the broker and key profile exist.

Safe local status check:

```bash
uv run python -m careerops.cli.greenhouse_submit status --json
```

With the checked-in defaults, the expected result is disabled or not configured, for example
`"enabled":false`. This command validates configuration shape, including absolute socket paths; it
does not probe a live broker, prove employer authorization or submit anything.

Execution commands are side-effectful once all gates are enabled:

```bash
uv run python -m careerops.cli.greenhouse_submit run-once --limit 1 --json
uv run python -m careerops.cli.greenhouse_submit worker --limit 10 --poll-seconds 5
```

Use them only after release evidence exists for the exact deployment, broker version, employer key
profile, caps and reconciliation path. Operators must not pass raw provider URLs, API keys, Basic
Auth headers or ad-hoc answers on the CLI; the worker accepts only reviewed database outbox rows.

## Review packet requirements

The human-visible review packet must show:

- employer-authorized account profile and board binding;
- board token hash, job post id and internal job id;
- public job/schema evidence SHA-256;
- exact allowed field set and answers;
- resume and cover letter filenames, MIME types, sizes and SHA-256 hashes;
- hard-stop classification result;
- payload hash, material hash and submission identity;
- approval or Campaign Grant version;
- cap usage and reservation key;
- release qualification status and expiry;
- active kill-switch and credential revocation state.

Approval is valid only for the immutable packet shown in review. Changing target job, schema,
answers, phone, attachments, material, credential profile, grant or release evidence creates a new
packet.

## V1 allowlist and hard stops

V1 may submit only:

| Field | Requirement |
| --- | --- |
| `first_name` | Required, reviewed, no URL. |
| `last_name` | Required, reviewed, no URL. |
| `email` | Required, reviewed, valid email address. |
| `phone` | Optional, reviewed, no URL. |
| `resume` | Optional or required by schema, reviewed attachment only. |
| `cover_letter` | Optional or required by schema, reviewed attachment only. |

Everything else is manual in v1. Hard stops include:

- government-compliance, EEO, demographic, disability, veteran, diversity or inclusion questions;
- GDPR, privacy, retention or other legal consent decisions;
- work authorization, visa/sponsorship, export-control, criminal-history or other legal
  statements;
- identity documents, payment, signatures, background checks or health data;
- assessment, test, scheduling, account creation, login, CAPTCHA or MFA flows;
- custom questions, custom attachments, location fields, location tuples, website, education,
  employment history, referral source or external attachment URL fields;
- hidden fields;
- unknown field names/types, ambiguous labels or schema drift.

Hard stops require manual application handoff. A generic approval, model answer or Campaign Grant
cannot override them.

## Execution flow

Allowed flow:

```text
public Greenhouse GET job/schema evidence
  -> immutable payload version and material hashes
  -> policy decision and hard-stop classification
  -> exact human review or active Campaign Grant
  -> release qualification and kill-switch check
  -> transactional cap reservation and outbox event
  -> Greenhouse broker re-GETs schema and verifies SHA-256
  -> broker constructs Basic Auth and POSTs once
  -> accepted_unverified, rejected or ambiguous evidence
  -> mandatory reconciliation for accepted_unverified or ambiguous outcomes
```

The worker must not accept ad-hoc field overrides, direct API keys, Basic Auth headers,
model-supplied execution commands or manual retry instructions that bypass this chain.

## Receipt and reconciliation states

Use these meanings:

| Status | Meaning |
| --- | --- |
| `not_configured` | Missing broker, employer authorization, credential profile or release evidence. |
| `disabled` | Switch, runtime mode, revocation or kill switch blocks execution. |
| `review_required` | Packet is eligible for human review but has not been approved. |
| `queued` | Cap was reserved and outbox event is eligible; no provider POST evidence yet. |
| `accepted_unverified` | Greenhouse returned bounded 2xx; mandatory reconciliation is still required. |
| `reconciliation_required` | Provider state is not independently proved; do not retry. |
| `rejected` | Definite pre-write validation or definite provider rejection. |
| `confirmed` | Independent employer-authority evidence or reviewed human evidence proves the result. |

Never use `confirmed` for a 2xx Job Board response, Gmail clue, generated material, screenshot,
manual draft or synthetic fixture.

Allowed confirmation evidence:

- independently authorized employer-side administrative read;
- recruiting webhook scoped to the target employer and application;
- reviewed human evidence from the employer system.

Gmail messages are useful clues only. They are not ATS-authoritative proof.

If reconciliation proves no application exists, create a fresh authorization before any new POST.
Never force an old event back to pending and never retry an ambiguous event.

## Production qualification

Before production use, collect evidence for:

- employer explicitly created or authorized the Job Board API key for this integration;
- key profile binds owner, employer, board token, operation and expiry/revocation metadata;
- broker owns provider egress and never returns the long-lived key;
- no API key appears in database rows, logs, audit events, command receipts or API responses;
- release qualification binds commit, migration, config, broker version and schema/material
  hashing rules;
- global, campaign, provider, account and credential kill switches block execution;
- daily, campaign and per-company caps are enforced in the reservation transaction;
- revocation stops new eligibility and before-call execution;
- at least 20 controlled live attempts have employer-authoritative reconciliation evidence;
- confirmed duplicate count is zero;
- default local and Compose stacks remain disabled without credentials.
