# M0 database capability matrix

## Status and authority

This document records the capability boundary implemented by the M0 migration and runtime
engine. It is not evidence that a production database deployment satisfies the boundary.
Production acceptance additionally requires non-owner service logins, verified memberships,
ownership/default-ACL inspection and restore testing.

Authoritative code locations are:

- `src/careerops/infrastructure/database/bootstrap_roles.sql` for role creation and hardening;
- `migrations/versions/0001_create_initial_business_schema.py::ROLE_GRANTS` for grants and
  database guards;
- `migrations/versions/0002_console_auth.py` for the console-authentication tables and their
  API-only grants;
- `migrations/versions/0003_harden_audit_append.py` for the database-owned audit append
  function and removal of direct API table/sequence write authority;
- `migrations/versions/0004_add_autopilot_control_plane.py` for append-only bounded-autopilot
  Campaign Grant, exact intent authorization and review-control-plane tables;
- `migrations/versions/0005_add_autopilot_cap_reservations.py` for synthetic-only cap
  reservations, immutable scope/material/release checks, and one internal Outbox eligibility
  boundary;
- `migrations/versions/0006_add_crawler_execution_control_plane.py` for append-only crawler
  execution requests, owner approval and dispatch binding;
- `migrations/versions/0007_add_crawler_execution_results.py` for append-only crawler results
  and the Outbox-only crawler completion function;
- `migrations/versions/0008_add_synthetic_submission_execution.py` for canonical release
  evidence reservation fields, append-only kill switches, a separate reservation execution
  guard and Outbox-only synthetic prepare/receipt/ambiguity functions;
- `migrations/versions/0009_add_release_qualification_control_plane.py` for immutable release
  qualification, evidence and decision records;
- `migrations/versions/0010_add_crawler_source_registry.py` for configurable crawler
  manifest snapshots, scheduler source/run state, canonical dedupe/evidence rows and narrow
  scheduler/evidence-guard functions;
- `migrations/versions/0011_add_goal_runs.py` for owner-scoped durable GoalRun records,
  checkpoint/event/review tables, optimistic version/fencing checks, idempotent
  `SECURITY DEFINER` transitions, reviewed crawler evidence bridging and authenticated
  review/resume/cancel state;
- `migrations/versions/0012_add_gmail_readonly_sync.py` for owner-scoped Gmail read-only
  accounts, sync runs, metadata-only signals, reviewed proposals, command receipts, append-only
  guards and separate API/mailbox/read-only grants;
- `migrations/versions/0013_add_gmail_send_channel.py` for the reviewed Gmail send database
  boundary: separate Gmail send accounts, drafts, reservations, review evidence, reconciliation
  jobs, command receipts, narrow API functions and `careerops_mail_sender` execution functions
  that do not widen the G011 read-only mailbox grants;
- `migrations/versions/0014_add_greenhouse_submit_channel.py` for the reviewed Greenhouse Job
  Board submit boundary: employer-authorized account registration, exact reviewed payload
  evidence, transactional cap reservation/outbox eligibility, command receipts, mandatory
  reconciliation jobs and narrow `careerops_greenhouse_sender` execution/reconciliation
  functions;
- `docs/adr/0013-reviewed-gmail-send.md` and
  `docs/runbooks/reviewed-gmail-send.md` for the operational G012/G015 Gmail send boundary and
  its live-qualification caveats;
- `docs/adr/0014-reviewed-greenhouse-job-board-submit.md` and
  `docs/runbooks/reviewed-greenhouse-submit.md` for the operational G013 Greenhouse boundary, its
  official Greenhouse Job Board API references and its live-qualification caveats;
- `src/careerops/application/goal_run.py`,
  `src/careerops/infrastructure/database/goal_run.py`,
  `src/careerops/infrastructure/goal_run_operator.py`,
  `src/careerops/api/goal_runs.py`,
  `src/careerops/cli/goal_runs.py`,
  `src/careerops/workflows/goal_run.py` and
  `src/careerops/infrastructure/temporal/goal_run_repository.py` for the current G010
  application, repository, operator API, read-only CLI and Temporal adapter contracts;
- `src/careerops/api/gmail_readonly.py`,
  `src/careerops/infrastructure/gmail_operator.py`,
  `src/careerops/infrastructure/database/gmail_readonly.py`,
  `src/careerops/infrastructure/gmail/client.py`,
  `src/careerops/infrastructure/gmail/credentials.py`,
  `src/careerops/infrastructure/gmail/worker.py` and
  `src/careerops/cli/gmail_readonly.py` for the current G011 operator API, database adapters,
  exact-read-only Gmail client, Unix broker client protocol, polling worker and CLI;
- `src/careerops/api/source_registry.py`,
  `src/careerops/application/crawler_source_registry.py` and
  `src/careerops/infrastructure/database/crawler_source_registry.py` for the current
  source-registry operator API, domain contract and repository implementation;
- `src/careerops/infrastructure/database/engine.py` for runtime `SET ROLE` binding;
- `src/careerops/infrastructure/database/submission_dispatch.py` and
  `src/careerops/infrastructure/database/submission_outbox.py` for the G005 synthetic
  reservation and receipt repositories;
