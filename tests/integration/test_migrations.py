from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.schema import APPEND_ONLY_TABLES, metadata

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL migration tests")
    return value


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def assert_role_permission_denied(
    engine: Engine,
    *,
    role: str,
    statement: str,
    parameters: dict[str, object] | None = None,
) -> None:
    assert role in {
        "careerops_api",
        "careerops_outbox",
        "careerops_retention",
        "careerops_side_effect",
    }
    with (
        pytest.raises(DBAPIError) as permission_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text(f"SET LOCAL ROLE {role}"))
        connection.execute(sa.text(statement), parameters or {})
    assert sqlstate(permission_error.value) == "42501"


def test_initial_migration_round_trip_and_database_guards(database_url: str) -> None:
    config = alembic_config(database_url)
    engine = sa.create_engine(database_url)

    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert not sa.inspect(connection).has_schema("careerops")

    command.upgrade(config, "head")
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        assert set(inspector.get_table_names(schema="careerops")) == {
            table.name for table in metadata.tables.values()
        }
        trigger_count = connection.scalar(
            sa.text(
                """
                SELECT count(*)
                FROM pg_trigger AS trigger
                JOIN pg_class AS table_info ON table_info.oid = trigger.tgrelid
                JOIN pg_namespace AS namespace ON namespace.oid = table_info.relnamespace
                WHERE namespace.nspname = 'careerops'
                  AND NOT trigger.tgisinternal
                """
            )
        )
        # The append-only guards plus lifecycle/reference guards and the Agent
        # console legacy-state normalization trigger.
        assert trigger_count == len(APPEND_ONLY_TABLES) + 9
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege('careerops_api', 'careerops.audit_events', 'INSERT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_api', 'careerops.append_audit_event("
                "uuid,timestamp with time zone,text,text,text,text,uuid,text,jsonb)', "
                "'EXECUTE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_sequence_privilege("
                "'careerops_api', pg_get_serial_sequence("
                "'careerops.audit_events', 'sequence'), 'USAGE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege('careerops_api', 'careerops.audit_events', 'UPDATE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_api', 'careerops.require_active_content_blob()', 'EXECUTE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_readonly', 'careerops.oauth_credential_references', "
                "'secret_handle', 'SELECT')"
            )
        )

    runtime_login = f"careerops_api_test_{uuid4().hex}"
    runtime_password = "careerops-api-test-password"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE ROLE {runtime_login} "
                "LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"NOREPLICATION NOBYPASSRLS PASSWORD '{runtime_password}'"
            )
        )
        connection.execute(sa.text(f"GRANT careerops_api TO {runtime_login}"))
    runtime_url = sa.engine.make_url(database_url).set(
        username=runtime_login,
        password=runtime_password,
    )
    runtime_engine = create_database_engine(
        Settings.model_validate(
            {
                "database_url": runtime_url.render_as_string(hide_password=False),
                "database_role": DatabaseCapabilityRole.API,
            }
        )
    )
    with runtime_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_user")) == "careerops_api"
        assert connection.scalar(sa.text("SELECT current_setting('search_path')")) == (
            "pg_catalog, careerops"
        )
        assert not connection.scalar(
            sa.text("SELECT has_table_privilege(current_user, 'careerops.audit_events', 'UPDATE')")
        )
        connection.execute(sa.text("RESET ROLE"))
        assert connection.scalar(sa.text("SELECT current_user")) == runtime_login
        with pytest.raises(DBAPIError) as reset_role_error:
            connection.execute(sa.text("SELECT count(*) FROM careerops.audit_events"))
        assert sqlstate(reset_role_error.value) == "42501"
    with runtime_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_user")) == "careerops_api"
    runtime_engine.dispose()
    with engine.begin() as connection:
        connection.execute(sa.text(f"REVOKE careerops_api FROM {runtime_login}"))
        connection.execute(sa.text(f"DROP ROLE {runtime_login}"))

    audit_event_id = uuid4()
    resource_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                SELECT sequence
                FROM careerops.append_audit_event(
                    :event_id,
                    :occurred_at,
                    'worker',
                    NULL,
                    'migration_test',
                    'test',
                    :resource_id,
                    'migration-test',
                    '{}'::jsonb
                )
                """
            ),
            {
                "event_id": audit_event_id,
                "occurred_at": datetime.now(UTC),
                "resource_id": resource_id,
            },
        )

    with (
        pytest.raises(DBAPIError) as role_permission_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                "UPDATE careerops.audit_events SET trace_id = 'mutated' WHERE event_id = :event_id"
            ),
            {"event_id": audit_event_id},
        )
    assert sqlstate(role_permission_error.value) == "42501"

    with (
        pytest.raises(DBAPIError) as append_only_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE careerops.audit_events SET trace_id = 'owner-mutated' "
                "WHERE event_id = :event_id"
            ),
            {"event_id": audit_event_id},
        )
    assert sqlstate(append_only_error.value) == "55000"

    blob_id = uuid4()
    content_object_id = uuid4()
    digest = "c" * 64
    retention_until = datetime.now(UTC) - timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.content_blobs (
                    id, sha256, object_key, byte_size
                ) VALUES (
                    :id, :sha256, :object_key, 4
                )
                """
            ),
            {
                "id": blob_id,
                "sha256": digest,
                "object_key": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.content_objects (
                    id, blob_id, media_type, classification,
                    owner_resource_type, owner_resource_id, retention_until
                ) VALUES (
                    :id, :blob_id, 'text/plain', 'decision_evidence',
                    'test', :owner_resource_id, :retention_until
                )
                """
            ),
            {
                "id": content_object_id,
                "blob_id": blob_id,
                "owner_resource_id": resource_id,
                "retention_until": retention_until,
            },
        )

    early_expired_at = retention_until - timedelta(seconds=1)
    with (
        pytest.raises(DBAPIError) as early_expiry_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        connection.execute(
            sa.text("UPDATE careerops.content_objects SET expired_at = :expired_at WHERE id = :id"),
            {"expired_at": early_expired_at, "id": content_object_id},
        )
    assert sqlstate(early_expiry_error.value) == "55000"

    expired_at = retention_until
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        result = connection.execute(
            sa.text("UPDATE careerops.content_objects SET expired_at = :expired_at WHERE id = :id"),
            {"expired_at": expired_at, "id": content_object_id},
        )
        assert result.rowcount == 1

    with (
        pytest.raises(DBAPIError) as expiry_reversal_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        connection.execute(
            sa.text("UPDATE careerops.content_objects SET expired_at = NULL WHERE id = :id"),
            {"id": content_object_id},
        )
    assert sqlstate(expiry_reversal_error.value) == "55000"

    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT expired_at FROM careerops.content_objects WHERE id = :id"),
                {"id": content_object_id},
            )
            == expired_at
        )

    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement=(
            "UPDATE careerops.content_objects SET retention_until = :retention_until WHERE id = :id"
        ),
        parameters={
            "retention_until": retention_until + timedelta(days=1),
            "id": content_object_id,
        },
    )
    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement="UPDATE careerops.evidence_records SET sanitized_span = 'tampered' WHERE false",
    )
    assert_role_permission_denied(
        engine,
        role="careerops_retention",
        statement=(
            "UPDATE careerops.content_objects SET owner_resource_type = 'tampered' WHERE id = :id"
        ),
        parameters={"id": content_object_id},
    )
    assert_role_permission_denied(
        engine,
        role="careerops_retention",
        statement="UPDATE careerops.content_blobs SET sha256 = :sha256 WHERE id = :id",
        parameters={"sha256": "d" * 64, "id": blob_id},
    )
    unreferenced_blob_id = uuid4()
    unreferenced_digest = "e" * 64
    with (
        pytest.raises(DBAPIError) as unreferenced_blob_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        connection.execute(
            sa.text(
                "INSERT INTO careerops.content_blobs (id, sha256, object_key, byte_size) "
                "VALUES (:id, :sha256, :object_key, 4)"
            ),
            {
                "id": unreferenced_blob_id,
                "sha256": unreferenced_digest,
                "object_key": (
                    f"sha256/{unreferenced_digest[:2]}/"
                    f"{unreferenced_digest[2:4]}/{unreferenced_digest}"
                ),
            },
        )
    assert sqlstate(unreferenced_blob_error.value) == "55000"
    with (
        pytest.raises(DBAPIError) as lifecycle_bypass_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        connection.execute(
            sa.text(
                "UPDATE careerops.content_blobs "
                "SET deletion_state = 'deleted', deleted_at = now(), "
                "delete_result = 'deleted' WHERE id = :id"
            ),
            {"id": blob_id},
        )
    assert sqlstate(lifecycle_bypass_error.value) == "55000"
    assert_role_permission_denied(
        engine,
        role="careerops_side_effect",
        statement="SELECT * FROM careerops.oauth_credential_references",
    )
    assert_role_permission_denied(
        engine,
        role="careerops_outbox",
        statement="SELECT * FROM careerops.action_payload_versions",
    )

    intent_a = uuid4()
    intent_b = uuid4()
    payload_b = uuid4()
    with engine.begin() as connection:
        for intent_id, key in ((intent_a, "intent-a"), (intent_b, "intent-b")):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO careerops.action_intents (
                        id, action_kind, resource_type, resource_id,
                        idempotency_key, created_by
                    ) VALUES (
                        :id, 'test_action', 'test', :resource_id,
                        :idempotency_key, 'migration-test'
                    )
                    """
                ),
                {
                    "id": intent_id,
                    "resource_id": resource_id,
                    "idempotency_key": key,
                },
            )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.action_payload_versions (
                    id, action_intent_id, version, target, payload, payload_hash
                ) VALUES (
                    :id, :intent_id, 1, '{}'::jsonb, '{}'::jsonb, :payload_hash
                )
                """
            ),
            {"id": payload_b, "intent_id": intent_b, "payload_hash": "b" * 64},
        )

    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement=("UPDATE careerops.action_intents SET action_kind = 'tampered' WHERE id = :id"),
        parameters={"id": intent_a},
    )
    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement=(
            "UPDATE careerops.approval_requests "
            "SET payload_version_id = payload_version_id WHERE false"
        ),
    )
    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement=(
            "INSERT INTO careerops.outbox_events ("
            "id, event_key, action_intent_id, payload_version_id, event_type, status, "
            "available_at, published_at) VALUES ("
            ":id, :event_key, :intent_id, :payload_id, 'workflow_signal', 'published', "
            "now(), now())"
        ),
        parameters={
            "id": uuid4(),
            "event_key": f"forged/{uuid4()}",
            "intent_id": intent_b,
            "payload_id": payload_b,
        },
    )

    with (
        pytest.raises(DBAPIError) as cross_intent_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE careerops.action_intents "
                "SET current_payload_version_id = :payload_id WHERE id = :intent_id"
            ),
            {"payload_id": payload_b, "intent_id": intent_a},
        )
    assert sqlstate(cross_intent_error.value) == "23503"

    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert not sa.inspect(connection).has_schema("careerops")

    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
