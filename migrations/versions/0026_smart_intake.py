"""add candidate-owned short-lived smart intake previews"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement: str) -> None:
    if not op.get_context().as_sql and not _role_exists("careerops_api"):
        return
    op.execute(sa.text(statement))


def _apply_retention(statement: str) -> None:
    if not op.get_context().as_sql and not _role_exists("careerops_retention"):
        return
    op.execute(sa.text(statement))


def upgrade() -> None:
    op.create_table(
        "smart_intake_previews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("context_digest", sa.String(64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column(
            "context_refs",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "fields", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("state", sa.String(24), server_default="unavailable", nullable=False),
        sa.Column("claim_state", sa.String(16), server_default="finalized", nullable=False),
        sa.Column("claim_token", sa.String(64), server_default="", nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("schema_version", sa.String(64), server_default="", nullable=False),
        sa.Column("prompt_version", sa.String(64), server_default="", nullable=False),
        sa.Column("model_id", sa.String(128), server_default="", nullable=False),
        sa.Column("capability_state", sa.String(64), server_default="", nullable=False),
        sa.Column("trace_id", sa.String(128), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "candidate_id",
            "target",
            "idempotency_key",
            name="uq_smart_intake_previews_candidate_target_key",
        ),
        sa.UniqueConstraint(
            "id",
            "candidate_id",
            name="uq_smart_intake_previews_id_candidate",
        ),
        sa.CheckConstraint("target IN ('profile', 'interview_context')", name="target_values"),
        sa.CheckConstraint(
            "state IN ("
            "'unavailable', 'ready', 'abstained', 'invalid', 'stale', 'expired', 'revoked'"
            ")",
            name="state_values",
        ),
        sa.CheckConstraint(
            "claim_state IN ('pending', 'finalized', 'reclaimed')", name="claim_state_values"
        ),
        sa.CheckConstraint("char_length(request_fingerprint) = 64", name="request_hash_length"),
        sa.CheckConstraint("char_length(input_digest) = 64", name="input_hash_length"),
        sa.CheckConstraint("char_length(context_digest) = 64", name="context_hash_length"),
        sa.CheckConstraint("char_length(input_text) <= 12000", name="input_length"),
        sa.CheckConstraint("jsonb_typeof(context_refs) = 'object'", name="context_object"),
        sa.CheckConstraint("jsonb_typeof(fields) = 'array'", name="fields_array"),
        schema="careerops",
    )
    op.create_index(
        "ix_smart_intake_previews_candidate_created",
        "smart_intake_previews",
        ["candidate_id", "created_at"],
        schema="careerops",
    )
    op.create_index(
        "ix_smart_intake_previews_expiry",
        "smart_intake_previews",
        ["expires_at", "purged_at"],
        schema="careerops",
    )

    op.create_table(
        "smart_intake_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "preview_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["preview_id", "candidate_id"],
            [
                "careerops.smart_intake_previews.id",
                "careerops.smart_intake_previews.candidate_id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["careerops.candidates.id"],
            ondelete="CASCADE",
        ),
        sa.Column("apply_idempotency_key", sa.String(128), nullable=False),
        sa.Column("decision_set_hash", sa.String(64), nullable=False),
        sa.Column(
            "decisions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "draft_patch", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "preview_id",
            "apply_idempotency_key",
            name="uq_smart_intake_decisions_preview_apply_key",
        ),
        sa.CheckConstraint("char_length(decision_set_hash) = 64", name="decision_hash_length"),
        sa.CheckConstraint("jsonb_typeof(decisions) = 'array'", name="decisions_array"),
        sa.CheckConstraint("jsonb_typeof(draft_patch) = 'object'", name="patch_object"),
        schema="careerops",
    )
    op.create_index(
        "ix_smart_intake_decisions_candidate_created",
        "smart_intake_decisions",
        ["candidate_id", "created_at"],
        schema="careerops",
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION careerops.reject_smart_intake_decision_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY INVOKER
            SET search_path = careerops, pg_catalog
            AS $function$
            BEGIN
                RAISE EXCEPTION 'smart intake decisions are append-only'
                    USING ERRCODE = '55000';
            END;
            $function$;
            CREATE TRIGGER smart_intake_decisions_append_only
            BEFORE UPDATE OR DELETE ON careerops.smart_intake_decisions
            FOR EACH ROW EXECUTE FUNCTION careerops.reject_smart_intake_decision_mutation();
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION careerops.purge_smart_intake_previews(
                p_now timestamptz DEFAULT now()
            ) RETURNS integer
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = careerops, pg_catalog
            AS $function$
            DECLARE purged integer;
            BEGIN
                UPDATE careerops.smart_intake_previews
                SET input_text = '',
                    context_refs = '{}'::jsonb,
                    fields = '[]'::jsonb,
                    state = 'expired',
                    claim_state = 'finalized',
                    claim_token = '',
                    claim_expires_at = NULL,
                    purged_at = COALESCE(purged_at, p_now)
                WHERE purged_at IS NULL
                  AND expires_at <= p_now - interval '24 hours';
                GET DIAGNOSTICS purged = ROW_COUNT;
                RETURN purged;
            END;
            $function$;
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.purge_smart_intake_previews(timestamptz) FROM PUBLIC"
        )
    )
    _apply_retention(
        "GRANT EXECUTE ON FUNCTION careerops.purge_smart_intake_previews(timestamptz) "
        "TO careerops_retention"
    )

    _apply("GRANT SELECT, INSERT ON careerops.smart_intake_previews TO careerops_api")
    _apply(
        "GRANT UPDATE (input_text, context_refs, fields, state, claim_state, claim_token, "
        "claim_expires_at, prompt_version, model_id, capability_state, purged_at, revoked_at) "
        "ON careerops.smart_intake_previews TO careerops_api"
    )
    _apply("GRANT SELECT, INSERT ON careerops.smart_intake_decisions TO careerops_api")
    if op.get_context().as_sql or _role_exists("careerops_retention"):
        op.execute(
            sa.text("GRANT SELECT ON careerops.smart_intake_previews TO careerops_retention")
        )
        op.execute(
            sa.text("GRANT SELECT ON careerops.smart_intake_decisions TO careerops_retention")
        )


def downgrade() -> None:
    _apply_retention(
        "REVOKE EXECUTE ON FUNCTION careerops.purge_smart_intake_previews(timestamptz) "
        "FROM careerops_retention"
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS smart_intake_decisions_append_only "
            "ON careerops.smart_intake_decisions"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops.reject_smart_intake_decision_mutation()"))
    op.execute(sa.text("DROP FUNCTION careerops.purge_smart_intake_previews(timestamptz)"))
    if op.get_context().as_sql or _role_exists("careerops_retention"):
        op.execute(
            sa.text("REVOKE SELECT ON careerops.smart_intake_previews FROM careerops_retention")
        )
        op.execute(
            sa.text("REVOKE SELECT ON careerops.smart_intake_decisions FROM careerops_retention")
        )
    _apply("REVOKE SELECT, INSERT ON careerops.smart_intake_decisions FROM careerops_api")
    _apply(
        "REVOKE UPDATE (input_text, context_refs, fields, state, claim_state, claim_token, "
        "claim_expires_at, prompt_version, model_id, capability_state, purged_at, revoked_at) "
        "ON careerops.smart_intake_previews FROM careerops_api"
    )
    _apply("REVOKE SELECT, INSERT ON careerops.smart_intake_previews FROM careerops_api")
    op.drop_index(
        "ix_smart_intake_decisions_candidate_created",
        table_name="smart_intake_decisions",
        schema="careerops",
    )
    op.drop_table("smart_intake_decisions", schema="careerops")
    op.drop_index(
        "ix_smart_intake_previews_expiry", table_name="smart_intake_previews", schema="careerops"
    )
    op.drop_index(
        "ix_smart_intake_previews_candidate_created",
        table_name="smart_intake_previews",
        schema="careerops",
    )
    op.drop_table("smart_intake_previews", schema="careerops")
