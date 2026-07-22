# Configured recruitment crawlers

`careerops-crawl-sources` turns the existing public-recruitment crawler scripts into a
declarative local pipeline. It does not create a job-board channel, use a browser, log in, submit
forms, execute JavaScript, or accept arbitrary target URLs.

The G009 source-registry work makes those crawler definitions durable and schedulable, but it does
not change the channel model. A configured source is still an operator-reviewed binding to one of
the repository-owned public crawler adapters below. It is not a free-form crawler plugin system.

The input is an existing triage JSONL artifact. The four registered adapters use it to find public
company recruitment data:

| Adapter | Existing implementation | Output |
| --- | --- | --- |
| `recruitment.sitemap_discovery` | `discover_recruitment_sitemaps.py` | Recruitment-like URLs found in public robots/sitemaps. |
| `recruitment.commoncrawl_discovery` | `discover_recruitment_commoncrawl.py` | Recruitment-like URLs from public Common Crawl index metadata. |
| `recruitment.public_ats_feed` | `discover_public_ats_jobs.py` | Public job records from recognized ATS feeds. |
| `recruitment.page_collection` | `collect_recruitment_pages.py` | Public recruitment-page receipts and bounded raw capture. |

## Create a manifest

Copy [the example manifest](../../datasets/manifests/recruitment-crawler-sources.example.json)
to a private local path and point `input_artifact` at a pre-existing triage JSONL file. Every input
and output path must be relative to the repository and stay under `datasets/`.

To materialize the manifest as a durable registry snapshot for later scheduling and review, run:

```bash
uv run careerops-crawl-sources --root . register \
  --config datasets/private/recruitment/crawler-sources.json --json
```

The command writes a normalized registry snapshot under
`datasets/private/recruitment/source-registry/<manifest-sha256>.json` that captures the manifest
and the resolved plan bindings used by the crawler runner.

When the database-backed registry is enabled, the same normalized manifest becomes an immutable
`crawler_source_registries` row plus one mutable runtime row per source in
`crawler_source_registry_sources`. Treat the manifest row as the audit identity: create a new
manifest/registry version for config changes that materially alter adapters, paths, policies,
budgets, dependencies, or command fingerprints. Do not patch old manifest evidence in place.

`additional_seed_sources` composes the pipeline without hard-coding URLs: a page-collection source
may reference earlier sitemap/Common-Crawl source IDs, and the runner passes their known JSONL
outputs to the collector.

## Validate and plan

```bash
uv run careerops-crawl-sources --root . validate \
  --config datasets/private/recruitment/crawler-sources.json --json

uv run careerops-crawl-sources --root . plan \
  --config datasets/private/recruitment/crawler-sources.json --json
```

Use `--source SOURCE_ID` repeatedly to select a subset. When selecting page collection alone, its
referenced discovery artifacts must already exist. `request` rejects a missing non-selected
dependency before approval, so a human never approves a run that is known to be blocked.
Sources with `enabled: false` are validated but never planned or run; selecting one explicitly is
an error until the manifest enables it.
`resume: false` is supported only by sitemap, Common Crawl, and page-collection adapters because
those scripts expose `--no-resume`; the public-ATS adapter intentionally rejects that field rather
than planning an unsupported command.

## Durable registry and scheduler lifecycle

The durable scheduler consumes persisted source rows; it must not discover channels by accepting
raw URLs, arbitrary scripts, browser settings, credentials, proxy settings, or external plugins.
The registry lifecycle is:

1. **Register immutable manifest.** An authenticated operator registers a manifest reference under
   `datasets/manifests/`. The API materializes the same reviewed plan bytes used by
   `careerops-crawl-sources` and stores manifest, registry, source, and command hashes.
2. **List due sources.** A scheduler reads enabled sources whose `next_run_at` is due. Sources with
   `robots_terms_policy: blocked` or `review_required` are not claimable by unattended scheduler
   workers.
3. **Claim one source.** The scheduler claims one due source at a time with a bounded lease owner
   and expiry. The claim boundary must be database-owned and token-fenced before unattended workers
   are enabled; a stale worker cannot complete or fail a later worker's lease.
