from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

_DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"
_ACTOR_ID = UUID("10000000-0000-0000-0000-000000000021")
_OTHER_ACTOR_ID = UUID("10000000-0000-0000-0000-000000000022")
_SNAPSHOT_SHA256 = "2" * 64
_PRE_APPLICATION_REVIEW_KIND = "goal_run_pre_application_review.v1"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL GoalRun tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "PostgreSQL GoalRun tests require a disposable database named "
            f"{_DISPOSABLE_DATABASE_PREFIX}*"
        )
    if os.environ.get(_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT) != "1":
        pytest.skip(
            f"{_DESTRUCTIVE_DATABASE_ACKNOWLEDGEMENT}=1 is required because GoalRun tests "
            "downgrade the target database to base"
        )
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    result = sa.create_engine(database_url)
    yield result
    result.dispose()


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)


def _require_api_role(engine: Engine) -> None:
    with engine.connect() as connection:
        exists = connection.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api')")
        )
    if not exists:
        pytest.skip("careerops_api role is required for direct create boundary tests")


def _seed_actor(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_changed_at
            )
            VALUES (
                :actor_id, 'preapplication-runner', '$argon2id$test', :now
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"actor_id": _ACTOR_ID, "now": datetime(2026, 7, 22, tzinfo=UTC)},
    )


def _digest() -> str:
    return f"{uuid4().hex}{uuid4().hex}"


def _seed_candidate_profile_version(
    connection: sa.Connection,
    *,
    candidate_id: UUID,
    version: int,
    approve: bool,
    material_active: bool = True,
) -> dict[str, object]:
    profile_version_id = uuid4()
    material_bundle_id = uuid4()
    material_id = uuid4()
    content_object_id = uuid4()
    blob_id = uuid4()
    material_sha256 = _digest()
    material_bundle_sha256 = _digest()
    snapshot_sha256 = _digest()
    object_key = (
        f"sha256/{material_sha256[:2]}/{material_sha256[2:4]}/{material_sha256}"
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.candidates (id, owner_user_id, display_name)
            VALUES (:candidate_id, :actor_id, 'Preapplication Candidate')
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"candidate_id": candidate_id, "actor_id": _ACTOR_ID},
    )
    parameters = {
        "actor_id": _ACTOR_ID,
        "candidate_id": candidate_id,
        "version": version,
        "profile_version_id": profile_version_id,
        "material_bundle_id": material_bundle_id,
        "material_id": material_id,
        "content_object_id": content_object_id,
        "blob_id": blob_id,
        "material_sha256": material_sha256,
        "material_bundle_sha256": material_bundle_sha256,
        "snapshot_sha256": snapshot_sha256,
        "object_key": object_key,
        "request_sha256": _digest(),
        "import_idempotency_key": f"profile.import.{uuid4().hex}",
        "trace_id": f"trace-profile-import-{uuid4().hex}",
        "retention_until": datetime.now(UTC)
        + (timedelta(days=7) if material_active else -timedelta(days=1)),
    }
    statements = (
        """
        INSERT INTO careerops.content_blobs (id, sha256, object_key, byte_size)
        VALUES (:blob_id, :material_sha256, :object_key, 1024)
        """,
        """
        INSERT INTO careerops.content_objects (
            id, blob_id, media_type, classification, owner_resource_type,
            owner_resource_id, retention_until
        ) VALUES (
            :content_object_id, :blob_id, 'application/pdf',
            'accepted_attachment', 'candidate', :candidate_id,
            :retention_until
        )
        """,
        """
        INSERT INTO careerops.candidate_material_bundles (
            id, owner_user_id, candidate_id, version, schema_version,
            canonicalization_version, manifest_json, material_count, bundle_sha256
        ) VALUES (
            :material_bundle_id, :actor_id, :candidate_id, :version,
            'candidate-material-bundle.v1', 'careerops-json-c14n.v1',
            '[{"kind":"resume"}]'::jsonb, 1, :material_bundle_sha256
        )
        """,
        """
        INSERT INTO careerops.candidate_material_versions (
            id, bundle_id, owner_user_id, candidate_id, ordinal, material_kind,
            label, filename, media_type, content_object_id, sha256, object_key,
            byte_size
        ) VALUES (
            :material_id, :material_bundle_id, :actor_id, :candidate_id, 1,
            'resume', 'Resume', 'resume.pdf', 'application/pdf',
            :content_object_id, :material_sha256, :object_key, 1024
        )
        """,
        """
        INSERT INTO careerops.candidate_profile_versions (
            id, owner_user_id, candidate_id, version, profile_schema_version,
            preferences_schema_version, snapshot_schema_version,
            canonicalization_version, profile_json, preferences_json,
            material_bundle_id, material_bundle_sha256, snapshot_sha256,
            request_sha256, import_idempotency_key, trace_id
        ) VALUES (
            :profile_version_id, :actor_id, :candidate_id, :version,
            'candidate-profile.v1', 'candidate-job-preferences.v1',
            'candidate-profile-snapshot.v1', 'careerops-json-c14n.v1',
            '{"schema_version":"candidate-profile.v1"}'::jsonb,
            '{"schema_version":"candidate-job-preferences.v1"}'::jsonb,
            :material_bundle_id, :material_bundle_sha256, :snapshot_sha256,
            :request_sha256, :import_idempotency_key, :trace_id
        )
        """,
    )
    for statement in statements:
        connection.execute(sa.text(statement), parameters)
    profile = {
        "candidate_id": candidate_id,
        "profile_version_id": profile_version_id,
        "snapshot_sha256": snapshot_sha256,
        "material_bundle_sha256": material_bundle_sha256,
        "profile_version": version,
        "content_object_id": content_object_id,
    }
    if approve:
        _approve_candidate_profile(connection, profile=profile)
    return profile


