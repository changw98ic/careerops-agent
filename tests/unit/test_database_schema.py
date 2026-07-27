from __future__ import annotations

import runpy
from io import StringIO
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from careerops.infrastructure.database.schema import APPEND_ONLY_TABLES, metadata

EXPECTED_TABLES = {
    "action_intents",
    "action_payload_versions",
    "application_cycles",
    "application_events",
    "application_lifecycle_events",
    "application_packages",
    "application_package_versions",
    "applications",
    "approval_requests",
    "attachment_quarantine",
    "audit_events",
    "bootstrap_tokens",
    "calendar_events",
    "candidates",
    "canonical_jobs",
    "companies",
    "compensation_records",
    "conflict_reviews",
    "console_sessions",
    "console_users",
    "contacts",
    "content_blobs",
    "content_objects",
    "crawl_plan_versions",
    "crawl_runs",
    "email_accounts",
    "email_event_proposals",
    "email_extractions",
    "email_messages",
    "email_sync_cursors",
    "email_sync_runs",
    "email_thread_links",
    "email_threads",
    "evidence_items",
    "evidence_records",
    "filter_decisions",
    "follow_up_reminders",
    "gmail_send_receipts",
    "inbox_snoozes",
    "interview_records",
    "job_aliases",
    "job_merge_decisions",
    "job_posting_assignments",
    "job_posting_versions",
    "job_postings",
    "job_sources",
    "match_results",
    "oauth_credential_references",
    "outbox_events",
    "policy_decisions",
    "profile_versions",
    "provider_receipts",
    "requirement_match_results",
    "reconciliation_records",
    "reply_drafts",
    "reply_draft_versions",
    "resume_versions",
    "agent_runs",
    "agent_run_reviews",
    "schedule_proposals",
    "send_attempts",
    "send_receipts",
    "side_effect_attempts",
}

EXPECTED_APPEND_ONLY = {
    "action_payload_versions",
    "application_events",
    "application_lifecycle_events",
    "audit_events",
    "email_extractions",
    "evidence_records",
    "job_merge_decisions",
    "job_posting_versions",
    "policy_decisions",
    "provider_receipts",
}

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0001_create_initial_business_schema.py"
)

MIGRATION_MODULE = runpy.run_path(str(MIGRATION_PATH))
MIGRATION_APPEND_ONLY = cast("tuple[str, ...]", MIGRATION_MODULE["APPEND_ONLY_TABLES"])
ROLE_GRANTS = cast(
    "dict[str, tuple[str, ...]]",
    MIGRATION_MODULE["ROLE_GRANTS"],
)

EXPECTED_API_UPDATE_GRANTS = {
    "GRANT UPDATE (display_name, updated_at) ON careerops.candidates TO careerops_api",
    "GRANT UPDATE (name, official_domains, terms_status, updated_at) "
    "ON careerops.companies TO careerops_api",
    "GRANT UPDATE (state, verified_at, last_discovery_at, updated_at) "
    "ON careerops.job_sources TO careerops_api",
    "GRANT UPDATE (aggregate_state, primary_posting_id, updated_at) "
    "ON careerops.canonical_jobs TO careerops_api",
    "GRANT UPDATE (source_state, last_seen_at, closed_at, updated_at) "
    "ON careerops.job_postings TO careerops_api",
    "GRANT UPDATE (canonical_job_id, decision_id, assigned_at) "
    "ON careerops.job_posting_assignments TO careerops_api",
    "GRANT UPDATE (status, current_payload_version_id, updated_at) "
    "ON careerops.action_intents TO careerops_api",
    "GRANT UPDATE (decision, decision_rule_reference, decided_at) "
    "ON careerops.approval_requests TO careerops_api",
}

