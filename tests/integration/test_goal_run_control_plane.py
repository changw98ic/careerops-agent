from __future__ import annotations

# ruff: noqa: E501,I001,SIM117

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.goal_run import goal_run_list_page_from_mapping

pytestmark = pytest.mark.integration

_DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"
ACTOR_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("10000000-0000-0000-0000-000000000002")
SNAPSHOT = "b" * 64


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL goal-run tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "PostgreSQL goal-run tests require a disposable database named "
            f"{_DISPOSABLE_DATABASE_PREFIX}*"
        )
    if os.environ.get(_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT) != "1":
        pytest.skip(
            f"{_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT}=1 is required because goal-run tests "
            "downgrade the target database to base"
        )
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    yield engine
    engine.dispose()


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)


def role_exists(engine: Engine, role: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                {"role": role},
            )
        )


def seed_actor(connection: sa.Connection, actor_id: UUID = ACTOR_ID) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_changed_at
            )
            VALUES (
                :actor_id, 'goalrunner', '$argon2id$test', :now
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"actor_id": actor_id, "now": datetime(2026, 7, 20, tzinfo=UTC)},
    )


def create_goal_run(
    connection: sa.Connection,
    *,
    idempotency_key: str = "goal.create.1",
    registry_id: UUID | None = None,
    source_id: str | None = "greenhouse",
) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_create(
                :actor_id, :idempotency_key, 'job-search',
                'Find bounded backend roles.', '{"target":"backend"}'::jsonb,
                :registry_id, :source_id, 25, 'goal-workflow-1', 'trace-create'
            )
            """
        ),
        {
            "actor_id": ACTOR_ID,
            "idempotency_key": idempotency_key,
            "registry_id": registry_id,
            "source_id": source_id,
        },
    )
    assert isinstance(result, dict)
    record = result["record"]
    assert isinstance(record, dict)
    return record


def seed_registry_source(connection: sa.Connection) -> UUID:
    registry_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.crawler_source_registries (
                id, kind, manifest_path, manifest_sha256, registry_sha256,
                source_count, created_by, generated_at, manifest_json
            )
            VALUES (
                :registry_id, 'recruitment', :manifest_path, :manifest_sha256,
                :registry_sha256, 1, 'test', now(), '{"sources":["greenhouse"]}'::jsonb
            )
            """
        ),
        {
            "registry_id": registry_id,
            "manifest_path": f"manifests/{registry_id.hex}.json",
            "manifest_sha256": "1" * 64,
            "registry_sha256": "2" * 64,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.crawler_source_registry_sources (
                id, registry_id, source_id, adapter, input_artifact, output_dir,
                command_sha256, source_sha256, source_json, cadence_seconds,
                retry_rounds, budget_json, rate_limit_json,
                robots_terms_policy, canonical_ingestion_policy,
                provenance_policy, dedupe_policy
            )
            VALUES (
                :source_row_id, :registry_id, 'greenhouse', 'recruitment.public_ats_feed',
                'inputs/greenhouse.json', 'outputs/greenhouse',
                :command_sha256, :source_sha256, '{"source":"greenhouse"}'::jsonb,
                3600, 1,
                jsonb_build_object(
                    'timeout_seconds', 30,
                    'max_feed_bytes', 1000000,
                    'retry_rounds', 1
                ),
                jsonb_build_object('concurrency', 1),
                'respect', 'canonical_job_ingestion',
                'preserve', 'hash'
            )
            """
        ),
        {
            "source_row_id": uuid4(),
            "registry_id": registry_id,
            "command_sha256": "3" * 64,
            "source_sha256": "4" * 64,
        },
    )
    return registry_id


def seed_reviewed_crawler_execution(
    connection: sa.Connection,
    *,
    registry_id: UUID,
    outcome: str | None,
    approved: bool = True,
    decision: str | None = None,
    expired: bool = False,
    manifest_sha256: str = "1" * 64,
    request_index: int = 1,
) -> dict[str, UUID]:
    action_intent_id = uuid4()
    payload_version_id = uuid4()
    request_id = uuid4()
    outbox_event_id = uuid4()
    outbox_lease_token = uuid4()
    payload_hash = f"{request_index:064x}"[-64:]
    execution_key = f"crawler-execution:{request_id}"
    database_now = connection.scalar(sa.text("SELECT clock_timestamp()"))
    assert isinstance(database_now, datetime)
    created_at = database_now
    expires_at = database_now + (timedelta(seconds=1) if expired else timedelta(days=1))
    approval_decision = decision or ("approved" if approved else None)
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.action_intents (
                id, action_kind, resource_type, resource_id, idempotency_key, created_by
            )
            VALUES (
                :action_intent_id, 'crawler_execution', 'crawler_registry',
                :registry_id, :idempotency_key, 'test'
            )
            """
        ),
        {
            "action_intent_id": action_intent_id,
            "registry_id": registry_id,
            "idempotency_key": f"crawler-execution:{request_id}",
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.action_payload_versions (
                id, action_intent_id, version, target, payload, payload_hash
            )
            VALUES (
                :payload_version_id, :action_intent_id, 1, '{}'::jsonb,
                jsonb_build_object('crawler', true), :payload_hash
            )
            """
        ),
        {
            "payload_version_id": payload_version_id,
            "action_intent_id": action_intent_id,
            "payload_hash": payload_hash,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.crawler_execution_requests (
                id, owner_user_id, action_intent_id, payload_version_id,
                payload_hash, manifest_path, request_artifact_path, manifest_sha256,
                request_sha256, reviewed_plan_sha256, source_ids, reason, expires_at,
                created_at
            )
            VALUES (
                :request_id, :actor_id, :action_intent_id, :payload_version_id,
                :payload_hash, 'manifests/reviewed.json', 'requests/reviewed.json',
                :manifest_sha256, :request_sha256, :reviewed_plan_sha256,
                '["greenhouse"]'::jsonb, 'reviewed crawler plan', :expires_at,
                :created_at
            )
            """
        ),
        {
            "request_id": request_id,
            "actor_id": ACTOR_ID,
            "action_intent_id": action_intent_id,
            "payload_version_id": payload_version_id,
            "payload_hash": payload_hash,
            "manifest_sha256": manifest_sha256,
            "request_sha256": "5" * 64,
            "reviewed_plan_sha256": "6" * 64,
            "created_at": created_at,
            "expires_at": expires_at,
        },
    )
    if approval_decision is not None:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.crawler_execution_approvals (
                    id, request_id, decided_by_user_id, decision, decision_reason,
                    approval_artifact_path, approval_artifact_sha256
                )
                VALUES (
                    :approval_id, :request_id, :actor_id, :decision,
                    'reviewed by owner', :approval_path, :approval_hash
                )
                """
            ),
            {
                "approval_id": uuid4(),
                "request_id": request_id,
                "actor_id": ACTOR_ID,
                "decision": approval_decision,
                "approval_path": (
                    "approvals/reviewed.json" if approval_decision == "approved" else None
                ),
                "approval_hash": "7" * 64 if approval_decision == "approved" else None,
            },
        )
    if approval_decision == "approved":
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.outbox_events (
                    id, event_key, action_intent_id, payload_version_id,
                    event_type, available_at
                )
                VALUES (
                    :outbox_event_id, :execution_key, :action_intent_id,
                    :payload_version_id, 'workflow_signal', now()
                )
                """
            ),
            {
                "outbox_event_id": outbox_event_id,
                "execution_key": execution_key,
                "action_intent_id": action_intent_id,
                "payload_version_id": payload_version_id,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.crawler_execution_dispatches (
                    id, request_id, action_intent_id, outbox_event_id, execution_key
                )
                VALUES (
                    :dispatch_id, :request_id, :action_intent_id,
                    :outbox_event_id, :execution_key
                )
                """
            ),
            {
                "dispatch_id": uuid4(),
                "request_id": request_id,
                "action_intent_id": action_intent_id,
                "outbox_event_id": outbox_event_id,
                "execution_key": execution_key,
            },
        )
        connection.execute(
            sa.text(
                """
                UPDATE careerops.outbox_events
                SET status = 'leased',
                    lease_owner = 'goal-run-test-worker',
                    lease_token = :lease_token,
                    lease_until = now() + interval '5 minutes',
                    attempt_count = 1
                WHERE id = :outbox_event_id
                """
            ),
            {
                "outbox_event_id": outbox_event_id,
                "lease_token": outbox_lease_token,
            },
        )
        connection.execute(
            sa.text(
                """
                SELECT set_config(
                    'careerops.outbox_lease_owner', 'goal-run-test-worker', true
                ), set_config(
                    'careerops.outbox_lease_token', :lease_token, true
                )
                """
            ),
            {"lease_token": str(outbox_lease_token)},
        )
        connection.execute(
            sa.text(
                """
                UPDATE careerops.outbox_events
                SET status = 'published',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    published_at = now()
                WHERE id = :outbox_event_id
                """
            ),
            {"outbox_event_id": outbox_event_id},
        )
        if outcome is not None:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO careerops.crawler_execution_results (
                        id, request_id, action_intent_id, outbox_event_id,
                        outcome, error_code
                    )
                    VALUES (
                        :result_id, :request_id, :action_intent_id,
                        :outbox_event_id, :outcome, :error_code
                    )
                    """
                ),
                {
                    "result_id": uuid4(),
                    "request_id": request_id,
                    "action_intent_id": action_intent_id,
                    "outbox_event_id": outbox_event_id,
                    "outcome": outcome,
                    "error_code": None if outcome == "succeeded" else "CRAWLER_ERROR",
                },
            )
    if expired:
        connection.execute(sa.text("SELECT pg_sleep(1.1)"))
    return {"request_id": request_id, "outbox_event_id": outbox_event_id}