4. **Execute the reviewed crawler path.** A claimed source still goes through the reviewed request,
   approval, snapshot, claim-artifact, and outbox rules described below. The scheduler supplies
   registry provenance; it does not bypass review or invoke a crawler directly from a mutable row.
5. **Complete or fail.** Completion advances the cursor and schedules the next cadence only when
   the exact active lease is presented. Failure records a bounded error and schedules a bounded
   retry according to the source retry policy. Unknown post-claim outcomes remain reconciliation
   work, not automatic replay.
6. **Ingest canonical jobs.** Structured public-ATS outputs may be converted into canonical job
   rows with source provenance and deterministic dedupe evidence. Sitemap, Common-Crawl and raw
   page outputs are discovery/capture artifacts until a structured parser promotes them.

Current API surface:

- `POST /api/v1/internal/source-registries/register`
- `GET /api/v1/internal/source-registries`
- `GET /api/v1/internal/source-registries/{registry_id}/due`
- `POST /api/v1/internal/source-registries/{registry_id}/sources/{source_id}/claim`
- `POST /api/v1/internal/source-registries/{registry_id}/sources/{source_id}/complete`
- `POST /api/v1/internal/source-registries/{registry_id}/sources/{source_id}/fail`
- `POST /api/v1/internal/source-registries/{registry_id}/sources/{source_id}/runs/{run_id}/ingest-public-ats`

These endpoints use the authenticated console session and CSRF checks. Claim, completion, failure,
lease expiry, cursor advancement, cadence and retry transitions are owned by PostgreSQL functions;
the API role has no direct update grant on source runtime rows. Completion and failure require the
exact source row, run, worker and lease token. Route/source mismatches are checked inside the same
transaction so a rejected request cannot commit a different source's transition.

The public-ATS ingestion endpoint accepts only a source-row identity and bounded record count. It
derives fixed artifact names from the registered dataset output directory, requires one matching
successful run event, verifies the run-bound manifest and discovered-job artifact hashes, rejects
symbolic links and path escape, and writes canonical job/version/dedupe/provenance rows in one
transaction. It never accepts a caller-provided path, URL, parser, command, dedupe rule or network
instruction.

The matching direct operator CLI is `careerops-source-registry`. It uses the restricted API database
capability and does not execute crawler commands:

```bash
uv run careerops-source-registry --actor-id "$OPERATOR_UUID" register \
  --manifest-ref recruitment-crawler-sources.example --json

uv run careerops-source-registry --actor-id "$OPERATOR_UUID" due \
  --registry-id "$REGISTRY_UUID" --json

uv run careerops-source-registry --actor-id "$OPERATOR_UUID" claim \
  --registry-id "$REGISTRY_UUID" --source-id public-ats-feeds \
  --worker-id goal-run-worker --lease-seconds 300 --json
```

Use the returned `run_id`, `source_row_id`, `lease_token`, and exact worker ID when recording
`complete` or `fail`. A canonical source completion must supply the SHA-256 of its versioned output
manifest; after completion, `ingest-public-ats` verifies the manifest-bound JSONL before ingestion.
The CLI's `--actor-id` is caller-supplied local audit context, not console authentication; prefer the
authenticated API for multi-user or remote operation.

This G009 control plane schedules and fences work but does not itself invoke the reviewed crawler
executor. The durable GoalRun/Temporal orchestration story connects a lease to the reviewed
request/approval/execution path; until then, operators must explicitly perform that handoff and
must not treat a successful claim as crawler execution.

### Scheduler policies

Use these manifest policies as fail-closed controls:

| Policy | Scheduler behavior |
| --- | --- |
| `robots_terms_policy: respect` | Eligible for claim when due. The underlying adapter must continue respecting its public-HTTP, redirect, timeout, and storage limits. |
| `robots_terms_policy: review_required` | Visible to operators but not claimable by unattended workers. Require a separate human review/change before enabling. |
| `robots_terms_policy: blocked` | Never claim. Create a new manifest version only after the blocking reason is resolved. |
| `canonical_ingestion_policy: canonical_job_ingestion` | Promote structured records into canonical job tables after provenance checks. |
| `canonical_ingestion_policy: dedupe_only` | Compute/retain dedupe evidence without creating new canonical job identity. |
| `provenance_policy: preserve` | Keep source URL, captured time, content hash, parser/rule version, manifest/source hash and run binding. |
| `dedupe_policy: hash`, `url`, `hybrid` | Use deterministic identity rules only. Semantic similarity may propose review work but must not auto-merge jobs. |

The scheduler must keep source state small and durable: cursor, last/next run timestamps,
attempt counters, bounded error code/message, lease owner/token/expiry and output manifest hash.
Crawler raw outputs remain under `datasets/`; do not expose them through API responses or model
prompts without the existing review/redaction flow.

## Reviewed execution flow

Every high-autonomy local run follows `request` -> `approve` -> `execute`. The `request` command
validates the manifest, selects the enabled sources, fingerprints the manifest, source definitions,
input artifacts, dependency artifacts, registered crawler implementation files, and generated plan,
then writes an immutable review request. The `approve` command records a local reviewer identifier
and binds the approval to the request fingerprint. The `execute` command reloads the manifest,
request, and approval, recomputes the reviewed plan, checks expiry, and refuses to start a crawler
if the manifest, plan, source selection, request, or approval no longer match. There is deliberately
no unreviewed `run` command in the public CLI.

```bash
uv run careerops-crawl-sources --root . request \
  --config datasets/private/recruitment/crawler-sources.json \
  --source public-ats-feeds \
  --request-output datasets/private/crawler-execution-reviews/ats-request.json \
  --reason "daily public ATS refresh" --json

uv run careerops-crawl-sources --root . approve \
  --request datasets/private/crawler-execution-reviews/ats-request.json \
  --approval-output datasets/private/crawler-execution-reviews/ats-approval.json \
  --approved-by operator@example --note "reviewed bounded plan" --json

uv run careerops-crawl-sources --root . execute \
  --config datasets/private/recruitment/crawler-sources.json \
  --request datasets/private/crawler-execution-reviews/ats-request.json \
  --approval datasets/private/crawler-execution-reviews/ats-approval.json --json
```

Requests expire after 24 hours by default; `request --expires-in-hours` accepts 1 through 168.
Request and approval artifacts must be new `.json` files under
`datasets/private/crawler-execution-reviews/`; no crawler output directory may be inside that
reserved namespace. This makes review evidence easy to retain without allowing crawler receipts to
overwrite it. `execute` stages a private execution
snapshot under `datasets/private/crawler-execution-snapshots/<request_id>/<snapshot_id>/` before it
claims the request. The snapshot contains byte-verified copies of the reviewed inputs, registered
crawler scripts, external dependency artifacts, and `reviewed-plan.json`; snapshot files are
written read-only and the snapshot directory is made owner-only. The subprocess commands are
rewritten to use those snapshots, so execution uses the exact bytes reviewed instead of mutable
source files.

After snapshot staging, `execute` writes a dedicated claim artifact at
`datasets/private/crawler-execution-claims/<request_id>.json`. The claim records the approval hash,
request hash, reviewed plan path and hash, snapshot path, selected sources, and claim timestamp.
After claiming, the CLI takes an advisory POSIX lock for every selected output directory, preventing
two reviewed executions from mixing resume state or outputs. A selected discovery producer that
feeds a selected page-collection source additionally requires a fresh, absent output directory and
reserves it with exclusive creation before launch. Claim creation and output locking make the
approval single-attempt: create a new request and approval for a retry after a claim. A failure
before claim creation (for example, a byte-verification failure while staging) does not consume the
approval and can be corrected while that request remains valid.

This gate is local and unauthenticated. The SHA-256 fingerprints are change-detection bindings, not
digital signatures; `approved_by` is a bounded string, not a verified identity. Treat the flow as a
local audit and operator-attestation gate, not a cryptographic or server-side human-only boundary,
and not as execution authority for the database-backed authenticated-review and outbox-dispatch
path described below. Snapshotting binds the repository scripts and artifacts; it still trusts the
local Python interpreter and installed project dependencies. The CLI launches crawlers with `-Es`
and a minimal environment to exclude `PYTHONPATH`, user-site packages, proxy variables, and similar
ambient overrides, but local runtime integrity remains an operator responsibility.

