from __future__ import annotations

import logging
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
    "application_events",
    "approval_requests",
    "audit_events",
    "autopilot_campaigns",
    "autopilot_cap_reservations",
    "autopilot_grant_revocations",
    "autopilot_grant_versions",
    "autopilot_intent_authorizations",
    "autopilot_kill_switch_events",
    "autopilot_review_items",
    "bootstrap_tokens",
    "candidate_material_bundles",
    "candidate_material_versions",
    "candidate_profile_decisions",
    "candidate_profile_versions",
    "candidates",
    "canonical_jobs",
    "companies",
    "console_sessions",
    "console_users",
    "content_blobs",
    "content_objects",
    "crawler_execution_approvals",
    "crawler_execution_dispatches",
    "crawler_execution_requests",
    "crawler_execution_results",
    "crawler_job_deduplication_keys",
    "crawler_job_ingestion_evidence",
    "crawler_source_registries",
    "crawler_source_registry_sources",
    "crawler_source_runs",
    "evidence_records",
    "goal_run_checkpoints",
    "goal_run_command_receipts",
    "goal_run_events",
    "goal_run_gmail_compositions",
    "goal_run_review_decisions",
    "goal_run_review_items",
    "goal_runs",
    "gmail_accounts",
    "gmail_command_receipts",
    "gmail_message_signals",
    "gmail_send_accounts",
    "gmail_send_command_receipts",
    "gmail_send_drafts",
    "gmail_send_reconciliation_jobs",
    "gmail_send_reservations",
    "gmail_send_review_evidence",
    "greenhouse_submit_accounts",
    "greenhouse_submit_command_receipts",
    "greenhouse_submit_reconciliation_evidence",
    "greenhouse_submit_reconciliation_jobs",
    "greenhouse_submit_reconciliation_reviews",
    "greenhouse_submit_review_evidence",
    "gmail_signal_proposals",
    "gmail_signal_review_decisions",
    "gmail_sync_runs",
    "job_aliases",
    "job_merge_decisions",
    "job_posting_assignments",
    "job_posting_versions",
    "job_postings",
    "job_sources",
    "oauth_credential_references",
    "outbox_events",
    "policy_decisions",
    "provider_receipts",
    "release_qualification_decisions",
    "release_qualification_evidence",
    "release_qualifications",
    "side_effect_attempts",
}

EXPECTED_APPEND_ONLY = {
    "action_payload_versions",
    "application_events",
    "audit_events",
    "autopilot_campaigns",
    "autopilot_cap_reservations",
    "autopilot_grant_revocations",
    "autopilot_grant_versions",
    "autopilot_intent_authorizations",
    "autopilot_kill_switch_events",
    "autopilot_review_items",
    "candidate_material_bundles",
    "candidate_material_versions",
    "candidate_profile_decisions",
    "candidate_profile_versions",
    "crawler_execution_approvals",
    "crawler_execution_dispatches",
    "crawler_execution_requests",
    "crawler_execution_results",
    "crawler_job_deduplication_keys",
    "crawler_job_ingestion_evidence",
    "crawler_source_registries",
    "crawler_source_runs",
    "evidence_records",
    "goal_run_checkpoints",
    "goal_run_command_receipts",
    "goal_run_events",
    "goal_run_review_decisions",
    "goal_run_review_items",
    "gmail_command_receipts",
    "gmail_message_signals",
    "gmail_send_command_receipts",
    "gmail_send_reservations",
    "gmail_send_review_evidence",
    "greenhouse_submit_command_receipts",
    "greenhouse_submit_reconciliation_evidence",
    "greenhouse_submit_reconciliation_reviews",
    "greenhouse_submit_review_evidence",
    "gmail_signal_proposals",
    "gmail_signal_review_decisions",
    "job_merge_decisions",
    "job_posting_versions",
    "policy_decisions",
    "provider_receipts",
    "release_qualification_decisions",
    "release_qualification_evidence",
    "release_qualifications",
}

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0001_create_initial_business_schema.py"
)
AUTOPILOT_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0004_add_autopilot_control_plane.py"
)
AUTOPILOT_RESERVATION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0005_add_autopilot_cap_reservations.py"
)
CRAWLER_EXECUTION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0006_add_crawler_execution_control_plane.py"
)
CRAWLER_EXECUTION_RESULTS_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0007_add_crawler_execution_results.py"
)
SYNTHETIC_SUBMISSION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0008_add_synthetic_submission_execution.py"
)
RELEASE_QUALIFICATION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0009_add_release_qualification_control_plane.py"
)
REGISTRY_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0010_add_crawler_source_registry.py"
)
GOAL_RUN_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0011_add_goal_runs.py"
)
GMAIL_READONLY_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0012_add_gmail_readonly_sync.py"
)
GMAIL_SEND_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0013_add_gmail_send_channel.py"
)
GREENHOUSE_SUBMIT_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0014_add_greenhouse_submit_channel.py"
)
GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0015_add_goal_run_gmail_composition.py"
)
CANDIDATE_PROFILE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0022_add_candidate_profile_materials.py"
)

