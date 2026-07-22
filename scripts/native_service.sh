#!/bin/sh
set -eu

SCRIPT_HOME=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
NATIVE_HOME=${CAREEROPS_NATIVE_HOME:-$SCRIPT_HOME}
ENV_FILE=${CAREEROPS_NATIVE_ENV_FILE:-$NATIVE_HOME/config/runtime.env}
VENV_BIN=${CAREEROPS_NATIVE_VENV_BIN:-$NATIVE_HOME/venv/bin}
WORKSPACE_ROOT=${CAREEROPS_NATIVE_WORKSPACE:-$NATIVE_HOME/workspace}
GMAIL_RUNTIME_DIRECTORY="$NATIVE_HOME/run/gmail"
OBJECT_ROOT="$NATIVE_HOME/objects"
if [ -n "${CAREEROPS_HOMEBREW_PREFIX:-}" ]; then
    HOMEBREW_PREFIX=$CAREEROPS_HOMEBREW_PREFIX
elif command -v brew >/dev/null 2>&1; then
    HOMEBREW_PREFIX=$(brew --prefix)
elif [ "$(uname -m)" = arm64 ]; then
    HOMEBREW_PREFIX=/opt/homebrew
else
    HOMEBREW_PREFIX=/usr/local
fi
POSTGRES_PREFIX=${CAREEROPS_POSTGRES_PREFIX:-$HOMEBREW_PREFIX/opt/postgresql@17}
REDIS_PREFIX=${CAREEROPS_REDIS_PREFIX:-$HOMEBREW_PREFIX/opt/valkey}
TEMPORAL_BIN=${CAREEROPS_TEMPORAL_BIN:-$HOMEBREW_PREFIX/bin/temporal}

if [ ! -f "$ENV_FILE" ]; then
    printf 'CareerOps native runtime requires %s\n' "$ENV_FILE" >&2
    exit 2
fi

set -a
# The project-owned .env is ignored by Git and must remain owner-only.
. "$ENV_FILE"
set +a

export PATH="$VENV_BIN:$HOME/.local/bin:$POSTGRES_PREFIX/bin:$REDIS_PREFIX/bin:$HOMEBREW_PREFIX/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG=C
export LC_ALL=C
export CAREEROPS_DEPLOYMENT_MODE=loopback
export CAREEROPS_BIND_HOST=127.0.0.1
export CAREEROPS_DB_HOST=127.0.0.1
export CAREEROPS_CRAWLER_WORKSPACE_ROOT="$WORKSPACE_ROOT"
export CAREEROPS_STORAGE_ROOT="$OBJECT_ROOT"
export CAREEROPS_BIND_PORT="${CAREEROPS_API_PORT:-${CAREEROPS_BIND_PORT:-8000}}"
export CAREEROPS_TEMPORAL_ADDRESS="127.0.0.1:${CAREEROPS_TEMPORAL_PORT:-7233}"
export CAREEROPS_CONSOLE_ALLOWED_HOSTS="[\"127.0.0.1:${CAREEROPS_API_PORT:-8000}\",\"localhost:${CAREEROPS_API_PORT:-8000}\"]"
export CAREEROPS_CONSOLE_ALLOWED_ORIGINS="[\"http://127.0.0.1:${CAREEROPS_API_PORT:-8000}\",\"http://localhost:${CAREEROPS_API_PORT:-8000}\"]"
export CAREEROPS_GOOGLE_OAUTH_ENABLED=false
export CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED=false
export CAREEROPS_EXTERNAL_WRITES_ENABLED=false
export CAREEROPS_AUTO_SEND_ENABLED=false
export CAREEROPS_AUTO_SUBMIT_ENABLED=false
export CAREEROPS_GMAIL_SEND_ENABLED=false
export CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED=false
export CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED=false
export CAREEROPS_GREENHOUSE_SUBMIT_ENABLED=false
export CAREEROPS_GREENHOUSE_SUBMIT_RELEASE_ATTESTED=false
export CAREEROPS_GREENHOUSE_SUBMIT_IN_PRODUCTION_ATTESTED=false

