"""repair gmail proposal listing and reviewed replay immutability

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-21
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIST_PROPOSALS_SIGNATURE = "(uuid, uuid, integer)"
_RESERVE_REVIEWED_SIGNATURE = (
    "(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)"
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


def _install_list_proposals(*, qualify_account_lookup: bool) -> None:
    account_table = "gmail_accounts AS account" if qualify_account_lookup else "gmail_accounts"
    owner_predicate = (
        "account.id = p_gmail_account_id AND account.owner_user_id = p_owner_user_id"
        if qualify_account_lookup
        else "id = p_gmail_account_id AND owner_user_id = p_owner_user_id"
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_readonly_list_proposals(
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
                    SELECT 1 FROM {account_table}
                    WHERE {owner_predicate}
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
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_readonly_list_proposals{_LIST_PROPOSALS_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_api",
        f"GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_list_proposals{_LIST_PROPOSALS_SIGNATURE} TO careerops_api",
    )


def _install_reserve_reviewed_intent(*, validate_replay: bool) -> None:
    existing_replay_guard = (
        """
                    IF v_existing.gmail_send_account_id IS DISTINCT FROM p_account_id
                       OR v_existing.reviewed_intent_id IS DISTINCT FROM p_reviewed_intent_id
                       OR v_existing.application_id IS DISTINCT FROM p_application_id
                       OR v_existing.recipient_email IS DISTINCT FROM p_recipient_email
                       OR v_existing.recipient_snapshot_sha256 IS DISTINCT FROM p_recipient_snapshot_sha256
                       OR v_existing.subject_sha256 IS DISTINCT FROM p_subject_sha256
                       OR v_existing.body_sha256 IS DISTINCT FROM p_body_sha256
                       OR v_existing.payload_sha256 IS DISTINCT FROM p_payload_sha256
                       OR v_existing.approval_snapshot_sha256 IS DISTINCT FROM p_approval_snapshot_sha256
                       OR v_existing.attachment_manifest_sha256 IS DISTINCT FROM p_attachment_manifest_sha256 THEN
                        RAISE EXCEPTION 'gmail send reviewed reservation idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
        """
        if validate_replay
        else ""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_send_reserve_reviewed_intent(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_reviewed_intent_id uuid,
                p_application_id uuid,
                p_recipient_email text,
                p_recipient_snapshot_sha256 text,
                p_subject_sha256 text,
                p_body_sha256 text,
                p_payload_sha256 text,
                p_approval_snapshot_sha256 text,
                p_attachment_manifest_sha256 text,
                p_idempotency_key text,
                p_trace_id text
            )
            RETURNS uuid
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_reservation_id uuid;
                v_outbox_event_id uuid;
                v_payload_version_id uuid;
                v_authorization careerops.autopilot_intent_authorizations%ROWTYPE;
                v_reservation_key text;
                v_reconciliation_key text;
                v_existing careerops.gmail_send_reservations%ROWTYPE;
            BEGIN
                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_reservations AS reservation
                WHERE reservation.owner_user_id = p_owner_user_id
                  AND reservation.idempotency_key = p_idempotency_key
                FOR KEY SHARE;
                IF v_existing.id IS NOT NULL THEN
                    {existing_replay_guard}
                    RETURN v_existing.id;
                END IF;

                PERFORM 1
                FROM careerops.gmail_send_drafts AS draft
                WHERE draft.owner_user_id = p_owner_user_id
                  AND draft.gmail_send_account_id = p_account_id
                  AND draft.reviewed_intent_id = p_reviewed_intent_id
                  AND draft.application_id = p_application_id
                  AND draft.recipient_email = p_recipient_email
                  AND draft.recipient_snapshot_sha256 = p_recipient_snapshot_sha256
                  AND draft.subject_sha256 = p_subject_sha256
                  AND draft.body_sha256 = p_body_sha256
                  AND draft.payload_sha256 = p_payload_sha256
                  AND draft.attachment_manifest_sha256 = p_attachment_manifest_sha256
                  AND draft.status = 'approved'
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send reservation requires approved exact draft'
                        USING ERRCODE = '23514';
                END IF;

                SELECT payload.id
                INTO v_payload_version_id
                FROM careerops.action_payload_versions AS payload
                WHERE payload.action_intent_id = p_reviewed_intent_id
                  AND payload.payload_hash = p_payload_sha256
                ORDER BY payload.version DESC
                LIMIT 1
                FOR KEY SHARE;
                IF v_payload_version_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send payload version is missing'
                        USING ERRCODE = '23503';
                END IF;

                SELECT *
                INTO v_authorization
                FROM careerops.autopilot_intent_authorizations AS authz
                WHERE authz.action_intent_id = p_reviewed_intent_id
                  AND authz.payload_version_id = v_payload_version_id
                  AND authz.payload_hash = p_payload_sha256
                  AND authz.authorization_outcome = 'allow_autopilot_submission'
                  AND authz.expires_at > v_now
                ORDER BY authz.created_at DESC
                LIMIT 1
                FOR KEY SHARE;
                IF v_authorization.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reservation requires live autopilot authorization'
                        USING ERRCODE = '23514';
                END IF;

                v_reservation_id := gen_random_uuid();
                v_outbox_event_id := gen_random_uuid();
                v_reservation_key := 'gmail-send-exact' || chr(58) || p_reviewed_intent_id::text || chr(58) || p_payload_sha256;
                v_reconciliation_key := v_reservation_key || chr(58) || 'reconcile';

                INSERT INTO careerops.autopilot_cap_reservations (
                    id,
                    campaign_id,
                    grant_version_id,
                    authorization_id,
                    action_intent_id,
                    payload_version_id,
                    payload_hash,
                    target_host,
                    channel,
                    release_version,
                    company_key,
                    adapter_id,
                    fixture_id,
                    reservation_key,
                    reconciliation_key,
                    release_evidence_hash,
                    release_evidence_expires_at,
                    reserved_at
                ) VALUES (
                    gen_random_uuid(),
                    v_authorization.campaign_id,
                    v_authorization.grant_version_id,
                    v_authorization.id,
                    p_reviewed_intent_id,
                    v_payload_version_id,
                    p_payload_sha256,
                    'gmail.googleapis.com',
                    'gmail:send',
                    'gmail-send.v1',
                    p_recipient_snapshot_sha256,
                    'gmail',
                    'gmail-send.v1',
                    v_reservation_key,
                    v_reconciliation_key,
                    p_approval_snapshot_sha256,
                    v_now + interval '1 day',
                    v_now
                );
                INSERT INTO careerops.outbox_events (
                    id, event_key, action_intent_id, payload_version_id, event_type, status, available_at
                ) VALUES (
                    v_outbox_event_id, 'gmail-send:' || v_reservation_key,
                    p_reviewed_intent_id, v_payload_version_id, 'workflow_signal', 'pending', v_now
                );
                INSERT INTO careerops.gmail_send_reservations (
                    id, owner_user_id, gmail_send_account_id, reviewed_intent_id, application_id,
                    recipient_email, recipient_snapshot_sha256, subject_sha256, body_sha256,
                    payload_sha256, approval_snapshot_sha256, attachment_manifest_sha256,
                    status, outbox_event_id, idempotency_key, trace_id, created_at
                ) VALUES (
                    v_reservation_id, p_owner_user_id, p_account_id, p_reviewed_intent_id, p_application_id,
                    p_recipient_email, p_recipient_snapshot_sha256, p_subject_sha256, p_body_sha256,
                    p_payload_sha256, p_approval_snapshot_sha256, p_attachment_manifest_sha256,
                    'reserved', v_outbox_event_id, p_idempotency_key, p_trace_id, v_now
                );
                INSERT INTO careerops.gmail_send_command_receipts (
                    id, owner_user_id, account_id, action_intent_id, outbox_event_id, command_kind,
                    idempotency_key, request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_account_id, p_reviewed_intent_id, v_outbox_event_id,
                    'reserve_and_enqueue', p_idempotency_key, p_payload_sha256,
                    jsonb_build_object('reservation_id', v_reservation_id::text, 'outbox_event_id', v_outbox_event_id::text),
                    p_trace_id
                );
                UPDATE careerops.action_intents
                SET status = 'eligible', updated_at = v_now
                WHERE id = p_reviewed_intent_id;
                RETURN v_reservation_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_send_reserve_reviewed_intent{_RESERVE_REVIEWED_SIGNATURE} FROM PUBLIC"
        )
    )


def upgrade() -> None:
    _install_list_proposals(qualify_account_lookup=True)
    _install_reserve_reviewed_intent(validate_replay=True)


def downgrade() -> None:
    _install_reserve_reviewed_intent(validate_replay=False)
    _install_list_proposals(qualify_account_lookup=False)
