"""add durable goal run control plane

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-20
"""
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "goal_run_checkpoints",
    "goal_run_events",
    "goal_run_review_items",
    "goal_run_review_decisions",
    "goal_run_command_receipts",
)

_PHASE_VALUES = (
    "'initializing', 'selecting_source', 'claiming_source', 'discovery', "
    "'complete_source', 'canonical_ingest', 'matching', 'draft_preparation', "
    "'review', 'dispatch', 'reconciliation', 'completed', 'blocked', 'failed', "
    "'cancelled', 'rejected'"
)
_STATUS_VALUES = (
    "'starting', 'running', 'waiting_review', 'blocked', "
    "'reconciliation_required', 'completed', 'failed', 'cancelled', 'rejected'"
)
_TERMINAL_STATUS_VALUES = (
    "'reconciliation_required', 'completed', 'failed', 'cancelled', 'rejected'"
)
_FINALIZE_STATUS_VALUES = "'completed', 'failed', 'cancelled', 'rejected'"
_OUTCOME_VALUES = "'progress', 'waiting_review', 'blocked', 'reconciliation_required', 'finalize'"

_FUNCTION_SIGNATURES = (
    "careerops.goal_run_create(uuid, text, text, text, jsonb, uuid, text, integer, text, text)",
    "careerops.goal_run_status(uuid, uuid)",
    "careerops.goal_run_list_owner(uuid, integer, text)",
    "careerops.goal_run_checkpoint(uuid, uuid, bigint, uuid, text, text, text, jsonb, text, text, text)",
    "careerops.goal_run_request_review(uuid, uuid, bigint, uuid, uuid, text, text, jsonb, text, text)",
    "careerops.goal_run_record_review_decision(uuid, uuid, bigint, uuid, uuid, text, text, text, text, text)",
    "careerops.goal_run_cancel(uuid, uuid, bigint, uuid, text, text, text)",
    "careerops.goal_run_resume(uuid, uuid, bigint, uuid, text, text)",
    "careerops.goal_run_reviewed_crawler_result(uuid, uuid, uuid, text)",
)

_API_FUNCTION_SIGNATURES = (
    _FUNCTION_SIGNATURES[0],
    _FUNCTION_SIGNATURES[1],
    _FUNCTION_SIGNATURES[2],
    _FUNCTION_SIGNATURES[5],
    _FUNCTION_SIGNATURES[6],
    _FUNCTION_SIGNATURES[7],
)

_WORKFLOW_FUNCTION_SIGNATURES = (
    _FUNCTION_SIGNATURES[1],
    _FUNCTION_SIGNATURES[3],
    _FUNCTION_SIGNATURES[4],
    _FUNCTION_SIGNATURES[8],
    "careerops.claim_due_crawler_source(uuid, text, uuid, integer)",
    "careerops.claim_crawler_source(uuid, text, text, uuid, integer)",
    "careerops.complete_crawler_source_run(uuid, text, uuid, text, text, text)",
    "careerops.fail_crawler_source_run(uuid, text, uuid, text)",
)

_READONLY_TABLES = (
    "careerops.goal_runs",
    "careerops.goal_run_checkpoints",
    "careerops.goal_run_events",
    "careerops.goal_run_review_items",
    "careerops.goal_run_review_decisions",
)

_API_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_api",
    f"GRANT SELECT ON {', '.join(_READONLY_TABLES)} TO careerops_api",
    *(
        f"GRANT EXECUTE ON FUNCTION {signature} TO careerops_api"
        for signature in _API_FUNCTION_SIGNATURES
    ),
)

_WORKFLOW_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_workflow",
    "GRANT SELECT ON careerops.goal_runs, careerops.goal_run_checkpoints, "
    "careerops.goal_run_review_items, careerops.crawler_source_registry_sources, "
    "careerops.crawler_source_runs, careerops.crawler_job_deduplication_keys, "
    "careerops.crawler_job_ingestion_evidence, careerops.companies, "
    "careerops.job_sources, careerops.canonical_jobs, "
    "careerops.job_postings, careerops.job_posting_versions, "
    "careerops.job_posting_assignments TO careerops_workflow",
    "GRANT INSERT ON careerops.companies, careerops.job_sources, "
    "careerops.canonical_jobs, careerops.job_postings, "
    "careerops.job_posting_versions, careerops.job_merge_decisions, "
    "careerops.job_posting_assignments TO careerops_workflow",
    "GRANT INSERT ON careerops.crawler_job_deduplication_keys, "
    "careerops.crawler_job_ingestion_evidence TO careerops_workflow",
    "GRANT UPDATE (last_discovery_at, updated_at) ON careerops.job_sources TO careerops_workflow",
    "GRANT UPDATE (primary_posting_id, updated_at) "
    "ON careerops.canonical_jobs TO careerops_workflow",
    "GRANT UPDATE (source_state, last_seen_at, closed_at, updated_at) "
    "ON careerops.job_postings TO careerops_workflow",
    *(
        f"GRANT EXECUTE ON FUNCTION {signature} TO careerops_workflow"
        for signature in _WORKFLOW_FUNCTION_SIGNATURES
    ),
)

_READONLY_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_readonly",
    f"GRANT SELECT ON {', '.join(_READONLY_TABLES)} TO careerops_readonly",
)

_API_REVOKES = (
    f"REVOKE ALL ON {', '.join(_READONLY_TABLES)} FROM careerops_api",
    *(
        f"REVOKE ALL ON FUNCTION {signature} FROM careerops_api"
        for signature in _API_FUNCTION_SIGNATURES
    ),
)

_WORKFLOW_REVOKES = (
    "REVOKE ALL ON careerops.goal_runs, careerops.goal_run_checkpoints, "
    "careerops.goal_run_review_items, careerops.crawler_source_registry_sources, "
    "careerops.crawler_source_runs, careerops.crawler_job_deduplication_keys, "
    "careerops.crawler_job_ingestion_evidence, careerops.companies, "
    "careerops.job_sources, careerops.canonical_jobs, "
    "careerops.job_postings, careerops.job_posting_versions, "
    "careerops.job_merge_decisions, careerops.job_posting_assignments "
    "FROM careerops_workflow",
    *(
        f"REVOKE ALL ON FUNCTION {signature} FROM careerops_workflow"
        for signature in _WORKFLOW_FUNCTION_SIGNATURES
    ),
)

