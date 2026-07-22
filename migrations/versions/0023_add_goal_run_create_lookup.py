"""add owner-scoped GoalRun create idempotency lookup

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-22
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = "careerops.goal_run_lookup_create"
_SIGNATURE = "(uuid,text)"
_PROFILE_GUARD_FUNCTION = "careerops.enforce_goal_run_preapplication_profile_approval"
_PROFILE_GUARD_SIGNATURE = "()"
_PROFILE_GUARD_TRIGGER = "trg_goal_run_review_decisions_profile_approval"


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _install_preapplication_profile_guard() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.enforce_goal_run_preapplication_profile_approval()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_context jsonb;
    v_candidate_id_text text;
    v_profile_version_id_text text;
    v_snapshot_sha256 text;
    v_material_bundle_sha256 text;
    v_candidate_id uuid;
    v_profile_version_id uuid;
    v_latest_profile_id uuid;
    v_latest_snapshot_sha256 text;
    v_latest_material_bundle_sha256 text;
BEGIN
    IF NEW.decision <> 'approve' THEN
        RETURN NEW;
    END IF;

    SELECT run.context_json INTO v_context
    FROM careerops.goal_runs AS run
    WHERE run.id = NEW.goal_run_id
      AND run.actor_user_id = NEW.actor_user_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run profile approval identity is unavailable'
            USING ERRCODE = '23503';
    END IF;
    IF COALESCE(v_context->>'mode', 'gmail_dispatch') <> 'pre_application_only' THEN
        RETURN NEW;
    END IF;

    v_candidate_id_text := v_context->>'candidate_id';
    v_profile_version_id_text := v_context->>'candidate_profile_version_id';
    v_snapshot_sha256 := v_context->>'candidate_profile_snapshot_sha256';
    v_material_bundle_sha256 := v_context->>'candidate_material_bundle_sha256';
    IF v_candidate_id_text IS NULL
       OR v_candidate_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR v_profile_version_id_text IS NULL
       OR v_profile_version_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR v_snapshot_sha256 IS NULL
       OR v_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR v_material_bundle_sha256 IS NULL
       OR v_material_bundle_sha256 !~ '^[a-f0-9]{64}$'
       OR jsonb_typeof(v_context->'candidate_profile') IS DISTINCT FROM 'object'
       OR v_context #>> '{candidate_profile,candidate_id}' IS DISTINCT FROM v_candidate_id_text
       OR v_context #>> '{candidate_profile,source_sha256}' IS DISTINCT FROM v_snapshot_sha256 THEN
        RAISE EXCEPTION 'pre-application profile approval identity is invalid'
            USING ERRCODE = '23514';
    END IF;
    v_candidate_id := v_candidate_id_text::uuid;
    v_profile_version_id := v_profile_version_id_text::uuid;

    PERFORM 1
    FROM careerops.candidates AS candidate
    WHERE candidate.id = v_candidate_id
      AND candidate.owner_user_id = NEW.actor_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'pre-application candidate is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;

    SELECT profile.id, profile.snapshot_sha256, profile.material_bundle_sha256
    INTO v_latest_profile_id, v_latest_snapshot_sha256, v_latest_material_bundle_sha256
    FROM careerops.candidate_profile_versions AS profile
    JOIN careerops.candidate_profile_decisions AS decision
      ON decision.profile_version_id = profile.id
     AND decision.owner_user_id = profile.owner_user_id
     AND decision.candidate_id = profile.candidate_id
     AND decision.snapshot_sha256 = profile.snapshot_sha256
    WHERE profile.owner_user_id = NEW.actor_user_id
      AND profile.candidate_id = v_candidate_id
      AND decision.decision = 'approve'
      AND careerops._candidate_profile_materials_active(profile.id)
    ORDER BY profile.version DESC
    LIMIT 1;
    IF v_latest_profile_id IS NULL
       OR v_latest_profile_id <> v_profile_version_id
       OR v_latest_snapshot_sha256 <> v_snapshot_sha256
       OR v_latest_material_bundle_sha256 <> v_material_bundle_sha256 THEN
        RAISE EXCEPTION 'pre-application profile is no longer the latest active approval'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_PROFILE_GUARD_FUNCTION}"
            f"{_PROFILE_GUARD_SIGNATURE} FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_PROFILE_GUARD_TRIGGER} "
            "BEFORE INSERT ON careerops.goal_run_review_decisions "
            "FOR EACH ROW EXECUTE FUNCTION "
            f"{_PROFILE_GUARD_FUNCTION}()"
        )
    )


def upgrade() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_lookup_create(
    p_actor_id uuid,
    p_idempotency_key text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_response jsonb;
    v_goal_run_id_text text;
    v_goal_run_id uuid;
    v_record jsonb;
BEGIN
    IF p_actor_id IS NULL
       OR p_idempotency_key IS NULL
       OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run create lookup command is invalid' USING ERRCODE = '22023';
    END IF;

    SELECT receipt.response_json INTO v_response
    FROM careerops.goal_run_command_receipts AS receipt
    WHERE receipt.actor_user_id = p_actor_id
      AND receipt.command_kind = 'create'
      AND receipt.idempotency_key = p_idempotency_key;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    v_goal_run_id_text := v_response #>> '{record,goal_run_id}';
    IF jsonb_typeof(v_response) <> 'object'
       OR v_goal_run_id_text IS NULL
       OR v_goal_run_id_text !~* (
           '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
           '[0-9a-f]{4}-[0-9a-f]{12}$'
       ) THEN
        RAISE EXCEPTION 'goal run create receipt is invalid' USING ERRCODE = '23514';
    END IF;
    v_goal_run_id := v_goal_run_id_text::uuid;

    SELECT careerops._goal_run_public_record(run.id) INTO v_record
    FROM careerops.goal_runs AS run
    WHERE run.id = v_goal_run_id
      AND run.actor_user_id = p_actor_id
      AND run.idempotency_key = p_idempotency_key;
    IF v_record IS NULL THEN
        RAISE EXCEPTION 'goal run create receipt is detached from its owner record'
            USING ERRCODE = '23514';
    END IF;

    RETURN jsonb_build_object(
        'record', v_record,
        'idempotency_key', p_idempotency_key,
        'newly_created', false
    );
END
$function$;
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_FUNCTION}{_SIGNATURE} FROM PUBLIC"))
    if op.get_context().as_sql or _role_exists("careerops_api"):
        op.execute(
            sa.text(
                f"GRANT EXECUTE ON FUNCTION {_FUNCTION}{_SIGNATURE} TO careerops_api"
            )
        )
    _install_preapplication_profile_guard()


def downgrade() -> None:
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS {_PROFILE_GUARD_TRIGGER} "
            "ON careerops.goal_run_review_decisions"
        )
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION IF EXISTS {_PROFILE_GUARD_FUNCTION}{_PROFILE_GUARD_SIGNATURE}"
        )
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}{_SIGNATURE}"))
