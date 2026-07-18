# CareerOps Agent

CareerOps is a safety-first, single-user career operations assistant. The repository is being
implemented from the risk-closed MVP plan in
[`.omx/plans/careerops-agent-mvp-plan.md`](.omx/plans/careerops-agent-mvp-plan.md).

Current implementation status:

- M-1 architecture, security, metric and dataset contracts are materialized.
- `make verify-m1-contracts` passes. This validates contract structure only.
- `make verify-m1-full` intentionally fails at **0/286 pilot rows**: all nine pilot manifests
  are absent, the independent reviewer and adjudicator are unassigned, the PII/secret scan is
  false, and Release Qualification remains false.
- M0 API/config/policy scaffolding is default-deny: model, Google integration and every external write are disabled.
- The PostgreSQL baseline now defines 25 business and console-authentication tables, eight append-only guards, three
  active-blob reference guards, content-blob and Outbox state guards, and five hardened
  `NOLOGIN` capability roles. On every pool checkout, the runtime engine rejects elevated,
  schema-owning, inheriting or multi-capability logins, then rebinds the one released role and
  bounded search path; real PostgreSQL verification remains a separate command.
- Physical `content_blobs` are separate from per-owner/per-TTL logical `content_objects`.
  Local storage uses SHA-256 addresses, bounded atomic writes and descriptor-relative,
  no-follow I/O. The implemented write path is the smaller `put -> register` protocol: a
  failed registration does not blindly delete possibly shared bytes, and a grace-bounded
  reconciler verifies and tombstones an unknown orphan before deletion.
- Retention commits a fresh opaque fencing token before filesystem deletion and persists the
  idempotent result afterward. Runtime logical-object retirement and state-aware reads remain
  the M1.13/post-M0 boundary for full raw-document purge semantics, not an M0 blocker.
- The transactional Outbox repository and lease-based publisher skeleton exist, but no
  delivery adapter is enabled; a disabled publisher does not claim rows. M0 event types are
  internal-only, and the Side-effect Worker has no provider adapter or token and always
  returns `CAPABILITY_NOT_RELEASED`.
- `/metrics` uses an application-local Prometheus registry with bounded labels; build,
  capability, HTTP and operational series are exposed without the library's default process
  collectors.
- M0 now includes Redis and a deterministic Temporal smoke workflow, a six-service
  loopback-only Compose stack, and a single-user web console. The Redis server is pinned to
  Redis 7.2.14, the BSD-licensed Redis line, by image digest. The console is bootstrapped by
  a one-time local CLI token, uses Argon2id password records, rotating opaque sessions,
  bounded 15-minute PREAUTH sessions, CSRF + Host/Origin checks, a shared Redis fixed-window
  limiter, and audited login/logout events. Redis or limiter-protocol failure denies the auth
  attempt, and the limiter derives its client key only from the trusted server-side
  `request.client.host`; forwarded-for headers do not influence it. This is an M0
  implementation slice, not a production release assertion.
- Audit append authority is now inside PostgreSQL revision `0003`: the database-owned
  `SECURITY DEFINER` function serializes appends, computes the SHA-256 chain and inserts the
  row. The API role has `EXECUTE` on that function but no direct audit-table `INSERT` or
  sequence access.
- All external Compose and container base images are digest-pinned. The workflow-worker
  service has a health command that verifies the configured worker identity is polling both
  workflow and activity tasks before Compose treats it as healthy.

## Local setup

```bash
make setup
make run
```

The API binds to `127.0.0.1:8000` by default. Initial endpoints:

- `GET /api/v1/health/live`
- `GET /api/v1/health/ready`
- `GET /api/v1/openapi.json`
- `GET /metrics` (Prometheus exposition; intentionally omitted from OpenAPI)

The console's unauthenticated entry points are `GET /bootstrap` and `GET /login`; the root
dashboard requires a session. See [the local Compose runbook](docs/runbooks/local-compose.md)
for the complete stack, bootstrap, login, logout, and cleanup procedure.

The Makefile exports `UV_PROJECT_ENVIRONMENT=venv`, keeping the editable environment in a
visible directory. This avoids a macOS edge case where Python skips editable-install `.pth`
files that have inherited the hidden flag from `.venv/`, while preserving immediate source
updates during development.

## D0 pilot intake

Use the intake CLI to inspect evidence-derived progress, create explicitly incomplete working
skeletons, and run the frozen verifier:

```bash
make d0-status
D0_DATASET=discovery_parser make d0-scaffold
uv run careerops-d0 --root . scaffold --dataset dedup
make d0-validate  # expected to fail until all 286 real pilot rows are accepted
```

