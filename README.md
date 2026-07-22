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
  active-blob reference guards, content-blob and Outbox state guards, and eight hardened
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
- M0 now includes a Redis-compatible state service and a deterministic Temporal smoke workflow, a Homebrew-installed,
  launchd-supervised native macOS stack, an optional loopback-only Compose compatibility stack,
  and a single-user web console. The Compose Redis server is pinned to
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
- G010/G014 durable GoalRun orchestration is a bounded, owner-scoped control plane with two
  immutable modes. Explicit `pre_application_only` ingests reviewed crawler evidence, ranks
  canonical jobs against an exact approved candidate profile version, and creates
  `goal_run_pre_application_review.v1`; approval ends locally as `completed` with
  `completion_kind=pre_application_package_approved`. That branch never runs GoalRun dispatch or
  reconciliation activities, creates no Gmail/application outbox event, and performs no provider
  write. `gmail_dispatch` preserves the existing reviewed Gmail draft/outbox/reconciliation path.
  A missing `mode` deliberately means `gmail_dispatch` for old API clients and Temporal replay; it
  never opts a run into the local-only branch. Migration `0021` installs the atomic
  pre-application review completion rule. A pre-application create request supplies only
  `candidate_id` and `candidate_profile_version_id`: in the same transaction as GoalRun creation,
  the operator verifies that this is the owner's latest approved version with active, unexpired
  material objects, then derives matching configuration from its approved preferences. Callers
  cannot inject or override `match_config`. `goal_run_pre_application_artifact.v2` classifies
  every match as `eligible`, `needs_manual_review`, or `ineligible`: required keywords are ALL-of
  hard gates, as are configured company, industry, seniority, and location allowlists; missing
  evidence becomes manual review. `selectable=true` requires both `eligibility=eligible` and a
  score at or above `min_score`; a `needs_manual_review` review packet instead uses
  `proposed_manual_review_job` with `review_disposition=manual_evidence_required` and never calls
  that proposal `selected_job`. Public ATS row schema v2 carries `source_job_industry` and
  `source_job_industries` into canonical structured `industry` and `industries`. Candidate SQL
  de-duplicates by `canonical_job_id`, retains the latest evidence/version, and only then applies
  `LIMIT`. Approval revalidates the exact active approved profile/material version, hashes, and
  expiry in the review-decision transaction. Migration `0024` also installs a database
  `BEFORE INSERT` guard: a newly inserted pre-application GoalRun must bind the owner's latest
  active approved profile ID/version and exact snapshot/material-bundle hashes; legacy missing-mode
  and explicit Gmail runs retain their existing semantics. These v2 artifact/ingestion/query paths,
  migrations `0022` through `0024`, and the `careerops-candidate-profiles` import/review/inspect
  CLI are installed in the isolated Homebrew-native runtime at database/source head `0024`. The
  first candidate profile
  and material bundle have been imported and remain pending explicit human approval; no GoalRun
  has therefore been created and the native end-to-end path is not yet verified. Source head
  `0023` also adds an
  owner-scoped create-receipt lookup: if PostgreSQL committed a GoalRun but Temporal start failed,
  retrying the
  original idempotency key as the same owner with identical immutable arguments reuses that record
  and safely retries workflow start; changed immutable arguments conflict. None of this enables
  external writes. GoalRun does not
  compose Greenhouse dispatch. See the
  [durable GoalRun runbook](docs/runbooks/durable-goal-runs.md).
- G011 Gmail read-only sync is implemented as a disabled-by-default, exact-read-only mailbox
  signal path: authenticated internal operator routes, owner-scoped PostgreSQL functions,
  a `careerops_mailbox` runtime role, a Unix credential-broker client protocol, a Gmail REST
  client limited to profile/list/get/history reads, a polling worker and the
  `careerops-gmail-readonly` CLI now exist. The local Gmail OAuth broker can import Desktop
  OAuth clients into macOS Keychain, run loopback/PKCE authorization, serve repo-local sockets
  and, after both readonly and send profiles are live-qualified, register their paired opaque
  handles through `careerops-gmail-onboarding register-local`.
  The checked-in Compose profile leaves
  `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`, so the default stack reports the worker as disabled.
  OAuth grants and qualification evidence are host deployment state, not a source-controlled
  readiness claim; inspect them with `make gmail-oauth-status`. The Homebrew-native database starts
  independently and still requires console bootstrap plus explicit handle registration. There is no Watch/PubSub,
  Gmail mutation path in the read-only worker, or production-readiness claim. See the
  [Gmail read-only sync runbook](docs/runbooks/gmail-readonly-sync.md).
