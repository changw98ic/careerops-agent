# M0 acceptance evidence

- Status date: 2026-07-18
- Evidence scope: current working tree; this repository has no committed `HEAD` yet.
- Current status: engineering evidence is present for M0.1-M0.12, and final local Compose
  verification is complete for the evidence listed below.

This matrix records evidence for the M0 engineering and local-development acceptance
boundary. It does not certify production readiness. Production still requires secret
injection and rotation, TLS/reverse-proxy hardening, backup/restore evidence, release
SBOM/advisory review, and deployment evidence.

## Command evidence

| Command | Result | Evidence |
| --- | --- | --- |
| `make verify` | Passed | Lock check, M-1 contracts, Ruff format/lint and Pyright passed; `343 passed, 16 skipped`; coverage `81.46%` against the enforced `75%` floor. |
| `make security` | Passed | Bandit reported no failed findings; the bounded secret scan passed across 184 repository files. |
| `make audit` | Passed | The lock export resolved 57 package records and `pip-audit --strict` reported no known vulnerabilities. |
| `make verify-m1-contracts` | Passed | `M-1 contracts_only: PASS`; accepted ADRs: 8; D0 evidence schemas: 3; dataset contracts: 9; metrics: 14. |
| `python3 -S scripts/verify_m1.py` | Expected fail | Full gate remains blocked at `pilot_rows_actual: 0`, `pilot_rows_required: 286`; all nine pilot manifests are missing, independent reviewer and adjudicator are unassigned, and PII/secret scan has not passed. |
| Disposable PostgreSQL 17.5 verification | Passed | `17/17` integration tests; migrations `0001 -> 0002 -> 0003`, downgrade to `0002`, re-upgrade to `0003`, and `alembic check`; API function `EXECUTE=true`, direct audit `INSERT=false`. |
| `docker compose config --quiet` with disposable environment values | Passed | Compose syntax and required environment interpolation validate. This is not a container startup proof. |
| Final local Compose verification | Passed | Audited image digest pins; Redis server `7.2.14`; empty-volume start; migration through revision `0003`; API readiness; dashboard; workflow-worker health with exact identity polling both workflow and activity task queues; first five invalid logins accepted by the limiter and sixth returned `429`; API restart remained limited through Redis; 22 targeted tests passed; `docker compose down -v` cleanup left zero residuals. |
| Compose `internal: true` network experiment | Rejected | Tested and rejected because Docker Desktop loopback host port publishing broke. Keep this as an explicit residual egress-isolation risk for later deployment design. |

## M0 matrix

