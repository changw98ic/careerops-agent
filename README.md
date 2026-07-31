# CareerOps Agent

CareerOps is a safety-first, single-user career operations assistant. The stack is:

- **Backend** -- Python (FastAPI), PostgreSQL 17, Redis 7.2, Temporal 1.29
- **Frontend** -- Vue 3 SPA (Vite, ant-design-vue, @antv/g2)
- **Orchestration** -- Docker Compose (six services)

All external writes, model providers, Google integrations, and auto-send features are disabled
by default. The application rejects unknown policy actions.

> **Sending terminology (planned, not yet implemented).** CareerOps distinguishes three things:
> (a) the **user confirms inside CareerOps** -- this is the authorization event and the only
> human action required for the system-managed path; (b) the **system sends via the Gmail
> provider on the user's behalf** through the Side-effect Kernel once an approval is durable --
> the user does **not** open Gmail and send manually for this path; (c) **unattended mass apply
> and auto-send remain prohibited** (`CAREEROPS_AUTO_SEND_ENABLED` stays default-off). The
> end-to-end application loop is still Phase 0 (contract freeze); the descriptions below cover
> the M0 stack as shipped today.

---

## Prerequisites

| Tool | Minimum version | Purpose |
|------|----------------|---------|
| Docker Desktop (or Podman) | 4.x | Compose stack |
| Python | 3.12+ | Backend runtime |
| uv | 0.5+ | Python dependency manager |
| Node.js | 20+ | Frontend build |
| npm | 10+ | Frontend dependency install |
| PostgreSQL client (`psql`) | 16+ | Role bootstrap (one-time) |
| GNU Make | 3.81+ | Task runner |

Verify your toolchain:

```bash
docker --version
# Docker version 27.x.x, build xxxxxxx

python3 --version
# Python 3.12.x

uv --version
# uv 0.x.x

node --version
# v20.x.x

npm --version
# 10.x.x

psql --version
# psql (PostgreSQL) 17.x

make --version
# GNU Make 4.x
```

---

## Environment variables

Copy the example file and fill in the values:

```bash
cp .env.example .env
```

The `.env.example` file documents every variable. The critical ones for local development:

```bash
# Database owner (admin, used for migrations and role bootstrap)
CAREEROPS_DB_OWNER_USER=careerops_owner
CAREEROPS_DB_OWNER_PASSWORD=<pick-a-strong-password>

# Database runtime user (the API connects as this user)
CAREEROPS_DB_RUNTIME_USER=careerops_api
CAREEROPS_DB_RUNTIME_PASSWORD=<pick-a-different-password>

# Database name
CAREEROPS_DB_NAME=careerops

# Temporal database credentials
CAREEROPS_TEMPORAL_DB_USER=temporal
CAREEROPS_TEMPORAL_DB_PASSWORD=<pick-another-password>
CAREEROPS_TEMPORAL_DB_NAME=temporal
CAREEROPS_TEMPORAL_VISIBILITY_DB_NAME=temporal_visibility

# Redis password
CAREEROPS_REDIS_PASSWORD=<pick-a-redis-password>

# Local port overrides (optional, defaults shown)
CAREEROPS_API_PORT=8000
CAREEROPS_FRONTEND_PORT=5173
CAREEROPS_TEMPORAL_UI_PORT=8233
```

All passwords are required by Docker Compose and will cause startup failure if missing.

---

## Quick start (Compose)

```bash
# 1. Install Python dependencies
make setup
# uv sync
# (creates venv/ directory)

# 2. Start the full stack; the frontend image builds the Vue app separately
docker compose up --build --wait
# [+] Building ... Done
# [+] Running 6/6
#  ✔ Container careerops-postgres-1        Healthy
#  ✔ Container careerops-redis-1           Healthy
#  ✔ Container careerops-temporal-1        Healthy
#  ✔ Container careerops-migration-1       Exited (0)
#  ✔ Container careerops-api-1             Healthy
#  ✔ Container careerops-temporal-ui-1     Healthy
```

After `docker compose up` succeeds, the stack is:

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://127.0.0.1:5173 | Browser entrypoint; forwards `/api/*` to the API |
| API | http://127.0.0.1:8000 | API/health diagnostics; not the browser UI |
| Temporal UI | http://127.0.0.1:8233 | Loopback only |

---

## Bootstrap and login

The Vue SPA owns both account setup and login. Open `http://127.0.0.1:5173/bootstrap` in both
development and Compose. The frontend keeps browser requests same-origin and forwards only
`/api/*` to the backend. The one-time bootstrap token disables itself after the account exists.
The backend exposes APIs and does not serve frontend HTML.

After login, the SPA dashboard is available at `/dashboard`. The frontend routes are:

| Route | Description |
|-------|-------------|
| `/login` | Login page |
| `/bootstrap` | One-time account setup |
| `/dashboard` | Overview with chart |
| `/jobs` | Job listings |
| `/jobs/:id` | Job detail |
| `/companies` | Company list |
| `/applications` | Application tracker |

---

## Database migration (standalone, outside Compose)

