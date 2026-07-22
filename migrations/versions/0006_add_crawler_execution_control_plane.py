"""add append-only crawler execution control plane

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "crawler_execution_requests",
    "crawler_execution_approvals",
    "crawler_execution_dispatches",
)

_API_GRANTS = (
    "GRANT SELECT ON careerops.crawler_execution_requests, "
    "careerops.crawler_execution_approvals, careerops.crawler_execution_dispatches "
    "TO careerops_api",
    "GRANT INSERT (id, owner_user_id, action_intent_id, payload_version_id, "
    "payload_hash, manifest_path, request_artifact_path, manifest_sha256, "
    "request_sha256, reviewed_plan_sha256, source_ids, reason, expires_at) "
    "ON careerops.crawler_execution_requests TO careerops_api",
    "GRANT INSERT (id, request_id, decided_by_user_id, decision, decision_reason, "
    "approval_artifact_path, approval_artifact_sha256) "
    "ON careerops.crawler_execution_approvals TO careerops_api",
    "GRANT INSERT (id, request_id, action_intent_id, outbox_event_id, execution_key) "
    "ON careerops.crawler_execution_dispatches TO careerops_api",
)

_OUTBOX_GRANTS = (
    "GRANT SELECT ON careerops.crawler_execution_requests, "
    "careerops.crawler_execution_approvals, careerops.crawler_execution_dispatches "
    "TO careerops_outbox",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.crawler_execution_requests, "
    "careerops.crawler_execution_approvals, careerops.crawler_execution_dispatches "
    "TO careerops_readonly",
)

_API_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_api" for table_name in APPEND_ONLY_TABLES
)
_OUTBOX_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_outbox"
    for table_name in APPEND_ONLY_TABLES
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


def _install_approval_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_crawler_execution_approval_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                request_expires_at timestamp with time zone;
                request_owner_user_id uuid;
            BEGIN
                NEW.decided_at := CURRENT_TIMESTAMP;

                SELECT request.expires_at, request.owner_user_id
                INTO request_expires_at, request_owner_user_id
                FROM careerops.crawler_execution_requests AS request
                WHERE request.id = NEW.request_id
                FOR KEY SHARE;

                IF request_expires_at IS NULL THEN
                    RAISE EXCEPTION 'crawler execution request % is missing', NEW.request_id
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.decided_by_user_id <> request_owner_user_id THEN
                    RAISE EXCEPTION 'crawler execution approval requires request owner'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.decision = 'approved' AND NEW.decided_at >= request_expires_at THEN
                    RAISE EXCEPTION 'crawler execution request % is expired for approval',
                        NEW.request_id
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
            "REVOKE ALL ON FUNCTION careerops.enforce_crawler_execution_approval_insert() "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_crawler_execution_approvals_insert_guard "
            "BEFORE INSERT ON careerops.crawler_execution_approvals "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_crawler_execution_approval_insert()"
        )
    )


def _drop_approval_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_crawler_execution_approvals_insert_guard "
            "ON careerops.crawler_execution_approvals"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_crawler_execution_approval_insert()")
    )


def _install_dispatch_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_crawler_execution_dispatch_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                request_expires_at timestamp with time zone;
                approval_decision text;
                outbox_event_key text;
                outbox_event_type text;
                outbox_status text;
            BEGIN
                NEW.dispatched_at := CURRENT_TIMESTAMP;

                SELECT request.expires_at
                INTO request_expires_at
                FROM careerops.crawler_execution_requests AS request
                WHERE request.id = NEW.request_id
                  AND request.action_intent_id = NEW.action_intent_id
                FOR KEY SHARE;

                IF request_expires_at IS NULL THEN
                    RAISE EXCEPTION 'crawler execution request % does not match dispatch',
                        NEW.request_id
                        USING ERRCODE = '23503';
                END IF;

                IF request_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'crawler execution request % is expired for dispatch',
                        NEW.request_id
                        USING ERRCODE = '23514';
                END IF;

                SELECT approval.decision
                INTO approval_decision
                FROM careerops.crawler_execution_approvals AS approval
                WHERE approval.request_id = NEW.request_id
                FOR KEY SHARE;

                IF approval_decision IS DISTINCT FROM 'approved' THEN
                    RAISE EXCEPTION 'crawler execution dispatch requires approved request'
                        USING ERRCODE = '23514';
                END IF;

                SELECT event.event_key, event.event_type, event.status
                INTO outbox_event_key, outbox_event_type, outbox_status
                FROM careerops.outbox_events AS event
                WHERE event.id = NEW.outbox_event_id
                  AND event.action_intent_id = NEW.action_intent_id
                FOR KEY SHARE;

                IF outbox_event_key IS NULL THEN
                    RAISE EXCEPTION 'crawler execution outbox event % is missing',
                        NEW.outbox_event_id
                        USING ERRCODE = '23503';
                END IF;

                IF outbox_event_key <> NEW.execution_key THEN
                    RAISE EXCEPTION 'crawler execution key must match outbox event key'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.execution_key NOT LIKE 'crawler-execution:%' THEN
                    RAISE EXCEPTION 'crawler execution key must use crawler-execution prefix'
                        USING ERRCODE = '23514';
                END IF;

                IF outbox_event_type <> 'workflow_signal' OR outbox_status <> 'pending' THEN
                    RAISE EXCEPTION 'crawler execution dispatch requires pending workflow signal'
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
            "REVOKE ALL ON FUNCTION careerops.enforce_crawler_execution_dispatch_insert() "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_crawler_execution_dispatches_insert_guard "
            "BEFORE INSERT ON careerops.crawler_execution_dispatches "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_crawler_execution_dispatch_insert()"
        )
    )


def _drop_dispatch_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_crawler_execution_dispatches_insert_guard "
            "ON careerops.crawler_execution_dispatches"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_crawler_execution_dispatch_insert()")
    )


def upgrade() -> None:
    op.create_table(
        "crawler_execution_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.Column("request_artifact_path", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_plan_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_execution_requests_payload_hash_format"),
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_execution_requests_manifest_sha256_format"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_execution_requests_request_sha256_format"),
        ),
        sa.CheckConstraint(
            "reviewed_plan_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_execution_requests_reviewed_plan_sha256_format"),
        ),
        sa.CheckConstraint(
            "manifest_path ~ '^[A-Za-z0-9._/-]+$' "
            "AND left(manifest_path, 1) <> '/' "
            "AND manifest_path !~ '(^|/)\\.\\.(/|$)'",
            name=op.f("ck_crawler_execution_requests_manifest_path_safe_relative"),
        ),
        sa.CheckConstraint(
            "request_artifact_path ~ '^[A-Za-z0-9._/-]+$' "
            "AND left(request_artifact_path, 1) <> '/' "
            "AND request_artifact_path !~ '(^|/)\\.\\.(/|$)'",
            name=op.f("ck_crawler_execution_requests_request_artifact_path_safe_relative"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_ids) = 'array'",
            name=op.f("ck_crawler_execution_requests_source_ids_array"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_crawler_execution_requests_reason_nonempty"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_crawler_execution_requests_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_crawler_execution_requests_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
            ],
            name="fk_crawler_execution_requests_payload_version_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_crawler_execution_requests_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_crawler_execution_requests_owner_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawler_execution_requests")),
        sa.UniqueConstraint(
            "action_intent_id",
            "payload_version_id",
            name=op.f("uq_crawler_execution_requests_intent_payload"),
        ),
        sa.UniqueConstraint(
            "id",
            "action_intent_id",
            name=op.f("uq_crawler_execution_requests_id_intent"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_execution_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=False),
        sa.Column("approval_artifact_path", sa.Text(), nullable=True),
        sa.Column("approval_artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name=op.f("ck_crawler_execution_approvals_decision_values"),
        ),
        sa.CheckConstraint(
            "btrim(decision_reason) <> ''",
            name=op.f("ck_crawler_execution_approvals_decision_reason_nonempty"),
        ),
        sa.CheckConstraint(
            "(decision = 'approved' AND approval_artifact_path IS NOT NULL "
            "AND approval_artifact_sha256 IS NOT NULL) OR "
            "(decision = 'rejected' AND approval_artifact_path IS NULL "
            "AND approval_artifact_sha256 IS NULL)",
            name=op.f("ck_crawler_execution_approvals_approval_artifact_matches_decision"),
        ),
        sa.CheckConstraint(
            "approval_artifact_sha256 IS NULL OR approval_artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_execution_approvals_approval_artifact_sha256_format"),
        ),
        sa.CheckConstraint(
            "approval_artifact_path IS NULL OR ("
            "approval_artifact_path ~ '^[A-Za-z0-9._/-]+$' "
            "AND left(approval_artifact_path, 1) <> '/' "
            "AND approval_artifact_path !~ '(^|/)\\.\\.(/|$)')",
            name=op.f("ck_crawler_execution_approvals_approval_artifact_path_safe_relative"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_crawler_execution_approvals_decided_by_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["careerops.crawler_execution_requests.id"],
            name=op.f("fk_crawler_execution_approvals_request_id_crawler_execution_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawler_execution_approvals")),
        sa.UniqueConstraint(
            "request_id",
            name=op.f("uq_crawler_execution_approvals_request_id"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_execution_dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("execution_key", sa.Text(), nullable=False),
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(execution_key) <> ''",
            name=op.f("ck_crawler_execution_dispatches_execution_key_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "outbox_event_id"],
            ["careerops.outbox_events.action_intent_id", "careerops.outbox_events.id"],
            name="fk_crawler_execution_dispatches_outbox_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "action_intent_id"],
            [
                "careerops.crawler_execution_requests.id",
                "careerops.crawler_execution_requests.action_intent_id",
            ],
            name="fk_crawler_execution_dispatches_request_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawler_execution_dispatches")),
        sa.UniqueConstraint(
            "execution_key",
            name=op.f("uq_crawler_execution_dispatches_execution_key"),
        ),
        sa.UniqueConstraint(
            "outbox_event_id",
            name=op.f("uq_crawler_execution_dispatches_outbox_event_id"),
        ),
        sa.UniqueConstraint(
            "request_id",
            name=op.f("uq_crawler_execution_dispatches_request_id"),
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_crawler_execution_requests_owner_created_at",
        "crawler_execution_requests",
        ["owner_user_id", "created_at"],
        unique=False,
        schema="careerops",
    )
    op.create_index(
        "ix_crawler_execution_requests_expires_at",
        "crawler_execution_requests",
        ["expires_at"],
        unique=False,
        schema="careerops",
    )
    op.create_index(
        "ix_crawler_execution_approvals_decision_created_at",
        "crawler_execution_approvals",
        ["decision", "created_at"],
        unique=False,
        schema="careerops",
    )
    _install_append_only_guards()
    _install_approval_guard()
    _install_dispatch_guard()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_outbox", _OUTBOX_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_outbox", _OUTBOX_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_dispatch_guard()
    _drop_approval_guard()
    _drop_append_only_guards()
    op.drop_index(
        "ix_crawler_execution_approvals_decision_created_at",
        table_name="crawler_execution_approvals",
        schema="careerops",
    )
    op.drop_index(
        "ix_crawler_execution_requests_expires_at",
        table_name="crawler_execution_requests",
        schema="careerops",
    )
    op.drop_index(
        "ix_crawler_execution_requests_owner_created_at",
        table_name="crawler_execution_requests",
        schema="careerops",
    )
    op.drop_table("crawler_execution_dispatches", schema="careerops")
    op.drop_table("crawler_execution_approvals", schema="careerops")
    op.drop_table("crawler_execution_requests", schema="careerops")
