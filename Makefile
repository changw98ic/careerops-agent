UV_PROJECT_ENVIRONMENT := venv
export UV_PROJECT_ENVIRONMENT

.PHONY: audit bootstrap coverage format migrate migration-check run security setup verify \
	verify-compose verify-db verify-frontend verify-m0 verify-m1 verify-m1-contracts verify-m1-full \
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
	uv run python -S scripts/verify_m1.py --contracts-only

verify-m1-full:
	uv run python -S scripts/verify_m1.py

# Compatibility alias: the historical target has always meant contract validation only.
verify-m1: verify-m1-contracts

verify-frontend:
	cd frontend && npm ci && npm test -- --run && npm run build
	@INITIAL_JS=$$(sed -n 's/.*<script type="module"[^>]*src="\([^"]*\.js\)".*/\1/p' frontend/dist/index.html | head -1); \
	INITIAL_JS="frontend/dist/$${INITIAL_JS#/}"; \
	if [ -z "$$INITIAL_JS" ] || [ ! -f "$$INITIAL_JS" ]; then echo "::error::No JS bundle found at $$INITIAL_JS"; exit 1; fi; \
	SIZE=$$(gzip -c "$$INITIAL_JS" | wc -c); \
	LIMIT=$$((250 * 1024)); \
	echo "Initial JS gzip size: $$SIZE bytes (limit: $$LIMIT)"; \
	if [ "$$SIZE" -gt "$$LIMIT" ]; then echo "::error::Bundle too large"; exit 1; fi

verify: verify-m1-contracts
	uv lock --check
	uv run --no-sync ruff format --check .
	uv run --no-sync ruff check .
	uv run --no-sync pyright
	uv run --no-sync python -m pytest --cov=careerops --cov-report=term-missing
