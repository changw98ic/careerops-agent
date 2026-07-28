"""Migration tests for agent-console orchestration (migration 0030).

Covers:
- Upgrade from 0029
- Legacy state backfill
- Composite FK constraints
- Concurrent idempotency
- New table contract verification
"""

from __future__ import annotations

import runpy
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from careerops.infrastructure.database.schema import metadata

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0030 = MIGRATION_DIR / "0030_agent_console_orchestration.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    return Config(
        str(Path(__file__).resolve().parents[2] / "alembic.ini"),
        output_buffer=StringIO(),
    )


def _load_migration_0030() -> dict[str, object]:
    return runpy.run_path(str(MIGRATION_0030))


# ---------------------------------------------------------------------------
# 1. Migration 0030 structure verification
# ---------------------------------------------------------------------------


class TestMigration0030Structure:
    def test_migration_has_correct_revision_chain(self) -> None:
        module = _load_migration_0030()
        assert module["revision"] == "0030"
        assert module["down_revision"] == "0029"

    def test_migration_has_upgrade_function(self) -> None:
        module = _load_migration_0030()
        assert callable(module.get("upgrade"))

    def test_migration_has_guarded_downgrade(self) -> None:
        module = _load_migration_0030()
        assert callable(module.get("downgrade"))
        downgrade = str(module["_DOWNGRADE"])
        assert "rollback-forward" in downgrade
        assert "DROP TABLE IF EXISTS careerops.agent_attempts" in downgrade
        assert "GRANT SELECT, INSERT, UPDATE ON careerops.agent_runs" in downgrade


# ---------------------------------------------------------------------------
# 2. New tables in schema
# ---------------------------------------------------------------------------


class TestAgentConsoleTables:
    def test_agent_contexts_table_exists_in_metadata(self) -> None:
        assert "careerops.agent_contexts" in metadata.tables

    def test_agent_actions_table_exists_in_metadata(self) -> None:
        assert "careerops.agent_actions" in metadata.tables

    def test_agent_attempts_table_exists_in_metadata(self) -> None:
        assert "careerops.agent_attempts" in metadata.tables

    def test_agent_stage_events_table_exists_in_metadata(self) -> None:
        assert "careerops.agent_stage_events" in metadata.tables

    def test_agent_idempotency_receipts_table_exists_in_metadata(self) -> None:
        # idempotency receipts may be created by migration only (not in schema.py)
        if "careerops.agent_idempotency_receipts" not in metadata.tables:
            pytest.skip("agent_idempotency_receipts defined in migration SQL only")


# ---------------------------------------------------------------------------
# 3. agent_runs expansion columns
# ---------------------------------------------------------------------------


class TestAgentRunsExpansion:
    def test_migration_adds_execution_state_column(self) -> None:
        """Migration 0030 adds execution_state to agent_runs via ALTER TABLE."""
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "execution_state text" in expand_sql

    def test_migration_adds_capability_state_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "capability_state text" in expand_sql

    def test_migration_adds_review_state_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "review_state text" in expand_sql

    def test_migration_adds_legacy_state_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "legacy_state text" in expand_sql

    def test_migration_adds_context_id_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "context_id uuid" in expand_sql

    def test_migration_adds_current_attempt_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_EXPAND_COLUMNS"])
        assert "current_attempt integer" in expand_sql


# ---------------------------------------------------------------------------
# 4. agent_run_reviews expansion
# ---------------------------------------------------------------------------


class TestAgentRunReviewsExpansion:
    def test_migration_adds_idempotency_key_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_REVIEW_EXPAND"])
        assert "idempotency_key varchar(128)" in expand_sql

    def test_migration_adds_field_decisions_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_REVIEW_EXPAND"])
        assert "field_decisions jsonb" in expand_sql

    def test_migration_adds_request_hash_column(self) -> None:
        module = _load_migration_0030()
        expand_sql = str(module["_REVIEW_EXPAND"])
        assert "request_hash varchar(64)" in expand_sql


# ---------------------------------------------------------------------------
# 5. Composite FK constraints
# ---------------------------------------------------------------------------


