"""add owner-scoped gmail readonly sync control plane

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-20
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "gmail_message_signals",
    "gmail_signal_proposals",
    "gmail_signal_review_decisions",
    "gmail_command_receipts",
)

_GMAIL_TABLES = (
    "gmail_accounts",
    "gmail_sync_runs",
    "gmail_message_signals",
    "gmail_signal_proposals",
    "gmail_signal_review_decisions",
    "gmail_command_receipts",
)

_API_GRANTS = (
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_register_account(uuid, uuid, text, text, text, text, text, text) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_request_sync(uuid, uuid, text, text, text) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_list_accounts(uuid, integer) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_account_status(uuid, uuid) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_list_sync_runs(uuid, uuid, integer) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_list_proposals(uuid, uuid, integer) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_review_proposal(uuid, uuid, text, uuid, uuid, text, text, text, text) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_reset_history(uuid, uuid, text, text, text, text) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_revoke_account(uuid, uuid, text, text, text) TO careerops_api",
)

_MAILBOX_GRANTS = (
    "GRANT SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs TO careerops_mailbox",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_claim_sync_runs(text, integer, integer) TO careerops_mailbox",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_record_message_signal(uuid, uuid, text, text, text, text, text, text, text, text, timestamptz, text, text, numeric, jsonb, text, text, text, jsonb, text, text) TO careerops_mailbox",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_defer_sync_run(uuid, uuid, text, text, integer, text) TO careerops_mailbox",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_complete_sync_run(uuid, uuid, text, text, integer) TO careerops_mailbox",
    "GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_fail_sync_run(uuid, uuid, text) TO careerops_mailbox",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs, careerops.gmail_message_signals, careerops.gmail_signal_proposals, careerops.gmail_signal_review_decisions, careerops.gmail_command_receipts TO careerops_readonly",
)

_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_api, careerops_workflow, careerops_mailbox, careerops_outbox, careerops_readonly"
    for table_name in _GMAIL_TABLES
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


def upgrade() -> None:
    op.create_table(
        "gmail_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "oauth_credential_reference_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.oauth_credential_references.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(16), server_default="gmail", nullable=False),
        sa.Column("account_subject", sa.Text(), nullable=False),
        sa.Column("sync_mode", sa.String(16), server_default="polling", nullable=False),
        sa.Column("dedicated_account_attested", sa.Boolean(), nullable=False),
        sa.Column("byo_account_attested", sa.Boolean(), nullable=False),
        sa.Column("publishing_status", sa.String(16), nullable=False),
        sa.Column("publishing_evidence_sha256", sa.String(64)),
        sa.Column("credential_store_evidence_sha256", sa.String(64), nullable=False),
        sa.Column(
            "in_production_attested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("last_history_id", sa.Text()),
        sa.Column("next_page_token", sa.Text()),
        sa.Column("anchor_received_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_full_sync_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(24), server_default="active", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "provider",
            "account_subject",
            name="uq_gmail_accounts_owner_provider_subject",
        ),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_gmail_accounts_owner_idempotency_key"
        ),
        sa.CheckConstraint("provider = 'gmail'", name="provider_values"),
        sa.CheckConstraint("sync_mode = 'polling'", name="sync_mode_readonly_polling_only"),
        sa.CheckConstraint("dedicated_account_attested IS TRUE", name="dedicated_account_required"),
        sa.CheckConstraint("byo_account_attested IS TRUE", name="byo_account_required"),
        sa.CheckConstraint(
            "credential_store_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="credential_store_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "publishing_status IN ('testing', 'in_production')", name="publishing_status_values"
        ),
        sa.CheckConstraint(
            "publishing_evidence_sha256 IS NULL OR publishing_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="publishing_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'sync_required', 'revoked', 'blocked')",
            name="status_values",
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        schema="careerops",
    )
    op.create_table(
        "gmail_sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "gmail_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("fencing_token", sa.Uuid(), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("history_start_id", sa.Text()),
        sa.Column("history_end_id", sa.Text()),
        sa.Column("next_page_token", sa.Text()),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "gmail_account_id", "idempotency_key", name="uq_gmail_sync_runs_account_idempotency_key"
        ),
        sa.CheckConstraint(
            "reason IN ('manual', 'history_expired', 'scheduled', 'recovery')", name="reason_values"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')",
            name="status_values",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint("message_count >= 0", name="message_count_nonnegative"),
        schema="careerops",
    )
    op.create_table(
        "gmail_message_signals",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "gmail_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gmail_sync_run_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_sync_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("provider_thread_id", sa.Text(), nullable=False),
        sa.Column("provider_history_id", sa.Text(), nullable=False),
        sa.Column("message_sha256", sa.String(64), nullable=False),
        sa.Column("thread_sha256", sa.String(64), nullable=False),
        sa.Column("sender_sha256", sa.String(64)),
        sa.Column("subject_sha256", sa.String(64), nullable=False),
        sa.Column("snippet_sha256", sa.String(64), nullable=False),
        sa.Column("redacted_excerpt", sa.Text(), nullable=False),
        sa.Column("label_ids_hash", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("relevance", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("signal_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "gmail_account_id",
            "provider_message_id",
            name="uq_gmail_message_signals_account_message",
        ),
        sa.UniqueConstraint(
            "gmail_account_id",
            "signal_sha256",
            name="uq_gmail_message_signals_account_signal_sha256",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        sa.CheckConstraint(
            "classification IN ('application_acknowledgement', 'assessment', 'document_request', 'interview_invitation', 'offer', 'rejection', 'recruiter_question', 'suspicious', 'unknown', 'unrelated')",
            name="classification_values",
        ),
        sa.CheckConstraint(
            "relevance IN ('relevant', 'uncertain', 'non_relevant')", name="relevance_values"
        ),
        sa.CheckConstraint("message_sha256 ~ '^[a-f0-9]{64}$'", name="message_sha256_format"),
        sa.CheckConstraint("thread_sha256 ~ '^[a-f0-9]{64}$'", name="thread_sha256_format"),
        sa.CheckConstraint(
            "sender_sha256 IS NULL OR sender_sha256 ~ '^[a-f0-9]{64}$'", name="sender_sha256_format"
        ),
        sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
        sa.CheckConstraint("snippet_sha256 ~ '^[a-f0-9]{64}$'", name="snippet_sha256_format"),
        sa.CheckConstraint("label_ids_hash ~ '^[a-f0-9]{64}$'", name="label_ids_hash_format"),
        sa.CheckConstraint("signal_sha256 ~ '^[a-f0-9]{64}$'", name="signal_sha256_format"),
        sa.CheckConstraint(
            "jsonb_typeof(provenance_json) = 'object'", name="provenance_json_object"
        ),
        schema="careerops",
    )
    op.create_table(
        "gmail_signal_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "gmail_message_signal_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_message_signals.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "gmail_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("proposal_kind", sa.Text(), nullable=False),
        sa.Column("review_priority", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_gmail_signal_proposals_owner_idempotency"
        ),
        sa.CheckConstraint(
            "proposal_kind IN ('application_status_update', 'follow_up_draft', 'review_only', 'reconciliation_update')",
            name="proposal_kind_values",
        ),
        sa.CheckConstraint("review_priority IN ('normal', 'high')", name="review_priority_values"),
        sa.CheckConstraint("status = 'pending_review'", name="proposal_rows_are_immutable_pending"),
        sa.CheckConstraint("jsonb_typeof(payload_json) = 'object'", name="payload_json_object"),
        sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
        schema="careerops",
    )
    op.create_table(
        "gmail_signal_review_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_signal_proposals.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "job_posting_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.job_postings.id", ondelete="RESTRICT"),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_gmail_signal_review_decisions_owner_idempotency",
        ),
        sa.CheckConstraint("decision IN ('approve', 'reject')", name="decision_values"),
        sa.CheckConstraint(
            "btrim(reason) <> '' AND char_length(reason) <= 4000", name="reason_bounded"
        ),
        sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
        schema="careerops",
    )
    op.create_table(
        "gmail_command_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gmail_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_accounts.id", ondelete="RESTRICT"),
        ),
        sa.Column("command_kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "gmail_account_id",
            "command_kind",
            "idempotency_key",
            name="uq_gmail_command_receipts_identity",
        ),
        sa.CheckConstraint(
            "command_kind IN ('register_account', 'request_sync', 'review_proposal', 'reset_history', 'revoke_account')",
            name="command_kind_values",
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_accounts_owner_updated_at",
        "gmail_accounts",
        ["owner_user_id", "updated_at"],
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_sync_runs_status_available",
        "gmail_sync_runs",
        ["status", "created_at"],
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_message_signals_account_history",
        "gmail_message_signals",
        ["gmail_account_id", "provider_history_id"],
        schema="careerops",
    )
    _install_guards_and_functions()
    for statement in _REVOKES:
        op.execute(sa.text(statement))
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_mailbox", _MAILBOX_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    for statement in _REVOKES:
        op.execute(sa.text(statement))
    _drop_functions()
    for table_name in reversed(APPEND_ONLY_TABLES):
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON careerops.{table_name}"
            )
        )
    op.drop_index(
        "ix_gmail_message_signals_account_history",
        table_name="gmail_message_signals",
        schema="careerops",
    )
    op.drop_index(
        "ix_gmail_sync_runs_status_available", table_name="gmail_sync_runs", schema="careerops"
    )
    op.drop_index(
        "ix_gmail_accounts_owner_updated_at", table_name="gmail_accounts", schema="careerops"
    )
    for table_name in reversed(_GMAIL_TABLES):
        op.drop_table(table_name, schema="careerops")


def _install_guards_and_functions() -> None:
    for table_name in APPEND_ONLY_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE "
                f"ON careerops.{table_name} FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
            )
        )
    op.execute(sa.text(_CONTROL_FUNCTIONS_SQL))
    for function_name in (
        "gmail_readonly_register_account(uuid, uuid, text, text, text, text, text, text)",
        "gmail_readonly_list_accounts(uuid, integer)",
        "gmail_readonly_account_status(uuid, uuid)",
        "gmail_readonly_list_sync_runs(uuid, uuid, integer)",
        "gmail_readonly_list_proposals(uuid, uuid, integer)",
        "gmail_readonly_request_sync(uuid, uuid, text, text, text)",
        "gmail_readonly_claim_sync_runs(text, integer, integer)",
        "gmail_readonly_record_message_signal(uuid, uuid, text, text, text, text, text, text, text, text, timestamptz, text, text, numeric, jsonb, text, text, text, jsonb, text, text)",
        "gmail_readonly_defer_sync_run(uuid, uuid, text, text, integer, text)",
        "gmail_readonly_complete_sync_run(uuid, uuid, text, text, integer)",
        "gmail_readonly_fail_sync_run(uuid, uuid, text)",
        "gmail_readonly_review_proposal(uuid, uuid, text, uuid, uuid, text, text, text, text)",
        "gmail_readonly_reset_history(uuid, uuid, text, text, text, text)",
        "gmail_readonly_revoke_account(uuid, uuid, text, text, text)",
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION careerops.{function_name} FROM PUBLIC"))


def _drop_functions() -> None:
    for signature in (
        "careerops.gmail_readonly_revoke_account(uuid, uuid, text, text, text)",
        "careerops.gmail_readonly_review_proposal(uuid, uuid, text, uuid, uuid, text, text, text, text)",
        "careerops.gmail_readonly_reset_history(uuid, uuid, text, text, text, text)",
        "careerops.gmail_readonly_fail_sync_run(uuid, uuid, text)",
        "careerops.gmail_readonly_complete_sync_run(uuid, uuid, text, text, integer)",
        "careerops.gmail_readonly_defer_sync_run(uuid, uuid, text, text, integer, text)",
        "careerops.gmail_readonly_record_message_signal(uuid, uuid, text, text, text, text, text, text, text, text, timestamptz, text, text, numeric, jsonb, text, text, text, jsonb, text, text)",
        "careerops.gmail_readonly_claim_sync_runs(text, integer, integer)",
        "careerops.gmail_readonly_request_sync(uuid, uuid, text, text, text)",
        "careerops.gmail_readonly_register_account(uuid, uuid, text, text, text, text, text, text)",
        "careerops.gmail_readonly_list_accounts(uuid, integer)",
        "careerops.gmail_readonly_account_status(uuid, uuid)",
        "careerops.gmail_readonly_list_sync_runs(uuid, uuid, integer)",
        "careerops.gmail_readonly_list_proposals(uuid, uuid, integer)",
    ):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {signature}"))


_CONTROL_FUNCTIONS_SQL = r"""
CREATE FUNCTION careerops.gmail_readonly_register_account(
    p_owner_user_id uuid,
    p_candidate_id uuid,
    p_credential_handle text,
    p_account_subject text,
    p_publishing_status text,
    p_credential_store_evidence_sha256 text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_account_id uuid := gen_random_uuid();
    v_credential_reference_id uuid := gen_random_uuid();
    v_fencing_token uuid := gen_random_uuid();
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'owner_user_id', p_owner_user_id, 'candidate_id', p_candidate_id,
        'credential_handle_sha256', encode(public.digest(p_credential_handle, 'sha256'), 'hex'),
        'account_subject', p_account_subject, 'publishing_status', p_publishing_status,
        'credential_store_evidence_sha256', p_credential_store_evidence_sha256
    )::text, 'sha256'), 'hex');
    v_existing careerops.gmail_accounts%ROWTYPE;
    v_credential careerops.oauth_credential_references%ROWTYPE;