Scaffolds are never added to the pilot plan automatically and are not evidence. Do not put raw
PII, credentials, private correspondence, or unredacted candidate material in this public
repository. The first operational tranche is 35 `discovery_parser` rows plus 60 `dedup` rows;
the full gate remains closed until all nine datasets reach 286 independently reviewed real
rows. Follow the [D0 pilot intake runbook](docs/runbooks/d0-pilot-intake.md) for roles, storage,
review thresholds, and stop rules.

## Verification

```bash
make verify
```

`make verify` runs the M-1 contract-only gate, lock check, formatter check, lint, type check,
and the local pytest suite with branch coverage. Coverage below 75% fails the command; the
latest full local run was about 77.7% (shown as 78% by the rounded terminal summary). It does
**not** run the real-PostgreSQL integration path or the full M-1 data gate. Run those
explicitly:

```bash
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" make verify-db
make verify-temporal
# Requires the Compose environment variables described in docs/runbooks/local-compose.md.
make verify-compose
make verify-m0
make verify-m1-full  # expected to fail until the real D0 pilot evidence exists
```

Run `make audit` separately when network access is available; it checks every locked runtime
and development dependency against current PyPI advisories.

Current M0 acceptance evidence and residual risks are recorded in
[docs/acceptance/m0-evidence.md](docs/acceptance/m0-evidence.md).

## Database migration

Create the NOLOGIN capability roles once as a database administrator, then migrate with the
credential-bearing URL supplied through the environment or `.env`:

```bash
psql "$ADMIN_DATABASE_URL" \
  --file src/careerops/infrastructure/database/bootstrap_roles.sql
CAREEROPS_DATABASE_URL="$MIGRATION_DATABASE_URL" make migrate
```

The bootstrap creates and hardens capability roles only; it does not create service logins or
grant memberships. Provision each runtime login as `NOINHERIT`, non-owner and non-elevated,
with membership in exactly one capability role and no direct object grants. The engine rejects
broader identities. Keep the migration identity distinct from every runtime login.

For a disposable PostgreSQL database only, run the destructive round-trip and database-guard
suite with `CAREEROPS_TEST_DATABASE_URL=... make verify-db`. See
[`migrations/README.md`](migrations/README.md) for the role and rollback contract.

Use `make verify-m1-full` to see the remaining real D0 pilot blockers. A failing full Gate is
expected until those artifacts exist; do not replace them with synthetic-only data.

`make verify-m0` is an acceptance command, not a promise that every production control has
been certified: it runs local code/security checks, disposable PostgreSQL integration tests,
and a temporary Compose stack. Supply only disposable local credentials to that command.

## Safety defaults

- `MODEL_PROVIDER=disabled`.
- No shared Google OAuth client, Gmail scope, public webhook or provider credential is present.
- Public network binding is rejected until the console authentication milestone exists.
- Email send, Calendar write and Auto-send configuration are rejected at startup.
- Unknown policy actions deny.

## Known M0 limits

The following boundaries are deliberately visible and prevent an M0 claim from being read as a
production-ready claim:

- Console auth throttling is shared through Redis and fails closed. Redis is therefore an
  availability dependency for bootstrap/login/logout; outage behavior must remain observable
  and must not be weakened to fail open.
- Revision `0003` closes direct API audit insertion, but it is not a zero-downtime rolling
  migration: old instances fail once `INSERT` is revoked, while new instances require the
  function. Deploy schema and application lockstep, or use a two-phase rollout that revokes
  the legacy grant only after old instances have drained.
- Temporal Activities have at-least-once delivery semantics. The smoke workflow proves restart
  and replay behavior, but every future provider-facing Activity must carry an idempotency and
  reconciliation key.
- Compose is a local development topology. Production still needs secret injection/rotation,
  TLS and reverse-proxy hardening, SBOM/advisory review for the pinned images, backups,
  restore tests, and deployment evidence.
- Compose `internal: true` network isolation was tested and rejected because Docker Desktop
  loopback host port publishing broke; production egress isolation remains a separate design
  and verification item.
- Logical-object retirement and state-aware reads are deferred to the M1.13/post-M0 boundary;
  full raw-document purge semantics are not complete.
- M1 is blocked at the D0 real-data gate: no pilot evidence is present and the full M1 verifier
  must continue to fail rather than accept synthetic substitutes.

See [the ADR index](docs/adr/README.md) and
[threat model](docs/security/threat-model.md) before widening any capability. Report security
issues through the private channel described in [SECURITY.md](SECURITY.md), never through a
public issue containing sensitive evidence.
