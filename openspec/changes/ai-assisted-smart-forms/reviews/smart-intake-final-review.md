# Final independent adversarial review

Date: 2026-07-28
Reviewer: independent adversarial subagent (Darwin)
Scope: default-deny, manual-first Profile and AgentWorkbench MVP

## Verdict

`GO` for the model-disabled/manual-first MVP.

`NO-GO` for a provider-enabled pilot. That pilot still requires a separate
fixture, real-provider qualification, browser evidence for ready/invalid/stale
states, and another independent review. This is an intentional boundary and
does not block the current MVP because the repository and Compose defaults
keep the provider disabled.

## Findings

- Blocker: none.
- High: none.
- Medium: migrations `0028` and `0029` redact legacy decision values and add a
  bounded proposal digest irreversibly. Treat the migration chain as
  forward-only after release; do not promise a downgrade that reconstructs
  removed values.
- Low: none affecting the manual-first release.

## Evidence reviewed

- `make verify`: 2054 passed, 66 skipped, coverage 75.08%.
- `make verify-frontend`: 173 frontend tests passed, production bundle built,
  initial JS gzip gate passed.
- `make security`: Bandit and bounded secret scan passed.
- Disposable PostgreSQL migration check: the chain was upgraded from `0028`
  to `0029`; `alembic check`, Compose role grants, redaction check, and
  append-only trigger/function preservation all passed.
- Compose API readiness: database, Redis, Temporal, and storage ready;
  provider, Google OAuth, and external writes disabled. Retention worker ran.
- Ego: Profile and AgentWorkbench manual-first states passed at desktop and
  narrow mobile widths without personal-data screenshots.

The review also confirmed the repaired areas from the previous independent
review: candidate ownership, retention-role separation, decision metadata
redaction, idempotent apply replay before stale checks, closed API response
schemas, default-deny behavior, manual fallback, and separation from save/start
or external-write actions.
