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
- `src/careerops/infrastructure/database/engine.py` for runtime `SET ROLE` binding;
- `tests/integration/test_migrations.py` for the currently exercised real-PostgreSQL paths.

The matrix uses `R` for `SELECT`, `I` for `INSERT`, `U` for the column-scoped updates defined
below, `EXEC` for function execution, and `—` for no privilege. No M0 runtime capability
receives table `DELETE`.

## Implemented role invariants

| Role | M0 purpose | Implemented boundary |
| --- | --- | --- |
| `careerops_api` | User-facing reads and trusted internal state transitions | Cannot alter evidence/provenance identities, content expiry/deletion state, provider receipts or credentials |
| `careerops_retention` | Expiry marking, fenced blob purge and orphan tombstones | Updates only expiry/blob lifecycle columns; cannot mutate business or evidence rows |
| `careerops_outbox` | Lease and publish internal workflow signals/notifications | Reads only Outbox rows and updates only lease/delivery columns |
| `careerops_side_effect` | Reserved for M5A external writes | Dormant: no schema usage, data grants or runtime configuration value |
| `careerops_readonly` | Operational inspection | Read-only; OAuth `secret_handle` is excluded from its column grant |

The bootstrap creates all five as `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
NOREPLICATION NOBYPASSRLS`. Those attributes are reapplied even when a role already exists.
It also removes memberships held *by* a capability role so one capability cannot inherit
another. It intentionally does not create service logins or remove memberships granted *to*
service logins.

The runtime engine accepts only `api`, `retention`, `outbox` and `readonly`. On every pool
checkout it executes `RESET ROLE`, assumes the corresponding fixed `careerops_*` role, fixes
`search_path` to `pg_catalog, careerops`, and rejects the session unless its login is
`NOINHERIT`, non-owner, non-elevated and an immediate member of exactly that one role.
`side_effect` is not a configurable M0 runtime role. The PostgreSQL suite creates a real
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

† `EXEC` is not a table privilege: the API executes the database-owned
`careerops.append_audit_event(...)` function. It has no direct `INSERT` on `audit_events` and
no direct usage/select privilege on the backing identity sequence.

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

The API can insert only the lifecycle-safe columns of `content_blobs`, `content_objects`,
`approval_requests` and `outbox_events`. Retention can insert only `id`, digest/key/size,
`deletion_state` and the orphan lease owner/token/until in `content_blobs`.

No runtime role can update `content_objects.retired_at`. That is deliberate fail-closed
behavior until a replay-safe/no-evidence-required retirement verifier exists, but it also
means ordinary end-to-end purge is not an implemented M0 path.

Console credentials are separate M0 tables, making the current total **25** business and
console-authentication tables. The API capability can select the hashed credential/session
fields because it performs authentication; readonly, retention, outbox, and side-effect roles
receive no access to those three tables. Passwords are Argon2id hashes and bootstrap/session
tokens are SHA-256 hashes, not recoverable raw credentials.

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

Eight tables have owner-level append-only triggers. Three active-blob reference triggers,
monotonic content-object lifecycle checks, a deferred active-blob/reference constraint and the
content-blob lifecycle trigger fence new references and deletion; a separate Outbox lifecycle
trigger enforces claim/reclaim/publish/release transitions.

## Credential boundary

`careerops_side_effect` has an empty grant tuple and cannot be selected in runtime settings.
M5A activation requires a separate migration and release record. A database grant alone never
proves that a provider adapter or token is safely released.

`careerops_readonly` may select only these OAuth reference columns:

`id`, `provider`, `account_subject`, `granted_scopes`, `status`, `issued_at`, `revoked_at`,
`created_at`, `updated_at`.

It cannot select `secret_handle` directly. A production inspection must also prove that no
view, function, inherited membership or restore-time ACL re-exposes that column.

## Closed code-level gaps

The following earlier static gaps are closed in the current tree:

- all five roles, including retention, are created and unconditionally hardened;
- API updates are column-scoped and evidence/content identities are immutable;
- retention expiry, deletion and orphan-tombstone columns are narrowly granted;
- the Side-effect role has no privileges or runtime selection path;
- readonly OAuth access excludes `secret_handle`;
- Outbox updates are column-scoped, token-fenced and limited to internal event types;
- content expiry is monotonic and cannot precede the stored retention deadline;
- API audit writes are restricted to the database-owned append/hash function, with no direct
  audit-table insert or sequence access;
- runtime checkouts validate a single non-owner `NOINHERIT` login membership, bind one selected
  capability role and fix the search path.

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
- restore onto a clean PostgreSQL instance and repeat the inspection and denial suite.

Store the PostgreSQL version, migration revision, bootstrap hash and test output with the M0
gate evidence so grant drift is detectable.
