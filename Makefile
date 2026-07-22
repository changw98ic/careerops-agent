UV_PROJECT_ENVIRONMENT := venv
export UV_PROJECT_ENVIRONMENT

-include .env
export

CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR ?= $(abspath data/runtime/gmail)
CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET := $(CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR)/readonly.sock
CAREEROPS_LOCAL_GMAIL_SEND_SOCKET := $(CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR)/send.sock
CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET := $(CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR)/attachments.sock
CAREEROPS_LOCAL_GMAIL_OBJECT_ROOT ?= $(abspath data/objects)
CAREEROPS_GMAIL_READONLY_POLL_SECONDS ?= 5
CAREEROPS_GMAIL_READONLY_LIMIT ?= 10
CAREEROPS_GMAIL_READONLY_MAX_RESULTS ?= 100
CAREEROPS_GMAIL_SEND_POLL_SECONDS ?= 5
CAREEROPS_GMAIL_SEND_LIMIT ?= 10

.PHONY: audit bootstrap coverage crawl-sources crawler-execution-request crawler-outbox-publish d0-scaffold d0-status d0-validate format gmail-oauth-authorize-readonly gmail-oauth-authorize-send gmail-oauth-import-readonly gmail-oauth-import-send gmail-oauth-recover-smoke-send gmail-oauth-serve gmail-oauth-smoke-send gmail-oauth-status gmail-readonly-status gmail-readonly-sync gmail-readonly-worker gmail-send-status gmail-send-once gmail-send-reconcile gmail-send-worker migrate migration-check native-gmail-readonly-start native-gmail-readonly-stop native-install native-logs native-restart native-start native-status native-stop run security setup verify \
	verify-compose verify-db verify-db-ephemeral verify-db-native-ephemeral verify-m0 verify-m0-compose verify-m1 verify-m1-contracts verify-m1-full verify-native-runtime \
	verify-temporal

audit:
	uv export --format requirements-txt --no-hashes --all-groups --no-emit-project | \
		uvx pip-audit --requirement /dev/stdin --no-deps --disable-pip --strict \
		--progress-spinner off

setup:
	uv sync

run:
	uv run careerops

native-install:
	scripts/native_runtime.sh install

native-start:
	scripts/native_runtime.sh start

native-stop:
	scripts/native_runtime.sh stop

native-restart:
	scripts/native_runtime.sh restart

native-status:
	scripts/native_runtime.sh status

native-logs:
	scripts/native_runtime.sh logs

native-gmail-readonly-start:
	scripts/native_runtime.sh start-gmail-readonly

native-gmail-readonly-stop:
	scripts/native_runtime.sh stop-gmail-readonly

crawl-sources:
	@test -n "$(CRAWLER_CONFIG)" || \
		(echo "CRAWLER_CONFIG must point to a configured crawler manifest"; exit 2)
	@test -n "$(CRAWLER_REQUEST)" || \
		(echo "CRAWLER_REQUEST must point to an approved execution request"; exit 2)
	@test -n "$(CRAWLER_APPROVAL)" || \
		(echo "CRAWLER_APPROVAL must point to the matching execution approval"; exit 2)
	uv run careerops-crawl-sources --root . execute --config "$(CRAWLER_CONFIG)" \
		--request "$(CRAWLER_REQUEST)" --approval "$(CRAWLER_APPROVAL)"

crawler-execution-request:
	@test -n "$(CRAWLER_CONFIG)" || \
		(echo "CRAWLER_CONFIG must point to a manifest under datasets/manifests"; exit 2)
	@test -n "$(CRAWLER_OWNER_USER_ID)" || \
		(echo "CRAWLER_OWNER_USER_ID must be an existing console user UUID"; exit 2)
	@test -n "$(CRAWLER_REASON)" || \
		(echo "CRAWLER_REASON is required for review auditability"; exit 2)
	uv run careerops-crawler-execution --root . request --config "$(CRAWLER_CONFIG)" \
		--owner-user-id "$(CRAWLER_OWNER_USER_ID)" --reason "$(CRAWLER_REASON)"

crawler-outbox-publish:
	uv run careerops-crawler-outbox --root .

gmail-oauth-import-readonly:
	@test -n "$(GMAIL_READONLY_CLIENT_JSON)" || \
		(echo "GMAIL_READONLY_CLIENT_JSON must point to a downloaded Desktop OAuth client JSON"; exit 2)
	uv run careerops-gmail-broker --json import-client "$(GMAIL_READONLY_CLIENT_JSON)" --label readonly