def test_goal_run_definer_functions_enforce_owner_idempotency_and_fencing(engine: Engine) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        record = create_goal_run(connection)
        goal_run_id = record["goal_run_id"]
        fencing_token = record["fencing_token"]
        assert goal_run_id
        assert fencing_token
        assert record["source_id"] == "greenhouse"
        assert record["max_records"] == 25
        assert record["temporal_workflow_id"] == "goal-workflow-1"

        replay = create_goal_run(connection)
        assert replay["goal_run_id"] == goal_run_id

        with pytest.raises(DBAPIError) as conflict:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_create(
                            :actor_id, 'goal.create.1', 'job-search',
                            'Different goal.', '{}'::jsonb, NULL, NULL, 25, NULL, 'trace-create'
                        )
                        """
                    ),
                    {"actor_id": ACTOR_ID},
                )
        assert sqlstate(conflict.value) == "23505"

        with pytest.raises(DBAPIError) as owner_error:
            with connection.begin_nested():
                connection.execute(
                    sa.text("SELECT careerops.goal_run_status(:actor_id, :goal_run_id)"),
                    {"actor_id": OTHER_ACTOR_ID, "goal_run_id": goal_run_id},
                )
        assert sqlstate(owner_error.value) == "23503"

        with pytest.raises(DBAPIError) as stale_fence:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_checkpoint(
                            :actor_id, :goal_run_id, 99, :fencing_token,
                            'selecting_source', 'running', 'progress', '{}'::jsonb,
                            NULL, 'goal.checkpoint.stale', 'trace-stale'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": goal_run_id,
                        "fencing_token": fencing_token,
                    },
                )
        assert sqlstate(stale_fence.value) == "55000"


def test_goal_run_create_lookup_is_owner_scoped_optional_and_returns_current_record(
    engine: Engine,
) -> None:
    idempotency_key = f"goal.create.{uuid4().hex}"
    with engine.begin() as connection:
        seed_actor(connection)
        record = create_goal_run(connection, idempotency_key=idempotency_key)
        checkpoint = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_checkpoint(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'selecting_source', 'running', 'progress', '{}'::jsonb,
                    NULL, :checkpoint_key, 'trace-lookup-checkpoint'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": record["goal_run_id"],
                "version": record["version"],
                "fencing_token": record["fencing_token"],
                "checkpoint_key": f"goal.checkpoint.{uuid4().hex}",
            },
        )
        assert isinstance(checkpoint, dict)

        found = connection.scalar(
            sa.text(
                "SELECT careerops.goal_run_lookup_create(:actor_id, :idempotency_key)"
            ),
            {"actor_id": ACTOR_ID, "idempotency_key": idempotency_key},
        )
        cross_owner = connection.scalar(
            sa.text(
                "SELECT careerops.goal_run_lookup_create(:actor_id, :idempotency_key)"
            ),
            {"actor_id": OTHER_ACTOR_ID, "idempotency_key": idempotency_key},
        )
        missing = connection.scalar(
            sa.text(
                "SELECT careerops.goal_run_lookup_create(:actor_id, :idempotency_key)"
            ),
            {"actor_id": ACTOR_ID, "idempotency_key": f"goal.create.{uuid4().hex}"},
        )

        assert isinstance(found, dict)
        assert found["newly_created"] is False
        assert found["idempotency_key"] == idempotency_key
        assert found["record"]["goal_run_id"] == str(record["goal_run_id"])
        assert found["record"]["version"] == checkpoint["record"]["version"]
        assert cross_owner is None
        assert missing is None

    signature = "careerops.goal_run_lookup_create(uuid,text)"
    with engine.connect() as connection:
        public_execute = connection.scalar(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_proc AS procedure
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(
                            procedure.proacl,
                            acldefault('f', procedure.proowner)
                        )
                    ) AS privilege
                    WHERE procedure.oid = to_regprocedure(:signature)
                      AND privilege.grantee = 0
                      AND privilege.privilege_type = 'EXECUTE'
                )
                """
            ),
            {"signature": signature},
        )
        assert public_execute is False

    if role_exists(engine, "careerops_api"):
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            assert connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            )
            api_found = connection.scalar(
                sa.text(
                    "SELECT careerops.goal_run_lookup_create(:actor_id, :idempotency_key)"
                ),
                {"actor_id": ACTOR_ID, "idempotency_key": idempotency_key},
            )
            assert isinstance(api_found, dict)
            assert api_found["record"]["goal_run_id"] == str(record["goal_run_id"])

    for denied_role in ("careerops_workflow", "careerops_readonly"):
        if not role_exists(engine, denied_role):
            continue
        with pytest.raises(DBAPIError) as denied, engine.begin() as connection:
            connection.execute(sa.text(f"SET LOCAL ROLE {denied_role}"))
            assert not connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            )
            connection.execute(
                sa.text(
                    "SELECT careerops.goal_run_lookup_create(:actor_id, :idempotency_key)"
                ),
                {"actor_id": ACTOR_ID, "idempotency_key": idempotency_key},
            )
        assert sqlstate(denied.value) == "42501"


