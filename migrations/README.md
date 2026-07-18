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

Use `CAREEROPS_TEST_DATABASE_URL=... make verify-db` only with a disposable database. The
test intentionally downgrades to base twice and destroys all data in the `careerops` schema.
