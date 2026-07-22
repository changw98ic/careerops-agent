#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
ENV_FILE="$ROOT/.env"
NATIVE_HOME="$HOME/Library/Application Support/CareerOps"
NATIVE_ENV_FILE="$NATIVE_HOME/config/runtime.env"
NATIVE_VENV_BIN="$NATIVE_HOME/venv/bin"
NATIVE_WORKSPACE="$NATIVE_HOME/workspace"
LAUNCH_AGENT_DIRECTORY="$HOME/Library/LaunchAgents"
GUI_DOMAIN="gui/$(id -u)"
if command -v brew >/dev/null 2>&1; then
    HOMEBREW_PREFIX=$(brew --prefix)
elif [ "$(uname -m)" = arm64 ]; then
    HOMEBREW_PREFIX=/opt/homebrew
else
    HOMEBREW_PREFIX=/usr/local
fi
POSTGRES_PREFIX="$HOMEBREW_PREFIX/opt/postgresql@17"
POSTGRES_BIN="$POSTGRES_PREFIX/bin"
VALKEY_PREFIX="$HOMEBREW_PREFIX/opt/valkey"
TEMPORAL_BIN="$HOMEBREW_PREFIX/bin/temporal"
NATIVE_ADMIN_USER=careerops_local_admin
CORE_LABELS="io.careerops.postgresql17 io.careerops.redis io.careerops.temporal io.careerops.gmail-broker io.careerops.api io.careerops.workflow-worker io.careerops.crawler-outbox"

usage() {
    printf '%s\n' \
        'usage: scripts/native_runtime.sh install|start|stop|restart|status|logs|start-gmail-readonly|stop-gmail-readonly'
}

require_env() {
    if [ ! -f "$ENV_FILE" ]; then
        printf 'missing %s; copy .env.example and replace every placeholder first\n' "$ENV_FILE" >&2
        exit 2
    fi
    mode=$(stat -f '%Lp' "$ENV_FILE")
    if [ "$mode" != 600 ]; then
        chmod 600 "$ENV_FILE"
    fi
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    for variable in \
        CAREEROPS_DB_OWNER_USER CAREEROPS_DB_OWNER_PASSWORD CAREEROPS_DB_NAME \
        CAREEROPS_DB_RUNTIME_USER CAREEROPS_DB_RUNTIME_PASSWORD \
        CAREEROPS_DB_WORKFLOW_USER CAREEROPS_DB_WORKFLOW_PASSWORD \
        CAREEROPS_DB_OUTBOX_USER CAREEROPS_DB_OUTBOX_PASSWORD \
        CAREEROPS_DB_MAILBOX_USER CAREEROPS_DB_MAILBOX_PASSWORD \
        CAREEROPS_DB_MAIL_SENDER_USER CAREEROPS_DB_MAIL_SENDER_PASSWORD \
        CAREEROPS_DB_GREENHOUSE_SENDER_USER CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD \
        CAREEROPS_REDIS_PASSWORD
    do
        eval "value=\${$variable:-}"
        if [ -z "$value" ]; then
            printf '%s is required\n' "$variable" >&2
            exit 2
        fi
        case "$variable:$value" in
            *_PASSWORD:replace-with-*|*_PASSWORD:change-me|*_PASSWORD:changeme|*_PASSWORD:password)
                printf '%s still contains an example placeholder\n' "$variable" >&2
                exit 2
                ;;
        esac
    done
    : "${CAREEROPS_POSTGRES_PORT:=55432}"
    : "${CAREEROPS_REDIS_PORT:=56379}"
    : "${CAREEROPS_TEMPORAL_PORT:=7233}"
    : "${CAREEROPS_TEMPORAL_UI_PORT:=8233}"
    : "${CAREEROPS_API_PORT:=${CAREEROPS_BIND_PORT:-8000}}"
    export CAREEROPS_POSTGRES_PORT CAREEROPS_REDIS_PORT CAREEROPS_TEMPORAL_PORT
    export CAREEROPS_TEMPORAL_UI_PORT CAREEROPS_API_PORT CAREEROPS_REDIS_PASSWORD
}

