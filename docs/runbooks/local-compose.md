# Optional local Compose compatibility runbook

## Purpose and boundary

This is an optional disposable, loopback-only compatibility stack. The normal macOS runtime uses
Homebrew binaries and user LaunchAgents as documented in
[`local-homebrew.md`](local-homebrew.md); the presence of `docker-compose.yml` does not make Docker
a runtime prerequisite or default. This compatibility path starts PostgreSQL, Redis, Temporal,
Temporal UI, a one-shot migration service, the API, the workflow worker, and the crawler outbox
publisher. It is not a production deployment procedure: use unique secrets through an approved
secret mechanism, TLS, SBOM/advisory review for pinned images, backup/restore evidence,
deployment evidence, and a reviewed reverse-proxy design before exposing any endpoint beyond
the local machine.

The manifest publishes API, Temporal UI, PostgreSQL, Redis and the Temporal server only on host
loopback. Their ports are controlled by `CAREEROPS_API_PORT`, `CAREEROPS_TEMPORAL_UI_PORT`,
`CAREEROPS_POSTGRES_PORT`, `CAREEROPS_REDIS_PORT` and `CAREEROPS_TEMPORAL_PORT`. None is exposed on
a public interface; the containers also share the internal Compose network.

The external images are pinned by digest in `docker-compose.yml`, `Dockerfile`, and
`deploy/postgres/Dockerfile`. The Redis server image is pinned to
`redis:7.2.14-alpine3.21@sha256:dfa18828cbc07b3ae6a95ec7343f6c214fdee2d836197b4be8e9904420762cd8`,
which keeps the Compose server on the Redis 7.2 BSD-licensed line.

## Required environment

Create a local ignored environment file or export each value. Do not commit a real secret. The
credentials and database names below are required by Compose. `CAREEROPS_CRAWLER_WORKSPACE_ROOT`,
`CAREEROPS_CRAWLER_OUTBOX_MAX_ATTEMPTS`, and the two host-port overrides are optional settings;
the values shown document their local defaults.

```bash
export CAREEROPS_DB_OWNER_USER=careerops_owner
export CAREEROPS_DB_OWNER_PASSWORD='replace-with-disposable-owner-password'
export CAREEROPS_DB_NAME=careerops
export CAREEROPS_DB_RUNTIME_USER=careerops_runtime
export CAREEROPS_DB_RUNTIME_PASSWORD='replace-with-disposable-runtime-password'
export CAREEROPS_DB_WORKFLOW_USER=careerops_workflow_runtime
export CAREEROPS_DB_WORKFLOW_PASSWORD='replace-with-disposable-workflow-password'
export CAREEROPS_DB_OUTBOX_USER=careerops_crawler_outbox
export CAREEROPS_DB_OUTBOX_PASSWORD='replace-with-disposable-outbox-password'
export CAREEROPS_DB_MAILBOX_USER=careerops_mailbox_runtime
export CAREEROPS_DB_MAILBOX_PASSWORD='replace-with-disposable-mailbox-password'
export CAREEROPS_DB_MAIL_SENDER_USER=careerops_mail_sender_runtime
export CAREEROPS_DB_MAIL_SENDER_PASSWORD='replace-with-disposable-mail-sender-password'
export CAREEROPS_DB_GREENHOUSE_SENDER_USER=careerops_greenhouse_sender_runtime
export CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD='replace-with-disposable-greenhouse-sender-password'
export CAREEROPS_REDIS_PASSWORD='replace-with-disposable-redis-password'
export CAREEROPS_TEMPORAL_DB_USER=temporal_runtime
export CAREEROPS_TEMPORAL_DB_PASSWORD='replace-with-disposable-temporal-password'
export CAREEROPS_TEMPORAL_DB_NAME=temporal
export CAREEROPS_TEMPORAL_VISIBILITY_DB_NAME=temporal_visibility
export CAREEROPS_CRAWLER_WORKSPACE_ROOT=/app
export CAREEROPS_CRAWLER_OUTBOX_MAX_ATTEMPTS=3

# Optional when the defaults are occupied; both remain bound to 127.0.0.1.
export CAREEROPS_API_PORT=8000
export CAREEROPS_TEMPORAL_UI_PORT=8233
```

Compose derives the console Host and Origin allowlists from `CAREEROPS_API_PORT`, so a custom
loopback API port remains usable without weakening origin validation.