When running the API directly with `make run` instead of Docker Compose, you must bootstrap
the database roles and run migrations manually.

### Step 1: Bootstrap capability roles (one-time, as DB admin)

```bash
psql "$CAREEROPS_DB_OWNER_URL" \
  --file src/careerops/infrastructure/database/bootstrap_roles.sql
# CREATE ROLE
# CREATE ROLE
# CREATE ROLE
# CREATE ROLE
# CREATE ROLE
# CREATE ROLE
# CREATE ROLE
```

This creates seven `NOLOGIN` capability roles. It does not create service logins or grant
memberships.

### Step 2: Run migrations

```bash
CAREEROPS_DATABASE_URL="$CAREEROPS_DB_OWNER_URL" make migrate
# INFO  [alembic.runtime.migration] Context impl PostgresqlDatabase.
# INFO  [alembic.runtime.migration] Will assume transactional DDL.
# INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema
# INFO  [alembic.runtime.migration] Running upgrade 0001 -> 0002, content blobs
# INFO  [alembic.runtime.migration] Running upgrade 0002 -> 0003, audit append function
```

### Step 3: Start the API

```bash
make run
# INFO:     Started server process [12345]
# INFO:     Waiting for application startup.
# INFO:     Application startup complete.
# INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

---

## Verification commands

### Fast gate (CI-style, no Docker)

```bash
make verify
# M-1 contracts ... OK
# Lock check ... OK
# ruff format ... OK
# ruff check ... OK
# pyright ... OK
# pytest ... XX passed, XX% branch coverage
```

This runs contract validation, lock file check, formatter, linter, type checker, and the
local pytest suite with branch coverage (minimum 75%).

### Frontend build check

```bash
make verify-frontend
# cd frontend && npm ci && npm run build
# ...
# Initial JS gzip size: XXXXX bytes (limit: 256000)
```

Fails if the gzipped initial JS bundle exceeds 250 KB.

### Disposable database integration tests

```bash
CAREEROPS_TEST_DATABASE_URL="postgresql://..." make verify-db
# pytest tests/integration ... XX passed
```

Requires a disposable PostgreSQL database. Runs destructive round-trip and database-guard
tests.

### Temporal smoke tests

```bash
make verify-temporal
# pytest tests/unit/test_temporal_smoke.py ... passed
# pytest tests/unit/test_temporal_worker.py ... passed
# pytest tests/integration/test_temporal_recovery.py ... passed
```

### Full Compose verification

```bash
make verify-compose
# docker compose config --quiet
# docker compose up --build --wait
# curl --fail http://127.0.0.1:8000/api/v1/health/ready
# curl --fail http://127.0.0.1:5173/ | grep -q '<div id="app"></div>'
# curl --fail http://127.0.0.1:5173/api/v1/health/live
# docker compose down --volumes --remove-orphans
```

Starts the full stack, checks API health, the frontend shell, and same-origin API forwarding,
then tears down.

### M0 acceptance

```bash
make verify-m0
# Runs: verify + security + verify-db + verify-compose
```

### M1 contract gate

```bash
make verify-m1-contracts
# M-1 contract validation: all structures present
```

### Full M1 gate (expected to fail)

```bash
make verify-m1-full
# FAIL: 0/286 pilot rows, no real D0 evidence
```

This command is expected to fail until real pilot data exists. Do not replace with synthetic
substitutes.

### Dependency audit

```bash
make audit
# No known vulnerabilities found
```

Checks all locked runtime and development dependencies against current PyPI advisories.
Requires network access.

---

## Common errors

### "CAREEROPS_DB_OWNER_USER is required"

Your `.env` file is missing or incomplete.

```bash
# Fix:
cp .env.example .env
# Edit .env and fill in all required values
```

### "port is already allocated"

Another process is using port 8000 or 8233.

```bash
# Find the process:
lsof -i :8000
# COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME
# python    1234 user   5u   IPv4 ...      0t0  TCP 127.0.0.1:http-alt (LISTEN)

# Kill it or change the port in .env:
# CAREEROPS_API_PORT=8001
```

### "relation does not exist" on API startup

Migrations have not been run.

```bash
# If using Compose, the migration service runs automatically. Check its logs:
docker compose logs migration
# If it failed, fix the error and restart:
docker compose up migration

# If running standalone:
CAREEROPS_DATABASE_URL="$CAREEROPS_DB_OWNER_URL" make migrate
```

### Frontend or API proxy unavailable

Check that both the API and frontend services are healthy.

```bash
docker compose ps api frontend
docker compose logs frontend --tail=30
docker compose up --build --wait frontend
```

### "BOOTSTRAP_ALREADY_COMPLETED" or bootstrap page unavailable

The account has already been created. Open the SPA `/login` page instead.

### Redis connection refused

Redis is not healthy. Check:

```bash
docker compose ps redis
# NAME                    STATUS
# careerops-redis-1       Up (healthy)

docker compose logs redis
# If password mismatch, update .env and restart:
docker compose down && docker compose up --build --wait
```

### Alembic "Can't locate revision"

The migration history in the database is out of sync with the code.

```bash
# Check current revision:
uv run alembic current
# (head) 0003

