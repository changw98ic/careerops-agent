from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.application_adapters import (
    BrowserIsolationMode,
    FormFieldSpec,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationFixture,
)
from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    SyntheticReleaseQualification,
)
from careerops.application.submission_dispatch import (
    DispatchAuthority,
    QualifiedSyntheticDispatchRequest,
    SyntheticSubmissionDispatchPlanner,
)
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.schema import APPEND_ONLY_TABLES, metadata
from careerops.infrastructure.database.submission_dispatch import (
    PostgresSyntheticDispatchReservationStore,
)
from careerops.policy.autopilot import AutopilotOutcome

pytestmark = pytest.mark.integration

_DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL migration tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "PostgreSQL migration tests require a disposable database named "
            f"{_DISPOSABLE_DATABASE_PREFIX}*"
        )
    if os.environ.get(_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT) != "1":
        pytest.skip(
            f"{_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT}=1 is required because migration tests "
            "downgrade the target database to base"
        )
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
        "careerops_mailbox",
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
        # Three reference guards, blob/object/outbox state guards, one deferred
        # active-blob registration guard, autopilot grant/authorization/revocation/
        # cap-reservation guards, the grant-revocation state lock, the separate
        # synthetic execution-boundary guard,
        # kill-switch per-scope serialization, crawler approval/dispatch guards, and
        # the release-qualification evidence-freeze and decision lifecycle guards,
        # the canonical crawler-ingestion evidence binding guard, and goal-run
        # identity/version immutability, plus goal-run Gmail composition identity
        # immutability, the legacy-compatible candidate-owner insert guard, and
        # the final pre-application profile-approval guard and the database-level
        # pre-application GoalRun create guard.
        assert trigger_count == len(APPEND_ONLY_TABLES) + 24
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
        assert connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_outbox', "
                "'careerops.complete_crawler_execution_outbox_event("
                "uuid,text,uuid,text,text,uuid)', 'EXECUTE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_api', "
                "'careerops.complete_crawler_execution_outbox_event("
                "uuid,text,uuid,text,text,uuid)', 'EXECUTE')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_workflow', "
                "'careerops.goal_run_prepare_gmail(uuid,uuid,jsonb,jsonb,jsonb,text)', "
                "'EXECUTE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_workflow', 'careerops.goal_run_gmail_compositions', 'INSERT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_workflow', "
                "'careerops.gmail_send_reserve_and_enqueue("
                "uuid,uuid,uuid,uuid,uuid,uuid,uuid,text,text,uuid,text,text,uuid,uuid,"
                "text,text,text,text,text,text,text)', 'EXECUTE')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_outbox', 'careerops.crawler_execution_results', 'SELECT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_outbox', 'careerops.crawler_execution_results', 'INSERT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_outbox', 'careerops.crawler_execution_results', 'UPDATE')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_readonly', 'careerops.oauth_credential_references', "
                "'secret_handle', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_api', 'careerops.autopilot_cap_reservations', "
                "'release_evidence_hash', 'INSERT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_api', 'careerops.autopilot_cap_reservations', "
                "'release_evidence_expires_at', 'INSERT')"
            )
        )
        assert connection.scalar(
            sa.text("SELECT has_schema_privilege('careerops_mailbox', 'careerops', 'USAGE')")
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', 'SELECT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_mailbox', 'careerops.gmail_sync_runs', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', 'id', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', 'provider', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', 'status', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_sync_runs', "
                "'gmail_account_id', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_sync_runs', 'status', 'SELECT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', "
                "'oauth_credential_reference_id', 'SELECT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_accounts', "
                "'account_subject', 'SELECT')"
            )
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_column_privilege("
                "'careerops_mailbox', 'careerops.gmail_sync_runs', "
                "'lease_token', 'SELECT')"
            )
        )
        assert connection.scalar(
            sa.text(
                "SELECT has_function_privilege("
                "'careerops_mailbox', "
                "'careerops.gmail_readonly_claim_sync_runs(text, integer, integer)', "
                "'EXECUTE')"
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
        role="careerops_side_effect",
        statement="SELECT * FROM careerops.outbox_events",
    )
    assert_role_permission_denied(
        engine,
        role="careerops_outbox",
        statement="SELECT * FROM careerops.action_payload_versions",
    )
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_mailbox"))
        connection.execute(
            sa.text("SELECT id, provider, status FROM careerops.gmail_accounts LIMIT 1")
        )
        connection.execute(
            sa.text("SELECT gmail_account_id, status FROM careerops.gmail_sync_runs LIMIT 1")
        )
    assert_role_permission_denied(
        engine,
        role="careerops_mailbox",
        statement="SELECT * FROM careerops.gmail_accounts LIMIT 1",
    )
    assert_role_permission_denied(
        engine,
        role="careerops_mailbox",
        statement=(
            "SELECT id, oauth_credential_reference_id FROM careerops.gmail_accounts LIMIT 1"
        ),
    )
    assert_role_permission_denied(
        engine,
        role="careerops_mailbox",
        statement="SELECT gmail_account_id, lease_token FROM careerops.gmail_sync_runs LIMIT 1",
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

    console_user_id = uuid4()
    campaign_id = uuid4()
    grant_version_id = uuid4()
    non_owner_user_id = uuid4()
    other_campaign_id = uuid4()
    other_grant_version_id = uuid4()
    autopilot_intent_id = uuid4()
    autopilot_payload_id = uuid4()
    deny_policy_decision_id = uuid4()
    generic_allow_policy_decision_id = uuid4()
    expired_autopilot_policy_decision_id = uuid4()
    mismatched_ruleset_policy_decision_id = uuid4()
    valid_autopilot_policy_decision_id = uuid4()
    source_draft_payload_hash = "b" * 64
    synthetic_payload = SandboxApplicationPayload(
        action_intent_id=autopilot_intent_id,
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "resume_sha256": "a" * 64,
            }
        ),
        source_draft_payload_hash=source_draft_payload_hash,
        approved_material_hashes=("a" * 64,),
    )
    payload_hash = synthetic_payload.payload_hash
    grant_created_at = datetime.now(UTC) - timedelta(minutes=1)
    grant_expires_at = datetime.now(UTC) + timedelta(days=1)
    authorized_at = datetime.now(UTC)
    authorization_expires_at = authorized_at + timedelta(hours=1)
    policy_expires_at = authorized_at + timedelta(hours=2)
    expired_policy_expires_at = authorized_at - timedelta(seconds=1)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.console_users (
                    id, username, password_hash, password_changed_at
                ) VALUES (
                    :id, 'migrationtester', '$argon2id$migration-test',
                    :password_changed_at
                )
                """
            ),
            {
                "id": console_user_id,
                "password_changed_at": grant_created_at,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_campaigns (
                    id, owner_user_id, name, objective, criteria, exclusions, created_by
                ) VALUES (
                    :id, :owner_user_id, 'Migration guard test',
                    'Verify database authorization guard',
                    '{}'::jsonb, '[]'::jsonb, 'migration-test'
                ), (
                    :other_id, :other_owner_user_id, 'Migration guard test 2',
                    'Verify database revocation guard',
                    '{}'::jsonb, '[]'::jsonb, 'migration-test'
                )
                """
            ),
            {
                "id": campaign_id,
                "owner_user_id": console_user_id,
                "other_id": other_campaign_id,
                "other_owner_user_id": console_user_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_grant_versions (
                    id, campaign_id, version, subject_actor, allowed_action_kinds,
                    allowed_channels, allowed_target_hosts, material_hashes,
                    max_total_submissions, max_daily_submissions, max_per_company,
                    policy_ruleset_version, release_version, expires_at, created_at
                ) VALUES (
                    :id, :campaign_id, 1, :subject_actor,
                    '["submit_application"]'::jsonb,
                    '["synthetic:greenhouse-sandbox"]'::jsonb,
                    '["sandbox.greenhouse.test"]'::jsonb,
                    '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]'::jsonb,
                    5, 2, 1, 'policy-v1', 'release-v1',
                    :expires_at, :created_at
                ), (
                    :other_id, :other_campaign_id, 1, :other_subject_actor,
                    '["submit_application"]'::jsonb,
                    '["synthetic:greenhouse-sandbox"]'::jsonb,
                    '["other-sandbox.test"]'::jsonb,
                    '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]'::jsonb,
                    5, 2, 1, 'policy-v1', 'release-v1',
                    :expires_at, :created_at
                )
                """
            ),
            {
                "id": grant_version_id,
                "campaign_id": campaign_id,
                "other_id": other_grant_version_id,
                "other_campaign_id": other_campaign_id,
                "subject_actor": str(console_user_id),
                "other_subject_actor": str(console_user_id),
                "expires_at": grant_expires_at,
                "created_at": grant_created_at,
            },
        )

    with (
        pytest.raises(DBAPIError) as grant_subject_actor_mismatch_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_grant_versions (
                    id, campaign_id, version, subject_actor, allowed_action_kinds,
                    allowed_channels, allowed_target_hosts, material_hashes,
                    max_total_submissions, max_daily_submissions, max_per_company,
                    policy_ruleset_version, release_version, expires_at
                ) VALUES (
                    :id, :campaign_id, 2, :subject_actor,
                    '["submit_application"]'::jsonb,
                    '["synthetic:greenhouse-sandbox"]'::jsonb,
                    '["sandbox.greenhouse.test"]'::jsonb,
                    '["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]'::jsonb,
                    5, 2, 1, 'policy-v1', 'release-v1', :expires_at
                )
                """
            ),
            {
                "id": uuid4(),
                "campaign_id": campaign_id,
                "subject_actor": str(non_owner_user_id),
                "expires_at": grant_expires_at,
            },
        )
    assert sqlstate(grant_subject_actor_mismatch_error.value) == "23514"

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.action_intents (
                    id, action_kind, resource_type, resource_id,
                    idempotency_key, created_by
                ) VALUES (
                    :id, 'submit_application', 'job_posting', :resource_id,
                    :idempotency_key, 'migration-test'
                )
                """
            ),
            {
                "id": autopilot_intent_id,
                "resource_id": resource_id,
                "idempotency_key": f"autopilot-guard/{uuid4()}",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.action_payload_versions (
                    id, action_intent_id, version, target, payload, attachment_refs, payload_hash
                ) VALUES (
                    :id, :intent_id, 1,
                    '{"target_host":"sandbox.greenhouse.test","channel":"synthetic:greenhouse-sandbox"}'::jsonb,
                    '{"candidate":"migration-test","source_draft_payload_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","fields":{"first_name":"Ada","last_name":"Lovelace","resume_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'::jsonb,
                    '[{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}]'::jsonb,
                    :payload_hash
                )
                """
            ),
            {
                "id": autopilot_payload_id,
                "intent_id": autopilot_intent_id,
                "payload_hash": payload_hash,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.policy_decisions (
                    id, action_intent_id, payload_version_id, ruleset_version,
                    decision, reason_codes, payload_hash, expires_at
                ) VALUES (
                    :id, :intent_id, :payload_id, 'policy-v1',
                    'deny', '["blocked"]'::jsonb, :payload_hash, :expires_at
                ), (
                    :generic_allow_id, :intent_id, :payload_id, 'policy-v1',
                    'allow', '["generic_allow"]'::jsonb, :payload_hash, :expires_at
                ), (
                    :expired_autopilot_id, :intent_id, :payload_id, 'policy-v1',
                    'allow_autopilot_submission', '["expired"]'::jsonb,
                    :payload_hash, :expired_expires_at
                ), (
                    :mismatched_ruleset_id, :intent_id, :payload_id, 'policy-v2',
                    'allow_autopilot_submission', '["wrong_ruleset"]'::jsonb,
                    :payload_hash, :expires_at
                ), (
                    :valid_autopilot_id, :intent_id, :payload_id, 'policy-v1',
                    'allow_autopilot_submission', '["qualified"]'::jsonb,
                    :payload_hash, :expires_at
                )
                """
            ),
            {
                "id": deny_policy_decision_id,
                "generic_allow_id": generic_allow_policy_decision_id,
                "expired_autopilot_id": expired_autopilot_policy_decision_id,
                "mismatched_ruleset_id": mismatched_ruleset_policy_decision_id,
                "valid_autopilot_id": valid_autopilot_policy_decision_id,
                "intent_id": autopilot_intent_id,
                "payload_id": autopilot_payload_id,
                "payload_hash": payload_hash,
                "expires_at": policy_expires_at,
                "expired_expires_at": expired_policy_expires_at,
            },
        )

    with (
        pytest.raises(DBAPIError) as denied_autopilot_authorization_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_intent_authorizations (
                    id, campaign_id, grant_version_id, action_intent_id,
                    payload_version_id, payload_hash, policy_decision_id,
                    authorization_outcome, reason_codes, authorized_at, expires_at
                ) VALUES (
                    :id, :campaign_id, :grant_version_id, :intent_id,
                    :payload_id, :payload_hash, :policy_decision_id,
                    'allow_autopilot_submission', '[]'::jsonb,
                    :authorized_at, :expires_at
                )
                """
            ),
            {
                "id": uuid4(),
                "campaign_id": campaign_id,
                "grant_version_id": grant_version_id,
                "intent_id": autopilot_intent_id,
                "payload_id": autopilot_payload_id,
                "payload_hash": payload_hash,
                "policy_decision_id": deny_policy_decision_id,
                "authorized_at": authorized_at,
                "expires_at": authorization_expires_at,
            },
        )
    assert sqlstate(denied_autopilot_authorization_error.value) == "23514"

    for policy_decision_id in (
        generic_allow_policy_decision_id,
        expired_autopilot_policy_decision_id,
        mismatched_ruleset_policy_decision_id,
    ):
        with (
            pytest.raises(DBAPIError) as invalid_policy_evidence_error,
            engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO careerops.autopilot_intent_authorizations (
                        id, campaign_id, grant_version_id, action_intent_id,
                        payload_version_id, payload_hash, policy_decision_id,
                        authorization_outcome, reason_codes, authorized_at, expires_at
                    ) VALUES (
                        :id, :campaign_id, :grant_version_id, :intent_id,
                        :payload_id, :payload_hash, :policy_decision_id,
                        'allow_autopilot_submission', '[]'::jsonb,
                        :authorized_at, :expires_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "campaign_id": campaign_id,
                    "grant_version_id": grant_version_id,
                    "intent_id": autopilot_intent_id,
                    "payload_id": autopilot_payload_id,
                    "payload_hash": payload_hash,
                    "policy_decision_id": policy_decision_id,
                    "authorized_at": authorized_at,
                    "expires_at": authorization_expires_at,
                },
            )
        assert sqlstate(invalid_policy_evidence_error.value) == "23514"

    valid_autopilot_authorization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_intent_authorizations (
                    id, campaign_id, grant_version_id, action_intent_id,
                    payload_version_id, payload_hash, policy_decision_id,
                    authorization_outcome, reason_codes, authorized_at, expires_at
                ) VALUES (
                    :id, :campaign_id, :grant_version_id, :intent_id,
                    :payload_id, :payload_hash, :policy_decision_id,
                    'allow_autopilot_submission', '["qualified"]'::jsonb,
                    :authorized_at, :expires_at
                )
                """
            ),
            {
                "id": valid_autopilot_authorization_id,
                "campaign_id": campaign_id,
                "grant_version_id": grant_version_id,
                "intent_id": autopilot_intent_id,
                "payload_id": autopilot_payload_id,
                "payload_hash": payload_hash,
                "policy_decision_id": valid_autopilot_policy_decision_id,
                "authorized_at": authorized_at,
                "expires_at": authorization_expires_at,
            },
        )
        synthetic_request = QualifiedSyntheticDispatchRequest(
            authority=DispatchAuthority(
                campaign_id=campaign_id,
                grant_version_id=grant_version_id,
                authorization_id=valid_autopilot_authorization_id,
                action_intent_id=autopilot_intent_id,
                payload_version_id=autopilot_payload_id,
                policy_decision_id=valid_autopilot_policy_decision_id,
                action_kind="submit_application",
                channel="synthetic:greenhouse-sandbox",
                release_version="release-v1",
                payload_hash=payload_hash,
                target_host="sandbox.greenhouse.test",
                company_key="example-inc",
                policy_outcome=AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
                authorized_at=authorized_at,
                expires_at=authorization_expires_at,
            ),
            fixture=SyntheticApplicationFixture(
                fixture_id="fixture-1",
                adapter_id="greenhouse-sandbox",
                allowed_host="sandbox.greenhouse.test",
                site_policy_text="Automated submissions are allowed for this synthetic sandbox.",
                allowed_fields=(
                    FormFieldSpec("first_name"),
                    FormFieldSpec("last_name"),
                    FormFieldSpec("resume_sha256"),
                ),
            ),
            session=SandboxBrowserSession(
                session_id=uuid4(),
                action_intent_id=autopilot_intent_id,
                isolation_mode=BrowserIsolationMode.PER_INTENT_CONTEXT,
                host="sandbox.greenhouse.test",
            ),
            payload=synthetic_payload,
            release_qualification=SyntheticReleaseQualification(
                adapter_id="greenhouse-sandbox",
                fixture_id="fixture-1",
                release_version="release-v1",
                stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
                evidence_hash="b" * 64,
                expires_at=authorization_expires_at,
            ),
            now=authorized_at + timedelta(seconds=1),
        )
        decision = SyntheticSubmissionDispatchPlanner().plan(synthetic_request)
        assert decision.can_reserve
        store = PostgresSyntheticDispatchReservationStore(connection)
        reservation_id = store.reserve_and_enqueue(decision, synthetic_request)
        assert store.reserve_and_enqueue(decision, synthetic_request) == reservation_id
        reservation_row = connection.execute(
            sa.text(
                """
                SELECT reservation_date, reserved_at
                FROM careerops.autopilot_cap_reservations
                WHERE id = :id
                """
            ),
            {"id": reservation_id},
        ).one()
        assert reservation_row.reservation_date == reservation_row.reserved_at.date()
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM careerops.outbox_events "
                    "WHERE action_intent_id = :intent_id"
                ),
                {"intent_id": autopilot_intent_id},
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM careerops.audit_events "
                    "WHERE event_type = 'synthetic_dispatch_reserved' "
                    "AND resource_id = :intent_id"
                ),
                {"intent_id": autopilot_intent_id},
            )
            == 1
        )
        audit_event_data = connection.scalar(
            sa.text(
                "SELECT event_data FROM careerops.audit_events "
                "WHERE event_type = 'synthetic_dispatch_reserved' "
                "AND resource_id = :intent_id"
            ),
            {"intent_id": autopilot_intent_id},
        )
        assert isinstance(audit_event_data, dict)
        assert audit_event_data["source_draft_payload_hash"] == source_draft_payload_hash
        assert audit_event_data["approved_material_hashes"] == ["a" * 64]

    with (
        pytest.raises(DBAPIError) as real_target_reservation_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_cap_reservations (
                    id, campaign_id, grant_version_id, authorization_id,
                    action_intent_id, payload_version_id, payload_hash,
                    target_host, channel, release_version, company_key, adapter_id, fixture_id,
                    reservation_key, reconciliation_key
                ) VALUES (
                    :id, :campaign_id, :grant_version_id, :authorization_id,
                    :intent_id, :payload_id, :payload_hash,
                    'greenhouse.io', 'synthetic:greenhouse-sandbox', 'release-v1',
                    'example-inc', 'greenhouse-sandbox', 'fixture-1',
                    :reservation_key, :reconciliation_key
                )
                """
            ),
            {
                "id": uuid4(),
                "campaign_id": campaign_id,
                "grant_version_id": grant_version_id,
                "authorization_id": valid_autopilot_authorization_id,
                "intent_id": autopilot_intent_id,
                "payload_id": autopilot_payload_id,
                "payload_hash": payload_hash,
                "reservation_key": f"reservation/{uuid4().hex}",
                "reconciliation_key": f"reconcile/{uuid4().hex}",
            },
        )
    assert sqlstate(real_target_reservation_error.value) == "23514"

    assert_role_permission_denied(
        engine,
        role="careerops_api",
        statement=(
            "INSERT INTO careerops.autopilot_cap_reservations (id, reserved_at) VALUES (:id, now())"
        ),
        parameters={"id": uuid4()},
    )

    manual_only_authorization_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_intent_authorizations (
                    id, campaign_id, grant_version_id, action_intent_id,
                    payload_version_id, payload_hash, policy_decision_id,
                    authorization_outcome, reason_codes, authorized_at, expires_at
                ) VALUES (
                    :id, :campaign_id, :grant_version_id, :intent_id,
                    :payload_id, :payload_hash, :policy_decision_id,
                    'manual_only', '["blocked"]'::jsonb,
                    :authorized_at, :expires_at
                )
                """
            ),
            {
                "id": manual_only_authorization_id,
                "campaign_id": campaign_id,
                "grant_version_id": grant_version_id,
                "intent_id": autopilot_intent_id,
                "payload_id": autopilot_payload_id,
                "payload_hash": payload_hash,
                "policy_decision_id": deny_policy_decision_id,
                "authorized_at": authorized_at,
                "expires_at": authorization_expires_at,
            },
        )

    with (
        pytest.raises(DBAPIError) as manual_only_pending_review_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_review_items (
                    id, authorization_id, review_kind, resolution_mode,
                    reason_codes, snapshot, created_by
                ) VALUES (
                    :id, :authorization_id, 'pending', 'manual_only',
                    '["blocked"]'::jsonb, '{}'::jsonb, 'migration-test'
                )
                """
            ),
            {
                "id": uuid4(),
                "authorization_id": manual_only_authorization_id,
            },
        )
    assert sqlstate(manual_only_pending_review_error.value) == "23514"

    with (
        pytest.raises(DBAPIError) as non_owner_revocation_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_grant_revocations (
                    id, grant_version_id, revoked_by_user_id,
                    superseded_by_grant_version_id, reason
                ) VALUES (
                    :id, :grant_version_id, :revoked_by_user_id,
                    NULL, 'non-owner revocation'
                )
                """
            ),
            {
                "id": uuid4(),
                "grant_version_id": grant_version_id,
                "revoked_by_user_id": non_owner_user_id,
            },
        )
    assert sqlstate(non_owner_revocation_error.value) == "23514"

    with (
        pytest.raises(DBAPIError) as cross_campaign_supersede_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_grant_revocations (
                    id, grant_version_id, revoked_by_user_id,
                    superseded_by_grant_version_id, reason
                ) VALUES (
                    :id, :grant_version_id, :revoked_by_user_id,
                    :superseded_by_grant_version_id, 'cross-campaign supersede'
                )
                """
            ),
            {
                "id": uuid4(),
                "grant_version_id": grant_version_id,
                "revoked_by_user_id": console_user_id,
                "superseded_by_grant_version_id": other_grant_version_id,
            },
        )
    assert sqlstate(cross_campaign_supersede_error.value) == "23514"

    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert not sa.inspect(connection).has_schema("careerops")

    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
