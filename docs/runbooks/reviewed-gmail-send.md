# Reviewed Gmail send runbook

## Purpose and boundary

G012 defines Gmail send as a reviewed outbox side-effect channel for the job-search workflow.
The G015 implementation adds the code/database scaffolding for that boundary. G014 can now compose
a GoalRun into an exact Gmail draft, Chinese simple review packet and Gmail outbox enqueue. It is
separate from G011 Gmail read-only sync: read-only sync can import recruiting signals, while Gmail
send can execute only an already-authorized immutable email payload.

This runbook is operational guidance for the intended G012 path. It is not proof that Gmail send
is configured. The checked-in repository must continue to fail closed unless the deployment has a
send-capable runtime role, send/read-only/attachment broker sockets, BYO OAuth material, exact
`gmail.send` and `gmail.readonly` scopes, release qualification, controlled test account and
send/reconciliation smoke evidence.

For host-side Desktop OAuth client import, loopback/PKCE authorization, in-process
Security.framework Keychain storage, broker socket ownership, broker `smoke-send` qualification
and opaque-handle registration, use
[Gmail OAuth broker runbook](gmail-oauth-broker.md). Chrome login alone is not a CareerOps
credential; browser sessions and plugin Gmail connectors must not substitute for the OAuth broker.

Do not report "邮箱链路已打通", "email sent", "Gmail configured" or "full chain complete" from this
runbook alone.

Current local evidence: the readonly grant for the configured dedicated Gmail account succeeded. The send grant,
broker `smoke-send`, Sent/read-only reconciliation and database registration are not complete.

## Current implementation status

Implemented boundary:

- exact Gmail send scope: `https://www.googleapis.com/auth/gmail.send`;
- exact Gmail read-only reconciliation scope: `https://www.googleapis.com/auth/gmail.readonly`;
- separate send credential reference from G011 read-only sync;
- explicit active-account binding to a G011 reconciliation account
  (`reconciliation_gmail_account_id`);
- canonical `gmail-send-payload.v1` reviewed payload for sender, recipient, subject, body,
  optional thread/reply identifiers and attachments;
- exact per-intent approval or active Campaign Grant;
- daily and per-recipient caps;
- global, campaign, provider and account kill switches;
- transactional outbox eligibility;
- append-only attempt, receipt, reconciliation and audit evidence;
- ambiguous-state stop before retry;
- Sent/thread reconciliation;
- dedicated `careerops_mail_sender` runtime role;
- `careerops-gmail-send` CLI with `status`, `run-once`, `reconcile-once` and `worker`
  subcommands;
- Unix-domain broker clients for send tokens, read-only reconciliation tokens and attachment
  bytes;
- host-side Gmail broker CLI/server for local loopback OAuth, in-process macOS
  Security.framework Keychain storage, credential sockets and attachment sockets;
- G014 GoalRun composition into the same Gmail send boundary: deterministic match, exact draft,
  Chinese review, approved outbox enqueue and receipt/reconciliation inspection with no provider
  I/O in the workflow role.

Not claimed by this runbook:

- browser-native Google consent is still operator-confirmed and not automated past the consent
  boundary;
- deployment-owned reviewed-attachment storage configuration;
- checked-in OAuth credentials;
- real Gmail account configuration;
- live Gmail send smoke;
- generic compose/draft/modify/label authority.

G014 composition does not claim a live Gmail send. It creates and enqueues the exact reviewed
payload; the separate mail-sender worker, host broker/Keychain boundary or deployment-equivalent
secret store, exact credentials and release evidence still gate provider execution.

## Gmail API facts

