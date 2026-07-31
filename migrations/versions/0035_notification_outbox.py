"""add durable notification outbox

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        schema="careerops",
    )
    op.create_index(
        "ix_notification_outbox_candidate_pending",
        "notification_outbox",
        ["candidate_id", "created_at"],
        schema="careerops",
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.execute(
        "GRANT SELECT, INSERT ON careerops.notification_outbox "
        "TO careerops_api, careerops_worker"
    )
    op.execute(
        "GRANT UPDATE (delivered_at) ON careerops.notification_outbox "
        "TO careerops_api, careerops_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT ON careerops.agent_actions "
        "TO careerops_api, careerops_worker"
    )
    op.execute(
        "GRANT UPDATE (state, updated_at) ON careerops.agent_actions "
        "TO careerops_api, careerops_worker"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION careerops.append_audit_event "
        "TO careerops_worker"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE EXECUTE ON FUNCTION careerops.append_audit_event "
        "FROM careerops_worker"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON careerops.notification_outbox "
        "FROM careerops_api, careerops_worker"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON careerops.agent_actions "
        "FROM careerops_api, careerops_worker"
    )
    op.drop_index(
        "ix_notification_outbox_candidate_pending",
        table_name="notification_outbox",
        schema="careerops",
    )
    op.drop_table("notification_outbox", schema="careerops")