def _approve_candidate_profile(
    connection: sa.Connection,
    *,
    profile: Mapping[str, object],
) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.candidate_profile_decide(
                :actor_id, :candidate_id, :profile_version_id, :snapshot_sha256,
                'approve', 'approved for preapplication tests',
                :idempotency_key, :trace_id
            )
            """
        ),
        {
            "actor_id": _ACTOR_ID,
            "candidate_id": profile["candidate_id"],
            "profile_version_id": profile["profile_version_id"],
            "snapshot_sha256": profile["snapshot_sha256"],
            "idempotency_key": f"profile.decision.{uuid4().hex}",
            "trace_id": f"trace-profile-decision-{uuid4().hex}",
        },
    )
    assert isinstance(result, dict)
    return result


def _insert_candidate_profile_approval_unchecked(
    connection: sa.Connection,
    *,
    profile: Mapping[str, object],
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.candidate_profile_decisions (
                id, profile_version_id, owner_user_id, candidate_id,
                reviewed_by_user_id, decision, reason, snapshot_sha256,
                request_sha256, idempotency_key, trace_id
            ) VALUES (
                :decision_id, :profile_version_id, :actor_id, :candidate_id,
                :actor_id, 'approve', 'seed inactive historical approval',
                :snapshot_sha256, :request_sha256, :idempotency_key, :trace_id
            )
            """
        ),
        {
            "decision_id": uuid4(),
            "profile_version_id": profile["profile_version_id"],
            "actor_id": _ACTOR_ID,
            "candidate_id": profile["candidate_id"],
            "snapshot_sha256": profile["snapshot_sha256"],
            "request_sha256": _digest(),
            "idempotency_key": f"profile.decision.{uuid4().hex}",
            "trace_id": f"trace-profile-decision-{uuid4().hex}",
        },
    )