_READONLY_REVOKES = (f"REVOKE ALL ON {', '.join(_READONLY_TABLES)} FROM careerops_readonly",)


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


def _install_append_only_guards() -> None:
    for table_name in APPEND_ONLY_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_append_only "
                f"BEFORE UPDATE OR DELETE ON careerops.{table_name} "
                "FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
            )
        )


def _drop_append_only_guards() -> None:
    for table_name in reversed(APPEND_ONLY_TABLES):
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON careerops.{table_name}"
            )
        )


def _install_helpers() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_public_record(p_goal_run_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, careerops
AS $function$
    SELECT jsonb_build_object(
        'goal_run_id', run.id,
        'actor_id', run.actor_user_id,
        'goal_kind', run.goal_kind,
        'goal', run.goal,
        'status', run.status,
        'phase', run.phase,
        'version', run.version,
        'fencing_token', run.fencing_token,
        'context', run.context_json,
        'checkpoint', run.checkpoint_json,
        'idempotency_key', run.idempotency_key,
        'trace_id', run.trace_id,
        'created_at', run.created_at,
        'updated_at', run.updated_at,
        'registry_id', run.registry_id,
        'source_id', run.source_id,
        'max_records', run.max_records,
        'temporal_workflow_id', run.temporal_workflow_id,
        'review_item_id', run.review_item_id,
        'review_snapshot_sha256', run.review_snapshot_sha256,
        'review_decision', run.review_decision,
        'review_kind', run.review_kind,
        'review_payload', run.review_payload,
        'last_error_code', run.last_error_code,
        'cancelled_at', run.cancelled_at,
        'completed_at', run.completed_at
    )
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_receipt_result(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_command_kind text,
    p_idempotency_key text,
    p_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_receipt careerops.goal_run_command_receipts%ROWTYPE;
BEGIN
    SELECT * INTO v_receipt
    FROM careerops.goal_run_command_receipts AS receipt
    WHERE receipt.actor_user_id = p_actor_id
      AND receipt.goal_run_id IS NOT DISTINCT FROM p_goal_run_id
      AND receipt.command_kind = p_command_kind
      AND receipt.idempotency_key = p_idempotency_key;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_receipt.request_sha256 <> p_request_hash THEN
        RAISE EXCEPTION 'goal run idempotency conflicts with existing request'
            USING ERRCODE = '23505';
    END IF;
    RETURN v_receipt.response_json || jsonb_build_object('newly_created', false);
END
$function$
"""
        )
    )
    for signature in (
        "careerops._goal_run_public_record(uuid)",
        "careerops._goal_run_receipt_result(uuid, uuid, text, text, text)",
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))


def _drop_helpers() -> None:
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "careerops._goal_run_receipt_result(uuid, uuid, text, text, text)"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops._goal_run_public_record(uuid)"))


def _install_identity_guard() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.enforce_goal_run_identity_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.goal_kind IS DISTINCT FROM OLD.goal_kind
       OR NEW.goal IS DISTINCT FROM OLD.goal
       OR NEW.context_json IS DISTINCT FROM OLD.context_json
       OR NEW.registry_id IS DISTINCT FROM OLD.registry_id
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.max_records IS DISTINCT FROM OLD.max_records
       OR NEW.temporal_workflow_id IS DISTINCT FROM OLD.temporal_workflow_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'goal run identity is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IN ('reconciliation_required', 'completed', 'failed', 'cancelled', 'rejected') THEN
        RAISE EXCEPTION 'terminal goal runs are immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'goal run version must advance by one' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$
"""
        )
    )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.enforce_goal_run_identity_update() FROM PUBLIC")
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_goal_runs_identity_update "
            "BEFORE UPDATE ON careerops.goal_runs "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_goal_run_identity_update()"
        )
    )


def _drop_identity_guard() -> None:
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_goal_runs_identity_update ON careerops.goal_runs")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops.enforce_goal_run_identity_update()"))


