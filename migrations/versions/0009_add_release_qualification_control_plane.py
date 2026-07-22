"""add release qualification control plane

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "release_qualifications",
    "release_qualification_evidence",
    "release_qualification_decisions",
)

_API_GRANTS = (
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

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.release_qualifications, "
    "careerops.release_qualification_evidence, careerops.release_qualification_decisions "
    "TO careerops_readonly",
)

_API_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_api" for table_name in APPEND_ONLY_TABLES
)
_READONLY_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_readonly"
    for table_name in APPEND_ONLY_TABLES
)


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


def _install_evidence_insert_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_release_qualification_evidence_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_status text;
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:'
                        || NEW.qualification_id::text,
                        0
                    )
                );

                SELECT COALESCE(
                    (
                        SELECT decision.to_status
                        FROM careerops.release_qualification_decisions AS decision
                        WHERE decision.qualification_id = qualification.id
                        ORDER BY decision.sequence DESC
                        LIMIT 1
                    ),
                    qualification.status
                )
                INTO v_status
                FROM careerops.release_qualifications AS qualification
                WHERE qualification.id = NEW.qualification_id;

                IF v_status IS NULL THEN
                    RAISE EXCEPTION 'release qualification % is missing', NEW.qualification_id
                        USING ERRCODE = '23503';
                END IF;

                IF v_status NOT IN ('draft', 'evaluating') THEN
                    RAISE EXCEPTION 'release qualification evidence is frozen after review begins'
                        USING ERRCODE = '55000';
                END IF;

                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "careerops.enforce_release_qualification_evidence_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_release_qualification_evidence_insert_guard "
            "BEFORE INSERT ON careerops.release_qualification_evidence "
            "FOR EACH ROW EXECUTE FUNCTION "
            "careerops.enforce_release_qualification_evidence_insert()"
        )
    )


def _drop_evidence_insert_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_release_qualification_evidence_insert_guard "
            "ON careerops.release_qualification_evidence"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_release_qualification_evidence_insert()")
    )


def _install_decision_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_release_qualification_decision_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_initial_status text;
                v_latest_status text;
                v_previous_actor text;
                v_previous_evidence_ids uuid[];
                v_current_evidence_ids uuid[];
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:'
                        || NEW.qualification_id::text,
                        0
                    )
                );

                SELECT qualification.status
                INTO v_initial_status
                FROM careerops.release_qualifications AS qualification
                WHERE qualification.id = NEW.qualification_id
                FOR KEY SHARE;

                IF v_initial_status IS NULL THEN
                    RAISE EXCEPTION 'release qualification % is missing', NEW.qualification_id
                        USING ERRCODE = '23503';
                END IF;

                SELECT decision.to_status, decision.actor_id, decision.evidence_ids
                INTO v_latest_status, v_previous_actor, v_previous_evidence_ids
                FROM careerops.release_qualification_decisions AS decision
                WHERE decision.qualification_id = NEW.qualification_id
                ORDER BY decision.sequence DESC
                LIMIT 1;

                v_latest_status := COALESCE(v_latest_status, v_initial_status);

                SELECT COALESCE(
                    pg_catalog.array_agg(evidence.id ORDER BY evidence.id),
                    ARRAY[]::uuid[]
                )
                INTO v_current_evidence_ids
                FROM careerops.release_qualification_evidence AS evidence
                WHERE evidence.qualification_id = NEW.qualification_id;

                IF NEW.from_status IS DISTINCT FROM v_latest_status THEN
                    RAISE EXCEPTION 'release qualification decision from_status is stale'
                        USING ERRCODE = '23514';
                END IF;

                IF NOT (
                    (NEW.from_status = 'draft'
                        AND NEW.to_status = 'evaluating'
                        AND NEW.decision_role = 'runner')
                    OR (NEW.from_status = 'evaluating'
                        AND NEW.to_status = 'pending_independent_review'
                        AND NEW.decision_role = 'reviewer')
                    OR (NEW.from_status = 'pending_independent_review'
                        AND NEW.to_status IN ('qualified', 'rejected')
                        AND NEW.decision_role = 'operator')
                    OR (NEW.from_status = 'qualified'
                        AND NEW.to_status IN ('expired', 'revoked')
                        AND NEW.decision_role = 'operator')
                ) THEN
                    RAISE EXCEPTION 'release qualification decision transition is invalid'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status = 'evaluating'
                   AND pg_catalog.cardinality(NEW.evidence_ids) <> 0 THEN
                    RAISE EXCEPTION 'evaluation opening must not bind evidence'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status = 'pending_independent_review'
                   AND (
                       pg_catalog.cardinality(NEW.evidence_ids) = 0
                       OR NEW.evidence_ids IS DISTINCT FROM v_current_evidence_ids
                   ) THEN
                    RAISE EXCEPTION 'review must bind the complete immutable evidence set'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status IN ('qualified', 'rejected')
                   AND (
                       pg_catalog.cardinality(NEW.evidence_ids) = 0
                       OR NEW.evidence_ids IS DISTINCT FROM v_previous_evidence_ids
                       OR NEW.evidence_ids IS DISTINCT FROM v_current_evidence_ids
                   ) THEN
                    RAISE EXCEPTION 'final decision evidence set was not independently reviewed'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status = 'qualified'
                   AND EXISTS (
                       SELECT 1
                       FROM careerops.release_qualification_evidence AS evidence
                       WHERE evidence.qualification_id = NEW.qualification_id
                         AND evidence.id = ANY(NEW.evidence_ids)
                         AND NOT evidence.metrics @> '{
                             "false_negative": 0,
                             "autonomous_provider_write_attempts": 0,
                             "no_autonomous_writes": true
                         }'::jsonb
                   ) THEN
                    RAISE EXCEPTION 'qualified release evidence contains a fail-open observation'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status = 'pending_independent_review'
                   AND EXISTS (
                       SELECT 1
                       FROM careerops.release_qualification_evidence AS evidence
                       WHERE evidence.qualification_id = NEW.qualification_id
                         AND evidence.id = ANY(NEW.evidence_ids)
                         AND evidence.runner_actor = NEW.actor_id
                   ) THEN
                    RAISE EXCEPTION 'release qualification reviewer must be independent'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.to_status IN ('qualified', 'rejected')
                   AND (
                       NEW.actor_id = v_previous_actor
                       OR EXISTS (
                           SELECT 1
                           FROM careerops.release_qualification_evidence AS evidence
                           WHERE evidence.qualification_id = NEW.qualification_id
                             AND evidence.id = ANY(NEW.evidence_ids)
                             AND evidence.runner_actor = NEW.actor_id
                       )
                   ) THEN
                    RAISE EXCEPTION 'release qualification operator must be independent'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "careerops.enforce_release_qualification_decision_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_release_qualification_decisions_insert_guard "
            "BEFORE INSERT ON careerops.release_qualification_decisions "
            "FOR EACH ROW EXECUTE FUNCTION "
            "careerops.enforce_release_qualification_decision_insert()"
        )
    )


def _drop_decision_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_release_qualification_decisions_insert_guard "
            "ON careerops.release_qualification_decisions"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_release_qualification_decision_insert()")
    )


def upgrade() -> None:
    op.create_table(
        "release_qualifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("action_name", sa.Text(), nullable=False),
        sa.Column("rollout_mode", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("implementation_hash", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("image_digest", sa.Text(), nullable=True),
        sa.Column("migration_revision", sa.Text(), nullable=False),
        sa.Column("oauth_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_ref_hash", sa.String(length=64), nullable=False),
        sa.Column("network_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("reconcile_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("hard_stop_hash", sa.String(length=64), nullable=False),
        sa.Column("sensitive_field_hash", sa.String(length=64), nullable=False),
        sa.Column("kill_switch_hash", sa.String(length=64), nullable=False),
        sa.Column("fixture_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("fault_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("holdout_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("live_sample_manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rollout_mode IN ('shadow', 'review_required', 'synthetic_sandbox', "
            "'limited_autopilot', 'expanded_autopilot')",
            name=op.f("ck_release_qualifications_rollout_mode_values"),
        ),
        sa.CheckConstraint(
            "status = 'draft'",
            name=op.f("ck_release_qualifications_status_initial_draft"),
        ),
        sa.CheckConstraint(
            "implementation_hash ~ '^[0-9a-f]{64}$' "
            "AND config_hash ~ '^[0-9a-f]{64}$' "
            "AND policy_hash ~ '^[0-9a-f]{64}$' "
            "AND dataset_hash ~ '^[0-9a-f]{64}$' "
            "AND oauth_scope_hash ~ '^[0-9a-f]{64}$' "
            "AND credential_ref_hash ~ '^[0-9a-f]{64}$' "
            "AND network_policy_hash ~ '^[0-9a-f]{64}$' "
            "AND reconcile_policy_hash ~ '^[0-9a-f]{64}$' "
            "AND hard_stop_hash ~ '^[0-9a-f]{64}$' "
            "AND sensitive_field_hash ~ '^[0-9a-f]{64}$' "
            "AND kill_switch_hash ~ '^[0-9a-f]{64}$' "
            "AND fixture_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND fault_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (holdout_manifest_sha256 IS NULL "
            "OR holdout_manifest_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (live_sample_manifest_sha256 IS NULL "
            "OR live_sample_manifest_sha256 ~ '^[0-9a-f]{64}$')",
            name=op.f("ck_release_qualifications_hashes_sha256"),
        ),
        sa.CheckConstraint(
            "git_commit ~ '^[0-9a-f]{7,64}$'",
            name=op.f("ck_release_qualifications_git_commit_format"),
        ),
        sa.CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_release_qualifications_image_digest_format"),
        ),
        sa.CheckConstraint(
            "btrim(capability) <> '' AND btrim(action_name) <> '' "
            "AND btrim(provider) <> '' AND btrim(adapter_id) <> '' "
            "AND btrim(adapter_version) <> '' AND btrim(migration_revision) <> '' "
            "AND btrim(created_by) <> ''",
            name=op.f("ck_release_qualifications_text_nonempty"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_release_qualifications_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_release_qualifications_requested_by_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_qualifications")),
        schema="careerops",
    )
    op.create_index(
        "ix_release_qualifications_binding_created_at",
        "release_qualifications",
        ["capability", "action_name", "rollout_mode", "provider", "adapter_id", "created_at"],
        unique=False,
        schema="careerops",
    )

    op.create_table(
        "release_qualification_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qualification_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_uri", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("runner_user_id", sa.Uuid(), nullable=False),
        sa.Column("runner_actor", sa.Text(), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("sample_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "evidence_kind IN ('fixture', 'fault_injection', 'holdout', "
            "'live_sample', 'metrics', 'audit_drill')",
            name=op.f("ck_release_qualification_evidence_kind_values"),
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' AND sample_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_release_qualification_evidence_hashes_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metrics) = 'object'",
            name=op.f("ck_release_qualification_evidence_metrics_object"),
        ),
        sa.CheckConstraint(
            "artifact_uri ~ '^[A-Za-z0-9._:/-]+$' "
            "AND artifact_uri !~ '(^|/)\\.\\.(/|$)' "
            "AND btrim(run_id) <> '' AND btrim(runner_actor) <> ''",
            name=op.f("ck_release_qualification_evidence_text_safe_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["qualification_id"],
            ["careerops.release_qualifications.id"],
            name=op.f("fk_release_qualification_evidence_qualification_id_release_qualifications"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runner_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_release_qualification_evidence_runner_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_qualification_evidence")),
        sa.UniqueConstraint(
            "qualification_id",
            "evidence_kind",
            "artifact_sha256",
            name="uq_release_qualification_evidence_kind_artifact",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_release_qualification_evidence_qualification_created_at",
        "release_qualification_evidence",
        ["qualification_id", "created_at"],
        unique=False,
        schema="careerops",
    )

    op.create_table(
        "release_qualification_decisions",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qualification_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("decision_role", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default=sa.text("ARRAY[]::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IN ('draft', 'evaluating', 'pending_independent_review', 'qualified')",
            name=op.f("ck_release_qualification_decisions_from_status_values"),
        ),
        sa.CheckConstraint(
            "to_status IN ('evaluating', 'pending_independent_review', 'qualified', "
            "'rejected', 'expired', 'revoked')",
            name=op.f("ck_release_qualification_decisions_to_status_values"),
        ),
        sa.CheckConstraint(
            "decision_role IN ('runner', 'reviewer', 'operator')",
            name=op.f("ck_release_qualification_decisions_role_values"),
        ),
        sa.CheckConstraint(
            "array_position(evidence_ids, NULL) IS NULL",
            name=op.f("ck_release_qualification_decisions_evidence_ids_no_nulls"),
        ),
        sa.CheckConstraint(
            "btrim(actor_id) <> '' AND "
            "(decision_role <> 'operator' OR decided_by_user_id IS NOT NULL)",
            name=op.f("ck_release_qualification_decisions_actor_binding"),
        ),
        sa.CheckConstraint(
            "evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_release_qualification_decisions_evidence_sha256_format"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_release_qualification_decisions_reason_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["qualification_id"],
            ["careerops.release_qualifications.id"],
            name=op.f("fk_release_qualification_decisions_qualification_id_release_qualifications"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_release_qualification_decisions_decided_by_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_qualification_decisions")),
        sa.UniqueConstraint(
            "sequence",
            name=op.f("uq_release_qualification_decisions_sequence"),
        ),
        sa.UniqueConstraint(
            "qualification_id",
            "to_status",
            name="uq_release_qualification_decisions_status_once",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_release_qualification_decisions_qualification_sequence",
        "release_qualification_decisions",
        ["qualification_id", "sequence"],
        unique=False,
        schema="careerops",
    )

    _install_evidence_insert_guard()
    _install_decision_guard()
    _install_append_only_guards()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_append_only_guards()
    _drop_decision_guard()
    _drop_evidence_insert_guard()
    op.drop_index(
        "ix_release_qualification_decisions_qualification_sequence",
        table_name="release_qualification_decisions",
        schema="careerops",
    )
    op.drop_table("release_qualification_decisions", schema="careerops")
    op.drop_index(
        "ix_release_qualification_evidence_qualification_created_at",
        table_name="release_qualification_evidence",
        schema="careerops",
    )
    op.drop_table("release_qualification_evidence", schema="careerops")
    op.drop_index(
        "ix_release_qualifications_binding_created_at",
        table_name="release_qualifications",
        schema="careerops",
    )
    op.drop_table("release_qualifications", schema="careerops")