MIGRATION_MODULE = runpy.run_path(str(MIGRATION_PATH))
MIGRATION_APPEND_ONLY = cast("tuple[str, ...]", MIGRATION_MODULE["APPEND_ONLY_TABLES"])
ROLE_GRANTS = cast(
    "dict[str, tuple[str, ...]]",
    MIGRATION_MODULE["ROLE_GRANTS"],
)
AUTOPILOT_MIGRATION_MODULE = runpy.run_path(str(AUTOPILOT_MIGRATION_PATH))
AUTOPILOT_MIGRATION_APPEND_ONLY = cast(
    "tuple[str, ...]", AUTOPILOT_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
AUTOPILOT_API_GRANTS = cast("tuple[str, ...]", AUTOPILOT_MIGRATION_MODULE["_API_GRANTS"])
AUTOPILOT_READONLY_GRANTS = cast("tuple[str, ...]", AUTOPILOT_MIGRATION_MODULE["_READONLY_GRANTS"])
AUTOPILOT_RESERVATION_MIGRATION_MODULE = runpy.run_path(str(AUTOPILOT_RESERVATION_MIGRATION_PATH))
AUTOPILOT_RESERVATION_APPEND_ONLY = cast(
    "tuple[str, ...]", AUTOPILOT_RESERVATION_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
AUTOPILOT_RESERVATION_API_GRANTS = cast(
    "tuple[str, ...]", AUTOPILOT_RESERVATION_MIGRATION_MODULE["_API_GRANTS"]
)
AUTOPILOT_RESERVATION_READONLY_GRANTS = cast(
    "tuple[str, ...]", AUTOPILOT_RESERVATION_MIGRATION_MODULE["_READONLY_GRANTS"]
)
CRAWLER_EXECUTION_MIGRATION_MODULE = runpy.run_path(str(CRAWLER_EXECUTION_MIGRATION_PATH))
CRAWLER_EXECUTION_APPEND_ONLY = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
CRAWLER_EXECUTION_API_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_MIGRATION_MODULE["_API_GRANTS"]
)
CRAWLER_EXECUTION_OUTBOX_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_MIGRATION_MODULE["_OUTBOX_GRANTS"]
)
CRAWLER_EXECUTION_READONLY_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_MIGRATION_MODULE["_READONLY_GRANTS"]
)
CRAWLER_EXECUTION_RESULTS_MIGRATION_MODULE = runpy.run_path(
    str(CRAWLER_EXECUTION_RESULTS_MIGRATION_PATH)
)
CRAWLER_EXECUTION_RESULTS_API_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_RESULTS_MIGRATION_MODULE["_API_GRANTS"]
)
CRAWLER_EXECUTION_RESULTS_OUTBOX_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_RESULTS_MIGRATION_MODULE["_OUTBOX_GRANTS"]
)
CRAWLER_EXECUTION_RESULTS_READONLY_GRANTS = cast(
    "tuple[str, ...]", CRAWLER_EXECUTION_RESULTS_MIGRATION_MODULE["_READONLY_GRANTS"]
)
SYNTHETIC_SUBMISSION_MIGRATION_SOURCE = SYNTHETIC_SUBMISSION_MIGRATION_PATH.read_text(
    encoding="utf-8"
)
SYNTHETIC_SUBMISSION_MIGRATION_MODULE = runpy.run_path(str(SYNTHETIC_SUBMISSION_MIGRATION_PATH))
SYNTHETIC_SUBMISSION_APPEND_ONLY = cast(
    "tuple[str, ...]", SYNTHETIC_SUBMISSION_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
SYNTHETIC_SUBMISSION_API_GRANTS = cast(
    "tuple[str, ...]", SYNTHETIC_SUBMISSION_MIGRATION_MODULE["_API_GRANTS"]
)
SYNTHETIC_SUBMISSION_OUTBOX_GRANTS = cast(
    "tuple[str, ...]", SYNTHETIC_SUBMISSION_MIGRATION_MODULE["_OUTBOX_GRANTS"]
)
SYNTHETIC_SUBMISSION_READONLY_GRANTS = cast(
    "tuple[str, ...]", SYNTHETIC_SUBMISSION_MIGRATION_MODULE["_READONLY_GRANTS"]
)
RELEASE_QUALIFICATION_MIGRATION_SOURCE = RELEASE_QUALIFICATION_MIGRATION_PATH.read_text(
    encoding="utf-8"
)
RELEASE_QUALIFICATION_MIGRATION_MODULE = runpy.run_path(str(RELEASE_QUALIFICATION_MIGRATION_PATH))
RELEASE_QUALIFICATION_APPEND_ONLY = cast(
    "tuple[str, ...]", RELEASE_QUALIFICATION_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
RELEASE_QUALIFICATION_API_GRANTS = cast(
    "tuple[str, ...]", RELEASE_QUALIFICATION_MIGRATION_MODULE["_API_GRANTS"]
)
RELEASE_QUALIFICATION_READONLY_GRANTS = cast(
    "tuple[str, ...]", RELEASE_QUALIFICATION_MIGRATION_MODULE["_READONLY_GRANTS"]
)
REGISTRY_MIGRATION_SOURCE = REGISTRY_MIGRATION_PATH.read_text(encoding="utf-8")
REGISTRY_MIGRATION_MODULE = runpy.run_path(str(REGISTRY_MIGRATION_PATH))
REGISTRY_APPEND_ONLY = cast("tuple[str, ...]", REGISTRY_MIGRATION_MODULE["APPEND_ONLY_TABLES"])
REGISTRY_API_GRANTS = cast("tuple[str, ...]", REGISTRY_MIGRATION_MODULE["_API_GRANTS"])
REGISTRY_READONLY_GRANTS = cast("tuple[str, ...]", REGISTRY_MIGRATION_MODULE["_READONLY_GRANTS"])
GOAL_RUN_MIGRATION_SOURCE = GOAL_RUN_MIGRATION_PATH.read_text(encoding="utf-8")
GOAL_RUN_MIGRATION_MODULE = runpy.run_path(str(GOAL_RUN_MIGRATION_PATH))
GOAL_RUN_APPEND_ONLY = cast("tuple[str, ...]", GOAL_RUN_MIGRATION_MODULE["APPEND_ONLY_TABLES"])
GOAL_RUN_API_GRANTS = cast("tuple[str, ...]", GOAL_RUN_MIGRATION_MODULE["_API_GRANTS"])
GOAL_RUN_WORKFLOW_GRANTS = cast("tuple[str, ...]", GOAL_RUN_MIGRATION_MODULE["_WORKFLOW_GRANTS"])
GOAL_RUN_READONLY_GRANTS = cast("tuple[str, ...]", GOAL_RUN_MIGRATION_MODULE["_READONLY_GRANTS"])
GMAIL_READONLY_MIGRATION_SOURCE = GMAIL_READONLY_MIGRATION_PATH.read_text(encoding="utf-8")
GMAIL_READONLY_MIGRATION_MODULE = runpy.run_path(str(GMAIL_READONLY_MIGRATION_PATH))
GMAIL_READONLY_APPEND_ONLY = cast(
    "tuple[str, ...]", GMAIL_READONLY_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
GMAIL_READONLY_API_GRANTS = cast("tuple[str, ...]", GMAIL_READONLY_MIGRATION_MODULE["_API_GRANTS"])
GMAIL_READONLY_MAILBOX_GRANTS = cast(
    "tuple[str, ...]", GMAIL_READONLY_MIGRATION_MODULE["_MAILBOX_GRANTS"]
)
GMAIL_READONLY_READONLY_GRANTS = cast(
    "tuple[str, ...]", GMAIL_READONLY_MIGRATION_MODULE["_READONLY_GRANTS"]
)
GMAIL_SEND_MIGRATION_SOURCE = GMAIL_SEND_MIGRATION_PATH.read_text(encoding="utf-8")
GMAIL_SEND_MIGRATION_MODULE = runpy.run_path(str(GMAIL_SEND_MIGRATION_PATH))
GMAIL_SEND_APPEND_ONLY = cast("tuple[str, ...]", GMAIL_SEND_MIGRATION_MODULE["APPEND_ONLY_TABLES"])
GMAIL_SEND_API_GRANTS = cast("tuple[str, ...]", GMAIL_SEND_MIGRATION_MODULE["_API_GRANTS"])
GMAIL_SEND_MAIL_SENDER_GRANTS = cast(
    "tuple[str, ...]", GMAIL_SEND_MIGRATION_MODULE["_MAIL_SENDER_GRANTS"]
)
GMAIL_SEND_READONLY_GRANTS = cast(
    "tuple[str, ...]", GMAIL_SEND_MIGRATION_MODULE["_READONLY_GRANTS"]
)
GREENHOUSE_SUBMIT_MIGRATION_SOURCE = GREENHOUSE_SUBMIT_MIGRATION_PATH.read_text(encoding="utf-8")
GREENHOUSE_SUBMIT_MIGRATION_MODULE = runpy.run_path(str(GREENHOUSE_SUBMIT_MIGRATION_PATH))
GREENHOUSE_SUBMIT_APPEND_ONLY = cast(
    "tuple[str, ...]", GREENHOUSE_SUBMIT_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE = GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_PATH.read_text(
    encoding="utf-8"
)
GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_MODULE = runpy.run_path(
    str(GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_PATH)
)
GOAL_RUN_GMAIL_WORKFLOW_GRANTS = cast(
    "tuple[str, ...]", GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_MODULE["_WORKFLOW_GRANTS"]
)
CANDIDATE_PROFILE_MIGRATION_MODULE = runpy.run_path(str(CANDIDATE_PROFILE_MIGRATION_PATH))
CANDIDATE_PROFILE_APPEND_ONLY = cast(
    "tuple[str, ...]", CANDIDATE_PROFILE_MIGRATION_MODULE["APPEND_ONLY_TABLES"]
)
EXPECTED_GOAL_RUN_WORKFLOW_UPDATE_GRANTS = {
    "GRANT UPDATE (last_discovery_at, updated_at) ON careerops.job_sources TO careerops_workflow",
    "GRANT UPDATE (primary_posting_id, updated_at) "
    "ON careerops.canonical_jobs TO careerops_workflow",
    "GRANT UPDATE (source_state, last_seen_at, closed_at, updated_at) "
    "ON careerops.job_postings TO careerops_workflow",
}

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
    (
        "autopilot_intent_authorizations",
        "fk_autopilot_intent_authorizations_grant_identity",
    ): (
        ("campaign_id", "grant_version_id"),
        (
            "careerops.autopilot_grant_versions.campaign_id",
            "careerops.autopilot_grant_versions.id",
        ),
    ),
    (
        "autopilot_intent_authorizations",
        "fk_autopilot_intent_authorizations_payload_identity",
    ): (
        ("action_intent_id", "payload_version_id", "payload_hash"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
            "careerops.action_payload_versions.payload_hash",
        ),
    ),
    (
        "autopilot_intent_authorizations",
        "fk_autopilot_intent_authorizations_policy_identity",
    ): (
        ("action_intent_id", "payload_version_id", "policy_decision_id"),
        (
            "careerops.policy_decisions.action_intent_id",
            "careerops.policy_decisions.payload_version_id",
            "careerops.policy_decisions.id",
        ),
    ),
    (
        "autopilot_cap_reservations",
        "fk_autopilot_cap_reservations_grant_identity",
    ): (
        ("campaign_id", "grant_version_id"),
        (
            "careerops.autopilot_grant_versions.campaign_id",
            "careerops.autopilot_grant_versions.id",
        ),
    ),
    (
        "autopilot_cap_reservations",
        "fk_autopilot_cap_reservations_payload_identity",
    ): (
        ("action_intent_id", "payload_version_id", "payload_hash"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
            "careerops.action_payload_versions.payload_hash",
        ),
    ),
    (
        "crawler_execution_requests",
        "fk_crawler_execution_requests_payload_identity",
    ): (
        ("action_intent_id", "payload_version_id", "payload_hash"),
        (
            "careerops.action_payload_versions.action_intent_id",
            "careerops.action_payload_versions.id",
            "careerops.action_payload_versions.payload_hash",
        ),
    ),
    (
        "crawler_execution_dispatches",
        "fk_crawler_execution_dispatches_request_identity",
    ): (
        ("request_id", "action_intent_id"),
        (
            "careerops.crawler_execution_requests.id",
            "careerops.crawler_execution_requests.action_intent_id",
        ),
    ),
    (
        "crawler_execution_dispatches",
        "fk_crawler_execution_dispatches_outbox_identity",
    ): (
        ("action_intent_id", "outbox_event_id"),
        (
            "careerops.outbox_events.action_intent_id",
            "careerops.outbox_events.id",
        ),
    ),
    (
        "crawler_execution_results",
        "fk_crawler_execution_results_request_identity",
    ): (
        ("request_id", "action_intent_id"),
        (
            "careerops.crawler_execution_requests.id",
            "careerops.crawler_execution_requests.action_intent_id",
        ),
    ),
    (
        "crawler_execution_results",
        "fk_crawler_execution_results_outbox_identity",
    ): (
        ("action_intent_id", "outbox_event_id"),
        (
            "careerops.outbox_events.action_intent_id",
            "careerops.outbox_events.id",
        ),
    ),
}


def test_initial_business_schema_table_contract() -> None:
    assert {table.name for table in metadata.tables.values()} == EXPECTED_TABLES


def test_append_only_contract_is_explicit() -> None:
    assert set(APPEND_ONLY_TABLES) == EXPECTED_APPEND_ONLY
    assert set(MIGRATION_APPEND_ONLY) == (
        EXPECTED_APPEND_ONLY
        - set(AUTOPILOT_MIGRATION_APPEND_ONLY)
        - set(AUTOPILOT_RESERVATION_APPEND_ONLY)
        - set(CRAWLER_EXECUTION_APPEND_ONLY)
        - {"crawler_execution_results"}
        - set(SYNTHETIC_SUBMISSION_APPEND_ONLY)
        - set(RELEASE_QUALIFICATION_APPEND_ONLY)
        - set(REGISTRY_APPEND_ONLY)
        - set(GOAL_RUN_APPEND_ONLY)
        - set(GMAIL_READONLY_APPEND_ONLY)
        - set(GMAIL_SEND_APPEND_ONLY)
        - set(GREENHOUSE_SUBMIT_APPEND_ONLY)
        - set(CANDIDATE_PROFILE_APPEND_ONLY)
    )
    assert set(GMAIL_READONLY_APPEND_ONLY) == {
        "gmail_command_receipts",
        "gmail_message_signals",
        "gmail_signal_proposals",
        "gmail_signal_review_decisions",
    }
    assert set(GMAIL_SEND_APPEND_ONLY) == {
        "gmail_send_command_receipts",
        "gmail_send_reservations",
        "gmail_send_review_evidence",
    }
    assert set(AUTOPILOT_MIGRATION_APPEND_ONLY) == {
        "autopilot_campaigns",
        "autopilot_grant_revocations",
        "autopilot_grant_versions",
        "autopilot_intent_authorizations",
        "autopilot_review_items",
    }
    assert set(AUTOPILOT_RESERVATION_APPEND_ONLY) == {"autopilot_cap_reservations"}
    assert set(CRAWLER_EXECUTION_APPEND_ONLY) == {
        "crawler_execution_approvals",
        "crawler_execution_dispatches",
        "crawler_execution_requests",
    }
    assert CRAWLER_EXECUTION_RESULTS_MIGRATION_MODULE["_RESULT_TABLE"] == (
        "crawler_execution_results"
    )
    assert set(SYNTHETIC_SUBMISSION_APPEND_ONLY) == {"autopilot_kill_switch_events"}
    assert set(RELEASE_QUALIFICATION_APPEND_ONLY) == {
        "release_qualifications",
        "release_qualification_evidence",
        "release_qualification_decisions",
    }
    assert set(REGISTRY_APPEND_ONLY) == {
        "crawler_job_deduplication_keys",
        "crawler_job_ingestion_evidence",
        "crawler_source_registries",
        "crawler_source_runs",
    }
    assert set(GOAL_RUN_APPEND_ONLY) == {
        "goal_run_checkpoints",
        "goal_run_command_receipts",
        "goal_run_events",
        "goal_run_review_decisions",
        "goal_run_review_items",
    }
    assert set(CANDIDATE_PROFILE_APPEND_ONLY) == {
        "candidate_material_bundles",
        "candidate_material_versions",
        "candidate_profile_decisions",
        "candidate_profile_versions",
    }
    assert EXPECTED_APPEND_ONLY <= EXPECTED_TABLES


def test_initial_migration_can_render_offline_sql() -> None:
    application_logger = logging.getLogger("careerops.web.security")
    application_logger.disabled = False
    output = StringIO()
    config = Config(
        str(Path(__file__).resolve().parents[2] / "alembic.ini"),
        output_buffer=output,
    )

    command.upgrade(config, "head", sql=True)

    assert application_logger.disabled is False
    rendered_sql = output.getvalue()
    assert "CREATE SCHEMA careerops" in rendered_sql
    assert "GRANT USAGE ON SCHEMA careerops TO careerops_retention" in rendered_sql
    assert "GRANT USAGE ON SCHEMA careerops TO careerops_side_effect" not in rendered_sql
    assert "CREATE TABLE careerops.autopilot_grant_versions" in rendered_sql
    assert "CREATE TRIGGER trg_autopilot_review_items_append_only" in rendered_sql
    assert "CREATE TABLE careerops.autopilot_cap_reservations" in rendered_sql
    assert "CREATE TRIGGER trg_autopilot_cap_reservations_append_only" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_autopilot_cap_reservation_insert()" in rendered_sql
    assert "CREATE TRIGGER trg_autopilot_cap_reservations_insert_guard" in rendered_sql
    assert "CREATE TABLE careerops.crawler_execution_requests" in rendered_sql
    assert "CREATE TABLE careerops.crawler_execution_approvals" in rendered_sql
    assert "CREATE TABLE careerops.crawler_execution_dispatches" in rendered_sql
    assert "CREATE TABLE careerops.crawler_execution_results" in rendered_sql
    assert "CREATE TABLE careerops.autopilot_kill_switch_events" in rendered_sql
    assert "CREATE TABLE careerops.release_qualifications" in rendered_sql
    assert "CREATE TABLE careerops.release_qualification_evidence" in rendered_sql
    assert "CREATE TABLE careerops.release_qualification_decisions" in rendered_sql
    assert "CREATE FUNCTION careerops.serialize_autopilot_kill_switch_event_insert()" in (
        rendered_sql
    )
    assert "CREATE TRIGGER trg_autopilot_kill_switch_events_serialize_insert" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_release_qualification_decision_insert()" in (
        rendered_sql
    )
    assert "CREATE FUNCTION careerops.enforce_release_qualification_evidence_insert()" in (
        rendered_sql
    )
    assert "CREATE TABLE careerops.goal_run_gmail_compositions" in rendered_sql
    assert "CREATE FUNCTION careerops.goal_run_record_gmail_match(" in rendered_sql
    assert "CREATE FUNCTION careerops.goal_run_prepare_gmail(" in rendered_sql
    assert "CREATE FUNCTION careerops.goal_run_dispatch_gmail(" in rendered_sql
    assert "CREATE FUNCTION careerops.goal_run_inspect_gmail(" in rendered_sql
    assert "CREATE TRIGGER trg_release_qualification_evidence_insert_guard" in rendered_sql
    assert "CREATE TRIGGER trg_release_qualification_decisions_insert_guard" in rendered_sql
    assert "CREATE TRIGGER trg_crawler_execution_requests_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_crawler_execution_approvals_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_crawler_execution_dispatches_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_crawler_execution_results_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_release_qualifications_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_release_qualification_evidence_append_only" in rendered_sql
    assert "CREATE TRIGGER trg_release_qualification_decisions_append_only" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_crawler_execution_approval_insert()" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_crawler_execution_dispatch_insert()" in rendered_sql
    assert "CREATE FUNCTION careerops.complete_crawler_execution_outbox_event(" in rendered_sql
    assert "CREATE FUNCTION careerops.prepare_synthetic_submission_outbox_event(" in rendered_sql
    assert "CREATE FUNCTION careerops.record_synthetic_submission_outbox_receipt(" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_autopilot_cap_reservation_execution_boundary()" in (
        rendered_sql
    )
    assert "NEW.decided_at := CURRENT_TIMESTAMP" in rendered_sql
    assert "NEW.dispatched_at := CURRENT_TIMESTAMP" in rendered_sql
    assert "NEW.decided_by_user_id <> request_owner_user_id" in rendered_sql
    assert "approval_decision IS DISTINCT FROM 'approved'" in rendered_sql
    assert "outbox_event_type <> 'workflow_signal'" in rendered_sql
    assert "outbox_event_key <> NEW.execution_key" in rendered_sql
    assert "NEW.execution_key NOT LIKE 'crawler-execution:%'" in rendered_sql
    assert "v_event_status <> 'leased'" in rendered_sql
    assert "SET status = 'confirmed'" in rendered_sql
    assert "SET status = 'failed'" in rendered_sql
    assert "SET status = p_outcome" in rendered_sql
    assert "'reconciliation_required'" in rendered_sql
    assert "INSERT INTO careerops.crawler_execution_results" in rendered_sql
    assert (
        "GRANT EXECUTE ON FUNCTION careerops.complete_crawler_execution_outbox_event"
        in rendered_sql
    )
    assert "manifest_path !~ '(^|/)\\.\\.(/|$)'" in rendered_sql
    assert "request_artifact_path !~ '(^|/)\\.\\.(/|$)'" in rendered_sql
    assert "approval_artifact_path !~ '(^|/)\\.\\.(/|$)'" in rendered_sql
    assert "FOR UPDATE" in rendered_sql
    assert "authorization_outcome <> 'allow_autopilot_submission'" in rendered_sql
    assert "total_reserved >= grant_max_total" in rendered_sql
    assert "careerops:autopilot-cap-reservation:" in rendered_sql
    assert "daily_reserved >= grant_max_daily" in rendered_sql
    assert "company_reserved >= grant_max_per_company" in rendered_sql
    assert "NEW.reserved_at := CURRENT_TIMESTAMP" in rendered_sql
    assert "NEW.reservation_date := CURRENT_DATE" in rendered_sql
    assert "right(lower(NEW.target_host), 5) <> '.test'" in rendered_sql
    assert "NEW.channel NOT LIKE 'synthetic:%'" in rendered_sql
    assert "NEW.channel <> ('synthetic:' || NEW.adapter_id)" in rendered_sql
    assert "payload_target ->> 'target_host' IS DISTINCT FROM NEW.target_host" in rendered_sql
    assert "payload_target ->> 'channel' IS DISTINCT FROM NEW.channel" in rendered_sql
    assert "grant_material_hashes @> jsonb_build_array" in rendered_sql
    assert "policy_decision <> 'allow_autopilot_submission'" in rendered_sql
    assert "NEW.release_version <> grant_release_version" in rendered_sql
    assert "release_evidence_hash IS NULL OR release_evidence_hash ~ '^[0-9a-f]{64}$'" in (
        rendered_sql
    )
    assert "(release_evidence_hash IS NULL) = (release_evidence_expires_at IS NULL)" in (
        rendered_sql
    )
    assert "NEW.release_evidence_expires_at <= CURRENT_TIMESTAMP" in rendered_sql
    assert "v_reservation.release_evidence_expires_at <= v_now" in rendered_sql
    assert "synthetic receipt release evidence is missing" in rendered_sql
    assert "release qualification decision from_status is stale" in rendered_sql
    assert "release qualification reviewer must be independent" in rendered_sql
    assert "release qualification operator must be independent" in rendered_sql
    assert "final decision evidence set was not independently reviewed" in rendered_sql
    assert "review must bind the complete immutable evidence set" in rendered_sql
    assert "release qualification evidence is frozen after review begins" in rendered_sql
    assert "GRANT SELECT ON careerops.release_qualifications" in rendered_sql
    assert "GRANT INSERT (id, qualification_id, from_status, to_status" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_autopilot_authorization_insert()" in rendered_sql
    assert "CREATE TRIGGER trg_autopilot_intent_authorizations_insert_guard" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_autopilot_grant_version_insert()" in rendered_sql
    assert "CREATE TRIGGER trg_autopilot_grant_versions_insert_guard" in rendered_sql
    assert "NEW.subject_actor <> campaign_owner_user_id::text" in rendered_sql
    assert "ALTER COLUMN decision TYPE character varying(32)" in rendered_sql
    assert "'allow_autopilot_submission'" in rendered_sql
    assert "NEW.authorization_outcome = 'allow_autopilot_submission'" in rendered_sql
    assert "policy_decision <> 'allow_autopilot_submission'" in rendered_sql
    assert "policy_ruleset_version <> grant_policy_ruleset_version" in rendered_sql
    assert "policy_expires_at <= CURRENT_TIMESTAMP" in rendered_sql
    assert "NEW.expires_at > policy_expires_at" in rendered_sql
    assert "NEW.authorized_at < grant_created_at" in rendered_sql
    assert "NEW.authorized_at > CURRENT_TIMESTAMP" in rendered_sql
    assert "grant_expires_at <= CURRENT_TIMESTAMP" in rendered_sql
    assert "NEW.expires_at > grant_expires_at" in rendered_sql
    assert "WHERE revocation.grant_version_id = NEW.grant_version_id" in rendered_sql
    assert "CREATE FUNCTION careerops.enforce_autopilot_grant_revocation_insert()" in rendered_sql
    assert "NEW.revoked_by_user_id <> grant_owner_user_id" in rendered_sql
    assert "superseding_campaign_id <> grant_campaign_id" in rendered_sql
    assert "revocation.created_at <= NEW.authorized_at" not in rendered_sql
    assert (
        "GRANT INSERT (id, campaign_id, grant_version_id, authorization_id, action_intent_id"
        in rendered_sql
    )
    assert "careerops_side_effect" not in " ".join(
        line for line in rendered_sql.splitlines() if "autopilot_" in line
    )


def test_m0_role_grants_are_minimal_and_column_scoped() -> None:
    assert set(ROLE_GRANTS) == {
        "careerops_api",
        "careerops_mailbox",
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


def test_autopilot_control_plane_grants_are_append_only_and_read_only() -> None:
    all_grants = (
        AUTOPILOT_API_GRANTS
        + AUTOPILOT_READONLY_GRANTS
        + AUTOPILOT_RESERVATION_API_GRANTS
        + AUTOPILOT_RESERVATION_READONLY_GRANTS
    )
    assert all(statement.startswith("GRANT SELECT") for statement in AUTOPILOT_READONLY_GRANTS)
    assert not any(
        statement.startswith("GRANT UPDATE")
        or statement.startswith("GRANT DELETE")
        or statement.startswith("GRANT ALL")
        or "outbox_events" in statement
        or "careerops_side_effect" in statement
        for statement in all_grants
    )
    api_insert_grants = {
        statement
        for statement in AUTOPILOT_API_GRANTS + AUTOPILOT_RESERVATION_API_GRANTS
        if statement.startswith("GRANT INSERT")
    }
    assert len(api_insert_grants) == 6
    assert any("authorization_outcome" in statement for statement in api_insert_grants)
    assert any("resolution_mode" in statement for statement in api_insert_grants)
    assert any("reservation_key" in statement for statement in api_insert_grants)


def test_crawler_execution_control_plane_grants_are_append_only_and_outbox_read_only() -> None:
    all_grants = (
        CRAWLER_EXECUTION_API_GRANTS
        + CRAWLER_EXECUTION_OUTBOX_GRANTS
        + CRAWLER_EXECUTION_READONLY_GRANTS
    )
    assert all(
        statement.startswith("GRANT SELECT") for statement in CRAWLER_EXECUTION_READONLY_GRANTS
    )
    assert all(
        statement.startswith("GRANT SELECT") for statement in CRAWLER_EXECUTION_OUTBOX_GRANTS
    )
    assert not any(
        statement.startswith("GRANT UPDATE")
        or statement.startswith("GRANT DELETE")
        or statement.startswith("GRANT ALL")
        or "careerops_side_effect" in statement
        for statement in all_grants
    )
    api_insert_grants = {
        statement
        for statement in CRAWLER_EXECUTION_API_GRANTS
        if statement.startswith("GRANT INSERT")
    }
    assert len(api_insert_grants) == 3
    assert any("manifest_path" in statement for statement in api_insert_grants)
    assert any("approval_artifact_sha256" in statement for statement in api_insert_grants)
    assert any("outbox_event_id" in statement for statement in api_insert_grants)
    assert not any("decided_at" in statement for statement in api_insert_grants)
    assert not any("dispatched_at" in statement for statement in api_insert_grants)


def test_crawler_execution_completion_grants_are_function_scoped() -> None:
    all_grants = (
        CRAWLER_EXECUTION_RESULTS_API_GRANTS
        + CRAWLER_EXECUTION_RESULTS_OUTBOX_GRANTS
        + CRAWLER_EXECUTION_RESULTS_READONLY_GRANTS
    )

    assert CRAWLER_EXECUTION_RESULTS_API_GRANTS == (
        "GRANT SELECT ON careerops.crawler_execution_results TO careerops_api",
    )
    assert CRAWLER_EXECUTION_RESULTS_READONLY_GRANTS == (
        "GRANT SELECT ON careerops.crawler_execution_results TO careerops_readonly",
    )
    assert CRAWLER_EXECUTION_RESULTS_OUTBOX_GRANTS == (
        "GRANT SELECT ON careerops.crawler_execution_results TO careerops_outbox",
        "GRANT EXECUTE ON FUNCTION "
        "careerops.complete_crawler_execution_outbox_event(uuid, text, uuid, text, text, uuid) "
        "TO careerops_outbox",
    )
    assert not any(
        statement.startswith("GRANT INSERT")
        or statement.startswith("GRANT UPDATE")
        or statement.startswith("GRANT DELETE")
        or statement.startswith("GRANT ALL")
        or "careerops_side_effect" in statement
        for statement in all_grants
    )


def test_crawler_source_registry_grants_are_append_only_and_scheduler_readable() -> None:
    assert REGISTRY_MIGRATION_MODULE["revision"] == "0010"
    assert REGISTRY_MIGRATION_MODULE["down_revision"] == "0009"
    assert set(REGISTRY_APPEND_ONLY) == {
        "crawler_job_deduplication_keys",
        "crawler_job_ingestion_evidence",
        "crawler_source_registries",
        "crawler_source_runs",
    }
    assert all(statement.startswith("GRANT SELECT") for statement in REGISTRY_READONLY_GRANTS)
    assert any("crawler_source_registries" in statement for statement in REGISTRY_API_GRANTS)
    assert any("crawler_source_registry_sources" in statement for statement in REGISTRY_API_GRANTS)
    assert any("next_run_at" in statement for statement in REGISTRY_API_GRANTS)
    assert any("last_result" in statement for statement in REGISTRY_API_GRANTS)
    assert not any(statement.startswith("GRANT DELETE") for statement in REGISTRY_API_GRANTS)
    assert "source registry" in REGISTRY_MIGRATION_SOURCE


def test_goal_run_control_plane_grants_are_function_scoped_by_capability() -> None:
    assert GOAL_RUN_MIGRATION_MODULE["revision"] == "0011"
    assert GOAL_RUN_MIGRATION_MODULE["down_revision"] == "0010"
    assert all("GRANT INSERT" not in statement for statement in GOAL_RUN_API_GRANTS)
    assert all("GRANT UPDATE" not in statement for statement in GOAL_RUN_API_GRANTS)
    api_execute_grants = [
        statement for statement in GOAL_RUN_API_GRANTS if "GRANT EXECUTE" in statement
    ]
    assert all("goal_run_checkpoint" not in statement for statement in api_execute_grants)
    assert all("goal_run_request_review" not in statement for statement in api_execute_grants)
    assert all(
        "goal_run_reviewed_crawler_result" not in statement for statement in api_execute_grants
    )
    assert any("goal_run_create" in statement for statement in GOAL_RUN_API_GRANTS)
    assert any("goal_run_record_review_decision" in statement for statement in GOAL_RUN_API_GRANTS)
    assert any("goal_run_cancel" in statement for statement in GOAL_RUN_API_GRANTS)
    assert any("goal_run_resume" in statement for statement in GOAL_RUN_API_GRANTS)
    assert any("goal_run_checkpoint" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any("goal_run_request_review" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any(
        "goal_run_reviewed_crawler_result" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS
    )
    assert any("claim_due_crawler_source" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any("claim_crawler_source" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any("complete_crawler_source_run" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any("fail_crawler_source_run" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    assert any(
        "crawler_job_ingestion_evidence" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS
    )
    assert any("canonical_jobs" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS)
    workflow_grants_joined = " ".join(GOAL_RUN_WORKFLOW_GRANTS)
    assert "content_blobs" not in workflow_grants_joined
    assert "content_objects" not in workflow_grants_joined
    assert "job_aliases" not in workflow_grants_joined
    assert "evidence_records" not in workflow_grants_joined
    workflow_update_grants = {
        statement for statement in GOAL_RUN_WORKFLOW_GRANTS if statement.startswith("GRANT UPDATE")
    }
    assert workflow_update_grants == EXPECTED_GOAL_RUN_WORKFLOW_UPDATE_GRANTS
    workflow_insert_grants = " ".join(
        statement for statement in GOAL_RUN_WORKFLOW_GRANTS if statement.startswith("GRANT INSERT")
    )
    assert "job_merge_decisions" in workflow_insert_grants
    assert "job_posting_assignments" in workflow_insert_grants
    assert "crawler_job_ingestion_evidence" in workflow_insert_grants
    assert not any(
        "crawler_source_registries TO careerops_workflow" in statement
        for statement in GOAL_RUN_WORKFLOW_GRANTS
    )
    assert not any(
        "crawler_execution_approvals TO careerops_workflow" in statement
        for statement in GOAL_RUN_WORKFLOW_GRANTS
    )
    assert not any(
        "outbox_events TO careerops_workflow" in statement for statement in GOAL_RUN_WORKFLOW_GRANTS
    )
    assert all(
        statement.startswith("GRANT SELECT") or "USAGE ON SCHEMA" in statement
        for statement in GOAL_RUN_READONLY_GRANTS
    )
    assert "goal_run_command_receipts" not in " ".join(GOAL_RUN_READONLY_GRANTS)
    assert "terminal goal runs are immutable" in GOAL_RUN_MIGRATION_SOURCE


def test_gmail_readonly_control_plane_is_owner_scoped_and_mailbox_limited() -> None:
    assert GMAIL_READONLY_MIGRATION_MODULE["revision"] == "0012"
    assert GMAIL_READONLY_MIGRATION_MODULE["down_revision"] == "0011"
    assert "gmail_mailboxes" not in GMAIL_READONLY_MIGRATION_SOURCE
    assert "gmail.metadata" not in GMAIL_READONLY_MIGRATION_SOURCE
    assert "gmail.send" not in GMAIL_READONLY_MIGRATION_SOURCE
    assert "gmail.compose" not in GMAIL_READONLY_MIGRATION_SOURCE
    assert "https://www.googleapis.com/auth/gmail.readonly" in GMAIL_READONLY_MIGRATION_SOURCE
    assert "credential_store_evidence_sha256" in GMAIL_READONLY_MIGRATION_SOURCE
    assert "credential_store_attested" not in GMAIL_READONLY_MIGRATION_SOURCE
    assert "SECURITY DEFINER" in GMAIL_READONLY_MIGRATION_SOURCE
    assert "SET search_path = pg_catalog, careerops" in GMAIL_READONLY_MIGRATION_SOURCE
    assert all("GRANT INSERT" not in statement for statement in GMAIL_READONLY_API_GRANTS)
    assert all("GRANT UPDATE" not in statement for statement in GMAIL_READONLY_API_GRANTS)
    assert all(
        "GRANT EXECUTE" in statement
        for statement in GMAIL_READONLY_API_GRANTS
        if "FUNCTION" in statement
    )
    assert any(
        "gmail_readonly_register_account" in statement for statement in GMAIL_READONLY_API_GRANTS
    )
    assert any(
        "gmail_readonly_review_proposal" in statement for statement in GMAIL_READONLY_API_GRANTS
    )
    mailbox_grants = " ".join(GMAIL_READONLY_MAILBOX_GRANTS)
    assert "application_events" not in mailbox_grants
    assert "outbox_events" not in mailbox_grants
    assert "action_intents" not in mailbox_grants
    assert "action_payload_versions" not in mailbox_grants
    assert any(
        "gmail_readonly_claim_sync_runs" in statement for statement in GMAIL_READONLY_MAILBOX_GRANTS
    )
    assert any(
        "gmail_readonly_record_message_signal" in statement
        for statement in GMAIL_READONLY_MAILBOX_GRANTS
    )
    assert any(
        "gmail_readonly_complete_sync_run" in statement
        for statement in GMAIL_READONLY_MAILBOX_GRANTS
    )
    assert any(
        "gmail_readonly_fail_sync_run" in statement for statement in GMAIL_READONLY_MAILBOX_GRANTS
    )
    assert "gmail_command_receipts" in " ".join(GMAIL_READONLY_READONLY_GRANTS)


def test_gmail_send_channel_is_exact_reviewed_and_mail_sender_scoped() -> None:
    assert GMAIL_SEND_MIGRATION_MODULE["revision"] == "0013"
    assert GMAIL_SEND_MIGRATION_MODULE["down_revision"] == "0012"
    assert "https://www.googleapis.com/auth/gmail.send" in GMAIL_SEND_MIGRATION_SOURCE
    assert "provider = 'gmail_send'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "credential.granted_scopes = jsonb_build_array" in GMAIL_SEND_MIGRATION_SOURCE
    assert "credential.provider = 'gmail'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "https://www.googleapis.com/auth/gmail.readonly" in GMAIL_SEND_MIGRATION_SOURCE
    assert "gmail_send_create_draft" in GMAIL_SEND_MIGRATION_SOURCE
    assert "gmail_send_review_draft" in GMAIL_SEND_MIGRATION_SOURCE
    assert "gmail_send_create_exact_payload_draft" in GMAIL_SEND_MIGRATION_SOURCE
    assert "gmail_send_review_exact_payload_draft" in GMAIL_SEND_MIGRATION_SOURCE
    assert "gmail_send_reserve_reviewed_intent" in GMAIL_SEND_MIGRATION_SOURCE
    assert "claim_gmail_send_reconciliation_jobs" in GMAIL_SEND_MIGRATION_SOURCE
    assert "readonly_credential_handle" in GMAIL_SEND_MIGRATION_SOURCE
    assert "requested_for = 'gmail_send_exact_payload'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "allow_autopilot_submission" in GMAIL_SEND_MIGRATION_SOURCE
    assert "action_kind = 'send_email'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "NEW.channel <> 'gmail:send'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "NEW.target_host <> 'gmail.googleapis.com'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "NEW.adapter_id <> 'gmail'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "NEW.fixture_id <> 'gmail-send.v1'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "payload_target ->> 'recipient_sha256' IS DISTINCT FROM NEW.company_key" in (
        GMAIL_SEND_MIGRATION_SOURCE
    )
    assert "payload_target ->> 'body_sha256'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "payload_target ->> 'grant_material_hash'" in GMAIL_SEND_MIGRATION_SOURCE
    assert "grant_material_hashes @> jsonb_build_array(attachment.value ->> 'sha256')" in (
        GMAIL_SEND_MIGRATION_SOURCE
    )
    assert all("GRANT EXECUTE" in statement for statement in GMAIL_SEND_API_GRANTS)
    assert all("GRANT INSERT" not in statement for statement in GMAIL_SEND_API_GRANTS)
    assert all("GRANT UPDATE" not in statement for statement in GMAIL_SEND_API_GRANTS)
    assert all("careerops_mail_sender" in statement for statement in GMAIL_SEND_MAIL_SENDER_GRANTS)
    assert "careerops_mailbox" not in " ".join(
        GMAIL_SEND_API_GRANTS + GMAIL_SEND_MAIL_SENDER_GRANTS + GMAIL_SEND_READONLY_GRANTS
    )
    assert "secret_handle" not in " ".join(
        GMAIL_SEND_MAIL_SENDER_GRANTS + GMAIL_SEND_READONLY_GRANTS
    )
    assert "gmail_send_command_receipts" in " ".join(GMAIL_SEND_READONLY_GRANTS)


def test_goal_run_tables_bind_owner_fencing_and_immutable_receipts() -> None:
    run = metadata.tables["careerops.goal_runs"]
    checkpoint = metadata.tables["careerops.goal_run_checkpoints"]
    review_item = metadata.tables["careerops.goal_run_review_items"]
    decision = metadata.tables["careerops.goal_run_review_decisions"]
    receipt = metadata.tables["careerops.goal_run_command_receipts"]

    assert ("actor_user_id", "idempotency_key") in {
        tuple(constraint.columns.keys())
        for constraint in run.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("goal_run_id", "idempotency_key") in {
        tuple(constraint.columns.keys())
        for constraint in checkpoint.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert review_item.c.snapshot_sha256.nullable is False
    assert decision.c.reason.nullable is False
    assert receipt.c.request_sha256.nullable is False
    assert receipt.c.response_json.nullable is False
    assert "fencing_token" in checkpoint.c


def test_goal_run_gmail_composition_contract_is_wrapper_scoped() -> None:
    assert GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_MODULE["revision"] == "0015"
    assert GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_MODULE["down_revision"] == "0014"
    assert "goal_run_gmail_compositions" in EXPECTED_TABLES

    composition = metadata.tables["careerops.goal_run_gmail_compositions"]
    assert composition.c.goal_run_id.nullable is False
    assert composition.c.actor_user_id.nullable is False
    assert composition.c.source_row_id.nullable is False
    assert composition.c.crawler_run_id.nullable is False
    assert composition.c.match_snapshot_sha256.nullable is False
    assert {"matched", "draft_prepared", "dispatch_enqueued"} <= {
        value
        for constraint in composition.constraints
        if isinstance(constraint, sa.CheckConstraint)
        for value in ("matched", "draft_prepared", "dispatch_enqueued")
        if value in str(constraint.sqltext)
    }
    assert ("goal_run_id",) in {
        tuple(constraint.columns.keys())
        for constraint in composition.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert GOAL_RUN_GMAIL_WORKFLOW_GRANTS == (
        "GRANT USAGE ON SCHEMA careerops TO careerops_workflow",
        "GRANT EXECUTE ON FUNCTION "
        "careerops.goal_run_record_gmail_match"
        "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, numeric, jsonb, text) "
        "TO careerops_workflow",
        "GRANT EXECUTE ON FUNCTION "
        "careerops.goal_run_prepare_gmail(uuid, uuid, jsonb, jsonb, jsonb, text) "
        "TO careerops_workflow",
        "GRANT EXECUTE ON FUNCTION careerops.goal_run_dispatch_gmail(uuid, uuid) "
        "TO careerops_workflow",
        "GRANT EXECUTE ON FUNCTION careerops.goal_run_inspect_gmail(uuid, uuid) "
        "TO careerops_workflow",
    )
    assert "GRANT INSERT" not in " ".join(GOAL_RUN_GMAIL_WORKFLOW_GRANTS)
    assert "GRANT UPDATE" not in " ".join(GOAL_RUN_GMAIL_WORKFLOW_GRANTS)
    assert "careerops.gmail_send_create_draft(" in GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    assert "careerops.gmail_send_review_draft(" in GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    assert "careerops.gmail_send_reserve_and_enqueue(" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "v_run.context_json -> 'gmail_dispatch' ->> 'account_id'" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "v_run.context_json -> 'gmail_dispatch' ->> 'authorization_expires_at'" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "v_run.context_json -> 'gmail_dispatch' ->> 'sender'" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "lower(account.account_subject) = v_sender" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "v_run.source_id IS NULL OR source_row.source_id = v_run.source_id" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "v_account.candidate_id,\n        v_account.candidate_id," in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "LEFT JOIN LATERAL" in GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    assert "current_attempt.action_intent_id = composition.action_intent_id" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "terminal_receipt.side_effect_attempt_id = attempt.id" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "terminal_receipt.reconciliation_key = composition.reconciliation_key" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert (
        "item.review_payload @> jsonb_build_object" in GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "item.review_kind = 'goal_run_application_review.v1'" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "'action_payload_hash', v_composition.payload_hash" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "item.review_payload #>> '{email,subject}'" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "jsonb_array_elements(payload_version.attachment_refs)" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )
    assert "goal run dispatch requires exact approved review snapshot" in (
        GOAL_RUN_GMAIL_COMPOSITION_MIGRATION_SOURCE
    )


def test_synthetic_submission_migration_extends_the_0007_chain() -> None:
    assert SYNTHETIC_SUBMISSION_MIGRATION_MODULE["revision"] == "0008"
    assert SYNTHETIC_SUBMISSION_MIGRATION_MODULE["down_revision"] == "0007"
    assert SYNTHETIC_SUBMISSION_MIGRATION_MODULE["branch_labels"] is None
    assert SYNTHETIC_SUBMISSION_MIGRATION_MODULE["depends_on"] is None


def test_synthetic_kill_switch_order_and_execution_reads_are_transaction_serialized() -> None:
    source = SYNTHETIC_SUBMISSION_MIGRATION_SOURCE

    assert "pg_advisory_xact_lock(" in source
    assert "pg_advisory_xact_lock_shared(" in source
    assert "hashtextextended(scope_lock_key, 0)" in source
    assert "NEW.sequence := pg_catalog.nextval(" in source
    assert "trg_autopilot_kill_switch_events_serialize_insert" in source


def test_synthetic_submission_downgrade_drops_the_execution_boundary_before_columns() -> None:
    downgrade_source = SYNTHETIC_SUBMISSION_MIGRATION_SOURCE.split("def downgrade() -> None:", 1)[1]

    assert "_drop_reservation_guard()" in downgrade_source
    assert "DROP TRIGGER IF EXISTS trg_autopilot_cap_reservations_execution_boundary" in (
        SYNTHETIC_SUBMISSION_MIGRATION_SOURCE
    )
    assert "DROP FUNCTION IF EXISTS " in SYNTHETIC_SUBMISSION_MIGRATION_SOURCE
    assert (
        "careerops.enforce_autopilot_cap_reservation_execution_boundary()"
        in SYNTHETIC_SUBMISSION_MIGRATION_SOURCE
    )
    drop_release_evidence_expires_at = (
        'op.drop_column(\n        "autopilot_cap_reservations",\n'
        '        "release_evidence_expires_at"'
    )
    assert downgrade_source.index("_drop_reservation_guard()") < downgrade_source.index(
        drop_release_evidence_expires_at
    )
    assert downgrade_source.index("_drop_reservation_guard()") < downgrade_source.index(
        'op.drop_column("autopilot_cap_reservations", "release_evidence_hash"'
    )


def test_synthetic_submission_grants_are_function_scoped_without_direct_outbox_mutation() -> None:
    all_grants = (
        SYNTHETIC_SUBMISSION_API_GRANTS
        + SYNTHETIC_SUBMISSION_OUTBOX_GRANTS
        + SYNTHETIC_SUBMISSION_READONLY_GRANTS
    )

    assert SYNTHETIC_SUBMISSION_API_GRANTS == (
        "GRANT INSERT (release_evidence_hash, release_evidence_expires_at) "
        "ON careerops.autopilot_cap_reservations TO careerops_api",
        "GRANT SELECT ON careerops.autopilot_kill_switch_events TO careerops_api",
        "GRANT INSERT (id, scope_type, campaign_id, provider, active, reason, actor_user_id, "
        "idempotency_key, trace_id) ON careerops.autopilot_kill_switch_events TO careerops_api",
    )
    assert SYNTHETIC_SUBMISSION_READONLY_GRANTS == (
        "GRANT SELECT ON careerops.autopilot_kill_switch_events TO careerops_readonly",
    )
    assert SYNTHETIC_SUBMISSION_OUTBOX_GRANTS == (
        "GRANT EXECUTE ON FUNCTION "
        "careerops.prepare_synthetic_submission_outbox_event(uuid, text, uuid) "
        "TO careerops_outbox",
        "GRANT EXECUTE ON FUNCTION "
        "careerops.record_synthetic_submission_outbox_receipt"
        "(uuid, text, uuid, text, text, text, text, timestamp with time zone, uuid) "
        "TO careerops_outbox",
        "GRANT EXECUTE ON FUNCTION "
        "careerops.record_synthetic_submission_outbox_ambiguity"
        "(uuid, text, uuid, text) TO careerops_outbox",
    )
    assert not any(
        statement.startswith("GRANT UPDATE")
        or statement.startswith("GRANT DELETE")
        or statement.startswith("GRANT ALL")
        or "GRANT INSERT ON careerops.outbox_events" in statement
        or "GRANT UPDATE ON careerops.outbox_events" in statement
        or "careerops_side_effect" in statement
        for statement in all_grants
    )
    assert "REVOKE ALL ON careerops.side_effect_attempts FROM careerops_outbox" in (
        SYNTHETIC_SUBMISSION_MIGRATION_SOURCE
    )
    assert "REVOKE ALL ON careerops.provider_receipts FROM careerops_outbox" in (
        SYNTHETIC_SUBMISSION_MIGRATION_SOURCE
    )


def test_release_qualification_migration_extends_the_0008_chain() -> None:
    assert RELEASE_QUALIFICATION_MIGRATION_MODULE["revision"] == "0009"
    assert RELEASE_QUALIFICATION_MIGRATION_MODULE["down_revision"] == "0008"
    assert RELEASE_QUALIFICATION_MIGRATION_MODULE["branch_labels"] is None
    assert RELEASE_QUALIFICATION_MIGRATION_MODULE["depends_on"] is None


def test_release_qualification_grants_are_append_only_without_provider_authority() -> None:
    all_grants = RELEASE_QUALIFICATION_API_GRANTS + RELEASE_QUALIFICATION_READONLY_GRANTS

    assert RELEASE_QUALIFICATION_READONLY_GRANTS == (
        "GRANT SELECT ON careerops.release_qualifications, "
        "careerops.release_qualification_evidence, careerops.release_qualification_decisions "
        "TO careerops_readonly",
    )
    assert RELEASE_QUALIFICATION_API_GRANTS == (
        "GRANT SELECT ON careerops.release_qualifications, "
        "careerops.release_qualification_evidence, careerops.release_qualification_decisions "
        "TO careerops_api",
        "GRANT INSERT (id, capability, action_name, rollout_mode, provider, adapter_id, "
        "adapter_version, implementation_hash, config_hash, policy_hash, dataset_hash, "
        "git_commit, image_digest, migration_revision, oauth_scope_hash, "
        "credential_ref_hash, network_policy_hash, reconcile_policy_hash, hard_stop_hash, "
        "sensitive_field_hash, kill_switch_hash, fixture_manifest_sha256, "
        "fault_manifest_sha256, holdout_manifest_sha256, live_sample_manifest_sha256, "
        "status, requested_by_user_id, created_by, expires_at) "
        "ON careerops.release_qualifications TO careerops_api",
        "GRANT INSERT (id, qualification_id, evidence_kind, artifact_uri, artifact_sha256, "
        "run_id, runner_user_id, runner_actor, metrics, sample_manifest_sha256) "
        "ON careerops.release_qualification_evidence TO careerops_api",
        "GRANT INSERT (id, qualification_id, from_status, to_status, decision_role, "
        "actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids) "
        "ON careerops.release_qualification_decisions TO careerops_api",
    )
    assert all(
        statement.startswith("GRANT SELECT") for statement in RELEASE_QUALIFICATION_READONLY_GRANTS
    )
    assert not any(
        statement.startswith("GRANT UPDATE")
        or statement.startswith("GRANT DELETE")
        or statement.startswith("GRANT ALL")
        or "outbox_events" in statement
        or "provider_receipts" in statement
        or "side_effect_attempts" in statement
        or "careerops_side_effect" in statement
        for statement in all_grants
    )
    assert "REVOKE ALL ON careerops.provider_receipts" not in RELEASE_QUALIFICATION_MIGRATION_SOURCE
    assert "REVOKE ALL ON careerops.side_effect_attempts" not in (
        RELEASE_QUALIFICATION_MIGRATION_SOURCE
    )


def test_release_qualification_decision_guard_enforces_lifecycle_and_separation() -> None:
    source = RELEASE_QUALIFICATION_MIGRATION_SOURCE

    assert "pg_advisory_xact_lock(" in source
    assert "release qualification decision from_status is stale" in source
    assert "NEW.from_status = 'draft'" in source
    assert "NEW.to_status = 'evaluating'" in source
    assert "NEW.to_status = 'pending_independent_review'" in source
    assert "NEW.to_status IN ('qualified', 'rejected')" in source
    assert "NEW.decision_role = 'operator'" in source
    assert "final decision evidence set was not independently reviewed" in source
    assert "review must bind the complete immutable evidence set" in source
    assert "release qualification evidence is frozen after review begins" in source
    assert "release qualification reviewer must be independent" in source
    assert "release qualification operator must be independent" in source
    assert "evidence.runner_actor = NEW.actor_id" in source
    assert "NEW.actor_id = v_previous_actor" in source
    assert "NEW.evidence_ids IS DISTINCT FROM v_previous_evidence_ids" in source
    assert "qualified release evidence contains a fail-open observation" in source
    assert '"false_negative": 0' in source
    assert '"autonomous_provider_write_attempts": 0' in source
    assert '"no_autonomous_writes": true' in source
    assert "CREATE TRIGGER trg_release_qualification_decisions_insert_guard" in source
    assert "CREATE TRIGGER trg_release_qualification_evidence_insert_guard" in source


def test_synthetic_submission_receipt_recording_does_not_recheck_release_expiry() -> None:
    record_function_source = SYNTHETIC_SUBMISSION_MIGRATION_SOURCE.split(
        "def _install_record_function() -> None:", 1
    )[1].split("def _drop_functions() -> None:", 1)[0]

    assert "v_reservation.release_evidence_hash IS NULL" in record_function_source
    assert "v_reservation.release_evidence_expires_at IS NULL" in record_function_source
    assert "release_evidence_expires_at <= " not in record_function_source
    assert "release_evidence_expires_at < " not in record_function_source
    assert "release evidence is missing or expired" not in record_function_source


def test_release_qualification_tables_are_immutably_bound_and_append_only() -> None:
    qualification = metadata.tables["careerops.release_qualifications"]
    evidence = metadata.tables["careerops.release_qualification_evidence"]
    decision = metadata.tables["careerops.release_qualification_decisions"]

    assert {
        "capability",
        "action_name",
        "rollout_mode",
        "provider",
        "adapter_id",
        "adapter_version",
        "implementation_hash",
        "config_hash",
        "policy_hash",
        "dataset_hash",
        "fixture_manifest_sha256",
        "fault_manifest_sha256",
        "requested_by_user_id",
    } <= set(qualification.c.keys())
    assert qualification.c.status.server_default is not None
    assert isinstance(qualification.c.expires_at.type, sa.DateTime)
    assert qualification.c.expires_at.type.timezone is True

    qualification_checks = {
        constraint.name: constraint
        for constraint in qualification.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_release_qualifications_rollout_mode_values",
        "ck_release_qualifications_status_initial_draft",
        "ck_release_qualifications_hashes_sha256",
        "ck_release_qualifications_git_commit_format",
        "ck_release_qualifications_image_digest_format",
        "ck_release_qualifications_expiry_after_creation",
    } <= set(qualification_checks)
    assert "review_required" in str(
        qualification_checks["ck_release_qualifications_rollout_mode_values"].sqltext
    )
    assert "synthetic_sandbox" in str(
        qualification_checks["ck_release_qualifications_rollout_mode_values"].sqltext
    )
    assert "limited_autopilot" in str(
        qualification_checks["ck_release_qualifications_rollout_mode_values"].sqltext
    )
    assert "status = 'draft'" in str(
        qualification_checks["ck_release_qualifications_status_initial_draft"].sqltext
    )

    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in qualification.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert (
        "capability",
        "action_name",
        "rollout_mode",
        "provider",
        "adapter_id",
        "adapter_version",
        "implementation_hash",
        "config_hash",
        "policy_hash",
        "dataset_hash",
    ) not in unique_columns

    assert {"runner_user_id", "metrics", "sample_manifest_sha256"} <= set(evidence.c.keys())
    evidence_checks = {
        constraint.name
        for constraint in evidence.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_release_qualification_evidence_kind_values",
        "ck_release_qualification_evidence_hashes_sha256",
        "ck_release_qualification_evidence_metrics_object",
        "ck_release_qualification_evidence_text_safe_nonempty",
    } <= evidence_checks

    assert {
        "from_status",
        "to_status",
        "decision_role",
        "actor_id",
        "decided_by_user_id",
        "evidence_ids",
    } <= set(decision.c.keys())
    assert isinstance(decision.c.sequence.type, sa.BigInteger)
    assert isinstance(decision.c.sequence.identity, sa.Identity)
    assert decision.c.sequence.unique is True
    decision_checks = {
        constraint.name: constraint
        for constraint in decision.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_release_qualification_decisions_from_status_values",
        "ck_release_qualification_decisions_to_status_values",
        "ck_release_qualification_decisions_role_values",
        "ck_release_qualification_decisions_actor_binding",
        "ck_release_qualification_decisions_evidence_sha256_format",
        "ck_release_qualification_decisions_evidence_ids_no_nulls",
    } <= set(decision_checks)
    assert "operator" in str(
        decision_checks["ck_release_qualification_decisions_role_values"].sqltext
    )
    assert "runner" in str(
        decision_checks["ck_release_qualification_decisions_role_values"].sqltext
    )
    assert "qualified" in str(
        decision_checks["ck_release_qualification_decisions_to_status_values"].sqltext
    )


def test_crawler_execution_results_are_append_only_and_identity_bound() -> None:
    results = metadata.tables["careerops.crawler_execution_results"]
    assert {"request_id", "action_intent_id", "outbox_event_id", "outcome", "error_code"} <= set(
        results.c.keys()
    )
    assert isinstance(results.c.outcome.type, sa.String)
    assert results.c.outcome.type.length is not None
    assert results.c.outcome.type.length >= len("reconciliation_required")
    checks = {
        constraint.name
        for constraint in results.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_crawler_execution_results_outcome_values",
        "ck_crawler_execution_results_error_code_matches_outcome",
        "ck_crawler_execution_results_error_code_format",
    } <= checks
    outcome_check = next(
        constraint
        for constraint in results.constraints
        if constraint.name == "ck_crawler_execution_results_outcome_values"
    )
    assert isinstance(outcome_check, sa.CheckConstraint)
    assert "reconciliation_required" in str(outcome_check.sqltext)
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in results.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert {("request_id",), ("outbox_event_id",)} <= unique_columns


def test_autopilot_cap_reservations_have_canonical_release_evidence_pair() -> None:
    reservation = metadata.tables["careerops.autopilot_cap_reservations"]

    assert {"release_evidence_hash", "release_evidence_expires_at"} <= set(reservation.c.keys())
    assert isinstance(reservation.c.release_evidence_hash.type, sa.String)
    assert reservation.c.release_evidence_hash.type.length == 64
    assert isinstance(reservation.c.release_evidence_expires_at.type, sa.DateTime)
    assert reservation.c.release_evidence_expires_at.type.timezone is True

    checks = {
        constraint.name: constraint
        for constraint in reservation.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_cap_reservations_release_evidence_hash_format",
        "ck_autopilot_cap_reservations_release_evidence_expiry_current",
    } <= set(checks)
    assert "release_evidence_hash ~ '^[0-9a-f]{64}$'" in str(
        checks["ck_autopilot_cap_reservations_release_evidence_hash_format"].sqltext
    )
    release_pair_sql = str(
        checks["ck_autopilot_cap_reservations_release_evidence_expiry_current"].sqltext
    )
    assert "(release_evidence_hash IS NULL) = (release_evidence_expires_at IS NULL)" in (
        release_pair_sql
    )
    assert "release_evidence_expires_at > reserved_at" in release_pair_sql


def test_autopilot_kill_switch_events_are_append_only_monotonic_and_actor_bound() -> None:
    kill_switch = metadata.tables["careerops.autopilot_kill_switch_events"]

    assert tuple(kill_switch.c.keys()) == (
        "sequence",
        "id",
        "scope_type",
        "campaign_id",
        "provider",
        "active",
        "reason",
        "actor_user_id",
        "idempotency_key",
        "trace_id",
        "created_at",
    )
    assert isinstance(kill_switch.c.sequence.type, sa.BigInteger)
    assert isinstance(kill_switch.c.sequence.identity, sa.Identity)
    assert kill_switch.c.sequence.unique is True
    assert kill_switch.c.id.primary_key is True

    actor_fk = next(iter(kill_switch.c.actor_user_id.foreign_keys))
    assert actor_fk.target_fullname == "careerops.console_users.id"
    assert actor_fk.ondelete == "RESTRICT"

    checks = {
        constraint.name: constraint
        for constraint in kill_switch.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_kill_switch_events_scope_type_values",
        "ck_autopilot_kill_switch_events_scope_binding",
        "ck_autopilot_kill_switch_events_text_nonempty",
    } <= set(checks)
    assert "global" in str(checks["ck_autopilot_kill_switch_events_scope_type_values"].sqltext)
    assert "campaign_id IS NOT NULL" in str(
        checks["ck_autopilot_kill_switch_events_scope_binding"].sqltext
    )
    assert "provider IS NOT NULL" in str(
        checks["ck_autopilot_kill_switch_events_scope_binding"].sqltext
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


def test_autopilot_control_plane_is_database_constrained() -> None:
    policy_decision = metadata.tables["careerops.policy_decisions"]
    decision_column = policy_decision.c.decision
    assert isinstance(decision_column.type, sa.String)
    assert decision_column.type.length == 32
    decision_check = next(
        constraint
        for constraint in policy_decision.constraints
        if constraint.name == "ck_policy_decisions_decision_values"
    )
    assert isinstance(decision_check, sa.CheckConstraint)
    assert "allow_autopilot_submission" in str(decision_check.sqltext)

    grant = metadata.tables["careerops.autopilot_grant_versions"]
    grant_checks = {
        constraint.name
        for constraint in grant.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_grant_versions_allowed_action_kinds_array",
        "ck_autopilot_grant_versions_allowed_channels_array",
        "ck_autopilot_grant_versions_allowed_target_hosts_array",
        "ck_autopilot_grant_versions_material_hashes_array",
        "ck_autopilot_grant_versions_max_total_submissions_positive",
        "ck_autopilot_grant_versions_max_daily_submissions_positive",
        "ck_autopilot_grant_versions_max_per_company_positive",
        "ck_autopilot_grant_versions_expiry_after_creation",
    } <= grant_checks

    authorization = metadata.tables["careerops.autopilot_intent_authorizations"]
    authorization_checks = {
        constraint.name
        for constraint in authorization.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_intent_authorizations_authorization_outcome_values",
        "ck_autopilot_intent_authorizations_payload_hash_format",
        "ck_autopilot_intent_authorizations_expiry_after_authorization",
    } <= authorization_checks
    outcome_check = next(
        constraint
        for constraint in authorization.constraints
        if constraint.name == "ck_autopilot_intent_authorizations_authorization_outcome_values"
    )
    assert isinstance(outcome_check, sa.CheckConstraint)
    assert "allow_autopilot_submission" in str(outcome_check.sqltext)
    assert "manual_only" in str(outcome_check.sqltext)

    review = metadata.tables["careerops.autopilot_review_items"]
    review_checks = {
        constraint.name
        for constraint in review.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_review_items_resolution_mode_values",
        "ck_autopilot_review_items_manual_only_not_pending",
        "ck_autopilot_review_items_snapshot_object",
    } <= review_checks
    resolution_check = next(
        constraint
        for constraint in review.constraints
        if constraint.name == "ck_autopilot_review_items_resolution_mode_values"
    )
    assert isinstance(resolution_check, sa.CheckConstraint)
    assert "agent_approvable" in str(resolution_check.sqltext)
    assert "manual_only" in str(resolution_check.sqltext)
    assert "remediation_required" in str(resolution_check.sqltext)
    review_kind_check = next(
        constraint
        for constraint in review.constraints
        if constraint.name == "ck_autopilot_review_items_review_kind_values"
    )
    assert isinstance(review_kind_check, sa.CheckConstraint)
    assert "pending" in str(review_kind_check.sqltext)
    assert "exceptions" in str(review_kind_check.sqltext)
    assert "pending_review" not in str(review_kind_check.sqltext)
    manual_only_check = next(
        constraint
        for constraint in review.constraints
        if constraint.name == "ck_autopilot_review_items_manual_only_not_pending"
    )
    assert isinstance(manual_only_check, sa.CheckConstraint)
    assert "review_kind = 'pending'" in str(manual_only_check.sqltext)

    reservation = metadata.tables["careerops.autopilot_cap_reservations"]
    reservation_checks = {
        constraint.name
        for constraint in reservation.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert {
        "ck_autopilot_cap_reservations_payload_hash_format",
        "ck_autopilot_cap_reservations_target_host_nonempty",
        "ck_autopilot_cap_reservations_channel_nonempty",
        "ck_autopilot_cap_reservations_release_version_nonempty",
        "ck_autopilot_cap_reservations_company_key_nonempty",
        "ck_autopilot_cap_reservations_adapter_id_nonempty",
        "ck_autopilot_cap_reservations_fixture_id_nonempty",
        "ck_autopilot_cap_reservations_reservation_key_nonempty",
        "ck_autopilot_cap_reservations_reconciliation_key_nonempty",
        "ck_autopilot_cap_reservations_reservation_date_matches_reserved_at",
    } <= reservation_checks
    assert {"channel", "release_version", "adapter_id", "fixture_id", "reconciliation_key"} <= set(
        reservation.c.keys()
    )
    assert reservation.c.reservation_date.server_default is not None
    assert reservation.c.reserved_at.server_default is not None