- Gmail send uses the fixed
  [`users.messages.send`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
  POST and returns a Gmail `Message` with provider ids.
- `gmail.send` and `gmail.readonly` are separate
  [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes); this runbook
  does not allow broad mailbox scopes to substitute for either exact scope.
- Sent reconciliation belongs to the read-only lane. The Gmail
  [`users.messages.list`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
  API supports `q` searches, including RFC 822 Message-ID search terms, and `labelIds`; the
  [`SENT` system label](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels)
  is the expected label boundary for Sent-folder evidence.
- Gmail send does not supply a provider idempotency key. CareerOps idempotency is local: it binds
  the reviewed payload, outbox event, receipt recording and reconciliation key, and it stops before
  resend when provider state is ambiguous.

## Configuration evidence

Before enabling Gmail send for an account, collect evidence for:

- dedicated Gmail account for recruiting/job-search only;
- BYO OAuth client;
- exact granted scope `https://www.googleapis.com/auth/gmail.send`;
- account subject and owner/candidate binding;
- opaque send credential handle;
- credential-store evidence SHA-256;
- release qualification id, commit/migration/config binding and expiry;
- global, campaign, provider and account kill-switch states;
- Campaign Grant or per-intent approval policy version;
- daily and per-recipient cap settings.

Use a send broker profile that is separate from the G011 read-only broker profile. Strict
deployments should use a separate Google Cloud project or OAuth client profile for `gmail.send`
because send is an external-write capability with a separate release gate, kill-switch path,
revocation path and incident blast radius. Import a Google Desktop OAuth client JSON into the host
broker, move the cleartext source JSON to the operator's Trash after successful import, run the
local loopback/PKCE authorization flow with `--account-subject`, and store client secrets plus
refresh tokens through in-process macOS Security.framework Keychain calls or an equivalent
OS-backed vault, never in this repository. The Keychain path must stay inside the broker process
rather than crossing a command-line boundary with client secrets or token JSON. The operator must
explicitly accept the Google consent screen for the expected account and exact `gmail.send` scope
before recording approval; URL generation, Chrome login or callback receipt alone is not consent
evidence. If the OAuth consent screen remains in Testing, treat the credential as
development-only: Google documents that External Testing apps receive refresh tokens that expire
after 7 days from authorization/re-authorization unless only basic profile/OIDC scopes are
requested.

The send account is active only when it is bound to a G011 read-only Gmail account for the same
owner/account. That binding supports Sent/thread reconciliation with the read-only credential; it
does not let the send worker inherit general mailbox authority.

Account bootstrap order:

1. Authorize and live-qualify the dedicated G011 Gmail read-only account first. It must use the
   BYO OAuth client, exact `gmail.readonly` scope, a dedicated account attestation, opaque
   credential handle and credential-store evidence.
2. Authorize and live-qualify the separate Gmail send handle. The broker must not issue a public
   send access token until the controlled same-account `smoke-send` path has produced
   qualification evidence.
3. Register both handles with the local onboarding CLI:

   ```bash
   uv run careerops-gmail-onboarding register-local \
     --account-subject jobs-only@example.com \
     --candidate-display-name "Local Gmail Candidate" \
     --json
   ```

   The command reads live-qualified broker status, creates or reuses the candidate, registers the
   read-only account with `publishing_status=testing`, then registers the send account bound to
   `reconciliation_gmail_account_id` with `requested_status=disabled`. It emits ids and hashes
   only and never enables any execution gate.
4. Use the authenticated Gmail send control-plane list/status routes to inspect registration while
   keeping `CAREEROPS_EXTERNAL_WRITES_ENABLED=false`, `CAREEROPS_AUTO_SEND_ENABLED=false` and
   `CAREEROPS_GMAIL_SEND_ENABLED=false`.
5. Supply GoalRun `gmail_dispatch` with account, campaign, grant and release ids plus reviewed
   attachment hashes. Do not put tokens, client secrets, refresh tokens, raw attachment bytes,
   Greenhouse credentials or browser state in GoalRun context.
6. Enable `CAREEROPS_GMAIL_SEND_ENABLED` and run the mail-sender worker only after the runtime
   switches, broker sockets, account evidence and release qualification all match this runbook.

Never paste or store:

- OAuth client secret;
- refresh token;
- access token;
- Gmail password;
- broad `https://mail.google.com/` scope;
- `gmail.compose`, `gmail.modify`, draft, label, settings, SMTP, IMAP or POP credentials.

## Runtime switches and commands

Default configuration is fail-closed:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CAREEROPS_GOOGLE_OAUTH_ENABLED` | `false` | No Google OAuth-backed worker can run live. |
| `CAREEROPS_EXTERNAL_WRITES_ENABLED` | `false` | Provider writes are blocked globally. |
| `CAREEROPS_AUTO_SEND_ENABLED` | `false` | Automated send execution is blocked. |
| `CAREEROPS_GMAIL_SEND_ENABLED` | `false` | Gmail send worker remains disabled. |
| `CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED` | `false` | Release qualification is absent. |
| `CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED` | `false` | Production use is not attested. |
| `CAREEROPS_GMAIL_SEND_BROKER_SOCKET` | `/run/careerops-gmail-send/broker.sock` | Send-token Unix broker path. |
| `CAREEROPS_MAILBOX_BROKER_SOCKET` | `/run/careerops-gmail/broker.sock` | G011 read-only reconciliation broker path. |
| `CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET` | `/run/careerops-gmail-send/attachments.sock` | Attachment-byte Unix broker path. |

The CLI entry point is `careerops-gmail-send`. Its safe status path reports whether the complete
configuration gate is enabled. It validates that broker paths are absolute, but it does not probe
the sockets or prove that a broker, vault, credential or Gmail account is available:

```bash
uv run careerops-gmail-send status --json
```

Makefile equivalents set the mail-sender database role from environment variables:

```bash
make gmail-send-status
make gmail-send-once
make gmail-send-reconcile
make gmail-send-worker
```

These Makefile targets derive `CAREEROPS_DATABASE_URL` through `careerops-database-url` using the
mail-sender login parts from environment variables, and stop before worker launch if URL encoding
fails. They then run the final CLI under `env -i`. The final worker environment contains only
`PATH`, `UV_PROJECT_ENVIRONMENT`, `CAREEROPS_ENVIRONMENT`,
`CAREEROPS_CONSOLE_COOKIE_SECURE`, encoded `CAREEROPS_DATABASE_URL`, the
readonly/send/attachment broker socket paths, and the explicit Google OAuth / external writes /
auto-send / Gmail send gates and attestations, including Google OAuth and Gmail send production
attestations. Every gate still defaults to `false`; these targets do not enable live send. The
long-running worker defaults to `CAREEROPS_GMAIL_SEND_POLL_SECONDS=5` and
`CAREEROPS_GMAIL_SEND_LIMIT=10`.

Activation is atomic. Setting only one Gmail-send switch is a configuration error; external
writes, auto-send, Google OAuth, Gmail send, release attestation and all three absolute broker
paths must be supplied together. Production mode additionally requires the Google OAuth and
Gmail-send production attestations. Before any live claim, perform a deployment-owned broker
round-trip and controlled-account send/reconciliation smoke test; `status --json` alone is not
that evidence.

`run-once`, `reconcile-once` and `worker` are execution commands. Do not run them against live
accounts without the release evidence in this runbook. The checked-in Compose service sets
`CAREEROPS_DATABASE_ROLE=mail_sender` and leaves Google OAuth and Gmail send disabled. The
`gmail-send` Compose profile mounts socket volumes but does not define broker, Security.framework
Keychain/vault or attachment broker services. Keep host broker services separate from Docker core
workers: a deployment must provide those processes and mount only their intended sockets before
live activation.

Compose is an optional packaging and compatibility path, not the default or required runtime for
this channel. Choose native versus containerized database/API/workers explicitly for each
deployment; the macOS Keychain broker remains host-native in either case.

For a controlled one-event smoke pass, use explicit bounds instead of the broader Makefile
defaults:

```bash
uv run careerops-gmail-send run-once --limit 1 --json
uv run careerops-gmail-send reconcile-once --limit 1 --json
uv run careerops-gmail-broker --json smoke-send --account-subject jobs-only@example.com
```

The `smoke-send` account subject must be the same Gmail subject registered on the send account and
bound to the G011 reconciliation account. Record live qualification only if that deployment-owned
hook verifies the registered send handle, exact `gmail.send` scope, Keychain/vault refresh path,
Google account eligibility and provider receipt or Sent/thread reconciliation evidence. Do not
claim live success from CLI availability, unit tests, fixtures, status output or generated drafts.

Run `smoke-send` once only after explicit approval. Gmail may replace the submitted RFC 822
`Message-ID`, so the broker verifies provider ids plus the exact From, To, Subject, body hash and
`SENT` label through the separate readonly credential. If Gmail may have accepted the message but
the command did not persist qualification, do not resend. Use the read-only recovery path:

```bash
uv run careerops-gmail-broker --json recover-smoke-send \
  --account-subject jobs-only@example.com
```

Recovery requires exactly one recent controlled-template match. If an audited incident produced
multiple exact matches, pass the reviewed subject SHA-256 with
`--expected-subject-sha256 <64-lowercase-hex-digest>` (or the guarded
`make gmail-oauth-recover-smoke-send` target). The implementation must fail closed on zero or
multiple matches and must never auto-select the newest message.

Socket requirements:

- `/run/careerops-gmail-send/broker.sock` must be the send-token broker and must return only
  `https://www.googleapis.com/auth/gmail.send`;
- `/run/careerops-gmail/broker.sock` must remain the read-only broker used for Sent/thread
  reconciliation and must return only `https://www.googleapis.com/auth/gmail.readonly`;
- `/run/careerops-gmail-send/attachments.sock` must return only reviewed attachment bytes matching
  the immutable size and SHA-256 references;
- parent directories should be owned by the broker runtime users and mode `0700`;
- socket files should allow only the intended runtime role to connect, for example owner-only
  mode `0600` or an equivalent service-manager ACL.

The send broker client sends this protocol request for each opaque handle:

```json
{
  "version": "careerops.gmail.send-credential-broker.v1",
  "provider": "gmail",
  "action": "send_email",
  "opaque_handle": "gmail:send:opaque-handle",
  "account_subject": "jobs-only@example.com",
  "scope": "https://www.googleapis.com/auth/gmail.send"
}
```

The broker must return a short-lived access-token envelope for the same account and exact send
scope. It must not return refresh tokens, client secrets, client ids, token URIs or private keys.

## Review packet requirements

The human-visible review packet must show exactly what will be sent:

- account subject and capability state;
- recipient set;
- subject;
- body preview with hash;
- attachment filenames, MIME types, sizes and hashes;
- target application/job/campaign;
- duplicate and same-recipient checks;
- policy decision and hard-stop result;
- approval or Campaign Grant version;
- daily/per-recipient cap usage;
- release qualification status and expiry;
- active kill-switch state;
- idempotency/reconciliation key.

Approval is valid only for the immutable snapshot shown in the packet. Editing recipient, subject,
body, attachments, account, target job/application, policy, grant or release evidence creates a new
payload version and requires revalidation.

When the packet comes from G014 GoalRun composition, the review is intentionally simple and
Chinese-first. It must show the selected job, deterministic match reasons, recipient, subject, body,
attachment filenames/types/sizes/hashes, `payload_hash`, `target_hash`, `grant_material_hash`,
review snapshot SHA-256 and action ids. Approval authorizes only that exact payload for outbox
enqueue; it does not authorize arbitrary Gmail drafting, account mutation, Greenhouse submission or
browser submission.

## Execution flow

The allowed path is:

```text
prepare send intent or G014 exact GoalRun Gmail draft
  -> create immutable payload version
  -> evaluate policy and hard stops
  -> collect exact approval or active Campaign Grant
  -> reserve cap and create eligible outbox event in one transaction
  -> mail-sender worker claims leased gmail-send outbox event
  -> worker revalidates payload, send/read-only credentials, caps, switch and release evidence
  -> worker resolves reviewed attachment bytes through the attachment broker
  -> worker calls Gmail send once
  -> record provider attempt and receipt, or mark reconciliation_required
  -> reducer updates application state only from receipt/reconciliation evidence
```

The worker must not accept ad-hoc recipient/body overrides, model-supplied execution commands or
manual retry instructions that bypass the intent/outbox chain.

## Idempotency key

The send idempotency key binds:

```text
mail_account
application
thread or new-thread marker
recipient set
subject hash
body hash
attachment hashes
approval or grant version
release qualification version
provider reconciliation key
```

Reusing the same key must converge to at most one confirmed Gmail provider effect. Changing any
bound value creates a new intent and re-enters review/policy validation.

## Ambiguous-state handling

Before the provider call:

- validation failures, inactive switches, missing credential, missing release qualification and
  stale approvals fail before Gmail is called;
- before-call worker crashes can be retried within the bounded lease policy.

After Gmail may have been called:

- timeout;
- broken connection;
- worker crash;
- partial response;
- unknown Gmail 5xx/transport error;
- missing receipt;
- inconsistent provider ids.

These states move the item to `reconciliation_required`. Do not resend automatically. Reconcile
against Sent/thread evidence, provider message/thread ids when available, immutable payload hashes
and the request/audit correlation id.

Allowed reconciliation outcomes:

- `confirmed_sent`: attach provider receipt and advance state;
- `confirmed_not_sent`: create a fresh eligible attempt only after policy/caps/switches still pass;
- `still_ambiguous`: keep the item blocked for operator review.

## Sent/thread reconciliation

Sent reconciliation is a separate evidence path. It may use G011 read-only sync under its own
`gmail.readonly` credential boundary. It must not grant read-only workers send authority and must
not let the send worker read arbitrary inbox content.

Match on:

- owner and dedicated account;
- application/job/campaign id;
- provider message/thread id;
- recipient;
- subject/body/attachment hashes;
- request/audit correlation id;
- sent timestamp window.

If multiple plausible Sent messages match, treat the result as ambiguous and require review.

## Kill switches and revocation

Gmail send must check switches before outbox eligibility and again before provider execution:

- global external-write switch;
- Gmail-send provider switch;
- campaign switch;
- account/credential switch.

Revoking the Gmail send credential stops new outbox eligibility and leased-before-call execution.
If revocation happens after an ambiguous provider call, reconciliation still needs to determine
whether a send occurred, but it must not call Gmail send again.

Application revocation is not enough by itself. Revoke or delete the matching broker-side OAuth
material from Keychain or the external vault, then confirm the send worker cannot resolve the
opaque handle. Keep the G011 read-only credential available only if it is still needed for
Sent/thread reconciliation and remains independently authorized.

## Reporting rules

Use these exact meanings:

These are product/account workflow states. The current CLI status surface reports only the
configuration gate (`enabled` plus a disabled reason), and the disabled API surface returns an
unavailable response; neither surface by itself proves live broker or Gmail readiness.

| Status | Meaning |
| --- | --- |
| `not_configured` | Missing credential/broker/Keychain-or-vault/account setup. No Gmail send can occur. |
| `not_qualified` | Implementation may exist, but release qualification is absent, expired or revoked. |
| `disabled` | Switch or runtime mode blocks send. |
| `review_required` | Payload is ready but needs exact approval or an active grant. |
| `queued` | Cap was reserved and outbox event is eligible, but worker has not called Gmail. |
| `reconciliation_required` | Gmail may have accepted the message; do not retry until reconciled. |
| `sent_confirmed` | Provider receipt or Sent/thread reconciliation confirms the send. |

Do not use `sent_confirmed` for drafts, generated email bodies, manual handoff, synthetic provider
fixtures or screenshots without provider/Sent evidence.

## Verification checklist

Before claiming Gmail send is configured:

- exact `gmail.send` scope evidence exists;
- read-only and send credential references are separate;
- broker resolves a short-lived token without logging or storing it;
- send, read-only reconciliation and attachment sockets are absolute and role-restricted;
- schema rejects token/client-secret/refresh-token/broad-scope/compose/modify/draft/label inputs;
- immutable payload hash changes when recipient, subject, body or attachments change;
- approval or Campaign Grant is bound to the same payload hash;
- daily and per-recipient caps are enforced in the same transaction as outbox eligibility;
- all kill switches block before provider call;
- before-call crash retry does not duplicate;
- after-call timeout moves to reconciliation instead of resend;
- Sent/thread reconciliation confirms timeout-with-success;
- revocation blocks new send and retry;
- at least 20 controlled real sends pass the release matrix before any production claim;
- confirmed duplicate count is zero;
- receipt/audit coverage is 1.00;
- default local and Compose stacks still report Gmail send unavailable when no credentials exist.
