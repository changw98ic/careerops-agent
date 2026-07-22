"""harden gmail replay invariants

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-21
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_READONLY_RECORD_SIGNAL_SIGNATURE = "(uuid, uuid, text, text, text, text, text, text, text, text, timestamptz, text, text, numeric, jsonb, text, text, text, jsonb, text, text)"
_READONLY_REVIEW_PROPOSAL_SIGNATURE = "(uuid, uuid, text, uuid, uuid, text, text, text, text)"
_SEND_REVIEW_EXACT_DRAFT_SIGNATURE = (
    "(uuid, uuid, uuid, text, text, text, text, text, text, text, text, text, text)"
)
_SEND_RECONCILE_OUTBOX_SIGNATURE = (
    "(uuid, text, uuid, text, text, text, jsonb, timestamp with time zone)"
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


def _install_readonly_record_message_signal(*, validate_replay: bool) -> None:
    replay_guard = ""
    conflict_clause = (
        """
    ON CONFLICT (gmail_account_id, provider_message_id) DO NOTHING
    RETURNING id INTO v_signal_id;
    IF v_signal_id IS NULL THEN
        SELECT id INTO v_signal_id FROM gmail_message_signals
        WHERE gmail_account_id = v_run.gmail_account_id
          AND provider_message_id = p_provider_message_id;
    END IF;
        """
        if not validate_replay
        else """
    ON CONFLICT (gmail_account_id, provider_message_id) DO NOTHING
    RETURNING id INTO v_signal_id;
    IF v_signal_id IS NULL THEN
        SELECT * INTO v_existing_signal
        FROM gmail_message_signals
        WHERE gmail_account_id = v_run.gmail_account_id
          AND provider_message_id = p_provider_message_id
        FOR KEY SHARE;
        IF v_existing_signal.id IS NULL THEN
            RAISE EXCEPTION 'gmail message signal replay target disappeared'
                USING ERRCODE = '40001';
        END IF;
        IF v_existing_signal.owner_user_id IS DISTINCT FROM v_run.owner_user_id
           OR v_existing_signal.provider_thread_id IS DISTINCT FROM p_provider_thread_id
           OR v_existing_signal.provider_history_id IS DISTINCT FROM p_provider_history_id
           OR v_existing_signal.message_sha256 IS DISTINCT FROM p_message_sha256
           OR v_existing_signal.thread_sha256 IS DISTINCT FROM p_thread_sha256
           OR v_existing_signal.sender_sha256 IS DISTINCT FROM p_sender_sha256
           OR v_existing_signal.subject_sha256 IS DISTINCT FROM p_subject_sha256
           OR v_existing_signal.snippet_sha256 IS DISTINCT FROM p_snippet_sha256
           OR v_existing_signal.redacted_excerpt IS DISTINCT FROM p_redacted_excerpt
           OR v_existing_signal.label_ids_hash IS DISTINCT FROM p_label_ids_hash
           OR v_existing_signal.received_at IS DISTINCT FROM p_received_at
           OR v_existing_signal.classification IS DISTINCT FROM p_classification
           OR v_existing_signal.relevance IS DISTINCT FROM p_relevance
           OR v_existing_signal.confidence IS DISTINCT FROM p_confidence
           OR v_existing_signal.provenance_json IS DISTINCT FROM p_provenance_json
           OR v_existing_signal.signal_sha256 IS DISTINCT FROM p_signal_sha256 THEN
            RAISE EXCEPTION 'gmail message signal replay conflicts'
                USING ERRCODE = '23505';
        END IF;
        SELECT * INTO v_existing_proposal
        FROM gmail_signal_proposals
        WHERE gmail_message_signal_id = v_existing_signal.id
        FOR KEY SHARE;
        IF p_proposal_payload_json IS NULL THEN
            IF v_existing_proposal.id IS NOT NULL THEN
                RAISE EXCEPTION 'gmail message signal proposal replay conflicts'
                    USING ERRCODE = '23505';
            END IF;
        ELSE
            IF jsonb_typeof(p_proposal_payload_json) <> 'object'
               OR p_proposal_payload_sha256 !~ '^[a-f0-9]{64}$' THEN
                RAISE EXCEPTION 'gmail proposal payload is invalid' USING ERRCODE = '22023';
            END IF;
            IF v_existing_proposal.id IS NULL
               OR v_existing_proposal.proposal_kind IS DISTINCT FROM COALESCE(p_proposal_payload_json->>'proposal_kind', 'review_only')
               OR v_existing_proposal.review_priority IS DISTINCT FROM COALESCE(p_proposal_payload_json->>'review_priority', 'normal')
               OR v_existing_proposal.payload_json IS DISTINCT FROM p_proposal_payload_json
               OR v_existing_proposal.payload_sha256 IS DISTINCT FROM p_proposal_payload_sha256 THEN
                RAISE EXCEPTION 'gmail message signal proposal replay conflicts'
                    USING ERRCODE = '23505';
            END IF;
        END IF;
        RETURN v_existing_signal.id;
    END IF;
        """
    )
    existing_declarations = (
        """
    v_existing_signal careerops.gmail_message_signals%ROWTYPE;
    v_existing_proposal careerops.gmail_signal_proposals%ROWTYPE;
        """
        if validate_replay
        else ""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_readonly_record_message_signal(
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
                {existing_declarations}
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
                   OR p_provider_history_id !~ '^[0-9]{{1,40}}$'
                   OR p_message_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_thread_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR (p_sender_sha256 IS NOT NULL AND p_sender_sha256 !~ '^[a-f0-9]{{64}}$')
                   OR p_subject_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_snippet_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_signal_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_label_ids_hash !~ '^[a-f0-9]{{64}}$'
                   OR p_redacted_excerpt IS NULL OR char_length(p_redacted_excerpt) > 500
                   OR p_provenance_json IS NULL OR jsonb_typeof(p_provenance_json) <> 'object'
                   OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{{1,128}}$' THEN
                    RAISE EXCEPTION 'gmail message signal command is invalid' USING ERRCODE = '22023';
                END IF;
                {replay_guard}
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
                {conflict_clause}
                IF p_proposal_payload_json IS NOT NULL THEN
                    IF jsonb_typeof(p_proposal_payload_json) <> 'object'
                       OR p_proposal_payload_sha256 !~ '^[a-f0-9]{{64}}$' THEN
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
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_readonly_record_message_signal{_READONLY_RECORD_SIGNAL_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_mailbox",
        f"GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_record_message_signal{_READONLY_RECORD_SIGNAL_SIGNATURE} TO careerops_mailbox",
    )


def _install_readonly_review_proposal(*, bind_reason: bool) -> None:
    reason_guard = (
        "           OR v_existing.reason IS DISTINCT FROM p_reason\n" if bind_reason else ""
    )
    reason_hash_part = "'reason', p_reason, " if bind_reason else ""
    insert_conflict_clause = (
        """
                ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
                RETURNING id INTO v_inserted_decision_id;
                IF v_inserted_decision_id IS NULL THEN
                    SELECT * INTO v_existing
                    FROM gmail_signal_review_decisions
                    WHERE owner_user_id = p_owner_user_id AND idempotency_key = p_idempotency_key
                    FOR KEY SHARE;
                    IF v_existing.id IS NULL THEN
                        RAISE EXCEPTION 'gmail review decision replay target disappeared' USING ERRCODE = '40001';
                    END IF;
                    IF v_existing.proposal_id <> p_proposal_id OR v_existing.decision <> p_decision
                       OR v_existing.canonical_job_id IS DISTINCT FROM p_canonical_job_id
                       OR v_existing.job_posting_id IS DISTINCT FROM p_job_posting_id
                       OR v_existing.reason IS DISTINCT FROM p_reason
                       OR v_existing.payload_sha256 <> p_payload_sha256 THEN
                        RAISE EXCEPTION 'gmail review idempotency key is bound to different decision' USING ERRCODE = '23505';
                    END IF;
                    RETURN v_existing.id;
                END IF;
        """
        if bind_reason
        else ";"
    )
    receipt_decision_id = "v_inserted_decision_id" if bind_reason else "v_decision_id"
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_readonly_review_proposal(
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
                v_inserted_decision_id uuid;
                v_existing careerops.gmail_signal_review_decisions%ROWTYPE;
            BEGIN
                IF p_owner_user_id IS NULL OR p_proposal_id IS NULL
                   OR p_decision NOT IN ('approve', 'reject')
                   OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
                   OR p_payload_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{{1,128}}$'
                   OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:-]{{1,128}}$' THEN
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
{reason_guard}                       OR v_existing.payload_sha256 <> p_payload_sha256 THEN
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
                )
                {insert_conflict_clause}
                INSERT INTO gmail_command_receipts (
                    id, owner_user_id, gmail_account_id, command_kind, idempotency_key,
                    request_sha256, response_json, trace_id, created_at
                )
                SELECT gen_random_uuid(), p_owner_user_id, proposal.gmail_account_id, 'review_proposal',
                       p_idempotency_key,
                       encode(public.digest(jsonb_build_object(
                           'owner_user_id', p_owner_user_id, 'proposal_id', p_proposal_id,
                           'decision', p_decision, 'canonical_job_id', p_canonical_job_id,
                           'job_posting_id', p_job_posting_id, {reason_hash_part}'payload_sha256', p_payload_sha256
                       )::text, 'sha256'), 'hex'),
                       jsonb_build_object(
                           'decision_id', v_decision_id,
                           'canonical_job_id', p_canonical_job_id,
                           'job_posting_id', p_job_posting_id
                       ),
                       p_trace_id, clock_timestamp()
                FROM gmail_signal_proposals AS proposal
                WHERE proposal.id = p_proposal_id;
                RETURN {receipt_decision_id};
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_readonly_review_proposal{_READONLY_REVIEW_PROPOSAL_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_api",
        f"GRANT EXECUTE ON FUNCTION careerops.gmail_readonly_review_proposal{_READONLY_REVIEW_PROPOSAL_SIGNATURE} TO careerops_api",
    )


def _install_send_review_exact_payload_draft(*, validate_replay: bool) -> None:
    existing_declaration = (
        "v_existing careerops.gmail_send_drafts%ROWTYPE;" if validate_replay else ""
    )
    existing_guard = (
        """
                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_drafts AS draft
                WHERE draft.owner_user_id = p_owner_user_id
                  AND draft.review_idempotency_key = p_idempotency_key
                FOR KEY SHARE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.id IS DISTINCT FROM p_draft_id
                       OR v_existing.gmail_send_account_id IS DISTINCT FROM p_account_id
                       OR v_existing.status IS DISTINCT FROM p_decision
                       OR v_existing.review_reason IS DISTINCT FROM p_reason
                       OR v_existing.requested_for IS DISTINCT FROM p_requested_for
                       OR v_existing.recipient_snapshot_sha256 IS DISTINCT FROM p_recipient_snapshot_sha256
                       OR v_existing.subject_sha256 IS DISTINCT FROM p_subject_sha256
                       OR v_existing.body_sha256 IS DISTINCT FROM p_body_sha256
                       OR v_existing.payload_sha256 IS DISTINCT FROM p_payload_sha256
                       OR v_existing.attachment_manifest_sha256 IS DISTINCT FROM p_attachment_manifest_sha256 THEN
                        RAISE EXCEPTION 'gmail send review idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN v_existing.id;
                END IF;
        """
        if validate_replay
        else ""
    )
    legacy_reviewed_guard = (
        """
                IF v_draft.status <> 'pending_review' THEN
                    IF v_draft.review_idempotency_key = p_idempotency_key THEN
                        RETURN v_draft.id;
                    END IF;
                    RAISE EXCEPTION 'gmail send draft was already reviewed'
                        USING ERRCODE = '23505';
                END IF;
        """
        if not validate_replay
        else """
                IF v_draft.status <> 'pending_review' THEN
                    IF v_draft.review_idempotency_key = p_idempotency_key
                       AND v_draft.status IS NOT DISTINCT FROM p_decision
                       AND v_draft.review_reason IS NOT DISTINCT FROM p_reason
                       AND v_draft.requested_for IS NOT DISTINCT FROM p_requested_for
                       AND v_draft.recipient_snapshot_sha256 IS NOT DISTINCT FROM p_recipient_snapshot_sha256
                       AND v_draft.subject_sha256 IS NOT DISTINCT FROM p_subject_sha256
                       AND v_draft.body_sha256 IS NOT DISTINCT FROM p_body_sha256
                       AND v_draft.payload_sha256 IS NOT DISTINCT FROM p_payload_sha256
                       AND v_draft.attachment_manifest_sha256 IS NOT DISTINCT FROM p_attachment_manifest_sha256 THEN
                        RETURN v_draft.id;
                    END IF;
                    RAISE EXCEPTION 'gmail send draft was already reviewed'
                        USING ERRCODE = '23505';
                END IF;
        """
    )
    request_hash_expression = (
        """encode(public.digest(jsonb_build_object(
                        'owner_user_id', p_owner_user_id,
                        'account_id', p_account_id,
                        'draft_id', p_draft_id,
                        'decision', p_decision,
                        'requested_for', p_requested_for,
                        'recipient_snapshot_sha256', p_recipient_snapshot_sha256,
                        'subject_sha256', p_subject_sha256,
                        'body_sha256', p_body_sha256,
                        'payload_sha256', p_payload_sha256,
                        'attachment_manifest_sha256', p_attachment_manifest_sha256,
                        'reason', p_reason
                    )::text, 'sha256'), 'hex')"""
        if validate_replay
        else "p_payload_sha256"
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.gmail_send_review_exact_payload_draft(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_draft_id uuid,
                p_decision text,
                p_requested_for text,
                p_recipient_snapshot_sha256 text,
                p_subject_sha256 text,
                p_body_sha256 text,
                p_payload_sha256 text,
                p_attachment_manifest_sha256 text,
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
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_draft careerops.gmail_send_drafts%ROWTYPE;
                {existing_declaration}
            BEGIN
                IF p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = '' THEN
                    RAISE EXCEPTION 'gmail send review payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                {existing_guard}
                IF p_requested_for <> 'gmail_send_exact_payload'
                   OR p_decision NOT IN ('approved', 'rejected') THEN
                    RAISE EXCEPTION 'gmail send review payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                SELECT *
                INTO v_draft
                FROM careerops.gmail_send_drafts AS draft
                WHERE draft.id = p_draft_id
                  AND draft.owner_user_id = p_owner_user_id
                  AND draft.gmail_send_account_id = p_account_id
                FOR UPDATE;
                IF v_draft.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send draft is missing'
                        USING ERRCODE = '23503';
                END IF;
                IF v_draft.recipient_snapshot_sha256 <> p_recipient_snapshot_sha256
                   OR v_draft.subject_sha256 <> p_subject_sha256
                   OR v_draft.body_sha256 <> p_body_sha256
                   OR v_draft.payload_sha256 <> p_payload_sha256
                   OR v_draft.attachment_manifest_sha256 <> p_attachment_manifest_sha256 THEN
                    RAISE EXCEPTION 'gmail send review must match exact draft payload'
                        USING ERRCODE = '23514';
                END IF;
                {legacy_reviewed_guard}
                UPDATE careerops.gmail_send_drafts
                SET status = p_decision,
                    review_reason = p_reason,
                    review_idempotency_key = p_idempotency_key,
                    reviewed_at = v_now,
                    updated_at = v_now,
                    trace_id = p_trace_id
                WHERE id = p_draft_id;
                INSERT INTO careerops.gmail_send_command_receipts (
                    id, owner_user_id, account_id, action_intent_id, command_kind,
                    idempotency_key, request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_account_id, v_draft.reviewed_intent_id,
                    'review_draft', p_idempotency_key, {request_hash_expression},
                    jsonb_build_object('draft_id', p_draft_id::text, 'decision', p_decision),
                    p_trace_id
                );
                RETURN p_draft_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.gmail_send_review_exact_payload_draft{_SEND_REVIEW_EXACT_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )


def _install_send_reconcile_outbox_event(*, validate_replay: bool) -> None:
    replay_guard = (
        """
                v_reconcile_request_sha256 := encode(public.digest(jsonb_build_object(
                    'provider_message_id', p_provider_message_id,
                    'provider_thread_id', p_provider_thread_id,
                    'rfc_message_id', p_rfc_message_id,
                    'sent_metadata_json', COALESCE(p_sent_metadata_json, '{}'::jsonb),
                    'reconciled_at', p_reconciled_at
                )::text, 'sha256'), 'hex');
                v_reconciled_metadata := COALESCE(p_sent_metadata_json, '{}'::jsonb) || jsonb_build_object(
                    'provider', 'gmail',
                    'provider_message_id', p_provider_message_id,
                    'provider_thread_id', p_provider_thread_id,
                    'rfc_message_id', p_rfc_message_id,
                    'reconciled_at', p_reconciled_at::text,
                    'gmail_reconcile_request_sha256', v_reconcile_request_sha256
                );
                IF v_attempt.response_metadata ? 'gmail_reconcile_request_sha256' THEN
                    IF v_attempt.response_metadata->>'gmail_reconcile_request_sha256' IS DISTINCT FROM v_reconcile_request_sha256 THEN
                        RAISE EXCEPTION 'gmail send reconcile replay conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN;
                END IF;
                IF v_attempt.response_metadata ? 'provider_message_id'
                   OR v_attempt.response_metadata ? 'provider_thread_id'
                   OR v_attempt.response_metadata ? 'rfc_message_id'
                   OR v_attempt.response_metadata ? 'reconciled_at' THEN
                    RAISE EXCEPTION 'gmail send reconcile replay is missing canonical marker'
                        USING ERRCODE = '23505';
                END IF;
        """
        if validate_replay
        else ""
    )
    metadata_expression = (
        "response_metadata || v_reconciled_metadata"
        if validate_replay
        else """response_metadata || COALESCE(p_sent_metadata_json, '{}'::jsonb) || jsonb_build_object(
                    'provider', 'gmail',
                    'provider_message_id', p_provider_message_id,
                    'provider_thread_id', p_provider_thread_id,
                    'rfc_message_id', p_rfc_message_id,
                    'reconciled_at', p_reconciled_at::text
                )"""
    )
    metadata_declaration = (
        """
                v_reconciled_metadata jsonb;
                v_reconcile_request_sha256 text;
        """
        if validate_replay
        else ""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.reconcile_gmail_send_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_provider_message_id text,
                p_provider_thread_id text,
                p_rfc_message_id text,
                p_sent_metadata_json jsonb,
                p_reconciled_at timestamp with time zone
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_event careerops.outbox_events%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
                {metadata_declaration}
            BEGIN
                SELECT *
                INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;
                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                    RAISE EXCEPTION 'gmail send reconcile lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                SELECT *
                INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state IN ('processing', 'confirmed')
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconcile requires prepared attempt'
                        USING ERRCODE = '23514';
                END IF;
                {replay_guard}
                UPDATE careerops.side_effect_attempts
                SET response_metadata = {metadata_expression}
                WHERE id = v_attempt.id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION careerops.reconcile_gmail_send_outbox_event{_SEND_RECONCILE_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )
    _grant_if_role_exists(
        "careerops_mail_sender",
        f"GRANT EXECUTE ON FUNCTION careerops.reconcile_gmail_send_outbox_event{_SEND_RECONCILE_OUTBOX_SIGNATURE} TO careerops_mail_sender",
    )


def upgrade() -> None:
    _install_readonly_record_message_signal(validate_replay=True)
    _install_readonly_review_proposal(bind_reason=True)
    _install_send_review_exact_payload_draft(validate_replay=True)
    _install_send_reconcile_outbox_event(validate_replay=True)


def downgrade() -> None:
    _install_send_reconcile_outbox_event(validate_replay=False)
    _install_send_review_exact_payload_draft(validate_replay=False)
    _install_readonly_review_proposal(bind_reason=False)
    _install_readonly_record_message_signal(validate_replay=False)