def test_goal_run_owner_pagination_cursor_round_trips_through_domain_model(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")
        create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")

        first_raw = connection.scalar(
            sa.text("SELECT careerops.goal_run_list_owner(:actor_id, 1, NULL)"),
            {"actor_id": ACTOR_ID},
        )
        assert isinstance(first_raw, dict)
        first = goal_run_list_page_from_mapping(first_raw)
        assert len(first.records) == 1
        assert first.next_cursor is not None

        second_raw = connection.scalar(
            sa.text("SELECT careerops.goal_run_list_owner(:actor_id, 1, :cursor)"),
            {"actor_id": ACTOR_ID, "cursor": first.next_cursor},
        )
        assert isinstance(second_raw, dict)
        second = goal_run_list_page_from_mapping(second_raw)
        assert len(second.records) == 1
        assert second.records[0].goal_run_id != first.records[0].goal_run_id


def test_goal_run_versioned_command_replay_binds_version_and_fence(engine: Engine) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        record = create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")
        version = record["version"]
        assert isinstance(version, int)
        command_key = f"goal.cancel.{uuid4().hex}"
        connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_cancel(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'operator stop', :command_key, 'trace-cancel'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": record["goal_run_id"],
                "version": version,
                "fencing_token": record["fencing_token"],
                "command_key": command_key,
            },
        )

        with pytest.raises(DBAPIError) as changed_version:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_cancel(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'operator stop', :command_key, 'trace-cancel'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": record["goal_run_id"],
                        "version": version + 1,
                        "fencing_token": record["fencing_token"],
                        "command_key": command_key,
                    },
                )
        assert sqlstate(changed_version.value) == "23505"

        second = create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")
        second_key = f"goal.cancel.{uuid4().hex}"
        connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_cancel(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'operator stop', :command_key, 'trace-cancel'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": second["goal_run_id"],
                "version": second["version"],
                "fencing_token": second["fencing_token"],
                "command_key": second_key,
            },
        )
        with pytest.raises(DBAPIError) as changed_fence:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_cancel(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'operator stop', :command_key, 'trace-cancel'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": second["goal_run_id"],
                        "version": second["version"],
                        "fencing_token": uuid4(),
                        "command_key": second_key,
                    },
                )
        assert sqlstate(changed_fence.value) == "23505"


