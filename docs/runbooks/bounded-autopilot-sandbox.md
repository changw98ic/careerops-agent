# Bounded-autopilot synthetic sandbox runbook

## Reader and purpose

This runbook is for the single CareerOps operator or release reviewer validating the bounded
autopilot path. It proves only the internal synthetic path:

```text
evidence
  -> review-required draft
  -> release-qualified .test fixture
  -> synthetic reservation
  -> generic outbox worker
  -> deterministic no-network synthetic provider
  -> side_effect_attempt
  -> provider_receipt or reconciliation
  -> audit
```

It does not log in to an ATS, use a browser driver, load a credential, send Gmail, execute a
real provider, submit a real application, or produce a real provider receipt.

## Preconditions

- Use the repository's Python environment (`make setup` if it has not been created).
- Do not configure real ATS credentials, browser cookies, or a public network endpoint.
- For the database path, use an empty disposable PostgreSQL database only. The migration test
  intentionally downgrades it to `base` before and after the test.

## Verify the local synthetic path

Run the focused in-memory evidence, adapter, release, and composed-workflow tests:

```bash
uv run pytest -q \
  tests/unit/test_application_prep.py \
  tests/unit/test_application_prep_repository.py \
  tests/unit/test_application_adapters.py \
  tests/unit/test_release_qualification.py \
  tests/unit/test_submission_dispatch.py \
  tests/unit/test_submission_dispatch_repository.py \
  tests/unit/test_bounded_autopilot_workflow.py
```

Success means the agent can prepare a deterministic internal draft, reserve one synthetic
workflow item, hand it to the generic outbox worker, run the deterministic no-network synthetic
provider, and record durable `side_effect_attempt` plus `provider_receipt` or reconciliation
only when its grant/payload/fixture/release facts align. It does not mean a job application was
sent.

To exercise the database trigger and retry convergence, use a disposable database whose name
starts with `careerops_test_`. The migration test downgrades that database to `base`, so it also
requires an explicit destructive-test acknowledgement:

```bash
CAREEROPS_TEST_DATABASE_URL="$DISPOSABLE_DATABASE_URL" \
  CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 \
  uv run pytest -q \
    tests/integration/test_migrations.py \
    tests/integration/test_submission_outbox_control_plane.py
```

If Docker is available, the repository can provision and destroy the isolated database itself:

```bash
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-db-ephemeral
```

This command refuses to use a caller-provided database URL and binds its temporary PostgreSQL
container only to `127.0.0.1`. The current G005 evidence is `40 passed in 11.09s` on a fresh
loopback disposable PostgreSQL container, with the container cleaned up afterward.

When Docker is unavailable but local PostgreSQL server tools are installed, use the native
ephemeral fallback instead:

```bash
CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 make verify-db-native-ephemeral
```

It refuses a caller-provided database URL, creates a fresh cluster in a uniquely named operating
system temporary directory, listens only on `127.0.0.1`, and removes that directory after the
test. It never connects to an existing PostgreSQL cluster.

Expected properties include one append-only cap reservation, one generic outbox worker item, one
durable synthetic `side_effect_attempt`, one durable synthetic `provider_receipt` or
reconciliation result, durable global/campaign/provider kill-switch events where exercised, and
one audit chain after a repeated synthetic request. If the database URL is absent, this test is
skipped; do not treat a skip as release evidence.

The current G005 evidence also includes `96 passed` for the focused synthetic unit path,
`15 passed` for the production-path integration files, and a passing security check. These are
synthetic release-qualification signals only; they do not authorize or demonstrate real
ATS/browser/Gmail/credential/provider execution or a real application.

## What the gate permits

- Only a host ending in `.test`.
- Only the `synthetic_sandbox` release stage.
- Only a matching fixture, per-intent session, immutable payload hash, exact autopilot policy
  outcome, current grant/release version, approved material hash, cap, and no active durable
  global, campaign or provider kill switch.

The submission hash is derived from the reviewed draft hash, exact action intent, `.test` target
and canonical form fields. It is never accepted as a caller-supplied label for different fields.

The following all stop before reservation or generic outbox worker execution: unknown or
prohibited site policy, shared/cross-intent browser context, credentials, real-provider
capability, unknown fields, hard-stop categories, payload drift, a generic approval, model
output, expired/revoked grants, a stale release, an active durable kill switch, or an ambiguous
provider state.

## Operator review boundary

Review the internal draft and its evidence/material hashes before any future conversion to a
submission intent. The currently shipped console presents grant and review projections only; it
does not expose a real-site submit control. A `shadow`, `review_required`, `limited_autopilot`,
or `expanded_autopilot` release stage cannot unlock real ATS/browser/Gmail/credential/provider
execution or a real application in this runtime.

If the intended next step is a human-owned real-world application, use the
[manual application handoff runbook](manual-application-handoff.md). That path prepares a
human-only packet and optional user attestation; it does not use provider writes, browser
automation, Gmail send, generic outbox worker execution, or synthetic-provider release evidence,
and does not count as autopilot release qualification.

If a test reports an ambiguity, release mismatch, or hard stop, leave the item in review/recovery.
Do not retry it against a real site or change the fixture to a real hostname.

## Escalation and next evidence

Before any real channel could be considered, collect the release evidence listed in
[bounded-autopilot acceptance](../acceptance/bounded-autopilot.md): real destination permission,
adapter safety/reconciliation proof, fault injection, provider receipt handling, reviewer metrics,
production role/credential isolation, kill-switch drills, and an explicit user authorization.