def _preapplication_context(profile: Mapping[str, object]) -> dict[str, object]:
    candidate_id = str(profile["candidate_id"])
    snapshot_sha256 = str(profile["snapshot_sha256"])
    return {
        "mode": "pre_application_only",
        "candidate_id": candidate_id,
        "candidate_profile_version_id": str(profile["profile_version_id"]),
        "candidate_profile_snapshot_sha256": snapshot_sha256,
        "candidate_material_bundle_sha256": str(profile["material_bundle_sha256"]),
        "candidate_profile": {
            "candidate_id": candidate_id,
            "profile_version": (
                f"candidate-profile:{profile['profile_version']}:"
                f"{profile['profile_version_id']}"
            ),
            "source_sha256": snapshot_sha256,
        },
        "match_config": {
            "include_keywords": [],
            "exclude_keywords": [],
            "min_score": 0.5,
            "max_applications": 1,
        },
    }


def _direct_create_goal_run(
    connection: sa.Connection,
    *,
    context: Mapping[str, object],
    idempotency_key: str,
    workflow_id: str,
    actor_id: UUID = _ACTOR_ID,
) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_create(
                :actor_id, :idempotency_key, 'job-search',
                'Prepare a bounded pre-application package.', CAST(:context AS jsonb),
                NULL, 'greenhouse', 25, :workflow_id, :trace_id
            )
            """
        ),
        {
            "actor_id": actor_id,
            "idempotency_key": idempotency_key,
            "context": json.dumps(context),
            "workflow_id": workflow_id,
            "trace_id": f"trace-{workflow_id}",
        },
    )
    assert isinstance(result, dict)
    return result


def _assert_create_failed_atomically(
    connection: sa.Connection,
    *,
    actor_id: UUID,
    idempotency_key: str,
) -> None:
    assert connection.scalar(
        sa.text(
            "SELECT count(*) FROM careerops.goal_runs "
            "WHERE actor_user_id = :actor_id AND idempotency_key = :idempotency_key"
        ),
        {"actor_id": actor_id, "idempotency_key": idempotency_key},
    ) == 0
    assert connection.scalar(
        sa.text(
            "SELECT count(*) FROM careerops.goal_run_command_receipts "
            "WHERE actor_user_id = :actor_id AND command_kind = 'create' "
            "AND idempotency_key = :idempotency_key"
        ),
        {"actor_id": actor_id, "idempotency_key": idempotency_key},
    ) == 0


def _create_reviewable_goal_run(
    connection: sa.Connection,
    *,
    context: Mapping[str, object],
    review_kind: str,
) -> dict[str, object]:
    suffix = uuid4().hex
    created = _direct_create_goal_run(
        connection,
        context=context,
        idempotency_key=f"goal.create.{suffix}",
        workflow_id=f"goal-workflow-{suffix}",
    )
    record = created["record"]
    assert isinstance(record, dict)
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
                    jsonb_build_object('semantic_outcome', CAST(:outcome AS text)),
                    NULL, :idempotency_key, :trace_id
                )
                """
            ),
            {
                "actor_id": _ACTOR_ID,
                "goal_run_id": goal_run_id,
                "version": version,
                "fencing_token": fencing_token,
                "phase": phase,
                "outcome": f"step-{index}",
                "idempotency_key": f"goal.checkpoint.{suffix}.{index}",
                "trace_id": f"trace-checkpoint-{suffix}-{index}",
            },
        )
        assert isinstance(checkpoint, dict)
        checkpoint_record = checkpoint["record"]
        assert isinstance(checkpoint_record, dict)
        version = checkpoint_record["version"]

    review_item_id = uuid4()
    review = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_request_review(
                :actor_id, :goal_run_id, :version, :fencing_token,
                :review_item_id, :snapshot_sha256, :review_kind,
                jsonb_build_object('packages', 1, 'external_writes_enabled', false),
                :idempotency_key, :trace_id
            )
            """
        ),
        {
            "actor_id": _ACTOR_ID,
            "goal_run_id": goal_run_id,
            "version": version,
            "fencing_token": fencing_token,
            "review_item_id": review_item_id,
            "snapshot_sha256": _SNAPSHOT_SHA256,
            "review_kind": review_kind,
            "idempotency_key": f"goal.review.request.{suffix}",
            "trace_id": f"trace-review-{suffix}",
        },
    )
    assert isinstance(review, dict)
    review_record = review["record"]
    assert isinstance(review_record, dict)
    assert review_record["status"] == "waiting_review"
    return review_record


def _record_decision(
    connection: sa.Connection,
    *,
    record: Mapping[str, object],
    decision: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, object]:
    result = connection.scalar(
        sa.text(
            """
            SELECT careerops.goal_run_record_review_decision(
                :actor_id, :goal_run_id, :version, :fencing_token,
                :review_item_id, :snapshot_sha256, :decision,
                :reason, :idempotency_key, :trace_id
            )
            """
        ),
        {
            "actor_id": _ACTOR_ID,
            "goal_run_id": record["goal_run_id"],
            "version": record["version"],
            "fencing_token": record["fencing_token"],
            "review_item_id": record["review_item_id"],
            "snapshot_sha256": record["review_snapshot_sha256"],
            "decision": decision,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "trace_id": f"trace-decision-{uuid4().hex}",
        },
    )
    assert isinstance(result, dict)
    return result


def test_pre_application_approval_atomically_finalizes_without_dispatch(engine: Engine) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=True,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context={**_preapplication_context(profile), "target": "backend"},
            review_kind=_PRE_APPLICATION_REVIEW_KIND,
        )
        decision_key = f"goal.review.decision.{uuid4().hex}"
        approved = _record_decision(
            connection,
            record=waiting,
            decision="approve",
            reason="approved local package",
            idempotency_key=decision_key,
        )

        approved_record = approved["record"]
        assert isinstance(approved_record, dict)
        waiting_version = waiting["version"]
        assert isinstance(waiting_version, int)
        assert approved_record["status"] == "completed"
        assert approved_record["phase"] == "completed"
        assert approved_record["version"] == waiting_version + 1
        assert approved_record["completed_at"] is not None
        assert approved_record["checkpoint"] == {
            "completion_kind": "pre_application_package_approved",
            "mode": "pre_application_only",
            "review_decision": "approve",
            "review_item_id": str(waiting["review_item_id"]),
            "review_snapshot_sha256": _SNAPSHOT_SHA256,
        }

        checkpoint = connection.execute(
            sa.text(
                """
                SELECT run_version, phase, status, outcome, checkpoint_json,
                       checkpoint_sha256, idempotency_key
                FROM careerops.goal_run_checkpoints
                WHERE goal_run_id = :goal_run_id AND outcome = 'finalize'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ).mappings().one()
        assert checkpoint["run_version"] == approved_record["version"]
        assert checkpoint["phase"] == "completed"
        assert checkpoint["status"] == "completed"
        assert checkpoint["checkpoint_json"] == approved_record["checkpoint"]
        assert checkpoint["idempotency_key"].startswith("review-finalize:")
        assert len(checkpoint["idempotency_key"]) == 80
        assert connection.scalar(
            sa.text(
                "SELECT encode(public.digest(CAST(:checkpoint AS jsonb)::text, 'sha256'), 'hex')"
            ),
            {"checkpoint": json.dumps(checkpoint["checkpoint_json"])},
        ) == checkpoint["checkpoint_sha256"]

        event = connection.execute(
            sa.text(
                """
                SELECT to_status, to_phase, version, event_json
                FROM careerops.goal_run_events
                WHERE goal_run_id = :goal_run_id AND event_kind = 'review_decided'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ).mappings().one()
        assert event["to_status"] == "completed"
        assert event["to_phase"] == "completed"
        assert event["version"] == approved_record["version"]
        assert event["event_json"]["completion_kind"] == "pre_application_package_approved"

        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_review_decisions
                WHERE goal_run_id = :goal_run_id
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 1
        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_command_receipts
                WHERE goal_run_id = :goal_run_id
                  AND command_kind = 'record_review_decision'
                  AND idempotency_key = :idempotency_key
                """
            ),
            {
                "goal_run_id": waiting["goal_run_id"],
                "idempotency_key": decision_key,
            },
        ) == 1

        replayed = _record_decision(
            connection,
            record=waiting,
            decision="approve",
            reason="approved local package",
            idempotency_key=decision_key,
        )
        assert replayed["newly_created"] is False
        assert replayed["record"] == approved_record
        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_checkpoints
                WHERE goal_run_id = :goal_run_id AND outcome = 'finalize'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 1