def test_goal_run_checkpoint_review_cancel_resume_and_terminal_guards(engine: Engine) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        record = create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")
        goal_run_id = record["goal_run_id"]
        fencing_token = record["fencing_token"]

        version = record["version"]
        for index, phase in enumerate(
            (
                "selecting_source",
                "claiming_source",
                "discovery",
                "complete_source",
                "canonical_ingest",
                "matching",
                "draft_preparation",
            ),
            start=1,
        ):
            checkpoint = connection.scalar(
                sa.text(
                    """
                    SELECT careerops.goal_run_checkpoint(
                        :actor_id, :goal_run_id, :version, :fencing_token,
                        :phase, 'running', 'progress',
                        jsonb_build_object(
                            'semantic_outcome', CAST(:semantic_outcome AS text)
                        ),
                        NULL, :idempotency_key, :trace_id
                    )
                    """
                ),
                {
                    "actor_id": ACTOR_ID,
                    "goal_run_id": goal_run_id,
                    "version": version,
                    "fencing_token": fencing_token,
                    "phase": phase,
                    "semantic_outcome": f"step-{index}",
                    "idempotency_key": f"goal.checkpoint.{index}",
                    "trace_id": f"trace-checkpoint-{index}",
                },
            )
            version = checkpoint["record"]["version"]

        with pytest.raises(DBAPIError) as skipped:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_checkpoint(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'reconciliation', 'running', 'progress', '{}'::jsonb,
                            NULL, 'goal.checkpoint.skip', 'trace-skip'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": goal_run_id,
                        "version": version,
                        "fencing_token": fencing_token,
                    },
                )
        assert sqlstate(skipped.value) == "23514"

        review = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_request_review(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    :review_item_id, :snapshot, 'manual-approval',
                    jsonb_build_object('drafts', 1),
                    'goal.review.request.1', 'trace-review'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
                "review_item_id": uuid4(),
                "snapshot": SNAPSHOT,
            },
        )
        version = review["record"]["version"]
        review_item_id = review["record"]["review_item_id"]
        assert review["record"]["status"] == "waiting_review"
        assert review["record"]["phase"] == "review"

        decided = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_record_review_decision(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    :review_item_id, :snapshot, 'approve',
                    'approved by owner', 'goal.review.decision.1', 'trace-decision'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
                "review_item_id": review_item_id,
                "snapshot": SNAPSHOT,
            },
        )
        version = decided["record"]["version"]
        assert decided["record"]["status"] == "running"
        assert decided["record"]["phase"] == "dispatch"

        blocked = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_checkpoint(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'blocked', 'blocked', 'blocked',
                    '{"resume_phase":"dispatch"}'::jsonb,
                    'WAITING_PROVIDER', 'goal.checkpoint.blocked', 'trace-blocked'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
            },
        )
        assert blocked["record"]["status"] == "blocked"
        resumed = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_resume(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'goal.resume.1', 'trace-resume'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": blocked["record"]["version"],
                "fencing_token": fencing_token,
            },
        )
        assert resumed["record"]["phase"] == "dispatch"
        version = resumed["record"]["version"]

        reconciliation = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_checkpoint(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'reconciliation', 'running', 'progress',
                    '{"semantic_outcome":"dispatch_complete"}'::jsonb,
                    NULL, 'goal.checkpoint.reconciliation', 'trace-reconciliation'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
            },
        )
        version = reconciliation["record"]["version"]
        final = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_checkpoint(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'completed', 'completed', 'finalize',
                    jsonb_build_object('done', true),
                    NULL, 'goal.checkpoint.final', 'trace-final'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
            },
        )
        assert final["record"]["status"] == "completed"
        with pytest.raises(DBAPIError) as terminal:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_cancel(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'too late', 'goal.cancel.terminal', 'trace-terminal'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": goal_run_id,
                        "version": final["record"]["version"],
                        "fencing_token": fencing_token,
                    },
                )
        assert sqlstate(terminal.value) == "23514"
        with pytest.raises(DBAPIError) as direct_update:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        UPDATE careerops.goal_runs
                        SET status = 'running', phase = 'dispatch',
                            version = version + 1, completed_at = NULL
                        WHERE id = :goal_run_id
                        """
                    ),
                    {"goal_run_id": goal_run_id},
                )
        assert sqlstate(direct_update.value) == "23514"

        cancellable = create_goal_run(
            connection,
            idempotency_key=f"goal.create.{uuid4().hex}",
        )
        cancelled = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_cancel(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'operator stopped run', 'goal.cancel.1', 'trace-cancel'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": cancellable["goal_run_id"],
                "version": cancellable["version"],
                "fencing_token": cancellable["fencing_token"],
            },
        )
        with pytest.raises(DBAPIError) as cancelled_resume:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_resume(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'goal.resume.cancelled', 'trace-resume-cancelled'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": cancellable["goal_run_id"],
                        "version": cancelled["record"]["version"],
                        "fencing_token": cancellable["fencing_token"],
                    },
                )
        assert sqlstate(cancelled_resume.value) == "23514"

        reconciling = create_goal_run(
            connection,
            idempotency_key=f"goal.create.{uuid4().hex}",
        )
        reconciliation_stop = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_checkpoint(
                    :actor_id, :goal_run_id, :version, :fencing_token,
                    'reconciliation', 'reconciliation_required',
                    'reconciliation_required',
                    jsonb_build_object('ambiguous_effect', true),
                    'AMBIGUOUS_EFFECT', 'goal.checkpoint.reconciliation-stop',
                    'trace-reconciliation-stop'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": reconciling["goal_run_id"],
                "version": reconciling["version"],
                "fencing_token": reconciling["fencing_token"],
            },
        )
        assert reconciliation_stop["record"]["status"] == "reconciliation_required"
        with pytest.raises(DBAPIError) as reconciliation_resume:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_resume(
                            :actor_id, :goal_run_id, :version, :fencing_token,
                            'goal.resume.reconciliation', 'trace-resume-reconciliation'
                        )
                        """
                    ),
                    {
                        "actor_id": ACTOR_ID,
                        "goal_run_id": reconciling["goal_run_id"],
                        "version": reconciliation_stop["record"]["version"],
                        "fencing_token": reconciling["fencing_token"],
                    },
                )
        assert sqlstate(reconciliation_resume.value) == "23514"
        with pytest.raises(DBAPIError) as reconciliation_update:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        UPDATE careerops.goal_runs
                        SET status = 'running', phase = 'reconciliation',
                            version = version + 1,
                            reconciliation_required_at = NULL
                        WHERE id = :goal_run_id
                        """
                    ),
                    {"goal_run_id": reconciling["goal_run_id"]},
                )
        assert sqlstate(reconciliation_update.value) == "23514"


def test_goal_run_append_only_tables_and_role_grants(engine: Engine) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        record = create_goal_run(connection, idempotency_key=f"goal.create.{uuid4().hex}")
        goal_run_id = record["goal_run_id"]
        with pytest.raises(DBAPIError) as append_only:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        UPDATE careerops.goal_run_events
                        SET event_json = event_json
                        WHERE goal_run_id = :goal_run_id
                        """
                    ),
                    {"goal_run_id": goal_run_id},
                )
        assert sqlstate(append_only.value) == "55000"

    if role_exists(engine, "careerops_api"):
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            assert connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, "
                    "'careerops.goal_run_create(uuid, text, text, text, jsonb, uuid, text, integer, text, text)', "
                    "'EXECUTE')"
                )
            )
            assert not connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, "
                    "'careerops.goal_run_checkpoint(uuid, uuid, bigint, uuid, text, text, text, jsonb, text, text, text)', "
                    "'EXECUTE')"
                )
            )
            assert not connection.scalar(
                sa.text("SELECT has_table_privilege(current_user, 'careerops.goal_runs', 'INSERT')")
            )

    if role_exists(engine, "careerops_workflow"):
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_workflow"))
            for signature in (
                "careerops.claim_due_crawler_source(uuid, text, uuid, integer)",
                "careerops.claim_crawler_source(uuid, text, text, uuid, integer)",
                "careerops.complete_crawler_source_run(uuid, text, uuid, text, text, text)",
                "careerops.fail_crawler_source_run(uuid, text, uuid, text)",
                "careerops.goal_run_reviewed_crawler_result(uuid, uuid, uuid, text)",
            ):
                assert connection.scalar(
                    sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                    {"signature": signature},
                )
            assert connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, "
                    "'careerops.goal_run_checkpoint(uuid, uuid, bigint, uuid, text, text, text, jsonb, text, text, text)', "
                    "'EXECUTE')"
                )
            )
            assert connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, "
                    "'careerops.goal_run_reviewed_crawler_result(uuid, uuid, uuid, text)', "
                    "'EXECUTE')"
                )
            )
            assert not connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(current_user, "
                    "'careerops.goal_run_record_review_decision(uuid, uuid, bigint, uuid, uuid, text, text, text, text, text)', "
                    "'EXECUTE')"
                )
            )
            for signature in (
                "careerops.goal_run_create(uuid, text, text, text, jsonb, uuid, text, integer, text, text)",
                "careerops.goal_run_cancel(uuid, uuid, bigint, uuid, text, text, text)",
                "careerops.goal_run_resume(uuid, uuid, bigint, uuid, text, text)",
            ):
                assert not connection.scalar(
                    sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                    {"signature": signature},
                )
            assert connection.scalar(
                sa.text(
                    "SELECT has_table_privilege(current_user, "
                    "'careerops.crawler_job_ingestion_evidence', 'INSERT')"
                )
            )
            for table_name in (
                "careerops.job_merge_decisions",
                "careerops.job_posting_assignments",
            ):
                assert connection.scalar(
                    sa.text("SELECT has_table_privilege(current_user, :table_name, 'INSERT')"),
                    {"table_name": table_name},
                )
            for table_name in (
                "careerops.content_blobs",
                "careerops.content_objects",
                "careerops.job_aliases",
                "careerops.evidence_records",
            ):
                assert not connection.scalar(
                    sa.text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
                    {"table_name": table_name},
                )
                assert not connection.scalar(
                    sa.text("SELECT has_table_privilege(current_user, :table_name, 'INSERT')"),
                    {"table_name": table_name},
                )
            for table_name, column_name in (
                ("careerops.job_sources", "last_discovery_at"),
                ("careerops.job_sources", "updated_at"),
                ("careerops.canonical_jobs", "primary_posting_id"),
                ("careerops.canonical_jobs", "updated_at"),
                ("careerops.job_postings", "source_state"),
                ("careerops.job_postings", "last_seen_at"),
                ("careerops.job_postings", "closed_at"),
                ("careerops.job_postings", "updated_at"),
            ):
                assert connection.scalar(
                    sa.text(
                        "SELECT has_column_privilege("
                        "current_user, :table_name, :column_name, 'UPDATE')"
                    ),
                    {"table_name": table_name, "column_name": column_name},
                )
            for table_name, column_name in (
                ("careerops.companies", "name"),
                ("careerops.companies", "official_domains"),
                ("careerops.job_sources", "state"),
                ("careerops.job_sources", "verified_at"),
                ("careerops.canonical_jobs", "aggregate_state"),
                ("careerops.job_posting_assignments", "canonical_job_id"),
                ("careerops.job_posting_assignments", "decision_id"),
                ("careerops.job_posting_assignments", "assigned_at"),
            ):
                assert not connection.scalar(
                    sa.text(
                        "SELECT has_column_privilege("
                        "current_user, :table_name, :column_name, 'UPDATE')"
                    ),
                    {"table_name": table_name, "column_name": column_name},
                )
            assert not connection.scalar(
                sa.text("SELECT has_table_privilege(current_user, 'careerops.goal_runs', 'INSERT')")
            )

    if role_exists(engine, "careerops_readonly"):
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_readonly"))
            assert connection.scalar(
                sa.text("SELECT has_table_privilege(current_user, 'careerops.goal_runs', 'SELECT')")
            )
            assert not connection.scalar(
                sa.text(
                    "SELECT has_table_privilege(current_user, "
                    "'careerops.goal_run_command_receipts', 'SELECT')"
                )
            )