def _install_control_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_create(
    p_actor_id uuid,
    p_idempotency_key text,
    p_goal_kind text,
    p_goal text,
    p_context jsonb,
    p_registry_id uuid,
    p_source_id text,
    p_max_records integer,
    p_temporal_workflow_id text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_goal_run_id uuid := gen_random_uuid();
    v_fencing_token uuid := gen_random_uuid();
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'actor_id', p_actor_id, 'goal_kind', p_goal_kind, 'goal', p_goal,
        'context', p_context, 'registry_id', p_registry_id, 'source_id', p_source_id,
        'max_records', p_max_records, 'temporal_workflow_id', p_temporal_workflow_id
    )::text, 'sha256'), 'hex');
    v_result jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_idempotency_key IS NULL
       OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_goal_kind IS NULL OR p_goal_kind !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_goal IS NULL OR btrim(p_goal) = '' OR char_length(p_goal) > 4000
       OR p_context IS NULL OR jsonb_typeof(p_context) <> 'object'
       OR p_max_records IS NULL OR p_max_records < 1
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR (p_source_id IS NOT NULL AND p_source_id !~ '^[A-Za-z0-9._:-]{1,128}$')
       OR (p_temporal_workflow_id IS NOT NULL
           AND p_temporal_workflow_id !~ '^[A-Za-z0-9._:-]{1,128}$') THEN
        RAISE EXCEPTION 'goal run create command is invalid' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('careerops:goal-run:create:' || p_actor_id::text || ':' || p_idempotency_key, 0)
    );
    v_result := careerops._goal_run_receipt_result(
        p_actor_id, NULL, 'create', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;

    INSERT INTO careerops.goal_runs (
        id, actor_user_id, idempotency_key, goal_kind, goal, context_json,
        registry_id, source_id, max_records, temporal_workflow_id,
        status, phase, version, fencing_token, checkpoint_json, trace_id, created_at, updated_at
    )
    VALUES (
        v_goal_run_id, p_actor_id, p_idempotency_key, p_goal_kind, p_goal, p_context,
        p_registry_id, p_source_id, p_max_records, p_temporal_workflow_id,
        'starting', 'initializing', 1, v_fencing_token, '{}'::jsonb, p_trace_id, v_now, v_now
    );

    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, to_status, to_phase, version,
        idempotency_key, fencing_token, trace_id, event_json, created_at
    )
    VALUES (
        gen_random_uuid(), v_goal_run_id, p_actor_id, 'created', 'starting',
        'initializing', 1, p_idempotency_key, v_fencing_token, p_trace_id,
        jsonb_build_object('goal_kind', p_goal_kind), v_now
    );

    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(v_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts (
        id, goal_run_id, actor_user_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), NULL, p_actor_id, 'create', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_status(p_actor_id uuid, p_goal_run_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_record jsonb;
BEGIN
    SELECT careerops._goal_run_public_record(run.id)
    INTO v_record
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id;
    IF v_record IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    RETURN v_record;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_list_owner(
    p_actor_id uuid,
    p_limit integer,
    p_cursor text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_limit integer := least(greatest(coalesce(p_limit, 100), 1), 500);
BEGIN
    RETURN (
        WITH page AS (
            SELECT run.id, run.updated_at
            FROM careerops.goal_runs AS run
            WHERE run.actor_user_id = p_actor_id
              AND (
                  p_cursor IS NULL
                  OR to_char(
                      run.updated_at AT TIME ZONE 'UTC',
                      'YYYYMMDDHH24MISSUS'
                  ) || ':' || run.id::text < p_cursor
              )
            ORDER BY run.updated_at DESC, run.id DESC
            LIMIT v_limit + 1
        ),
        visible AS (
            SELECT id, updated_at FROM page ORDER BY updated_at DESC, id DESC LIMIT v_limit
        )
        SELECT jsonb_build_object(
            'records', coalesce(jsonb_agg(careerops._goal_run_public_record(visible.id)
                ORDER BY visible.updated_at DESC, visible.id DESC), '[]'::jsonb),
            'next_cursor', CASE
                WHEN (SELECT count(*) FROM page) > v_limit THEN
                    (SELECT to_char(
                        updated_at AT TIME ZONE 'UTC',
                        'YYYYMMDDHH24MISSUS'
                    ) || ':' || id::text FROM visible
                     ORDER BY updated_at ASC, id ASC LIMIT 1)
                ELSE NULL
            END
        )
        FROM visible
    );
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_checkpoint(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_expected_version bigint,
    p_fencing_token uuid,
    p_phase text,
    p_status text,
    p_outcome text,
    p_checkpoint jsonb,
    p_last_error_code text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.goal_runs%ROWTYPE;
    v_next_phase text;
    v_resume_phase text;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version, 'fencing_token', p_fencing_token,
        'phase', p_phase, 'status', p_status, 'outcome', p_outcome,
        'checkpoint', p_checkpoint, 'last_error_code', p_last_error_code
    )::text, 'sha256'), 'hex');
    v_result jsonb;
BEGIN
    IF p_phase NOT IN ("""
            + _PHASE_VALUES
            + """)
       OR p_status NOT IN ("""
            + _STATUS_VALUES
            + """)
       OR p_outcome NOT IN ("""
            + _OUTCOME_VALUES
            + """)
       OR p_checkpoint IS NULL OR jsonb_typeof(p_checkpoint) <> 'object'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR (p_last_error_code IS NOT NULL AND p_last_error_code !~ '^[A-Za-z0-9._:-]{1,128}$') THEN
        RAISE EXCEPTION 'goal run checkpoint command is invalid' USING ERRCODE = '22023';
    END IF;
    IF (p_status IN ("""
            + _FINALIZE_STATUS_VALUES
            + """)) <> (p_outcome = 'finalize') THEN
        RAISE EXCEPTION 'terminal checkpoint status requires finalize outcome' USING ERRCODE = '23514';
    END IF;

    v_result := careerops._goal_run_receipt_result(
        p_actor_id, p_goal_run_id, 'checkpoint', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;

    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    IF v_run.version <> p_expected_version OR v_run.fencing_token IS DISTINCT FROM p_fencing_token THEN
        RAISE EXCEPTION 'goal run transition fencing is stale' USING ERRCODE = '55000';
    END IF;
    IF v_run.status IN ("""
            + _TERMINAL_STATUS_VALUES
            + """) THEN
        RAISE EXCEPTION 'terminal goal runs are immutable' USING ERRCODE = '23514';
    END IF;
    v_next_phase := CASE v_run.phase
        WHEN 'initializing' THEN 'selecting_source'
        WHEN 'selecting_source' THEN 'claiming_source'
        WHEN 'claiming_source' THEN 'discovery'
        WHEN 'discovery' THEN 'complete_source'
        WHEN 'complete_source' THEN 'canonical_ingest'
        WHEN 'canonical_ingest' THEN 'matching'
        WHEN 'matching' THEN 'draft_preparation'
        WHEN 'draft_preparation' THEN 'review'
        WHEN 'review' THEN 'dispatch'
        WHEN 'dispatch' THEN 'reconciliation'
        WHEN 'reconciliation' THEN 'completed'
        ELSE NULL
    END;
    IF p_outcome = 'progress' THEN
        IF p_status <> 'running' OR p_phase NOT IN (v_run.phase, v_next_phase) THEN
            RAISE EXCEPTION 'goal run checkpoint must target current or next phase'
                USING ERRCODE = '23514';
        END IF;
    ELSIF p_outcome = 'waiting_review' THEN
        IF p_status <> 'waiting_review'
           OR v_run.phase <> 'draft_preparation'
           OR p_phase <> 'review' THEN
            RAISE EXCEPTION 'goal run review checkpoint must follow draft preparation'
                USING ERRCODE = '23514';
        END IF;
    ELSIF p_outcome = 'blocked' THEN
        v_resume_phase := COALESCE(
            NULLIF(p_checkpoint->>'resume_phase', ''),
            NULLIF(p_checkpoint->>'blocked_phase', '')
        );
        IF p_status <> 'blocked'
           OR p_phase <> 'blocked'
           OR v_resume_phase NOT IN (v_run.phase, v_next_phase) THEN
            RAISE EXCEPTION 'goal run blocked checkpoint must bind a resumable phase'
                USING ERRCODE = '23514';
        END IF;
    ELSIF p_outcome = 'reconciliation_required' THEN
        IF p_status <> 'reconciliation_required' OR p_phase <> 'reconciliation' THEN
            RAISE EXCEPTION 'goal run reconciliation checkpoint is invalid'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF p_status = 'completed' AND p_phase <> 'completed' THEN
            RAISE EXCEPTION 'completed checkpoint must target completed phase'
                USING ERRCODE = '23514';
        ELSIF p_status = 'failed' AND p_phase <> 'failed' THEN
            RAISE EXCEPTION 'failed checkpoint must target failed phase'
                USING ERRCODE = '23514';
        ELSIF p_status = 'cancelled' AND p_phase <> 'cancelled' THEN
            RAISE EXCEPTION 'cancelled checkpoint must target cancelled phase'
                USING ERRCODE = '23514';
        ELSIF p_status = 'rejected' AND p_phase <> 'rejected' THEN
            RAISE EXCEPTION 'rejected checkpoint must target rejected phase'
                USING ERRCODE = '23514';
        END IF;
        IF p_status = 'completed' AND v_run.phase <> 'reconciliation' THEN
            RAISE EXCEPTION 'completed checkpoint must finalize reconciliation'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    INSERT INTO careerops.goal_run_checkpoints (
        id, goal_run_id, actor_user_id, run_version, phase, status, outcome,
        checkpoint_json, checkpoint_sha256, last_error_code, idempotency_key,
        fencing_token, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, v_run.version + 1, p_phase,
        p_status, p_outcome, p_checkpoint, encode(public.digest(p_checkpoint::text, 'sha256'), 'hex'),
        p_last_error_code, p_idempotency_key, p_fencing_token, p_trace_id, v_now
    );

    UPDATE careerops.goal_runs
    SET status = p_status,
        phase = p_phase,
        version = v_run.version + 1,
        checkpoint_json = p_checkpoint,
        last_error_code = p_last_error_code,
        completed_at = CASE WHEN p_status = 'completed' THEN v_now ELSE completed_at END,
        failed_at = CASE WHEN p_status = 'failed' THEN v_now ELSE failed_at END,
        cancelled_at = CASE WHEN p_status = 'cancelled' THEN v_now ELSE cancelled_at END,
        blocked_at = CASE WHEN p_status = 'blocked' THEN v_now ELSE blocked_at END,
        reconciliation_required_at = CASE WHEN p_status = 'reconciliation_required' THEN v_now ELSE reconciliation_required_at END,
        rejected_at = CASE WHEN p_status = 'rejected' THEN v_now ELSE rejected_at END,
        updated_at = v_now
    WHERE id = p_goal_run_id;

    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, from_status, to_status,
        from_phase, to_phase, version, idempotency_key, fencing_token, trace_id,
        event_json, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'checkpoint_recorded',
        v_run.status, p_status, v_run.phase, p_phase, v_run.version + 1,
        p_idempotency_key, p_fencing_token, p_trace_id,
        jsonb_build_object(
            'outcome', p_outcome,
            'last_error_code', p_last_error_code,
            'previous_phase', v_run.phase,
            'previous_status', v_run.status
        ),
        v_now
    );

    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(p_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'checkpoint', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_request_review(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_expected_version bigint,
    p_fencing_token uuid,
    p_review_item_id uuid,
    p_review_snapshot_sha256 text,
    p_review_kind text,
    p_review_payload jsonb,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.goal_runs%ROWTYPE;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version, 'fencing_token', p_fencing_token,
        'review_item_id', p_review_item_id, 'review_snapshot_sha256', p_review_snapshot_sha256,
        'review_kind', p_review_kind, 'review_payload', p_review_payload
    )::text, 'sha256'), 'hex');
    v_result jsonb;
