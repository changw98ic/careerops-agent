# Retention and CI evidence contract

This annex makes operational evidence machine-checkable. It is a release
contract, not a claim that the current repository has already produced these
artifacts.

## Retention job

The `agent-console-retention` job runs hourly as a dedicated retention
principal. It acquires advisory lock
`hashtextextended('careerops.agent_console_retention', 0)`, processes at most
500 due rows per transaction with `FOR UPDATE SKIP LOCKED`, and commits each
batch. It never uses an API-role delete. Rows with
`legal_hold_until > now()` are counted as skipped. The purge function is
idempotent: rerunning after a committed batch cannot delete a newer row or
double-count an audit event.

Retry policy is exactly three attempts at 1s, 4s, and 16s plus full jitter. A
failed final attempt emits the following alert JSON and exits with status 2:

```json
{
  "event": "agent_console_retention_failed",
  "severity": "critical",
  "trace_id": "uuid",
  "migration_revision": "0030_agent_console_orchestration",
  "attempt": 3,
  "oldest_due_at": "2026-07-28T00:00:00Z",
  "failed_batches": 1,
  "skipped_legal_hold": 2,
  "safe_reason_code": "DB_UNAVAILABLE",
  "raw_values_included": false
}
```

Success emits bounded counters (`purged`, `skipped_legal_hold`, `batches`,
`duration_ms`, `trace_id`) and exits 0. A lock timeout is a retryable failure;
an authorization or legal-hold function error is non-retryable after the final
attempt. Alert and job logs contain no candidate content, provider text, raw
source, or secrets.

## Required CI evidence

Every scenario evidence-producing command writes a JSON record validated against
`scenario-evidence.schema.json`, returns non-zero on a missing assertion or
artifact, and writes only to one of two approved roots: UI/Ego evidence uses
`artifacts/ego/`; API, database, Temporal, model, browser-boundary, and
operations evidence uses `artifacts/agent-console/`. No other artifact root is
accepted. The artifact directories are uploaded with secret scanning and are
deleted from local workspaces after CI retention expires. The record includes
command, commit, environment-safe digests, scenario ID, and result; it does
not include credentials or raw external content.

| Gate | Command/job | Required negative evidence | Exit code |
| --- | --- | --- | ---: |
| `API-01` | contract API suite + `make verify` | auth/Origin/CSRF/idempotency/cursor/no-store/audit rollback | non-zero on any mismatch |
| `DB-01` | disposable PostgreSQL + `make verify-db` | 0029→0030, composite ownership, old/new rows, grants, purge lock/hold | non-zero on migration or privilege mismatch |
| `TEMP-01` | Temporal replay suite + `make verify-temporal` | fencing, lease expiry, cancel race, duplicate receipt, retry budget | non-zero on nondeterminism |
| `MODEL-01` | fake-provider contract suite | disabled default, exact allowlist, consent revoke/drift, schema/XSS/injection | non-zero if a forbidden field is sent |
| `CRAWL-01` | Compose sidecar suite + `make verify-compose` | sidecar absent/ready, SSRF/subresource/DNS/redirect/task-space | non-zero if boundary bypasses |
| `CRAWL-02` | browser boundary harness | exact sidecar wire JSON, mTLS/signature/nonce, IDN/CNAME/private-IP/connect binding | non-zero if a forbidden request or malformed receipt is accepted |
| `UX-01` | frontend bundle/tests + Ego route harness | no-data/blocked/failure/recovery, context preservation | non-zero on broken route/CTA |
| `A11Y-01` | keyboard/DOM/accessibility harness | labels, focus restore, live region, zoom, reduced motion | non-zero on required assertion |
| `OPS-01` | retention/runbook/secret scan | purge alert/retry, digest pins, no synthetic M1 data, safe logs | non-zero on leakage or missing runbook |

Release-blocking gate records use the separate
`release-gate-evidence.schema.json` and its finite release-gate enum:
`MODEL-02` captures the fake-provider exact JSON and consent/policy
revocation; `CRAWL-02` captures the sidecar/browser boundary, signature,
nonce, DNS, redirect, and subresource deny evidence; `AUTH-02` captures
cross-candidate/Origin/CSRF/idempotency races;
`DB-02` captures 0029→0030 compatibility and rollback-forward;
`AUDIT-02` captures same-transaction rollback; `TEMP-02` captures fencing,
cancel, dead-letter, and retry budget; and `PRIV-02` captures purge/legal hold,
no-store, retention, and raw-content absence. Their evidence records use
`artifacts/agent-console/<gate>-<commit>.json` and the release job fails if a
required gate record is absent, fails schema validation, has a gate/path
mismatch, is not `passed=true`, or has a score below 95. Primary scenario
artifacts remain `scenario-evidence.schema.json` records named by scenario ID;
release-gate artifacts are successful-only (`passed=true`, score at least 95)
and are never counted as substitutes for one of the 106 scenario records. A
failed gate is reported as a CI failure and cannot be uploaded as a valid
release-gate artifact.

The release job verifies that all 106 scenario IDs have exactly one primary
evidence record, that its `command` is one of the gate commands above, that
required screenshots exist for UI scenarios, and that the artifact's commit
matches the release candidate. It separately requires exactly one valid
release-gate record for each of `MODEL-02`, `CRAWL-02`, `AUTH-02`, `DB-02`,
`AUDIT-02`, `TEMP-02`, and `PRIV-02`, with the same candidate commit. It fails closed on
an unknown scenario ID, unknown gate, missing artifact, schema error, or score
below 95.

The validator applies the schema's `x-validation` rules as normative rules:
the JSON artifact basename must equal its `scenario_id` (for example,
`AO-R1-S1.json`), its root must equal the register's evidence root, and a PNG
is valid only as a separately referenced screenshot under `artifacts/ego/`.
For release records it also requires the artifact prefix to equal `gate` and
the filename suffix to equal the exact candidate `commit`; a valid JSON file
with a different gate or commit is rejected.
The release job invokes the checker with `--require-artifacts --commit
<release-commit>` so an otherwise valid design-only workspace cannot be
reported as a release pass while any of the 106 scenario records, seven
release-gate records, or required screenshots is absent.