EXPECTED_RETENTION_GRANTS = {
    "GRANT USAGE ON SCHEMA careerops TO careerops_retention",
    "GRANT INSERT (id, sha256, object_key, byte_size, deletion_state, "
    "delete_lease_owner, delete_lease_token, delete_lease_until) "
    "ON careerops.content_blobs TO careerops_retention",
    "GRANT SELECT ON careerops.content_blobs, careerops.content_objects, "
    "careerops.evidence_records, careerops.job_posting_versions TO careerops_retention",
    "GRANT UPDATE (expired_at) ON careerops.content_objects TO careerops_retention",
    "GRANT UPDATE (deletion_state, delete_lease_owner, delete_lease_token, "
    "delete_lease_until, deleted_at, delete_result) ON careerops.content_blobs "
    "TO careerops_retention",
}

EXPECTED_OUTBOX_GRANTS = {
    "GRANT USAGE ON SCHEMA careerops TO careerops_outbox",
    "GRANT SELECT ON careerops.outbox_events TO careerops_outbox",
    "GRANT UPDATE (status, available_at, lease_owner, lease_token, lease_until, "
    "attempt_count, published_at, last_error_code) ON careerops.outbox_events "
    "TO careerops_outbox",
}

EXPECTED_IDENTITY_FOREIGN_KEYS = {
    ("action_intents", "fk_action_intents_current_payload_identity"): (
        ("id", "current_payload_version_id"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
        ),
    ),
    ("canonical_jobs", "fk_canonical_jobs_primary_assignment"): (
        ("id", "primary_posting_id"),
        (
            "careerops.job_posting_assignments.canonical_job_id",
            "careerops.job_posting_assignments.job_posting_id",
        ),
    ),
    ("job_posting_assignments", "fk_job_posting_assignments_decision_identity"): (
        ("job_posting_id", "canonical_job_id", "decision_id"),
        (
            "careerops.job_merge_decisions.job_posting_id",
            "careerops.job_merge_decisions.to_canonical_job_id",
            "careerops.job_merge_decisions.id",
        ),
    ),
    ("outbox_events", "fk_outbox_events_payload_identity"): (
        ("action_intent_id", "payload_version_id"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
        ),
    ),
    ("policy_decisions", "fk_policy_decisions_payload_identity"): (
        ("action_intent_id", "payload_version_id", "payload_hash"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
            "careerops.action_payload_versions.payload_hash",
        ),
    ),
}


def test_initial_business_schema_table_contract() -> None:
    assert {table.name for table in metadata.tables.values()} == EXPECTED_TABLES


def test_append_only_contract_is_explicit() -> None:
    assert set(APPEND_ONLY_TABLES) == EXPECTED_APPEND_ONLY
    # The initial migration (0001) predates M3; application_lifecycle_events was added in 0005.
    assert set(MIGRATION_APPEND_ONLY) <= EXPECTED_APPEND_ONLY
    assert EXPECTED_APPEND_ONLY <= EXPECTED_TABLES


def test_initial_migration_can_render_offline_sql() -> None:
    output = StringIO()
    config = Config(
        str(Path(__file__).resolve().parents[2] / "alembic.ini"),
        output_buffer=output,
    )

    command.upgrade(config, "head", sql=True)

    rendered_sql = output.getvalue()
    assert "CREATE SCHEMA careerops" in rendered_sql
    assert "GRANT USAGE ON SCHEMA careerops TO careerops_retention" in rendered_sql
    # M5A.5: the independent side-effect worker role receives minimal grants.
    assert "GRANT USAGE ON SCHEMA careerops TO careerops_side_effect" in rendered_sql


