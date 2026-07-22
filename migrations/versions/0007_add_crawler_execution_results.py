"""record terminal crawler execution results atomically

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESULT_TABLE = "crawler_execution_results"
_COMPLETION_FUNCTION = "careerops.complete_crawler_execution_outbox_event"
_COMPLETION_SIGNATURE = "(uuid, text, uuid, text, text, uuid)"

_API_GRANTS = ("GRANT SELECT ON careerops.crawler_execution_results TO careerops_api",)

_OUTBOX_GRANTS = (
    "GRANT SELECT ON careerops.crawler_execution_results TO careerops_outbox",
    f"GRANT EXECUTE ON FUNCTION {_COMPLETION_FUNCTION}{_COMPLETION_SIGNATURE} TO careerops_outbox",
)

_READONLY_GRANTS = ("GRANT SELECT ON careerops.crawler_execution_results TO careerops_readonly",)

_API_REVOKES = ("REVOKE ALL ON careerops.crawler_execution_results FROM careerops_api",)

_OUTBOX_REVOKES = (
    "REVOKE ALL ON careerops.crawler_execution_results FROM careerops_outbox",
    f"REVOKE ALL ON FUNCTION {_COMPLETION_FUNCTION}{_COMPLETION_SIGNATURE} FROM careerops_outbox",
)

_READONLY_REVOKES = ("REVOKE ALL ON careerops.crawler_execution_results FROM careerops_readonly",)


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _run_for_role(role_name: str, statements: tuple[str, ...]) -> None:
    if op.get_context().as_sql:
        for statement in statements:
            op.execute(sa.text(statement))
        return
    if _role_exists(role_name):
        for statement in statements:
            op.execute(sa.text(statement))


def _install_append_only_guard() -> None:
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_crawler_execution_results_append_only "
            "BEFORE UPDATE OR DELETE ON careerops.crawler_execution_results "
            "FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
        )
    )


def _drop_append_only_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_crawler_execution_results_append_only "
            "ON careerops.crawler_execution_results"
        )
    )


def _install_completion_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.complete_crawler_execution_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_outcome text,
                p_error_code text,
                p_result_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_action_intent_id uuid;
                v_request_id uuid;
                v_execution_key text;
                v_event_key text;
                v_event_type text;
                v_event_status text;
                v_event_lease_owner text;
                v_event_lease_token uuid;
                v_event_lease_until timestamp with time zone;
                v_intent_status text;
                v_completed_at timestamp with time zone := CURRENT_TIMESTAMP;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL OR p_result_id IS NULL THEN
                    RAISE EXCEPTION 'crawler completion requires event, lease token, and result ids'
                        USING ERRCODE = '22004';
                END IF;

                IF p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
                    RAISE EXCEPTION 'crawler completion lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;

                IF p_outcome NOT IN ('succeeded', 'failed', 'reconciliation_required') THEN
                    RAISE EXCEPTION 'crawler completion outcome is invalid'
                        USING ERRCODE = '22023';
                END IF;

                IF (p_outcome = 'succeeded' AND p_error_code IS NOT NULL)
                   OR (p_outcome IN ('failed', 'reconciliation_required') AND (
                       p_error_code IS NULL
                       OR p_error_code !~ '^[A-Z0-9_]{1,64}$'
                   )) THEN
                    RAISE EXCEPTION 'crawler completion error code does not match outcome'
                        USING ERRCODE = '22023';
                END IF;

                SELECT
                    request.action_intent_id,
                    request.id,
                    dispatch.execution_key,
                    event.event_key,
                    event.event_type,
                    event.status,
                    event.lease_owner,
                    event.lease_token,
                    event.lease_until,
                    intent.status
                INTO
                    v_action_intent_id,
                    v_request_id,
                    v_execution_key,
                    v_event_key,
                    v_event_type,
                    v_event_status,
                    v_event_lease_owner,
                    v_event_lease_token,
                    v_event_lease_until,
                    v_intent_status
                FROM careerops.outbox_events AS event
                JOIN careerops.crawler_execution_dispatches AS dispatch
                  ON dispatch.outbox_event_id = event.id
                 AND dispatch.action_intent_id = event.action_intent_id
                JOIN careerops.crawler_execution_requests AS request
                  ON request.id = dispatch.request_id
                 AND request.action_intent_id = dispatch.action_intent_id
                JOIN careerops.action_intents AS intent
                  ON intent.id = request.action_intent_id
                WHERE event.id = p_event_id
                FOR UPDATE OF event, dispatch, request, intent;

                IF v_request_id IS NULL THEN
                    RAISE EXCEPTION 'crawler completion event % is not a bound crawler dispatch',
                        p_event_id
                        USING ERRCODE = '23503';
                END IF;

                IF v_event_type <> 'workflow_signal'
                   OR v_event_key <> v_execution_key
                   OR v_event_key NOT LIKE 'crawler-execution:%' THEN
                    RAISE EXCEPTION 'crawler completion event % has invalid crawler binding',
                        p_event_id
                        USING ERRCODE = '23514';
                END IF;

                IF v_event_status <> 'leased'
                   OR v_event_lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event_lease_token IS DISTINCT FROM p_lease_token
                   OR v_event_lease_until <= v_completed_at THEN
                    RAISE EXCEPTION 'crawler completion lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                IF v_intent_status <> 'processing' THEN
                    RAISE EXCEPTION 'crawler completion requires a processing action intent'
                        USING ERRCODE = '23514';
                END IF;

                PERFORM set_config('careerops.outbox_lease_owner', p_lease_owner, true);
                PERFORM set_config('careerops.outbox_lease_token', p_lease_token::text, true);

                IF p_outcome = 'succeeded' THEN
                    UPDATE careerops.outbox_events
                    SET status = 'published',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_until = NULL,
                        published_at = v_completed_at,
                        last_error_code = NULL
                    WHERE id = p_event_id;

                    UPDATE careerops.action_intents
                    SET status = 'confirmed',
                        updated_at = v_completed_at
                    WHERE id = v_action_intent_id;
                ELSE
                    UPDATE careerops.outbox_events
                    SET status = 'failed',
                        available_at = v_completed_at,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_until = NULL,
                        last_error_code = p_error_code
                    WHERE id = p_event_id;

                    UPDATE careerops.action_intents
                    SET status = p_outcome,
                        updated_at = v_completed_at
                    WHERE id = v_action_intent_id;
                END IF;

                INSERT INTO careerops.crawler_execution_results (
                    id,
                    request_id,
                    action_intent_id,
                    outbox_event_id,
                    outcome,
                    error_code,
                    completed_at
                ) VALUES (
                    p_result_id,
                    v_request_id,
                    v_action_intent_id,
                    p_event_id,
                    p_outcome,
                    p_error_code,
                    v_completed_at
                );
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_COMPLETION_FUNCTION}{_COMPLETION_SIGNATURE} FROM PUBLIC")
    )


def _drop_completion_function() -> None:
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_COMPLETION_FUNCTION}{_COMPLETION_SIGNATURE}"))


def upgrade() -> None:
    op.create_table(
        _RESULT_TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'reconciliation_required')",
            name=op.f("ck_crawler_execution_results_outcome_values"),
        ),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND error_code IS NULL) OR "
            "(outcome IN ('failed', 'reconciliation_required') AND error_code IS NOT NULL)",
            name=op.f("ck_crawler_execution_results_error_code_matches_outcome"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z0-9_]{1,64}$'",
            name=op.f("ck_crawler_execution_results_error_code_format"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "action_intent_id"],
            [
                "careerops.crawler_execution_requests.id",
                "careerops.crawler_execution_requests.action_intent_id",
            ],
            name="fk_crawler_execution_results_request_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "outbox_event_id"],
            [
                "careerops.outbox_events.action_intent_id",
                "careerops.outbox_events.id",
            ],
            name="fk_crawler_execution_results_outbox_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawler_execution_results")),
        sa.UniqueConstraint(
            "request_id",
            name=op.f("uq_crawler_execution_results_request_id"),
        ),
        sa.UniqueConstraint(
            "outbox_event_id",
            name=op.f("uq_crawler_execution_results_outbox_event_id"),
        ),
        schema="careerops",
    )
    _install_append_only_guard()
    _install_completion_function()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_outbox", _OUTBOX_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_outbox", _OUTBOX_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_completion_function()
    _drop_append_only_guard()
    op.drop_table(_RESULT_TABLE, schema="careerops")