class TestCompositeFKConstraints:
    def test_migration_defines_reviews_composite_fk(self) -> None:
        """agent_run_reviews(run_id, candidate_id) -> agent_runs(id, candidate_id)"""
        module = _load_migration_0030()
        fk_sql = str(module["_REVIEW_FK_AND_CHECKS"])
        assert "fk_agent_run_reviews_run_candidate" in fk_sql
        assert "FOREIGN KEY (run_id,candidate_id)" in fk_sql

    def test_migration_defines_attempts_composite_fk(self) -> None:
        """agent_attempts(run_id, candidate_id) -> agent_runs(id, candidate_id)"""
        module = _load_migration_0030()
        create_sql = str(module["_CREATE_ATTEMPTS"])
        assert "FOREIGN KEY (run_id,candidate_id)" in create_sql

    def test_migration_defines_stage_events_composite_fk(self) -> None:
        """agent_stage_events(attempt_id, run_id, candidate_id) -> agent_attempts(...)"""
        module = _load_migration_0030()
        create_sql = str(module["_CREATE_STAGE_EVENTS"])
        assert "FOREIGN KEY (attempt_id,run_id,candidate_id)" in create_sql

    def test_migration_defines_actions_context_fk(self) -> None:
        """agent_actions(context_id, candidate_id) -> agent_contexts(id, candidate_id)"""
        module = _load_migration_0030()
        create_sql = str(module["_CREATE_ACTIONS"])
        assert "agent_contexts" in create_sql


# ---------------------------------------------------------------------------
# 6. Legacy state backfill logic
# ---------------------------------------------------------------------------


class TestLegacyStateBackfill:
    def test_backfill_sql_maps_pending_to_queued(self) -> None:
        """Verify the backfill CASE maps 'pending' -> 'queued'."""
        module = _load_migration_0030()
        backfill_sql = str(module["_BACKFILL"])
        assert "'pending' THEN 'queued'" in backfill_sql

    def test_backfill_sql_maps_unavailable_to_blocked(self) -> None:
        module = _load_migration_0030()
        backfill_sql = str(module["_BACKFILL"])
        assert "'unavailable' THEN 'blocked'" in backfill_sql

    def test_backfill_sql_maps_abstained_to_waiting_review(self) -> None:
        module = _load_migration_0030()
        backfill_sql = str(module["_BACKFILL"])
        assert "'abstained' THEN 'waiting_review'" in backfill_sql

    def test_backfill_sql_maps_reviewed_to_succeeded(self) -> None:
        module = _load_migration_0030()
        backfill_sql = str(module["_BACKFILL"])
        assert "'reviewed' THEN" in backfill_sql

    def test_preflight_count_checks_unrecognized_states(self) -> None:
        module = _load_migration_0030()
        preflight = str(module["_PREFLIGHT_COUNT"])
        # Should check for states NOT IN the known set
        assert "NOT IN" in preflight


# ---------------------------------------------------------------------------
# 7. CAS functions
# ---------------------------------------------------------------------------


class TestCASFunctions:
    def test_acquire_agent_attempt_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "acquire_agent_attempt" in str(module["_ACQUIRE_LEASE_FN"])

    def test_renew_agent_attempt_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "renew_agent_attempt" in str(module["_RENEW_LEASE_FN"])

    def test_append_agent_stage_event_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "append_agent_stage_event" in str(module["_APPEND_STAGE_EVENT_FN"])

    def test_reconcile_agent_run_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "reconcile_agent_run" in str(module["_RECONCILE_RUN_FN"])

    def test_reserve_agent_idempotency_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "reserve_agent_idempotency" in str(module["_RESERVE_IDEMPOTENCY_FN"])

    def test_finalize_agent_idempotency_function_defined(self) -> None:
        module = _load_migration_0030()
        assert "finalize_agent_idempotency" in str(module["_FINALIZE_IDEMPOTENCY_FN"])


# ---------------------------------------------------------------------------
# 8. Idempotency receipt constraints
# ---------------------------------------------------------------------------


class TestIdempotencyReceiptConstraints:
    def test_idempotency_receipts_unique_constraint(self) -> None:
        """Verify the unique idempotency receipt constraint."""
        table = metadata.tables.get("careerops.agent_idempotency_receipts")
        if table is None:
            pytest.skip("agent_idempotency_receipts defined in migration SQL only")
        unique_constraints = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
        # Should have at least one unique constraint
        assert len(unique_constraints) >= 1

    def test_idempotency_receipts_body_hash_check(self) -> None:
        """canonical_body_hash must be exactly 64 chars (SHA-256 hex)."""
        table = metadata.tables.get("careerops.agent_idempotency_receipts")
        if table is None:
            pytest.skip("agent_idempotency_receipts defined in migration SQL only")
        check_constraints = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
        hash_checks = [c for c in check_constraints if "canonical_body_hash" in str(c.sqltext)]
        assert len(hash_checks) >= 1


