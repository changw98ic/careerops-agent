# Local M0 Compose runbook

## Purpose and boundary

This is a disposable, loopback-only development stack. It starts PostgreSQL, Redis, Temporal,
Temporal UI, a one-shot migration service, the API, and the workflow worker. It is not a
production deployment procedure: use unique secrets through an approved secret mechanism,
TLS, SBOM/advisory review for pinned images, backup/restore evidence, deployment evidence,
and a reviewed reverse-proxy design before exposing any endpoint beyond the local machine.

The only host-published services are API on `127.0.0.1:8000` (or
`CAREEROPS_API_PORT`) and Temporal UI on `127.0.0.1:8233` (or
`CAREEROPS_TEMPORAL_UI_PORT`). PostgreSQL, Redis, and the Temporal server remain on the
internal Compose network.

The external images are pinned by digest in `docker-compose.yml`, `Dockerfile`, and
`deploy/postgres/Dockerfile`. The Redis server image is pinned to
`redis:7.2.14-alpine3.21@sha256:dfa18828cbc07b3ae6a95ec7343f6c214fdee2d836197b4be8e9904420762cd8`,
which keeps the Compose server on the Redis 7.2 BSD-licensed line.

> **Scope note on email sending.** This runbook starts the M0 stack with all external writes,
> Gmail integration, and auto-send disabled. The planned end-to-end application loop (still
> Phase 0, not yet implemented here) uses a **system-managed send** path: the user **confirms
> inside CareerOps** (the authorization event), and the system sends via the Gmail provider on
> the user's behalf. The user is **not** expected to open Gmail and send manually for that
> path. Unattended mass apply and auto-send remain prohibited regardless of configuration.

## Required environment

Create a local ignored environment file or export each value. Do not commit a real secret. All
values below are required by Compose except the two optional host-port overrides.

```bash
export CAREEROPS_DB_OWNER_USER=careerops_owner
export CAREEROPS_DB_OWNER_PASSWORD='replace-with-disposable-owner-password'
export CAREEROPS_DB_NAME=careerops
export CAREEROPS_DB_RUNTIME_USER=careerops_runtime
export CAREEROPS_DB_RUNTIME_PASSWORD='replace-with-disposable-runtime-password'
export CAREEROPS_REDIS_PASSWORD='replace-with-disposable-redis-password'
export CAREEROPS_TEMPORAL_DB_USER=temporal_runtime
export CAREEROPS_TEMPORAL_DB_PASSWORD='replace-with-disposable-temporal-password'
export CAREEROPS_TEMPORAL_DB_NAME=temporal
export CAREEROPS_TEMPORAL_VISIBILITY_DB_NAME=temporal_visibility

# Optional when the defaults are occupied; both remain bound to 127.0.0.1.
export CAREEROPS_API_PORT=8000
export CAREEROPS_TEMPORAL_UI_PORT=8233
```

The owner credential is used only by PostgreSQL initialization and the migration service. API
and workflow worker use the separate single-capability runtime login. Do not reuse these values
for a non-disposable database.

## Start and verify

```bash
docker compose config --quiet
docker compose up --build --wait
docker compose ps
```

After `docker compose ps` shows all services healthy, run the verification sequence below.

### Database readiness check

PostgreSQL readiness has two layers: the container healthcheck (`pg_isready`) and the
application-level Alembic migration.

**Container healthcheck.** The `postgres` service passes `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB`
every 5 seconds with a 10-second start period and 20 retries. A green `healthy` state in
`docker compose ps` means PostgreSQL is accepting connections on the internal network.

**Migration service.** The `migration` service runs `alembic upgrade head` with the owner
credential and exits. Its `depends_on` uses `condition: service_completed_successfully`, so
the API will not start until migrations finish. Check migration outcome:

```bash
docker compose ps migration          # status should be "exited (0)"
docker compose logs migration        # look for "Running upgrade" lines ending at the head revision
```

A non-zero exit means a migration failed. See Troubleshooting below.

**Application-level readiness.** The `/api/v1/health/ready` endpoint verifies all runtime
dependencies, not just PostgreSQL connectivity. It confirms the database connection pool is
live, Redis responds, Temporal is reachable, and the storage directory exists. A green Compose
health state is not a substitute for this check because the API has dependencies outside its
process.

```bash
curl --fail --silent http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/api/v1/health/ready
```

A successful response confirms all subsystems are ready.

### Full verification sequence

```bash
# 1. Confirm all containers report healthy
docker compose ps

# 2. Check migration completed
docker compose ps migration
docker compose logs migration --tail=5

# 3. Check database readiness via application endpoint
curl --fail --silent http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/api/v1/health/ready

# 4. Verify the SPA is served at the login route
curl --fail --silent http://127.0.0.1:${CAREEROPS_API_PORT:-8000}/login | grep -q 'CareerOps</title>'

# 5. Verify Temporal UI is reachable
curl --fail --silent http://127.0.0.1:${CAREEROPS_TEMPORAL_UI_PORT:-8233}/

# 6. Check workflow worker is polling (optional, for worker verification)
docker compose logs workflow-worker --tail=10 | grep -i 'poll\|task'
```

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

