# Independent review 2 — findings and fixes

Date: 2026-07-28
Reviewer: independent read-only subagent (Kuhn)
Initial verdict: `NO-GO`

## Findings

- Compose required retention credentials and started a retention service, but
  the CI Compose job did not provide the credentials or include the service in
  its running-service contract.
- The pre-0028 decision row stored raw decisions and `draft_patch`, while
  retention only scrubbed preview values.
- Apply checked the current context before looking up an existing apply key,
  so a retry could become stale instead of replaying the same draft.
- The first 0027 implementation assumed `careerops_retention` already existed;
  a migration-only test container creates capability roles after Alembic.
- Response DTOs exposed open `Any`/dictionary shapes, URL rejection did not
  cover non-HTTP URI schemes and bare domains, and a profile patch could inherit
  a non-`preferred` location kind.
- Dedicated smart-intake outcome/latency/decision metrics were not wired.
- The checked task list overstated provider-enabled/browser and broad database
  coverage.

## Resolution

- CI now supplies retention user/password variables and asserts
  `smart-intake-retention` is running. Compose and `.env.example` explicitly
  keep `CAREEROPS_SMART_INTAKE_ENABLED=false`.
- `0027` now tolerates role creation after migration and its downgrade restores
  the 0026 append-only trigger. `0028` scrubs legacy raw decision values and
  reinstalls the trigger. New decision rows contain only path, action, reason,
  submitted value digest, and immutable proposal value digest; draft patches
  are rebuilt on equivalent retries while preview values are retained.
- Apply resolves an existing candidate-owned idempotency row before stale
  checks, then recomputes the non-persisted patch from the submitted decisions.
- API response models now close source references, scalar values, draft patches,
  target/state, and digest formats. URI schemes, `www.` URLs, and bare domains
  are rejected before context resolution.
- Profile merge rejects smart location patches for non-preferred locations and
  initializes new smart locations as `preferred`.
- Prometheus now exposes bounded smart-intake outcome, decision, latency, and
  aggregate token metrics; metric failures are advisory and fail closed.
- Tasks were rewritten to match the evidence actually claimed. Real-provider
  ready/invalid/stale browser qualification remains a separate pilot boundary.

## Re-verification required

The repaired tree subsequently passed the full Python/frontend/security gates,
migration 0001→head including a container where retention roles are created
after Alembic, and Compose health/retention checks. The final independent
adversarial verdict is recorded in `smart-intake-final-review.md`.
