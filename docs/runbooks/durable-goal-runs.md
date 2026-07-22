# Durable GoalRun runbook

## Purpose and boundary

G010 adds a durable GoalRun control plane for one authenticated owner. The current source defines
two immutable execution modes over the same reviewed-crawler, canonical-ingestion and Temporal/
PostgreSQL checkpoint path:

| Mode | Approval effect | Provider-side effect |
| --- | --- | --- |
| `pre_application_only` | Atomically completes the local reviewed package as `completed/completed` with `completion_kind=pre_application_package_approved` | None: no GoalRun dispatch or reconciliation activity, no Gmail/application outbox event, no email or application submit |
| `gmail_dispatch` | Returns the run to `running/dispatch` and follows the existing reviewed Gmail outbox/reconciliation path | Possible only through the separately gated Gmail send worker; approval alone does not perform provider I/O |

`mode` is not a convenience flag. Missing `mode` in an old API request, database record or
Temporal payload deliberately means `gmail_dispatch`. This preserves replay compatibility and
prevents an old outbound-capable run from being silently reinterpreted as the newer local-only
mode. A caller must explicitly send `"mode": "pre_application_only"` to select the zero-provider-
write branch.

This remains a bounded implementation milestone, not an end-to-end runtime claim. It does not
configure Gmail, Greenhouse, browser credentials, OAuth scopes, provider secrets or live provider
accounts. The pre-application slice requires an exact approved profile reference; callers cannot
inline candidate facts or matching policy. Source head is migration `0024`: `0022` provides the
owner-bound candidate profile/material records and `careerops-candidate-profiles`
import/review/inspect CLI, while `0023` adds owner-scoped GoalRun create-receipt lookup for safe
recovery after a Temporal-start failure. `0024` adds a database `BEFORE INSERT` profile-identity
guard for new pre-application GoalRuns. Source and the isolated Homebrew-native database/runtime
are now both at `0024`; the API, Temporal worker, crawler outbox and read-only Gmail worker are
healthy, and all external-write gates remain disabled. The first candidate profile/material bundle
is imported but still awaits explicit human approval, so no GoalRun exists and the native path has
not yet been exercised end to end. The scorer operates on canonical fields available today, whose
rich description/skills fidelity is still incomplete.

GoalRun does not compose Greenhouse dispatch. G013 Greenhouse Job Board submit remains a separate
reviewed-submit channel with its own account profile, packet review, credential broker,
outbox/reconciliation path and release qualification. It must not be reported as live, universal
auto-apply, or part of G014 Gmail composition.

## Implemented surfaces

- Authenticated internal operator API:
  - `POST /api/v1/internal/goal-runs`
  - `GET /api/v1/internal/goal-runs?limit=...`
  - `GET /api/v1/internal/goal-runs/{goal_run_id}`
  - `POST /api/v1/internal/goal-runs/{goal_run_id}/reviews/{review_id}`
  - `POST /api/v1/internal/goal-runs/{goal_run_id}/resume`
  - `POST /api/v1/internal/goal-runs/{goal_run_id}/cancel`
- Read-only local CLI:
  - `uv run careerops-goal-runs list --limit 20 --json`
  - `uv run careerops-goal-runs status --goal-run-id "$GOAL_RUN_ID" --json`
- Temporal worker:
  - `uv run careerops-worker`
  - the normal native runtime starts it as `io.careerops.workflow-worker` on task queue
    `careerops-m0`;
  - optional Compose compatibility starts the same worker implementation.

The CLI is intentionally read-only. Review, resume, cancel, and create actions require the
authenticated API session and CSRF boundary. The HTML console exposes review actions at
`/goal-runs`; it does not currently expose GoalRun creation.

## Pre-application-only flow

The implemented local-only path is:

```text
candidate profile/material version imported and approved
  -> GoalRun created with exact candidate/profile-version reference
  -> fresh reviewed crawler request/result
  -> canonical public-ATS ingestion
  -> deterministic profile-aware ranking
  -> match snapshot SHA-256
  -> local review packet (`goal_run_pre_application_review.v1`)
  -> authenticated approve/reject
  -> approve: completed + pre_application_package_approved
     reject: rejected
```

The create body requires `mode`, `candidate_id`, and `candidate_profile_version_id`. It rejects both
a `gmail_dispatch` envelope and caller-provided `match_config` in this mode. The current request
shape is:

```json
{
  "command_id": "11111111-1111-4111-8111-111111111111",
  "registry_id": "22222222-2222-4222-8222-222222222222",
  "source_id": "public-ats-native-smoke",
  "mode": "pre_application_only",
  "candidate_id": "33333333-3333-4333-8333-333333333333",
  "candidate_profile_version_id": "44444444-4444-4444-8444-444444444444"
}
```

Send this body to `POST /api/v1/internal/goal-runs` through an authenticated console session and
valid CSRF boundary. In the same database transaction as GoalRun creation, the operator loads the
owner's latest approved candidate profile whose material objects remain active, unretired and
unexpired, verifies that its ID exactly equals `candidate_profile_version_id`, and then creates the
run. A missing profile, a stale/non-latest version, a non-`APPROVE` decision, an expired/retired
content object, or a non-active blob fails before GoalRun creation.

The operator materializes the approved profile, preferences, material hashes and bounded resume
reference into the immutable GoalRun context. It derives `match_config` from approved required and
preferred keywords, desired titles, exclusions and `minimum_match_score`; request data cannot
override any of these values. Raw resume bytes and arbitrary resume text do not enter the GoalRun
request.

Source revision `0022` provides append-only profile/material versions and decisions, while
`careerops-candidate-profiles` implements `import`, `approve`, `reject`, `list`, and `show`. The
isolated Homebrew runtime has installed source head `0024` and this CLI. Candidate profile V1 and
its material bundle are present and active but remain pending explicit human approval; accordingly
no native profile-import-to-GoalRun E2E has passed yet.

Public ATS row schema v2 maps `source_job_industry` and `source_job_industries` into canonical
structured `industry` and `industries`, so industry gates use normalized ingestion evidence rather
than crawler-only fields. The GoalRun candidate SQL partitions by `canonical_job_id`, retains the
latest row by evidence capture time, posting-version capture time, and version ID, and filters to
that row before ordering and applying `LIMIT`. Duplicate versions of one canonical job therefore
cannot crowd newer distinct jobs out of the candidate window.

Matching emits `goal_run_pre_application_artifact.v2` and gives every canonical job exactly one
eligibility state: `eligible`, `needs_manual_review`, or `ineligible`. Hard gates include exclude
terms; ALL-of approved required keywords when job-text evidence exists; and the approved company,
industry, seniority, and location allowlists, together with the other bounded profile constraints.
Missing evidence needed to decide a gate becomes `needs_manual_review`, never an assumed pass.
`selectable=true` requires both `eligibility=eligible` and score >= `min_score`. A manual-review
candidate at or above the threshold may be surfaced only as `proposed_manual_review_job` with
`review_disposition=manual_evidence_required`; it remains `selectable=false`, never appears as
`selected_job`, and cannot enter an executable set. `ineligible` rows score zero. Draft preparation
recomputes the review-candidate match snapshot and blocks on a missing or changed hash. The review
payload includes eligible-selection or manual-proposal provenance, ranking reasons, the
candidate/profile hashes, optional resume metadata and explicit flags that provider execution,
outbox enqueue, Gmail send and application submit are disabled. These v2 ingestion, query, and
artifact paths are deployed in the native runtime but remain unverified by a completed native E2E.

Approval is package approval for manual follow-up only. Migration `0021` requires the exact review
kind `goal_run_pre_application_review.v1`, writes a terminal `finalize` checkpoint in the same
database transaction as the review decision, and records
`completion_kind=pre_application_package_approved`. Before that decision is written, the same
approval transaction reloads the exact bound approved profile/material version and verifies its
owner, candidate/profile IDs, profile snapshot hash, material bundle hash, active/retired state,
and expiry against the immutable GoalRun context. Drift or expiry fails closed before the decision
and no Temporal signal is sent. The workflow observes the terminal database state and returns
without invoking `goal.dispatch_goal` or `goal.reconcile_goal`; repository guards also reject
either activity if it is called accidentally. Crawler network reads may have occurred earlier,
but the pre-application branch performs zero provider writes.

## Gmail-dispatch flow

The implemented G014 path is:

```text
configured crawler request/result
  -> canonical public-ATS ingestion
  -> deterministic match from match_config
  -> exact Gmail draft payload
  -> Chinese simple review packet
  -> authenticated approve/reject
  -> Gmail send outbox enqueue
  -> receipt/reconciliation inspection
```