- G012/G015 Gmail send now has reviewed outbox control-plane and worker scaffolding, separate
  from the read-only mailbox worker: exact `gmail.send` and `gmail.readonly` broker contracts,
  explicit G011 reconciliation-account binding, canonical reviewed payload hashes, attachment
  Unix-broker resolution, a dedicated `careerops_mail_sender` role, durable
  outbox/attempt/receipt/reconciliation records, ambiguous-state stop and no blind resend. The
  checked-in stack must still report Gmail send as disabled, unconfigured or not qualified until
  the separate send OAuth grant, broker `smoke-send`, DB registration, release evidence and
  execution gates all exist. See the
  [reviewed Gmail send runbook](docs/runbooks/reviewed-gmail-send.md).
- G013 Greenhouse Job Board submit is a reviewed, broker-executed application channel, not a
  universal auto-apply API. Public Greenhouse GET endpoints can discover jobs and schemas, but
  POST is allowed only for a target employer's explicitly created or authorized Job Board API key.
  The fixed destination is `boards-api.greenhouse.io`; the credential broker owns schema re-read,
  Basic Auth construction and the single POST, and it never returns the long-lived API key to a
  worker, database, log or API response. V1 submission is limited to reviewed
  `first_name`/`last_name`/`email`/`phone` plus reviewed resume and cover letter attachments.
  Custom questions, location, website, legal/compliance statements, demographic questions,
  assessments, payment, identity, signatures, CAPTCHA, MFA and account flows stop for manual
  handling. Every bounded 2xx is only `accepted_unverified` and moves to mandatory
  reconciliation with no retry; `confirmed` requires independent employer-authority evidence or
  reviewed human evidence. The `greenhouse-submit` Compose profile and
  `careerops.cli.greenhouse_submit` CLI worker exist, but there is no checked-in live key, broker
  service or smoke evidence. See the
  [reviewed Greenhouse submit runbook](docs/runbooks/reviewed-greenhouse-submit.md) and
  [ADR 0014](docs/adr/0014-reviewed-greenhouse-job-board-submit.md).

## Local setup

```bash
cp .env.example .env
# Replace every placeholder in .env, then:
make native-install
make native-start
make native-status
```

The normal macOS runtime uses Homebrew binaries and user-level `io.careerops.*` LaunchAgents;
Docker is not required. CareerOps owns isolated PostgreSQL/Redis ports and does not modify an
existing Homebrew service on `5432` or `6379`. The native stack uses BSD-licensed Valkey for the
Redis protocol instead of the current AGPL Redis formula. Runtime code and state live under
`~/Library/Application Support/CareerOps` so launchd does not require access to the source tree in
`Documents`. See the [Homebrew native runbook](docs/runbooks/local-homebrew.md). The API binds to
`127.0.0.1:8000` by default. Initial endpoints:

- `GET /api/v1/health/live`
- `GET /api/v1/health/ready`
- `GET /api/v1/openapi.json`
- `GET /metrics` (Prometheus exposition; intentionally omitted from OpenAPI)
- Authenticated internal GoalRun operator routes live under
  `/api/v1/internal/goal-runs`; they require the console session and CSRF boundary.
- Authenticated internal Gmail read-only operator routes live under
  `/api/v1/internal/gmail-readonly`; they also require the console session and CSRF boundary.
  They return unavailable unless the runtime provider is explicitly configured.
- Authenticated internal Gmail send operator routes live under
  `/api/v1/internal/gmail-send`; they register send accounts bound to a G011 reconciliation
  account, create/review exact payload drafts, reserve reviewed intents and inspect send
  reconciliation. Provider send execution still requires the disabled-by-default mail-sender
  worker, host broker/Keychain boundary, exact credentials and live release evidence.
- Authenticated internal Greenhouse reviewed-submit operator routes live under
  `/api/v1/internal/greenhouse-submit`; they register employer-authorized account profiles,
  create/review immutable packets, reserve reviewed intents, and submit/review reconciliation
  evidence. Provider POST execution still requires the separate disabled-by-default worker,
  employer-authorized broker profile and reconciliation evidence.

The console's unauthenticated entry points are `GET /bootstrap` and `GET /login`; the root
dashboard requires a session. The [local Compose runbook](docs/runbooks/local-compose.md) is an
optional container compatibility path, not the normal runtime.