def test_m0_role_grants_are_minimal_and_column_scoped() -> None:
    assert set(ROLE_GRANTS) == {
        "careerops_api",
        "careerops_outbox",
        "careerops_readonly",
        "careerops_retention",
        "careerops_side_effect",
    }

    api_update_grants = {
        statement
        for statement in ROLE_GRANTS["careerops_api"]
        if statement.startswith("GRANT UPDATE")
    }
    assert api_update_grants == EXPECTED_API_UPDATE_GRANTS
    assert set(ROLE_GRANTS["careerops_retention"]) == EXPECTED_RETENTION_GRANTS
    assert set(ROLE_GRANTS["careerops_outbox"]) == EXPECTED_OUTBOX_GRANTS
    assert ROLE_GRANTS["careerops_side_effect"] == ()
    api_insert_grants = {
        statement
        for statement in ROLE_GRANTS["careerops_api"]
        if statement.startswith("GRANT INSERT (")
    }
    assert {
        "GRANT INSERT (id, action_kind, resource_type, resource_id, idempotency_key, created_by) "
        "ON careerops.action_intents TO careerops_api",
        "GRANT INSERT (id, sha256, object_key, byte_size) "
        "ON careerops.content_blobs TO careerops_api",
        "GRANT INSERT (id, blob_id, media_type, classification, owner_resource_type, "
        "owner_resource_id, retention_until) ON careerops.content_objects TO careerops_api",
        "GRANT INSERT (id, action_intent_id, payload_version_id, policy_decision_id, "
        "requested_for, expires_at) ON careerops.approval_requests TO careerops_api",
        "GRANT INSERT (id, event_key, action_intent_id, payload_version_id, event_type, "
        "available_at) ON careerops.outbox_events TO careerops_api",
    } <= api_insert_grants
    broad_api_insert = " ".join(
        statement
        for statement in ROLE_GRANTS["careerops_api"]
        if statement.startswith("GRANT INSERT ON")
    )
    assert "careerops.content_blobs" not in broad_api_insert
    assert "careerops.content_objects" not in broad_api_insert
    assert "careerops.approval_requests" not in broad_api_insert
    assert "careerops.outbox_events" not in broad_api_insert
    assert "careerops.action_intents" not in broad_api_insert
    assert not any(
        statement.startswith("GRANT UPDATE ON") or statement.startswith("GRANT ALL")
        for statements in ROLE_GRANTS.values()
        for statement in statements
    )
    assert "secret_handle" not in " ".join(
        statement for statements in ROLE_GRANTS.values() for statement in statements
    )


def test_identity_chain_uses_composite_foreign_keys() -> None:
    for (table_name, constraint_name), expected in EXPECTED_IDENTITY_FOREIGN_KEYS.items():
        table = metadata.tables[f"careerops.{table_name}"]
        constraint = next(
            item
            for item in table.constraints
            if isinstance(item, sa.ForeignKeyConstraint) and item.name == constraint_name
        )
        local_columns = tuple(constraint.column_keys)
        remote_columns = tuple(element.target_fullname for element in constraint.elements)

        assert (local_columns, remote_columns) == expected


def test_content_metadata_and_outbox_state_are_database_enforced() -> None:
    blob = metadata.tables["careerops.content_blobs"]
    blob_checks = {
        constraint.name
        for constraint in blob.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_content_blobs_deletion_state_consistent",
        "ck_content_blobs_delete_result_values",
        "ck_content_blobs_object_key_matches_sha256",
        "ck_content_blobs_sha256_format",
    } <= blob_checks

    content = metadata.tables["careerops.content_objects"]
    assert {"blob_id", "owner_resource_type", "owner_resource_id", "retired_at"} <= set(
        content.c.keys()
    )
    assert {"sha256", "object_key", "byte_size"}.isdisjoint(content.c.keys())
    content_checks = {
        constraint.name
        for constraint in content.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_content_objects_classification_values",
        "ck_content_objects_quarantine_retention_limit",
    } <= content_checks
    classification_check = next(
        constraint
        for constraint in content.constraints
        if constraint.name == "ck_content_objects_classification_values"
    )
    assert isinstance(classification_check, sa.CheckConstraint)
    assert "model_debug" not in str(classification_check.sqltext)

    outbox = metadata.tables["careerops.outbox_events"]
    assert {"event_key", "lease_token"} <= set(outbox.c.keys())
    outbox_checks = {
        constraint.name
        for constraint in outbox.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_outbox_events_event_type_values",
        "ck_outbox_events_lease_state_consistent",
        "ck_outbox_events_published_state_consistent",
    } <= outbox_checks
    lease_check = next(
        constraint
        for constraint in outbox.constraints
        if constraint.name == "ck_outbox_events_lease_state_consistent"
    )
    assert isinstance(lease_check, sa.CheckConstraint)
    assert "lease_token" in str(lease_check.sqltext)