# If stuck, verify against:
uv run alembic history
```

---

## Stop and cleanup

```bash
# Stop all services (preserves data volumes):
docker compose down
# [+] Running 6/6
#  ✔ Container careerops-api-1             Removed
#  ✔ Container careerops-workflow-worker-1 Removed
#  ✔ Container careerops-temporal-ui-1     Removed
#  ✔ Container careerops-temporal-1        Removed
#  ✔ Container careerops-migration-1       Removed
#  ✔ Container careerops-redis-1           Removed
#  ✔ Container careerops-postgres-1        Removed

# Stop and remove all data (fresh start):
docker compose down --volumes --remove-orphans
# [+] Running 6/6
#  ... (same as above, plus:)
#  Volume careerops-postgres-data  Removed
#  Volume careerops-redis-data     Removed
#  Volume careerops-object-data    Removed

# Remove locally built images for this Compose project:
docker compose down --rmi local
```

---

## Currently disabled capabilities

The following features are implemented in code but intentionally disabled at the M0 stage.
They will not activate even if configured:

| Capability | Env variable | Current state |
|------------|-------------|---------------|
| LLM model provider | `CAREEROPS_MODEL_PROVIDER` | `disabled` -- no model calls |
| Google OAuth | `CAREEROPS_GOOGLE_OAUTH_ENABLED` | `false` -- no shared OAuth client present |
| Gmail integration | (part of Google) | No Gmail scope or token |
| Calendar write | (part of Google) | No Calendar API access |
| External writes | `CAREEROPS_EXTERNAL_WRITES_ENABLED` | `false` -- all external write paths reject |
| Auto-send | `CAREEROPS_AUTO_SEND_ENABLED` | `false` -- startup rejection |
| Public webhooks | (no config) | Not implemented |
| Public network binding | `CAREEROPS_BIND_HOST` | Loopback (`127.0.0.1` in standalone, `0.0.0.0` inside Compose network only) |

Two rows deserve explicit terminology to avoid confusion with manual Gmail use:

- **Gmail integration / Gmail send** -- when enabled by a future release, the user **confirms
  inside CareerOps**; the system then sends through the Gmail provider on the user's behalf.
  The user is never required to open Gmail and send manually for this system-managed path.
- **Auto-send** -- unattended sending without per-message user confirmation. This is a
  distinct, separately-gated capability and remains prohibited by default
  (`CAREEROPS_AUTO_SEND_ENABLED=false`); mass apply / batch automation is out of scope.

Enabling any of these requires completing the corresponding security milestone and updating
the ADR index. See [docs/adr/README.md](docs/adr/README.md) and
[docs/security/threat-model.md](docs/security/threat-model.md).

---

## API health endpoints

```bash
# Liveness (always 200 if the process is up):
curl http://127.0.0.1:8000/api/v1/health/live
# {"status":"ok"}

# Readiness (200 only when DB, Redis, and Temporal are connected):
curl http://127.0.0.1:8000/api/v1/health/ready
# {"status":"ok"}

# OpenAPI spec:
curl -s http://127.0.0.1:8000/api/v1/openapi.json | python3 -m json.tool | head -5
# {
#     "openapi": "3.1.0",
#     "info": {
#         "title": "CareerOps",
#         "version": "0.1.0"
```

---

## Project structure

```
career/
├── frontend/           # Vue 3 SPA (Vite + ant-design-vue)
│   ├── src/
│   │   ├── views/      # Dashboard, Jobs, Companies, Applications, Login, Bootstrap
│   │   ├── router.js   # Vue Router with auth guard
│   │   └── stores/     # Session state
│   ├── package.json
│   └── vite.config.js
├── src/careerops/      # Python backend (FastAPI)
│   └── infrastructure/
│       └── database/   # bootstrap_roles.sql
├── migrations/         # Alembic migrations
├── tests/
│   ├── unit/
│   └── integration/
├── deploy/
│   ├── postgres/       # Custom Postgres image with role setup
│   └── temporal/       # Temporal dynamic config
├── docs/
│   ├── acceptance/     # M0 evidence
│   ├── adr/            # Architecture Decision Records
│   ├── runbooks/       # Operational runbooks
│   └── security/       # Threat model
├── scripts/            # Verification and utility scripts
├── docker-compose.yml  # Full stack (6 services)
├── Makefile            # Task runner
├── pyproject.toml      # Python project config
├── alembic.ini         # Alembic config
├── .env.example        # Environment variable template
└── README.md
```

---

## Further reading

- [Local Compose runbook](docs/runbooks/local-compose.md) -- detailed stack, bootstrap,
  login, logout, and cleanup procedure
- [M0 acceptance evidence](docs/acceptance/m0-evidence.md) -- current acceptance status
  and residual risks
- [ADR index](docs/adr/README.md) -- architecture decisions
- [Threat model](docs/security/threat-model.md) -- security boundaries
- [Migrations README](migrations/README.md) -- role and rollback contract