After pulling or changing source, run `make native-install`, not only `make native-restart`.
Installation syncs the isolated runtime payload; startup then applies `alembic upgrade head`
(the source migration head is currently `0024`) before the API and workflow
worker start. `make native-status` proves
process/readiness health, not that a crawler-to-review GoalRun has completed end to end.

Bootstrap tokens are explicit:

```bash
uv run careerops-bootstrap issue
```

`careerops-bootstrap --help` and `careerops-bootstrap` with no subcommand do not issue a token.

The Homebrew-native core stack runs the Keychain broker as `io.careerops.gmail-broker`. Inspect its
stored, redacted credential status with:

```bash
make gmail-oauth-status
make native-status
```

The read-only mailbox worker is deliberately optional because a fresh native database has no
console owner or registered mailbox. After console bootstrap and `register-local`, start only that
read-only worker with `make native-gmail-readonly-start`. The Gmail send worker is not installed as
an auto-start job; all send and external-write gates remain false.

The Gmail host targets build `CAREEROPS_DATABASE_URL` through the `careerops-database-url`
helper and stop before worker launch if URL encoding fails. They launch workers with `env -i`,
preserving the deployment environment/cookie-security boundary, the encoded database URL,
repo-local broker sockets and disabled-by-default gates/attestations. They do not enable Google
OAuth, external writes, auto-send or Gmail send gates.

Only after both OAuth profiles are live-qualified and `smoke-send` has persisted send
qualification evidence, register the paired handles:

```bash
uv run careerops-gmail-onboarding register-local \
  --account-subject jobs-only@example.com \
  --candidate-display-name "Local Gmail Candidate" \
  --json
```

`register-local` is currently all-or-nothing: it requires live-qualified readonly and send
profiles for the same account, registers readonly as `testing`, registers send as `disabled`,
emits ids/hashes only and never enables Google OAuth, external writes, auto-send or Gmail send
gates. A readonly-only grant is therefore not yet sufficient for DB registration. When `.env` is
included by Make, JSON array settings must be
unquoted values such as `CAREEROPS_CONSOLE_ALLOWED_HOSTS=["127.0.0.1:8000","localhost:8000"]`.

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

## Recruitment crawl data processing (local only)

`scripts/process_recruitment_crawl_batches.py` incrementally turns raw recruitment crawl JSONL
into derived signals, a deduplicated signal set, and a company rollup. It reads only complete
lines from a byte snapshot, never starts a crawler or makes a network request, and keeps a
separate watermark for each input crawl output. Use an explicitly private/ignored output path
until derived evidence has been reviewed and redacted:

```bash
uv run python scripts/process_recruitment_crawl_batches.py \
  --crawl-output datasets/raw/recruitment_pages/2026-07-18/run14_sitemap_priority_v1 \
  --output-dir datasets/private/recruitment-processing/2026-07-19/run14
```

For a sharded crawl, pass `--sharded-batch`; shards that have not written a raw JSONL file are
reported as skipped and can be picked up by a later rerun. This is offline preparation only: it
does not authorize M1, recurring crawling, Release Qualification, or Auto-send. See the
[recruitment processing runbook](docs/runbooks/recruitment-data-processing.md) for the complete
operator flow and data-handling constraints.

## Configured recruitment crawlers

The public-recruitment crawler scripts can be run from one validated local JSON manifest with
`careerops-crawl-sources`. It composes sitemap, Common Crawl, public ATS, and first-party-page
collection stages without treating them as fixed external channels. The manifest only references
pre-existing `datasets/` seed artifacts and registered adapters; it cannot supply arbitrary URLs,
proxies, browser settings, credentials, or script paths. See the
[configured crawler runbook](docs/runbooks/configured-recruitment-crawlers.md). The same CLI also
supports an immutable `request` -> `approve` -> `execute` flow so one local review can bind a
high-autonomy crawl to the exact validated manifest, plan, input bytes, dependency bytes, and
registered crawler implementation. `execute` stages private byte-verified snapshots, writes a
single-use claim under `datasets/private/crawler-execution-claims/`, and requires selected
producer dependencies to use fresh output directories and generate output in the same run before
dependent page collection starts; other reviewed outputs are protected by a duration-scoped local
lock. This is a local unauthenticated audit gate, not a cryptographic or server-side human-only
approval boundary.

For the server control plane, use `careerops-crawler-execution request` instead of local
`approve`: it accepts only a manifest under `datasets/manifests/`, selected source IDs, and a
console owner user UUID, then generates the review artifact path itself and stores a pending
console-review request. Scheduler provenance belongs in the reason/audit text. Execution authority
comes from the authenticated console and the filtered `careerops-crawler-outbox` publisher, not from
the local JSON artifact.

