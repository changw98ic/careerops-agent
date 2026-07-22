from __future__ import annotations

import runpy
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0023_add_goal_run_create_lookup.py"
)


def test_lookup_migration_is_additive_and_follows_candidate_profiles() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))

    assert module["revision"] == "0023"
    assert module["down_revision"] == "0022"
    assert module["_FUNCTION"] == "careerops.goal_run_lookup_create"
    assert module["_SIGNATURE"] == "(uuid,text)"


def test_lookup_function_is_owner_scoped_read_only_and_returns_null_on_miss() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "STABLE" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog, careerops" in source
    assert "receipt.actor_user_id = p_actor_id" in source
    assert "run.actor_user_id = p_actor_id" in source
    assert "receipt.command_kind = 'create'" in source
    assert "RETURN NULL" in source
    assert "_goal_run_public_record(run.id)" in source
    assert "INSERT INTO" not in source
    assert "UPDATE careerops" not in source
    assert "DELETE FROM" not in source


def test_lookup_function_is_not_public_and_rolls_back_cleanly() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "REVOKE ALL ON FUNCTION" in source
    assert "GRANT EXECUTE ON FUNCTION" in source
    assert "TO careerops_api" in source
    assert "DROP FUNCTION IF EXISTS" in source


def test_preapplication_approve_guard_is_database_enforced_and_serialized() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert module["_PROFILE_GUARD_TRIGGER"] == (
        "trg_goal_run_review_decisions_profile_approval"
    )
    assert "BEFORE INSERT ON careerops.goal_run_review_decisions" in source
    assert "IF NEW.decision <> 'approve'" in source
    assert "pre_application_only" in source
    assert "FROM careerops.candidates AS candidate" in source
    assert "FOR UPDATE" in source
    assert "candidate_profile_version_id" in source
    assert "candidate_profile_snapshot_sha256" in source
    assert "candidate_material_bundle_sha256" in source
    assert "_candidate_profile_materials_active(profile.id)" in source
    assert "ORDER BY profile.version DESC" in source
    assert "DROP TRIGGER IF EXISTS" in source
    assert "enforce_goal_run_preapplication_profile_approval()" in source
