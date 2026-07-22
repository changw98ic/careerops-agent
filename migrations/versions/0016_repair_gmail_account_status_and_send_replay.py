"""repair gmail readonly status and send replay immutability

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-21
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_READONLY_STATUS_SIGNATURE = "(uuid, uuid)"
_SEND_REGISTER_WITH_READONLY_SIGNATURE = (
    "(uuid, uuid, text, text, text, text, text, integer, uuid, text, text)"
)


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _grant_if_role_exists(role_name: str, statement: str) -> None:
    if context.is_offline_mode():
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_sql_literal(role_name)}) THEN
                        {statement};
                    END IF;
                END $$;
                """
            )
        )
        return
    if _role_exists(role_name):
        op.execute(sa.text(statement))


def _install_readonly_status_function(*, qualify_account_lookup: bool) -> None:
    account_table = "gmail_accounts AS account" if qualify_account_lookup else "gmail_accounts"
    owner_predicate = (
        "account.id = p_gmail_account_id AND account.owner_user_id = p_owner_user_id"
        if qualify_account_lookup
        else "id = p_gmail_account_id AND owner_user_id = p_owner_user_id"
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_readonly_account_status(
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
                FROM {account_table}
                WHERE {owner_predicate};
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
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_readonly_account_status{_READONLY_STATUS_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_api",
        f"GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_account_status{_READONLY_STATUS_SIGNATURE} TO careerops_api",
    )


def _install_send_register_wrapper(*, immutable_reconciliation: bool) -> None:
    account_declaration = (
        "v_account careerops.gmail_send_accounts%ROWTYPE;" if immutable_reconciliation else ""
    )
    reconciliation_guard = (
        """
                SELECT *
                INTO v_account
                FROM careerops.gmail_send_accounts AS account
                WHERE account.id = v_account_id
                FOR UPDATE;
                IF v_account.reconciliation_gmail_account_id IS NOT NULL
                   AND v_account.reconciliation_gmail_account_id <> p_reconciliation_gmail_account_id THEN
                    RAISE EXCEPTION 'gmail send registration idempotency key conflicts'
                        USING ERRCODE = '23505';
                END IF;
                IF v_account.reconciliation_gmail_account_id IS NULL THEN
                    UPDATE careerops.gmail_send_accounts
                    SET reconciliation_gmail_account_id = p_reconciliation_gmail_account_id,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = v_account_id;
                END IF;
        """
        if immutable_reconciliation
        else """
                UPDATE careerops.gmail_send_accounts
                SET reconciliation_gmail_account_id = p_reconciliation_gmail_account_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = v_account_id;
        """
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_send_register_account(
                p_owner_user_id uuid,
                p_candidate_id uuid,
                p_credential_handle text,
                p_account_subject text,
                p_publishing_status text,
                p_credential_store_evidence_sha256 text,
                p_release_evidence_sha256 text,
                p_daily_send_limit integer,
                p_reconciliation_gmail_account_id uuid,
                p_idempotency_key text,
                p_trace_id text
            )
            RETURNS uuid
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_account_id uuid;
                v_status text := CASE WHEN p_publishing_status IN ('testing', 'in_production') THEN 'active' ELSE 'disabled' END;
                {account_declaration}
            BEGIN
                IF p_reconciliation_gmail_account_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send registration requires readonly reconciliation account'
                        USING ERRCODE = '22004';
                END IF;
                PERFORM 1
                FROM careerops.gmail_accounts AS readonly_account
                JOIN careerops.oauth_credential_references AS credential
                  ON credential.id = readonly_account.oauth_credential_reference_id
                WHERE readonly_account.id = p_reconciliation_gmail_account_id
                  AND readonly_account.owner_user_id = p_owner_user_id
                  AND readonly_account.candidate_id = p_candidate_id
                  AND readonly_account.account_subject = p_account_subject
                  AND readonly_account.status = 'active'
                  AND credential.provider = 'gmail'
                  AND credential.account_subject = readonly_account.account_subject
                  AND credential.status = 'active'
                  AND credential.granted_scopes = jsonb_build_array('https://www.googleapis.com/auth/gmail.readonly')
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send readonly reconciliation account is unavailable'
                        USING ERRCODE = '23514';
                END IF;
                SELECT account_id
                INTO v_account_id
                FROM careerops.gmail_send_register_account(
                    p_owner_user_id,
                    p_candidate_id,
                    p_account_subject,
                    p_credential_handle,
                    p_credential_store_evidence_sha256,
                    p_release_evidence_sha256,
                    v_status,
                    p_idempotency_key,
                    p_trace_id,
                    p_daily_send_limit
                );
                {reconciliation_guard}
                RETURN v_account_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_send_register_account{_SEND_REGISTER_WITH_READONLY_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_api",
        f"GRANT EXECUTE ON FUNCTION careerops.gmail_send_register_account{_SEND_REGISTER_WITH_READONLY_SIGNATURE} TO careerops_api",
    )


def upgrade() -> None:
    _install_readonly_status_function(qualify_account_lookup=True)
    _install_send_register_wrapper(immutable_reconciliation=True)


def downgrade() -> None:
    _install_send_register_wrapper(immutable_reconciliation=False)
    _install_readonly_status_function(qualify_account_lookup=False)
