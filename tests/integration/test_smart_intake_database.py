from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for smart intake database tests")
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    yield database_engine
    database_engine.dispose()


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _preview_values(
    candidate_id: object, preview_id: object, *, now: datetime
) -> dict[str, object]:
    return {
        "id": preview_id,
        "candidate_id": candidate_id,
        "target": "profile",
        "idempotency_key": "same-key",
        "request_fingerprint": "a" * 64,
        "input_digest": "b" * 64,
        "context_digest": "c" * 64,
        "input_text": "candidate text",
        "context_refs": {"_model_version": "disabled"},
        "fields": [{"path": "roles[0].title", "value": "Backend Engineer"}],
        "state": "ready",
        "claim_state": "finalized",
        "claim_token": "",
        "schema_version": "smart-intake-v1",
        "prompt_version": "none",
        "model_id": "disabled",
        "capability_state": "disabled",
        "trace_id": "smart-intake-db-test",
        "created_at": now,
        "expires_at": now - timedelta(hours=25),
    }


_INSERT_PREVIEW = sa.text(
    """
    INSERT INTO careerops.smart_intake_previews (
        id, candidate_id, target, idempotency_key, request_fingerprint,
        input_digest, context_digest, input_text, context_refs, fields,
        state, claim_state, claim_token, schema_version, prompt_version,
        model_id, capability_state, trace_id, created_at, expires_at
    ) VALUES (
        :id, :candidate_id, :target, :idempotency_key, :request_fingerprint,
        :input_digest, :context_digest, :input_text, CAST(:context_refs AS jsonb),
        CAST(:fields AS jsonb), :state, :claim_state, :claim_token,
        :schema_version, :prompt_version, :model_id, :capability_state,
        :trace_id, :created_at, :expires_at
    )
    """
)


def _insert_preview(
    engine: Engine,
    candidate_id: object,
    preview_id: object,
    *,
    now: datetime,
    fields: str = "[]",
) -> None:
    values = _preview_values(candidate_id, preview_id, now=now)
    values["context_refs"] = '{"_model_version":"disabled"}'
    values["fields"] = fields
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(_INSERT_PREVIEW, values)


def test_candidate_scoping_idempotency_and_retention_purge(engine: Engine) -> None:
    candidate_a = uuid4()
    candidate_b = uuid4()
    preview_a = uuid4()
    preview_b = uuid4()
    now = datetime.now(UTC)

    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO careerops.candidates (id, display_name) "
                    "VALUES (:id, :display_name)"
                ),
                [
                    {"id": candidate_a, "display_name": "smart-intake-test-a"},
                    {"id": candidate_b, "display_name": "smart-intake-test-b"},
                ],
            )

        _insert_preview(
            engine,
            candidate_a,
            preview_a,
            now=now,
            fields='[{"path":"roles[0].title","value":"Backend Engineer"}]',
        )

        with pytest.raises(DBAPIError) as duplicate_error:
            _insert_preview(engine, candidate_a, uuid4(), now=now)
        assert _sqlstate(duplicate_error.value) == "23505"

        _insert_preview(engine, candidate_b, preview_b, now=now)

        with pytest.raises(DBAPIError) as cross_candidate_error, engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            connection.execute(
                sa.text(
                    """
                    INSERT INTO careerops.smart_intake_decisions (
                        id, preview_id, candidate_id, apply_idempotency_key,
                        decision_set_hash, decisions, draft_patch, actor_id, trace_id
                    ) VALUES (
                        :id, :preview_id, :candidate_id, 'apply-key', :decision_set_hash,
                        '[]'::jsonb, '{}'::jsonb, 'candidate-b', 'smart-intake-db-test'
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "preview_id": preview_a,
                    "candidate_id": candidate_b,
                    "decision_set_hash": "d" * 64,
                },
            )
        assert _sqlstate(cross_candidate_error.value) == "23503"

        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
            purged = connection.scalar(
                sa.text("SELECT careerops.purge_smart_intake_previews(CAST(:now AS timestamptz))"),
                {"now": now},
            )
            assert purged == 2

        with engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT input_text, context_refs, fields, state, purged_at "
                        "FROM careerops.smart_intake_previews "
                        "WHERE id IN (:preview_a, :preview_b) ORDER BY id"
                    ),
                    {"preview_a": preview_a, "preview_b": preview_b},
                )
                .mappings()
                .all()
            )
            assert len(rows) == 2
            assert all(row["input_text"] == "" for row in rows)
            assert all(row["context_refs"] == {} for row in rows)
            assert all(row["fields"] == [] for row in rows)
            assert all(row["state"] == "expired" for row in rows)
            assert all(row["purged_at"] is not None for row in rows)

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO careerops.smart_intake_decisions (
                            id, preview_id, candidate_id, apply_idempotency_key,
                            decision_set_hash, decisions, draft_patch, actor_id, trace_id
                        ) VALUES (
                            :id, :preview_id, :candidate_id, 'append-only-test', :decision_set_hash,
                            '[]'::jsonb, '{}'::jsonb, 'test', 'smart-intake-db-test'
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "preview_id": preview_a,
                        "candidate_id": candidate_a,
                        "decision_set_hash": "e" * 64,
                    },
                )
                with pytest.raises(DBAPIError) as mutation_error, connection.begin_nested():
                    connection.execute(
                        sa.text(
                            "UPDATE careerops.smart_intake_decisions "
                            "SET actor_id = 'tampered' "
                            "WHERE apply_idempotency_key = 'append-only-test'"
                        )
                    )
                assert _sqlstate(mutation_error.value) == "55000"
            finally:
                transaction.rollback()

        with engine.connect() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
            retention_delete = connection.scalar(
                sa.text(
                    """SELECT has_table_privilege(
                        'careerops_retention',
                        'careerops.smart_intake_previews',
                        'DELETE'
                    )"""
                )
            )
            assert retention_delete is False
    finally:
        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM careerops.candidates WHERE id IN (:a, :b)"),
                {"a": candidate_a, "b": candidate_b},
            )
