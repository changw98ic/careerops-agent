# AGENTS.md

This file provides guidance to the AI agent when working with code in this repository.

## Build & Verify

- Package manager: `uv` (not pip). Virtualenv lives in `venv/` (not `.venv/`) via `UV_PROJECT_ENVIRONMENT=venv`.
- Python 3.12. Source layout: `src/careerops/`.
- Primary gate: `make verify` (contracts, lock check, ruff format --check, ruff check, pyright, pytest with branch coverage >= 75%).
- Integration tests require a disposable PostgreSQL: `CAREEROPS_TEST_DATABASE_URL=... make verify-db`.
- `make verify-m1-full` is **expected to fail** until real pilot data exists. Do not "fix" it or generate synthetic substitutes.

## Code Style

- Ruff: line-length 100, rules `E,F,I,B,UP,SIM,RUF`, target py312.
- Pyright: strict mode on `src/`, basic on `tests/`.
- Formatter: `uv run ruff format .` (not black/isort).

## Testing

- Test dirs: `tests/unit`, `tests/integration`, `tests/contract`.
- Mark integration tests with `@pytest.mark.integration`.
- Coverage source is `careerops` (the installed package), fail_under 75%.

## Safety Invariants (do not weaken)

- Default-deny: unknown policy actions deny, model provider is `disabled`, external writes rejected.
- Never enable `CAREEROPS_EXTERNAL_WRITES_ENABLED`, `CAREEROPS_AUTO_SEND_ENABLED`, or `CAREEROPS_GOOGLE_OAUTH_ENABLED`.
- Audit log is append-only via a `SECURITY DEFINER` function; the API role has no direct INSERT.
- Redis/limiter failure must deny (fail closed), never fail open.
- All container images must remain digest-pinned.

## Repo Etiquette

- Never commit `.env`, `data/`, `secrets/`, `datasets/private/`, `datasets/raw/`, `datasets/holdout/`.
- CI runs `make verify` + bandit + secret scan on every push/PR.
- Migrations: Alembic in `migrations/`. Bootstrap roles via `bootstrap_roles.sql` before migrating.