@pytest.mark.parametrize(
    "context",
    (
        {"target": "backend"},
        {"mode": "gmail_dispatch", "target": "backend"},
    ),
)
def test_legacy_and_explicit_gmail_approval_still_enter_dispatch(
    engine: Engine,
    context: Mapping[str, object],
) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        waiting = _create_reviewable_goal_run(
            connection,
            context=context,
            review_kind="manual-approval",
        )
        approved = _record_decision(
            connection,
            record=waiting,
            decision="approve",
            reason="approved Gmail dispatch",
            idempotency_key=f"goal.review.decision.{uuid4().hex}",
        )
        record = approved["record"]
        assert isinstance(record, dict)
        assert record["status"] == "running"
        assert record["phase"] == "dispatch"
        assert record["completed_at"] is None
        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_checkpoints
                WHERE goal_run_id = :goal_run_id AND outcome = 'finalize'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 0
        event_json = connection.scalar(
            sa.text(
                """
                SELECT event_json FROM careerops.goal_run_events
                WHERE goal_run_id = :goal_run_id AND event_kind = 'review_decided'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        )
        assert event_json == {
            "decision": "approve",
            "review_item_id": str(waiting["review_item_id"]),
        }


def test_pre_application_approval_rejects_mismatched_review_kind_atomically(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=True,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context=_preapplication_context(profile),
            review_kind="manual-approval",
        )
        with pytest.raises(DBAPIError) as mismatch, connection.begin_nested():
            _record_decision(
                connection,
                record=waiting,
                decision="approve",
                reason="wrong review kind",
                idempotency_key=f"goal.review.decision.{uuid4().hex}",
            )
        assert _sqlstate(mismatch.value) == "23514"
        current = connection.scalar(
            sa.text(
                "SELECT careerops.goal_run_status(:actor_id, :goal_run_id)"
            ),
            {"actor_id": _ACTOR_ID, "goal_run_id": waiting["goal_run_id"]},
        )
        assert current["status"] == "waiting_review"
        assert current["phase"] == "review"
        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_review_decisions
                WHERE goal_run_id = :goal_run_id
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 0


def test_pre_application_rejection_preserves_legacy_terminal_semantics(engine: Engine) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=True,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context=_preapplication_context(profile),
            review_kind=_PRE_APPLICATION_REVIEW_KIND,
        )
        rejected = _record_decision(
            connection,
            record=waiting,
            decision="reject",
            reason="package needs revision",
            idempotency_key=f"goal.review.decision.{uuid4().hex}",
        )
        record = rejected["record"]
        assert isinstance(record, dict)
        assert record["status"] == "rejected"
        assert record["phase"] == "rejected"
        assert record["completed_at"] is None
        assert connection.scalar(
            sa.text(
                """
                SELECT count(*) FROM careerops.goal_run_checkpoints
                WHERE goal_run_id = :goal_run_id AND outcome = 'finalize'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 0
        event_json = connection.scalar(
            sa.text(
                """
                SELECT event_json FROM careerops.goal_run_events
                WHERE goal_run_id = :goal_run_id AND event_kind = 'review_decided'
                """
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        )
        assert event_json == {
            "decision": "reject",
            "review_item_id": str(waiting["review_item_id"]),
        }


def test_direct_final_approval_rejects_stale_profile_without_application_layer(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        candidate_id = uuid4()
        profile_v1 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=1,
            approve=True,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context=_preapplication_context(profile_v1),
            review_kind=_PRE_APPLICATION_REVIEW_KIND,
        )
        _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=2,
            approve=True,
        )

    with pytest.raises(DBAPIError) as stale, engine.begin() as connection:
        _record_decision(
            connection,
            record=waiting,
            decision="approve",
            reason="direct definer call must fail closed",
            idempotency_key=f"goal.review.decision.{uuid4().hex}",
        )
    assert _sqlstate(stale.value) == "23514"

    with engine.connect() as connection:
        current = connection.scalar(
            sa.text("SELECT careerops.goal_run_status(:actor_id, :goal_run_id)"),
            {"actor_id": _ACTOR_ID, "goal_run_id": waiting["goal_run_id"]},
        )
        assert current["status"] == "waiting_review"
        assert current["phase"] == "review"
        assert current["version"] == waiting["version"]
        assert connection.scalar(
            sa.text(
                "SELECT count(*) FROM careerops.goal_run_review_decisions "
                "WHERE goal_run_id = :goal_run_id"
            ),
            {"goal_run_id": waiting["goal_run_id"]},
        ) == 0


def test_profile_approval_and_final_approval_share_candidate_serialization_lock(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        candidate_id = uuid4()
        profile_v1 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=1,
            approve=True,
        )
        profile_v2 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=2,
            approve=False,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context=_preapplication_context(profile_v1),
            review_kind=_PRE_APPLICATION_REVIEW_KIND,
        )

    with engine.connect() as profile_connection, engine.connect() as final_connection:
        profile_transaction = profile_connection.begin()
        final_transaction = final_connection.begin()
        try:
            _approve_candidate_profile(profile_connection, profile=profile_v2)
            final_connection.execute(sa.text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(DBAPIError) as lock_timeout:
                _record_decision(
                    final_connection,
                    record=waiting,
                    decision="approve",
                    reason="must wait for concurrent profile approval",
                    idempotency_key=f"goal.review.decision.{uuid4().hex}",
                )
            assert _sqlstate(lock_timeout.value) == "55P03"
            final_transaction.rollback()
            profile_transaction.commit()
        finally:
            if final_transaction.is_active:
                final_transaction.rollback()
            if profile_transaction.is_active:
                profile_transaction.rollback()

    with pytest.raises(DBAPIError) as stale, engine.begin() as connection:
        _record_decision(
            connection,
            record=waiting,
            decision="approve",
            reason="V2 is now the latest approval",
            idempotency_key=f"goal.review.decision.{uuid4().hex}",
        )
    assert _sqlstate(stale.value) == "23514"


def test_reject_is_not_blocked_by_a_newer_approved_profile(engine: Engine) -> None:
    with engine.begin() as connection:
        _seed_actor(connection)
        candidate_id = uuid4()
        profile_v1 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=1,
            approve=True,
        )
        waiting = _create_reviewable_goal_run(
            connection,
            context=_preapplication_context(profile_v1),
            review_kind=_PRE_APPLICATION_REVIEW_KIND,
        )
        _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=2,
            approve=True,
        )
        rejected = _record_decision(
            connection,
            record=waiting,
            decision="reject",
            reason="stale package rejected without profile authorization",
            idempotency_key=f"goal.review.decision.{uuid4().hex}",
        )

        rejected_record = rejected["record"]
        assert isinstance(rejected_record, dict)
        assert rejected_record["status"] == "rejected"
        assert rejected_record["phase"] == "rejected"


