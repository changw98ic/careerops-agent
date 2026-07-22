# Gmail OAuth broker runbook

## Purpose and boundary

This runbook covers the host-side Gmail OAuth credential broker used by Gmail read-only sync and
reviewed Gmail send. It documents deployment operations around BYO Google OAuth clients, local
authorization, in-process macOS Security.framework Keychain storage, Unix-domain sockets and
opaque credential handles.

The repository contains Gmail broker clients, broker servers, a redacting broker CLI, a local
macOS Keychain-backed broker host, local onboarding registration and worker contracts. It does
not contain checked-in Google OAuth client secrets, refresh tokens or production Gmail account
evidence.

OAuth grants, same-account qualification and database registration are deployment state, not a
source-controlled readiness claim. Inspect redacted Keychain status with `make gmail-oauth-status`,
then verify native-database registration and worker state separately. Do not report the full email
chain as complete from this document alone.

Official Google references:

- [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2)
- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [OAuth 2.0 Scopes for Google APIs](https://developers.google.com/identity/protocols/oauth2/scopes)
- [OAuth 2.0 Policies](https://developers.google.com/identity/protocols/oauth2/policies)

## Required separation

Use separate broker profiles for read-only sync and send execution:

| Capability | Broker protocol | Socket | Exact scope |
| --- | --- | --- | --- |
| Gmail read-only sync | `careerops.gmail.credential-broker.v1` | `/run/careerops-gmail/broker.sock` | `https://www.googleapis.com/auth/gmail.readonly` |
| Gmail send | `careerops.gmail.send-credential-broker.v1` | `/run/careerops-gmail-send/broker.sock` | `https://www.googleapis.com/auth/gmail.send` |
| Gmail send attachments | `careerops.gmail-send.attachment-broker.v1` | `/run/careerops-gmail-send/attachments.sock` | Not an OAuth scope; returns reviewed attachment bytes only |

Do not combine `gmail.readonly` and `gmail.send` in one credential handle. Strict deployments
should use separate Google Cloud projects or at least separate OAuth client profiles for read-only
and send because each capability has a different release gate, audit trail, revocation path and
blast radius. A send incident must not invalidate or widen the read-only evidence chain, and a
read-only broker compromise must not produce a send-capable token.

## Google Cloud OAuth setup

Create OAuth clients outside this repository:

1. Use a dedicated Gmail account for recruiting/job-search workflow use.
2. Create one Google Cloud project/client profile for `gmail.readonly`.
3. Create a separate Google Cloud project/client profile for `gmail.send`.
4. Configure an External consent screen for personal BYO use.
5. Move long-running deployments to In production before claiming live readiness.
6. Keep Testing mode only for short development. Google documents that External apps in Testing
   receive refresh tokens that expire after 7 days unless scopes are limited to basic profile/OIDC
   scopes; Gmail scopes are outside that exception.

Use Google Desktop OAuth client JSON for the broker import path. Desktop clients support the
installed-app local loopback redirect flow that the checked-in CLI uses. The broker binds the
callback server to `127.0.0.1`, receives `/oauth2/callback`, verifies a single-use `state`, and
uses PKCE with an S256 code challenge. The deployment host must exchange the authorization code
only for the account supplied with `--account-subject`.

Chrome login is not a CareerOps credential. Being signed in to Gmail or Chrome proves only that a
browser profile has a Google session. CareerOps workers need an OAuth access token resolved from a
registered opaque handle through the local broker. Browser cookies, Chrome profiles and plugin
Gmail connectors must not be used as the product runtime provider.

## Host-side broker workflow

The command shape below is verified against the checked-in CLI and Makefile targets. On macOS,
the default host stores client secrets and OAuth credentials with in-process Security.framework
Keychain calls. Core services normally run through the Homebrew-native user LaunchAgents described
in [`local-homebrew.md`](local-homebrew.md). The Keychain broker must run as the logged-in macOS
user unless a deployment supplies an equivalent OS-backed secret boundary.

### Import OAuth clients

```bash
uv run careerops-gmail-broker --json import-client <readonly-desktop-oauth-client-json> --label readonly
uv run careerops-gmail-broker --json import-client <send-desktop-oauth-client-json> --label send
```

The equivalent Makefile targets are:

```bash
GMAIL_READONLY_CLIENT_JSON=<readonly-desktop-oauth-client-json> make gmail-oauth-import-readonly
GMAIL_SEND_CLIENT_JSON=<send-desktop-oauth-client-json> make gmail-oauth-import-send
```

Import must store the client secret outside the repository. On macOS, use in-process
Security.framework Keychain calls for items scoped to the local user or deployment service
account. The Keychain path must stay inside the broker process rather than crossing a command-line
boundary with client secrets or token JSON. The broker may store non-secret metadata such as
profile name, Google Cloud project id, OAuth client id, redirect URI and allowed scope in its own
state, but the client secret must stay in Keychain or an equivalent OS-backed secret store.

This implementation uses the Keychain item's default application ACL for the Python host process;
it does not install a dedicated signed helper or an explicit trusted-application ACL. Combined with
same-UID Unix-socket authorization, that is a local same-user boundary: it prevents container,
repository, argv and cross-user disclosure, but it does not protect against another process already
running as the same macOS user. A hardened multi-process deployment must add a signed helper and
explicit item ACL before claiming a stronger secret-isolation boundary.

The downloaded Google Desktop OAuth client JSON is a cleartext import source, not durable broker
state. After a successful import, move the source JSON to the operator's Trash and keep only the
non-secret metadata plus Keychain/vault evidence. If import fails, leave the file in place so the
operator can inspect and retry without claiming the secret has been secured.

### Authorize with a local loopback callback

```bash
uv run careerops-gmail-broker --json authorize readonly --account-subject jobs-only@example.com
uv run careerops-gmail-broker --json authorize send --account-subject jobs-only@example.com
```

The equivalent Makefile targets use `GMAIL_ACCOUNT_SUBJECT`:

```bash
GMAIL_ACCOUNT_SUBJECT=jobs-only@example.com make gmail-oauth-authorize-readonly
GMAIL_ACCOUNT_SUBJECT=jobs-only@example.com make gmail-oauth-authorize-send
```

The broker should open or print a Google authorization URL, listen on a loopback callback, exchange
the one-time code for tokens, then store only the refresh token and related secret material in
Keychain through the in-process Security.framework path. The operator must explicitly confirm the
Google consent screen account, exact scope and publishing status before the deployment records the
credential as approved. URL generation, browser sign-in or loopback callback receipt alone is not
authorization evidence.

The current `authorize --json` result emits only safe authorization metadata after redaction:

- `ok=true`;
- `scope` as the broker profile selector (`readonly` or `send`);
- `scopes` containing the exact granted Gmail OAuth scope;
- `account_subject`;
- `active`, `account_bound_by_operator` and `live_qualified`;
- safe handle/hash metadata such as `handle_sha256`.

For `readonly`, authorization marks the credential `live_qualified=true` after the broker verifies
the authorized Gmail profile email. For `send`, authorization binds the expected account supplied by
the operator but leaves live qualification to `smoke-send`, which records
`qualification_evidence_sha256`. The authorization result does not emit credential-store evidence,
Google OAuth publishing status or explicit consent evidence. Record those operator-observed facts in
the deployment evidence trail outside the authorization command output.

Use `--no-open-browser` only for a headless host where the operator will open the printed URL
manually. With `--json`, the CLI writes a safe pending authorization event to stderr containing
`authorization_url`; the URL contains `state` and PKCE challenge material but not the code
verifier, client secret, authorization code, refresh token or access token. The CLI also accepts
`--timeout-seconds`, bounded to 0-600 seconds, and `--profile` (`readonly` or `send`) when the
selected profile differs from the scope argument.

Do not paste the authorization code, refresh token, access token or client secret into CareerOps
config, database rows, issue comments, logs or runbooks.

### Serve broker sockets

```bash
uv run careerops-gmail-broker --json serve \
  --readonly-socket /run/careerops-gmail/broker.sock \
  --send-socket /run/careerops-gmail-send/broker.sock \
  --attachment-socket /run/careerops-gmail-send/attachments.sock \
  --attachment-root /absolute/reviewed/object/root
```

For local host smoke, the Makefile target uses repo-local absolute paths under
`data/runtime/gmail` and `data/objects`:

```bash
make gmail-oauth-serve
```

The paired Makefile readonly/send worker targets derive `CAREEROPS_DATABASE_URL` through
`careerops-database-url`, which receives the role-specific login parts through environment
variables and emits an encoded PostgreSQL URL. The recipe short-circuits before worker launch if
URL encoding fails. The final worker process is launched with `env -i`, carrying only `PATH`,
`UV_PROJECT_ENVIRONMENT`, `CAREEROPS_ENVIRONMENT`, `CAREEROPS_CONSOLE_COOKIE_SECURE`, the encoded
`CAREEROPS_DATABASE_URL`, the intended broker socket paths and disabled-by-default
gates/attestations, including Google OAuth production attestation.

The target creates a missing runtime directory under `umask 077`. It deliberately does not
`chmod` an existing directory. It also creates the repo-local attachment object root
`data/objects` under the same `umask 077` boundary when missing. The broker validates existing
socket parents and fails closed if that boundary is unsafe. Although Make loads `.env` for other
host commands, this long-lived broker is exec'd under a minimal environment containing only
`PATH` and `UV_PROJECT_ENVIRONMENT`; it does not inherit database, console, or external-write
secrets.

Socket requirements:

- socket paths must be absolute;
- an existing parent directory must be owned by the broker runtime user, grant no group/world
  permissions, and retain owner write/execute access; the broker rejects an unsafe parent instead
  of changing its permissions, while a parent it creates is mode `0700`;
- socket files should allow only the intended CareerOps runtime role to connect, for example
  owner-only mode `0600` or an equivalent service-manager ACL;
- an adjacent owner-only lock and a liveness probe prevent a second broker from unlinking or
  replacing an active socket; only a same-owner, demonstrably stale socket is removed;
- the read-only worker should reach only `/run/careerops-gmail/broker.sock`;
- the send worker should reach `/run/careerops-gmail-send/broker.sock`,
  `/run/careerops-gmail/broker.sock` for Sent reconciliation, and
  `/run/careerops-gmail-send/attachments.sock` for reviewed attachment bytes;
- broker logs must not include tokens, client secrets, authorization codes or refresh tokens;
- stale sockets must be removed before bind only when the owning broker process is known dead.
- Docker Compose workers can consume mounted Unix sockets, but the checked-in Compose profile does
  not provide the broker, Security.framework Keychain/vault or attachment broker processes. Keep
  host broker services and Docker core workers separated: run broker processes on the host or
  supply deployment-owned services, then mount only the intended sockets before enabling live
  workers.

## Broker protocol contract

The read-only broker client sends one small JSON request and expects one JSON response before the
socket closes:

```json
{
  "version": "careerops.gmail.credential-broker.v1",
  "provider": "gmail",
  "opaque_handle": "gmail:readonly:opaque-handle",
  "account_subject": "jobs-only@example.com",
  "scope": "https://www.googleapis.com/auth/gmail.readonly"
}
```

The send broker client sends:

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

The broker response must contain only a short-lived access-token envelope:

```json
{
  "access_token": "ya29.redacted-runtime-token",
  "account_subject": "jobs-only@example.com",
  "scope": "https://www.googleapis.com/auth/gmail.readonly",
  "expires_at": "2026-07-21T12:30:00+00:00"
}
```

For send, the `scope` value must be `https://www.googleapis.com/auth/gmail.send`. The CareerOps
clients reject mismatched account subjects, wrong scopes, expired tokens, malformed JSON, oversized
responses and broker envelopes containing forbidden secret fields such as `refresh_token`,
`client_secret`, `client_id`, `token_uri` or `private_key`.

## Register opaque handles

After both profiles are live-qualified and send qualification evidence exists, register the paired
local broker handles with the onboarding CLI:

```bash
uv run careerops-gmail-onboarding register-local \
  --account-subject jobs-only@example.com \
  --candidate-display-name "Local Gmail Candidate" \
  --json
```

`register-local` is currently all-or-nothing. It reads the broker's local status, requires both
profiles to be live-qualified, requires send qualification evidence, and requires exact
`gmail.readonly` and `gmail.send` scopes for the same `account_subject`, creates or reuses the
candidate, registers the read-only account first with `publishing_status=testing`, then registers
the send account bound to that read-only account with `requested_status=disabled`. It emits account
ids, command ids and SHA-256 hashes only. It does not print credential handles, client secrets,
refresh tokens or access tokens, and it never enables Google OAuth, external writes, auto-send or
Gmail send gates.

Onboarding derives and persists the credential-store evidence SHA-256 values from broker status:
profile, `account_subject`, exact scope, `active=true`, `live_qualified=true`, whether the OAuth
client is configured, and the credential handle SHA-256. It derives the send release evidence
SHA-256 from the `account_subject`, read-only account id, `requested_status=disabled` and the
send `qualification_evidence_sha256` produced by `smoke-send`. The registration output reports
those hashes, plus credential handle hashes, account ids, command ids and status fields
(`publishing_status=testing` for read-only and `requested_status=disabled` for send); the raw
opaque handles are persisted through the registration providers but are not public CLI output.

A readonly-only grant cannot yet be registered through this command. Complete the separate send
consent and controlled `smoke-send` first; this pairing requirement does not enable send execution,
because the registered send account remains `disabled` and all execution/release gates remain off.

Use `--candidate-id` instead of `--candidate-display-name` when the candidate already exists. Use
`--owner-user-id` when more than one active console owner could exist; the local repository path
otherwise requires exactly one active console owner.

The authenticated internal control-plane APIs can register, list and report status for
Gmail read-only and Gmail send accounts while execution gates remain off. Registration stores
opaque handles and evidence hashes, not OAuth secrets. Keep the send account disabled until the
full send release gate and smoke evidence pass.

Bootstrap the console owner explicitly before registration:

```bash
uv run careerops-bootstrap issue
```

`careerops-bootstrap --help` and `careerops-bootstrap` with no subcommand do not issue a token.
Only the explicit `issue` subcommand creates and prints a one-time bootstrap token.

## Live smoke flow

The OAuth/Keychain broker is a native macOS host process. Docker is not a prerequisite for
authorization, qualification, recovery or the Unix-socket broker. A deployment may run the
database and application workers natively or in containers; that choice is separate from this
broker procedure and must not be inferred from the presence of `docker-compose.yml`.

Paired local qualification followed by read-only smoke:

1. Start the host broker. For local development, use repo-local sockets:

   ```bash
   make gmail-oauth-serve
   ```

2. Complete the separate send OAuth consent and controlled qualification before registration:

   ```bash
   uv run careerops-gmail-broker --json smoke-send --account-subject jobs-only@example.com
   ```

   This is the only controlled send in onboarding. It sends to the same account's plus alias and
   must retrieve the exact Sent message through the readonly profile before qualification succeeds.
   Run it once only, after explicit approval. If the process exits after Gmail may have accepted
   the message, do not invoke `smoke-send` again as a retry.

   Gmail may replace the submitted RFC 822 `Message-ID`. Qualification therefore binds the
   provider message/thread ids and verifies the exact From, To, Subject, body hash and `SENT`
   label through the separate readonly credential; the submitted `Message-ID` is not treated as
   immutable provider evidence.

   If the send may have succeeded but qualification persistence failed, recover from Sent without
   another provider write:

   ```bash
   uv run careerops-gmail-broker --json recover-smoke-send \
     --account-subject jobs-only@example.com
   ```

   Recovery fails closed unless exactly one recent message matches the controlled template. If an
   audited incident has already produced multiple exact candidates, select the reviewed subject
   explicitly instead of choosing the latest message automatically:

   ```bash
   make gmail-oauth-recover-smoke-send \
     GMAIL_ACCOUNT_SUBJECT=jobs-only@example.com \
     GMAIL_EXPECTED_SUBJECT_SHA256=<64-lowercase-hex-digest>
   ```

   Derive and review that digest from the intended Sent message through an authorized local
   operator path. Never place an email address, token, raw subject or provider id in logs or goal
   context when an opaque handle or digest is sufficient.

3. Register the live-qualified opaque handles:

   ```bash
   uv run careerops-gmail-onboarding register-local \
     --account-subject jobs-only@example.com \
     --candidate-display-name "Local Gmail Candidate" \
     --json
   ```

4. Verify the worker status:

   ```bash
   make gmail-readonly-status
   ```

5. Queue a manual sync through the operator API.
6. Run one bounded polling pass, or start the bounded long-running worker after smoke:

   ```bash
   make gmail-readonly-sync
   make gmail-readonly-worker
   ```

7. Confirm only metadata/redacted/hash evidence and reviewed proposals were persisted.

Send smoke:

1. Keep the read-only broker available for Sent/thread reconciliation.
2. Keep the send broker on `/run/careerops-gmail-send/broker.sock` or the repo-local Makefile
   socket from `make gmail-oauth-serve`.
3. Keep the attachment broker on `/run/careerops-gmail-send/attachments.sock` or the repo-local
   Makefile socket when attachments are in scope.
4. Register the send handle with `requested_status=disabled`, then enable only after release
   evidence exists.
5. Create an exact Gmail send draft, review it, reserve capacity and enqueue the outbox event
   through the authenticated API.
6. Verify the worker status:

   ```bash
   make gmail-send-status
   ```

7. Execute one bounded pass against a controlled test recipient, or start the bounded
   long-running worker after smoke:

   ```bash
   make gmail-send-once
   make gmail-send-worker
   ```

8. Run reconciliation:

   ```bash
   make gmail-send-reconcile
   ```

9. Confirm provider receipt or Sent/thread reconciliation evidence before using
   `sent_confirmed`.

10. Confirm that the pre-registration, same-account `smoke-send` qualification evidence is bound
    to the registered send handle and account. Do not run the hook again at this stage.

    Treat the recorded result as evidence only when it verifies the same `account_subject`, exact
    `gmail.send` scope, Keychain/vault refresh path, Google account eligibility and a controlled
    provider send or provider/Sent reconciliation proof. If the original process failed after
    Gmail may have accepted the message, use the read-only `recover-smoke-send` path described
    above. It never sends, does not auto-select the newest of multiple candidates, and must not be
    replaced with another `smoke-send` invocation.

`status --json` proves only the local configuration gate. It does not prove the broker can refresh
tokens, the account exists, Google accepted a provider call or the full email chain is complete.

## Revocation

Revocation has two layers:

- CareerOps account revocation records owner-scoped audit evidence and blocks future sync/send
  work.
- Broker revocation invalidates or deletes OAuth material in the external vault/Keychain.

Broker command template:

```bash
uv run careerops-gmail-broker --json revoke --scope readonly --account-subject jobs-only@example.com
uv run careerops-gmail-broker --json revoke --scope send --account-subject jobs-only@example.com
uv run careerops-gmail-broker --json status --scope readonly
uv run careerops-gmail-broker --json status --scope send
```

After broker revocation:

1. Revoke the read-only or send account through the authenticated CareerOps API.
2. Stop or reload the relevant broker service.
3. Confirm `careerops-gmail-readonly status --json` or `careerops-gmail-send status --json`
   reports the expected disabled/unavailable gate for the revoked capability.
4. For send, reconcile any event that may have called Gmail before revocation; do not resend.

## Troubleshooting

| Symptom | Check | Expected fix |
| --- | --- | --- |
| `credential broker unavailable` | Socket path, service state, parent directory permissions | Start broker, correct absolute socket path, repair owner/ACL |
| `scope mismatch` | Broker profile and Google OAuth client scope | Re-authorize with exactly one allowed scope for that profile |
| `account mismatch` | Handle registered for a different Gmail subject | Register the matching handle/account pair |
| `broker access token expired` | Broker refresh path and Keychain token state | Refresh through broker; re-authorize if refresh token is invalid |
| `invalid_grant` after about 7 days | OAuth consent screen still Testing | Re-authorize for development or move eligible BYO app to In production |
| Worker disabled despite socket paths | Runtime switches and release attestations | Enable the complete gate; do not flip only one switch |
| Send stuck in `reconciliation_required` | Possible after-call ambiguity | Reconcile Sent/thread evidence; never blind-retry |
| Operator says they are logged into Chrome | Browser session is not an OAuth broker credential | Authorize through the broker and register the opaque handle |

## Verification checklist

Before claiming Gmail OAuth broker readiness:

- read-only and send profiles use separate exact scopes;
- strict deployment uses separate Google Cloud projects or separate OAuth client profiles;
- OAuth clients and refresh tokens are stored outside the repo in Keychain or equivalent storage;
- local loopback callback is restricted to loopback and single-use authorization;
- Testing-mode 7-day refresh-token expiry from authorization/re-authorization has been recorded
  or avoided with In production status;
- broker sockets are absolute and owner/role restricted;
- broker request/response protocol matches the CareerOps clients;
- registration uses opaque handles and SHA-256 evidence only;
- live read-only smoke persisted only redacted/hash evidence;
- live send smoke produced receipt or Sent/thread reconciliation evidence;
- revocation blocks future work and removes or invalidates broker-side OAuth material.