The `gmail_dispatch` mode may include `match_config` and `gmail_dispatch`. `match_config` supplies
include keywords, exclude keywords, a minimum score and `max_applications=1`. `gmail_dispatch`
supplies the candidate/account/grant/release identities, sender, recipient, subject, text body,
reviewed attachment references and expiry timestamps. Credential handles and raw secrets are
intentionally absent from the GoalRun request.

This is also the default when `mode` is omitted. That default is for compatibility, not a claim
that Gmail is configured or enabled. Do not omit `mode` when the intended effect is local-only.

Matching is deterministic and local. It de-duplicates candidates by canonical job id, scores
include-keyword coverage, blocks exclude-keyword matches, chooses the first candidate meeting the
minimum score, records the match snapshot and then prepares the exact Gmail payload. Model output is
not execution authority.

The Chinese review packet shows only the decision data needed to approve or reject the exact send:
job, match reasons, recipient, subject, body, attachment names/types/sizes/hashes and exact payload
hashes. Any payload, target, attachment, grant, policy or release-evidence change creates a new
payload version and requires a new review.

Dispatch enqueues only after the database verifies the GoalRun review decision, campaign grant,
release qualification, cap reservation and kill-switch state. The workflow role calls only narrow
`SECURITY DEFINER` wrappers; it does not write Gmail tables directly, read credentials or call
Gmail. Reconciliation inspection maps outbox and Gmail-send evidence to pending, confirmed or
ambiguous GoalRun outcomes.

## Local stack

Homebrew is the normal macOS runtime. From the source checkout:

```bash
make native-install
make native-start
make native-status
curl --fail --silent http://127.0.0.1:8000/api/v1/health/ready
```

`native-install` syncs code and the reviewed crawler workspace into
`~/Library/Application Support/CareerOps`; startup creates/repairs isolated PostgreSQL roles and
applies `alembic upgrade head` before the API and workflow worker start. Migration `0021` must
therefore be present before a pre-application approval can end locally, `0022` before an approved
profile version can be resolved, `0023` before a committed GoalRun create can be recovered by
owner/idempotency key after Temporal start fails, and `0024` before new pre-application rows receive
the database create-time profile-identity guard. After pulling source changes, run
`make native-install`, not only `make native-restart`, so the isolated runtime does not keep old
code. Source and the isolated native runtime are both at `0024`; deployment has been verified, but
the native end-to-end path remains pending the candidate-profile and crawler/package human gates.

The normal core LaunchAgents force `CAREEROPS_EXTERNAL_WRITES_ENABLED=false`,
`CAREEROPS_AUTO_SEND_ENABLED=false`, `CAREEROPS_AUTO_SUBMIT_ENABLED=false`,
`CAREEROPS_GMAIL_SEND_ENABLED=false`, and the provider release attestations false. They do not
install an auto-start Gmail send or Greenhouse submit worker. These deployment gates are additive
to the `pre_application_only` workflow boundary.

Create the first owner through the native bootstrap flow in
[local-homebrew.md](local-homebrew.md). The API and workflow worker use distinct non-owner,
`NOINHERIT`, single-capability database logins. Do not put real provider credentials or fake
Gmail/ATS secrets in `.env`; GoalRun requests contain hashes and opaque identities, not secrets.

Native checks:

```bash
make native-status
make verify-temporal
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-m0
```

The [local Compose runbook](local-compose.md) remains an optional compatibility path. It is not a
normal-runtime prerequisite. For destructive database guards, use only a disposable database:

```bash
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" \
  CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 \
  make verify-db
```

## State model

Canonical statuses are:

```text
starting
running
waiting_review
blocked
reconciliation_required
completed
failed
cancelled
rejected
```

Canonical phases are:

```text
initializing
selecting_source
claiming_source
discovery
complete_source
canonical_ingest
matching
draft_preparation
review
dispatch
reconciliation
completed
blocked
failed
cancelled
rejected
```

Shared progression through review is:

```text
initializing
  -> selecting_source
  -> claiming_source
  -> discovery
  -> complete_source
  -> canonical_ingest
  -> matching
  -> draft_preparation
  -> review

pre_application_only + approve -> completed
gmail_dispatch + approve -> dispatch -> reconciliation -> completed
either mode + reject -> rejected
```

Current fail-closed paths:

```text
pre_application_only without candidate_id or candidate_profile_version_id -> API validation error
pre_application_only with gmail_dispatch or caller match_config -> API validation error
missing/non-latest/non-approved/expired/inactive candidate profile version -> create fails
pre_application_only with invalid materialized profile/match snapshot or snapshot drift -> blocked
pre_application_only dispatch/reconciliation activity -> blocked (must not be scheduled)
gmail_dispatch with missing match_config or gmail_dispatch -> blocked
no reviewed crawler candidates -> blocked
no candidate meeting deterministic match_config -> blocked
invalid exact Gmail draft identity -> blocked
review -> rejected
dispatch blocked by rejected review, stale version, revoked grant, exhausted cap,
  kill switch, missing account or missing release evidence
reconciliation -> pending_external | completed | reconciliation_required
```

Failure and operator paths are:

```text
any running phase -> blocked -> resume -> same phase
any running/waiting phase -> cancelled
review -> rejected
uncertain provider/source outcome -> reconciliation_required
resume budget exhausted -> failed
```

Workflow-terminal statuses are `reconciliation_required`, `completed`, `failed`, `cancelled`,
and `rejected`. A pre-application approval writes the terminal `finalize` checkpoint directly in
the review-decision transaction. `reconciliation_required` is a terminal operator stop but uses
the `reconciliation_required` checkpoint outcome, not `finalize`.

## PostgreSQL and Temporal division

PostgreSQL owns:

- actor-owned GoalRun records;
- generated `goal_run_id` and `fencing_token`;
- optimistic `expected_version` checks;
- idempotency keys for create/checkpoint/review/resume/cancel commands;
- command receipts that make replayed mutating commands converge on the original response;
- owner-scoped lookup of the original GoalRun create receipt, so a committed create can be
  reattached before retrying Temporal start;
- pending `review_item_id`, `review_snapshot_sha256`, and authenticated `approve`/`reject`;
- operator-visible checkpoints, events, last error code, and terminal state.

Temporal owns:

- deterministic phase ordering;
- activity retry policy and replay history;
- waiting for review/resume/cancel signals and 30-second database polling while blocked or
  awaiting review;
- continue-as-new after Gmail review approval and resume loops;
- terminal return after an approved `pre_application_only` review; it does not continue as new to
  dispatch;
- best-effort source-run failure write when discovery/source completion fails.

Signals are wake hints only. The workflow reloads the actor-owned database record after a review,
resume, or cancel signal, and also polls that record if a wake signal is lost. Database state is
the authorization source of truth.

The workflow database role can call the database-owned reviewed crawler bridge. Discovery is
ready only when that bridge returns reviewed crawler evidence: crawler execution request ID,
outbox event ID, result ID, reviewed plan/request hashes, source hashes, and output manifest
hash. Missing approval, unpublished Outbox delivery, failed result, or reconciliation-required
result blocks the GoalRun before matching.

### Crawler request ordering

The bridge filters crawler requests with `request.created_at >= goal_run.created_at`, in addition
to owner, registry-manifest and source checks. The safe integrated sequence is therefore:

1. Create the GoalRun. Its first discovery attempt may block while no eligible request exists.
2. Create a fresh server-side crawler execution request for the same owner, registry manifest and
   source.
3. Approve it in the authenticated crawler review console and let crawler Outbox publish a
   terminal result.
4. Resume the blocked GoalRun; discovery can now bind the new request/result evidence.

The native handoff on 2026-07-22 already has pending standalone request
`51b6837c-7cfe-483e-9fce-8c00588928b6`, created before any GoalRun. Whether it is later approved or
rejected, it cannot feed a subsequently created GoalRun. It may be used only to validate the
standalone crawler request/approval/execution path. Do not alter timestamps or force-bind it; make
a fresh post-GoalRun request.

For `gmail_dispatch`, the workflow database role can also call the G014 Gmail composition wrappers.
PostgreSQL owns the match record, exact draft payload identity, approval binding, cap reservation,
outbox enqueue and reconciliation inspection. For `pre_application_only`, the review decision
function installed by migration `0021` owns the atomic terminal transition and completion
checkpoint; no Gmail composition/outbox wrapper is called. The workflow role cannot register Gmail
accounts, inspect OAuth material, resolve broker credentials, execute Gmail send, submit
Greenhouse, or mutate application projections directly.

## Review, resume, and cancel

Review requests bind a pending review item to the run version and immutable snapshot SHA-256.
An approval or rejection must include:

- authenticated owner identity from the session;
- `goal_run_id`;
- `review_item_id`;
- `review_snapshot_sha256`;
- current expected version;
- current fencing token;
- idempotency key and trace id.

Approval semantics depend on the immutable mode:

- `pre_application_only` requires `goal_run_pre_application_review.v1`. Migration `0021` records
  the decision only after the same transaction reloads and verifies the exact active approved
  profile/material version, snapshot and bundle hashes, and expiry; it then writes the terminal
  checkpoint, `completed_at`, `completed/completed` state and
  `completion_kind=pre_application_package_approved` atomically. The retained review item remains
  audit evidence and is no longer projected as an actionable pending review.
- `gmail_dispatch` records `review_decision = approve` and returns the run to
  `running/dispatch`; the workflow reloads that database fact before continuing.

Rejection becomes an operator terminal path in either mode. Cancel writes a terminal database
state and then signals the workflow; if the signal is replayed or duplicated, the reloaded
database state still wins. Temporal signals are wake hints and never replace the database decision.

Resume is for `blocked` runs only. It is versioned, fenced, and idempotent. The workflow caps
resume attempts; after the budget is exhausted, it checkpoints `failed`. A
`reconciliation_required` run is an explicit stop for uncertain external/source state; do not
generic-resume it or replay dispatch. Preserve the evidence and create a separately authorized
follow-up run only after operator inspection.

## Gmail account bootstrap order

Only `gmail_dispatch` needs account and release records. Bootstrap them in this order:

1. Register the dedicated G011 Gmail read-only account through the authenticated Gmail read-only
   operator API, using a BYO OAuth client, exact `gmail.readonly` scope, an opaque broker handle and
   credential-store evidence.
2. Register the G012/G015 Gmail send account through the authenticated Gmail send operator API,
   binding it to the G011 `reconciliation_gmail_account_id`, exact `gmail.send` evidence and
   release evidence. Keep the account disabled until broker/vault/OAuth and smoke evidence exist.
3. Create the GoalRun with explicit `mode=gmail_dispatch`, `match_config` and `gmail_dispatch` that
   reference the registered account ids, grant version, release qualification and approved
   attachment hashes. Do not include credential handles, tokens, OAuth secrets, raw attachment
   bytes, Greenhouse credentials or browser state in the GoalRun context.
4. Start the Gmail send worker only after the separate Gmail send runbook gates pass. GoalRun
   dispatch enqueue alone is not a live send claim.

Greenhouse account bootstrap is separate. Use the reviewed Greenhouse submit runbook and its
operator API for employer-authorized Job Board profiles; G014 Gmail GoalRuns do not create or
dispatch Greenhouse packets.

## Idempotency, fencing, and reconciliation

Every mutating repository call uses a stable idempotency key. Replayed Temporal activities and
duplicated operator requests must converge on the existing database record rather than creating
another transition.

Migration `0023` makes GoalRun creation recoverable across the PostgreSQL/Temporal boundary. If
the database create committed but Temporal start failed, retry the authenticated request as the
same owner with the original idempotency key and exactly the same immutable create arguments. The
operator reattaches to the original GoalRun and retries Temporal start; it does not create a second
run. A changed goal, trace, registry/source/workflow/mode/context value, or pre-application
candidate/profile reference conflicts. The receipt lookup is owner-scoped, so it cannot expose or
reuse another owner's run.

Migration `0024` adds a `BEFORE INSERT` guard on `careerops.goal_runs`. For every newly inserted
explicit `pre_application_only` run, PostgreSQL locks the owner-bound candidate row and verifies
that the context references the latest material-active approved profile with the exact profile
ID/version, profile snapshot hash and material-bundle hash. It rejects missing `match_config`, a
mixed Gmail envelope and an unknown explicit mode. Missing mode and explicit `gmail_dispatch`
retain legacy compatibility. The trigger runs only for a new insert, so an exact `0023` create
receipt replay can still reattach after later profile drift; final package approval revalidates the
profile again. This is an approved-profile identity binding, not database-side proof of every
derived candidate-profile or matching-policy field. Full derived-context proof requires a future
database-generated context or approved execution-context hash.

Every versioned command carries the run `fencing_token` and `expected_version`. A stale worker,
old browser tab, replayed signal, or old API request must fail closed at the database function.

Use `reconciliation_required` when a state is externally uncertain or post-claim recovery is not
safe to replay. Preserve the existing GoalRun event/checkpoint evidence and create a fresh
operator-approved run if a replacement is safe. Do not reset a terminal event, mutate historical
review decisions, or reuse a stale fencing token.

## Verification commands

Focused GoalRun checks:

```bash
uv run pytest -q \
  tests/unit/test_discover_public_ats_jobs.py \
  tests/unit/test_canonical_job_ingestion.py \
  tests/unit/test_candidate_profile.py \
  tests/unit/test_candidate_profile_migration.py \
  tests/unit/test_candidate_profile_repository.py \
  tests/unit/test_candidate_profiles_cli.py \
  tests/unit/test_goal_run_create_lookup_migration.py \
  tests/unit/test_goal_run_create_profile_guard_migration.py \
  tests/unit/test_goal_run_repository.py \
  tests/unit/test_goal_run_operator_provider.py \
  tests/unit/test_goal_runs_cli.py \
  tests/unit/test_goal_run_composition.py \
  tests/unit/test_goal_run_composition_repository.py \
  tests/unit/test_preapplication.py \
  tests/unit/test_preapplication_goal_run_safety.py \
  tests/unit/test_temporal_goal_run_composition.py \
  tests/contract/test_goal_runs_api_contract.py
```

Temporal replay and worker checks:

```bash
make verify-temporal
```

Repository-wide local checks:

```bash
make verify
```

`make verify` does not replace the disposable PostgreSQL integration path. Migration `0021` and
the atomic terminal review decision are exercised by
`tests/integration/test_goal_run_pre_application_mode.py`; use
`CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 make verify-db`
with an explicitly disposable database, or
`CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-db-native-ephemeral`, for migration and
database-function evidence, including the `0022` profile/material functions, `0023` owner-scoped
create-receipt lookup and `0024` create-time profile-identity guard.
Workflow coverage that asserts no
dispatch/reconciliation activities are scheduled lives in
`tests/integration/test_goal_run_workflow_contract.py`.

## Troubleshooting

| Symptom | Likely cause | Operator action |
| --- | --- | --- |
| API returns `503` for GoalRun routes | Runtime provider or database/Temporal client is not configured | Check API readiness, migration state, and worker/Temporal connectivity |
| First create returns `503` after its database row committed | Temporal start failed after PostgreSQL commit, or deployed schema predates `0023` | After installing the current source head (`0024`), retry as the same owner with the original idempotency key and identical immutable create body; changed arguments must conflict |
| CLI prints `goal run operator runtime is not configured` | Read-only CLI factory is disabled by default | Use it only after a runtime provider is wired, or inspect through the authenticated API |
| API rejects a pre-application create body | Missing explicit `pre_application_only`, `candidate_id`, or `candidate_profile_version_id`; body includes `gmail_dispatch` or caller `match_config`; or referenced version is not the latest active, unexpired `APPROVE` version | Correct the immutable reference or approve a new profile version; omitting `mode` selects Gmail compatibility semantics |
| Run blocks in `discovery` although a standalone crawler request exists | The request predates the GoalRun, or owner/manifest/source/result evidence does not match | Create and approve a fresh request after the GoalRun, then resume; never change timestamps |
| Run stays `blocked` in `matching` or `draft_preparation` | No candidates, invalid materialized approved profile, no eligible selectable match, missing match snapshot or snapshot drift; Gmail mode can also be missing dispatch configuration | Inspect `eligible`/`needs_manual_review`/`ineligible` evidence, then fix the approved profile version or source evidence; never auto-select a manual-review row or inject replacement matching policy into the GoalRun |
| Pre-application approval conflicts after review | The exact profile/material version, snapshot/bundle hash, active state, or expiry drifted since create | Import and approve a new immutable profile/material version and create a newly reviewed GoalRun; do not bypass approval-time revalidation |
| Pre-application approval returns `running/dispatch` | Deployed database is older than migration `0021`, mode is missing/invalid, or review kind is not `goal_run_pre_application_review.v1` | Stop before any dispatch; deploy current source with `make native-install`, verify the exact mode/review kind, and submit a fresh reviewed run if required |
| Run blocks at `dispatch` | Review, grant, cap, release, account or kill-switch state does not authorize enqueue | Inspect the exact blocked reason; create a new reviewed version if any bound value changed |
| Run stays pending in `reconciliation` | Gmail send outbox has not produced a confirmed receipt yet | Inspect the Gmail send worker and reconciliation queue; do not replay dispatch |
| Run reaches `reconciliation_required` | Source or provider state is ambiguous after a boundary where replay could duplicate effects | Preserve evidence and perform a separate operator review before any replacement run |
| Review signal ignored | Review item or snapshot does not match the pending database row | Reload status and submit the decision for the current pending review |
| Version or fencing conflict | Stale workflow/activity/browser/API request | Reload current status; do not retry with the stale token/version |
