UV_PROJECT_ENVIRONMENT := venv
export UV_PROJECT_ENVIRONMENT

.PHONY: audit bootstrap coverage format migrate migration-check run security setup verify \
	verify-compose verify-db verify-m0 verify-m1 verify-m1-contracts verify-m1-full \
	verify-temporal

audit:
	uv export --format requirements-txt --no-hashes --all-groups --no-emit-project | \
		uvx pip-audit --requirement /dev/stdin --no-deps --disable-pip --strict \
		--progress-spinner off

setup:
	uv sync

run:
	uv run careerops

bootstrap:
	uv run careerops-bootstrap

format:
	uv run ruff format .

migrate:
	uv run alembic upgrade head

migration-check:
	uv run alembic check

security:
	uv run bandit -q -r src
	python3 -S scripts/verify_no_secrets.py

coverage:
	uv run python -m pytest --cov=careerops --cov-report=term-missing

verify-db:
	@test -n "$(CAREEROPS_TEST_DATABASE_URL)" || \
		(echo "CAREEROPS_TEST_DATABASE_URL must point to a disposable PostgreSQL database"; exit 2)
	uv run python -m pytest tests/integration

verify-temporal:
	uv run python -m pytest tests/unit/test_temporal_smoke.py \
		tests/unit/test_temporal_worker.py tests/integration/test_temporal_recovery.py

verify-compose:
	docker compose config --quiet
	@set -eu; \
		trap 'docker compose down --volumes --remove-orphans' EXIT; \
		docker compose up --build --wait; \
		curl --fail --silent http://127.0.0.1:$${CAREEROPS_API_PORT:-8000}/api/v1/health/ready; \
		curl --fail --silent http://127.0.0.1:$${CAREEROPS_API_PORT:-8000}/login | \
			grep -q 'CareerOps</title>'

verify-m0: verify security verify-db verify-compose

verify-m1-contracts:
	python3 -S scripts/verify_m1.py --contracts-only

verify-m1-full:
	python3 -S scripts/verify_m1.py

# Compatibility alias: the historical target has always meant contract validation only.
verify-m1: verify-m1-contracts

verify: verify-m1-contracts
	uv lock --check
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run python -m pytest --cov=careerops --cov-report=term-missing