redis_url() {
    CAREEROPS_NATIVE_REDIS_PASSWORD=$CAREEROPS_REDIS_PASSWORD \
    CAREEROPS_NATIVE_REDIS_PORT=${CAREEROPS_REDIS_PORT:-56379} \
        "$VENV_BIN/python" -c '
import os
from urllib.parse import quote

password = quote(os.environ["CAREEROPS_NATIVE_REDIS_PASSWORD"], safe="")
port = int(os.environ["CAREEROPS_NATIVE_REDIS_PORT"])
print(f"redis://:{password}@127.0.0.1:{port}/0")
'
}

clear_runtime_secrets() {
    unset \
        CAREEROPS_DATABASE_URL CAREEROPS_REDIS_URL CAREEROPS_REDIS_PASSWORD \
        CAREEROPS_DB_OWNER_USER CAREEROPS_DB_OWNER_PASSWORD \
        CAREEROPS_DB_RUNTIME_USER CAREEROPS_DB_RUNTIME_PASSWORD \
        CAREEROPS_DB_WORKFLOW_USER CAREEROPS_DB_WORKFLOW_PASSWORD \
        CAREEROPS_DB_OUTBOX_USER CAREEROPS_DB_OUTBOX_PASSWORD \
        CAREEROPS_DB_MAILBOX_USER CAREEROPS_DB_MAILBOX_PASSWORD \
        CAREEROPS_DB_MAIL_SENDER_USER CAREEROPS_DB_MAIL_SENDER_PASSWORD \
        CAREEROPS_DB_GREENHOUSE_SENDER_USER CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD \
        CAREEROPS_TEMPORAL_DB_USER CAREEROPS_TEMPORAL_DB_PASSWORD
}

database_url() {
    CAREEROPS_DB_URL_USER=$1 \
    CAREEROPS_DB_URL_PASSWORD=$2 \
    CAREEROPS_DB_URL_HOST=127.0.0.1 \
    CAREEROPS_DB_URL_PORT=${CAREEROPS_POSTGRES_PORT:-55432} \
    CAREEROPS_DB_URL_NAME=$CAREEROPS_DB_NAME \
        "$VENV_BIN/careerops-database-url"
}

