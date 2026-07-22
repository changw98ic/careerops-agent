# Homebrew-native macOS runtime

## Runtime contract

Homebrew is the normal local dependency installer. Docker is not required. CareerOps uses the
Homebrew `postgresql@17`, `valkey`, and `temporal` binaries, but it does not use their shared
`brew services` instances. Valkey intentionally remains unlinked so an existing linked Homebrew
Redis installation keeps its commands and services unchanged. The lifecycle resolves
`brew --prefix` instead of assuming the Apple Silicon path, so the same service boundary works
with Intel Homebrew under `/usr/local`. Dedicated user LaunchAgents own isolated endpoints and
state:

| Component | LaunchAgent | Loopback endpoint |
| --- | --- | --- |
| PostgreSQL 17 | `io.careerops.postgresql17` | `127.0.0.1:55432` |
| Redis-compatible Valkey | `io.careerops.redis` | `127.0.0.1:56379` |
| Temporal + UI | `io.careerops.temporal` | `127.0.0.1:7233`, `127.0.0.1:8233` |
| API | `io.careerops.api` | `127.0.0.1:8000` |
| Workflow worker | `io.careerops.workflow-worker` | Temporal task queue |
| Crawler Outbox | `io.careerops.crawler-outbox` | no public listener |
| Gmail broker | `io.careerops.gmail-broker` | owner-only Unix sockets |

The optional `io.careerops.gmail-readonly` worker is installed but does not auto-start. There is no
auto-start Gmail send or Greenhouse submit worker. External writes, auto-send, auto-submit and all
provider release gates remain false in the core services.

## Files and secrets

Source remains in the repository. launchd cannot reliably access repositories under macOS
`Documents` privacy controls, so installation builds a non-editable runtime venv and an APFS
copy-on-write data workspace under:

```text
~/Library/Application Support/CareerOps/
  bin/native_service.sh
  config/runtime.env
  venv/
  postgres/
  redis/
  temporal/
  run/gmail/
  objects/
  workspace/{datasets,scripts}/
  logs/
```

`.env` and `config/runtime.env` are mode `0600`. PostgreSQL uses SCRAM for loopback logins and one
`NOINHERIT` login per capability role; its server timezone is pinned to UTC so audit and decision
timestamps match the tested Compose contract. Valkey serves the Redis protocol using an ACL file that
contains only a SHA-256 password verifier; the plaintext password is not written to a plist,
process argument, service config or log.
Gmail OAuth clients and refresh tokens remain in macOS Keychain.

## Install and start

Create `.env` from `.env.example`, replace every placeholder and keep the isolated host ports
`55432` and `56379`. Then run:

```bash
make native-install
make native-start
make native-status
```

`native-install` installs the Brewfile dependencies, builds the runtime venv, copies the owner-only runtime config,
creates the persistent PostgreSQL/Valkey/Temporal state, and renders secret-free plists under
`~/Library/LaunchAgents`. The first dataset install uses APFS copy-on-write; later installs sync
scripts, manifests, schemas and labeling guides without deleting runtime outputs. When a native
stack is already running, reinstall performs an ordered stop/update/start and restores the optional
Gmail read-only worker to its previous state; it never leaves old application code running against
new migrations.

`native-start` refuses an occupied port unless the listener belongs to the expected CareerOps
LaunchAgent. If a core job is already loaded, it first performs an ordered stop so changed source,
configuration and migrations cannot be mixed with old processes. It then starts infrastructure,
waits for functional health, idempotently repairs database logins, applies `alembic upgrade head`,
then starts the broker, workflow worker, crawler Outbox and API. A second call safely reconciles the
complete stack. `native-restart` is the explicit alias for the same ordered stop/start path.

## Health and logs

```bash
make native-status
curl --fail --silent http://127.0.0.1:8000/api/v1/health/ready
~/Library/Application\ Support/CareerOps/venv/bin/careerops-worker-health
make native-logs
```

The API readiness check covers the exact API database capability, authenticated Redis protocol,
and writable object storage. The Temporal worker health command requires both workflow and
activity pollers under the exact native worker identity. Crawler Outbox requires a live launchd PID
and has durable database results but no separate semantic health endpoint.

The Docker-free M0 acceptance command also creates and removes its own isolated PostgreSQL 17
cluster for destructive database tests:

```bash
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0
```

## Gmail read-only worker

The broker auto-starts and exposes separate read-only, send and attachment sockets. Starting the
broker does not enable provider writes. A fresh database must first have one active console owner,
a candidate and paired broker handles registered with `careerops-gmail-onboarding register-local`.
Only then start the read-only worker:

```bash
make native-gmail-readonly-start
make native-status
```

Stop it with `make native-gmail-readonly-stop`. This worker receives only the read-only broker
socket and forces external writes and auto-send false. The native lifecycle never runs Gmail
`smoke-send`, recovery-send, or a Gmail send worker.

## Stop and data retention

```bash
make native-stop
```

Stop unloads and persistently disables only `io.careerops.*` jobs in reverse dependency order;
`native-start` enables them again. It does not stop or reconfigure another Homebrew
PostgreSQL/Redis service, remove plists, delete state, rotate credentials or purge data. There is
intentionally no purge command; destructive removal requires a separate reviewed operation with an
explicit backup decision.

## Compatibility limits

- Homebrew Temporal currently embeds a local development server with SQLite persistence. It is not
  a production deployment and does not import histories from the Compose PostgreSQL store.
- Homebrew package versions can move ahead of the container compatibility pins. Re-run the native
  restart, readiness and test checks after an explicit package upgrade.
- The runtime data workspace is independent after its initial copy-on-write clone. Reinstall syncs
  code and reviewed configuration, but it does not overwrite raw/derived runtime results.