BEGIN
    IF p_owner_user_id IS NULL OR p_candidate_id IS NULL
       OR p_credential_handle IS NULL
       OR char_length(p_credential_handle) NOT BETWEEN 8 AND 256
       OR p_credential_handle !~ '^[A-Za-z0-9._:@/-]+$'
       OR p_account_subject IS NULL OR btrim(p_account_subject) = '' OR char_length(p_account_subject) > 320
       OR p_publishing_status NOT IN ('testing', 'in_production')
       OR p_credential_store_evidence_sha256 IS NULL
       OR p_credential_store_evidence_sha256 !~ '^[a-f0-9]{64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail readonly registration command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM console_users WHERE id = p_owner_user_id
    ) OR NOT EXISTS (
        SELECT 1 FROM candidates WHERE id = p_candidate_id
    ) THEN
        RAISE EXCEPTION 'gmail readonly owner or candidate is missing' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_credential
    FROM oauth_credential_references
    WHERE provider = 'gmail' AND account_subject = p_account_subject
    FOR UPDATE;
    IF FOUND THEN
        IF v_credential.secret_handle <> p_credential_handle
           OR v_credential.status <> 'active'
           OR v_credential.granted_scopes <> '["https://www.googleapis.com/auth/gmail.readonly"]'::jsonb THEN
            RAISE EXCEPTION 'gmail credential must be active and limited to exact gmail.readonly scope' USING ERRCODE = '22023';
        END IF;
        v_credential_reference_id := v_credential.id;
    ELSE
        INSERT INTO oauth_credential_references (
            id, provider, account_subject, secret_handle, granted_scopes, status, issued_at
        )
        VALUES (
            v_credential_reference_id, 'gmail', p_account_subject, p_credential_handle,
            '["https://www.googleapis.com/auth/gmail.readonly"]'::jsonb, 'active', v_now
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM gmail_accounts
        WHERE oauth_credential_reference_id = v_credential_reference_id
          AND owner_user_id <> p_owner_user_id
    ) THEN
        RAISE EXCEPTION 'gmail credential is already bound to another owner'
            USING ERRCODE = '23505';
    END IF;

    SELECT * INTO v_existing
    FROM gmail_accounts
    WHERE owner_user_id = p_owner_user_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.candidate_id <> p_candidate_id
           OR v_existing.oauth_credential_reference_id <> v_credential_reference_id
           OR v_existing.account_subject <> p_account_subject
           OR v_existing.publishing_status <> p_publishing_status
           OR v_existing.credential_store_evidence_sha256 <> p_credential_store_evidence_sha256 THEN
            RAISE EXCEPTION 'gmail readonly idempotency key is bound to different registration' USING ERRCODE = '23505';
        END IF;
        RETURN v_existing.id;
    END IF;

    INSERT INTO gmail_accounts (
        id, owner_user_id, candidate_id, oauth_credential_reference_id, provider,
        account_subject, sync_mode, dedicated_account_attested, byo_account_attested,
        publishing_status, publishing_evidence_sha256, credential_store_evidence_sha256,
        in_production_attested, status, version, fencing_token, idempotency_key,
        trace_id, created_at, updated_at
    )
    VALUES (
        v_account_id, p_owner_user_id, p_candidate_id, v_credential_reference_id,
        'gmail', p_account_subject, 'polling', true, true, p_publishing_status,
        NULL, p_credential_store_evidence_sha256,
        p_publishing_status = 'in_production',
        'active', 1, v_fencing_token, p_idempotency_key, p_trace_id, v_now, v_now
    );
    INSERT INTO gmail_command_receipts (
        id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_owner_user_id, v_account_id, 'register_account',
        p_idempotency_key, v_request_hash, jsonb_build_object('account_id', v_account_id),
        p_trace_id, v_now
    );
    RETURN v_account_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_list_accounts(
    p_owner_user_id uuid,
    p_limit integer
)
RETURNS TABLE (
    account_id uuid,
    owner_user_id uuid,
    candidate_id uuid,
    provider character varying,
    account_subject text,
    sync_mode character varying,
    publishing_status character varying,
    status character varying,
    version bigint,
    last_history_id text,
    next_page_token text,
    last_synced_at timestamptz,
    last_full_sync_at timestamptz,
    last_error_code text,
    created_at timestamptz,
    updated_at timestamptz,
    snapshot_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF p_owner_user_id IS NULL OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'gmail readonly list command is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT
        account.id,
        account.owner_user_id,
        account.candidate_id,
        account.provider,
        account.account_subject,
        account.sync_mode,
        account.publishing_status,
        account.status,
        account.version,
        account.last_history_id,
        account.next_page_token,
        account.last_synced_at,
        account.last_full_sync_at,
        account.last_error_code,
        account.created_at,
        account.updated_at,
        encode(public.digest(jsonb_build_object(
            'id', account.id,
            'version', account.version,
            'status', account.status,
            'last_history_id', account.last_history_id,
            'next_page_token', account.next_page_token
        )::text, 'sha256'), 'hex')
    FROM gmail_accounts AS account
    WHERE account.owner_user_id = p_owner_user_id
    ORDER BY account.updated_at DESC, account.id
    LIMIT p_limit;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_account_status(
    p_owner_user_id uuid,
    p_gmail_account_id uuid
)
RETURNS TABLE (
    account_id uuid,
    owner_user_id uuid,
    candidate_id uuid,
    provider character varying,
    account_subject text,
    sync_mode character varying,
    publishing_status character varying,
    status character varying,
    version bigint,
    last_history_id text,
    next_page_token text,
    last_synced_at timestamptz,
    last_full_sync_at timestamptz,
    last_error_code text,
    created_at timestamptz,
    updated_at timestamptz,
    snapshot_sha256 text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_account careerops.gmail_accounts%ROWTYPE;
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL THEN
        RAISE EXCEPTION 'gmail readonly status command is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_account
    FROM gmail_accounts
    WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    RETURN QUERY
    SELECT
        v_account.id,
        v_account.owner_user_id,
        v_account.candidate_id,
        v_account.provider,
        v_account.account_subject,
        v_account.sync_mode,
        v_account.publishing_status,
        v_account.status,
        v_account.version,
        v_account.last_history_id,
        v_account.next_page_token,
        v_account.last_synced_at,
        v_account.last_full_sync_at,
        v_account.last_error_code,
        v_account.created_at,
        v_account.updated_at,
        encode(public.digest(jsonb_build_object(
            'id', v_account.id,
            'version', v_account.version,
            'status', v_account.status,
            'last_history_id', v_account.last_history_id,
            'next_page_token', v_account.next_page_token
        )::text, 'sha256'), 'hex');
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_list_sync_runs(
    p_owner_user_id uuid,
    p_gmail_account_id uuid,
    p_limit integer
)
RETURNS SETOF careerops.gmail_sync_runs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'gmail readonly list sync runs command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM gmail_accounts
        WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id
    ) THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    RETURN QUERY
    SELECT *
    FROM gmail_sync_runs
    WHERE gmail_account_id = p_gmail_account_id
      AND owner_user_id = p_owner_user_id
    ORDER BY created_at DESC, id
    LIMIT p_limit;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_list_proposals(
    p_owner_user_id uuid,
    p_gmail_account_id uuid,
    p_limit integer
)
RETURNS TABLE (
    proposal_id uuid,
    gmail_account_id uuid,
    owner_user_id uuid,
    gmail_message_signal_id uuid,
    signal_sha256 character varying,
    classification character varying,
    relevance character varying,
    confidence numeric,
    redacted_excerpt text,
    proposal_kind text,
    review_priority text,
    payload_json jsonb,
    payload_sha256 character varying,
    decision text,
    canonical_job_id uuid,
    job_posting_id uuid,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'gmail readonly list proposals command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM gmail_accounts
        WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id
    ) THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    RETURN QUERY
    SELECT
        proposal.id,
        proposal.gmail_account_id,
        proposal.owner_user_id,
        proposal.gmail_message_signal_id,
        signal.signal_sha256,
        signal.classification,
        signal.relevance,
        signal.confidence,
        signal.redacted_excerpt,
        proposal.proposal_kind,
        proposal.review_priority,
        proposal.payload_json,
        proposal.payload_sha256,
        decision.decision,
        decision.canonical_job_id,
        decision.job_posting_id,
        proposal.created_at
    FROM gmail_signal_proposals AS proposal
    JOIN gmail_message_signals AS signal
      ON signal.id = proposal.gmail_message_signal_id
    LEFT JOIN gmail_signal_review_decisions AS decision
      ON decision.proposal_id = proposal.id
    WHERE proposal.gmail_account_id = p_gmail_account_id
      AND proposal.owner_user_id = p_owner_user_id
    ORDER BY proposal.created_at DESC, proposal.id
    LIMIT p_limit;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_request_sync(
    p_owner_user_id uuid,
    p_gmail_account_id uuid,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run_id uuid := gen_random_uuid();
    v_fencing_token uuid := gen_random_uuid();
    v_account careerops.gmail_accounts%ROWTYPE;
    v_existing careerops.gmail_sync_runs%ROWTYPE;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'owner_user_id', p_owner_user_id, 'gmail_account_id', p_gmail_account_id, 'reason', p_reason
    )::text, 'sha256'), 'hex');
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL
       OR p_reason NOT IN ('manual', 'history_expired', 'scheduled', 'recovery')
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail readonly sync command is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_account FROM gmail_accounts
    WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    IF v_account.status IN ('revoked', 'blocked') THEN
        RAISE EXCEPTION 'gmail account cannot sync in current status' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO v_existing FROM gmail_sync_runs
    WHERE gmail_account_id = p_gmail_account_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.owner_user_id <> p_owner_user_id OR v_existing.reason <> p_reason THEN
            RAISE EXCEPTION 'gmail sync idempotency key is bound to different request' USING ERRCODE = '23505';
        END IF;
        RETURN v_existing.id;
    END IF;
    INSERT INTO gmail_sync_runs (
        id, gmail_account_id, owner_user_id, reason, status, idempotency_key,
        fencing_token, history_start_id, next_page_token, trace_id, created_at, updated_at
    )
    VALUES (
        v_run_id, p_gmail_account_id, p_owner_user_id, p_reason, 'queued',
        p_idempotency_key, v_fencing_token, v_account.last_history_id,
        v_account.next_page_token, p_trace_id, v_now, v_now
    );
    INSERT INTO gmail_command_receipts (
        id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_owner_user_id, p_gmail_account_id, 'request_sync',
        p_idempotency_key, v_request_hash, jsonb_build_object('run_id', v_run_id),
        p_trace_id, v_now
    );
    RETURN v_run_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_claim_sync_runs(
    p_lease_owner text,
    p_lease_seconds integer,
    p_limit integer
)
RETURNS TABLE (
    sync_run_id uuid,
    owner_user_id uuid,
    lease_token uuid,
    attempt_count integer,
    run_next_page_token text,
    anchor_history_id text,
    mailbox_id uuid,
    candidate_id uuid,
    credential_handle text,
    account_subject text,
    history_start_id text,
    account_status text,
    account_next_page_token text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$'
       OR p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 3600
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'gmail sync claim command is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH exhausted AS (
        UPDATE gmail_sync_runs AS run
        SET status = 'failed',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            failed_at = clock_timestamp(),
            last_error_code = 'GMAIL_WORKER_LEASE_EXHAUSTED',
            updated_at = clock_timestamp()
        WHERE run.status = 'leased'
          AND run.lease_until IS NOT NULL
          AND run.lease_until <= clock_timestamp()
          AND run.attempt_count >= 5
        RETURNING run.gmail_account_id
    ),
    paused AS (
        UPDATE gmail_accounts AS account
        SET status = 'paused',
            last_error_code = 'GMAIL_WORKER_LEASE_EXHAUSTED',
            version = account.version + 1,
            fencing_token = gen_random_uuid(),
            updated_at = clock_timestamp()
        WHERE account.id IN (SELECT exhausted.gmail_account_id FROM exhausted)
        RETURNING account.id
    ),
    due AS (
        SELECT run.id
        FROM gmail_sync_runs AS run
        JOIN gmail_accounts AS account ON account.id = run.gmail_account_id
        JOIN oauth_credential_references AS credential
          ON credential.id = account.oauth_credential_reference_id
        WHERE (
              run.status = 'queued'
              OR (run.status = 'leased' AND run.lease_until IS NOT NULL
                  AND run.lease_until <= clock_timestamp())
          )
          AND run.attempt_count < 5
          AND NOT EXISTS (SELECT 1 FROM paused WHERE paused.id = account.id)
          AND account.provider = 'gmail'
          AND account.status IN ('active', 'sync_required')
          AND credential.provider = 'gmail'
          AND credential.status = 'active'
          AND credential.account_subject = account.account_subject
          AND credential.granted_scopes = '["https://www.googleapis.com/auth/gmail.readonly"]'::jsonb
        ORDER BY run.created_at, run.id
        LIMIT p_limit
        FOR UPDATE OF run SKIP LOCKED
    ),
    claimed AS (
        UPDATE gmail_sync_runs AS run
        SET status = 'leased',
            lease_owner = p_lease_owner,
            lease_token = gen_random_uuid(),
            lease_until = clock_timestamp() + make_interval(secs => p_lease_seconds),
            attempt_count = run.attempt_count + 1,
            started_at = COALESCE(run.started_at, clock_timestamp()),
            updated_at = clock_timestamp()
        FROM due
        WHERE run.id = due.id
        RETURNING run.*
    )
    SELECT
        claimed.id AS sync_run_id,
        claimed.owner_user_id,
        claimed.lease_token,
        claimed.attempt_count,
        claimed.next_page_token AS run_next_page_token,
        claimed.history_end_id AS anchor_history_id,
        account.id AS mailbox_id,
        account.candidate_id,
        credential.secret_handle AS credential_handle,
        account.account_subject,
        claimed.history_start_id,
        account.status::text AS account_status,
        account.next_page_token AS account_next_page_token
    FROM claimed
    JOIN gmail_accounts AS account ON account.id = claimed.gmail_account_id
    JOIN oauth_credential_references AS credential
      ON credential.id = account.oauth_credential_reference_id
    WHERE account.provider = 'gmail'
      AND account.status IN ('active', 'sync_required')
      AND credential.provider = 'gmail'
      AND credential.status = 'active'
      AND credential.account_subject = account.account_subject
      AND credential.granted_scopes = '["https://www.googleapis.com/auth/gmail.readonly"]'::jsonb;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_record_message_signal(
    p_gmail_sync_run_id uuid,
    p_lease_token uuid,
    p_provider_message_id text,
    p_provider_thread_id text,
    p_provider_history_id text,
    p_message_sha256 text,
    p_thread_sha256 text,
    p_sender_sha256 text,
    p_subject_sha256 text,
    p_snippet_sha256 text,
    p_received_at timestamptz,
    p_classification text,
    p_relevance text,
    p_confidence numeric,
    p_provenance_json jsonb,
    p_signal_sha256 text,
    p_redacted_excerpt text,
    p_label_ids_hash text,
    p_proposal_payload_json jsonb,
    p_proposal_payload_sha256 text,
    p_trace_id text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.gmail_sync_runs%ROWTYPE;
    v_signal_id uuid := gen_random_uuid();
    v_proposal_id uuid;
BEGIN
    SELECT * INTO v_run FROM gmail_sync_runs
    WHERE id = p_gmail_sync_run_id AND lease_token = p_lease_token AND status = 'leased'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail sync run lease is missing' USING ERRCODE = '23503';
    END IF;
    IF v_run.lease_until IS NULL OR v_run.lease_until < v_now THEN
        RAISE EXCEPTION 'gmail sync run lease expired' USING ERRCODE = '23514';
    END IF;
    IF p_provider_message_id IS NULL OR p_provider_thread_id IS NULL OR p_provider_history_id IS NULL
       OR p_provider_history_id !~ '^[0-9]{1,40}$'
       OR p_message_sha256 !~ '^[a-f0-9]{64}$'
       OR p_thread_sha256 !~ '^[a-f0-9]{64}$'
       OR (p_sender_sha256 IS NOT NULL AND p_sender_sha256 !~ '^[a-f0-9]{64}$')
       OR p_subject_sha256 !~ '^[a-f0-9]{64}$'
       OR p_snippet_sha256 !~ '^[a-f0-9]{64}$'
       OR p_signal_sha256 !~ '^[a-f0-9]{64}$'
       OR p_label_ids_hash !~ '^[a-f0-9]{64}$'
       OR p_redacted_excerpt IS NULL OR char_length(p_redacted_excerpt) > 500
       OR p_provenance_json IS NULL OR jsonb_typeof(p_provenance_json) <> 'object'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail message signal command is invalid' USING ERRCODE = '22023';
    END IF;
    INSERT INTO gmail_message_signals (
        id, gmail_account_id, gmail_sync_run_id, owner_user_id, provider_message_id,
        provider_thread_id, provider_history_id, message_sha256, thread_sha256,
        sender_sha256, subject_sha256, snippet_sha256, redacted_excerpt, label_ids_hash,
        received_at, classification, relevance, confidence, provenance_json,
        signal_sha256, created_at
    )
    VALUES (
        v_signal_id, v_run.gmail_account_id, v_run.id, v_run.owner_user_id,
        p_provider_message_id, p_provider_thread_id, p_provider_history_id,
        p_message_sha256, p_thread_sha256, p_sender_sha256, p_subject_sha256,
        p_snippet_sha256, p_redacted_excerpt, p_label_ids_hash, p_received_at,
        p_classification, p_relevance, p_confidence, p_provenance_json,
        p_signal_sha256, v_now
    )
    ON CONFLICT (gmail_account_id, provider_message_id) DO NOTHING
    RETURNING id INTO v_signal_id;
    IF v_signal_id IS NULL THEN
        SELECT id INTO v_signal_id FROM gmail_message_signals
        WHERE gmail_account_id = v_run.gmail_account_id
          AND provider_message_id = p_provider_message_id;
    END IF;
    IF p_proposal_payload_json IS NOT NULL THEN
        IF jsonb_typeof(p_proposal_payload_json) <> 'object'
           OR p_proposal_payload_sha256 !~ '^[a-f0-9]{64}$' THEN
            RAISE EXCEPTION 'gmail proposal payload is invalid' USING ERRCODE = '22023';
        END IF;
        INSERT INTO gmail_signal_proposals (
            id, gmail_message_signal_id, gmail_account_id, owner_user_id,
            proposal_kind, review_priority, payload_json, payload_sha256,
            status, idempotency_key, trace_id, created_at
        )
        VALUES (
            gen_random_uuid(), v_signal_id, v_run.gmail_account_id, v_run.owner_user_id,
            COALESCE(p_proposal_payload_json->>'proposal_kind', 'review_only'),
            COALESCE(p_proposal_payload_json->>'review_priority', 'normal'),
            p_proposal_payload_json, p_proposal_payload_sha256, 'pending_review',
            'gmail-proposal:' || v_signal_id::text || ':' || p_proposal_payload_sha256,
            p_trace_id, v_now
        )
        ON CONFLICT (gmail_message_signal_id) DO NOTHING
        RETURNING id INTO v_proposal_id;
    END IF;
    RETURN v_signal_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_defer_sync_run(
    p_gmail_sync_run_id uuid,
    p_lease_token uuid,
    p_history_end_id text,
    p_next_page_token text,
    p_message_count integer,
    p_error_code text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.gmail_sync_runs%ROWTYPE;
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$'
       OR (p_history_end_id IS NOT NULL AND p_history_end_id !~ '^[0-9]{1,40}$')
       OR (p_next_page_token IS NOT NULL AND btrim(p_next_page_token) = '')
       OR (p_error_code = 'GMAIL_PAGE_INCOMPLETE'
           AND (p_next_page_token IS NULL OR p_history_end_id IS NULL))
       OR p_message_count IS NULL OR p_message_count < 0 THEN
        RAISE EXCEPTION 'gmail sync defer error code is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run FROM gmail_sync_runs
    WHERE id = p_gmail_sync_run_id AND lease_token = p_lease_token AND status = 'leased'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail sync run lease is missing' USING ERRCODE = '23503';
    END IF;
    IF v_run.lease_until IS NULL OR v_run.lease_until < v_now THEN
        RAISE EXCEPTION 'gmail sync run lease expired' USING ERRCODE = '23514';
    END IF;
    IF v_run.attempt_count >= 5 AND p_error_code <> 'GMAIL_PAGE_INCOMPLETE' THEN
        UPDATE gmail_sync_runs
        SET status = 'failed',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            failed_at = v_now,
            last_error_code = p_error_code,
            updated_at = v_now
        WHERE id = p_gmail_sync_run_id;
        UPDATE gmail_accounts
        SET status = 'paused',
            last_error_code = p_error_code,
            version = version + 1,
            fencing_token = gen_random_uuid(),
            updated_at = v_now
        WHERE id = v_run.gmail_account_id;
        RETURN;
    END IF;
    UPDATE gmail_sync_runs
    SET status = 'queued', lease_owner = NULL, lease_token = NULL, lease_until = NULL,
        history_end_id = COALESCE(history_end_id, p_history_end_id),
        next_page_token = COALESCE(p_next_page_token, next_page_token),
        last_error_code = p_error_code,
        message_count = message_count + p_message_count,
        updated_at = v_now
    WHERE id = p_gmail_sync_run_id;
    UPDATE gmail_accounts
    SET next_page_token = COALESCE(p_next_page_token, next_page_token),
        last_error_code = p_error_code,
        version = version + 1, fencing_token = gen_random_uuid(), updated_at = v_now
    WHERE id = v_run.gmail_account_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_complete_sync_run(
    p_gmail_sync_run_id uuid,
    p_lease_token uuid,
    p_history_end_id text,
    p_next_page_token text,
    p_message_count integer
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.gmail_sync_runs%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM gmail_sync_runs
    WHERE id = p_gmail_sync_run_id AND lease_token = p_lease_token AND status = 'leased'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail sync run lease is missing' USING ERRCODE = '23503';
    END IF;
    IF v_run.lease_until IS NULL OR v_run.lease_until < v_now THEN
        RAISE EXCEPTION 'gmail sync run lease expired' USING ERRCODE = '23514';
    END IF;
    IF p_history_end_id IS NULL OR p_history_end_id !~ '^[0-9]{1,40}$'
       OR p_next_page_token IS NOT NULL
       OR p_message_count IS NULL OR p_message_count < 0 THEN
        RAISE EXCEPTION 'gmail sync completion is invalid' USING ERRCODE = '22023';
    END IF;
    UPDATE gmail_sync_runs
    SET status = 'succeeded',
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        completed_at = v_now,
        history_end_id = p_history_end_id,
        next_page_token = p_next_page_token, message_count = message_count + p_message_count,
        updated_at = v_now
    WHERE id = p_gmail_sync_run_id;
    UPDATE gmail_accounts
    SET last_history_id = p_history_end_id, next_page_token = p_next_page_token,
        last_synced_at = v_now,
        last_full_sync_at = CASE WHEN status = 'sync_required' THEN v_now ELSE last_full_sync_at END,
        status = 'active', version = version + 1, fencing_token = gen_random_uuid(),
        last_error_code = NULL, updated_at = v_now
    WHERE id = v_run.gmail_account_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_fail_sync_run(
    p_gmail_sync_run_id uuid,
    p_lease_token uuid,
    p_error_code text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run careerops.gmail_sync_runs%ROWTYPE;
    v_account_status text := 'active';
BEGIN
    IF p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
        RAISE EXCEPTION 'gmail sync error code is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run FROM gmail_sync_runs
    WHERE id = p_gmail_sync_run_id AND lease_token = p_lease_token AND status = 'leased'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail sync run lease is missing' USING ERRCODE = '23503';
    END IF;
    IF v_run.lease_until IS NULL OR v_run.lease_until < v_now THEN
        RAISE EXCEPTION 'gmail sync run lease expired' USING ERRCODE = '23514';
    END IF;
    IF p_error_code IN ('GMAIL_HISTORY_EXPIRED', 'GMAIL_404_HISTORY_EXPIRED') THEN
        v_account_status := 'sync_required';
    ELSIF p_error_code IN ('GMAIL_REAUTH_REQUIRED', 'GMAIL_TOKEN_REVOKED') THEN
        v_account_status := 'blocked';
    END IF;
    UPDATE gmail_sync_runs
    SET status = 'failed',
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        failed_at = v_now,
        last_error_code = p_error_code,
        updated_at = v_now
    WHERE id = p_gmail_sync_run_id;
    UPDATE gmail_accounts
    SET status = v_account_status, last_error_code = p_error_code,
        version = version + 1, fencing_token = gen_random_uuid(), updated_at = v_now
    WHERE id = v_run.gmail_account_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_review_proposal(
    p_owner_user_id uuid,
    p_proposal_id uuid,
    p_decision text,
    p_canonical_job_id uuid,
    p_job_posting_id uuid,
    p_reason text,
    p_payload_sha256 text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_decision_id uuid := gen_random_uuid();
    v_existing careerops.gmail_signal_review_decisions%ROWTYPE;
BEGIN
    IF p_owner_user_id IS NULL OR p_proposal_id IS NULL
       OR p_decision NOT IN ('approve', 'reject')
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_payload_sha256 !~ '^[a-f0-9]{64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail review decision command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM gmail_signal_proposals
        WHERE id = p_proposal_id AND owner_user_id = p_owner_user_id
          AND payload_sha256 = p_payload_sha256
    ) THEN
        RAISE EXCEPTION 'gmail proposal is missing for owner or snapshot' USING ERRCODE = '23503';
    END IF;
    IF p_canonical_job_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM canonical_jobs WHERE id = p_canonical_job_id) THEN
        RAISE EXCEPTION 'selected canonical job is missing' USING ERRCODE = '23503';
    END IF;
    IF p_job_posting_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM job_postings WHERE id = p_job_posting_id) THEN
        RAISE EXCEPTION 'selected job posting is missing' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_existing
    FROM gmail_signal_review_decisions
    WHERE owner_user_id = p_owner_user_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.proposal_id <> p_proposal_id OR v_existing.decision <> p_decision
           OR v_existing.canonical_job_id IS DISTINCT FROM p_canonical_job_id
           OR v_existing.job_posting_id IS DISTINCT FROM p_job_posting_id
           OR v_existing.payload_sha256 <> p_payload_sha256 THEN
            RAISE EXCEPTION 'gmail review idempotency key is bound to different decision' USING ERRCODE = '23505';
        END IF;
        RETURN v_existing.id;
    END IF;
    INSERT INTO gmail_signal_review_decisions (
        id, proposal_id, owner_user_id, decision, canonical_job_id, job_posting_id,
        reason, payload_sha256,
        idempotency_key, trace_id, created_at
    )
    VALUES (
        v_decision_id, p_proposal_id, p_owner_user_id, p_decision,
        p_canonical_job_id, p_job_posting_id, p_reason,
        p_payload_sha256, p_idempotency_key, p_trace_id, clock_timestamp()
    );
    INSERT INTO gmail_command_receipts (
        id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    SELECT gen_random_uuid(), p_owner_user_id, proposal.gmail_account_id, 'review_proposal',
           p_idempotency_key,
           encode(public.digest(jsonb_build_object(
               'owner_user_id', p_owner_user_id, 'proposal_id', p_proposal_id,
               'decision', p_decision, 'canonical_job_id', p_canonical_job_id,
               'job_posting_id', p_job_posting_id, 'payload_sha256', p_payload_sha256
           )::text, 'sha256'), 'hex'),
           jsonb_build_object(
               'decision_id', v_decision_id,
               'canonical_job_id', p_canonical_job_id,
               'job_posting_id', p_job_posting_id
           ),
           p_trace_id, clock_timestamp()
    FROM gmail_signal_proposals AS proposal
    WHERE proposal.id = p_proposal_id;
    RETURN v_decision_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_reset_history(
    p_owner_user_id uuid,
    p_gmail_account_id uuid,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text,
    p_snapshot_sha256 text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_run_id uuid := gen_random_uuid();
    v_fencing_token uuid := gen_random_uuid();
    v_account careerops.gmail_accounts%ROWTYPE;
    v_receipt careerops.gmail_command_receipts%ROWTYPE;
    v_snapshot_sha256 text;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'owner_user_id', p_owner_user_id,
        'gmail_account_id', p_gmail_account_id,
        'reason', p_reason,
        'snapshot_sha256', p_snapshot_sha256
    )::text, 'sha256'), 'hex');
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_snapshot_sha256 IS NULL OR p_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail reset history command is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM 1
    FROM gmail_sync_runs
    WHERE gmail_account_id = p_gmail_account_id
      AND owner_user_id = p_owner_user_id
      AND status IN ('queued', 'leased')
    ORDER BY id
    FOR UPDATE;
    SELECT * INTO v_account
    FROM gmail_accounts
    WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_receipt
    FROM gmail_command_receipts
    WHERE owner_user_id = p_owner_user_id
      AND gmail_account_id = p_gmail_account_id
      AND command_kind = 'reset_history'
      AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.request_sha256 <> v_request_hash THEN
            RAISE EXCEPTION 'gmail reset idempotency key is bound to a different request'
                USING ERRCODE = '23505';
        END IF;
        RETURN (v_receipt.response_json->>'run_id')::uuid;
    END IF;
    v_snapshot_sha256 := encode(public.digest(jsonb_build_object(
        'id', v_account.id,
        'version', v_account.version,
        'status', v_account.status,
        'last_history_id', v_account.last_history_id,
        'next_page_token', v_account.next_page_token
    )::text, 'sha256'), 'hex');
    IF v_snapshot_sha256 <> p_snapshot_sha256 THEN
        RAISE EXCEPTION 'gmail account snapshot is stale' USING ERRCODE = '23514';
    END IF;
    UPDATE gmail_sync_runs
    SET status = 'cancelled',
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        updated_at = v_now,
        last_error_code = 'GMAIL_HISTORY_RESET'
    WHERE gmail_account_id = p_gmail_account_id
      AND owner_user_id = p_owner_user_id
      AND status IN ('queued', 'leased');
    UPDATE gmail_accounts
    SET last_history_id = NULL,
        next_page_token = NULL,
        status = 'sync_required',
        last_error_code = NULL,
        version = version + 1,
        fencing_token = gen_random_uuid(),
        updated_at = v_now
    WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id;
    INSERT INTO gmail_sync_runs (
        id, gmail_account_id, owner_user_id, reason, status, idempotency_key,
        fencing_token, history_start_id, next_page_token, trace_id, created_at, updated_at
    )
    VALUES (
        v_run_id, p_gmail_account_id, p_owner_user_id, 'recovery', 'queued',
        p_idempotency_key, v_fencing_token, NULL, NULL, p_trace_id, v_now, v_now
    );
    INSERT INTO gmail_command_receipts (
        id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_owner_user_id, p_gmail_account_id, 'reset_history',
        p_idempotency_key,
        v_request_hash,
        jsonb_build_object('run_id', v_run_id, 'status', 'sync_required'),
        p_trace_id, v_now
    );
    RETURN v_run_id;
END
$function$;

CREATE FUNCTION careerops.gmail_readonly_revoke_account(
    p_owner_user_id uuid,
    p_gmail_account_id uuid,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_account careerops.gmail_accounts%ROWTYPE;
    v_receipt careerops.gmail_command_receipts%ROWTYPE;
    v_request_hash text := encode(public.digest(jsonb_build_object(
        'owner_user_id', p_owner_user_id,
        'gmail_account_id', p_gmail_account_id,
        'reason', p_reason
    )::text, 'sha256'), 'hex');
BEGIN
    IF p_owner_user_id IS NULL OR p_gmail_account_id IS NULL
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'gmail revoke command is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM 1
    FROM gmail_sync_runs
    WHERE gmail_account_id = p_gmail_account_id
      AND owner_user_id = p_owner_user_id
      AND status IN ('queued', 'leased')
    ORDER BY id
    FOR UPDATE;
    SELECT * INTO v_account
    FROM gmail_accounts
    WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'gmail account is missing for owner' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_receipt
    FROM gmail_command_receipts
    WHERE owner_user_id = p_owner_user_id
      AND gmail_account_id = p_gmail_account_id
      AND command_kind = 'revoke_account'
      AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.request_sha256 <> v_request_hash THEN
            RAISE EXCEPTION 'gmail revoke idempotency key is bound to a different request'
                USING ERRCODE = '23505';
        END IF;
        RETURN;
    END IF;
    IF v_account.status <> 'revoked' THEN
        UPDATE gmail_sync_runs
        SET status = 'cancelled',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            updated_at = v_now,
            last_error_code = 'GMAIL_ACCOUNT_REVOKED'
        WHERE gmail_account_id = p_gmail_account_id
          AND owner_user_id = p_owner_user_id
          AND status IN ('queued', 'leased');
        UPDATE gmail_accounts
        SET status = 'revoked', version = version + 1,
            fencing_token = gen_random_uuid(), updated_at = v_now
        WHERE id = p_gmail_account_id AND owner_user_id = p_owner_user_id;
        UPDATE oauth_credential_references
        SET status = 'revoked', revoked_at = v_now, updated_at = v_now
        WHERE id = v_account.oauth_credential_reference_id
          AND provider = 'gmail';
    END IF;
    INSERT INTO gmail_command_receipts (
        id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
        request_sha256, response_json, trace_id, created_at
    )
    VALUES (
        gen_random_uuid(), p_owner_user_id, p_gmail_account_id, 'revoke_account',
        p_idempotency_key,
        v_request_hash,
        jsonb_build_object('status', 'revoked'), p_trace_id, v_now
    );
END
$function$;
"""