The owner credential is used only by PostgreSQL initialization and the migration service. API
and workflow worker use the separate single-capability runtime login. The crawler outbox publisher
uses its own `CAREEROPS_DB_OUTBOX_USER` login with only the `careerops_outbox` capability. Do not
reuse these values for a non-disposable database.

The Gmail read-only and Gmail send workers use separate `CAREEROPS_DB_MAILBOX_USER` and
`CAREEROPS_DB_MAIL_SENDER_USER` logins. These disposable database passwords are required for
Compose interpolation even while the optional Gmail profiles remain disabled; they are not Gmail,
OAuth or broker credentials.

The optional reviewed Greenhouse submit worker uses the separate
`CAREEROPS_DB_GREENHOUSE_SENDER_USER` login and only the `careerops_greenhouse_sender` capability.
The required values above are still disposable database credentials; they are not Greenhouse API
keys. Do not put a Job Board API key, Basic Auth header or employer credential into `.env`.

## Start and verify

```bash
docker compose config --quiet
docker compose up --build --wait
docker compose ps
curl --fail --silent http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/api/v1/health/ready
curl --fail --silent http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/login | \
  grep -q '<title>登录 · CareerOps</title>'
```

`/api/v1/health/ready` reports database, Redis, Temporal, and storage readiness. A green
Compose health state is not a substitute for checking this endpoint because the API has
dependencies outside its process.

The Temporal container healthcheck uses the current CLI form:

```bash
temporal operator cluster health --address temporal:7233
```

The latest documented local acceptance run on 2026-07-18 passed empty-volume startup,
migration through revision `0003`, API readiness, the dashboard check, audited image digest
pins, Redis server `7.2.14`, 22 targeted tests, and `docker compose down -v` cleanup with
zero residual state. The workflow worker has a dedicated `careerops-worker-health`
healthcheck; the run verified the configured worker identity was polling both workflow and
activity tasks.

That historical acceptance record predates the crawler execution control-plane migrations. The
current `crawler-outbox` service starts only after the Compose migration service completes; a
non-Compose deployment must apply schema revision `0007` (or `alembic upgrade head`) before it
starts a matching crawler publisher.

The `crawler-outbox` service runs `careerops-crawler-outbox --root /app --poll-seconds 5` with
`CAREEROPS_CRAWLER_WORKSPACE_ROOT=/app` and the shared `crawler-datasets` volume mounted at
`/app/datasets`. Override `CAREEROPS_CRAWLER_OUTBOX_LIMIT`,
`CAREEROPS_CRAWLER_OUTBOX_LEASE_SECONDS`, or `CAREEROPS_CRAWLER_OUTBOX_POLL_SECONDS` to tune
batch size, lease length, or polling interval. `CAREEROPS_CRAWLER_OUTBOX_MAX_ATTEMPTS` controls
only the safe pre-execution retry budget; it defaults to `3` and must be between `2` and `10`.
The publisher delays a retry by five minutes when the reviewed request/approval artifact or shared
volume is temporarily unavailable before the local crawler claim is created. It does not retry a
claimed crawl. A known executor exit of `1`, rejected reviewed execution, or exhausted
pre-execution artifact retries is a deterministic `failed` result and moves the outbox event/action
intent to `failed`. A preexisting claim, runner exception, or unexpected post-claim result records
`CRAWLER_EXECUTION_RECONCILIATION_REQUIRED`: the outbox event is `failed`, while the action intent
and append-only `crawler_execution_results` row are `reconciliation_required`. Success records
`succeeded` and moves them to `published`/`confirmed`. For a terminal recovery, preserve the
result/error code and immutable claim/snapshot/receipt evidence. Reconcile an uncertain outcome
before deciding whether a replacement crawl is safe; then create and console-approve a new request
if appropriate. Never reset the old event or reuse its approval artifact.

For a clean, self-contained validation that starts and tears down the stack automatically:

```bash
make verify-compose
```

`make verify-compose` uses the currently exported disposable values and deletes its volumes on
exit. The normal `CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0` path is Docker-free;
`CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0-compose` adds this Compose compatibility
stack to the native acceptance sequence.

## Optional reviewed Greenhouse submit profile

The checked-in `greenhouse-submit` profile is an execution worker profile, not a broker or key
provisioning service. By default it starts fail-closed because external writes, auto-submit,
Greenhouse enablement and release attestation are all false:

```bash
docker compose --profile greenhouse-submit config --quiet
docker compose --profile greenhouse-submit run --rm greenhouse-submit \
  python -m careerops.cli.greenhouse_submit status --json
```