BEGIN
    IF p_review_item_id IS NULL
       OR p_review_snapshot_sha256 IS NULL OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_review_kind IS NULL OR p_review_kind !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_review_payload IS NULL OR jsonb_typeof(p_review_payload) <> 'object'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run review request command is invalid' USING ERRCODE = '22023';
    END IF;
    v_result := careerops._goal_run_receipt_result(
        p_actor_id, p_goal_run_id, 'request_review', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    IF v_run.version <> p_expected_version OR v_run.fencing_token IS DISTINCT FROM p_fencing_token THEN
        RAISE EXCEPTION 'goal run transition fencing is stale' USING ERRCODE = '55000';
    END IF;
    IF v_run.status IN ("""
            + _TERMINAL_STATUS_VALUES
            + """) THEN
        RAISE EXCEPTION 'terminal goal runs are immutable' USING ERRCODE = '23514';
    END IF;
    IF v_run.phase <> 'draft_preparation' OR v_run.status <> 'running' THEN
        RAISE EXCEPTION 'goal run review request must follow draft preparation'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO careerops.goal_run_review_items (
        id, goal_run_id, actor_user_id, run_version, review_kind, review_payload,
        snapshot_sha256, snapshot_json, idempotency_key, trace_id, created_at
    )
    VALUES (
        p_review_item_id, p_goal_run_id, p_actor_id, v_run.version,
        p_review_kind, p_review_payload, p_review_snapshot_sha256,
        careerops._goal_run_public_record(p_goal_run_id), p_idempotency_key, p_trace_id, v_now
    );
    UPDATE careerops.goal_runs
    SET status = 'waiting_review',
        phase = 'review',
        version = v_run.version + 1,
        review_item_id = p_review_item_id,
        review_snapshot_sha256 = p_review_snapshot_sha256,
        review_kind = p_review_kind,
        review_payload = p_review_payload,
        updated_at = v_now
    WHERE id = p_goal_run_id;
    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, from_status, to_status,
        from_phase, to_phase, version, idempotency_key, fencing_token, trace_id,
        event_json, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'review_requested',
        v_run.status, 'waiting_review', v_run.phase, 'review', v_run.version + 1,
        p_idempotency_key, p_fencing_token, p_trace_id,
        jsonb_build_object('review_item_id', p_review_item_id, 'snapshot_sha256', p_review_snapshot_sha256),
        v_now
    );
    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(p_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'request_review', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_record_review_decision(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_expected_version bigint,
    p_fencing_token uuid,
    p_review_item_id uuid,
    p_review_snapshot_sha256 text,
    p_decision text,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.goal_runs%ROWTYPE;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version, 'fencing_token', p_fencing_token,
        'review_item_id', p_review_item_id, 'review_snapshot_sha256', p_review_snapshot_sha256,
        'decision', p_decision, 'reason', p_reason
    )::text, 'sha256'), 'hex');
    v_result jsonb;
    v_next_status text;
    v_next_phase text;