gmail-oauth-import-send:
	@test -n "$(GMAIL_SEND_CLIENT_JSON)" || \
		(echo "GMAIL_SEND_CLIENT_JSON must point to a downloaded Desktop OAuth client JSON"; exit 2)
	uv run careerops-gmail-broker --json import-client "$(GMAIL_SEND_CLIENT_JSON)" --label send

gmail-oauth-authorize-readonly:
	@test -n "$(GMAIL_ACCOUNT_SUBJECT)" || \
		(echo "GMAIL_ACCOUNT_SUBJECT is required"; exit 2)
	uv run careerops-gmail-broker --json authorize readonly \
		--account-subject "$(GMAIL_ACCOUNT_SUBJECT)"

gmail-oauth-authorize-send:
	@test -n "$(GMAIL_ACCOUNT_SUBJECT)" || \
		(echo "GMAIL_ACCOUNT_SUBJECT is required"; exit 2)
	uv run careerops-gmail-broker --json authorize send \
		--account-subject "$(GMAIL_ACCOUNT_SUBJECT)"

gmail-oauth-status:
	uv run careerops-gmail-broker --json status

gmail-oauth-smoke-send:
	@test -n "$(GMAIL_ACCOUNT_SUBJECT)" || \
		(echo "GMAIL_ACCOUNT_SUBJECT is required"; exit 2)
	uv run careerops-gmail-broker --json smoke-send \
		--account-subject "$(GMAIL_ACCOUNT_SUBJECT)"

gmail-oauth-recover-smoke-send:
	@test -n "$(GMAIL_ACCOUNT_SUBJECT)" || \
		(echo "GMAIL_ACCOUNT_SUBJECT is required"; exit 2)
	@test -n "$(GMAIL_EXPECTED_SUBJECT_SHA256)" || \
		(echo "GMAIL_EXPECTED_SUBJECT_SHA256 is required"; exit 2)
	uv run careerops-gmail-broker --json recover-smoke-send \
		--account-subject "$(GMAIL_ACCOUNT_SUBJECT)" \
		--expected-subject-sha256 "$(GMAIL_EXPECTED_SUBJECT_SHA256)"

gmail-oauth-serve:
	umask 077; mkdir -p "$(CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR)" "$(CAREEROPS_LOCAL_GMAIL_OBJECT_ROOT)"
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		uv run careerops-gmail-broker --json serve \
		--readonly-socket "$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		--send-socket "$(CAREEROPS_LOCAL_GMAIL_SEND_SOCKET)" \
		--attachment-socket "$(CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET)" \
		--attachment-root "$(CAREEROPS_LOCAL_GMAIL_OBJECT_ROOT)"

gmail-readonly-status:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAILBOX_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAILBOX_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		uv run careerops-gmail-readonly status --json

gmail-readonly-sync:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAILBOX_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAILBOX_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		uv run careerops-gmail-readonly run-once --limit "$(CAREEROPS_GMAIL_READONLY_LIMIT)" \
		--max-results "$(CAREEROPS_GMAIL_READONLY_MAX_RESULTS)" --json

gmail-readonly-worker:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAILBOX_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAILBOX_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		uv run careerops-gmail-readonly worker --poll-seconds "$(CAREEROPS_GMAIL_READONLY_POLL_SECONDS)" \
		--limit "$(CAREEROPS_GMAIL_READONLY_LIMIT)" \
		--max-results "$(CAREEROPS_GMAIL_READONLY_MAX_RESULTS)"

gmail-send-status:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAIL_SENDER_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAIL_SENDER_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_ENABLED="$${CAREEROPS_GMAIL_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED="$${CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED:-false}" \
		CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		CAREEROPS_GMAIL_SEND_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_SEND_SOCKET)" \
		CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET)" \
		uv run careerops-gmail-send status --json

gmail-send-once:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAIL_SENDER_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAIL_SENDER_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_ENABLED="$${CAREEROPS_GMAIL_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED="$${CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED:-false}" \
		CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		CAREEROPS_GMAIL_SEND_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_SEND_SOCKET)" \
		CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET)" \
		uv run careerops-gmail-send run-once --limit "$(CAREEROPS_GMAIL_SEND_LIMIT)" --json