Expected disabled output contains `"enabled":false`. That only proves the local safety gate. A
live deployment must provide an external credential broker process that owns the
employer-authorized Job Board API key and exposes sockets at
`CAREEROPS_GREENHOUSE_SUBMIT_CREDENTIAL_BROKER_SOCKET` and
`CAREEROPS_GREENHOUSE_SUBMIT_ATTACHMENT_BROKER_SOCKET`. The worker container mounts only the
opaque `/run/careerops-greenhouse` socket directory; it must not mount API keys, secret files,
object storage or arbitrary provider data.

Do not run `run-once` or `worker` against a live account unless all of these are already true:

- `CAREEROPS_EXTERNAL_WRITES_ENABLED=true`;
- `CAREEROPS_AUTO_SUBMIT_ENABLED=true`;
- `CAREEROPS_GREENHOUSE_SUBMIT_ENABLED=true`;
- `CAREEROPS_GREENHOUSE_SUBMIT_RELEASE_ATTESTED=true`;
- production deployments also set `CAREEROPS_GREENHOUSE_SUBMIT_IN_PRODUCTION_ATTESTED=true`;
- the target employer explicitly created or authorized the Job Board API key for this integration;
- a controlled broker smoke and employer-authoritative reconciliation path have passed.

Even with every switch enabled, a bounded Greenhouse 2xx is only `accepted_unverified`; it still
requires reconciliation before any `confirmed` application state.

## First-owner bootstrap and normal sign-in

After the migration service has completed and API is healthy, issue a one-time bootstrap token
from a local shell or via the API container. The raw token is printed once; do not paste it into
logs, shell history, tickets, or source control.

```bash
# On a host configured with the same database/runtime environment:
make bootstrap

# Or, while the Compose stack is running:
docker compose run --rm api careerops-bootstrap
```

The token expires in 15 minutes and is invalid after use. Open
`http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/bootstrap`, provide the token, choose a canonical
username (`[a-z0-9][a-z0-9._-]{2,63}`), and choose a password. Completion creates the sole
owner account and rotates the pre-authentication cookie into an authenticated session.

For subsequent access, open `/login` and sign in with that username and password. The root
dashboard is session-protected. Use its CSRF-protected **Log out** form to end the session;
logout revokes the server-side session and clears the browser cookies. Lost-password recovery
is intentionally not an email flow; it needs a local console/host recovery procedure before it
is enabled.

The console accepts only its configured Host and Origin allowlists. It uses `HttpOnly`,
`SameSite=Strict` cookies and requires Secure cookies when configured for HTTPS. Do not add
public hostnames or a reverse proxy without extending the security design and its tests.
Bootstrap, login, and logout throttling uses a shared Redis fixed window. Redis and protocol
errors deny the request. The limiter key is derived from `request.client.host`; it deliberately
does not trust `X-Forwarded-For` or related client-supplied forwarding headers.
The latest local acceptance run verified that the first five invalid login attempts were
accepted by the limiter, the sixth returned `429`, and an API restart remained limited by the
shared Redis state.

## Cleanup

To stop containers while retaining local data:

```bash
docker compose down --remove-orphans
```

To remove all disposable state, including PostgreSQL, Redis, and object volumes:

```bash
docker compose down --volumes --remove-orphans
```

The second command is destructive for this local stack only. It does not replace a production
data-retention or backup procedure.

## Acceptance commands and remaining risks

```bash
make verify                 # also enforces branch coverage >=75%; latest local run ~77.7%
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" make verify-db
make verify-temporal
make security
make audit                  # requires network access
make verify-compose
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0-compose
make verify-m1-contracts
make verify-m1-full         # expected to fail until real D0 pilot evidence exists
```

The former process-local auth limiter and direct API audit-table `INSERT` risks are closed:
auth requests share a fail-closed Redis limiter, and database-owned
`careerops.append_audit_event(...)` performs serialized hash-chain appends behind API
`EXECUTE` permission. Revision `0003` still requires a lockstep application/schema deployment
or a deliberately split two-phase migration; it is not rolling-compatible as written.

Risks that remain open are: at-least-once Temporal Activities needing idempotency and
reconciliation; missing production secret/TLS, restore, SBOM/advisory and deployment evidence;
Compose `internal: true` network isolation tested and rejected because Docker Desktop
loopback host port publishing broke; logical-object retirement and state-aware reads deferred
to the M1.13/post-M0 boundary; and the M1 D0 gate at **0/286 real pilot rows**. Do not
represent this stack as clearing those risks.
