"""M5A side-effect kernel: provider_write outbox event + side-effect worker role.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-21

M5A.4 adds a ``provider_write`` outbox event type so the side-effect worker can
claim execution work via lease/heartbeat. M5A.5 grants the independent
``careerops_side_effect`` role the minimal, column-scoped privileges required to
read the authorization chain, record attempts/receipts, and append audit events
through the database-owned function. The role intentionally has no broad INSERT
or UPDATE and never touches OAuth secret handles.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIDE_EFFECT_GRANTS: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_side_effect",
    "GRANT SELECT ON careerops.action_intents, careerops.action_payload_versions, "
    "careerops.policy_decisions, careerops.approval_requests, careerops.outbox_events, "
    "careerops.side_effect_attempts, careerops.provider_receipts, careerops.audit_events "
    "TO careerops_side_effect",
    "GRANT UPDATE (status, current_payload_version_id, updated_at) "
    "ON careerops.action_intents TO careerops_side_effect",
    "GRANT UPDATE (status, available_at, lease_owner, lease_token, lease_until, "
    "attempt_count, published_at, last_error_code) ON careerops.outbox_events "
    "TO careerops_side_effect",
    "GRANT INSERT (id, action_intent_id, outbox_event_id, ordinal, state, "
    "request_fingerprint, started_at, finished_at, error_code, response_metadata) "
    "ON careerops.side_effect_attempts TO careerops_side_effect",
    "GRANT INSERT (id, side_effect_attempt_id, provider, provider_resource_id, "
    "reconciliation_key, final_state, provider_timestamp, received_at, "
    "receipt_metadata) ON careerops.provider_receipts TO careerops_side_effect",
    "GRANT EXECUTE ON FUNCTION careerops.append_audit_event("
    "uuid, timestamp with time zone, text, text, text, text, uuid, text, jsonb"
    ") TO careerops_side_effect",
)


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply(statement: str) -> None:
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _role_exists("careerops_side_effect"):
        op.execute(sa.text(statement))


def upgrade() -> None:
    # M5A.4: allow the outbox to carry provider-write work claimed by the worker.
    op.drop_constraint(
        "ck_outbox_events_event_type_values",
        "outbox_events",
        schema="careerops",
        type_="check",
    )
    op.create_check_constraint(
        "ck_outbox_events_event_type_values",
        "outbox_events",
        "event_type IN ('workflow_signal', 'internal_notification', 'provider_write')",
        schema="careerops",
    )

    # M5A.5: independent side-effect worker role with minimal column-scoped grants.
    for statement in SIDE_EFFECT_GRANTS:
        _apply(statement)


def downgrade() -> None:
    if not op.get_context().as_sql and _role_exists("careerops_side_effect"):
        op.execute(
            sa.text(
                "REVOKE EXECUTE ON FUNCTION careerops.append_audit_event("
                "uuid, timestamp with time zone, text, text, text, text, uuid, text, jsonb"
                ") FROM careerops_side_effect"
            )
        )
        op.execute(
            sa.text(
                "REVOKE INSERT ON careerops.side_effect_attempts, "
                "careerops.provider_receipts FROM careerops_side_effect"
            )
        )
        op.execute(
            sa.text(
                "REVOKE UPDATE ON careerops.action_intents, careerops.outbox_events "
                "FROM careerops_side_effect"
            )
        )
        op.execute(
            sa.text(
                "REVOKE SELECT ON careerops.action_intents, "
                "careerops.action_payload_versions, careerops.policy_decisions, "
                "careerops.approval_requests, careerops.outbox_events, "
                "careerops.side_effect_attempts, careerops.provider_receipts, "
                "careerops.audit_events FROM careerops_side_effect"
            )
        )
        op.execute(sa.text("REVOKE USAGE ON SCHEMA careerops FROM careerops_side_effect"))

    op.drop_constraint(
        "ck_outbox_events_event_type_values",
        "outbox_events",
        schema="careerops",
        type_="check",
    )
    op.create_check_constraint(
        "ck_outbox_events_event_type_values",
        "outbox_events",
        "event_type IN ('workflow_signal', 'internal_notification')",
        schema="careerops",
    )