| ID | Plan requirement | Current evidence | Acceptance status |
| --- | --- | --- | --- |
| M0.1 | Python 3.12 project, `uv.lock`, src layout, formatter/linter/type/test/coverage config | `pyproject.toml` defines Python `>=3.12`, project scripts, dependency groups, Ruff, Pyright, pytest, and coverage; `uv.lock` exists; `src/careerops/` and `tests/` exist; `Makefile` has `setup`, `format`, `coverage`, and `verify`; `make verify` passed with 343 passed, 16 environment-gated skips and 81.46% coverage. | Accepted for M0 local engineering evidence. |
| M0.2 | Runtime config loads from environment/secret references and fails closed without leaking secrets | `src/careerops/config.py` validates Redis URL schemes and default-deny capability settings; Compose sets `CAREEROPS_MODEL_PROVIDER=disabled`, `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`, `CAREEROPS_EXTERNAL_WRITES_ENABLED=false`, and `CAREEROPS_AUTO_SEND_ENABLED=false`; `Makefile` includes `security` with Bandit and secret scan. | Accepted for M0 local engineering evidence. |
| M0.3 | API app, health routes, request IDs/traces, `/metrics` | `src/careerops/api/app.py`, `src/careerops/api/routes/health.py`, and `src/careerops/observability/metrics.py` exist; README documents `/api/v1/health/live`, `/api/v1/health/ready`, `/api/v1/openapi.json`, and `/metrics`; final local verification covered API readiness. | Accepted for M0 local engineering evidence. |
| M0.4 | Alembic/PostgreSQL baseline with minimum business, auth, outbox, audit, and credential-reference schema | `migrations/versions/0001_create_initial_business_schema.py`, `0002_console_auth.py`, and `0003_harden_audit_append.py` exist; `docs/security/database-capability-matrix.md` records the current 25-table M0 boundary. A disposable PostgreSQL 17.5 run passed all 17 integration tests and the `0003 -> 0002 -> 0003` migration cycle. | Accepted for M0 local engineering evidence. |
| M0.5 | Append-only audit/payload/application-event controls; runtime roles cannot mutate history | Revision `0003` creates `careerops.append_audit_event(...)` as `SECURITY DEFINER`, computes the SHA-256 chain under an advisory transaction lock, revokes direct API audit `INSERT` and sequence access, and grants API only function `EXECUTE`. `PostgresAuditWriter` calls that function. The real PostgreSQL run proved API `EXECUTE=true`, direct audit `INSERT=false`, and serialized concurrent appends. | Accepted for M0 local engineering evidence. |
| M0.6 | Policy engine defaults unknown/external actions to deny or approval | `src/careerops/policy/engine.py` and side-effect tests exist; README states model, Google integration, and every external write are disabled by default; 22 targeted tests passed in final local verification. | Accepted for M0 local engineering evidence. |
| M0.7 | Temporal workflow/worker skeleton with restart/replay smoke evidence | `src/careerops/workflows/smoke.py`, `src/careerops/infrastructure/temporal/worker.py`, and `health.py` exist; `make verify-temporal` runs smoke, worker, and recovery tests; final local verification proved exact worker identity health for both workflow and activity pollers. | Accepted for M0 local engineering evidence. |
| M0.8 | Outbox publisher and disabled side-effect worker cannot perform provider writes | `src/careerops/infrastructure/database/outbox.py` limits M0 event types to internal-only values; `src/careerops/application/side_effects.py` is unconditionally disabled without provider adapter/token/release capability; tests cover the disabled boundary. | Accepted for M0 local engineering evidence. |
| M0.9 | Single-user console auth: CLI bootstrap, Argon2id, rotating sessions, CSRF, secure cookies, shared limiter | `careerops-bootstrap` script is registered; auth service sets 15-minute bootstrap and PREAUTH TTLs; PREAUTH sessions are bounded and rotated into authenticated sessions; Redis fixed-window limiter keys contain only action plus SHA-256 subject hash and fail closed on backend/protocol errors; final local verification confirmed the first five invalid logins were accepted by the limiter, the sixth returned `429`, and an API restart remained limited via Redis. | Accepted for M0 local engineering evidence. |
| M0.10 | Web dashboard shell, error pages, navigation, CSP, health/integration/pending-count display | `src/careerops/web/` and `src/careerops/infrastructure/dashboard.py` exist; README and runbook document `/bootstrap`, `/login`, and the session-protected root dashboard; final local verification covered the dashboard. | Accepted for M0 local engineering evidence. |
| M0.11 | Local Compose topology: API, workflow-worker, PostgreSQL, Redis, Temporal, Temporal UI; no Browser Worker | `docker-compose.yml` defines exactly `postgres`, `redis`, `temporal`, `temporal-ui`, `migration`, `api`, and `workflow-worker`; no Browser Worker exists; API and Temporal UI are loopback-published; PostgreSQL/Redis/Temporal remain internal. All external images are digest-pinned and audited: Redis `7.2.14-alpine3.21`, Temporal auto-setup `1.29.6`, Temporal UI `2.34.0`, app base `uv:0.11.16`, Python `3.12.11-slim-bookworm`, and PostgreSQL `17.5-alpine3.22`. | Accepted for M0 local Compose evidence. |
| M0.12 | CI for lint, format check, typecheck, unit tests, migration/integration, security and secret scans | `.github/workflows/ci.yml` pins GitHub actions by commit SHA; quality runs `make verify`, Bandit, and secret scan; PostgreSQL job bootstraps roles and runs `make verify-db`; Compose job runs config validation, `docker compose up --build --wait`, health/login probes, and asserts six running services. | Local evidence accepted; live CI run evidence is outside this working-tree pass. |

## Explicit non-claims

- Logical-object runtime retirement and state-aware reads are not M0 blockers. They are the
  M1.13/post-M0 boundary for full raw-document purge semantics.
- `make verify-m1-full` is expected to fail until real D0 pilot evidence exists. Current D0
  remains `0/286`; synthetic or mock data must not be substituted.
- M-1 evidence is limited to `d0_pilot_engineering_consistency`, with
  `release_qualification_allowed=false`. M0/M-1 local evidence does not authorize release,
  Release Qualification, or Auto-send; M7 requires separate trusted human, legal, privacy,
  custody, security, and product release evidence.
- The Redis server image is pinned to `redis:7.2.14-alpine3.21@sha256:dfa188...`, a Redis
  7.2 line image under the BSD-3-Clause Redis server licensing boundary. The Python `redis`
  client remains a separate MIT dependency in `uv.lock`.
- `internal: true` on the Compose backend network was tested and rejected because Docker
  Desktop loopback host port publishing broke. Treat this as a residual egress-isolation risk,
  not as a cleared production network control.
