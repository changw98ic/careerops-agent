from collections.abc import Callable

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from careerops.config import DatabaseCapabilityRole, Settings

_ROLE_INITIALIZATION_SQL = {
    DatabaseCapabilityRole.API: "SET ROLE careerops_api",
    DatabaseCapabilityRole.WORKFLOW: "SET ROLE careerops_workflow",
    DatabaseCapabilityRole.MAILBOX: "SET ROLE careerops_mailbox",
    DatabaseCapabilityRole.RETENTION: "SET ROLE careerops_retention",
    DatabaseCapabilityRole.OUTBOX: "SET ROLE careerops_outbox",
    DatabaseCapabilityRole.MAIL_SENDER: "SET ROLE careerops_mail_sender",
    DatabaseCapabilityRole.GREENHOUSE_SENDER: "SET ROLE careerops_greenhouse_sender",
    DatabaseCapabilityRole.READONLY: "SET ROLE careerops_readonly",
}
_FIXED_SEARCH_PATH_SQL = "SET search_path TO pg_catalog, careerops"
_RUNTIME_IDENTITY_CHECK_SQL = """
SELECT
    current_user::text,
    session_user::text,
    login.rolsuper,
    login.rolcreatedb,
    login.rolcreaterole,
    login.rolreplication,
    login.rolbypassrls,
    login.rolinherit,
    EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'careerops'
          AND namespace.nspowner = login.oid
    ),
    ARRAY(
        SELECT granted.rolname::text
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
        WHERE membership.member = login.oid
        ORDER BY granted.rolname
    )
FROM pg_catalog.pg_roles AS login
WHERE login.rolname = session_user
"""


class UnsafeDatabaseIdentity(RuntimeError):
    """The runtime login is elevated, owns the schema, or has ambiguous memberships."""


def _runtime_session_initializer(
    database_role: DatabaseCapabilityRole,
) -> Callable[[DBAPIConnection, ConnectionPoolEntry, object | None], None]:
    role_sql = _ROLE_INITIALIZATION_SQL[database_role]
    expected_role = f"careerops_{database_role.value}"

    def initialize(
        dbapi_connection: DBAPIConnection,
        _connection_record: ConnectionPoolEntry,
        _connection_proxy: object | None = None,
    ) -> None:
        previous_autocommit = bool(dbapi_connection.autocommit)
        if not previous_autocommit:
            dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("RESET ROLE")
                cursor.execute(role_sql)
                cursor.execute(_FIXED_SEARCH_PATH_SQL)
                cursor.execute(_RUNTIME_IDENTITY_CHECK_SQL)
                row = cursor.fetchone()
                if row is None:
                    raise UnsafeDatabaseIdentity("runtime session login does not exist")
                (
                    current_role,
                    session_login,
                    is_superuser,
                    can_create_database,
                    can_create_role,
                    can_replicate,
                    can_bypass_rls,
                    inherits_memberships,
                    owns_schema,
                    memberships,
                ) = row
                if (
                    current_role != expected_role
                    or session_login == current_role
                    or is_superuser
                    or can_create_database
                    or can_create_role
                    or can_replicate
                    or can_bypass_rls
                    or inherits_memberships
                    or owns_schema
                    or tuple(memberships) != (expected_role,)
                ):
                    raise UnsafeDatabaseIdentity(
                        "runtime login must be NOINHERIT, non-owner, non-elevated, "
                        "and a member of exactly one capability role"
                    )
            finally:
                cursor.close()
        finally:
            if not previous_autocommit:
                dbapi_connection.autocommit = False

    return initialize


def create_database_engine(settings: Settings) -> Engine:
    """Create an engine that revalidates one fixed capability on every pool checkout."""

    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_recycle=300,
    )
    event.listen(engine, "checkout", _runtime_session_initializer(settings.database_role))
    return engine
