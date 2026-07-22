# Database migrations

Migrations target PostgreSQL and the `careerops` business schema. The Alembic version table
stays in `public` so the first revision can create and the base downgrade can remove the
business schema cleanly. Migration connections force `search_path=public` before SQLAlchemy
detects the default schema; this prevents a login role named `careerops` from producing false
foreign-key drift during `alembic check`.

Run role bootstrap as a database administrator before the first upgrade when role-level
grants are required:

```bash
psql "$ADMIN_DATABASE_URL" \
  --file src/careerops/infrastructure/database/bootstrap_roles.sql
make migrate
```

Application processes must use separate, non-owner `NOINHERIT` login roles that are members
of exactly one NOLOGIN capability role and have no direct object grants. Runtime checkouts use
explicit `SET ROLE`; the migration never creates passwords, logins or memberships.

Audit events are appended through `careerops.append_audit_event(...)`, a `SECURITY DEFINER`
function with a fixed `pg_catalog, careerops` search path. The API capability role has
`EXECUTE` on that function, but no direct `INSERT` on `careerops.audit_events` and no direct
`USAGE` on the table identity sequence. This keeps the sequence, tail hash lookup, advisory
lock, and SHA-256 hash-chain calculation inside PostgreSQL. Revision `0003` is a security
tightening change, not a zero-downtime rolling-compatible change: old application instances
that still insert `audit_events` directly will fail after the grant is revoked, while new
instances require the function to exist. Deploy it lockstep with the matching application
version, or split the grant revocation into a later migration for a two-phase rolling deploy.
Downgrading `0003` restores the previous direct table/sequence grants for compatibility with
the old writer.

Authoritative revision sequence:

| Revision | Purpose |
| --- | --- |
| `0001` | Initial business schema, M0 capability grants and table lifecycle guards |
| `0002` | Console authentication tables and API-only credential/session grants |
| `0003` | Database-owned audit append function and direct audit-write revocation |
| `0004` | Append-only bounded-autopilot grants, authorizations and review work |
| `0005` | Synthetic-only cap reservations and internal Outbox eligibility events |
| `0006` | Append-only crawler execution request, approval and dispatch control plane |
| `0007` | Append-only crawler results and Outbox-only crawler completion function |
| `0008` | Synthetic submission execution boundary, canonical release evidence fields and append-only kill switches |
| `0009` | Synthetic release-qualification runs, immutable evidence-set binding and DB fail-open rejection |
| `0010` | Configurable crawler registries, token-fenced source scheduling, run audit and canonical-ingestion provenance |
| `0011` | Durable GoalRun state machine, owner/fencing checkpoints and simple review records |
| `0012` | Read-only Gmail sync, reviewed signal proposals and mailbox capability boundary |
| `0013` | Reviewed Gmail send channel with exact-payload drafts, outbox send and reconciliation workers |
| `0014` | Reviewed Greenhouse Job Board submit channel with fixed-origin broker and reconciliation |
| `0015` | GoalRun Gmail composition wrappers tying crawler provenance, matching, draft review and dispatch |
| `0016` | Repair Gmail readonly status lookup ambiguity and make send-to-readonly reconciliation binding immutable on replay |
| `0017` | Repair Gmail proposal listing ambiguity and reject changed reviewed-send reservation replays |
| `0018` | Make Gmail signal, review and send-reconciliation replays exact and tamper-evident |
| `0019` | Restore least-privilege schema usage for the Gmail readonly mailbox capability role |
| `0020` | Narrow Gmail mailbox direct reads to the four columns used by due-count status |

Revision `0008` does not grant Outbox direct table writes to `side_effect_attempts` or
`provider_receipts`. It revokes those direct grants and exposes only three narrow
`SECURITY DEFINER` functions to `careerops_outbox`: prepare a leased synthetic dispatch, record
a synthetic receipt, and record synthetic ambiguity. The functions validate the leased
`workflow_signal`, reservation binding, current authorization/grant/release evidence and latest
global, campaign and synthetic-provider kill-switch rows. The generic Outbox publisher remains
the owner of claim, retry, release and publish transitions.

The synthetic submission path is still a sandbox boundary. It does not provision real provider
credentials, browser automation, Gmail access or a real external application submission adapter.

Revision `0015` intentionally grants `careerops_workflow` only four compact
`SECURITY DEFINER` functions: record one GoalRun match from exact crawler provenance, prepare a
Gmail payload draft through the existing Gmail send boundary, dispatch only after an exact
GoalRun approval snapshot, and inspect outbox/receipt/reconciliation state. It does not grant
workflow direct table mutation or direct Gmail send functions.

Revision `0016` is a forward repair for already-migrated deployments. It qualifies the
`gmail_readonly_account_status` account lookup so PL/pgSQL output parameters cannot collide with
table columns, and rejects an idempotent Gmail send registration replay that attempts to change its
existing readonly reconciliation-account binding. It preserves the prior function signatures,
fixed search paths and narrow API-role grants.

Revision `0017` applies the same qualified-lookup repair to readonly proposal listing and makes a
reviewed Gmail send reservation idempotent only when every persisted semantic input is identical;
same-key replays with changed account, intent, application, recipient or reviewed evidence hashes
are rejected instead of silently returning the earlier reservation.

Revision `0018` closes the remaining Gmail replay gaps without widening capabilities. Mailbox
signal/proposal replays must match every persisted semantic field, readonly proposal reviews bind
the human reason, legacy exact-draft reviews bind the complete decision snapshot, and send
reconciliation stores a canonical request digest so exact retries are no-ops while changed provider
or metadata evidence is rejected. Operational lease, trace and creation-time fields remain outside
the semantic replay identity.

Revision `0019` is a forward privilege repair for existing databases. It grants only
`USAGE` on the `careerops` schema to `careerops_mailbox`; table access and function execution
remain limited to the explicit grants installed by revision `0012`. It does not grant schema
creation, direct mutation, or access to unrelated business tables.

Revision `0020` replaces the mailbox role's table-wide readonly grants with column grants for
`gmail_accounts(id, provider, status)` and `gmail_sync_runs(gmail_account_id, status)`. Those are
the only columns used by the worker's direct due-count query; all claim and state transitions stay
behind the existing fixed-search-path `SECURITY DEFINER` functions.

Revision `0010` keeps crawler definitions immutable and gives the API role only narrow scheduler
functions for claim, completion and failure. Those functions use PostgreSQL time, bounded leases,
exact owner/token fencing and append-only run events. A source must be enabled, due and marked
`robots_terms_policy=respect` before it can be claimed. Structured public-ATS ingestion must bind
to a successful source-run event and matching output-manifest digest; no registry field enables
login, MFA, CAPTCHA bypass, arbitrary scripts/URLs, browser automation or application submission.

Use `CAREEROPS_TEST_DATABASE_URL=... CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 make
verify-db` only with a disposable database. The test intentionally downgrades to base twice and
destroys all data in the `careerops` schema. The target database name must begin with
`careerops_test_`.

For a self-contained local verification, `CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make
verify-db-ephemeral` creates a uniquely named loopback-only Docker PostgreSQL container, runs
the same check, and removes that container even when the test fails. It never targets an existing
PostgreSQL cluster.

If Docker is unavailable but the local PostgreSQL server tools (`initdb`, `pg_ctl`, `createdb`,
and `psql`) are installed, `CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make
verify-db-native-ephemeral` creates a fresh temporary cluster under the operating system
temporary directory. It listens only on `127.0.0.1`, refuses a caller-supplied test URL, and
stops and removes only its own uniquely named data directory after verification.
