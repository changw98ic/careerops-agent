"""add pre-application-only GoalRun review completion

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-22
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION_SIGNATURE = (
    "careerops.goal_run_record_review_decision"
    "(uuid, uuid, bigint, uuid, uuid, text, text, text, text, text)"
)


def _install_pre_application_review_completion() -> None:
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.goal_run_record_review_decision(
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
    v_review_item careerops.goal_run_review_items%ROWTYPE;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'expected_version', p_expected_version, 'fencing_token', p_fencing_token,
        'review_item_id', p_review_item_id, 'review_snapshot_sha256', p_review_snapshot_sha256,
        'decision', p_decision, 'reason', p_reason
    )::text, 'sha256'), 'hex');
    v_result jsonb;
    v_next_status text;
    v_next_phase text;
    v_mode text;
    v_is_pre_application_completion boolean := false;
    v_completion_checkpoint jsonb;
    v_checkpoint_idempotency_key text;
    v_event_json jsonb;
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
    SELECT * INTO v_review_item
    FROM careerops.goal_run_review_items AS item
    WHERE item.id = p_review_item_id
      AND item.goal_run_id = p_goal_run_id
      AND item.actor_user_id = p_actor_id
      AND item.snapshot_sha256 = p_review_snapshot_sha256
    FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run review item is unavailable for actor or snapshot'
            USING ERRCODE = '23503';
    END IF;

    v_mode := COALESCE(v_run.context_json->>'mode', 'gmail_dispatch');
    IF p_decision = 'approve' THEN
        IF v_mode = 'pre_application_only' THEN
            IF v_run.review_kind IS DISTINCT FROM 'goal_run_pre_application_review.v1'
               OR v_review_item.review_kind IS DISTINCT FROM 'goal_run_pre_application_review.v1' THEN
                RAISE EXCEPTION 'pre-application approval requires the exact review kind'
                    USING ERRCODE = '23514';
            END IF;
            v_next_status := 'completed';
            v_next_phase := 'completed';
            v_is_pre_application_completion := true;
            v_completion_checkpoint := jsonb_build_object(
                'completion_kind', 'pre_application_package_approved',
                'mode', 'pre_application_only',
                'review_item_id', p_review_item_id,
                'review_snapshot_sha256', p_review_snapshot_sha256,
                'review_decision', p_decision
            );
            v_checkpoint_idempotency_key := 'review-finalize:' || encode(public.digest(
                jsonb_build_object(
                    'goal_run_id', p_goal_run_id,
                    'review_item_id', p_review_item_id,
                    'review_snapshot_sha256', p_review_snapshot_sha256,
                    'decision_idempotency_key', p_idempotency_key
                )::text,
                'sha256'
            ), 'hex');
            v_event_json := jsonb_build_object(
                'review_item_id', p_review_item_id,
                'decision', p_decision,
                'mode', 'pre_application_only',
                'completion_kind', 'pre_application_package_approved'
            );
        ELSIF v_mode = 'gmail_dispatch' THEN
            v_next_status := 'running';
            v_next_phase := 'dispatch';
            v_event_json := jsonb_build_object(
                'review_item_id', p_review_item_id,
                'decision', p_decision
            );
        ELSE
            RAISE EXCEPTION 'goal run mode is unsupported for approval' USING ERRCODE = '23514';
        END IF;
    ELSE
        v_next_status := 'rejected';
        v_next_phase := 'rejected';
        v_event_json := jsonb_build_object(
            'review_item_id', p_review_item_id,
            'decision', p_decision
        );
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

    IF v_is_pre_application_completion THEN
        INSERT INTO careerops.goal_run_checkpoints (
            id, goal_run_id, actor_user_id, run_version, phase, status, outcome,
            checkpoint_json, checkpoint_sha256, last_error_code, idempotency_key,
            fencing_token, trace_id, created_at
        )
        VALUES (
            gen_random_uuid(), p_goal_run_id, p_actor_id, v_run.version + 1,
            'completed', 'completed', 'finalize', v_completion_checkpoint,
            encode(public.digest(v_completion_checkpoint::text, 'sha256'), 'hex'),
            NULL, v_checkpoint_idempotency_key, p_fencing_token, p_trace_id, v_now
        );
    END IF;

    UPDATE careerops.goal_runs
    SET status = v_next_status,
        phase = v_next_phase,
        version = v_run.version + 1,
        checkpoint_json = CASE
            WHEN v_is_pre_application_completion THEN v_completion_checkpoint
            ELSE checkpoint_json
        END,
        review_decision = p_decision,
        completed_at = CASE
            WHEN v_is_pre_application_completion THEN v_now
            ELSE completed_at
        END,
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
        v_event_json,
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
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC"))


def _restore_legacy_review_completion() -> None:
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.goal_run_record_review_decision(
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
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_FUNCTION_SIGNATURE} FROM PUBLIC"))


def upgrade() -> None:
    _install_pre_application_review_completion()


def downgrade() -> None:
    _restore_legacy_review_completion()
