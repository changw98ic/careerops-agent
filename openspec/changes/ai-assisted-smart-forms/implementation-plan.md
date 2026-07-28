# Smart-intake implementation and rollout plan

## Outcome

Ship a manual-first MVP for two surfaces only:

1. Profile: text proposals for low-risk roles, preferred locations, include
   keywords, and exclude keywords.
2. AgentWorkbench: text proposal for the local interview `user_context`.

The existing Profile save and Agent start actions remain the only durable
authorities. The model, external writes, auto-send, and Google OAuth stay
disabled by default.

## Work sequence

### 1. Contract and safety freeze

- Keep `CAREEROPS_SMART_INTAKE_ENABLED=false` and `MODEL_PROVIDER=disabled`.
- Enforce the closed target matrix and bounded text/source-span contract in
  `src/careerops/application/smart_intake.py` and
  `src/careerops/api/routes/smart_intake.py`.
- Verify candidate ownership, CSRF/origin, Redis fail-closed rate limiting,
  no-tools untrusted input, and no-store responses before model assembly.

### 2. Profile vertical slice

- Create candidate-owned preview/decision storage with migrations `0026` through
  `0029`; `0028` removes any legacy raw decision values and `0029` adds the
  bounded proposal digest before restoring the append-only trigger.
- Compute input/context/request/decision digests server-side and claim an
  idempotency key for 30 seconds before provider invocation.
- Persist decision metadata only: path, decision, bounded reason, submitted
  value digest, and immutable proposal value digest. Rebuild the draft patch
  from the immutable preview and the equivalent apply request so idempotent
  retries do not require raw values in the decision table; after expiry/purge,
  the tombstone takes precedence and returns no old draft values.
- Keep apply as a non-persisted patch. Merge only clean local fields; let the
  existing Profile save API create the immutable version.
- Verify with unit/contract tests, the disposable PostgreSQL test, and the
  disabled/manual browser path.

### 3. AgentWorkbench extension

- Reuse the shared panel and API contract after Profile passes.
- Require the existing canonical job, bound job version, confirmed resume
  version, and optional confirmed evidence references.
- Apply only to local `user_context`; keep “Start interview preparation” as a
  separate explicit action.

### 4. Retention and rollback

- Run `smart-intake-retention` with the dedicated retention role from Compose.
- Purge values after the 30-minute review window plus the 24-hour retention
  window, retaining only bounded tombstone/decision metadata.
- On rollback, disable the capability and run startup reconciliation or
  `scripts/revoke_smart_intake.py --apply`; unapplied values are revoked and
  manual Profile/Agent workflows remain available.

### 5. Release gates

Run the following in order:

```text
openspec validate ai-assisted-smart-forms --type change --strict --no-interactive
make verify
make verify-frontend
make security
```

Then run the disposable PostgreSQL migration/integration checks, Compose health
and retention checks, API negative-path checks, and the isolated Ego browser
acceptance documented in `reviews/smart-intake-browser.md`.

## Rollout matrix

| State | Expected behavior | Evidence |
| --- | --- | --- |
| Capability off | `403 SMART_INTAKE_DISABLED`; manual fields remain usable | API/unit/Ego |
| Capability on, provider off | `200 state=unavailable`; zero provider calls | unit/contract |
| Provider failure | `200 state=unavailable`; no trusted fields | fake-provider tests |
| Invalid/abstained output | `200 state=invalid`/`abstained`; no partial fields | fake-provider tests |
| Ready provider result | explicit review/apply only | separate pilot qualification |
| Stale/expired/revoked | `409`/`410`; no mutation or value disclosure | unit/DB tests |

## Explicit non-goals for this release

Do not enable resume-file parsing, arbitrary job URL intake, crawl/Ego actions,
application/package/send/OAuth actions, or real-provider quality claims. Those
require a follow-up spec with authoritative evidence projection, destination
contracts, pilot fixtures, and a new independent review.