- `src/careerops/application/outbox.py` and
  `src/careerops/infrastructure/database/outbox.py` for the generic Outbox publisher and
  lease lifecycle owner;
- `tests/integration/test_migrations.py`,
  `tests/integration/test_crawler_source_registry_control_plane.py` and
  `tests/integration/test_canonical_job_ingestion_db.py` for the exercised real-PostgreSQL paths.

The matrix uses `R` for `SELECT`, `I` for `INSERT`, `U` for the column-scoped updates defined
below, `EXEC` for function execution, and `—` for no privilege. No M0 runtime capability
receives table `DELETE`.

## Synthetic bounded-autopilot boundary

Revision `0005` grants the API role a column-scoped append-only insert into
`autopilot_cap_reservations`; the database trigger overwrites caller time with database time,
checks an unrevoked/unexpired exact autopilot authorization, exact policy outcome, immutable
payload identity, granted action/channel/target/material/release scope and all caps. It accepts
only a `.test` target and `synthetic:*` channel, then the repository can enqueue the existing
internal `workflow_signal` event once and append an audit event.

Revision `0008` makes `release_evidence_hash` and `release_evidence_expires_at` the canonical
release-evidence reservation fields. A separate reservation execution guard requires current
release evidence and rejects new reservations while the latest global, campaign or synthetic
provider kill-switch row is active. Kill switches are append-only events, not mutable flags.

This is not a real-provider side-effect capability: `careerops_side_effect` remains dormant,
no runtime role receives provider credentials or direct provider-receipt insertion authority,
and the default Outbox publisher has no sink unless an internal sink is explicitly configured.
The G005 synthetic sink is a
no-network provider boundary; it does not prove real provider credentials, browser automation,
Gmail access or any external application submission path. A deployment must run the disposable
PostgreSQL migration test before relying on the trigger semantics; local unit evidence cannot
prove PL/pgSQL execution or role ACLs.

## Implemented role invariants

| Role | M0 purpose | Implemented boundary |
| --- | --- | --- |
| `careerops_api` | User-facing reads and trusted internal state transitions | Cannot alter evidence/provenance identities, content expiry/deletion state, provider receipts or credentials |
| `careerops_workflow` | Temporal Activities for durable orchestration | Can checkpoint GoalRuns, inspect reviewed crawler execution evidence, claim/finalize crawler source runs, ingest canonical public-ATS artifacts and call the narrow G014 Gmail composition wrappers for match/draft/outbox-enqueue/reconciliation inspection; cannot review, resume, cancel, access credentials, call provider APIs, mutate Gmail tables directly or dispatch Greenhouse/browser writes |
| `careerops_mailbox` | Gmail read-only mailbox polling | Can claim queued Gmail sync runs and execute database-owned functions that record redacted/hash-only message signals and proposals; cannot register accounts, review proposals, send email, mutate Gmail, access provider receipts or directly update business state |
| `careerops_mail_sender` | Reviewed Gmail send execution and reconciliation | Can execute only Gmail-send prepare/claim/release/receipt/ambiguity/reconciliation functions; cannot use G011 mailbox functions, directly insert receipts, review proposals, mutate payload/policy/grants or update application projections |
| `careerops_greenhouse_sender` | Reviewed Greenhouse Job Board submission and reconciliation | Can execute only Greenhouse prepare/receipt/ambiguity/reconciliation functions; cannot register accounts, review packets, mutate payload/policy/grants, access raw API keys, update application projections or browse unrelated application data |
| `careerops_retention` | Expiry marking, fenced blob purge and orphan tombstones | Updates only expiry/blob lifecycle columns; cannot mutate business or evidence rows |
| `careerops_outbox` | Lease and publish internal workflow signals/notifications | Reads only Outbox rows and updates only lease/delivery columns |
| `careerops_side_effect` | Reserved for non-Gmail external writes | Dormant for Gmail send; G012/G015 uses `careerops_mail_sender` instead |
| `careerops_readonly` | Operational inspection | Read-only; OAuth `secret_handle` is excluded from its column grant |

