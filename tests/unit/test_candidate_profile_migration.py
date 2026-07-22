from __future__ import annotations

import runpy
from pathlib import Path
from typing import cast

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0022_add_candidate_profile_materials.py"
)


def test_candidate_profile_migration_follows_pre_application_revision() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))

    assert module["revision"] == "0022"
    assert module["down_revision"] == "0021"
    assert set(cast("tuple[str, ...]", module["APPEND_ONLY_TABLES"])) == {
        "candidate_material_bundles",
        "candidate_material_versions",
        "candidate_profile_decisions",
        "candidate_profile_versions",
    }


def test_migration_owner_binds_candidates_and_existing_channel_accounts() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "trg_candidates_fill_owner" in source
    assert "candidate has conflicting owner bindings" in source
    assert "FOREIGN KEY (owner_user_id, candidate_id)" in source
    assert "REFERENCES careerops.candidates (owner_user_id, id)" in source
    assert "NOT VALID" in source
    assert cast("tuple[str, ...]", module["_RUNTIME_ROLES"])
    for table_name in (
        "gmail_accounts",
        "gmail_send_accounts",
        "greenhouse_submit_accounts",
    ):
        assert table_name in source
    assert 'constraint_name = f"fk_{table_name}_owner_candidate"' in source


def test_migration_binds_approved_hashes_and_active_cas_objects_in_database() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "uq_candidate_material_bundles_hash_identity" in source
    assert (
        '["material_bundle_id", "owner_user_id", "candidate_id", '
        '"material_bundle_sha256"]' in source
    )
    assert "uq_candidate_profile_versions_hash_identity" in source
    assert (
        '["profile_version_id", "owner_user_id", "candidate_id", "snapshot_sha256"]'
        in source
    )
    assert "content.classification = 'accepted_attachment'" in source
    assert "content.owner_resource_type = 'candidate'" in source
    assert "content.retention_until > CURRENT_TIMESTAMP" in source
    assert "blob.deletion_state = 'active'" in source
    assert "uq_candidate_material_versions_one_resume" in source
    assert "exactly one resume" in source


def test_runtime_roles_use_definer_functions_without_direct_new_table_writes() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    api_grants = cast("tuple[str, ...]", module["_API_GRANTS"])
    workflow_grants = cast("tuple[str, ...]", module["_WORKFLOW_GRANTS"])

    assert all("GRANT EXECUTE ON FUNCTION" in statement for statement in api_grants)
    assert workflow_grants == (
        "GRANT EXECUTE ON FUNCTION "
        "careerops.candidate_profile_get_approved(uuid,uuid) TO careerops_workflow",
    )
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog, careerops" in source
    assert "REVOKE ALL ON FUNCTION" in source
    assert "GRANT INSERT ON careerops.candidate_" not in source
    assert "GRANT UPDATE ON careerops.candidate_" not in source
    assert "GRANT DELETE ON careerops.candidate_" not in source


def test_migration_keeps_material_and_profile_tables_append_only() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "careerops.reject_append_only_mutation()" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert 'f"DROP FUNCTION IF EXISTS {function}{signature}"' in source
    assert 'op.drop_column("candidates", "owner_user_id"' in source


def test_profile_decisions_serialize_on_owner_candidate_before_idempotency_lookup() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    decide_start = source.index("CREATE FUNCTION careerops.candidate_profile_decide(")
    decide_end = source.index(
        "def _install_query_functions()",
        decide_start,
    )
    decide_source = source[decide_start:decide_end]

    candidate_lock = decide_source.index("FROM careerops.candidates AS candidate")
    receipt_lookup = decide_source.index("FROM careerops.candidate_profile_decisions")
    profile_lock = decide_source.index("FROM careerops.candidate_profile_versions")
    assert candidate_lock < receipt_lookup < profile_lock
    assert "candidate.owner_user_id = p_actor_id" in decide_source
    assert decide_source.count("FOR UPDATE") >= 2