If a selected page-collection source depends on selected sitemap or Common-Crawl producers, those
selected producers must use fresh, previously absent output directories before `execute`, then
produce their dependency artifacts successfully and have those artifacts byte-copied into the
private snapshot during the same run before collection starts. Other adapters retain their existing
resume behavior, while the reviewed CLI holds their output lock for the duration of the run. Older
output or resume state therefore cannot satisfy a selected producer that failed or was blocked. If a
dependency producer is not selected, its existing dependency artifact must be present and its bytes
are fingerprinted in the reviewed request and copied into the execution snapshot.

Each source writes `configured_crawl_receipt.json` inside its output directory. The receipt records
the adapter, source/config fingerprints, timing, and exit status; it deliberately excludes process
stdout/stderr and raw response data. Runs launched through `execute` additionally record the
immutable execution-request ID for audit correlation.

## Contract and safety limits

The manifest validator rejects unknown fields. It deliberately has no fields for URL, host, proxy,
headers, cookies, credentials, browser automation, `allow_sandbox_egress_alias`, script path, or
arbitrary output location. The runner maps only registered adapters to repository-owned scripts,
strips ambient proxy environment variables, and keeps bounded timeout, retry, discovery, response,
storage, depth, concurrency, and per-host-concurrency limits.

The crawler scripts retain their existing public-HTTP/redirect validation and resumable receipts.
Treat page-collection raw artifacts as private: do not expose them through APIs or copy them into
model prompts before the project’s review/redaction flow.

The durable registry must preserve the same prohibitions. It may store only a normalized manifest
binding, fixed adapter identifier, safe relative dataset paths, hashes, scheduler policy, cursor,
budget/rate-limit metadata, lease state and provenance. It must not become a generic web crawling
or login automation interface. In particular, it must not support:

- login, MFA, CAPTCHA solving, session-cookie reuse or paywall/terms bypass;
- arbitrary URLs, scripts, shell fragments, request headers, proxies or browser automation flags;
- autonomous application submission or provider writes;
- replaying a reviewed execution after a one-shot claim exists;
- semantic-only canonical-job merges.

## Durable console request path

Use `careerops-crawler-execution` when a configured crawl should enter the server control plane
instead of being locally approved by the same operator shell. This command is stricter than the
local artifact tool:

- `--config` must point to an existing manifest under `datasets/manifests/`.
- The caller may provide only manifest `source_id` values, an existing console owner user UUID, a
  bounded reason, and expiry. Scheduler provenance belongs in the reason/audit text; the database
  enforces that `owner_user_id` refers to `console_users.id`.
- The command does not accept raw URLs, script paths, headers, proxy settings, browser settings,
  arbitrary output paths, or a caller-chosen request artifact path.
- The request artifact filename is generated from the immutable request UUID under
  `datasets/private/crawler-execution-reviews/`.

```bash
uv run careerops-crawler-execution --root . request \
  --config datasets/manifests/recruitment-crawler-sources.example.json \
  --source public-ats-feeds \
  --owner-user-id 00000000-0000-0000-0000-000000000001 \
  --reason "scheduler requested public ATS refresh for console owner review" --json
```

The resulting local JSON file is audit evidence only. It is not execution authority. The durable
request remains `pending_review` until the authenticated console approves or rejects it using the
server-side actor identity. A local `careerops-crawl-sources approve` artifact may be retained as
operator evidence, but it does not substitute for console authentication and does not enqueue
execution by itself.

After console approval creates the dispatch/outbox binding, the crawler publisher reads dispatch
metadata from the database and processes only crawler execution outbox events:

```bash
uv run careerops-crawler-outbox --root . --max-attempts 3
```

