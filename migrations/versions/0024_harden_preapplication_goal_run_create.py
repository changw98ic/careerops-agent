"""harden pre-application GoalRun creation in the database

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-22
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = "careerops.enforce_goal_run_preapplication_create"
_SIGNATURE = "()"
_TRIGGER = "trg_goal_runs_preapplication_create"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.enforce_goal_run_preapplication_create()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_candidate_id_text text;
    v_profile_version_id_text text;
    v_snapshot_sha256 text;
    v_material_bundle_sha256 text;
    v_candidate_id uuid;
    v_profile_version_id uuid;
    v_latest_profile_id uuid;
    v_latest_profile_version bigint;
    v_latest_snapshot_sha256 text;
    v_latest_material_bundle_sha256 text;
BEGIN
    IF NEW.context_json->>'mode' IS NULL
       OR NEW.context_json->>'mode' = 'gmail_dispatch' THEN
        RETURN NEW;
    END IF;
    IF NEW.context_json->>'mode' <> 'pre_application_only' THEN
        RAISE EXCEPTION 'GoalRun mode is unsupported for create'
            USING ERRCODE = '23514';
    END IF;

    v_candidate_id_text := NEW.context_json->>'candidate_id';
    v_profile_version_id_text := NEW.context_json->>'candidate_profile_version_id';
    v_snapshot_sha256 := NEW.context_json->>'candidate_profile_snapshot_sha256';
    v_material_bundle_sha256 := NEW.context_json->>'candidate_material_bundle_sha256';
    IF v_candidate_id_text IS NULL
       OR v_candidate_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR v_profile_version_id_text IS NULL
       OR v_profile_version_id_text !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR v_snapshot_sha256 IS NULL
       OR v_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR v_material_bundle_sha256 IS NULL
       OR v_material_bundle_sha256 !~ '^[a-f0-9]{64}$'
       OR jsonb_typeof(NEW.context_json->'candidate_profile') IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.context_json->'match_config') IS DISTINCT FROM 'object'
       OR NEW.context_json ? 'gmail_dispatch'
       OR NEW.context_json #>> '{candidate_profile,candidate_id}' IS DISTINCT FROM v_candidate_id_text
       OR NEW.context_json #>> '{candidate_profile,source_sha256}' IS DISTINCT FROM v_snapshot_sha256 THEN
        RAISE EXCEPTION 'pre-application GoalRun profile identity is invalid'
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
        RAISE EXCEPTION 'pre-application GoalRun candidate is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;

    SELECT profile.id, profile.version, profile.snapshot_sha256,
           profile.material_bundle_sha256
    INTO v_latest_profile_id, v_latest_profile_version, v_latest_snapshot_sha256,
         v_latest_material_bundle_sha256
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
       OR v_latest_material_bundle_sha256 <> v_material_bundle_sha256
       OR NEW.context_json #>> '{candidate_profile,profile_version}' IS DISTINCT FROM
          'candidate-profile:' || v_latest_profile_version::text || ':' ||
          v_latest_profile_id::text THEN
        RAISE EXCEPTION 'pre-application GoalRun requires the latest active approved profile'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_FUNCTION}{_SIGNATURE} FROM PUBLIC"))
    op.execute(
        sa.text(
            f"CREATE TRIGGER {_TRIGGER} "
            "BEFORE INSERT ON careerops.goal_runs "
            f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON careerops.goal_runs")
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_FUNCTION}{_SIGNATURE}"))
