"""add durable Agent runs and explicit crawl executor mode"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
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


def upgrade() -> None:
    op.add_column(
        "job_sources",
        sa.Column("executor_mode", sa.String(16), server_default="http", nullable=False),
        schema="careerops",
    )
    op.create_check_constraint(
        "executor_mode_values",
        "job_sources",
        "executor_mode IN ('http', 'ego')",
        schema="careerops",
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), server_default="pending", nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column(
            "input_identities",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(64), server_default="", nullable=False),
        sa.Column("prompt_version", sa.String(64), server_default="", nullable=False),
        sa.Column("model_id", sa.String(128), server_default="", nullable=False),
        sa.Column("trace_id", sa.String(128), server_default="", nullable=False),
        sa.Column(
            "result", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("error_category", sa.String(64), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("review_decision", sa.String(16), nullable=True),
        sa.Column("reviewed_by", sa.String(128), server_default="", nullable=False),
        sa.Column("review_note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "candidate_id",
            "capability",
            "idempotency_key",
            name="uq_agent_runs_candidate_capability_key",
        ),
        sa.CheckConstraint(
            "capability IN ('job_matching', 'resume_review', 'interview_preparation')",
            name="capability_values",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'unavailable', "
            "'abstained', 'stale', 'cancelled', 'reviewed')",
            name="state_values",
        ),
        sa.CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('accepted', 'rejected', 'edited')",
            name="review_decision_values",
        ),
        sa.CheckConstraint("char_length(input_hash) = 64", name="input_hash_length"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="usage_nonnegative"),
        sa.CheckConstraint("jsonb_typeof(input_identities) = 'object'", name="input_object"),
        sa.CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="evidence_array"),
        sa.CheckConstraint("jsonb_typeof(result) = 'object'", name="result_object"),
        schema="careerops",
    )
    op.create_index(
        "ix_agent_runs_candidate_state", "agent_runs", ["candidate_id", "state"], schema="careerops"
    )
    op.create_index(
        "ix_agent_runs_candidate_created",
        "agent_runs",
        ["candidate_id", "created_at"],
        schema="careerops",
    )

    op.create_table(
        "agent_run_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "edited_result",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('accepted', 'rejected', 'edited')", name="decision_values"
        ),
        sa.CheckConstraint("jsonb_typeof(edited_result) = 'object'", name="edited_object"),
        schema="careerops",
    )
    op.create_index(
        "ix_agent_run_reviews_candidate_run",
        "agent_run_reviews",
        ["candidate_id", "run_id"],
        schema="careerops",
    )
    _apply("GRANT UPDATE (executor_mode) ON careerops.job_sources TO careerops_api")
    _apply("GRANT SELECT, INSERT, UPDATE ON careerops.agent_runs TO careerops_api")
    _apply("GRANT SELECT, INSERT ON careerops.agent_run_reviews TO careerops_api")


def downgrade() -> None:
    _apply("REVOKE SELECT, INSERT ON careerops.agent_run_reviews FROM careerops_api")
    _apply("REVOKE SELECT, INSERT, UPDATE ON careerops.agent_runs FROM careerops_api")
    _apply("REVOKE UPDATE (executor_mode) ON careerops.job_sources FROM careerops_api")
    op.drop_index(
        "ix_agent_run_reviews_candidate_run", table_name="agent_run_reviews", schema="careerops"
    )
    op.drop_table("agent_run_reviews", schema="careerops")
    op.drop_index("ix_agent_runs_candidate_created", table_name="agent_runs", schema="careerops")
    op.drop_index("ix_agent_runs_candidate_state", table_name="agent_runs", schema="careerops")
    op.drop_table("agent_runs", schema="careerops")
    op.drop_constraint("executor_mode_values", "job_sources", schema="careerops")
    op.drop_column("job_sources", "executor_mode", schema="careerops")