For a clean, self-contained validation that starts and tears down the stack automatically:

```bash
make verify-compose
```

`make verify-compose` runs `docker compose config --quiet`, brings up the stack with
`--build --wait`, checks `/api/v1/health/ready`, checks `/login` serves the SPA, and tears
down volumes on exit. `make verify-m0` additionally requires `CAREEROPS_TEST_DATABASE_URL` for
a separate disposable PostgreSQL integration database.

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

## Troubleshooting

### Migration service fails with exit code 1

**Symptom.** `docker compose ps migration` shows `exited (1)` and the API never starts.

**Common causes:**
- Missing or incorrect `CAREEROPS_DB_OWNER_USER` / `CAREEROPS_DB_OWNER_PASSWORD` -- the
  migration runs with the owner credential, not the runtime credential.
- PostgreSQL not yet accepting connections when migration starts. The `depends_on` with
  `service_healthy` should prevent this, but if the healthcheck start period was reduced,
  the first connection attempt may race.
- Schema conflict from a prior non-clean run.

**Fix:**

```bash
docker compose logs migration --tail=30
# Read the specific Alembic or psycopg error, then:
docker compose down --volumes --remove-orphans
docker compose up --build --wait
```

### API healthcheck keeps restarting

**Symptom.** `docker compose ps` shows the `api` service cycling through `unhealthy` / `restarting`.

**Common causes:**
- Migration did not complete successfully (check `migration` service logs).
- Redis is not healthy -- the API healthcheck pings Redis; a password mismatch or stale
  volume will block it.
- Temporal not yet ready. The `depends_on` block waits for `service_healthy`, but the
  healthcheck retries may be exhausted if Temporal startup is slow on your machine.

**Fix:**

```bash
docker compose logs api --tail=30
docker compose ps redis temporal
```

If Temporal keeps timing out, increase its `healthcheck.retries` temporarily (the default
is 30 retries at 10-second intervals = 5 minutes max wait).

### Port conflict: `bind: address already in use`

**Symptom.** `docker compose up` fails with a port-binding error for 8000 or 8233.

**Fix:**

```bash
# Find the occupying process
lsof -i :8000
lsof -i :8233

# Override the port
export CAREEROPS_API_PORT=8001
export CAREEROPS_TEMPORAL_UI_PORT=8234
docker compose up --build --wait
```

Both ports remain bound to `127.0.0.1`; they are never exposed to the network.

### `docker compose down -v` leaves residual volumes

**Symptom.** After teardown, `docker volume ls` still shows `careerops_*` volumes.

**Cause.** Docker Desktop sometimes retains volumes if a container was force-removed outside
Compose.

**Fix:**

```bash
docker compose down --volumes --remove-orphans
docker volume prune -f
```

The second command removes all unused volumes system-wide. Use with caution if you run other
Compose projects.

### Redis authentication error in API logs

**Symptom.** API logs show `NOAUTH Authentication required` or `WRONGPASS`.

**Cause.** The `CAREEROPS_REDIS_PASSWORD` value differs between the Redis service (started
from the environment) and the API service (read from the shared environment block). This
happens when the env file was changed after the Redis container was created.

**Fix:**

```bash
docker compose down --volumes --remove-orphans
docker compose up --build --wait
```

A full volume teardown ensures Redis restarts with the current password.

### Temporal UI shows "Namespace not found"

**Symptom.** The Temporal UI loads but displays a namespace error.

**Cause.** Temporal auto-setup registered the `default` namespace before the Temporal database
was fully migrated, or the Temporal database user lacks permissions.

**Fix:**

```bash
docker compose logs temporal --tail=30 | grep -i 'namespace\|error'
docker compose restart temporal
```

If the error persists, check that `CAREEROPS_TEMPORAL_DB_USER` has `CREATEDB` privileges
(the Compose init grants them) and that `SKIP_DB_CREATE` is `true` (the CareerOps
PostgreSQL init creates the Temporal databases).

### Workflow worker unhealthy

**Symptom.** `docker compose ps workflow-worker` shows `unhealthy`.

**Cause.** The worker depends on both `migration` and `temporal`. If either is not healthy,
the worker cannot start. The `careerops-worker-health` binary checks that the configured
worker identity is polling tasks.

**Fix:**

```bash
docker compose logs workflow-worker --tail=20
# Verify temporal is healthy:
docker compose ps temporal
# If temporal just restarted, wait for healthcheck to pass:
docker compose ps -w temporal=healthy
```

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
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" make verify-m0
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
