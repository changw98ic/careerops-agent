# Smart-intake operational runbook

The MVP is default-deny. Keep `CAREEROPS_SMART_INTAKE_ENABLED=false`,
`MODEL_PROVIDER=disabled`, `CAREEROPS_EXTERNAL_WRITES_ENABLED=false`,
`CAREEROPS_AUTO_SEND_ENABLED=false`, and `CAREEROPS_GOOGLE_OAUTH_ENABLED=false`
unless a separately approved pilot changes those settings.

## Rollback

1. Set `CAREEROPS_SMART_INTAKE_ENABLED=false` and restart the API. Startup
   reconciliation calls `SmartIntakeService.revoke_unapplied`, which locks each
   candidate-owned preview, clears its values, records a system audit event, and
   leaves bounded decision metadata untouched.
2. If the API is not available, run
   `UV_PROJECT_ENVIRONMENT=venv uv run python scripts/revoke_smart_intake.py --apply`
   against the intended database. The command is explicit and does not print
   database/provider error details.
3. Confirm the ordinary Profile and Agent routes remain usable; this rollback
   does not delete candidates, profile versions, resumes, jobs, evidence, or
   decisions.

## Retention

Compose runs `smart-intake-retention` from the same digest-pinned application
image. It invokes the retention function hourly and retries on the next
interval without exposing database/provider details. For a one-shot operator
run, use the following command using the retention database role:

```text
UV_PROJECT_ENVIRONMENT=venv uv run python scripts/purge_smart_intake.py
```

The script invokes only the `SECURITY DEFINER`
`careerops.purge_smart_intake_previews(timestamptz)` function. Values are
cleared after the 30-minute preview lifetime plus a 24-hour retention window;
the tombstone and append-only decision metadata remain. The retention role has
`SELECT`/function execution only and no direct `DELETE` privilege on preview
rows.

## Evidence boundary

- `make verify` and `make verify-frontend` prove repository/build behavior.
- The disposable PostgreSQL test proves candidate scoping, composite foreign
  keys, retention purge, direct-delete denial, and the append-only trigger.
- Ego evidence proves the disabled/manual-first desktop and narrow-mobile UI.
- Fake-provider tests are the only model-enabled engineering evidence. A real
  provider requires a separate current qualification review and is not enabled
  by this change.
