from __future__ import annotations

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

from careerops.infrastructure.database.release_evidence import PostgresReleaseEvidenceReader

pytestmark = pytest.mark.integration

_DATABASE_PREFIX = "careerops_test_"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(_DATABASE_PREFIX):
        pytest.skip(f"release qualification tests require {_DATABASE_PREFIX}* database")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


def test_api_appends_independently_reviewed_qualification_and_readonly_drills_down(
    engine: Engine,
) -> None:
    operator_id = _create_operator(engine, "accepted")
    runner_actor = "g006-runner-accepted"
    reviewer_actor = "g006-reviewer-accepted"
    qualification_id = uuid4()
    _insert_qualification(engine, qualification_id, operator_id)
    evidence_id = _insert_evidence(engine, qualification_id, operator_id, runner_actor)
    _insert_decision(
        engine,
        qualification_id,
        from_status="draft",
        to_status="evaluating",
        role="runner",
        actor_id=runner_actor,
    )
    _insert_decision(
        engine,
        qualification_id,
        from_status="evaluating",
        to_status="pending_independent_review",
        role="reviewer",
        actor_id=reviewer_actor,
        evidence_ids=(evidence_id,),
    )
    _insert_decision(
        engine,
        qualification_id,
        from_status="pending_independent_review",
        to_status="qualified",
        role="operator",
        actor_id=f"operator:{operator_id}",
        decided_by_user_id=operator_id,
        evidence_ids=(evidence_id,),
    )

    with engine.connect() as connection:
        connection.execute(sa.text("SET ROLE careerops_readonly"))
        drilldown = PostgresReleaseEvidenceReader(connection).show(qualification_id)
        assert drilldown is not None
        assert [item.sequence for item in drilldown.decisions] == sorted(
            item.sequence for item in drilldown.decisions
        )
        assert [item.to_status for item in drilldown.decisions] == [
            "evaluating",
            "pending_independent_review",
            "qualified",
        ]
        assert drilldown.decisions[1].evidence_ids == (evidence_id,)
        assert drilldown.decisions[2].evidence_ids == (evidence_id,)
        assert drilldown.effective_lifecycle_status(now=datetime.now(UTC)) == "qualified"
        payload = drilldown.to_json(now=datetime.now(UTC))
        effective_state = payload["effective_state"]
        assert isinstance(effective_state, dict)
        assert effective_state["external_write_authorized"] is False
        connection.execute(sa.text("RESET ROLE"))

    with (
        pytest.raises(DBAPIError) as append_only_error,
        engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE careerops.release_qualifications "
                "SET created_by = 'tampered' WHERE id = :qualification_id"
            ),
            {"qualification_id": qualification_id},
        )
    assert _sqlstate(append_only_error.value) == "55000"