def test_api_direct_create_accepts_valid_profile_and_replays_after_profile_drift(
    engine: Engine,
) -> None:
    _require_api_role(engine)
    with engine.begin() as connection:
        _seed_actor(connection)
        candidate_id = uuid4()
        profile_v1 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=1,
            approve=True,
        )
    context = _preapplication_context(profile_v1)
    suffix = uuid4().hex
    idempotency_key = f"goal.create.{suffix}"
    workflow_id = f"goal-workflow-{suffix}"

    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        created = _direct_create_goal_run(
            connection,
            context=context,
            idempotency_key=idempotency_key,
            workflow_id=workflow_id,
        )
        assert created["newly_created"] is True
        created_record = created["record"]
        assert isinstance(created_record, dict)
        assert created_record["status"] == "starting"

    with engine.begin() as connection:
        _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=2,
            approve=True,
        )

    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        replayed = _direct_create_goal_run(
            connection,
            context=context,
            idempotency_key=idempotency_key,
            workflow_id=workflow_id,
        )
        assert replayed["newly_created"] is False
        replayed_record = replayed["record"]
        assert isinstance(replayed_record, dict)
        assert replayed_record["goal_run_id"] == created_record["goal_run_id"]

    stale_key = f"goal.create.{uuid4().hex}"
    with pytest.raises(DBAPIError) as stale, engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        _direct_create_goal_run(
            connection,
            context=context,
            idempotency_key=stale_key,
            workflow_id=f"goal-workflow-{uuid4().hex}",
        )
    assert _sqlstate(stale.value) == "23514"
    with engine.connect() as connection:
        _assert_create_failed_atomically(
            connection,
            actor_id=_ACTOR_ID,
            idempotency_key=stale_key,
        )