case "${1:-}" in
    postgres)
        exec /usr/bin/env -i HOME="$HOME" PATH="$PATH" LANG=C LC_ALL=C \
            "$POSTGRES_PREFIX/bin/postgres" \
            -D "$NATIVE_HOME/postgres/data" \
            -h 127.0.0.1 \
            -p "${CAREEROPS_POSTGRES_PORT:-55432}" \
            -k "$NATIVE_HOME/postgres/socket" \
            -c password_encryption=scram-sha-256 \
            -c timezone=UTC
        ;;
    redis)
        exec /usr/bin/env -i HOME="$HOME" PATH="$PATH" LANG=C LC_ALL=C \
            "$REDIS_PREFIX/bin/valkey-server" "$NATIVE_HOME/redis/redis.conf"
        ;;
    temporal)
        exec /usr/bin/env -i HOME="$HOME" PATH="$PATH" LANG=C LC_ALL=C \
            "$TEMPORAL_BIN" server start-dev \
            --disable-config-env \
            --disable-config-file \
            --ip 127.0.0.1 \
            --ui-ip 127.0.0.1 \
            --port "${CAREEROPS_TEMPORAL_PORT:-7233}" \
            --ui-port "${CAREEROPS_TEMPORAL_UI_PORT:-8233}" \
            --db-filename "$NATIVE_HOME/temporal/temporal.db" \
            --ui-disable-news-fetch
        ;;
    gmail-broker)
        umask 077
        exec /usr/bin/env -i HOME="$HOME" PATH="$PATH" LANG=C LC_ALL=C \
            PYTHONUNBUFFERED=1 \
            "$VENV_BIN/careerops-gmail-broker" --json serve \
            --readonly-socket "$GMAIL_RUNTIME_DIRECTORY/readonly.sock" \
            --send-socket "$GMAIL_RUNTIME_DIRECTORY/send.sock" \
            --attachment-socket "$GMAIL_RUNTIME_DIRECTORY/attachments.sock" \
            --attachment-root "$OBJECT_ROOT"
        ;;
    api)
        role_database_url=$(database_url \
            "$CAREEROPS_DB_RUNTIME_USER" "$CAREEROPS_DB_RUNTIME_PASSWORD")
        role_redis_url=$(redis_url)
        clear_runtime_secrets
        export CAREEROPS_DATABASE_URL="$role_database_url"
        export CAREEROPS_REDIS_URL="$role_redis_url"
        unset role_database_url role_redis_url
        export CAREEROPS_DATABASE_ROLE=api
        # Core readiness remains fail-closed even when the optional mailbox worker is enabled.
        exec "$VENV_BIN/careerops"
        ;;
    workflow-worker)
        role_database_url=$(database_url \
            "$CAREEROPS_DB_WORKFLOW_USER" "$CAREEROPS_DB_WORKFLOW_PASSWORD")
        clear_runtime_secrets
        export CAREEROPS_DATABASE_URL="$role_database_url"
        unset role_database_url
        export CAREEROPS_DATABASE_ROLE=workflow
        export CAREEROPS_TEMPORAL_WORKER_IDENTITY=careerops-native-worker
        exec "$VENV_BIN/careerops-worker"
        ;;
    crawler-outbox)
        role_database_url=$(database_url \
            "$CAREEROPS_DB_OUTBOX_USER" "$CAREEROPS_DB_OUTBOX_PASSWORD")
        clear_runtime_secrets
        export CAREEROPS_DATABASE_URL="$role_database_url"
        unset role_database_url
        export CAREEROPS_DATABASE_ROLE=outbox
        exec "$VENV_BIN/careerops-crawler-outbox" \
            --root "$WORKSPACE_ROOT" \
            --owner native-crawler-outbox \
            --poll-seconds "${CAREEROPS_CRAWLER_OUTBOX_POLL_SECONDS:-5}" \
            --max-attempts "${CAREEROPS_CRAWLER_OUTBOX_MAX_ATTEMPTS:-3}" \
            --limit "${CAREEROPS_CRAWLER_OUTBOX_LIMIT:-10}" \
            --lease-seconds "${CAREEROPS_CRAWLER_OUTBOX_LEASE_SECONDS:-30}"
        ;;
    gmail-readonly)
        role_database_url=$(database_url \
            "$CAREEROPS_DB_MAILBOX_USER" "$CAREEROPS_DB_MAILBOX_PASSWORD")
        clear_runtime_secrets
        export CAREEROPS_DATABASE_URL="$role_database_url"
        unset role_database_url
        export CAREEROPS_DATABASE_ROLE=mailbox
        export CAREEROPS_MAILBOX_BROKER_SOCKET="$GMAIL_RUNTIME_DIRECTORY/readonly.sock"
        export CAREEROPS_GOOGLE_OAUTH_ENABLED=true
        exec "$VENV_BIN/careerops-gmail-readonly" \
            --owner native-gmail-readonly \
            worker \
            --poll-seconds "${CAREEROPS_GMAIL_READONLY_POLL_SECONDS:-5}" \
            --limit "${CAREEROPS_GMAIL_READONLY_LIMIT:-10}" \
            --max-results "${CAREEROPS_GMAIL_READONLY_MAX_RESULTS:-100}"
        ;;
    *)
        printf 'unknown CareerOps native service: %s\n' "${1:-}" >&2
        exit 2
        ;;
esac