def test_goal_run_reviewed_crawler_result_bridge_states(engine: Engine) -> None:
    with engine.begin() as connection:
        seed_actor(connection)
        registry_id = seed_registry_source(connection)
        record = create_goal_run(
            connection,
            idempotency_key=f"goal.create.{uuid4().hex}",
            registry_id=registry_id,
        )
        goal_run_id = record["goal_run_id"]

        waiting = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert waiting["state"] == "waiting_review"
        assert waiting["registry_id"] == str(registry_id)
        assert waiting["source_row_id"]
        assert waiting["source_id"] == "greenhouse"
        assert waiting["command_sha256"] == "3" * 64
        assert waiting["source_sha256"] == "4" * 64
        assert waiting["output_dir"] == "outputs/greenhouse"
        assert waiting["adapter"] == "recruitment.public_ats_feed"

        auto_selected_record = create_goal_run(
            connection,
            idempotency_key=f"goal.create.{uuid4().hex}",
            registry_id=registry_id,
            source_id=None,
        )
        auto_selected_waiting = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {
                "actor_id": ACTOR_ID,
                "goal_run_id": auto_selected_record["goal_run_id"],
                "registry_id": registry_id,
            },
        )
        assert auto_selected_waiting["state"] == "waiting_review"

        with pytest.raises(DBAPIError) as wrong_owner:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_reviewed_crawler_result(
                            :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                        )
                        """
                    ),
                    {
                        "actor_id": OTHER_ACTOR_ID,
                        "goal_run_id": goal_run_id,
                        "registry_id": registry_id,
                    },
                )
        assert sqlstate(wrong_owner.value) == "23503"

        with pytest.raises(DBAPIError) as wrong_source:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        """
                        SELECT careerops.goal_run_reviewed_crawler_result(
                            :actor_id, :goal_run_id, :registry_id, 'lever'
                        )
                        """
                    ),
                    {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
                )
        assert sqlstate(wrong_source.value) == "23503"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="succeeded",
            approved=True,
            manifest_sha256="9" * 64,
            request_index=1,
        )
        wrong_manifest = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert wrong_manifest["state"] == "waiting_review"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="succeeded",
            approved=False,
            request_index=2,
        )
        unapproved = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert unapproved["state"] == "waiting_review"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="failed",
            request_index=3,
        )
        failed = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert failed["state"] == "failed"
        assert failed["error_code"] == "CRAWLER_ERROR"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="reconciliation_required",
            request_index=4,
        )
        reconciliation = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert reconciliation["state"] == "reconciliation_required"

        seeded = seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="succeeded",
            request_index=5,
        )
        ready = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert ready["state"] == "ready"
        assert ready["request_id"] == str(seeded["request_id"])
        assert ready["outbox_event_id"] == str(seeded["outbox_event_id"])
        assert ready["outcome"] == "succeeded"
        assert ready["reviewed_plan_sha256"] == "6" * 64
        assert ready["request_sha256"] == "5" * 64

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome=None,
            approved=False,
            decision="rejected",
            request_index=6,
        )
        rejected_latest = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert rejected_latest["state"] == "failed"
        assert rejected_latest["error_code"] == "CRAWLER_EXECUTION_REJECTED"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome=None,
            approved=False,
            request_index=7,
        )
        pending_latest = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert pending_latest["state"] == "waiting_review"

        seed_reviewed_crawler_execution(
            connection,
            registry_id=registry_id,
            outcome="succeeded",
            approved=True,
            expired=True,
            request_index=8,
        )
        expired_latest = connection.scalar(
            sa.text(
                """
                SELECT careerops.goal_run_reviewed_crawler_result(
                    :actor_id, :goal_run_id, :registry_id, 'greenhouse'
                )
                """
            ),
            {"actor_id": ACTOR_ID, "goal_run_id": goal_run_id, "registry_id": registry_id},
        )
        assert expired_latest["state"] == "failed"
        assert expired_latest["error_code"] == "CRAWLER_EXECUTION_REQUEST_EXPIRED"