require_homebrew() {
    command -v brew >/dev/null 2>&1 || {
        printf 'Homebrew is required\n' >&2
        exit 2
    }
    for formula in postgresql@17 valkey temporal; do
        brew list --formula --versions "$formula" >/dev/null 2>&1 || {
            printf 'missing Homebrew formula: %s; run brew bundle --file %s/Brewfile\n' \
                "$formula" "$ROOT" >&2
            exit 2
        }
    done
    ensure_postgresql_runtime_links
}

install_homebrew_dependencies() {
    command -v brew >/dev/null 2>&1 || {
        printf 'Homebrew is required\n' >&2
        exit 2
    }
    for formula in postgresql@17 temporal uv; do
        if ! brew list --formula --versions "$formula" >/dev/null 2>&1; then
            brew install "$formula"
        fi
    done
    if ! brew list --formula --versions valkey >/dev/null 2>&1; then
        brew install --formula valkey --skip-link
    fi
    # CareerOps addresses Valkey through its keg path. Keep it unlinked so it
    # never replaces an existing Redis CLI or service in the global prefix.
    brew unlink valkey >/dev/null 2>&1 || true
    brew bundle check --file "$ROOT/Brewfile"
}

ensure_postgresql_runtime_links() {
    # Repair only missing formula-owned runtime links after an incomplete
    # Homebrew post-install; never relink bin or replace another keg.
    for relative in share/postgresql@17 lib/postgresql@17; do
        target="$HOMEBREW_PREFIX/$relative"
        case "$relative" in
            share/*) source="$POSTGRES_PREFIX/share/postgresql" ;;
            lib/*) source="$POSTGRES_PREFIX/lib/postgresql" ;;
        esac
        if [ -e "$target" ] || [ -L "$target" ]; then
            continue
        fi
        if [ ! -d "$source" ]; then
            printf 'PostgreSQL formula asset is missing: %s\n' "$source" >&2
            exit 2
        fi
        ln -s "$source" "$target"
    done
}

port_is_free() {
    ! lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

install_runtime_payload() {
    require_env
    require_homebrew
    umask 077
    mkdir -p "$NATIVE_HOME/bin" "$NATIVE_HOME/config" "$NATIVE_HOME/logs" \
        "$NATIVE_HOME/run/gmail" "$NATIVE_HOME/objects" "$NATIVE_WORKSPACE/scripts"
    install -m 600 "$ENV_FILE" "$NATIVE_ENV_FILE"
    install -m 755 "$ROOT/scripts/native_service.sh" "$NATIVE_HOME/bin/native_service.sh"

    UV_PREFIX=$(brew --prefix uv)
    UV_BIN="$UV_PREFIX/bin/uv"
    if [ ! -x "$UV_BIN" ]; then
        printf 'Homebrew uv is required to install the native runtime environment\n' >&2
        exit 2
    fi
    if [ ! -x "$NATIVE_VENV_BIN/python" ]; then
        "$UV_BIN" venv --python 3.12 "$NATIVE_HOME/venv"
    fi
    "$UV_BIN" pip install \
        --python "$NATIVE_VENV_BIN/python" \
        --reinstall-package careerops \
        "$ROOT"

    /usr/bin/rsync -a --exclude '__pycache__' "$ROOT/scripts/" "$NATIVE_WORKSPACE/scripts/"
    if [ ! -d "$NATIVE_WORKSPACE/datasets" ]; then
        /bin/cp -cR "$ROOT/datasets" "$NATIVE_WORKSPACE/datasets"
    else
        for directory in manifests schemas labeling-guides; do
            mkdir -p "$NATIVE_WORKSPACE/datasets/$directory"
            /usr/bin/rsync -a "$ROOT/datasets/$directory/" \
                "$NATIVE_WORKSPACE/datasets/$directory/"
        done
    fi
}

label_is_loaded() {
    launchctl print "$GUI_DOMAIN/$1" >/dev/null 2>&1
}

label_pid() {
    launchctl print "$GUI_DOMAIN/$1" 2>/dev/null | awk '/^[[:space:]]*pid = / {print $3; exit}'
}

core_runtime_is_loaded() {
    for label in $CORE_LABELS; do
        if label_is_loaded "$label"; then
            return 0
        fi
    done
    return 1
}

assert_port_available_or_owned() {
    port=$1
    label=$2
    if port_is_free "$port"; then
        return
    fi
    expected_pid=$(label_pid "$label")
    listener_pid=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed -n '1p')
    if [ -z "$expected_pid" ] || [ "$listener_pid" != "$expected_pid" ]; then
        printf 'port %s is owned by PID %s, not %s\n' "$port" "${listener_pid:-unknown}" "$label" >&2
        exit 1
    fi
}

prepare_runtime() {
    require_env
    require_homebrew
    umask 077
    mkdir -p "$NATIVE_HOME/bin" "$NATIVE_HOME/config"
    install -m 600 "$ENV_FILE" "$NATIVE_ENV_FILE"
    install -m 755 "$ROOT/scripts/native_service.sh" "$NATIVE_HOME/bin/native_service.sh"
    if [ ! -x "$NATIVE_VENV_BIN/careerops" ]; then
        printf 'native runtime is not installed; run scripts/native_runtime.sh install\n' >&2
        exit 2
    fi
    mkdir -p \
        "$NATIVE_HOME/postgres/data" \
        "$NATIVE_HOME/postgres/socket" \
        "$NATIVE_HOME/redis/data" \
        "$NATIVE_HOME/temporal" \
        "$NATIVE_HOME/logs" \
        "$NATIVE_HOME/run/gmail" \
        "$NATIVE_HOME/objects" \
        "$LAUNCH_AGENT_DIRECTORY"
    chmod 700 "$NATIVE_HOME" "$NATIVE_HOME/postgres" \
        "$NATIVE_HOME/postgres/data" "$NATIVE_HOME/postgres/socket" \
        "$NATIVE_HOME/redis" "$NATIVE_HOME/redis/data" \
        "$NATIVE_HOME/temporal" "$NATIVE_HOME/logs" \
        "$NATIVE_HOME/run/gmail" "$NATIVE_HOME/objects"

    if [ ! -f "$NATIVE_HOME/postgres/data/PG_VERSION" ]; then
        if find "$NATIVE_HOME/postgres/data" -mindepth 1 -maxdepth 1 | grep -q .; then
            printf 'refusing non-empty uninitialized PostgreSQL data directory\n' >&2
            exit 1
        fi
        "$POSTGRES_BIN/initdb" \
            -D "$NATIVE_HOME/postgres/data" \
            -L "$POSTGRES_PREFIX/share/postgresql" \
            --username="$NATIVE_ADMIN_USER" \
            --auth-local=trust \
            --auth-host=scram-sha-256 \
            --encoding=UTF8 \
            --no-locale
    fi
    if [ "$(sed -n '1p' "$NATIVE_HOME/postgres/data/PG_VERSION")" != 17 ]; then
        printf 'CareerOps native PostgreSQL data directory is not version 17\n' >&2
        exit 1
    fi

    CAREEROPS_REDIS_PASSWORD=$CAREEROPS_REDIS_PASSWORD \
        /usr/bin/python3 "$ROOT/scripts/render_native_runtime.py" redis \
        --runtime-directory "$NATIVE_HOME" \
        --port "$CAREEROPS_REDIS_PORT" >/dev/null
    /usr/bin/python3 "$ROOT/scripts/render_native_runtime.py" launchd \
        --runtime-home "$NATIVE_HOME" \
        --home "$HOME" \
        --output-directory "$LAUNCH_AGENT_DIRECTORY" >/dev/null
}

start_label() {
    label=$1
    plist="$LAUNCH_AGENT_DIRECTORY/$label.plist"
    launchctl enable "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
    if label_is_loaded "$label"; then
        return
    fi
    launchctl bootstrap "$GUI_DOMAIN" "$plist"
    if [ "$label" = io.careerops.gmail-readonly ]; then
        launchctl kickstart "$GUI_DOMAIN/$label"
    fi
}

stop_label() {
    label=$1
    if label_is_loaded "$label"; then
        launchctl bootout "$GUI_DOMAIN/$label"
    fi
    launchctl disable "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
}

disable_unloaded_labels() {
    for label in $CORE_LABELS io.careerops.gmail-readonly; do
        if ! label_is_loaded "$label"; then
            launchctl disable "$GUI_DOMAIN/$label" >/dev/null 2>&1 || true
        fi
    done
}

wait_postgres() {
    attempts=0
    until "$POSTGRES_BIN/pg_isready" -h 127.0.0.1 -p "$CAREEROPS_POSTGRES_PORT" \
        -U "$NATIVE_ADMIN_USER" >/dev/null 2>&1
    do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 60 ]; then
            printf 'PostgreSQL did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

wait_redis() {
    attempts=0
    until REDISCLI_AUTH=$CAREEROPS_REDIS_PASSWORD \
        "$VALKEY_PREFIX/bin/valkey-cli" -h 127.0.0.1 -p "$CAREEROPS_REDIS_PORT" \
        ping 2>/dev/null | grep -q '^PONG$'
    do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 60 ]; then
            printf 'Redis did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

wait_temporal() {
    attempts=0
    until "$TEMPORAL_BIN" operator cluster health \
        --address "127.0.0.1:$CAREEROPS_TEMPORAL_PORT" >/dev/null 2>&1
    do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 90 ]; then
            printf 'Temporal did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

wait_api() {
    attempts=0
    until curl --fail --silent --show-error \
        "http://127.0.0.1:$CAREEROPS_API_PORT/api/v1/health/ready" >/dev/null 2>&1
    do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 180 ]; then
            printf 'CareerOps API did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

probe_gmail_broker_sockets() {
    /usr/bin/python3 - "$NATIVE_HOME/run/gmail" <<'PY'
import json
import socket
import sys
from pathlib import Path

root = Path(sys.argv[1])
for name in ("readonly.sock", "send.sock", "attachments.sock"):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.0)
    try:
        client.connect(str(root / name))
        client.sendall(b"{}")
        client.shutdown(socket.SHUT_WR)
        response = client.recv(65536)
        payload = json.loads(response)
        if payload.get("error") != "broker request rejected":
            raise RuntimeError(f"unexpected broker response on {name}")
    finally:
        client.close()
PY
}

wait_gmail_broker() {
    attempts=0
    until probe_gmail_broker_sockets >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
            printf 'Gmail broker sockets did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

wait_label_pid() {
    label=$1
    attempts=0
    until [ -n "$(label_pid "$label")" ]; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 30 ]; then
            printf '%s did not remain running\n' "$label" >&2
            exit 1
        fi
        sleep 1
    done
}

workflow_worker_health() {
    CAREEROPS_TEMPORAL_ADDRESS="127.0.0.1:$CAREEROPS_TEMPORAL_PORT" \
    CAREEROPS_TEMPORAL_WORKER_IDENTITY=careerops-native-worker \
        "$NATIVE_VENV_BIN/careerops-worker-health"
}

wait_workflow_worker() {
    attempts=0
    until workflow_worker_health >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 60 ]; then
            printf 'Temporal workflow worker did not become ready\n' >&2
            exit 1
        fi
        sleep 1
    done
}

start_runtime() {
    prepare_runtime
    assert_port_available_or_owned "$CAREEROPS_POSTGRES_PORT" io.careerops.postgresql17
    assert_port_available_or_owned "$CAREEROPS_REDIS_PORT" io.careerops.redis
    assert_port_available_or_owned "$CAREEROPS_TEMPORAL_PORT" io.careerops.temporal
    assert_port_available_or_owned "$CAREEROPS_TEMPORAL_UI_PORT" io.careerops.temporal
    assert_port_available_or_owned "$CAREEROPS_API_PORT" io.careerops.api

    start_label io.careerops.postgresql17
    start_label io.careerops.redis
    start_label io.careerops.temporal
    wait_postgres
    wait_redis
    wait_temporal
    CAREEROPS_NATIVE_HOME="$NATIVE_HOME" \
    CAREEROPS_NATIVE_ENV_FILE="$NATIVE_ENV_FILE" \
    CAREEROPS_NATIVE_VENV_BIN="$NATIVE_VENV_BIN" \
    CAREEROPS_HOMEBREW_PREFIX="$HOMEBREW_PREFIX" \
    CAREEROPS_POSTGRES_PREFIX="$POSTGRES_PREFIX" \
    "$ROOT/scripts/native_postgres_bootstrap.sh"
    start_label io.careerops.gmail-broker
    wait_gmail_broker
    start_label io.careerops.workflow-worker
    start_label io.careerops.crawler-outbox
    wait_label_pid io.careerops.crawler-outbox
    start_label io.careerops.api
    wait_api
    wait_workflow_worker
}

stop_runtime() {
    for label in \
        io.careerops.gmail-readonly \
        io.careerops.api \
        io.careerops.crawler-outbox \
        io.careerops.workflow-worker \
        io.careerops.gmail-broker \
        io.careerops.temporal \
        io.careerops.redis \
        io.careerops.postgresql17
    do
        stop_label "$label"
    done
}

start_or_reconcile_runtime() {
    require_env
    gmail_readonly_was_running=false
    if label_is_loaded io.careerops.gmail-readonly; then
        gmail_readonly_was_running=true
    fi
    if core_runtime_is_loaded; then
        stop_runtime
    fi
    start_runtime
    if [ "$gmail_readonly_was_running" = true ]; then
        start_label io.careerops.gmail-readonly
        wait_label_pid io.careerops.gmail-readonly
    fi
}

status_runtime() {
    require_env
    require_homebrew
    failed=0
    for label in $CORE_LABELS; do
        if label_is_loaded "$label"; then
            pid=$(label_pid "$label")
            printf '%-38s loaded pid=%s\n' "$label" "${pid:-none}"
            if [ -z "$pid" ]; then
                failed=1
            fi
        else
            printf '%-38s stopped\n' "$label"
            failed=1
        fi
    done
    if label_is_loaded io.careerops.gmail-readonly; then
        pid=$(label_pid io.careerops.gmail-readonly)
        printf '%-38s loaded pid=%s\n' io.careerops.gmail-readonly \
            "${pid:-none}"
        if [ -z "$pid" ]; then
            failed=1
        fi
    else
        printf '%-38s optional-stopped\n' io.careerops.gmail-readonly
    fi
    if ! curl --fail --silent \
        "http://127.0.0.1:$CAREEROPS_API_PORT/api/v1/health/ready" >/dev/null 2>&1; then
        failed=1
    fi
    if ! workflow_worker_health >/dev/null 2>&1; then
        failed=1
    fi
    if probe_gmail_broker_sockets >/dev/null 2>&1; then
        printf '%-38s healthy\n' gmail-broker-sockets
    else
        printf '%-38s not-ready\n' gmail-broker-sockets
        failed=1
    fi
    return "$failed"
}

case "${1:-}" in
    install)
        runtime_was_running=false
        gmail_readonly_was_running=false
        for label in $CORE_LABELS; do
            if label_is_loaded "$label"; then
                runtime_was_running=true
                break
            fi
        done
        if label_is_loaded io.careerops.gmail-readonly; then
            gmail_readonly_was_running=true
        fi
        install_homebrew_dependencies
        if [ "$runtime_was_running" = true ]; then
            require_env
            stop_runtime
        fi
        install_runtime_payload
        prepare_runtime
        if [ "$runtime_was_running" = true ]; then
            start_runtime
            if [ "$gmail_readonly_was_running" = true ]; then
                start_label io.careerops.gmail-readonly
                wait_label_pid io.careerops.gmail-readonly
            fi
        else
            disable_unloaded_labels
        fi
        ;;
    start)
        start_or_reconcile_runtime
        ;;
    stop)
        stop_runtime
        ;;
    restart)
        start_or_reconcile_runtime
        ;;
    status)
        status_runtime
        ;;
    logs)
        tail -n 80 "$NATIVE_HOME"/logs/*.log
        ;;
    start-gmail-readonly)
        if ! status_runtime >/dev/null; then
            printf 'core native runtime must be healthy before Gmail read-only starts\n' >&2
            exit 1
        fi
        start_label io.careerops.gmail-broker
        wait_gmail_broker
        start_label io.careerops.gmail-readonly
        wait_label_pid io.careerops.gmail-readonly
        ;;
    stop-gmail-readonly)
        stop_label io.careerops.gmail-readonly
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