def test_decision_guard_rejects_self_review_and_outbox_has_no_release_authority(
    engine: Engine,
) -> None:
    operator_id = _create_operator(engine, "rejected")
    runner_actor = "g006-runner-rejected"
    reviewer_actor = "g006-reviewer-rejected"
    qualification_id = uuid4()
    _insert_qualification(engine, qualification_id, operator_id)
    evidence_id = _insert_evidence(engine, qualification_id, operator_id, runner_actor)
    _insert_decision(
        engine,
        qualification_id,
        from_status="draft",
        to_status="evaluating",
        role="runner",
        actor_id=runner_actor,
    )
    second_evidence_id = _insert_evidence(
        engine,
        qualification_id,
        operator_id,
        "g006-late-evidence",
        artifact_sha256="9" * 64,
    )
    review_evidence_ids = tuple(sorted((evidence_id, second_evidence_id), key=str))

    with pytest.raises(DBAPIError) as incomplete_review_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="evaluating",
            to_status="pending_independent_review",
            role="reviewer",
            actor_id=reviewer_actor,
            evidence_ids=(evidence_id,),
        )
    assert _sqlstate(incomplete_review_error.value) == "23514"

    with pytest.raises(DBAPIError) as runner_review_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="evaluating",
            to_status="pending_independent_review",
            role="reviewer",
            actor_id=runner_actor,
            evidence_ids=review_evidence_ids,
        )
    assert _sqlstate(runner_review_error.value) == "23514"

    _insert_decision(
        engine,
        qualification_id,
        from_status="evaluating",
        to_status="pending_independent_review",
        role="reviewer",
        actor_id=reviewer_actor,
        evidence_ids=review_evidence_ids,
    )

    with pytest.raises(DBAPIError) as frozen_evidence_error:
        _insert_evidence(engine, qualification_id, operator_id, "late-runner")
    assert _sqlstate(frozen_evidence_error.value) == "55000"

    with pytest.raises(DBAPIError) as stale_transition_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="evaluating",
            to_status="pending_independent_review",
            role="reviewer",
            actor_id="second-reviewer",
            evidence_ids=(evidence_id,),
        )
    assert _sqlstate(stale_transition_error.value) == "23514"

    with pytest.raises(DBAPIError) as unreviewed_evidence_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="pending_independent_review",
            to_status="qualified",
            role="operator",
            actor_id=f"operator:{operator_id}",
            decided_by_user_id=operator_id,
            evidence_ids=(uuid4(),),
        )
    assert _sqlstate(unreviewed_evidence_error.value) == "23514"

    with pytest.raises(DBAPIError) as self_review_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="pending_independent_review",
            to_status="qualified",
            role="operator",
            actor_id=reviewer_actor,
            decided_by_user_id=operator_id,
            evidence_ids=review_evidence_ids,
        )
    assert _sqlstate(self_review_error.value) == "23514"

    with (
        pytest.raises(DBAPIError) as outbox_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_outbox"))
        connection.execute(sa.text("SELECT count(*) FROM careerops.release_qualifications"))
    assert _sqlstate(outbox_error.value) == "42501"

    with (
        pytest.raises(DBAPIError) as api_update_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                "UPDATE careerops.release_qualification_decisions "
                "SET reason = 'tampered' WHERE qualification_id = :qualification_id"
            ),
            {"qualification_id": qualification_id},
        )
    assert _sqlstate(api_update_error.value) == "42501"

    with engine.connect() as connection:
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_readonly', "
                "'careerops.release_qualification_decisions', "
                "'INSERT,UPDATE,DELETE')"
            )
        )


def test_decision_guard_rejects_fail_open_metrics_after_independent_review(
    engine: Engine,
) -> None:
    operator_id = _create_operator(engine, "fail-open")
    runner_actor = "g006-runner-fail-open"
    reviewer_actor = "g006-reviewer-fail-open"
    qualification_id = uuid4()
    _insert_qualification(engine, qualification_id, operator_id)
    evidence_id = _insert_evidence(
        engine,
        qualification_id,
        operator_id,
        runner_actor,
        metrics=(
            '{"total_observations":50,"false_negative":1,'
            '"autonomous_provider_write_attempts":1,"no_autonomous_writes":false}'
        ),
    )
    _insert_decision(
        engine,
        qualification_id,
        from_status="draft",
        to_status="evaluating",
        role="runner",
        actor_id=runner_actor,
    )
    _insert_decision(
        engine,
        qualification_id,
        from_status="evaluating",
        to_status="pending_independent_review",
        role="reviewer",
        actor_id=reviewer_actor,
        evidence_ids=(evidence_id,),
    )

    with pytest.raises(DBAPIError) as fail_open_error:
        _insert_decision(
            engine,
            qualification_id,
            from_status="pending_independent_review",
            to_status="qualified",
            role="operator",
            actor_id=f"operator:{operator_id}",
            decided_by_user_id=operator_id,
            evidence_ids=(evidence_id,),
        )
    assert _sqlstate(fail_open_error.value) == "23514"

    _insert_decision(
        engine,
        qualification_id,
        from_status="pending_independent_review",
        to_status="rejected",
        role="operator",
        actor_id=f"operator:{operator_id}",
        decided_by_user_id=operator_id,
        evidence_ids=(evidence_id,),
    )