The outbox publisher uses the `careerops_outbox` database capability role and filters event keys to
the `crawler-execution:` prefix. The database dispatch reader resolves the persisted binding for
the claimed event and must point back to review artifacts under
`datasets/private/crawler-execution-reviews/`; the sink rechecks request and approval artifact
hashes before invoking the reviewed executor. This lifecycle requires schema revision `0007`
(`crawler_execution_results` and the database-owned completion function), so run `alembic upgrade
head` before enabling a matching crawler publisher outside Compose.

In the local Compose stack, the `crawler-outbox` service runs this publisher continuously with
`--poll-seconds`, mounts the shared `crawler-datasets` volume at `/app/datasets`, and connects
with the distinct `CAREEROPS_DB_OUTBOX_USER` login granted only to `careerops_outbox`.

## Outbox lifecycle, retry, and recovery

The durable worker deliberately has two failure classes. Do not treat every failed delivery as
safe to replay:

| Stage | Worker behavior | Operator action |
| --- | --- | --- |
| Before `careerops-crawl-sources execute` creates its local one-shot claim | A temporarily unavailable review artifact or shared volume is deferred as `CRAWLER_ARTIFACT_UNAVAILABLE`. The outbox remains pending and retries after the publisher's five-minute delay. | Restore the mounted review artifacts or volume and let the worker retry. This is safe because no crawl claim has been created. |
| Pre-execution retry budget exhausted | The event becomes terminal after `--max-attempts` attempts (default `3`, accepted range `2` through `10`). Its `CRAWLER_ARTIFACT_UNAVAILABLE` result is a deterministic `failed` outcome. | Investigate the storage/runtime fault, then submit a new durable request and obtain a new console approval. Do not reset or replay the old event. |
| Executor returns its known failure exit (`1`) or rejects the reviewed execution | The event is terminal with `CRAWLER_EXECUTION_FAILED` or `CRAWLER_EXECUTION_REJECTED` and a deterministic `failed` outcome. The worker never reruns it because the reviewed execution can have created a single-use claim. | Inspect the immutable claim, snapshot, and receipts; correct the cause; then create and approve a fresh request. Do not reuse the old approval or mutate the claimed artifacts. |
| A one-shot claim already exists, the runner raises, or an unexpected post-claim result occurs | The outcome may be unknown. The worker sets `CRAWLER_EXECUTION_RECONCILIATION_REQUIRED` and records terminal `reconciliation_required`; it never automatically reruns this boundary. | Reconcile the claim, snapshot, receipts, and output directories before deciding whether a replacement crawl is safe. Never replay the old event or reuse its approval. |

On success, the database-owned completion path atomically marks the outbox event `published`,
marks its generic action intent `confirmed`, and appends a `succeeded`
`crawler_execution_results` record. A deterministic terminal failure atomically marks the outbox
event and action intent `failed` and appends a `failed` result with its bounded error code. An
uncertain post-claim outcome still marks the outbox event `failed`, but moves the action intent and
append-only result to `reconciliation_required`. These result rows are the source for
post-dispatch status, not the local JSON review artifact or a worker log line. Each request/outbox
binding receives at most one result row, written atomically by the restricted outbox database path.

Each single batch prints `claimed`, `published`, `deferred`, and `failed`. With `--poll-seconds 0`,
the command exits nonzero when the batch contains a terminal failure; a deferred item is expected
to retry later and does not by itself make the command fail. The Compose worker polls continuously,
so treat a recurring `failed` count or a terminal result record as an operator alert. The retry
limit changes only the safe pre-execution artifact-availability attempts; it does not permit a
second crawl after a claim exists.

For a terminal execution, retain the request ID, action-intent ID, result/error code, outbox event
ID, and any claim/snapshot/receipt paths during investigation. For `reconciliation_required`, first
establish whether the crawler produced any outputs or side effects; only then decide whether a new
`careerops-crawler-execution request` and authenticated console review are safe. For a deterministic
`failed` result, repair the cause and create a new request before retrying. Direct SQL status edits,
reusing a prior approval artifact, or forcing an old outbox event back to pending would break the
one-shot execution guarantee and are not supported recovery procedures.
