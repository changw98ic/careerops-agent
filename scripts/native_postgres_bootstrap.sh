#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
NATIVE_HOME=${CAREEROPS_NATIVE_HOME:-$ROOT/data/runtime/native}
ENV_FILE=${CAREEROPS_NATIVE_ENV_FILE:-$ROOT/.env}
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
VENV_BIN=${CAREEROPS_NATIVE_VENV_BIN:-$ROOT/venv/bin}
ADMIN_USER=careerops_local_admin
SOCKET_DIRECTORY="$NATIVE_HOME/postgres/socket"

if [ ! -f "$ENV_FILE" ]; then
    printf 'CareerOps native runtime requires %s\n' "$ENV_FILE" >&2
    exit 2
fi

set -a
. "$ENV_FILE"
set +a

for variable in \
    CAREEROPS_DB_OWNER_USER CAREEROPS_DB_OWNER_PASSWORD CAREEROPS_DB_NAME \
    CAREEROPS_DB_RUNTIME_USER CAREEROPS_DB_RUNTIME_PASSWORD \
    CAREEROPS_DB_WORKFLOW_USER CAREEROPS_DB_WORKFLOW_PASSWORD \
    CAREEROPS_DB_OUTBOX_USER CAREEROPS_DB_OUTBOX_PASSWORD \
    CAREEROPS_DB_MAILBOX_USER CAREEROPS_DB_MAILBOX_PASSWORD \
    CAREEROPS_DB_MAIL_SENDER_USER CAREEROPS_DB_MAIL_SENDER_PASSWORD \
    CAREEROPS_DB_GREENHOUSE_SENDER_USER CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD
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

PSQL="$POSTGRES_PREFIX/bin/psql"
PORT=${CAREEROPS_POSTGRES_PORT:-55432}

psql_admin() {
    "$PSQL" -X -v ON_ERROR_STOP=1 \
        -h "$SOCKET_DIRECTORY" -p "$PORT" -U "$ADMIN_USER" "$@"
}

psql_admin -d postgres <<'SQL'
\getenv owner_user CAREEROPS_DB_OWNER_USER
\getenv owner_password CAREEROPS_DB_OWNER_PASSWORD
\getenv database_name CAREEROPS_DB_NAME
SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
    :'owner_user', :'owner_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner_user')
\gexec
ALTER ROLE :"owner_user" WITH LOGIN PASSWORD :'owner_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT format('CREATE DATABASE %I OWNER %I', :'database_name', :'owner_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')
\gexec
ALTER DATABASE :"database_name" OWNER TO :"owner_user";
SQL

psql_admin -d "$CAREEROPS_DB_NAME" \
    -f "$ROOT/src/careerops/infrastructure/database/bootstrap_roles.sql"

psql_admin -d "$CAREEROPS_DB_NAME" <<'SQL'
\getenv runtime_user CAREEROPS_DB_RUNTIME_USER
\getenv runtime_password CAREEROPS_DB_RUNTIME_PASSWORD
\getenv workflow_user CAREEROPS_DB_WORKFLOW_USER
\getenv workflow_password CAREEROPS_DB_WORKFLOW_PASSWORD
\getenv outbox_user CAREEROPS_DB_OUTBOX_USER
\getenv outbox_password CAREEROPS_DB_OUTBOX_PASSWORD
\getenv mailbox_user CAREEROPS_DB_MAILBOX_USER
\getenv mailbox_password CAREEROPS_DB_MAILBOX_PASSWORD
\getenv mail_sender_user CAREEROPS_DB_MAIL_SENDER_USER
\getenv mail_sender_password CAREEROPS_DB_MAIL_SENDER_PASSWORD
\getenv greenhouse_sender_user CAREEROPS_DB_GREENHOUSE_SENDER_USER
\getenv greenhouse_sender_password CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'runtime_user', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'workflow_user', :'workflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'workflow_user')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'outbox_user', :'outbox_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'outbox_user')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'mailbox_user', :'mailbox_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'mailbox_user')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'mail_sender_user', :'mail_sender_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'mail_sender_user')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'greenhouse_sender_user', :'greenhouse_sender_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'greenhouse_sender_user')
\gexec

ALTER ROLE :"runtime_user" WITH LOGIN PASSWORD :'runtime_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"workflow_user" WITH LOGIN PASSWORD :'workflow_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"outbox_user" WITH LOGIN PASSWORD :'outbox_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"mailbox_user" WITH LOGIN PASSWORD :'mailbox_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"mail_sender_user" WITH LOGIN PASSWORD :'mail_sender_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"greenhouse_sender_user" WITH LOGIN PASSWORD :'greenhouse_sender_password'
    NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

SELECT format('REVOKE %I FROM %I', granted.rolname, expected.login_name)
FROM (
    VALUES
        (:'runtime_user', 'careerops_api'),
        (:'workflow_user', 'careerops_workflow'),
        (:'outbox_user', 'careerops_outbox'),
        (:'mailbox_user', 'careerops_mailbox'),
        (:'mail_sender_user', 'careerops_mail_sender'),
        (:'greenhouse_sender_user', 'careerops_greenhouse_sender')
) AS expected(login_name, capability_name)
JOIN pg_roles AS login ON login.rolname = expected.login_name
JOIN pg_auth_members AS membership ON membership.member = login.oid
JOIN pg_roles AS granted ON granted.oid = membership.roleid
WHERE granted.rolname <> expected.capability_name
\gexec

GRANT careerops_api TO :"runtime_user";
GRANT careerops_workflow TO :"workflow_user";
GRANT careerops_outbox TO :"outbox_user";
GRANT careerops_mailbox TO :"mailbox_user";
GRANT careerops_mail_sender TO :"mail_sender_user";
GRANT careerops_greenhouse_sender TO :"greenhouse_sender_user";
SQL

OWNER_DATABASE_URL=$(
    CAREEROPS_DB_URL_USER=$CAREEROPS_DB_OWNER_USER \
    CAREEROPS_DB_URL_PASSWORD=$CAREEROPS_DB_OWNER_PASSWORD \
    CAREEROPS_DB_URL_HOST=127.0.0.1 \
    CAREEROPS_DB_URL_PORT=$PORT \
    CAREEROPS_DB_URL_NAME=$CAREEROPS_DB_NAME \
        "$VENV_BIN/careerops-database-url"
)
env -i \
    HOME="$HOME" \
    PATH="$VENV_BIN:$HOMEBREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    CAREEROPS_DATABASE_URL="$OWNER_DATABASE_URL" \
    CAREEROPS_DATABASE_ROLE=api \
    "$VENV_BIN/alembic" upgrade head

printf 'CareerOps native PostgreSQL roles and migrations are current.\n'