def test_readonly_intent_trace_uses_kill_switch_state_as_of_decision_time(
    engine: Engine,
) -> None:
    operator_id = _create_operator(engine, "kill-switch-trace")
    intent_id = uuid4()
    before_event_id = uuid4()
    decision_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_kill_switch_events (
                    id, scope_type, active, reason, actor_user_id,
                    idempotency_key, trace_id, created_at
                ) VALUES (
                    :id, 'global', true, 'pre-decision stop', :operator_id,
                    :idempotency_key, :trace_id, :created_at
                )
                """
            ),
            {
                "id": before_event_id,
                "operator_id": operator_id,
                "idempotency_key": f"g006-before-{uuid4()}",
                "trace_id": f"g006-before-{uuid4()}",
                "created_at": decision_at - timedelta(seconds=1),
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.action_intents (
                    id, action_kind, resource_type, resource_id, idempotency_key,
                    status, created_by, created_at, updated_at
                ) VALUES (
                    :id, 'submit_application', 'job_posting', :resource_id,
                    :idempotency_key, 'proposed', 'g006-trace', :created_at, :created_at
                )
                """
            ),
            {
                "id": intent_id,
                "resource_id": uuid4(),
                "idempotency_key": f"g006-intent-{uuid4()}",
                "created_at": decision_at,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.autopilot_kill_switch_events (
                    id, scope_type, active, reason, actor_user_id,
                    idempotency_key, trace_id, created_at
                ) VALUES (
                    :id, 'global', false, 'post-decision resume', :operator_id,
                    :idempotency_key, :trace_id, :created_at
                )
                """
            ),
            {
                "id": uuid4(),
                "operator_id": operator_id,
                "idempotency_key": f"g006-after-{uuid4()}",
                "trace_id": f"g006-after-{uuid4()}",
                "created_at": decision_at + timedelta(seconds=1),
            },
        )

    with engine.connect() as connection:
        connection.execute(sa.text("SET ROLE careerops_readonly"))
        trace = PostgresReleaseEvidenceReader(connection).trace_intent(intent_id)
        connection.execute(sa.text("RESET ROLE"))

    assert trace is not None
    assert len(trace.kill_switch_events) == 1
    state = trace.kill_switch_events[0]
    assert state["id"] == str(before_event_id)
    assert state["active"] is True
    assert state["decision_anchor_at"] == decision_at.isoformat()
    payload = trace.to_json()
    assert payload["kill_switch_state_at_decision"] == payload["kill_switch_events"]


def _create_operator(engine: Engine, suffix: str) -> UUID:
    actor_id = uuid4()
    changed_at = datetime.now(UTC)
    with engine.begin() as connection:
        existing = connection.scalar(sa.text("SELECT id FROM careerops.console_users LIMIT 1"))
        if existing is not None:
            return UUID(str(existing))
        connection.execute(
            sa.text(
                "INSERT INTO careerops.console_users ("
                "id, username, password_hash, password_changed_at"
                ") VALUES (:id, :username, '$argon2id$release-test', :changed_at)"
            ),
            {
                "id": actor_id,
                "username": f"release-{suffix}-operator-{uuid4().hex}",
                "changed_at": changed_at,
            },
        )
    return actor_id


def _insert_qualification(engine: Engine, qualification_id: UUID, operator_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualifications (
                    id, capability, action_name, rollout_mode, provider, adapter_id,
                    adapter_version, implementation_hash, config_hash, policy_hash,
                    dataset_hash, git_commit, migration_revision, oauth_scope_hash,
                    credential_ref_hash, network_policy_hash, reconcile_policy_hash,
                    hard_stop_hash, sensitive_field_hash, kill_switch_hash,
                    fixture_manifest_sha256, fault_manifest_sha256, status,
                    requested_by_user_id, created_by, expires_at
                ) VALUES (
                    :id, 'synthetic_submission', 'submit_application', 'review_required',
                    'synthetic', 'greenhouse-sandbox', :adapter_version,
                    :hash_a, :hash_b, :hash_c, :hash_d, :git_commit, '0009',
                    :hash_e, :hash_f, :hash_0, :hash_1, :hash_2, :hash_3, :hash_4,
                    :hash_5, :hash_6, 'draft', :operator_id, 'g006-integration',
                    :expires_at
                )
                """
            ),
            {
                "id": qualification_id,
                "adapter_version": f"release-v1-{qualification_id.hex[:12]}",
                "hash_a": "a" * 64,
                "hash_b": "b" * 64,
                "hash_c": "c" * 64,
                "hash_d": "d" * 64,
                "git_commit": "e" * 40,
                "hash_e": "e" * 64,
                "hash_f": "f" * 64,
                "hash_0": "0" * 64,
                "hash_1": "1" * 64,
                "hash_2": "2" * 64,
                "hash_3": "3" * 64,
                "hash_4": "4" * 64,
                "hash_5": "5" * 64,
                "hash_6": "6" * 64,
                "operator_id": operator_id,
                "expires_at": datetime.now(UTC) + timedelta(days=1),
            },
        )