def test_api_direct_create_rejects_invalid_profile_bindings_atomically(
    engine: Engine,
) -> None:
    _require_api_role(engine)
    with engine.begin() as connection:
        _seed_actor(connection)
        valid_profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=True,
        )
        unapproved_profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=False,
        )
        inactive_profile = _seed_candidate_profile_version(
            connection,
            candidate_id=uuid4(),
            version=1,
            approve=False,
            material_active=False,
        )
        _insert_candidate_profile_approval_unchecked(
            connection,
            profile=inactive_profile,
        )

    valid_context = _preapplication_context(valid_profile)
    snapshot_mismatch = json.loads(json.dumps(valid_context))
    snapshot_mismatch["candidate_profile_snapshot_sha256"] = "a" * 64
    snapshot_mismatch["candidate_profile"]["source_sha256"] = "a" * 64
    bundle_mismatch = json.loads(json.dumps(valid_context))
    bundle_mismatch["candidate_material_bundle_sha256"] = "b" * 64
    version_mismatch = json.loads(json.dumps(valid_context))
    version_mismatch["candidate_profile"]["profile_version"] = (
        f"candidate-profile:999:{valid_profile['profile_version_id']}"
    )
    missing_match_config = json.loads(json.dumps(valid_context))
    del missing_match_config["match_config"]
    gmail_mixed = json.loads(json.dumps(valid_context))
    gmail_mixed["gmail_dispatch"] = {"recipient": "unsafe@example.com"}
    cases = (
        ("unknown-mode", {"mode": "unsupported"}, _ACTOR_ID, "23514"),
        ("missing-profile", {"mode": "pre_application_only"}, _ACTOR_ID, "23514"),
        ("unapproved", _preapplication_context(unapproved_profile), _ACTOR_ID, "23514"),
        ("inactive", _preapplication_context(inactive_profile), _ACTOR_ID, "23514"),
        ("snapshot", snapshot_mismatch, _ACTOR_ID, "23514"),
        ("bundle", bundle_mismatch, _ACTOR_ID, "23514"),
        ("profile-version", version_mismatch, _ACTOR_ID, "23514"),
        ("match-config", missing_match_config, _ACTOR_ID, "23514"),
        ("gmail-mixed", gmail_mixed, _ACTOR_ID, "23514"),
        ("cross-owner", valid_context, _OTHER_ACTOR_ID, "23503"),
    )
    for label, context, actor_id, expected_sqlstate in cases:
        idempotency_key = f"goal.create.invalid.{label}.{uuid4().hex}"
        with pytest.raises(DBAPIError) as rejected, engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            _direct_create_goal_run(
                connection,
                context=context,
                idempotency_key=idempotency_key,
                workflow_id=f"goal-workflow-{uuid4().hex}",
                actor_id=actor_id,
            )
        assert _sqlstate(rejected.value) == expected_sqlstate
        with engine.connect() as connection:
            _assert_create_failed_atomically(
                connection,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )


@pytest.mark.parametrize(
    "context",
    (
        {"target": "backend"},
        {"mode": "gmail_dispatch", "target": "backend"},
    ),
)
def test_api_direct_create_preserves_legacy_and_gmail_modes(
    engine: Engine,
    context: Mapping[str, object],
) -> None:
    _require_api_role(engine)
    with engine.begin() as connection:
        _seed_actor(connection)
    suffix = uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        created = _direct_create_goal_run(
            connection,
            context=context,
            idempotency_key=f"goal.create.{suffix}",
            workflow_id=f"goal-workflow-{suffix}",
        )
        assert created["newly_created"] is True


def test_direct_create_serializes_with_concurrent_profile_approval(engine: Engine) -> None:
    _require_api_role(engine)
    with engine.begin() as connection:
        _seed_actor(connection)
        candidate_id = uuid4()
        profile_v1 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=1,
            approve=True,
        )
        profile_v2 = _seed_candidate_profile_version(
            connection,
            candidate_id=candidate_id,
            version=2,
            approve=False,
        )
    idempotency_key = f"goal.create.{uuid4().hex}"

    with engine.connect() as profile_connection, engine.connect() as create_connection:
        profile_transaction = profile_connection.begin()
        create_transaction = create_connection.begin()
        try:
            profile_connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            _approve_candidate_profile(profile_connection, profile=profile_v2)
            create_connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            create_connection.execute(sa.text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(DBAPIError) as lock_timeout:
                _direct_create_goal_run(
                    create_connection,
                    context=_preapplication_context(profile_v1),
                    idempotency_key=idempotency_key,
                    workflow_id=f"goal-workflow-{uuid4().hex}",
                )
            assert _sqlstate(lock_timeout.value) == "55P03"
            create_transaction.rollback()
            profile_transaction.commit()
        finally:
            if create_transaction.is_active:
                create_transaction.rollback()
            if profile_transaction.is_active:
                profile_transaction.rollback()

    with pytest.raises(DBAPIError) as stale, engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        _direct_create_goal_run(
            connection,
            context=_preapplication_context(profile_v1),
            idempotency_key=idempotency_key,
            workflow_id=f"goal-workflow-{uuid4().hex}",
        )
    assert _sqlstate(stale.value) == "23514"
    with engine.connect() as connection:
        _assert_create_failed_atomically(
            connection,
            actor_id=_ACTOR_ID,
            idempotency_key=idempotency_key,
        )


def test_0024_downgrade_restores_0023_create_behavior(
    engine: Engine,
    database_url: str,
) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    invalid_context = {"mode": "pre_application_only"}
    with engine.begin() as connection:
        _seed_actor(connection)
    with pytest.raises(DBAPIError) as guarded, engine.begin() as connection:
        _direct_create_goal_run(
            connection,
            context=invalid_context,
            idempotency_key=f"goal.create.{uuid4().hex}",
            workflow_id=f"goal-workflow-{uuid4().hex}",
        )
    assert _sqlstate(guarded.value) == "23514"

    command.downgrade(config, "0023")
    try:
        with engine.begin() as connection:
            assert not connection.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_goal_runs_preapplication_create')"
                )
            )
            assert connection.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_goal_run_review_decisions_profile_approval')"
                )
            )
            unguarded = _direct_create_goal_run(
                connection,
                context=invalid_context,
                idempotency_key=f"goal.create.{uuid4().hex}",
                workflow_id=f"goal-workflow-{uuid4().hex}",
            )
            assert unguarded["newly_created"] is True
    finally:
        command.upgrade(config, "head")

    with pytest.raises(DBAPIError) as restored, engine.begin() as connection:
        _direct_create_goal_run(
            connection,
            context=invalid_context,
            idempotency_key=f"goal.create.{uuid4().hex}",
            workflow_id=f"goal-workflow-{uuid4().hex}",
        )
    assert _sqlstate(restored.value) == "23514"