For a GoalRun, create the run before creating its crawler execution request. The reviewed-crawler
bridge intentionally considers only requests whose `created_at` is on or after the GoalRun's
`created_at`. A standalone request created earlier can validate request/approval/execution, but it
cannot become discovery evidence for a later GoalRun; create and approve a fresh request, then
resume the blocked run. See the ordering in the
[durable GoalRun runbook](docs/runbooks/durable-goal-runs.md#crawler-request-ordering).

The outbox retries only a pre-execution review-artifact/shared-volume availability failure, before
the local one-shot crawler claim exists. It retries after five minutes and defaults to three
attempts. A known executor exit of `1` or rejected reviewed execution is a terminal `failed`
result. A preexisting claim, runner exception, or other uncertain post-claim outcome is terminal
`reconciliation_required`: the outbox event fails, while the action intent and append-only crawler
result require operator inspection.
Success records `succeeded` and transitions the event/action intent to `published`/`confirmed`.
Reconcile uncertain outcomes before deciding whether a fresh request plus authenticated approval is
safe; never force an old event back to pending or reuse an approval.

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
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" \
  CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 \
  make verify-db
# Docker must be running. This starts a loopback-only database container and always removes it.
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-db-ephemeral
# Docker-free fallback when the local PostgreSQL server tools are installed. This starts a
# fresh loopback-only cluster under the operating system temporary directory and removes it.
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-db-native-ephemeral
make verify-temporal
# Focused G010 operator/repository/CLI checks:
uv run pytest -q \
  tests/unit/test_goal_run_repository.py \
  tests/unit/test_goal_run_operator_provider.py \
  tests/unit/test_goal_runs_cli.py \
  tests/contract/test_goal_runs_api_contract.py
# Requires the Compose environment variables described in docs/runbooks/local-compose.md.
make verify-compose
# Docker-free default M0 acceptance: full checks, disposable native PG17, live native health.
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0
# Optional compatibility acceptance adds the temporary Compose stack.
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0-compose
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
with membership in exactly one capability role and no direct object grants. The API and Temporal
worker require distinct runtime credentials bound to `careerops_api` and `careerops_workflow`.
The engine rejects broader identities. Keep the migration identity distinct from every runtime
login.

For a disposable PostgreSQL database only, run the destructive round-trip and database-guard
suite with `CAREEROPS_TEST_DATABASE_URL=... CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 make
verify-db`. See
[`migrations/README.md`](migrations/README.md) for the role and rollback contract.

Use `make verify-m1-full` to see the remaining real D0 pilot blockers. A failing full Gate is
expected until those artifacts exist; do not replace them with synthetic-only data.

`make verify-m0` is an acceptance command, not a promise that every production control has
been certified: it runs local code/security checks, a disposable Homebrew PostgreSQL 17 cluster,
and the live native runtime checks. `make verify-m0-compose` additionally exercises the optional
temporary Compose compatibility stack. Supply only disposable credentials to either command.

## Safety defaults

- `MODEL_PROVIDER=disabled`.
- No shared Google OAuth client, public webhook or provider credential is present. Gmail
  read-only code exists, but the default configuration leaves Google OAuth disabled and no
  credential is configured.
- Public network binding is rejected until the console authentication milestone exists.
- Email send, Gmail send, Calendar write and Auto-send remain disabled by default; Gmail send
  configuration is rejected unless external writes, auto-send, Gmail send enablement and release
  attestation are all explicitly set, and it still requires a host broker/Keychain boundary or
  deployment-equivalent secret store plus live credential evidence.
- Greenhouse submit is disabled by default. Configuration is rejected unless external writes,
  the separate auto-submit switch, Greenhouse submit enablement, release attestation and absolute
  Greenhouse credential and attachment broker sockets are all explicitly set. The checked-in
  Compose profile mounts only the opaque Unix broker socket directory for this worker, not API
  keys, secret files or object storage. Production also requires in-production attestation,
  employer-authorized Job Board API key evidence, broker-owned egress, caps, kill switches,
  revocation handling and live reconciliation qualification.
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
  and replay behavior, and G010 GoalRun adds versioned checkpoints, idempotency keys, fencing
  tokens, authenticated review decisions, resume/cancel signals and continue-as-new. Future
  provider-facing Activities still need their own release-qualified idempotency and
  reconciliation proof before any real channel is enabled.
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