def _insert_evidence(
    engine: Engine,
    qualification_id: UUID,
    operator_id: UUID,
    runner_actor: str,
    *,
    artifact_sha256: str = "7" * 64,
    metrics: str = (
        '{"total_observations":50,"external_provider_calls":0,'
        '"false_negative":0,"autonomous_provider_write_attempts":0,'
        '"no_autonomous_writes":true}'
    ),
) -> UUID:
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualification_evidence (
                    id, qualification_id, evidence_kind, artifact_uri, artifact_sha256,
                    run_id, runner_user_id, runner_actor, metrics, sample_manifest_sha256
                ) VALUES (
                    :id, :qualification_id, 'metrics',
                    'datasets/derived/release/g006-metrics.json', :artifact_sha256,
                    :run_id, :operator_id, :runner_actor,
                    CAST(:metrics AS jsonb),
                    :sample_manifest_sha256
                )
                """
            ),
            {
                "id": evidence_id,
                "qualification_id": qualification_id,
                "artifact_sha256": artifact_sha256,
                "run_id": f"g006-{uuid4()}",
                "operator_id": operator_id,
                "runner_actor": runner_actor,
                "metrics": metrics,
                "sample_manifest_sha256": "8" * 64,
            },
        )
    return evidence_id


def _insert_decision(
    engine: Engine,
    qualification_id: UUID,
    *,
    from_status: str,
    to_status: str,
    role: str,
    actor_id: str,
    decided_by_user_id: UUID | None = None,
    evidence_ids: tuple[UUID, ...] = (),
) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualification_decisions (
                    id, qualification_id, from_status, to_status, decision_role,
                    actor_id, decided_by_user_id, reason, evidence_sha256,
                    evidence_ids
                ) VALUES (
                    :id, :qualification_id, :from_status, :to_status, :role,
                    :actor_id, :decided_by_user_id, :reason, :evidence_sha256,
                    CAST(:evidence_ids AS uuid[])
                )
                """
            ),
            {
                "id": uuid4(),
                "qualification_id": qualification_id,
                "from_status": from_status,
                "to_status": to_status,
                "role": role,
                "actor_id": actor_id,
                "decided_by_user_id": decided_by_user_id,
                "reason": f"g006 transition to {to_status}",
                "evidence_sha256": "9" * 64,
                "evidence_ids": list(evidence_ids),
            },
        )


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)