gmail-send-reconcile:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAIL_SENDER_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAIL_SENDER_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_ENABLED="$${CAREEROPS_GMAIL_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED="$${CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED:-false}" \
		CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		CAREEROPS_GMAIL_SEND_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_SEND_SOCKET)" \
		CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET)" \
		uv run careerops-gmail-send reconcile-once --limit "$(CAREEROPS_GMAIL_SEND_LIMIT)" --json

gmail-send-worker:
	@CAREEROPS_DATABASE_URL="$$(env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DB_URL_USER="$${CAREEROPS_DB_MAIL_SENDER_USER}" \
		CAREEROPS_DB_URL_PASSWORD="$${CAREEROPS_DB_MAIL_SENDER_PASSWORD}" \
		CAREEROPS_DB_URL_HOST="$${CAREEROPS_DB_HOST:-127.0.0.1}" \
		CAREEROPS_DB_URL_PORT="$${CAREEROPS_POSTGRES_PORT:-5432}" \
		CAREEROPS_DB_URL_NAME="$${CAREEROPS_DB_NAME}" \
		uv run careerops-database-url)" && \
	env -i PATH="$(PATH)" UV_PROJECT_ENVIRONMENT="$(UV_PROJECT_ENVIRONMENT)" \
		CAREEROPS_DATABASE_URL="$${CAREEROPS_DATABASE_URL}" \
		CAREEROPS_ENVIRONMENT="$${CAREEROPS_ENVIRONMENT:-development}" \
		CAREEROPS_CONSOLE_COOKIE_SECURE="$${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}" \
		CAREEROPS_GOOGLE_OAUTH_ENABLED="$${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}" \
		CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_EXTERNAL_WRITES_ENABLED="$${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}" \
		CAREEROPS_AUTO_SEND_ENABLED="$${CAREEROPS_AUTO_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_ENABLED="$${CAREEROPS_GMAIL_SEND_ENABLED:-false}" \
		CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED="$${CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED:-false}" \
		CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED="$${CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED:-false}" \
		CAREEROPS_MAILBOX_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_READONLY_SOCKET)" \
		CAREEROPS_GMAIL_SEND_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_SEND_SOCKET)" \
		CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET="$(CAREEROPS_LOCAL_GMAIL_ATTACHMENT_SOCKET)" \
		uv run careerops-gmail-send worker --poll-seconds "$(CAREEROPS_GMAIL_SEND_POLL_SECONDS)" \
		--limit "$(CAREEROPS_GMAIL_SEND_LIMIT)"

bootstrap:
	uv run careerops-bootstrap issue

d0-status:
	uv run careerops-d0 --root . status

d0-scaffold:
	@test -n "$(D0_DATASET)" || \
		(echo "D0_DATASET is required (for example: D0_DATASET=discovery_parser)"; exit 2)
	uv run careerops-d0 --root . scaffold --dataset "$(D0_DATASET)"

d0-validate:
	uv run careerops-d0 --root . validate

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
	@test "$(CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE)" = "1" || \
		(echo "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 is required because migration tests downgrade the target database to base"; exit 2)
	uv run python -m pytest tests/integration

verify-db-ephemeral:
	@test "$(CAREEROPS_ALLOW_EPHEMERAL_POSTGRES)" = "1" || \
		(echo "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 is required to create a temporary loopback PostgreSQL container"; exit 2)
	uv run python -S scripts/verify_disposable_postgres.py

verify-db-native-ephemeral:
	@test "$(CAREEROPS_ALLOW_EPHEMERAL_POSTGRES)" = "1" || \
		(echo "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 is required to create a temporary loopback PostgreSQL cluster"; exit 2)
	uv run python -S scripts/verify_native_ephemeral_postgres.py

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
			grep -q '<title>登录 · CareerOps</title>'

verify-native-runtime:
	scripts/native_runtime.sh status

verify-m0: verify security verify-db-native-ephemeral verify-native-runtime

verify-m0-compose: verify-m0 verify-compose

verify-m1-contracts:
	uv run python -S scripts/verify_m1.py --contracts-only

verify-m1-full:
	uv run python -S scripts/verify_m1.py

# Compatibility alias: the historical target has always meant contract validation only.
verify-m1: verify-m1-contracts

verify: verify-m1-contracts
	uv lock --check
	uv run ruff format --check .
	uv run ruff check .
	uv run pyright
	uv run python -m pytest --cov=careerops --cov-report=term-missing