The bootstrap creates all capability roles as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
NOREPLICATION NOBYPASSRLS`. Those attributes are reapplied even when a role already exists.
It also removes memberships held *by* a capability role so one capability cannot inherit
another. It intentionally does not create service logins or remove memberships granted *to*
service logins.

The runtime engine accepts `api`, `workflow`, `mailbox`, `retention`, `outbox`, `mail_sender`,
`greenhouse_sender` and `readonly`. On every pool checkout it executes `RESET ROLE`, assumes the
corresponding fixed `careerops_*` role, fixes `search_path` to `pg_catalog, careerops`, and rejects
the session unless its login is `NOINHERIT`, non-owner, non-elevated and an immediate member of
exactly that one role. `side_effect` is not a configurable runtime role for Gmail send or
Greenhouse submit. G012/G015 Gmail send uses the dedicated `mail_sender` role and remains disabled
unless config/release/OAuth/broker gates pass. G013 Greenhouse submit uses the dedicated
`greenhouse_sender` role and remains disabled unless config/release/broker/authorization gates
pass.
The PostgreSQL suite creates a real
non-owner login, proves `RESET ROLE` has no business-table access, and proves checkout rebinds
the capability.

## Implemented table grant matrix

`R*` means the explicit safe OAuth column list shown later. Retention's `I` on
`content_blobs` is a column-scoped insert used only for a claimed orphan tombstone; the
lifecycle trigger accepts it only with the fixed `write-orphan-reconciler` owner, a complete
lease/token tuple and no final metadata. A deferred constraint also rejects any committed
active blob without a logical reference; Retention cannot insert that reference.

| Table | API | Retention | Outbox | Side effect | Readonly |
| --- | --- | --- | --- | --- | --- |
| `candidates` | R/I/U | — | — | — | R |
| `companies` | R/I/U | — | — | — | R |
| `job_sources` | R/I/U | — | — | — | R |
| `content_blobs` | R/I | R/I/U | — | — | R |
| `content_objects` | R/I | R/U | — | — | R |
| `canonical_jobs` | R/I/U | — | — | — | R |
| `job_postings` | R/I/U | — | — | — | R |
| `job_posting_versions` | R/I | R | — | — | R |
| `job_merge_decisions` | R/I | — | — | — | R |
| `job_posting_assignments` | R/I/U | — | — | — | R |
| `job_aliases` | R/I | — | — | — | R |
| `evidence_records` | R/I | R | — | — | R |
| `application_events` | R/I | — | — | — | R |
| `oauth_credential_references` | — | — | — | — | R* |
| `action_intents` | R/I/U | — | — | — | R |
| `action_payload_versions` | R/I | — | — | — | R |
| `policy_decisions` | R/I | — | — | — | R |
| `approval_requests` | R/I/U | — | — | — | R |
| `outbox_events` | R/I | — | R/U | — | R |
| `side_effect_attempts` | — | — | — | — | R |
| `provider_receipts` | — | — | — | — | R |
| `audit_events` | R/EXEC† | — | — | — | R |
| `console_users` | R/I/U | — | — | — | — |
| `bootstrap_tokens` | R/I/U | — | — | — | — |
| `console_sessions` | R/I/U | — | — | — | — |
| `autopilot_campaigns` | R/I | — | — | — | R |
| `autopilot_grant_versions` | R/I | — | — | — | R |
| `autopilot_grant_revocations` | R/I | — | — | — | R |
| `autopilot_intent_authorizations` | R/I | — | — | — | R |
| `autopilot_review_items` | R/I | — | — | — | R |
| `autopilot_cap_reservations` | R/I‡ | — | — | — | R |
| `crawler_execution_requests` | R/I | — | R | — | R |
| `crawler_execution_approvals` | R/I | — | R | — | R |
| `crawler_execution_dispatches` | R/I | — | R | — | R |
| `crawler_execution_results` | R | — | R/EXEC§ | — | R |
| `autopilot_kill_switch_events` | R/I | — | — | — | R |
| `crawler_source_registries` | R/I | — | — | — | R |
| `crawler_source_registry_sources` | R/I/EXEC¶ | — | — | — | R |
| `crawler_source_runs` | R/EXEC¶ | — | — | — | R |
| `crawler_job_deduplication_keys` | R/I | — | — | — | R |
| `crawler_job_ingestion_evidence` | R/I | — | — | — | R |
| `goal_runs` | R/EXECG | — | — | — | R |
| `goal_run_checkpoints` | R/EXECG | — | — | — | R |
| `goal_run_events` | R/EXECG | — | — | — | R |
| `goal_run_review_items` | R/EXECG | — | — | — | R |
| `goal_run_review_decisions` | R/EXECG | — | — | — | R |
| `goal_run_command_receipts` | EXECG | — | — | — | — |

† `EXEC` is not a table privilege: the API executes the database-owned
`careerops.append_audit_event(...)` function. It has no direct `INSERT` on `audit_events` and
no direct usage/select privilege on the backing identity sequence.

‡ `I` is a strict column-scoped insert. The reservation date and time are database-owned, and
the append-only reservation trigger must validate the exact grant, authorization, payload,
release, target, material, channel and cap before it accepts the row. Revision `0008` adds the
canonical release-evidence pair to that same reservation row and a separate execution guard.

§ `EXEC` is not a table privilege: Outbox executes
`careerops.complete_crawler_execution_outbox_event(...)`, which validates the leased
`workflow_signal`, records one append-only result, and moves the bound Outbox event/action
intent to a terminal state.

¶ `EXEC` is not a table privilege. Revision `0010` grants the API database-owned
`SECURITY DEFINER` claim, complete and fail functions. Those functions alone mutate source lease,
cursor, attempt, cadence and result state and append run events. The API has no direct `UPDATE` on
source rows and no direct `INSERT` on run events. Enablement and other configuration changes require
a new immutable manifest/registry version.

G `EXEC` is not a table privilege. Revision `0011` grants the API database-owned
`SECURITY DEFINER` GoalRun functions for create/status/list, review-decision, cancel and resume.
The workflow role, not the API role, receives checkpoint and request-review execution. Those
functions alone mutate run status, phase, version, fencing-sensitive checkpoints, events, command
receipts and review decisions. The API has no direct `INSERT`, `UPDATE` or `DELETE` on GoalRun
tables. Readonly receives table `SELECT` only.

The workflow role is intentionally absent from the legacy table matrix columns above because it
is a G010 orchestration role, not an M0 user-facing/runtime role. Its grants are limited to:

- `SELECT` on GoalRun records, checkpoints and review items needed to reload DB-authoritative
  workflow state;
- `EXECUTE` on GoalRun status, checkpoint, request-review and reviewed-crawler-result functions;
- `EXECUTE` on crawler source claim/complete/fail functions;
- `SELECT`/`INSERT` and narrow update grants required for hash-bound public-ATS canonical
  ingestion evidence, using the same immutable identity columns as the API path.

It receives no review-decision, resume or cancel function, no GoalRun command-receipt table
grant, no provider credential grant and no external write authority.

Revision `0012` adds the Gmail read-only mailbox boundary. The API role receives only
database-owned functions for owner-scoped account registration/list/status, sync requests,
proposal listing/review, reset-history and revoke. The mailbox role receives only `SELECT` on
`gmail_accounts`/`gmail_sync_runs` plus `SECURITY DEFINER` functions to claim sync runs,
record metadata-only signals/proposals, defer incomplete pages, complete runs and fail runs. It
does not receive direct table `INSERT`, `UPDATE` or `DELETE` on Gmail evidence tables, cannot
review proposals and cannot access OAuth tokens. The read-only role can inspect Gmail summary and
evidence tables, while credential handles remain excluded from API responses. The default Compose
profile sets `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`; no checked-in database grant configures a
broker server, refresh-token vault, Gmail send path or provider-write authority.

Revision `0013` adds the Gmail send reviewed-outbox boundary. The API role receives only
database-owned functions to register/list/status send accounts, create exact-payload drafts,
review drafts, reserve reviewed intents and list reservations. Active send accounts require an
explicit G011 `reconciliation_gmail_account_id` binding; the send credential is stored as an
opaque `gmail_send` credential reference with exact `gmail.send`, while reconciliation joins to
the separate G011 `gmail` credential reference with exact `gmail.readonly`.

The `careerops_mail_sender` role receives only Gmail-send-specific prepare/claim/mark/release,
receipt, ambiguity and reconciliation functions. It does not receive G011 mailbox functions,
direct table mutation authority, raw credential access, direct `provider_receipts` insertion,
payload/policy/grant review authority or application-projection update authority. The checked-in
Compose runtime remains fail-closed by default: it does not start the host-side OAuth/Keychain
broker or attachment broker, contains no live credential material and has no live send
qualification. Desktop OAuth, native Keychain storage and the Unix broker are implemented as
separate macOS-host processes; their existence does not grant this database role provider-write
authority or enable any send gate.

Revision `0014` adds the Greenhouse Job Board submit reviewed-outbox boundary. The API role
receives only database-owned functions to register employer-authorized Greenhouse accounts, create
exact reviewed drafts, record review decisions and reserve/enqueue reviewed intents. The
reservation path binds owner, account, campaign, grant, authorization, payload hash, board token
hash, job id hash, schema hash, review evidence, reviewed material and release qualification. It
also extends the cap-reservation guard for the Greenhouse channel and fixed
`boards-api.greenhouse.io` provider origin.

Revision `0016` repairs two Gmail function invariants without widening grants: readonly account
status uses a qualified owner lookup, and an idempotent send-account registration cannot rebind an
existing readonly reconciliation account.

Revision `0017` qualifies the readonly proposal preflight lookup and rejects reviewed-send
reservation replays whose immutable account, intent, application, recipient or evidence bindings
differ from the original command. It does not widen table or function grants.

Revision `0018` makes mailbox signal/proposal, human review and send-reconciliation replay
identities exact. Changed same-key semantic evidence raises a conflict; exact retries preserve the
original rows and response metadata. Function signatures, fixed search paths and capability-role
grants remain unchanged.

The `careerops_greenhouse_sender` role receives only Greenhouse prepare, receipt, ambiguity and
reconciliation functions. It does not receive account-registration, review, payload mutation,
policy/grant mutation, application-projection update or raw credential access. The broker, not the
database role or worker, owns provider egress and constructs Basic Auth inside the fixed-origin
Greenhouse POST path. Job Board API keys must never be returned to workers or written to database
rows, logs, command receipts, audit events or API responses.

The checked-in `greenhouse-submit` Compose profile runs only the reviewed worker path with the
`greenhouse_sender` role. Its runtime defaults leave external writes, the separate auto-submit
switch, Greenhouse enablement and release attestations false, and it mounts only the opaque Unix
credential/attachment broker socket directory. It does not mount API keys, provider secrets,
object storage or arbitrary provider data into the worker container. Live deployment still
requires an external authorized broker/key profile and employer-authoritative reconciliation
evidence.

Every bounded Greenhouse Job Board 2xx records only `accepted_unverified` evidence and creates
mandatory reconciliation work. It cannot mark an application confirmed and it is not permission to
retry. Ambiguous outcomes also stop without retry. `confirmed` requires independent employer-side
authority or reviewed human evidence; Gmail signals are clues only. V1 payload eligibility is
limited to reviewed `first_name`, `last_name`, `email`, `phone`, `resume` and `cover_letter`.
Custom questions, location/location tuple, website, legal/compliance statements, demographic
questions, assessments, payment, identity, signatures, CAPTCHA, MFA, account flows and unknown
fields remain manual.

Revision `0008` grants Outbox only these additional `SECURITY DEFINER` functions:
`careerops.prepare_synthetic_submission_outbox_event(...)`,
`careerops.record_synthetic_submission_outbox_receipt(...)` and
`careerops.record_synthetic_submission_outbox_ambiguity(...)`. Outbox receives no direct
`INSERT`, `UPDATE` or `SELECT` grant on `side_effect_attempts`, `provider_receipts` or
`autopilot_kill_switch_events` for the synthetic path. The functions validate the leased
synthetic `workflow_signal`, reservation binding, current grant/authorization/release evidence
and kill-switch state before creating or finalizing the database-owned synthetic attempt and
receipt rows.

Revision `0010` adds the configurable crawler source registry. The registry manifest table is
append-only. The source rows hold scheduler runtime state for fixed public crawler adapters:
enabled flag, safe dataset paths, hashes, cursor, cadence, retry/budget/rate-limit metadata,
robots/terms policy, ingestion/provenance/dedupe policy and last run status. The authenticated
source-registry API supports register/list/due, lease-fenced claim/complete/fail, and run-bound
public-ATS canonical ingestion. Scheduler transitions use database time, locked due-source
selection, bounded leases and immutable run events. Canonical ingestion requires a matching
successful run event, output manifest hash, source policy, deterministic dedupe rule and provenance
policy. This authority schedules reviewed public crawlers only; it does not execute crawler
commands, log in, browse, or authorize application submission.

The Outbox event-type constraint allows only `workflow_signal` and
`internal_notification`. A provider-write event requires an independently released M5A path;
adding it to this base table without database row isolation or a narrowly granted function
would invalidate the M0 boundary.

## Implemented column grants

All columns not listed here are denied to the role. Table-wide `UPDATE` is not granted.

| Role | Table | Only updateable columns |
| --- | --- | --- |
| API | `candidates` | `display_name`, `updated_at` |
| API | `companies` | `name`, `official_domains`, `terms_status`, `updated_at` |
| API | `job_sources` | `state`, `verified_at`, `last_discovery_at`, `updated_at` |
| API | `canonical_jobs` | `aggregate_state`, `primary_posting_id`, `updated_at` |
| API | `job_postings` | `source_state`, `last_seen_at`, `closed_at`, `updated_at` |
| API | `job_posting_assignments` | `canonical_job_id`, `decision_id`, `assigned_at` |
| API | `action_intents` | `status`, `current_payload_version_id`, `updated_at` |
| API | `approval_requests` | `decision`, `decision_rule_reference`, `decided_at` |
| Retention | `content_objects` | `expired_at` |
| Retention | `content_blobs` | `deletion_state`, `delete_lease_owner`, `delete_lease_token`, `delete_lease_until`, `deleted_at`, `delete_result` |
| Outbox | `outbox_events` | `status`, `available_at`, `lease_owner`, `lease_token`, `lease_until`, `attempt_count`, `published_at`, `last_error_code` |
| API | `console_users` | password hash/algorithm/parameters, password-change, disable and update timestamps only |
| API | `bootstrap_tokens` | use/revocation state only |
| API | `console_sessions` | state, touch/expiry, revoke and rotation-link fields only |

`crawler_source_registry_sources` intentionally has no API-updateable columns. Its static columns
are inserted only as part of a validated immutable registry snapshot; its runtime columns are
changed only by the `0010` scheduler functions.

For the base lifecycle tables, the API can insert only the lifecycle-safe columns of
`content_blobs`, `content_objects`, `approval_requests` and `outbox_events`. The append-only
bounded-autopilot, crawler-execution and kill-switch tables use their own column-scoped insert
grants in revisions `0004` through `0008`. Retention can insert only `id`, digest/key/size,
`deletion_state` and the orphan lease owner/token/until in `content_blobs`.

No runtime role can update `content_objects.retired_at`. That is deliberate fail-closed
behavior until a replay-safe/no-evidence-required retirement verifier exists, but it also
means ordinary end-to-end purge is not an implemented M0 path.

Console credentials are separate M0 tables, making the current total **25** business and
console-authentication tables. The API capability can select the hashed credential/session
fields because it performs authentication; readonly, retention, outbox, and side-effect roles
receive no access to those three tables. Passwords are Argon2id hashes and bootstrap/session
tokens are SHA-256 hashes, not recoverable raw credentials.

The bounded-autopilot control plane added in revision `0004`, its synthetic reservation table
in revision `0005`, crawler execution tables in revisions `0006` and `0007`, and the
kill-switch table in revision `0008` raise the current total to **36** tables. The five
control-plane tables and the reservation table are deliberately append-only: API can read and
insert Campaigns, Grant versions, Grant revocations, exact intent authorizations, review items
and a strictly guarded synthetic cap reservation, while Readonly can only inspect them. No
runtime role can update or delete these rows, and neither Outbox nor Side-effect receives any
control-plane privilege. Revocation, supersession, manual-only exceptions, review work and kill
switches are represented by new rows rather than mutation. Review vocabulary is intentionally
aligned to the application surface: `review_kind` is `pending`, `exceptions` or `recovery`, and
`resolution_mode` is `agent_approvable`, `manual_only` or `remediation_required`. The database
rejects `manual_only` rows that are still in the `pending` review kind so a prohibited/manual
path cannot be shown as ordinary agent-approvable queue work.

The API can also read and insert the three crawler execution control-plane tables added in
revision `0006`; Outbox and Readonly can inspect them. The request, approval and dispatch
guards require owner approval, unexpired requests, a `crawler-execution:*` key, a pending
internal `workflow_signal`, and exact action-intent/request/event binding. Revision `0007`
adds one append-only result table. Outbox can inspect it and execute one narrow completion
function; it cannot insert results directly.

Revision `0009` adds three release-qualification tables, revision `0010` adds five
crawler-source tables, revision `0011` adds six durable GoalRun tables and revision `0014` adds
six Greenhouse submit tables, raising the current SQLAlchemy metadata table count to **68**. The immutable registry
table records the
normalized manifest and hash identity. Source rows are runtime scheduler state for
already-registered public adapters; run events, dedupe keys and ingestion evidence are append-only.
They are not provider credentials, browser sessions or arbitrary crawl targets. The API surface
remains a control plane: registering manifests, listing registries/due sources, claiming/finalizing
scheduler leases and ingesting hash-bound structured ATS artifacts. It does not execute crawlers
directly or authorize application submission.

The implemented G009 scheduler boundary is database-owned:

- claim must select one due, enabled, `robots_terms_policy = 'respect'` source using a locked
  server-side transition and bounded lease duration;
- claim must set lease owner/token/expiry and increment/record attempt state without letting the
  caller choose stale scheduling timestamps;
- completion and failure must require the exact active lease token and owner, reject expired or
  stale leases, clear lease state, append run evidence and schedule either the next cadence or a
  bounded retry;
- `review_required` and `blocked` sources must fail closed;
- canonical ingestion must retain source URL, captured time, content hash, manifest/source/run
  binding, parser/rule version and deterministic dedupe evidence;
- semantic similarity can create review proposals only and must not be sufficient for automatic
  canonical-job merge.

If a deployment has broad direct `UPDATE` on `crawler_source_registry_sources`, it is not at the
documented `0010` boundary. Revoke that grant and upgrade before attaching scheduler workers.

Revision `0011` adds the durable GoalRun control plane. PostgreSQL owns actor visibility,
generated run/fencing identities, optimistic version checks, idempotency convergence, checkpoint
state, command receipts, pending review item/snapshot hash, review decision rows and terminal
state. Temporal owns phase ordering, replay, retries, review/resume/cancel waits, 30-second
database polling while blocked or awaiting review, and continue-as-new. Workflow signals are wake
hints only; the workflow reloads the owner-scoped database record before treating review, resume
or cancel as authorized.

The canonical GoalRun statuses are `starting`, `running`, `waiting_review`, `blocked`,
`reconciliation_required`, `completed`, `failed`, `cancelled` and `rejected`. The canonical phases
are `initializing`, `selecting_source`, `claiming_source`, `discovery`, `complete_source`,
`canonical_ingest`, `matching`, `draft_preparation`, `review`, `dispatch`, `reconciliation`,
`completed`, `blocked`, `failed`, `cancelled` and `rejected`. Terminal states require a finalized
checkpoint transition and cannot be converted back by direct table mutation.

G010 does not add provider credentials or external write authority. G014 binds GoalRun to the
reviewed Gmail path by letting `careerops_workflow` call database-owned wrappers that record one
match from reviewed crawler provenance, prepare an exact Gmail payload draft, enqueue only after
the exact GoalRun approval/grant/release/cap/kill-switch checks pass, and inspect outbox, receipt
and reconciliation state. The workflow role still never receives OAuth material, never calls Gmail,
never writes Gmail send tables directly and never marks provider receipt evidence on its own.

G014 does not compose Greenhouse or browser submission. G011 adds only the separate
default-disabled Gmail read-only signal boundary, G012/G015 adds the separate default-disabled
Gmail send worker/outbox boundary, and G013 adds separate default-disabled Greenhouse Job Board
submit scaffolding. None of these make live provider effects available without account bootstrap,
broker/vault services, release qualification and runtime evidence.

## Audit append boundary

Revision `0003` removes direct API `INSERT` and audit-sequence access. Its database-owned
`SECURITY DEFINER` function fixes `search_path` to `pg_catalog, careerops`, takes a PostgreSQL
transaction advisory lock, reads the current tail, computes the SHA-256 event hash, and inserts
the new row as one server-side operation. `PUBLIC` execution is revoked; `careerops_api`
receives only `EXECUTE`. This closes the same-capability direct-insert path for audit rows and
makes PostgreSQL, rather than caller-supplied hashes, authoritative for each append.

This security-tightening migration is deliberately **not zero-downtime rolling-compatible as
written**. Once the grant is revoked, an old application instance that directly inserts audit
rows fails; a new instance cannot run before the function exists. Deploy migration `0003` and
the matching application lockstep, or split function creation/application rollout/direct-grant
revocation into a reviewed two-phase migration and drain old writers before revocation.

The following identities remain immutable after insert for every M0 runtime role:

- primary keys and provenance foreign keys;
- company/source/posting normalized identities and canonical URLs;
- blob digest, object key and byte size;
- logical blob binding, owner, classification and retention policy;
- every evidence field and append-only payload/policy/event/receipt/audit field;
- Outbox `event_key`, event type and action/payload binding.

Nineteen tables have owner-level append-only triggers. Three active-blob reference triggers,
monotonic content-object lifecycle checks, a deferred active-blob/reference constraint and the
content-blob lifecycle trigger fence new references and deletion; a separate Outbox lifecycle
trigger enforces claim/reclaim/publish/release transitions.

The autopilot authorization table binds `campaign_id + grant_version_id`,
`action_intent_id + payload_version_id + payload_hash`, and
`action_intent_id + payload_version_id + policy_decision_id` through composite foreign keys.
It records a distinct `allow_autopilot_submission` outcome, and a database-owned insert guard
re-reads the exact referenced policy decision for the same intent and payload version. The guard
rejects autonomous authorization unless `policy_decisions.decision` is exactly
`allow_autopilot_submission`; generic `allow`, deny, expired autopilot evidence, and mismatched
rulesets cannot authorize autonomous submission. The same guard verifies the payload hash,
requires the policy decision ruleset to match the grant `policy_ruleset_version`, requires an
explicit non-expired policy-decision expiry, and rejects authorization expiry beyond either the
grant or policy-decision expiry. It rejects authorization timestamps before grant creation or
after database `CURRENT_TIMESTAMP`, grant versions expired at insertion time, authorizations at
or after grant expiry, and any grant version with a revocation row regardless of caller-supplied
`authorized_at`. A separate revocation guard requires the campaign owner and prevents a
superseding grant from crossing campaign boundaries, while a grant-version guard binds
`subject_actor` to that campaign owner. Revision `0005` adds transaction-bound cap reservation
only for `.test` synthetic fixtures: it can create one append-only reservation, one internal
`workflow_signal` eligibility record and one audit event, but it cannot create a provider-write
event, side-effect attempt or provider receipt. Revision `0008` reserves release evidence on
that row, checks append-only kill-switch events before reservation and dispatch preparation,
and permits Outbox to create or finalize synthetic side-effect attempt/receipt rows only
through narrow database-owned functions. The generic `OutboxPublisher` remains the sole owner
of the event claim, release, retry and publish lifecycle. Any future real provider path
requires a separately released, evidenced migration.

## Credential boundary

`careerops_side_effect` has an empty grant tuple and cannot be selected in runtime settings.
M5A/G012 activation requires a separate migration and release record. A database grant alone never
proves that a Gmail send adapter, broker, vault, credential, receipt path or token is safely
released.

`careerops_readonly` may select only these OAuth reference columns:

`id`, `provider`, `account_subject`, `granted_scopes`, `status`, `issued_at`, `revoked_at`,
`created_at`, `updated_at`.

It cannot select `secret_handle` directly. A production inspection must also prove that no
view, function, inherited membership or restore-time ACL re-exposes that column.

## Closed code-level gaps

The following earlier static gaps are closed in the current tree:

- all runtime capability roles, including retention and mailbox, are created and unconditionally
  hardened;
- API updates are column-scoped and evidence/content identities are immutable;
- retention expiry, deletion and orphan-tombstone columns are narrowly granted;
- the Side-effect role has no privileges or runtime selection path for Gmail send;
- readonly OAuth access excludes `secret_handle`;
- Outbox updates are column-scoped, token-fenced and limited to internal event types;
- content expiry is monotonic and cannot precede the stored retention deadline;
- API audit writes are restricted to the database-owned append/hash function, with no direct
  audit-table insert or sequence access;
- runtime checkouts validate a single non-owner `NOINHERIT` login membership, bind one selected
  capability role and fix the search path;
- crawler source config is immutable by registry version, schedule controls are adapter-bounded,
  source runtime mutation is function-owned and token-fenced, and canonical ingestion evidence is
  guarded by a matching successful run and output artifact hash.
- Gmail read-only sync uses a separate mailbox role, exact-read-only broker client contract,
  metadata-only Gmail client, owner-scoped database functions and redacted/hash-only proposal
  storage; default runtime configuration leaves OAuth disabled and no credential configured.
- Gmail send uses `careerops_mail_sender` rather than `careerops_side_effect`: it has only
  Gmail-send-specific prepare/claim/release/receipt/ambiguity/reconciliation functions, requires
  separate exact `gmail.send` and `gmail.readonly` credential references plus explicit G011
  reconciliation binding, and remains disabled/unconfigured without deployment-owned brokers,
  vault, live credentials and release qualification.
- Greenhouse submit uses `careerops_greenhouse_sender` rather than `careerops_side_effect`: it has
  only Greenhouse-specific prepare/receipt/ambiguity/reconciliation functions, requires an
  employer-authorized Job Board API key profile, broker-owned fixed-origin egress, exact
  schema/material hash binding, mandatory reconciliation for every 2xx or ambiguous provider
  outcome, and remains disabled/unconfigured without deployment-owned broker, live key evidence
  and release qualification. The dedicated Compose worker profile mounts only opaque Unix broker
  sockets and no API-key, secret or object-data volume.

## Remaining deployment and acceptance risks

These are real gaps; passing local unit tests does not waive them:

1. **Production service-login provisioning.** The repository deliberately does not create
   executable production logins or memberships. A disposable PostgreSQL test proves one real
   non-owner `NOINHERIT` API login, single-role membership, `RESET ROLE` denial and checkout
   rebinding; deployed API/Retention/Outbox/Readonly identities still require equivalent
   environment-specific evidence and a direct-grant audit.
2. **Ownership separation.** Tests do not yet assert that runtime logins/capability roles own
   no schema, table, sequence, function or trigger. Migrations must run as a separate owner.
3. **`PUBLIC` and default ACL evidence.** The migration revokes `PUBLIC` schema access,
   revokes default function execution for the migration owner and revokes direct execution on
   the installed guard functions. An exhaustive real-database assertion across schema,
   tables, sequences, functions and future-object default ACLs is still missing. Default ACLs
   are owner-specific and can drift when migrations run under another identity.
4. **Restore fidelity.** No clean restore has proved role attributes, memberships, owners,
   ACLs, default ACLs and function security properties together.
5. **Coverage depth.** Existing `SET ROLE` tests exercise selected positive and negative
   paths, not every table/column, `DELETE`/`TRUNCATE`, role-membership edge, elevated view or
   concurrent lease case in this matrix.
6. **Lifecycle completion.** The retirement verifier is absent. Adding a broad `retired_at`
   grant to make purge move would violate the evidence boundary; implement and test the
   verifier first.
7. **Same-capability claimant isolation.** Row predicates plus transaction-local fencing-token
   checks reject stale or omitted tokens, but a different session holding the same Retention or
   Outbox capability can read the current token and present it. Treat each capability login as
   one trust domain until direct lease-column grants are replaced by narrowly revoked
   `SECURITY DEFINER` claim/finalize functions or an equivalent server-side API.
8. **Audit migration rollout.** Revision `0003` closes the direct-write P1 but requires a
   lockstep deploy as written. Any rolling deployment must use the explicit two-phase sequence
   described above; temporarily retaining the old grant keeps the old risk open until every
   legacy writer has drained.
9. **Crawler source deployment parity.** The code-level `0010` boundary and disposable-database
   tests do not prove that a deployed database has the same revision, role grants, function
   ownership or worker configuration. Keep scheduler workers disabled in an environment until its
   own non-owner login proves token fencing, robots/terms fail-closed behavior, bounded schedule
   controls, append-only run/evidence history and denial of direct source-row updates.
10. **Greenhouse production qualification.** The code-level `0014` boundary does not prove that an
    employer-authorized Job Board API key, broker-owned egress, live Greenhouse smoke,
    reconciliation evidence, revocation path, caps or kill switches are deployed. Keep Greenhouse
    submit disabled until the environment proves those controls with a target employer that
    explicitly authorized the integration.

## Required acceptance evidence

Before treating this matrix as a deployed control, a disposable PostgreSQL run must:

- connect as the actual non-owner service logins and prove allowed and forbidden `SET ROLE`
  paths;
- inspect `pg_roles`, `pg_auth_members`, object ownership, `PUBLIC` privileges,
  `information_schema` grants and `pg_default_acl`;
- execute representative allowed writes plus every immutable-column, credential,
  side-effect, audit direct-`INSERT`, `DELETE` and `TRUNCATE` denial and assert the expected
  SQLSTATE; prove API `EXECUTE` on `append_audit_event` and denial for `PUBLIC`;
- prove stale/wrong retention and Outbox tokens cannot finalize and that a new reference
  cannot race a deletion claim;
- prove stale/wrong crawler-source scheduler lease tokens cannot complete/fail, blocked or
  review-required sources cannot be claimed, canonical-ingestion provenance cannot be orphaned,
  semantic-only dedupe cannot auto-merge, and `careerops_api` cannot bypass scheduler functions
  with broad direct source-row updates;
- prove Greenhouse submit account registration, exact review evidence, cap reservation/outbox
  eligibility, sender prepare, `accepted_unverified` receipt, ambiguity recording, no-retry
  reconciliation, revocation/kill-switch denial, raw-key non-exposure and denial of direct
  table/provider-receipt/application-projection mutation;
- restore onto a clean PostgreSQL instance and repeat the inspection and denial suite.

Store the PostgreSQL version, migration revision, bootstrap hash and test output with the M0
gate evidence so grant drift is detectable.