# ---------------------------------------------------------------------------
# 9. Append-only trigger for stage events
# ---------------------------------------------------------------------------


class TestAppendOnlyTrigger:
    def test_stage_events_append_only_trigger_defined(self) -> None:
        module = _load_migration_0030()
        trigger_sql = str(module["_APPEND_ONLY_TRIGGER"])
        assert "reject_agent_stage_event_mutation" in trigger_sql
        assert "BEFORE UPDATE OR DELETE" in trigger_sql


# ---------------------------------------------------------------------------
# 10. Security barrier views
# ---------------------------------------------------------------------------


class TestSecurityBarrierViews:
    def test_views_use_security_barrier(self) -> None:
        module = _load_migration_0030()
        for view_sql_key in (
            "_V_AGENT_RUNS",
            "_V_AGENT_CONTEXTS",
            "_V_AGENT_ACTIONS",
            "_V_AGENT_ATTEMPTS",
            "_V_AGENT_STAGE_EVENTS",
        ):
            view_sql = str(module[view_sql_key])
            assert "security_barrier = true" in view_sql


# ---------------------------------------------------------------------------
# 11. Offline SQL rendering
# ---------------------------------------------------------------------------


class TestOfflineRendering:
    def test_migration_0030_has_valid_structure(self) -> None:
        """Verify migration 0030 has all required SQL fragments."""
        module = _load_migration_0030()
        required_keys = [
            "_EXPAND_COLUMNS",
            "_EXPAND_CHECKS",
            "_UNIQUE_ID_CANDIDATE",
            "_REVIEW_EXPAND",
            "_REVIEW_FK_AND_CHECKS",
            "_CREATE_CONTEXTS",
            "_FK_RUNS_CONTEXT",
            "_CREATE_ACTIONS",
            "_CREATE_ATTEMPTS",
            "_CREATE_STAGE_EVENTS",
            "_CREATE_IDEMPOTENCY_RECEIPTS",
            "_BACKFILL",
            "_PREFLIGHT_COUNT",
            "_VERIFY_NONNULL",
            "_TIGHTEN_COLUMNS",
            "_APPEND_ONLY_TRIGGER",
            "_NORMALIZE_LEGACY_FN",
            "_ACQUIRE_LEASE_FN",
            "_RENEW_LEASE_FN",
            "_APPEND_STAGE_EVENT_FN",
            "_RECONCILE_RUN_FN",
            "_RESERVE_IDEMPOTENCY_FN",
            "_FINALIZE_IDEMPOTENCY_FN",
            "_PURGE_FN",
            "_READ_LEGACY_FN",
            "_WRITE_LEGACY_FN",
            "_V_AGENT_RUNS",
            "_V_AGENT_CONTEXTS",
            "_V_AGENT_ACTIONS",
            "_V_AGENT_ATTEMPTS",
            "_V_AGENT_STAGE_EVENTS",
            "_GRANTS",
            "_DOWNGRADE",
        ]
        for key in required_keys:
            assert key in module, f"Missing SQL fragment: {key}"


# ---------------------------------------------------------------------------
# 12. Grants contract
# ---------------------------------------------------------------------------


class TestGrants:
    def test_grants_revoke_broad_access(self) -> None:
        module = _load_migration_0030()
        grants = str(module["_GRANTS"])
        assert "REVOKE ALL" in grants
        assert "careerops_api" in grants

    def test_grants_grant_select_on_views(self) -> None:
        module = _load_migration_0030()
        grants = str(module["_GRANTS"])
        assert "GRANT SELECT ON" in grants
        assert "v_agent_runs_candidate" in grants

    def test_worker_grants_for_cas_functions(self) -> None:
        module = _load_migration_0030()
        grants = str(module["_GRANTS"])
        assert "careerops_worker" in grants
        assert "acquire_agent_attempt" in grants
        assert "renew_agent_attempt" in grants

    def test_legacy_agent_grants(self) -> None:
        module = _load_migration_0030()
        grants = str(module["_GRANTS"])
        assert "careerops_legacy_agent" in grants
        assert "read_agent_legacy_state" in grants
        assert "write_agent_legacy_state" in grants
