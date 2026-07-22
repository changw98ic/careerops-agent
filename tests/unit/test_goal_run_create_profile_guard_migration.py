from __future__ import annotations

import runpy
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0024_harden_preapplication_goal_run_create.py"
)


def test_create_guard_migration_follows_goal_run_create_lookup() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))

    assert module["revision"] == "0024"
    assert module["down_revision"] == "0023"
    assert module["_FUNCTION"] == "careerops.enforce_goal_run_preapplication_create"
    assert module["_SIGNATURE"] == "()"
    assert module["_TRIGGER"] == "trg_goal_runs_preapplication_create"


def test_create_guard_validates_latest_active_approved_profile_under_candidate_lock() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "BEFORE INSERT ON careerops.goal_runs" in source
    assert "NEW.context_json->>'mode' IS NULL" in source
    assert "NEW.context_json->>'mode' = 'gmail_dispatch'" in source
    assert "NEW.context_json->>'mode' <> 'pre_application_only'" in source
    assert "FROM careerops.candidates AS candidate" in source
    assert "candidate.owner_user_id = NEW.actor_user_id" in source
    assert "FOR UPDATE" in source
    assert "candidate_profile_version_id" in source
    assert "candidate_profile_snapshot_sha256" in source
    assert "candidate_material_bundle_sha256" in source
    assert "jsonb_typeof(NEW.context_json->'match_config')" in source
    assert "NEW.context_json ? 'gmail_dispatch'" in source
    assert "{candidate_profile,profile_version}" in source
    assert "decision.decision = 'approve'" in source
    assert "careerops._candidate_profile_materials_active(profile.id)" in source
    assert "ORDER BY profile.version DESC" in source
    assert "SECURITY DEFINER" in source
    assert "VOLATILE" in source
    assert "SET search_path = pg_catalog, careerops" in source


def test_create_guard_is_not_public_and_downgrade_removes_the_boundary() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "REVOKE ALL ON FUNCTION" in source
    assert "DROP TRIGGER IF EXISTS" in source
    assert "DROP FUNCTION IF EXISTS" in source