BEGIN
    IF p_decision NOT IN ('approve', 'reject')
       OR p_review_snapshot_sha256 IS NULL OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run review decision command is invalid' USING ERRCODE = '22023';
    END IF;
    v_result := careerops._goal_run_receipt_result(
        p_actor_id, p_goal_run_id, 'record_review_decision', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    IF v_run.version <> p_expected_version OR v_run.fencing_token IS DISTINCT FROM p_fencing_token THEN
        RAISE EXCEPTION 'goal run transition fencing is stale' USING ERRCODE = '55000';
    END IF;
    IF v_run.status <> 'waiting_review' OR v_run.review_item_id IS DISTINCT FROM p_review_item_id
       OR v_run.review_snapshot_sha256 IS DISTINCT FROM p_review_snapshot_sha256 THEN
        RAISE EXCEPTION 'goal run review item is not current' USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM careerops.goal_run_review_items AS item
        WHERE item.id = p_review_item_id
          AND item.goal_run_id = p_goal_run_id
          AND item.actor_user_id = p_actor_id
          AND item.snapshot_sha256 = p_review_snapshot_sha256
    ) THEN
        RAISE EXCEPTION 'goal run review item is unavailable for actor or snapshot'
            USING ERRCODE = '23503';
    END IF;
    IF p_decision = 'approve' THEN
        v_next_status := 'running';
        v_next_phase := 'dispatch';
    ELSE
        v_next_status := 'rejected';
        v_next_phase := 'rejected';
    END IF;
    INSERT INTO careerops.goal_run_review_decisions (
        id, review_item_id, goal_run_id, actor_user_id, run_version, decision,
        reason, snapshot_sha256, idempotency_key, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_review_item_id, p_goal_run_id, p_actor_id,
        v_run.version, p_decision, p_reason, p_review_snapshot_sha256,
        p_idempotency_key, p_trace_id, v_now
    );
    UPDATE careerops.goal_runs
    SET status = v_next_status,
        phase = v_next_phase,
        version = v_run.version + 1,
        review_decision = p_decision,
        rejected_at = CASE WHEN p_decision = 'reject' THEN v_now ELSE rejected_at END,
        updated_at = v_now
    WHERE id = p_goal_run_id;
    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, from_status, to_status,
        from_phase, to_phase, version, idempotency_key, fencing_token, trace_id,
        reason, event_json, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'review_decided',
        v_run.status, v_next_status, v_run.phase, v_next_phase, v_run.version + 1,
        p_idempotency_key, p_fencing_token, p_trace_id, p_reason,
        jsonb_build_object('review_item_id', p_review_item_id, 'decision', p_decision),
        v_now
    );
    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(p_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'record_review_decision', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_cancel(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_expected_version bigint,
    p_fencing_token uuid,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.goal_runs%ROWTYPE;
    v_result jsonb;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version,
        'fencing_token', p_fencing_token,
        'reason', p_reason
    )::text, 'sha256'), 'hex');
BEGIN
    IF p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run cancel command is invalid' USING ERRCODE = '22023';
    END IF;
    v_result := careerops._goal_run_receipt_result(
        p_actor_id, p_goal_run_id, 'cancel', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;
    SELECT * INTO v_run FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    IF v_run.version <> p_expected_version OR v_run.fencing_token IS DISTINCT FROM p_fencing_token THEN
        RAISE EXCEPTION 'goal run transition fencing is stale' USING ERRCODE = '55000';
    END IF;
    IF v_run.status IN ("""
            + _TERMINAL_STATUS_VALUES
            + """) THEN
        RAISE EXCEPTION 'terminal goal runs are immutable' USING ERRCODE = '23514';
    END IF;
    UPDATE careerops.goal_runs
    SET status = 'cancelled', phase = 'cancelled', version = v_run.version + 1,
        cancelled_at = v_now, updated_at = v_now
    WHERE id = p_goal_run_id;
    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, from_status, to_status,
        from_phase, to_phase, version, idempotency_key, fencing_token, trace_id,
        reason, event_json, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'cancelled',
        v_run.status, 'cancelled', v_run.phase, 'cancelled', v_run.version + 1,
        p_idempotency_key, p_fencing_token, p_trace_id, p_reason,
        jsonb_build_object('previous_status', v_run.status, 'previous_phase', v_run.phase),
        v_now
    );
    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(p_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'cancel', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_resume(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_expected_version bigint,
    p_fencing_token uuid,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.goal_runs%ROWTYPE;
    v_result jsonb;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version,
        'fencing_token', p_fencing_token,
        'resume', true
    )::text, 'sha256'), 'hex');
    v_phase text;
    v_status text;
BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run resume command is invalid' USING ERRCODE = '22023';
    END IF;
    v_result := careerops._goal_run_receipt_result(
        p_actor_id, p_goal_run_id, 'resume', p_idempotency_key, v_request_hash
    );
    IF v_result IS NOT NULL THEN
        RETURN v_result;
    END IF;
    SELECT * INTO v_run FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id AND run.actor_user_id = p_actor_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    IF v_run.version <> p_expected_version OR v_run.fencing_token IS DISTINCT FROM p_fencing_token THEN
        RAISE EXCEPTION 'goal run transition fencing is stale' USING ERRCODE = '55000';
    END IF;
    IF v_run.status <> 'blocked' THEN
        RAISE EXCEPTION 'only resumable goal runs can resume' USING ERRCODE = '23514';
    END IF;
    IF v_run.status = 'blocked' THEN
        SELECT checkpoint.checkpoint_json->>'resume_phase',
               checkpoint.checkpoint_json->>'blocked_phase'
        INTO v_phase, v_status
        FROM careerops.goal_run_checkpoints AS checkpoint
        WHERE checkpoint.goal_run_id = p_goal_run_id
          AND checkpoint.outcome = 'blocked'
        ORDER BY checkpoint.run_version DESC
        LIMIT 1;
        v_phase := COALESCE(NULLIF(v_phase, ''), NULLIF(v_status, ''), 'initializing');
    END IF;
    v_status := CASE WHEN v_phase = 'review' THEN 'waiting_review' ELSE 'running' END;
    UPDATE careerops.goal_runs
    SET status = v_status, phase = v_phase, version = v_run.version + 1,
        cancelled_at = CASE WHEN v_run.status = 'cancelled' THEN NULL ELSE cancelled_at END,
        blocked_at = CASE WHEN v_run.status = 'blocked' THEN NULL ELSE blocked_at END,
        reconciliation_required_at = CASE
            WHEN v_run.status = 'reconciliation_required' THEN NULL
            ELSE reconciliation_required_at
        END,
        updated_at = v_now
    WHERE id = p_goal_run_id;
    INSERT INTO careerops.goal_run_events (
        id, goal_run_id, actor_user_id, event_kind, from_status, to_status,
        from_phase, to_phase, version, idempotency_key, fencing_token, trace_id,
        event_json, created_at
    )
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'resumed',
        v_run.status, v_status, v_run.phase, v_phase, v_run.version + 1,
        p_idempotency_key, p_fencing_token, p_trace_id, '{}'::jsonb, v_now
    );
    v_result := jsonb_build_object(
        'record', careerops._goal_run_public_record(p_goal_run_id),
        'idempotency_key', p_idempotency_key,
        'newly_created', true
    );
    INSERT INTO careerops.goal_run_command_receipts
    VALUES (
        gen_random_uuid(), p_goal_run_id, p_actor_id, 'resume', p_idempotency_key,
        v_request_hash, v_result, p_trace_id, v_now
    );
    RETURN v_result;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_reviewed_crawler_result(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_registry_id uuid,
    p_source_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_run careerops.goal_runs%ROWTYPE;
    v_source careerops.crawler_source_registry_sources%ROWTYPE;
    v_manifest_sha256 text;
    v_row record;
    v_state text;
    v_error_code text;
BEGIN
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
      AND run.registry_id = p_registry_id
      AND (run.source_id IS NULL OR run.source_id = p_source_id);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run crawler result is unavailable for owner registry source'
            USING ERRCODE = '23503';
    END IF;

    SELECT registry.manifest_sha256
    INTO v_manifest_sha256
    FROM careerops.crawler_source_registries AS registry
    WHERE registry.id = p_registry_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run crawler registry is unavailable' USING ERRCODE = '23503';
    END IF;
    SELECT *
    INTO v_source
    FROM careerops.crawler_source_registry_sources AS source
    WHERE source.registry_id = p_registry_id
      AND source.source_id = p_source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run crawler source is not in registry' USING ERRCODE = '23503';
    END IF;

    SELECT
        request.id AS request_id,
        request.action_intent_id AS action_intent_id,
        request.request_sha256 AS request_sha256,
        request.reviewed_plan_sha256 AS reviewed_plan_sha256,
        request.expires_at AS expires_at,
        approval.decision AS approval_decision,
        approval.decided_by_user_id AS approval_user_id,
        dispatch.outbox_event_id AS outbox_event_id,
        outbox.status AS outbox_status,
        outbox.last_error_code AS outbox_error_code,
        result.id AS result_id,
        result.outcome AS outcome,
        result.error_code AS error_code,
        result.completed_at AS completed_at
    INTO v_row
    FROM careerops.crawler_execution_requests AS request
    LEFT JOIN careerops.crawler_execution_approvals AS approval
      ON approval.request_id = request.id
    LEFT JOIN careerops.crawler_execution_dispatches AS dispatch
      ON dispatch.request_id = request.id
     AND dispatch.action_intent_id = request.action_intent_id
    LEFT JOIN careerops.outbox_events AS outbox
      ON outbox.id = dispatch.outbox_event_id
     AND outbox.action_intent_id = dispatch.action_intent_id
    LEFT JOIN careerops.crawler_execution_results AS result
      ON result.request_id = request.id
     AND result.action_intent_id = request.action_intent_id
     AND result.outbox_event_id = dispatch.outbox_event_id
    WHERE request.owner_user_id = p_actor_id
      AND request.manifest_sha256 = v_manifest_sha256
      AND request.source_ids ? p_source_id
      AND request.created_at >= v_run.created_at
    ORDER BY request.created_at DESC, request.id DESC
    LIMIT 1;

    IF v_row.request_id IS NULL THEN
        v_state := 'waiting_review';
    ELSIF v_row.expires_at <= clock_timestamp() THEN
        v_state := 'failed';
        v_error_code := 'CRAWLER_EXECUTION_REQUEST_EXPIRED';
    ELSIF v_row.approval_decision IS NULL THEN
        v_state := 'waiting_review';
    ELSIF v_row.approval_decision = 'rejected' THEN
        v_state := 'failed';
        v_error_code := 'CRAWLER_EXECUTION_REJECTED';
    ELSIF v_row.approval_user_id IS DISTINCT FROM p_actor_id THEN
        v_state := 'failed';
        v_error_code := 'CRAWLER_EXECUTION_APPROVAL_OWNER_MISMATCH';
    ELSIF v_row.outbox_status = 'failed' THEN
        v_state := 'failed';
        v_error_code := COALESCE(
            v_row.outbox_error_code,
            'CRAWLER_EXECUTION_OUTBOX_FAILED'
        );
    ELSIF v_row.outbox_status IS DISTINCT FROM 'published' OR v_row.result_id IS NULL THEN
        v_state := 'waiting_review';
    ELSIF v_row.outcome = 'succeeded' THEN
        v_state := 'ready';
    ELSIF v_row.outcome = 'reconciliation_required' THEN
        v_state := 'reconciliation_required';
        v_error_code := COALESCE(
            v_row.error_code,
            'CRAWLER_EXECUTION_RECONCILIATION_REQUIRED'
        );
    ELSE
        v_state := 'failed';
        v_error_code := COALESCE(v_row.error_code, 'CRAWLER_EXECUTION_FAILED');
    END IF;

    RETURN jsonb_build_object(
        'state', v_state,
        'goal_run_id', p_goal_run_id,
        'registry_id', p_registry_id,
        'source_row_id', v_source.id,
        'source_id', p_source_id,
        'request_id', v_row.request_id,
        'outbox_event_id', v_row.outbox_event_id,
        'result_id', v_row.result_id,
        'outcome', v_row.outcome,
        'error_code', v_error_code,
        'reviewed_plan_sha256', v_row.reviewed_plan_sha256,
        'request_sha256', v_row.request_sha256,
        'completed_at', v_row.completed_at,
        'command_sha256', v_source.command_sha256,
        'source_sha256', v_source.source_sha256,
        'output_dir', v_source.output_dir,
        'adapter', v_source.adapter
    );
END
$function$
"""
        )
    )
    for signature in _FUNCTION_SIGNATURES:
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC"))


def _drop_control_functions() -> None:
    for signature in reversed(_FUNCTION_SIGNATURES):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {signature}"))


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    op.create_table(
        "goal_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("goal_kind", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("registry_id", sa.Uuid()),
        sa.Column("source_id", sa.Text()),
        sa.Column("max_records", sa.Integer(), nullable=False),
        sa.Column("temporal_workflow_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("checkpoint_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("review_item_id", sa.Uuid()),
        sa.Column("review_snapshot_sha256", sa.String(64)),
        sa.Column("review_decision", sa.Text()),
        sa.Column("review_kind", sa.Text()),
        sa.Column("review_payload", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("reconciliation_required_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_runs_idempotency_key_format"),
        ),
        sa.CheckConstraint(
            "goal_kind ~ '^[A-Za-z0-9._:-]{1,128}$'", name=op.f("ck_goal_runs_goal_kind_format")
        ),
        sa.CheckConstraint(
            "btrim(goal) <> '' AND char_length(goal) <= 4000",
            name=op.f("ck_goal_runs_goal_bounded"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(context_json) = 'object'", name=op.f("ck_goal_runs_context_json_object")
        ),
        sa.CheckConstraint(
            "source_id IS NULL OR source_id ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_runs_source_id_format"),
        ),
        sa.CheckConstraint("max_records > 0", name=op.f("ck_goal_runs_max_records_positive")),
        sa.CheckConstraint(
            "temporal_workflow_id IS NULL OR temporal_workflow_id ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_runs_temporal_workflow_id_format"),
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})", name=op.f("ck_goal_runs_status_values")
        ),
        sa.CheckConstraint(f"phase IN ({_PHASE_VALUES})", name=op.f("ck_goal_runs_phase_values")),
        sa.CheckConstraint("version > 0", name=op.f("ck_goal_runs_version_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(checkpoint_json) = 'object'",
            name=op.f("ck_goal_runs_checkpoint_json_object"),
        ),
        sa.CheckConstraint(
            "trace_id ~ '^[A-Za-z0-9._:-]{1,128}$'", name=op.f("ck_goal_runs_trace_id_format")
        ),
        sa.CheckConstraint(
            "review_snapshot_sha256 IS NULL OR review_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_goal_runs_review_snapshot_sha256_format"),
        ),
        sa.CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('approve', 'reject')",
            name=op.f("ck_goal_runs_review_decision_values"),
        ),
        sa.CheckConstraint(
            "review_kind IS NULL OR review_kind ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_runs_review_kind_format"),
        ),
        sa.CheckConstraint(
            "review_payload IS NULL OR jsonb_typeof(review_payload) = 'object'",
            name=op.f("ck_goal_runs_review_payload_object"),
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_runs_last_error_code_format"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_runs_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["careerops.crawler_source_registries.id"],
            name=op.f("fk_goal_runs_registry_id_crawler_source_registries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_runs")),
        sa.UniqueConstraint(
            "actor_user_id", "idempotency_key", name=op.f("uq_goal_runs_actor_idempotency_key")
        ),
        schema="careerops",
    )
    op.create_table(
        "goal_run_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("run_version", sa.BigInteger(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("checkpoint_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(64), nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_version > 0", name=op.f("ck_goal_run_checkpoints_run_version_positive")
        ),
        sa.CheckConstraint(
            f"phase IN ({_PHASE_VALUES})", name=op.f("ck_goal_run_checkpoints_phase_values")
        ),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})", name=op.f("ck_goal_run_checkpoints_status_values")
        ),
        sa.CheckConstraint(
            f"outcome IN ({_OUTCOME_VALUES})", name=op.f("ck_goal_run_checkpoints_outcome_values")
        ),
        sa.CheckConstraint(
            "(status IN (" + _FINALIZE_STATUS_VALUES + ")) = (outcome = 'finalize')",
            name=op.f("ck_goal_run_checkpoints_terminal_finalize"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(checkpoint_json) = 'object'",
            name=op.f("ck_goal_run_checkpoints_checkpoint_json_object"),
        ),
        sa.CheckConstraint(
            "checkpoint_sha256 ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_goal_run_checkpoints_checkpoint_sha256_format"),
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["careerops.goal_runs.id"],
            name=op.f("fk_goal_run_checkpoints_goal_run_id_goal_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_run_checkpoints_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_run_checkpoints")),
        sa.UniqueConstraint(
            "goal_run_id",
            "idempotency_key",
            name=op.f("uq_goal_run_checkpoints_goal_run_id_idempotency"),
        ),
        schema="careerops",
    )
    op.create_table(
        "goal_run_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text()),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("from_phase", sa.Text()),
        sa.Column("to_phase", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("event_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_kind IN ('created', 'checkpoint_recorded', 'review_requested', 'review_decided', 'cancelled', 'resumed')",
            name=op.f("ck_goal_run_events_event_kind_values"),
        ),
        sa.CheckConstraint(
            f"to_status IN ({_STATUS_VALUES})", name=op.f("ck_goal_run_events_to_status_values")
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUS_VALUES})",
            name=op.f("ck_goal_run_events_from_status_values"),
        ),
        sa.CheckConstraint(
            f"to_phase IN ({_PHASE_VALUES})", name=op.f("ck_goal_run_events_to_phase_values")
        ),
        sa.CheckConstraint(
            f"from_phase IS NULL OR from_phase IN ({_PHASE_VALUES})",
            name=op.f("ck_goal_run_events_from_phase_values"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_goal_run_events_version_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(event_json) = 'object'", name=op.f("ck_goal_run_events_event_json_object")
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["careerops.goal_runs.id"],
            name=op.f("fk_goal_run_events_goal_run_id_goal_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_run_events_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_run_events")),
        schema="careerops",
    )
    op.create_table(
        "goal_run_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("run_version", sa.BigInteger(), nullable=False),
        sa.Column("review_kind", sa.Text(), nullable=False),
        sa.Column("review_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_version > 0", name=op.f("ck_goal_run_review_items_run_version_positive")
        ),
        sa.CheckConstraint(
            "review_kind ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_run_review_items_kind_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(review_payload) = 'object'",
            name=op.f("ck_goal_run_review_items_payload_object"),
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_goal_run_review_items_snapshot_sha256_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot_json) = 'object'",
            name=op.f("ck_goal_run_review_items_snapshot_object"),
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["careerops.goal_runs.id"],
            name=op.f("fk_goal_run_review_items_goal_run_id_goal_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_run_review_items_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_run_review_items")),
        sa.UniqueConstraint(
            "goal_run_id",
            "idempotency_key",
            name=op.f("uq_goal_run_review_items_goal_run_id_idempotency"),
        ),
        schema="careerops",
    )
    op.create_table(
        "goal_run_review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_item_id", sa.Uuid(), nullable=False),
        sa.Column("goal_run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("run_version", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "run_version > 0", name=op.f("ck_goal_run_review_decisions_run_version_positive")
        ),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')",
            name=op.f("ck_goal_run_review_decisions_decision_values"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> '' AND char_length(reason) <= 4000",
            name=op.f("ck_goal_run_review_decisions_reason_bounded"),
        ),
        sa.CheckConstraint(
            "snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_goal_run_review_decisions_snapshot_sha256_format"),
        ),
        sa.ForeignKeyConstraint(
            ["review_item_id"],
            ["careerops.goal_run_review_items.id"],
            name=op.f("fk_goal_run_review_decisions_review_item_id_goal_run_review_items"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["careerops.goal_runs.id"],
            name=op.f("fk_goal_run_review_decisions_goal_run_id_goal_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_run_review_decisions_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_run_review_decisions")),
        sa.UniqueConstraint(
            "review_item_id", name=op.f("uq_goal_run_review_decisions_review_item_id")
        ),
        sa.UniqueConstraint(
            "goal_run_id",
            "idempotency_key",
            name=op.f("uq_goal_run_review_decisions_goal_run_id_idempotency"),
        ),
        schema="careerops",
    )
    op.create_table(
        "goal_run_command_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("goal_run_id", sa.Uuid()),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("command_kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "command_kind IN ('create', 'checkpoint', 'request_review', 'record_review_decision', 'cancel', 'resume')",
            name=op.f("ck_goal_run_command_receipts_kind_values"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_run_command_receipts_idempotency_key_format"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[a-f0-9]{64}$'",
            name=op.f("ck_goal_run_command_receipts_request_sha256_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response_json) = 'object'",
            name=op.f("ck_goal_run_command_receipts_response_json_object"),
        ),
        sa.CheckConstraint(
            "trace_id ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name=op.f("ck_goal_run_command_receipts_trace_id_format"),
        ),
        sa.ForeignKeyConstraint(
            ["goal_run_id"],
            ["careerops.goal_runs.id"],
            name=op.f("fk_goal_run_command_receipts_goal_run_id_goal_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_goal_run_command_receipts_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_run_command_receipts")),
        sa.UniqueConstraint(
            "actor_user_id",
            "goal_run_id",
            "command_kind",
            "idempotency_key",
            name=op.f("uq_goal_run_command_receipts_identity"),
        ),
        schema="careerops",
    )
    op.create_index(
        op.f("ix_goal_runs_actor_updated_at"),
        "goal_runs",
        ["actor_user_id", "updated_at"],
        schema="careerops",
    )
    op.create_index(
        op.f("ix_goal_run_events_goal_run_sequence"),
        "goal_run_events",
        ["goal_run_id", "sequence"],
        schema="careerops",
    )
    op.create_index(
        op.f("ix_goal_run_checkpoints_goal_run_version"),
        "goal_run_checkpoints",
        ["goal_run_id", "run_version"],
        schema="careerops",
    )
    op.create_index(
        op.f("ix_goal_run_review_items_goal_run_created_at"),
        "goal_run_review_items",
        ["goal_run_id", "created_at"],
        schema="careerops",
    )
    op.create_index(
        op.f("ix_goal_run_review_decisions_goal_run_created_at"),
        "goal_run_review_decisions",
        ["goal_run_id", "created_at"],
        schema="careerops",
    )
    _install_append_only_guards()
    _install_identity_guard()
    _install_helpers()
    _install_control_functions()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_workflow", _WORKFLOW_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_workflow", _WORKFLOW_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_control_functions()
    _drop_helpers()
    _drop_identity_guard()
    _drop_append_only_guards()
    op.drop_index(
        op.f("ix_goal_run_review_decisions_goal_run_created_at"),
        table_name="goal_run_review_decisions",
        schema="careerops",
    )
    op.drop_index(
        op.f("ix_goal_run_review_items_goal_run_created_at"),
        table_name="goal_run_review_items",
        schema="careerops",
    )
    op.drop_index(
        op.f("ix_goal_run_checkpoints_goal_run_version"),
        table_name="goal_run_checkpoints",
        schema="careerops",
    )
    op.drop_index(
        op.f("ix_goal_run_events_goal_run_sequence"),
        table_name="goal_run_events",
        schema="careerops",
    )
    op.drop_index(op.f("ix_goal_runs_actor_updated_at"), table_name="goal_runs", schema="careerops")
    op.drop_table("goal_run_command_receipts", schema="careerops")
    op.drop_table("goal_run_review_decisions", schema="careerops")
    op.drop_table("goal_run_review_items", schema="careerops")
    op.drop_table("goal_run_events", schema="careerops")
    op.drop_table("goal_run_checkpoints", schema="careerops")
    op.drop_table("goal_runs", schema="careerops")
