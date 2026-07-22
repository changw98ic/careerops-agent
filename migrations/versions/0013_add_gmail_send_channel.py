"""add reviewed gmail send channel boundary

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-20
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "gmail_send_command_receipts",
    "gmail_send_reservations",
    "gmail_send_review_evidence",
)

_GMAIL_SEND_TABLES = (
    "gmail_send_accounts",
    "gmail_send_command_receipts",
    "gmail_send_drafts",
    "gmail_send_reconciliation_jobs",
    "gmail_send_reservations",
    "gmail_send_review_evidence",
)

_REGISTER_FUNCTION = "careerops.gmail_send_register_account"
_REGISTER_SIGNATURE = "(uuid, uuid, text, text, text, text, text, text, text, integer)"
_RESERVE_FUNCTION = "careerops.gmail_send_reserve_and_enqueue"
_RESERVE_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, text, text, uuid, uuid, text, text, text, text, text, text, text)"
_PREPARE_FUNCTION = "careerops.gmail_send_prepare_outbox_event"
_PREPARE_SIGNATURE = "(uuid, text, uuid)"
_RECORD_FUNCTION = "careerops.gmail_send_record_receipt"
_RECORD_SIGNATURE = "(uuid, text, uuid, text, text, timestamp with time zone, uuid)"
_AMBIGUOUS_FUNCTION = "careerops.gmail_send_record_ambiguity"
_AMBIGUOUS_SIGNATURE = "(uuid, text, uuid, text)"
_RECONCILE_FUNCTION = "careerops.gmail_send_reconcile_receipt"
_RECONCILE_SIGNATURE = "(uuid, uuid, text, text, uuid, text)"
_STATUS_FUNCTION = "careerops.gmail_send_status"
_STATUS_SIGNATURE = "(uuid, uuid)"
_LIST_FUNCTION = "careerops.gmail_send_list_accounts"
_LIST_SIGNATURE = "(uuid, integer)"
_CREATE_DRAFT_FUNCTION = "careerops.gmail_send_create_draft"
_CREATE_DRAFT_SIGNATURE = "(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)"
_REVIEW_DRAFT_FUNCTION = "careerops.gmail_send_review_draft"
_REVIEW_DRAFT_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, uuid, text, text, timestamp with time zone, text, text)"
_REGISTER_ACCOUNT_WITH_READONLY_SIGNATURE = (
    "(uuid, uuid, text, text, text, text, text, integer, uuid, text, text)"
)
_REGISTRATION_RECEIPT_FUNCTION = "careerops.gmail_send_registration_receipt"
_REGISTRATION_RECEIPT_SIGNATURE = "(uuid, text)"
_ACCOUNT_STATUS_FUNCTION = "careerops.gmail_send_account_status"
_ACCOUNT_STATUS_SIGNATURE = "(uuid, uuid)"
_CREATE_EXACT_DRAFT_FUNCTION = "careerops.gmail_send_create_exact_payload_draft"
_CREATE_EXACT_DRAFT_SIGNATURE = (
    "(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)"
)
_REVIEW_EXACT_DRAFT_FUNCTION = "careerops.gmail_send_review_exact_payload_draft"
_REVIEW_EXACT_DRAFT_SIGNATURE = (
    "(uuid, uuid, uuid, text, text, text, text, text, text, text, text, text, text)"
)
_LIST_DRAFTS_FUNCTION = "careerops.gmail_send_list_drafts"
_LIST_DRAFTS_SIGNATURE = "(uuid, uuid, integer)"
_RESERVE_REVIEWED_FUNCTION = "careerops.gmail_send_reserve_reviewed_intent"
_RESERVE_REVIEWED_SIGNATURE = (
    "(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)"
)
_LIST_RESERVATIONS_FUNCTION = "careerops.gmail_send_list_reservations"
_LIST_RESERVATIONS_SIGNATURE = "(uuid, uuid, integer)"
_PREPARE_OUTBOX_FUNCTION = "careerops.prepare_gmail_send_outbox_event"
_PREPARE_OUTBOX_SIGNATURE = "(uuid, text, uuid)"
_CLAIM_OUTBOX_FUNCTION = "careerops.claim_gmail_send_outbox_events"
_CLAIM_OUTBOX_SIGNATURE = "(text, integer, integer)"
_MARK_PUBLISHED_FUNCTION = "careerops.mark_gmail_send_outbox_published"
_MARK_PUBLISHED_SIGNATURE = "(uuid, text, uuid)"
_RELEASE_OUTBOX_FUNCTION = "careerops.release_gmail_send_outbox_event"
_RELEASE_OUTBOX_SIGNATURE = "(uuid, text, uuid, timestamp with time zone, text, boolean)"
_RECONCILE_OUTBOX_FUNCTION = "careerops.reconcile_gmail_send_outbox_event"
_RECONCILE_OUTBOX_SIGNATURE = (
    "(uuid, text, uuid, text, text, text, jsonb, timestamp with time zone)"
)
_RECORD_OUTBOX_RECEIPT_FUNCTION = "careerops.record_gmail_send_outbox_receipt"
_RECORD_OUTBOX_RECEIPT_SIGNATURE = (
    "(uuid, text, uuid, text, text, text, text, timestamp with time zone, uuid)"
)
_RECORD_OUTBOX_AMBIGUITY_FUNCTION = "careerops.record_gmail_send_outbox_ambiguity"
_RECORD_OUTBOX_AMBIGUITY_SIGNATURE = "(uuid, text, uuid, text)"
_CLAIM_RECONCILIATION_FUNCTION = "careerops.claim_gmail_send_reconciliation_jobs"
_CLAIM_RECONCILIATION_SIGNATURE = "(text, integer, integer)"
_RECONCILE_AMBIGUOUS_FUNCTION = "careerops.reconcile_ambiguous_gmail_send_outbox_event"
_RECONCILE_AMBIGUOUS_SIGNATURE = (
    "(uuid, text, uuid, text, text, text, jsonb, timestamp with time zone, uuid)"
)
_KEEP_AMBIGUOUS_FUNCTION = "careerops.keep_gmail_send_reconciliation_ambiguous"
_KEEP_AMBIGUOUS_SIGNATURE = "(uuid, text, uuid, text)"

_API_GRANTS = (
    f"GRANT EXECUTE ON FUNCTION {_CREATE_DRAFT_FUNCTION}{_CREATE_DRAFT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_REVIEW_DRAFT_FUNCTION}{_REVIEW_DRAFT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_REGISTER_FUNCTION}{_REGISTER_ACCOUNT_WITH_READONLY_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_RESERVE_FUNCTION}{_RESERVE_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_STATUS_FUNCTION}{_STATUS_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_LIST_FUNCTION}{_LIST_SIGNATURE} TO careerops_api",
)

_MAIL_SENDER_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_PREPARE_OUTBOX_FUNCTION}{_PREPARE_OUTBOX_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_CLAIM_OUTBOX_FUNCTION}{_CLAIM_OUTBOX_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_MARK_PUBLISHED_FUNCTION}{_MARK_PUBLISHED_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_RELEASE_OUTBOX_FUNCTION}{_RELEASE_OUTBOX_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECONCILE_OUTBOX_FUNCTION}{_RECONCILE_OUTBOX_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECORD_OUTBOX_RECEIPT_FUNCTION}{_RECORD_OUTBOX_RECEIPT_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECORD_OUTBOX_AMBIGUITY_FUNCTION}{_RECORD_OUTBOX_AMBIGUITY_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_CLAIM_RECONCILIATION_FUNCTION}{_CLAIM_RECONCILIATION_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECONCILE_AMBIGUOUS_FUNCTION}{_RECONCILE_AMBIGUOUS_SIGNATURE} TO careerops_mail_sender",
    f"GRANT EXECUTE ON FUNCTION {_KEEP_AMBIGUOUS_FUNCTION}{_KEEP_AMBIGUOUS_SIGNATURE} TO careerops_mail_sender",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.gmail_send_accounts, careerops.gmail_send_command_receipts, careerops.gmail_send_drafts, careerops.gmail_send_reconciliation_jobs, careerops.gmail_send_reservations, careerops.gmail_send_review_evidence TO careerops_readonly",
)

_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_api, careerops_workflow, careerops_mailbox, careerops_outbox, careerops_readonly"
    for table_name in _GMAIL_SEND_TABLES
)

_API_REVOKES = tuple(
    f"REVOKE ALL ON FUNCTION {function}{signature} FROM careerops_api"
    for function, signature in (
        (_CREATE_DRAFT_FUNCTION, _CREATE_DRAFT_SIGNATURE),
        (_REVIEW_DRAFT_FUNCTION, _REVIEW_DRAFT_SIGNATURE),
        (_REGISTER_FUNCTION, _REGISTER_ACCOUNT_WITH_READONLY_SIGNATURE),
        (_REGISTRATION_RECEIPT_FUNCTION, _REGISTRATION_RECEIPT_SIGNATURE),
        (_RESERVE_FUNCTION, _RESERVE_SIGNATURE),
        (_STATUS_FUNCTION, _STATUS_SIGNATURE),
        (_ACCOUNT_STATUS_FUNCTION, _ACCOUNT_STATUS_SIGNATURE),
        (_LIST_FUNCTION, _LIST_SIGNATURE),
        (_CREATE_EXACT_DRAFT_FUNCTION, _CREATE_EXACT_DRAFT_SIGNATURE),
        (_REVIEW_EXACT_DRAFT_FUNCTION, _REVIEW_EXACT_DRAFT_SIGNATURE),
        (_LIST_DRAFTS_FUNCTION, _LIST_DRAFTS_SIGNATURE),
        (_RESERVE_REVIEWED_FUNCTION, _RESERVE_REVIEWED_SIGNATURE),
        (_LIST_RESERVATIONS_FUNCTION, _LIST_RESERVATIONS_SIGNATURE),
    )
)

_MAIL_SENDER_REVOKES = (
    "REVOKE USAGE ON SCHEMA careerops FROM careerops_mail_sender",
    *(
        f"REVOKE ALL ON FUNCTION {function}{signature} FROM careerops_mail_sender"
        for function, signature in (
            (_PREPARE_FUNCTION, _PREPARE_SIGNATURE),
            (_RECORD_FUNCTION, _RECORD_SIGNATURE),
            (_AMBIGUOUS_FUNCTION, _AMBIGUOUS_SIGNATURE),
            (_RECONCILE_FUNCTION, _RECONCILE_SIGNATURE),
            (_PREPARE_OUTBOX_FUNCTION, _PREPARE_OUTBOX_SIGNATURE),
            (_CLAIM_OUTBOX_FUNCTION, _CLAIM_OUTBOX_SIGNATURE),
            (_MARK_PUBLISHED_FUNCTION, _MARK_PUBLISHED_SIGNATURE),
            (_RELEASE_OUTBOX_FUNCTION, _RELEASE_OUTBOX_SIGNATURE),
            (_RECONCILE_OUTBOX_FUNCTION, _RECONCILE_OUTBOX_SIGNATURE),
            (_RECORD_OUTBOX_RECEIPT_FUNCTION, _RECORD_OUTBOX_RECEIPT_SIGNATURE),
            (_RECORD_OUTBOX_AMBIGUITY_FUNCTION, _RECORD_OUTBOX_AMBIGUITY_SIGNATURE),
            (_CLAIM_RECONCILIATION_FUNCTION, _CLAIM_RECONCILIATION_SIGNATURE),
            (_RECONCILE_AMBIGUOUS_FUNCTION, _RECONCILE_AMBIGUOUS_SIGNATURE),
            (_KEEP_AMBIGUOUS_FUNCTION, _KEEP_AMBIGUOUS_SIGNATURE),
        )
    ),
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


def _install_outbox_lifecycle_guard(*, allow_reconciled_gmail: bool) -> None:
    reconciliation_transition = (
        """
                IF OLD.status = 'failed' AND NEW.status = 'published' THEN
                    IF current_user IS DISTINCT FROM (
                           SELECT pg_catalog.pg_get_userbyid(class.relowner)
                           FROM pg_catalog.pg_class AS class
                           JOIN pg_catalog.pg_namespace AS namespace
                             ON namespace.oid = class.relnamespace
                           WHERE namespace.nspname = 'careerops'
                             AND class.relname = 'outbox_events'
                       )
                       OR current_setting(
                           'careerops.outbox_reconciliation_event_id', true
                       ) IS DISTINCT FROM OLD.id::text
                       OR NEW.attempt_count <> OLD.attempt_count
                       OR NEW.last_error_code IS NOT NULL
                       OR NEW.lease_owner IS NOT NULL
                       OR NEW.lease_token IS NOT NULL
                       OR NEW.lease_until IS NOT NULL
                       OR NEW.published_at IS NULL THEN
                        RAISE EXCEPTION 'invalid reconciled outbox publish transition'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;
    """
        if allow_reconciled_gmail
        else ""
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION careerops.enforce_outbox_lifecycle()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.status = 'pending'
                       AND NEW.lease_owner IS NULL
                       AND NEW.lease_token IS NULL
                       AND NEW.lease_until IS NULL
                       AND NEW.attempt_count = 0
                       AND NEW.published_at IS NULL
                       AND NEW.last_error_code IS NULL THEN
                        RETURN NEW;
                    END IF;
                    RAISE EXCEPTION 'new outbox event must start pending'
                        USING ERRCODE = '55000';
                END IF;

                IF OLD.status = 'pending' AND NEW.status = 'leased' THEN
                    IF NEW.attempt_count <> OLD.attempt_count + 1
                       OR NEW.last_error_code IS NOT NULL THEN
                        RAISE EXCEPTION 'invalid initial outbox claim'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.status = 'leased' AND NEW.status = 'leased' THEN
                    IF OLD.lease_until > CURRENT_TIMESTAMP
                       OR NEW.lease_token IS NOT DISTINCT FROM OLD.lease_token
                       OR NEW.attempt_count <> OLD.attempt_count + 1
                       OR NEW.last_error_code IS NOT NULL THEN
                        RAISE EXCEPTION 'outbox lease is not reclaimable'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.status = 'leased' AND NEW.status = 'published' THEN
                    IF current_setting(
                           'careerops.outbox_lease_owner', true
                       ) IS DISTINCT FROM OLD.lease_owner
                       OR current_setting(
                           'careerops.outbox_lease_token', true
                       ) IS DISTINCT FROM OLD.lease_token::text
                       OR NEW.attempt_count <> OLD.attempt_count
                       OR NEW.last_error_code IS NOT NULL THEN
                        RAISE EXCEPTION 'invalid outbox publish transition'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.status = 'leased' AND NEW.status IN ('pending', 'failed') THEN
                    IF current_setting(
                           'careerops.outbox_lease_owner', true
                       ) IS DISTINCT FROM OLD.lease_owner
                       OR current_setting(
                           'careerops.outbox_lease_token', true
                       ) IS DISTINCT FROM OLD.lease_token::text
                       OR NEW.attempt_count <> OLD.attempt_count
                       OR NEW.last_error_code IS NULL THEN
                        RAISE EXCEPTION 'invalid outbox release transition'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                {reconciliation_transition}

                RAISE EXCEPTION 'invalid outbox lifecycle transition % -> %',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END
            $function$
            """
        )
    )


def _install_cap_reservation_insert_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION careerops.enforce_autopilot_cap_reservation_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                authorization_outcome text;
                authorized_payload_hash text;
                authorized_expires_at timestamp with time zone;
                policy_decision text;
                grant_expires_at timestamp with time zone;
                grant_max_total integer;
                grant_max_daily integer;
                grant_max_per_company integer;
                grant_allowed_action_kinds jsonb;
                grant_allowed_channels jsonb;
                grant_allowed_target_hosts jsonb;
                grant_material_hashes jsonb;
                grant_release_version text;
                intent_action_kind text;
                payload_target jsonb;
                payload_attachment_refs jsonb;
                total_reserved integer;
                daily_reserved integer;
                company_reserved integer;
            BEGIN
                NEW.reserved_at := CURRENT_TIMESTAMP;
                NEW.reservation_date := CURRENT_DATE;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-grant-state:' || NEW.grant_version_id::text,
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-cap-reservation:' || NEW.grant_version_id::text,
                        0
                    )
                );

                SELECT
                    grant_version.expires_at,
                    grant_version.max_total_submissions,
                    grant_version.max_daily_submissions,
                    grant_version.max_per_company,
                    grant_version.allowed_action_kinds,
                    grant_version.allowed_channels,
                    grant_version.allowed_target_hosts,
                    grant_version.material_hashes,
                    grant_version.release_version
                INTO
                    grant_expires_at,
                    grant_max_total,
                    grant_max_daily,
                    grant_max_per_company,
                    grant_allowed_action_kinds,
                    grant_allowed_channels,
                    grant_allowed_target_hosts,
                    grant_material_hashes,
                    grant_release_version
                FROM careerops.autopilot_grant_versions AS grant_version
                WHERE grant_version.id = NEW.grant_version_id
                  AND grant_version.campaign_id = NEW.campaign_id
                FOR UPDATE;

                IF grant_expires_at IS NULL THEN
                    RAISE EXCEPTION 'autopilot grant % is missing for cap reservation', NEW.grant_version_id
                        USING ERRCODE = '23503';
                END IF;
                IF grant_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot grant % is expired for cap reservation', NEW.grant_version_id
                        USING ERRCODE = '23514';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM careerops.autopilot_grant_revocations AS revocation
                    WHERE revocation.grant_version_id = NEW.grant_version_id
                ) THEN
                    RAISE EXCEPTION 'autopilot grant % is revoked for cap reservation', NEW.grant_version_id
                        USING ERRCODE = '23514';
                END IF;

                SELECT
                    authz.authorization_outcome,
                    authz.payload_hash,
                    authz.expires_at,
                    decision_record.decision
                INTO
                    authorization_outcome,
                    authorized_payload_hash,
                    authorized_expires_at,
                    policy_decision
                FROM careerops.autopilot_intent_authorizations AS authz
                JOIN careerops.policy_decisions AS decision_record
                  ON decision_record.id = authz.policy_decision_id
                 AND decision_record.action_intent_id = authz.action_intent_id
                 AND decision_record.payload_version_id = authz.payload_version_id
                WHERE authz.id = NEW.authorization_id
                  AND authz.campaign_id = NEW.campaign_id
                  AND authz.grant_version_id = NEW.grant_version_id
                  AND authz.action_intent_id = NEW.action_intent_id
                  AND authz.payload_version_id = NEW.payload_version_id
                FOR KEY SHARE OF authz, decision_record;

                IF authorization_outcome IS NULL THEN
                    RAISE EXCEPTION 'autopilot authorization % does not match reservation', NEW.authorization_id
                        USING ERRCODE = '23503';
                END IF;
                IF authorization_outcome <> 'allow_autopilot_submission'
                   OR policy_decision <> 'allow_autopilot_submission' THEN
                    RAISE EXCEPTION 'cap reservation requires autopilot submission authorization'
                        USING ERRCODE = '23514';
                END IF;
                IF authorized_payload_hash <> NEW.payload_hash THEN
                    RAISE EXCEPTION 'cap reservation payload hash mismatch'
                        USING ERRCODE = '23514';
                END IF;
                IF authorized_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot authorization is expired for cap reservation'
                        USING ERRCODE = '23514';
                END IF;
                IF NEW.release_version <> grant_release_version THEN
                    RAISE EXCEPTION 'cap reservation release version is stale'
                        USING ERRCODE = '23514';
                END IF;

                SELECT intent.action_kind, payload.target, payload.attachment_refs
                INTO intent_action_kind, payload_target, payload_attachment_refs
                FROM careerops.action_intents AS intent
                JOIN careerops.action_payload_versions AS payload
                  ON payload.action_intent_id = intent.id
                 AND payload.id = NEW.payload_version_id
                WHERE intent.id = NEW.action_intent_id
                FOR KEY SHARE OF intent, payload;

                IF intent_action_kind IS NULL THEN
                    RAISE EXCEPTION 'dispatch intent % is missing', NEW.action_intent_id
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.channel = 'gmail:send'
                   OR NEW.target_host = 'gmail.googleapis.com'
                   OR NEW.adapter_id = 'gmail'
                   OR NEW.fixture_id = 'gmail-send.v1'
                   OR intent_action_kind = 'send_email' THEN
                    IF intent_action_kind <> 'send_email'
                       OR NEW.channel <> 'gmail:send'
                       OR NEW.target_host <> 'gmail.googleapis.com'
                       OR NEW.adapter_id <> 'gmail'
                       OR NEW.fixture_id <> 'gmail-send.v1' THEN
                        RAISE EXCEPTION 'gmail send reservation must use exact gmail send channel'
                            USING ERRCODE = '23514';
                    END IF;

                    IF NOT COALESCE(grant_allowed_action_kinds @> jsonb_build_array('send_email'), false)
                       OR NOT COALESCE(grant_allowed_channels @> jsonb_build_array('gmail:send'), false)
                       OR NOT COALESCE(grant_allowed_target_hosts @> jsonb_build_array('gmail.googleapis.com'), false) THEN
                        RAISE EXCEPTION 'gmail send reservation exceeds grant scope'
                            USING ERRCODE = '23514';
                    END IF;

                    IF payload_target ->> 'target_host' IS DISTINCT FROM 'gmail.googleapis.com'
                       OR payload_target ->> 'channel' IS DISTINCT FROM 'gmail:send'
                       OR payload_target ->> 'recipient_sha256' IS DISTINCT FROM NEW.company_key
                       OR (payload_target ->> 'body_sha256') IS NULL THEN
                        RAISE EXCEPTION 'gmail send reservation target does not match payload'
                            USING ERRCODE = '23514';
                    END IF;

                    IF NOT COALESCE(
                           grant_material_hashes @> jsonb_build_array(payload_target ->> 'body_sha256'),
                           false
                       ) THEN
                        RAISE EXCEPTION 'gmail send body material is outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                    IF COALESCE(
                           (payload_target ->> 'grant_material_hash') !~ '^[a-f0-9]{64}$',
                           true
                       )
                       OR NOT COALESCE(
                           grant_material_hashes @> jsonb_build_array(payload_target ->> 'grant_material_hash'),
                           false
                       ) THEN
                        RAISE EXCEPTION 'gmail send target material is outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;

                    IF jsonb_typeof(payload_attachment_refs) <> 'array' THEN
                        RAISE EXCEPTION 'gmail send attachment refs must be an array'
                            USING ERRCODE = '23514';
                    END IF;

                    IF EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(payload_attachment_refs) AS attachment(value)
                        WHERE jsonb_typeof(attachment.value) <> 'object'
                           OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                           OR NOT COALESCE(
                                   grant_material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'),
                                   false
                               )
                    ) THEN
                        RAISE EXCEPTION 'gmail send reservation includes material outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                ELSE
                    IF intent_action_kind <> 'submit_application'
                       OR NOT COALESCE(grant_allowed_action_kinds @> jsonb_build_array(intent_action_kind), false) THEN
                        RAISE EXCEPTION 'cap reservation requires granted application submission action'
                            USING ERRCODE = '23514';
                    END IF;
                    IF right(lower(NEW.target_host), 5) <> '.test'
                       OR NEW.channel NOT LIKE 'synthetic:%'
                       OR NEW.channel <> ('synthetic:' || NEW.adapter_id) THEN
                        RAISE EXCEPTION 'cap reservation must remain in the synthetic sandbox'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NOT COALESCE(grant_allowed_channels @> jsonb_build_array(NEW.channel), false)
                       OR NOT COALESCE(grant_allowed_target_hosts @> jsonb_build_array(NEW.target_host), false) THEN
                        RAISE EXCEPTION 'cap reservation exceeds grant channel or target scope'
                            USING ERRCODE = '23514';
                    END IF;
                    IF payload_target ->> 'target_host' IS DISTINCT FROM NEW.target_host
                       OR payload_target ->> 'channel' IS DISTINCT FROM NEW.channel THEN
                        RAISE EXCEPTION 'cap reservation target or channel does not match payload'
                            USING ERRCODE = '23514';
                    END IF;
                    IF jsonb_typeof(payload_attachment_refs) <> 'array'
                       OR jsonb_array_length(payload_attachment_refs) = 0 THEN
                        RAISE EXCEPTION 'cap reservation requires approved material evidence'
                            USING ERRCODE = '23514';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements(payload_attachment_refs) AS attachment(value)
                        WHERE jsonb_typeof(attachment.value) <> 'object'
                           OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                           OR NOT COALESCE(
                               grant_material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'),
                               false
                           )
                    ) THEN
                        RAISE EXCEPTION 'cap reservation includes material outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                SELECT count(*) INTO total_reserved
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.grant_version_id = NEW.grant_version_id;
                IF total_reserved >= grant_max_total THEN
                    RAISE EXCEPTION 'autopilot grant total cap exhausted'
                        USING ERRCODE = '23514';
                END IF;

                SELECT count(*) INTO daily_reserved
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.grant_version_id = NEW.grant_version_id
                  AND reservation.reservation_date = NEW.reservation_date;
                IF daily_reserved >= grant_max_daily THEN
                    RAISE EXCEPTION 'autopilot grant daily cap exhausted'
                        USING ERRCODE = '23514';
                END IF;

                SELECT count(*) INTO company_reserved
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.grant_version_id = NEW.grant_version_id
                  AND reservation.company_key = NEW.company_key;
                IF company_reserved >= grant_max_per_company THEN
                    RAISE EXCEPTION 'autopilot grant per-company cap exhausted'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NEW;
            END
            $function$
            """
        )
    )


def _install_grant_revocation_state_lock() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.lock_autopilot_grant_revocation_state()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-grant-state:' || NEW.grant_version_id::text,
                        0
                    )
                );
                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.lock_autopilot_grant_revocation_state() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_grant_revocations_00_state_lock "
            "BEFORE INSERT ON careerops.autopilot_grant_revocations "
            "FOR EACH ROW EXECUTE FUNCTION careerops.lock_autopilot_grant_revocation_state()"
        )
    )


def _drop_grant_revocation_state_lock() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_grant_revocations_00_state_lock "
            "ON careerops.autopilot_grant_revocations"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops.lock_autopilot_grant_revocation_state()"))


def _install_cap_reservation_execution_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION careerops.enforce_autopilot_cap_reservation_execution_boundary()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                intent_payload_version_id uuid;
                latest_kill_active boolean;
                provider_name text;
            BEGIN
                IF NEW.release_evidence_hash IS NULL
                   OR NEW.release_evidence_hash !~ '^[0-9a-f]{64}$'
                   OR NEW.release_evidence_expires_at IS NULL
                   OR NEW.release_evidence_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'cap reservation requires current release evidence'
                        USING ERRCODE = '23514';
                END IF;

                provider_name := CASE
                    WHEN NEW.channel = 'gmail:send' THEN 'gmail'
                    ELSE 'synthetic'
                END;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended('careerops:autopilot-kill-switch:global', 0));
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended('careerops:autopilot-kill-switch:campaign:' || NEW.campaign_id::text, 0));
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended('careerops:autopilot-kill-switch:provider:' || provider_name, 0));

                SELECT active INTO latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'global'
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(latest_kill_active, false) THEN
                    RAISE EXCEPTION 'global kill switch is active for cap reservation'
                        USING ERRCODE = '55000';
                END IF;

                SELECT active INTO latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'campaign'
                  AND campaign_id = NEW.campaign_id
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(latest_kill_active, false) THEN
                    RAISE EXCEPTION 'campaign kill switch is active for cap reservation'
                        USING ERRCODE = '55000';
                END IF;

                SELECT active INTO latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'provider'
                  AND provider = provider_name
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(latest_kill_active, false) THEN
                    RAISE EXCEPTION 'provider kill switch is active for cap reservation'
                        USING ERRCODE = '55000';
                END IF;

                UPDATE careerops.action_intents
                SET status = 'eligible',
                    current_payload_version_id = NEW.payload_version_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = NEW.action_intent_id
                  AND status IN ('proposed', 'awaiting_approval', 'eligible')
                  AND (
                      current_payload_version_id IS NULL
                      OR current_payload_version_id = NEW.payload_version_id
                  )
                RETURNING current_payload_version_id INTO intent_payload_version_id;

                IF intent_payload_version_id IS DISTINCT FROM NEW.payload_version_id THEN
                    RAISE EXCEPTION 'cap reservation requires an eligible action intent'
                        USING ERRCODE = '23514';
                END IF;

                RETURN NEW;
            END
            $function$
            """
        )
    )


def _install_draft_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_create_draft(
                p_owner_user_id uuid,
                p_candidate_id uuid,
                p_resource_id uuid,
                p_target jsonb,
                p_payload jsonb,
                p_attachment_refs jsonb,
                p_payload_hash text,
                p_ruleset_version text,
                p_idempotency_key text,
                p_trace_id text,
                p_decision_rule_reference text,
                p_expires_at timestamp with time zone
            )
            RETURNS TABLE (
                action_intent_id uuid,
                payload_version_id uuid,
                payload_hash text,
                policy_decision_id uuid,
                approval_request_id uuid,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_action_intent_id uuid;
                v_payload_version_id uuid;
                v_policy_decision_id uuid;
                v_approval_request_id uuid;
                v_computed_hash text;
                v_existing careerops.gmail_send_command_receipts%ROWTYPE;
            BEGIN
                IF p_owner_user_id IS NULL
                   OR p_candidate_id IS NULL
                   OR p_resource_id IS NULL
                   OR p_target IS NULL
                   OR p_payload IS NULL
                   OR p_attachment_refs IS NULL
                   OR p_ruleset_version IS NULL
                   OR btrim(p_ruleset_version) = ''
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL
                   OR btrim(p_trace_id) = ''
                   OR p_expires_at IS NULL
                   OR p_expires_at <= v_now THEN
                    RAISE EXCEPTION 'gmail send draft payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF jsonb_typeof(p_target) <> 'object'
                   OR jsonb_typeof(p_payload) <> 'object'
                   OR jsonb_typeof(p_attachment_refs) <> 'array'
                   OR p_target ->> 'target_host' IS DISTINCT FROM 'gmail.googleapis.com'
                   OR p_target ->> 'channel' IS DISTINCT FROM 'gmail:send'
                   OR (p_target ->> 'recipient_sha256') !~ '^[a-f0-9]{64}$'
                   OR (p_target ->> 'body_sha256') !~ '^[a-f0-9]{64}$' THEN
                    RAISE EXCEPTION 'gmail send draft target is not exact'
                        USING ERRCODE = '22023';
                END IF;

                v_computed_hash := encode(
                    sha256(convert_to(jsonb_build_object(
                        'target', p_target,
                        'payload', p_payload,
                        'attachment_refs', p_attachment_refs
                    )::text, 'UTF8')),
                    'hex'
                );
                p_payload_hash := COALESCE(p_payload_hash, v_computed_hash);
                IF v_computed_hash <> p_payload_hash THEN
                    RAISE EXCEPTION 'gmail send draft payload hash mismatch'
                        USING ERRCODE = '23514';
                END IF;

                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'create_draft'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> p_payload_hash THEN
                        RAISE EXCEPTION 'gmail send draft idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY
                    SELECT
                        (v_existing.response_json ->> 'action_intent_id')::uuid,
                        (v_existing.response_json ->> 'payload_version_id')::uuid,
                        v_existing.response_json ->> 'payload_hash',
                        (v_existing.response_json ->> 'policy_decision_id')::uuid,
                        (v_existing.response_json ->> 'approval_request_id')::uuid,
                        'replayed'::text;
                    RETURN;
                END IF;

                v_action_intent_id := gen_random_uuid();
                v_payload_version_id := gen_random_uuid();
                v_policy_decision_id := gen_random_uuid();
                v_approval_request_id := gen_random_uuid();

                INSERT INTO careerops.action_intents (
                    id,
                    action_kind,
                    resource_type,
                    resource_id,
                    idempotency_key,
                    status,
                    current_payload_version_id,
                    created_by
                ) VALUES (
                    v_action_intent_id,
                    'send_email',
                    'candidate',
                    p_resource_id,
                    'gmail-send-draft:' || p_idempotency_key,
                    'awaiting_approval',
                    NULL,
                    p_owner_user_id::text
                );

                INSERT INTO careerops.action_payload_versions (
                    id,
                    action_intent_id,
                    version,
                    target,
                    payload,
                    attachment_refs,
                    payload_hash
                ) VALUES (
                    v_payload_version_id,
                    v_action_intent_id,
                    1,
                    p_target,
                    p_payload,
                    p_attachment_refs,
                    p_payload_hash
                );

                UPDATE careerops.action_intents
                SET current_payload_version_id = v_payload_version_id
                WHERE id = v_action_intent_id;

                INSERT INTO careerops.policy_decisions (
                    id,
                    action_intent_id,
                    payload_version_id,
                    ruleset_version,
                    decision,
                    reason_codes,
                    payload_hash,
                    expires_at
                ) VALUES (
                    v_policy_decision_id,
                    v_action_intent_id,
                    v_payload_version_id,
                    p_ruleset_version,
                    'require_approval',
                    jsonb_build_array('GMAIL_SEND_REQUIRES_EXACT_HUMAN_REVIEW'),
                    p_payload_hash,
                    p_expires_at
                );

                INSERT INTO careerops.approval_requests (
                    id,
                    action_intent_id,
                    payload_version_id,
                    policy_decision_id,
                    requested_for,
                    decision,
                    decision_rule_reference,
                    expires_at
                ) VALUES (
                    v_approval_request_id,
                    v_action_intent_id,
                    v_payload_version_id,
                    v_policy_decision_id,
                    'gmail_send_exact_payload',
                    'pending',
                    p_decision_rule_reference,
                    p_expires_at
                );

                INSERT INTO careerops.gmail_send_command_receipts (
                    id,
                    owner_user_id,
                    action_intent_id,
                    command_kind,
                    idempotency_key,
                    request_sha256,
                    response_json,
                    trace_id
                ) VALUES (
                    gen_random_uuid(),
                    p_owner_user_id,
                    v_action_intent_id,
                    'create_draft',
                    p_idempotency_key,
                    p_payload_hash,
                    jsonb_build_object(
                        'action_intent_id', v_action_intent_id::text,
                        'payload_version_id', v_payload_version_id::text,
                        'payload_hash', p_payload_hash,
                        'policy_decision_id', v_policy_decision_id::text,
                        'approval_request_id', v_approval_request_id::text
                    ),
                    p_trace_id
                );

                RETURN QUERY
                SELECT v_action_intent_id, v_payload_version_id, p_payload_hash, v_policy_decision_id, v_approval_request_id, 'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_CREATE_DRAFT_FUNCTION}{_CREATE_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_review_draft(
                p_owner_user_id uuid,
                p_action_intent_id uuid,
                p_payload_version_id uuid,
                p_approval_request_id uuid,
                p_campaign_id uuid,
                p_grant_version_id uuid,
                p_payload_hash text,
                p_decision text,
                p_reviewed_by_user_id uuid,
                p_authorization_id uuid,
                p_review_snapshot_sha256 text,
                p_idempotency_key text,
                p_authorization_expires_at timestamp with time zone,
                p_trace_id text,
                p_reason text
            )
            RETURNS TABLE (
                approval_request_id uuid,
                policy_decision_id uuid,
                authorization_id uuid,
                decision text,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_existing careerops.gmail_send_command_receipts%ROWTYPE;
                v_request_sha256 text;
                v_payload_target jsonb;
                v_payload_attachment_refs jsonb;
                v_grant careerops.autopilot_grant_versions%ROWTYPE;
                v_policy_decision_id uuid;
            BEGIN
                IF p_owner_user_id IS NULL
                   OR p_action_intent_id IS NULL
                   OR p_payload_version_id IS NULL
                   OR p_approval_request_id IS NULL
                   OR p_campaign_id IS NULL
                   OR p_grant_version_id IS NULL
                   OR p_reviewed_by_user_id IS NULL
                   OR p_authorization_id IS NULL
                   OR p_payload_hash !~ '^[a-f0-9]{64}$'
                   OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_decision NOT IN ('approved', 'rejected')
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL
                   OR btrim(p_trace_id) = ''
                   OR p_reason IS NULL
                   OR btrim(p_reason) = ''
                   OR p_authorization_expires_at IS NULL
                   OR p_authorization_expires_at <= v_now THEN
                    RAISE EXCEPTION 'gmail send review payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF p_reviewed_by_user_id <> p_owner_user_id THEN
                    RAISE EXCEPTION 'gmail send reviewer must match owner'
                        USING ERRCODE = '23514';
                END IF;

                v_request_sha256 := encode(
                    sha256(convert_to(jsonb_build_object(
                        'owner_user_id', p_owner_user_id::text,
                        'action_intent_id', p_action_intent_id::text,
                        'payload_version_id', p_payload_version_id::text,
                        'approval_request_id', p_approval_request_id::text,
                        'campaign_id', p_campaign_id::text,
                        'grant_version_id', p_grant_version_id::text,
                        'payload_hash', p_payload_hash,
                        'decision', p_decision,
                        'reviewed_by_user_id', p_reviewed_by_user_id::text,
                        'authorization_id', p_authorization_id::text,
                        'review_snapshot_sha256', p_review_snapshot_sha256,
                        'authorization_expires_at', p_authorization_expires_at::text,
                        'reason', p_reason
                    )::text, 'UTF8')),
                    'hex'
                );

                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'review_draft'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'gmail send review idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY
                    SELECT
                        p_approval_request_id,
                        (v_existing.response_json ->> 'policy_decision_id')::uuid,
                        NULLIF(v_existing.response_json ->> 'authorization_id', '')::uuid,
                        p_decision,
                        'replayed'::text;
                    RETURN;
                END IF;

                SELECT payload.target, payload.attachment_refs
                INTO v_payload_target, v_payload_attachment_refs
                FROM careerops.action_payload_versions AS payload
                JOIN careerops.action_intents AS intent
                  ON intent.id = payload.action_intent_id
                WHERE payload.action_intent_id = p_action_intent_id
                  AND payload.id = p_payload_version_id
                  AND payload.payload_hash = p_payload_hash
                  AND intent.created_by = p_owner_user_id::text
                  AND intent.action_kind = 'send_email'
                  AND EXISTS (
                      SELECT 1
                      FROM careerops.gmail_send_command_receipts AS create_receipt
                      WHERE create_receipt.owner_user_id = p_owner_user_id
                        AND create_receipt.command_kind = 'create_draft'
                        AND create_receipt.action_intent_id = p_action_intent_id
                        AND create_receipt.response_json ->> 'payload_version_id' = p_payload_version_id::text
                        AND create_receipt.response_json ->> 'payload_hash' = p_payload_hash
                  )
                FOR UPDATE OF intent, payload;
                IF v_payload_target IS NULL THEN
                    RAISE EXCEPTION 'gmail send review target is missing'
                        USING ERRCODE = '23503';
                END IF;
                IF v_payload_target ->> 'target_host' IS DISTINCT FROM 'gmail.googleapis.com'
                   OR v_payload_target ->> 'channel' IS DISTINCT FROM 'gmail:send'
                   OR (v_payload_target ->> 'recipient_sha256') !~ '^[a-f0-9]{64}$'
                   OR (v_payload_target ->> 'body_sha256') !~ '^[a-f0-9]{64}$' THEN
                    RAISE EXCEPTION 'gmail send review target is not exact'
                        USING ERRCODE = '23514';
                END IF;

                SELECT grant_version.*
                INTO v_grant
                FROM careerops.autopilot_grant_versions AS grant_version
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = grant_version.campaign_id
                 AND campaign.owner_user_id = p_owner_user_id
                WHERE grant_version.id = p_grant_version_id
                  AND grant_version.campaign_id = p_campaign_id
                  AND grant_version.subject_actor = p_owner_user_id::text
                FOR KEY SHARE;
                IF v_grant.id IS NULL
                   OR v_grant.expires_at <= v_now
                   OR p_authorization_expires_at > v_grant.expires_at
                   OR NOT COALESCE(v_grant.allowed_action_kinds @> jsonb_build_array('send_email'), false)
                   OR NOT COALESCE(v_grant.allowed_channels @> jsonb_build_array('gmail:send'), false)
                   OR NOT COALESCE(v_grant.allowed_target_hosts @> jsonb_build_array('gmail.googleapis.com'), false)
                   OR NOT COALESCE(
                       v_grant.material_hashes @> jsonb_build_array(v_payload_target ->> 'body_sha256'),
                       false
                   )
                   OR COALESCE(
                       (v_payload_target ->> 'grant_material_hash') !~ '^[a-f0-9]{64}$',
                       true
                   )
                   OR NOT COALESCE(
                       v_grant.material_hashes @> jsonb_build_array(v_payload_target ->> 'grant_material_hash'),
                       false
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements(v_payload_attachment_refs) AS attachment(value)
                       WHERE jsonb_typeof(attachment.value) <> 'object'
                          OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                          OR NOT COALESCE(
                              v_grant.material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'),
                              false
                          )
                   ) THEN
                    RAISE EXCEPTION 'gmail send review exceeds grant scope'
                        USING ERRCODE = '23514';
                END IF;

                v_policy_decision_id := gen_random_uuid();
                INSERT INTO careerops.policy_decisions (
                    id,
                    action_intent_id,
                    payload_version_id,
                    ruleset_version,
                    decision,
                    reason_codes,
                    payload_hash,
                    expires_at
                ) VALUES (
                    v_policy_decision_id,
                    p_action_intent_id,
                    p_payload_version_id,
                    v_grant.policy_ruleset_version,
                    CASE WHEN p_decision = 'approved' THEN 'allow_autopilot_submission' ELSE 'deny' END,
                    jsonb_build_array(CASE WHEN p_decision = 'approved' THEN 'GMAIL_SEND_EXACT_PAYLOAD_APPROVED' ELSE 'GMAIL_SEND_EXACT_PAYLOAD_REJECTED' END),
                    p_payload_hash,
                    p_authorization_expires_at
                );

                UPDATE careerops.approval_requests AS approval
                SET decision = p_decision,
                    decision_rule_reference = p_review_snapshot_sha256,
                    decided_at = v_now,
                    policy_decision_id = v_policy_decision_id
                WHERE approval.id = p_approval_request_id
                  AND approval.action_intent_id = p_action_intent_id
                  AND approval.payload_version_id = p_payload_version_id
                  AND approval.requested_for = 'gmail_send_exact_payload'
                  AND approval.decision = 'pending'
                  AND approval.expires_at > v_now;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send review requires pending exact approval'
                        USING ERRCODE = '23514';
                END IF;

                IF p_decision = 'approved' THEN
                    INSERT INTO careerops.autopilot_intent_authorizations (
                        id,
                        campaign_id,
                        grant_version_id,
                        action_intent_id,
                        payload_version_id,
                        payload_hash,
                        policy_decision_id,
                        authorization_outcome,
                        reason_codes,
                        authorized_at,
                        expires_at
                    ) VALUES (
                        p_authorization_id,
                        p_campaign_id,
                        p_grant_version_id,
                        p_action_intent_id,
                        p_payload_version_id,
                        p_payload_hash,
                        v_policy_decision_id,
                        'allow_autopilot_submission',
                        jsonb_build_array('GMAIL_SEND_EXACT_PAYLOAD_APPROVED'),
                        v_now,
                        p_authorization_expires_at
                    );
                    UPDATE careerops.action_intents
                    SET status = 'eligible',
                        updated_at = v_now
                    WHERE id = p_action_intent_id;
                ELSE
                    UPDATE careerops.action_intents
                    SET status = 'denied',
                        updated_at = v_now
                    WHERE id = p_action_intent_id;
                END IF;

                INSERT INTO careerops.gmail_send_command_receipts (
                    id,
                    owner_user_id,
                    action_intent_id,
                    command_kind,
                    idempotency_key,
                    request_sha256,
                    response_json,
                    trace_id
                ) VALUES (
                    gen_random_uuid(),
                    p_owner_user_id,
                    p_action_intent_id,
                    'review_draft',
                    p_idempotency_key,
                    v_request_sha256,
                    jsonb_build_object(
                        'approval_request_id', p_approval_request_id::text,
                        'policy_decision_id', v_policy_decision_id::text,
                        'authorization_id', CASE WHEN p_decision = 'approved' THEN p_authorization_id::text ELSE '' END,
                        'decision', p_decision
                    ),
                    p_trace_id
                );

                RETURN QUERY
                SELECT p_approval_request_id, v_policy_decision_id,
                       CASE WHEN p_decision = 'approved' THEN p_authorization_id ELSE NULL::uuid END,
                       p_decision, 'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_REVIEW_DRAFT_FUNCTION}{_REVIEW_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )


def _install_register_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_register_account(
                p_owner_user_id uuid,
                p_candidate_id uuid,
                p_account_subject text,
                p_secret_handle text,
                p_credential_store_evidence_sha256 text,
                p_release_evidence_sha256 text,
                p_status text,
                p_idempotency_key text,
                p_trace_id text,
                p_daily_send_limit integer
            )
            RETURNS TABLE (
                account_id uuid,
                status text,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_request_sha256 text;
                v_existing careerops.gmail_send_accounts%ROWTYPE;
                v_credential careerops.oauth_credential_references%ROWTYPE;
                v_account_id uuid;
            BEGIN
                IF p_owner_user_id IS NULL OR p_candidate_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send registration requires owner and candidate'
                        USING ERRCODE = '22004';
                END IF;
                IF p_status NOT IN ('disabled', 'active') THEN
                    RAISE EXCEPTION 'gmail send account status is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF p_account_subject IS NULL
                   OR btrim(p_account_subject) = ''
                   OR p_secret_handle IS NULL
                   OR btrim(p_secret_handle) = ''
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL
                   OR btrim(p_trace_id) = ''
                   OR p_credential_store_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_release_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_daily_send_limit <= 0 THEN
                    RAISE EXCEPTION 'gmail send registration payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                v_request_sha256 := encode(
                    sha256(convert_to(jsonb_build_object(
                        'owner_user_id', p_owner_user_id::text,
                        'candidate_id', p_candidate_id::text,
                        'account_subject', p_account_subject,
                        'secret_handle', p_secret_handle,
                        'credential_store_evidence_sha256', p_credential_store_evidence_sha256,
                        'release_evidence_sha256', p_release_evidence_sha256,
                        'status', p_status,
                        'daily_send_limit', p_daily_send_limit
                    )::text, 'UTF8')),
                    'hex'
                );

                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_accounts AS account
                WHERE account.owner_user_id = p_owner_user_id
                  AND account.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    SELECT *
                    INTO v_credential
                    FROM careerops.oauth_credential_references AS credential
                    WHERE credential.id = v_existing.oauth_credential_reference_id
                    FOR KEY SHARE;
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'gmail send registration idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT v_existing.id, v_existing.status::text, 'replayed'::text;
                    RETURN;
                END IF;

                SELECT *
                INTO v_credential
                FROM careerops.oauth_credential_references AS credential
                WHERE credential.provider = 'gmail_send'
                  AND credential.account_subject = p_account_subject
                  AND credential.secret_handle = p_secret_handle
                FOR UPDATE;

                IF v_credential.id IS NULL THEN
                    INSERT INTO careerops.oauth_credential_references (
                        id,
                        provider,
                        account_subject,
                        secret_handle,
                        granted_scopes,
                        status,
                        issued_at
                    ) VALUES (
                        gen_random_uuid(),
                        'gmail_send',
                        p_account_subject,
                        p_secret_handle,
                        jsonb_build_array('https://www.googleapis.com/auth/gmail.send'),
                        'active',
                        v_now
                    )
                    RETURNING * INTO v_credential;
                END IF;

                IF v_credential.provider <> 'gmail_send'
                   OR v_credential.account_subject <> p_account_subject
                   OR v_credential.status <> 'active'
                   OR v_credential.granted_scopes <> jsonb_build_array('https://www.googleapis.com/auth/gmail.send') THEN
                    RAISE EXCEPTION 'gmail send credential must be active and exact gmail.send scope'
                        USING ERRCODE = '23514';
                END IF;

                v_account_id := gen_random_uuid();
                INSERT INTO careerops.gmail_send_accounts (
                    id,
                    owner_user_id,
                    candidate_id,
                    oauth_credential_reference_id,
                    account_subject,
                    status,
                    daily_send_limit,
                    credential_store_evidence_sha256,
                    release_evidence_sha256,
                    request_sha256,
                    idempotency_key,
                    trace_id
                ) VALUES (
                    v_account_id,
                    p_owner_user_id,
                    p_candidate_id,
                    v_credential.id,
                    p_account_subject,
                    p_status,
                    p_daily_send_limit,
                    p_credential_store_evidence_sha256,
                    p_release_evidence_sha256,
                    v_request_sha256,
                    p_idempotency_key,
                    p_trace_id
                );

                INSERT INTO careerops.gmail_send_command_receipts (
                    id,
                    owner_user_id,
                    account_id,
                    command_kind,
                    idempotency_key,
                    request_sha256,
                    response_json,
                    trace_id
                ) VALUES (
                    gen_random_uuid(),
                    p_owner_user_id,
                    v_account_id,
                    'register_account',
                    p_idempotency_key,
                    v_request_sha256,
                    jsonb_build_object('account_id', v_account_id::text, 'status', p_status),
                    p_trace_id
                );

                PERFORM *
                FROM careerops.append_audit_event(
                    gen_random_uuid(),
                    v_now,
                    'user',
                    p_owner_user_id::text,
                    'gmail_send_account_registered',
                    'gmail_send_account',
                    v_account_id,
                    p_idempotency_key,
                    jsonb_build_object('status', p_status, 'account_subject_sha256', encode(sha256(convert_to(p_account_subject, 'UTF8')), 'hex'))
                );

                RETURN QUERY SELECT v_account_id, p_status, 'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_REGISTER_FUNCTION}{_REGISTER_SIGNATURE} FROM PUBLIC")
    )


def _install_reserve_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_reserve_and_enqueue(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_campaign_id uuid,
                p_grant_version_id uuid,
                p_authorization_id uuid,
                p_action_intent_id uuid,
                p_payload_version_id uuid,
                p_payload_hash text,
                p_recipient_sha256 text,
                p_approval_request_id uuid,
                p_review_evidence_sha256 text,
                p_review_snapshot_sha256 text,
                p_reviewed_by_user_id uuid,
                p_release_qualification_id uuid,
                p_reservation_key text,
                p_reconciliation_key text,
                p_event_key text,
                p_idempotency_key text,
                p_trace_id text,
                p_subject_sha256 text,
                p_body_sha256 text
            )
            RETURNS TABLE (
                account_id uuid,
                reservation_id uuid,
                outbox_event_id uuid,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_account careerops.gmail_send_accounts%ROWTYPE;
                v_request_sha256 text;
                v_existing careerops.gmail_send_command_receipts%ROWTYPE;
                v_payload_target jsonb;
                v_payload jsonb;
                v_payload_attachment_refs jsonb;
                v_approval_decision text;
                v_authorization_expires_at timestamp with time zone;
                v_authorization_policy_decision_id uuid;
                v_release_evidence_hash text;
                v_release_evidence_expires_at timestamp with time zone;
                v_reservation_id uuid;
                v_outbox_event_id uuid;
                v_latest_kill_active boolean;
            BEGIN
                IF p_owner_user_id IS NULL
                   OR p_account_id IS NULL
                   OR p_campaign_id IS NULL
                   OR p_grant_version_id IS NULL
                   OR p_authorization_id IS NULL
                   OR p_action_intent_id IS NULL
                   OR p_payload_version_id IS NULL
                   OR p_approval_request_id IS NULL
                   OR p_reviewed_by_user_id IS NULL
                   OR p_release_qualification_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reservation requires all identity ids'
                        USING ERRCODE = '22004';
                END IF;
                IF p_reviewed_by_user_id <> p_owner_user_id THEN
                    RAISE EXCEPTION 'gmail send reviewer must match owner'
                        USING ERRCODE = '23514';
                END IF;
                IF p_payload_hash !~ '^[a-f0-9]{64}$'
                   OR p_recipient_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_review_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_subject_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_body_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_reservation_key IS NULL
                   OR btrim(p_reservation_key) = ''
                   OR p_reconciliation_key IS NULL
                   OR btrim(p_reconciliation_key) = ''
                   OR p_event_key IS NULL
                   OR p_event_key <> ('gmail-send:' || p_reservation_key)
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL
                   OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'gmail send reservation payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                v_request_sha256 := encode(
                    sha256(convert_to(jsonb_build_object(
                        'owner_user_id', p_owner_user_id::text,
                        'account_id', p_account_id::text,
                        'campaign_id', p_campaign_id::text,
                        'grant_version_id', p_grant_version_id::text,
                        'authorization_id', p_authorization_id::text,
                        'action_intent_id', p_action_intent_id::text,
                        'payload_version_id', p_payload_version_id::text,
                        'payload_hash', p_payload_hash,
                        'recipient_sha256', p_recipient_sha256,
                        'approval_request_id', p_approval_request_id::text,
                        'review_evidence_sha256', p_review_evidence_sha256,
                        'review_snapshot_sha256', p_review_snapshot_sha256,
                        'reviewed_by_user_id', p_reviewed_by_user_id::text,
                        'release_qualification_id', p_release_qualification_id::text,
                        'reservation_key', p_reservation_key,
                        'reconciliation_key', p_reconciliation_key,
                        'event_key', p_event_key,
                        'subject_sha256', p_subject_sha256,
                        'body_sha256', p_body_sha256
                    )::text, 'UTF8')),
                    'hex'
                );

                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'reserve_and_enqueue'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'gmail send reserve idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY
                    SELECT
                        p_account_id,
                        (v_existing.response_json ->> 'reservation_id')::uuid,
                        (v_existing.response_json ->> 'outbox_event_id')::uuid,
                        'replayed'::text;
                    RETURN;
                END IF;

                SELECT *
                INTO v_account
                FROM careerops.gmail_send_accounts AS account
                WHERE account.id = p_account_id
                  AND account.owner_user_id = p_owner_user_id
                FOR UPDATE;
                IF v_account.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send account % is missing for owner', p_account_id
                        USING ERRCODE = '23503';
                END IF;
                IF v_account.id IS NULL OR v_account.status <> 'active' THEN
                    RAISE EXCEPTION 'gmail send account must be active before enqueue'
                        USING ERRCODE = '23514';
                END IF;

                PERFORM 1
                FROM careerops.oauth_credential_references AS credential
                WHERE credential.id = v_account.oauth_credential_reference_id
                  AND credential.provider = 'gmail_send'
                  AND credential.account_subject = v_account.account_subject
                  AND credential.status = 'active'
                  AND credential.granted_scopes = jsonb_build_array('https://www.googleapis.com/auth/gmail.send')
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send credential is not active exact gmail.send'
                        USING ERRCODE = '23514';
                END IF;

                SELECT payload.target, payload.payload, payload.attachment_refs
                INTO v_payload_target, v_payload, v_payload_attachment_refs
                FROM careerops.action_payload_versions AS payload
                JOIN careerops.action_intents AS intent
                  ON intent.id = payload.action_intent_id
                WHERE payload.action_intent_id = p_action_intent_id
                  AND payload.id = p_payload_version_id
                  AND payload.payload_hash = p_payload_hash
                  AND intent.created_by = p_owner_user_id::text
                  AND intent.action_kind = 'send_email'
                  AND EXISTS (
                      SELECT 1
                      FROM careerops.gmail_send_command_receipts AS create_receipt
                      WHERE create_receipt.owner_user_id = p_owner_user_id
                        AND create_receipt.command_kind = 'create_draft'
                        AND create_receipt.action_intent_id = p_action_intent_id
                        AND create_receipt.response_json ->> 'payload_version_id' = p_payload_version_id::text
                        AND create_receipt.response_json ->> 'payload_hash' = p_payload_hash
                  )
                FOR KEY SHARE OF payload, intent;
                IF v_payload_target IS NULL THEN
                    RAISE EXCEPTION 'gmail send payload is missing or wrong action kind'
                        USING ERRCODE = '23503';
                END IF;
                IF v_payload_target ->> 'recipient_sha256' IS DISTINCT FROM p_recipient_sha256
                   OR v_payload_target ->> 'target_host' IS DISTINCT FROM 'gmail.googleapis.com'
                   OR v_payload_target ->> 'channel' IS DISTINCT FROM 'gmail:send'
                   OR v_payload_target ->> 'body_sha256' IS DISTINCT FROM p_body_sha256
                   OR jsonb_typeof(v_payload -> 'recipient') IS DISTINCT FROM 'string'
                   OR encode(
                       sha256(convert_to(lower(btrim(v_payload ->> 'recipient')), 'UTF8')),
                       'hex'
                   ) IS DISTINCT FROM p_recipient_sha256
                   OR jsonb_typeof(v_payload -> 'subject') IS DISTINCT FROM 'string'
                   OR encode(
                       sha256(convert_to(v_payload ->> 'subject', 'UTF8')),
                       'hex'
                   ) IS DISTINCT FROM p_subject_sha256
                   OR jsonb_typeof(v_payload -> 'text_body') IS DISTINCT FROM 'string'
                   OR encode(
                       sha256(convert_to(v_payload ->> 'text_body', 'UTF8')),
                       'hex'
                   ) IS DISTINCT FROM p_body_sha256
                   OR v_payload ->> 'body_sha256' IS DISTINCT FROM p_body_sha256 THEN
                    RAISE EXCEPTION 'gmail send payload target is not exact'
                        USING ERRCODE = '23514';
                END IF;

                SELECT authz.expires_at, authz.policy_decision_id
                INTO v_authorization_expires_at, v_authorization_policy_decision_id
                FROM careerops.autopilot_intent_authorizations AS authz
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = authz.campaign_id
                 AND campaign.owner_user_id = p_owner_user_id
                JOIN careerops.autopilot_grant_versions AS grant_version
                  ON grant_version.campaign_id = authz.campaign_id
                 AND grant_version.id = authz.grant_version_id
                 AND grant_version.subject_actor = p_owner_user_id::text
                WHERE authz.id = p_authorization_id
                  AND authz.campaign_id = p_campaign_id
                  AND authz.grant_version_id = p_grant_version_id
                  AND authz.action_intent_id = p_action_intent_id
                  AND authz.payload_version_id = p_payload_version_id
                  AND authz.payload_hash = p_payload_hash
                  AND authz.authorization_outcome = 'allow_autopilot_submission'
                  AND authz.expires_at > v_now
                FOR KEY SHARE;
                IF v_authorization_expires_at IS NULL THEN
                    RAISE EXCEPTION 'gmail send requires current bounded authorization'
                        USING ERRCODE = '23514';
                END IF;

                SELECT approval.decision
                INTO v_approval_decision
                FROM careerops.approval_requests AS approval
                WHERE approval.id = p_approval_request_id
                  AND approval.action_intent_id = p_action_intent_id
                  AND approval.payload_version_id = p_payload_version_id
                  AND approval.policy_decision_id = v_authorization_policy_decision_id
                  AND approval.requested_for = 'gmail_send_exact_payload'
                  AND approval.decision = 'approved'
                  AND approval.decision_rule_reference = p_review_snapshot_sha256
                  AND approval.expires_at > v_now
                FOR KEY SHARE;
                IF v_approval_decision <> 'approved' THEN
                    RAISE EXCEPTION 'gmail send requires a current exact approved review'
                        USING ERRCODE = '23514';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:'
                        || p_release_qualification_id::text,
                        0
                    )
                );
                SELECT decision.evidence_sha256,
                       LEAST(qualification.expires_at, v_authorization_expires_at)
                INTO v_release_evidence_hash, v_release_evidence_expires_at
                FROM careerops.release_qualifications AS qualification
                JOIN LATERAL (
                    SELECT release_decision.to_status, release_decision.evidence_sha256
                    FROM careerops.release_qualification_decisions AS release_decision
                    WHERE release_decision.qualification_id = qualification.id
                    ORDER BY release_decision.sequence DESC
                    LIMIT 1
                ) AS decision ON true
                WHERE qualification.id = p_release_qualification_id
                  AND qualification.requested_by_user_id = p_owner_user_id
                  AND qualification.capability = 'gmail_send'
                  AND qualification.action_name = 'send_email'
                  AND qualification.rollout_mode = 'review_required'
                  AND qualification.provider = 'gmail'
                  AND qualification.adapter_id = 'gmail'
                  AND qualification.adapter_version = 'gmail-send.v1'
                  AND qualification.migration_revision = '0013'
                  AND qualification.oauth_scope_hash = encode(
                      sha256(convert_to('https://www.googleapis.com/auth/gmail.send', 'UTF8')),
                      'hex'
                  )
                  AND qualification.credential_ref_hash = v_account.credential_store_evidence_sha256
                  AND qualification.expires_at > v_now
                  AND decision.to_status = 'qualified'
                  AND decision.evidence_sha256 ~ '^[a-f0-9]{64}$'
                FOR KEY SHARE OF qualification;
                IF v_release_evidence_hash IS NULL
                   OR v_release_evidence_expires_at <= v_now THEN
                    RAISE EXCEPTION 'gmail send requires a qualified current release'
                        USING ERRCODE = '23514';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(pg_catalog.hashtextextended('careerops:autopilot-kill-switch:provider:gmail', 0));
                SELECT active
                INTO v_latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'provider'
                  AND provider = 'gmail'
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(v_latest_kill_active, false) THEN
                    RAISE EXCEPTION 'gmail provider kill switch is active'
                        USING ERRCODE = '55000';
                END IF;

                v_reservation_id := gen_random_uuid();
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
                    release_evidence_expires_at
                )
                SELECT
                    v_reservation_id,
                    p_campaign_id,
                    p_grant_version_id,
                    p_authorization_id,
                    p_action_intent_id,
                    p_payload_version_id,
                    p_payload_hash,
                    'gmail.googleapis.com',
                    'gmail:send',
                    grant_version.release_version,
                    p_recipient_sha256,
                    'gmail',
                    'gmail-send.v1',
                    p_reservation_key,
                    p_reconciliation_key,
                    v_release_evidence_hash,
                    v_release_evidence_expires_at
                FROM careerops.autopilot_grant_versions AS grant_version
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = grant_version.campaign_id
                 AND campaign.owner_user_id = p_owner_user_id
                WHERE grant_version.id = p_grant_version_id
                  AND grant_version.campaign_id = p_campaign_id
                  AND grant_version.subject_actor = p_owner_user_id::text;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send grant is not owned by reservation owner'
                        USING ERRCODE = '23514';
                END IF;

                INSERT INTO careerops.gmail_send_review_evidence (
                    id,
                    owner_user_id,
                    account_id,
                    release_qualification_id,
                    action_intent_id,
                    payload_version_id,
                    payload_hash,
                    approval_request_id,
                    reviewed_by_user_id,
                    recipient_sha256,
                    subject_sha256,
                    body_sha256,
                    attachment_sha256s,
                    review_evidence_sha256,
                    review_snapshot_sha256,
                    expires_at,
                    idempotency_key,
                    trace_id
                ) VALUES (
                    gen_random_uuid(),
                    p_owner_user_id,
                    p_account_id,
                    p_release_qualification_id,
                    p_action_intent_id,
                    p_payload_version_id,
                    p_payload_hash,
                    p_approval_request_id,
                    p_reviewed_by_user_id,
                    p_recipient_sha256,
                    p_subject_sha256,
                    p_body_sha256,
                    COALESCE(
                        (
                            SELECT jsonb_agg(attachment.value ->> 'sha256' ORDER BY attachment.value ->> 'sha256')
                            FROM jsonb_array_elements(v_payload_attachment_refs) AS attachment(value)
                        ),
                        '[]'::jsonb
                    ),
                    p_review_evidence_sha256,
                    p_review_snapshot_sha256,
                    v_release_evidence_expires_at,
                    p_idempotency_key,
                    p_trace_id
                );

                v_outbox_event_id := gen_random_uuid();
                INSERT INTO careerops.outbox_events (
                    id,
                    event_key,
                    action_intent_id,
                    payload_version_id,
                    event_type,
                    available_at
                ) VALUES (
                    v_outbox_event_id,
                    p_event_key,
                    p_action_intent_id,
                    p_payload_version_id,
                    'workflow_signal',
                    v_now
                );

                INSERT INTO careerops.gmail_send_command_receipts (
                    id,
                    owner_user_id,
                    account_id,
                    action_intent_id,
                    outbox_event_id,
                    command_kind,
                    idempotency_key,
                    request_sha256,
                    response_json,
                    trace_id
                ) VALUES (
                    gen_random_uuid(),
                    p_owner_user_id,
                    p_account_id,
                    p_action_intent_id,
                    v_outbox_event_id,
                    'reserve_and_enqueue',
                    p_idempotency_key,
                    v_request_sha256,
                    jsonb_build_object(
                        'reservation_id', v_reservation_id::text,
                        'outbox_event_id', v_outbox_event_id::text,
                        'reconciliation_key', p_reconciliation_key
                    ),
                    p_trace_id
                );

                PERFORM *
                FROM careerops.append_audit_event(
                    gen_random_uuid(),
                    v_now,
                    'user',
                    p_owner_user_id::text,
                    'gmail_send_reserved_and_enqueued',
                    'action_intent',
                    p_action_intent_id,
                    p_reconciliation_key,
                    jsonb_build_object(
                        'account_id', p_account_id::text,
                        'reservation_id', v_reservation_id::text,
                        'outbox_event_id', v_outbox_event_id::text,
                        'recipient_sha256', p_recipient_sha256
                    )
                );

                RETURN QUERY SELECT p_account_id, v_reservation_id, v_outbox_event_id, 'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_RESERVE_FUNCTION}{_RESERVE_SIGNATURE} FROM PUBLIC")
    )


def _install_outbox_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_prepare_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid
            )
            RETURNS TABLE (
                event_id uuid,
                event_key text,
                account_id uuid,
                credential_reference_id uuid,
                account_subject text,
                action_intent_id uuid,
                payload_version_id uuid,
                reservation_key text,
                reconciliation_key text,
                prepare_state text,
                reason_code text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_event careerops.outbox_events%ROWTYPE;
                v_reservation careerops.autopilot_cap_reservations%ROWTYPE;
                v_evidence careerops.gmail_send_review_evidence%ROWTYPE;
                v_account careerops.gmail_send_accounts%ROWTYPE;
                v_release_evidence_hash text;
                v_attempt_id uuid;
                v_existing_attempt careerops.side_effect_attempts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                    RAISE EXCEPTION 'gmail send prepare requires event and lease token'
                        USING ERRCODE = '22004';
                END IF;
                IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
                    RAISE EXCEPTION 'gmail send lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;
                IF v_event.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send event % is missing', p_event_id
                        USING ERRCODE = '23503';
                END IF;
                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now THEN
                    RAISE EXCEPTION 'gmail send lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                IF v_event.event_type <> 'workflow_signal'
                   OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                    RAISE EXCEPTION 'gmail send event has invalid type or key'
                        USING ERRCODE = '23514';
                END IF;

                SELECT *
                INTO v_reservation
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.reservation_key = substring(v_event.event_key FROM char_length('gmail-send:') + 1)
                  AND reservation.channel = 'gmail:send'
                  AND reservation.target_host = 'gmail.googleapis.com'
                FOR UPDATE;
                IF v_reservation.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send event is not bound to a reservation'
                        USING ERRCODE = '23503';
                END IF;
                IF v_reservation.release_evidence_expires_at <= v_now THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_RELEASE_EVIDENCE_EXPIRED'::text;
                    RETURN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.autopilot_intent_authorizations AS authz
                    WHERE authz.id = v_reservation.authorization_id
                      AND authz.campaign_id = v_reservation.campaign_id
                      AND authz.grant_version_id = v_reservation.grant_version_id
                      AND authz.action_intent_id = v_event.action_intent_id
                      AND authz.payload_version_id = v_event.payload_version_id
                      AND authz.payload_hash = v_reservation.payload_hash
                      AND authz.authorization_outcome = 'allow_autopilot_submission'
                      AND authz.expires_at > v_now
                ) THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_AUTHORIZATION_EXPIRED'::text;
                    RETURN;
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-grant-state:'
                        || v_reservation.grant_version_id::text,
                        0
                    )
                );
                PERFORM 1
                FROM careerops.autopilot_grant_versions AS grant_version
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = grant_version.campaign_id
                WHERE grant_version.id = v_reservation.grant_version_id
                  AND grant_version.campaign_id = v_reservation.campaign_id
                  AND grant_version.subject_actor = campaign.owner_user_id::text
                  AND grant_version.expires_at > v_now
                  AND NOT EXISTS (
                      SELECT 1
                      FROM careerops.autopilot_grant_revocations AS revocation
                      WHERE revocation.grant_version_id = grant_version.id
                  )
                FOR KEY SHARE OF grant_version, campaign;
                IF NOT FOUND THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_GRANT_NOT_ACTIVE'::text;
                    RETURN;
                END IF;

                SELECT evidence.*
                INTO v_evidence
                FROM careerops.gmail_send_review_evidence AS evidence
                JOIN careerops.gmail_send_command_receipts AS reserve_receipt
                  ON reserve_receipt.outbox_event_id = v_event.id
                 AND reserve_receipt.command_kind = 'reserve_and_enqueue'
                 AND reserve_receipt.action_intent_id = v_event.action_intent_id
                 AND reserve_receipt.account_id = evidence.account_id
                 AND reserve_receipt.owner_user_id = evidence.owner_user_id
                 AND reserve_receipt.response_json ->> 'reservation_id' = v_reservation.id::text
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = v_reservation.campaign_id
                 AND campaign.owner_user_id = reserve_receipt.owner_user_id
                JOIN careerops.autopilot_grant_versions AS grant_version
                  ON grant_version.id = v_reservation.grant_version_id
                 AND grant_version.campaign_id = campaign.id
                 AND grant_version.subject_actor = reserve_receipt.owner_user_id::text
                JOIN careerops.autopilot_intent_authorizations AS authz
                  ON authz.id = v_reservation.authorization_id
                 AND authz.campaign_id = campaign.id
                 AND authz.grant_version_id = grant_version.id
                 AND authz.action_intent_id = v_event.action_intent_id
                 AND authz.payload_version_id = v_event.payload_version_id
                 AND authz.payload_hash = v_reservation.payload_hash
                 AND authz.authorization_outcome = 'allow_autopilot_submission'
                 AND authz.expires_at > v_now
                JOIN careerops.approval_requests AS approval
                  ON approval.id = evidence.approval_request_id
                 AND approval.action_intent_id = v_event.action_intent_id
                 AND approval.payload_version_id = v_event.payload_version_id
                 AND approval.policy_decision_id = authz.policy_decision_id
                 AND approval.requested_for = 'gmail_send_exact_payload'
                 AND approval.decision = 'approved'
                 AND approval.decision_rule_reference = evidence.review_snapshot_sha256
                 AND approval.expires_at > v_now
                JOIN careerops.action_intents AS intent
                  ON intent.id = v_event.action_intent_id
                 AND intent.created_by = reserve_receipt.owner_user_id::text
                 AND intent.action_kind = 'send_email'
                WHERE evidence.action_intent_id = v_event.action_intent_id
                  AND evidence.payload_version_id = v_event.payload_version_id
                  AND evidence.payload_hash = v_reservation.payload_hash
                  AND evidence.recipient_sha256 = v_reservation.company_key
                  AND evidence.reviewed_by_user_id = reserve_receipt.owner_user_id
                  AND evidence.expires_at > v_now
                  AND EXISTS (
                      SELECT 1
                      FROM careerops.gmail_send_command_receipts AS create_receipt
                      WHERE create_receipt.owner_user_id = reserve_receipt.owner_user_id
                        AND create_receipt.command_kind = 'create_draft'
                        AND create_receipt.action_intent_id = v_event.action_intent_id
                        AND create_receipt.response_json ->> 'payload_version_id' = v_event.payload_version_id::text
                        AND create_receipt.response_json ->> 'payload_hash' = v_reservation.payload_hash
                  )
                ORDER BY evidence.created_at DESC
                LIMIT 1;
                IF v_evidence.id IS NULL THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_REVIEW_BINDING_INVALID'::text;
                    RETURN;
                END IF;

                SELECT account.*
                INTO v_account
                FROM careerops.gmail_send_accounts AS account
                WHERE account.id = v_evidence.account_id
                  AND account.owner_user_id = v_evidence.owner_user_id
                FOR UPDATE;
                IF v_account.status <> 'active' THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_ACCOUNT_NOT_ACTIVE'::text;
                    RETURN;
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.oauth_credential_references AS credential
                    WHERE credential.id = v_account.oauth_credential_reference_id
                      AND credential.provider = 'gmail_send'
                      AND credential.account_subject = v_account.account_subject
                      AND credential.status = 'active'
                      AND credential.granted_scopes = jsonb_build_array('https://www.googleapis.com/auth/gmail.send')
                ) THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_CREDENTIAL_NOT_ACTIVE'::text;
                    RETURN;
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.gmail_accounts AS readonly_account
                    JOIN careerops.oauth_credential_references AS readonly_credential
                      ON readonly_credential.id = readonly_account.oauth_credential_reference_id
                    WHERE readonly_account.id = v_account.reconciliation_gmail_account_id
                      AND readonly_account.owner_user_id = v_evidence.owner_user_id
                      AND readonly_account.account_subject = v_account.account_subject
                      AND readonly_account.status = 'active'
                      AND readonly_credential.provider = 'gmail'
                      AND readonly_credential.account_subject = readonly_account.account_subject
                      AND readonly_credential.status = 'active'
                      AND readonly_credential.granted_scopes = jsonb_build_array(
                          'https://www.googleapis.com/auth/gmail.readonly'
                      )
                ) THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_READONLY_CREDENTIAL_NOT_ACTIVE'::text;
                    RETURN;
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:'
                        || v_evidence.release_qualification_id::text,
                        0
                    )
                );
                SELECT decision.evidence_sha256
                INTO v_release_evidence_hash
                FROM careerops.release_qualifications AS qualification
                JOIN LATERAL (
                    SELECT release_decision.to_status, release_decision.evidence_sha256
                    FROM careerops.release_qualification_decisions AS release_decision
                    WHERE release_decision.qualification_id = qualification.id
                    ORDER BY release_decision.sequence DESC
                    LIMIT 1
                ) AS decision ON true
                WHERE qualification.id = v_evidence.release_qualification_id
                  AND qualification.requested_by_user_id = v_evidence.owner_user_id
                  AND qualification.capability = 'gmail_send'
                  AND qualification.action_name = 'send_email'
                  AND qualification.rollout_mode = 'review_required'
                  AND qualification.provider = 'gmail'
                  AND qualification.adapter_id = 'gmail'
                  AND qualification.adapter_version = 'gmail-send.v1'
                  AND qualification.migration_revision = '0013'
                  AND qualification.oauth_scope_hash = encode(
                      sha256(convert_to('https://www.googleapis.com/auth/gmail.send', 'UTF8')),
                      'hex'
                  )
                  AND qualification.credential_ref_hash = v_account.credential_store_evidence_sha256
                  AND qualification.expires_at > v_now
                  AND qualification.expires_at >= v_reservation.release_evidence_expires_at
                  AND decision.to_status = 'qualified'
                  AND decision.evidence_sha256 = v_reservation.release_evidence_hash
                FOR KEY SHARE OF qualification;
                IF v_release_evidence_hash IS NULL THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_RELEASE_NOT_QUALIFIED'::text;
                    RETURN;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM careerops.provider_receipts AS receipt
                    WHERE receipt.provider = 'gmail'
                      AND receipt.reconciliation_key = v_reservation.reconciliation_key
                ) THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, v_account.id, v_account.oauth_credential_reference_id, v_account.account_subject,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'already_confirmed'::text, NULL::text;
                    RETURN;
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended('careerops:autopilot-kill-switch:global', 0)
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:campaign:' || v_reservation.campaign_id::text,
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended('careerops:autopilot-kill-switch:provider:gmail', 0)
                );
                IF EXISTS (
                    SELECT 1
                    FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'global'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest
                    WHERE latest.active
                ) OR EXISTS (
                    SELECT 1
                    FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'campaign'
                          AND kill.campaign_id = v_reservation.campaign_id
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest
                    WHERE latest.active
                ) OR EXISTS (
                    SELECT 1
                    FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'provider'
                          AND kill.provider = 'gmail'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest
                    WHERE latest.active
                ) THEN
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::uuid, NULL::text,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'stopped'::text, 'GMAIL_SEND_KILL_SWITCH_ACTIVE'::text;
                    RETURN;
                END IF;

                SELECT *
                INTO v_existing_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;
                IF v_existing_attempt.id IS NOT NULL
                   AND v_existing_attempt.state <> 'failed' THEN
                    UPDATE careerops.side_effect_attempts
                    SET state = 'reconciliation_required',
                        finished_at = v_now,
                        error_code = 'GMAIL_SEND_REPLAY_RECONCILIATION_REQUIRED',
                        response_metadata = response_metadata || jsonb_build_object('reason', 'leased event replayed before receipt')
                    WHERE id = v_existing_attempt.id;
                    UPDATE careerops.action_intents
                    SET status = 'reconciliation_required',
                        updated_at = v_now
                    WHERE id = v_event.action_intent_id;
                    RETURN QUERY
                    SELECT v_event.id, v_event.event_key, v_account.id, v_account.oauth_credential_reference_id, v_account.account_subject,
                           v_event.action_intent_id, v_event.payload_version_id,
                           v_reservation.reservation_key, v_reservation.reconciliation_key,
                           'reconciliation_required'::text, 'GMAIL_SEND_REPLAY_RECONCILIATION_REQUIRED'::text;
                    RETURN;
                END IF;

                v_attempt_id := gen_random_uuid();
                INSERT INTO careerops.side_effect_attempts (
                    id,
                    action_intent_id,
                    outbox_event_id,
                    ordinal,
                    state,
                    request_fingerprint,
                    started_at,
                    response_metadata
                ) VALUES (
                    v_attempt_id,
                    v_event.action_intent_id,
                    v_event.id,
                    COALESCE(v_existing_attempt.ordinal, 0) + 1,
                    'processing',
                    encode(sha256(convert_to(jsonb_build_object(
                        'event_id', v_event.id::text,
                        'event_key', v_event.event_key,
                        'payload_hash', v_reservation.payload_hash,
                        'reconciliation_key', v_reservation.reconciliation_key,
                        'account_id', v_account.id::text
                    )::text, 'UTF8')), 'hex'),
                    v_now,
                    jsonb_build_object(
                        'provider', 'gmail',
                        'account_id', v_account.id::text,
                        'credential_reference_id', v_account.oauth_credential_reference_id::text,
                        'release_evidence_hash', v_reservation.release_evidence_hash
                    )
                );
                UPDATE careerops.action_intents
                SET status = 'processing',
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;

                RETURN QUERY
                SELECT v_event.id, v_event.event_key, v_account.id, v_account.oauth_credential_reference_id, v_account.account_subject,
                       v_event.action_intent_id, v_event.payload_version_id,
                       v_reservation.reservation_key, v_reservation.reconciliation_key,
                       'ready'::text, NULL::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE} FROM PUBLIC")
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_record_receipt(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_provider_message_id text,
                p_reconciliation_key text,
                p_provider_timestamp timestamp with time zone,
                p_receipt_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_event careerops.outbox_events%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL OR p_receipt_id IS NULL THEN
                    RAISE EXCEPTION 'gmail receipt requires event, lease, and receipt ids'
                        USING ERRCODE = '22004';
                END IF;
                IF p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_provider_message_id IS NULL
                   OR p_provider_message_id !~ '^[A-Za-z0-9._:@/-]{1,200}$'
                   OR p_reconciliation_key IS NULL
                   OR p_reconciliation_key !~ '^[A-Za-z0-9._:/-]{1,200}$'
                   OR p_provider_timestamp IS NULL THEN
                    RAISE EXCEPTION 'gmail receipt payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT * INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;
                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now
                   OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                    RAISE EXCEPTION 'gmail receipt lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                SELECT * INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state = 'processing'
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'gmail receipt requires one processing attempt'
                        USING ERRCODE = '23514';
                END IF;

                INSERT INTO careerops.provider_receipts (
                    id,
                    side_effect_attempt_id,
                    provider,
                    provider_resource_id,
                    reconciliation_key,
                    final_state,
                    provider_timestamp,
                    received_at,
                    receipt_metadata
                ) VALUES (
                    p_receipt_id,
                    v_attempt.id,
                    'gmail',
                    p_provider_message_id,
                    p_reconciliation_key,
                    'confirmed',
                    p_provider_timestamp,
                    v_now,
                    jsonb_build_object('provider_state', 'sent')
                );

                UPDATE careerops.side_effect_attempts
                SET state = 'confirmed',
                    finished_at = v_now,
                    error_code = NULL,
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'gmail',
                        'provider_message_id', p_provider_message_id,
                        'receipt_id', p_receipt_id::text
                    )
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'confirmed',
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;
            END
            $function$
            """
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_RECORD_FUNCTION}{_RECORD_SIGNATURE} FROM PUBLIC"))

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_record_ambiguity(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_error_code text
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_event careerops.outbox_events%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL
                   OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_error_code IS NULL
                   OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                    RAISE EXCEPTION 'gmail ambiguity payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                SELECT * INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;
                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now
                   OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                    RAISE EXCEPTION 'gmail ambiguity lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                SELECT * INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'gmail ambiguity requires an attempt'
                        USING ERRCODE = '23514';
                END IF;
                UPDATE careerops.side_effect_attempts
                SET state = 'reconciliation_required',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object('provider', 'gmail', 'error_code', p_error_code)
                WHERE id = v_attempt.id;
                PERFORM set_config('careerops.outbox_lease_owner', v_event.lease_owner, true);
                PERFORM set_config('careerops.outbox_lease_token', v_event.lease_token::text, true);
                UPDATE careerops.outbox_events
                SET status = 'failed',
                    available_at = v_now,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = p_error_code
                WHERE id = v_event.id;
                UPDATE careerops.action_intents
                SET status = 'reconciliation_required',
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_AMBIGUOUS_FUNCTION}{_AMBIGUOUS_SIGNATURE} FROM PUBLIC")
    )


def _install_read_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_reconcile_receipt(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_reconciliation_key text,
                p_provider_message_id text,
                p_receipt_id uuid,
                p_trace_id text
            )
            RETURNS TABLE (
                reconciliation_key text,
                provider_message_id text,
                final_state text,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_account_id uuid;
                v_receipt careerops.provider_receipts%ROWTYPE;
            BEGIN
                SELECT id INTO v_account_id
                FROM careerops.gmail_send_accounts
                WHERE id = p_account_id
                  AND owner_user_id = p_owner_user_id
                FOR KEY SHARE;
                IF v_account_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send account is missing for reconcile'
                        USING ERRCODE = '23503';
                END IF;
                SELECT receipt.* INTO v_receipt
                FROM careerops.provider_receipts AS receipt
                WHERE receipt.provider = 'gmail'
                  AND receipt.reconciliation_key = p_reconciliation_key
                ORDER BY receipt.received_at DESC
                LIMIT 1;
                IF v_receipt.id IS NULL THEN
                    RETURN QUERY SELECT p_reconciliation_key, p_provider_message_id, 'missing'::text, 'not_found'::text;
                    RETURN;
                END IF;
                RETURN QUERY SELECT p_reconciliation_key, v_receipt.provider_resource_id, v_receipt.final_state::text, 'found'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_RECONCILE_FUNCTION}{_RECONCILE_SIGNATURE} FROM PUBLIC")
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_status(
                p_owner_user_id uuid,
                p_account_id uuid
            )
            RETURNS TABLE (
                account_id uuid,
                account_subject text,
                status text,
                daily_send_limit integer,
                credential_status text,
                reconciliation_gmail_account_id uuid
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT
                    account.id,
                    account.account_subject,
                    account.status::text,
                    account.daily_send_limit,
                    credential.status::text,
                    account.reconciliation_gmail_account_id
                FROM careerops.gmail_send_accounts AS account
                JOIN careerops.oauth_credential_references AS credential
                  ON credential.id = account.oauth_credential_reference_id
                WHERE account.owner_user_id = p_owner_user_id
                  AND account.id = p_account_id
            $function$
            """
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_STATUS_FUNCTION}{_STATUS_SIGNATURE} FROM PUBLIC"))

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_list_accounts(
                p_owner_user_id uuid,
                p_limit integer
            )
            RETURNS TABLE (
                account_id uuid,
                account_subject text,
                status text,
                daily_send_limit integer,
                reconciliation_gmail_account_id uuid,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT
                    account.id,
                    account.account_subject,
                    account.status::text,
                    account.daily_send_limit,
                    account.reconciliation_gmail_account_id,
                    account.updated_at
                FROM careerops.gmail_send_accounts AS account
                WHERE account.owner_user_id = p_owner_user_id
                ORDER BY account.updated_at DESC, account.id
                LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
            $function$
            """
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_LIST_FUNCTION}{_LIST_SIGNATURE} FROM PUBLIC"))


def _install_operator_api_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_register_account(
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
                UPDATE careerops.gmail_send_accounts
                SET reconciliation_gmail_account_id = p_reconciliation_gmail_account_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = v_account_id;
                RETURN v_account_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_REGISTER_FUNCTION}{_REGISTER_ACCOUNT_WITH_READONLY_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_registration_receipt(
                p_owner_user_id uuid,
                p_idempotency_key text
            )
            RETURNS TABLE (
                account_id uuid,
                receipt_state text
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT
                    (receipt.response_json ->> 'account_id')::uuid,
                    CASE
                        WHEN account.created_at = receipt.created_at THEN 'created'
                        ELSE 'replayed'
                    END
                FROM careerops.gmail_send_command_receipts AS receipt
                JOIN careerops.gmail_send_accounts AS account
                  ON account.id = (receipt.response_json ->> 'account_id')::uuid
                 AND account.owner_user_id = receipt.owner_user_id
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'register_account'
                  AND receipt.idempotency_key = p_idempotency_key
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_REGISTRATION_RECEIPT_FUNCTION}{_REGISTRATION_RECEIPT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_account_status(
                p_owner_user_id uuid,
                p_account_id uuid
            )
            RETURNS TABLE (
                account_id uuid,
                account_subject text,
                status text,
                daily_send_limit integer,
                credential_status text,
                reconciliation_gmail_account_id uuid,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT
                    account.id,
                    account.account_subject,
                    account.status::text,
                    account.daily_send_limit,
                    credential.status::text,
                    account.reconciliation_gmail_account_id,
                    account.updated_at
                FROM careerops.gmail_send_accounts AS account
                JOIN careerops.oauth_credential_references AS credential
                  ON credential.id = account.oauth_credential_reference_id
                WHERE account.owner_user_id = p_owner_user_id
                  AND account.id = p_account_id
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_ACCOUNT_STATUS_FUNCTION}{_ACCOUNT_STATUS_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_create_exact_payload_draft(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_reviewed_intent_id uuid,
                p_application_id uuid,
                p_recipient_email text,
                p_recipient_snapshot_sha256 text,
                p_subject_sha256 text,
                p_body_sha256 text,
                p_payload_sha256 text,
                p_attachment_manifest_sha256 text,
                p_requested_for text,
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
                v_existing careerops.gmail_send_drafts%ROWTYPE;
                v_draft_id uuid;
            BEGIN
                IF p_requested_for <> 'gmail_send_exact_payload'
                   OR p_recipient_snapshot_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_subject_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_body_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_payload_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_attachment_manifest_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_recipient_email IS NULL
                   OR btrim(p_recipient_email) = ''
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL
                   OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'gmail send draft payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                PERFORM 1
                FROM careerops.gmail_send_accounts
                WHERE id = p_account_id
                  AND owner_user_id = p_owner_user_id
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send account is missing'
                        USING ERRCODE = '23503';
                END IF;

                SELECT *
                INTO v_existing
                FROM careerops.gmail_send_drafts AS draft
                WHERE draft.owner_user_id = p_owner_user_id
                  AND draft.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.gmail_send_account_id <> p_account_id
                       OR v_existing.reviewed_intent_id <> p_reviewed_intent_id
                       OR v_existing.application_id <> p_application_id
                       OR v_existing.recipient_email <> p_recipient_email
                       OR v_existing.recipient_snapshot_sha256 <> p_recipient_snapshot_sha256
                       OR v_existing.subject_sha256 <> p_subject_sha256
                       OR v_existing.body_sha256 <> p_body_sha256
                       OR v_existing.payload_sha256 <> p_payload_sha256
                       OR v_existing.attachment_manifest_sha256 <> p_attachment_manifest_sha256 THEN
                        RAISE EXCEPTION 'gmail send draft idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN v_existing.id;
                END IF;

                v_draft_id := gen_random_uuid();
                INSERT INTO careerops.gmail_send_drafts (
                    id,
                    owner_user_id,
                    gmail_send_account_id,
                    reviewed_intent_id,
                    application_id,
                    requested_for,
                    recipient_email,
                    recipient_snapshot_sha256,
                    subject_sha256,
                    body_sha256,
                    payload_sha256,
                    attachment_manifest_sha256,
                    status,
                    idempotency_key,
                    trace_id,
                    created_at,
                    updated_at
                ) VALUES (
                    v_draft_id,
                    p_owner_user_id,
                    p_account_id,
                    p_reviewed_intent_id,
                    p_application_id,
                    p_requested_for,
                    p_recipient_email,
                    p_recipient_snapshot_sha256,
                    p_subject_sha256,
                    p_body_sha256,
                    p_payload_sha256,
                    p_attachment_manifest_sha256,
                    'pending_review',
                    p_idempotency_key,
                    p_trace_id,
                    v_now,
                    v_now
                );
                INSERT INTO careerops.gmail_send_command_receipts (
                    id, owner_user_id, account_id, action_intent_id, command_kind,
                    idempotency_key, request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_account_id, p_reviewed_intent_id,
                    'create_draft', p_idempotency_key, p_payload_sha256,
                    jsonb_build_object('draft_id', v_draft_id::text), p_trace_id
                );
                RETURN v_draft_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_CREATE_EXACT_DRAFT_FUNCTION}{_CREATE_EXACT_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_review_exact_payload_draft(
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
            BEGIN
                IF p_requested_for <> 'gmail_send_exact_payload'
                   OR p_decision NOT IN ('approved', 'rejected')
                   OR p_idempotency_key IS NULL
                   OR btrim(p_idempotency_key) = '' THEN
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
                IF v_draft.status <> 'pending_review' THEN
                    IF v_draft.review_idempotency_key = p_idempotency_key THEN
                        RETURN v_draft.id;
                    END IF;
                    RAISE EXCEPTION 'gmail send draft was already reviewed'
                        USING ERRCODE = '23505';
                END IF;
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
                    'review_draft', p_idempotency_key, p_payload_sha256,
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
            f"REVOKE ALL ON FUNCTION {_REVIEW_EXACT_DRAFT_FUNCTION}{_REVIEW_EXACT_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_list_drafts(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_limit integer
            )
            RETURNS TABLE (
                draft_id uuid,
                gmail_send_account_id uuid,
                owner_user_id uuid,
                reviewed_intent_id uuid,
                application_id uuid,
                requested_for text,
                recipient_email text,
                recipient_snapshot_sha256 text,
                subject_sha256 text,
                body_sha256 text,
                payload_sha256 text,
                attachment_manifest_sha256 text,
                status text,
                reviewed_at timestamp with time zone,
                created_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT id, gmail_send_account_id, owner_user_id, reviewed_intent_id, application_id,
                       requested_for, recipient_email, recipient_snapshot_sha256, subject_sha256,
                       body_sha256, payload_sha256, attachment_manifest_sha256, status::text,
                       reviewed_at, created_at
                FROM careerops.gmail_send_drafts
                WHERE owner_user_id = p_owner_user_id
                  AND gmail_send_account_id = p_account_id
                ORDER BY created_at DESC, id
                LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_LIST_DRAFTS_FUNCTION}{_LIST_DRAFTS_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_reserve_reviewed_intent(
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
            f"REVOKE ALL ON FUNCTION {_RESERVE_REVIEWED_FUNCTION}{_RESERVE_REVIEWED_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.gmail_send_list_reservations(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_limit integer
            )
            RETURNS TABLE (
                reservation_id uuid,
                gmail_send_account_id uuid,
                owner_user_id uuid,
                reviewed_intent_id uuid,
                application_id uuid,
                recipient_email text,
                recipient_snapshot_sha256 text,
                subject_sha256 text,
                body_sha256 text,
                payload_sha256 text,
                approval_snapshot_sha256 text,
                attachment_manifest_sha256 text,
                status text,
                outbox_event_id uuid,
                created_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT id, gmail_send_account_id, owner_user_id, reviewed_intent_id, application_id,
                       recipient_email, recipient_snapshot_sha256, subject_sha256, body_sha256,
                       payload_sha256, approval_snapshot_sha256, attachment_manifest_sha256,
                       status::text, outbox_event_id, created_at
                FROM careerops.gmail_send_reservations
                WHERE owner_user_id = p_owner_user_id
                  AND gmail_send_account_id = p_account_id
                ORDER BY created_at DESC, id
                LIMIT LEAST(GREATEST(COALESCE(p_limit, 50), 1), 200)
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_LIST_RESERVATIONS_FUNCTION}{_LIST_RESERVATIONS_SIGNATURE} FROM PUBLIC"
        )
    )


def _install_worker_adapter_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.claim_gmail_send_outbox_events(
                p_lease_owner text,
                p_lease_seconds integer,
                p_limit integer
            )
            RETURNS TABLE (
                event_id uuid,
                event_key text,
                action_intent_id uuid,
                payload_version_id uuid,
                event_type text,
                available_at timestamp with time zone,
                attempt_count integer,
                lease_token uuid,
                lease_until timestamp with time zone
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_lease_token uuid := gen_random_uuid();
                v_expired record;
            BEGIN
                IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
                    RAISE EXCEPTION 'gmail send lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;
                FOR v_expired IN
                    SELECT event.id, event.action_intent_id,
                           event.lease_owner, event.lease_token
                    FROM careerops.outbox_events AS event
                    WHERE event.status = 'leased'
                      AND event.lease_until <= v_now
                      AND event.event_key LIKE 'gmail-send:%'
                      AND EXISTS (
                          SELECT 1
                          FROM careerops.side_effect_attempts AS attempt
                          WHERE attempt.action_intent_id = event.action_intent_id
                            AND attempt.outbox_event_id = event.id
                            AND attempt.state = 'processing'
                      )
                    ORDER BY event.id
                    FOR UPDATE
                LOOP
                    PERFORM set_config('careerops.outbox_lease_owner', v_expired.lease_owner, true);
                    PERFORM set_config('careerops.outbox_lease_token', v_expired.lease_token::text, true);
                    UPDATE careerops.outbox_events AS event
                    SET status = 'failed',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_until = NULL,
                        last_error_code = 'GMAIL_SEND_PREPARED_LEASE_EXPIRED'
                    WHERE event.id = v_expired.id;
                    UPDATE careerops.side_effect_attempts AS attempt
                    SET state = 'reconciliation_required',
                        finished_at = COALESCE(attempt.finished_at, v_now),
                        error_code = 'GMAIL_SEND_PREPARED_LEASE_EXPIRED'
                    WHERE attempt.action_intent_id = v_expired.action_intent_id
                      AND attempt.outbox_event_id = v_expired.id
                      AND attempt.state = 'processing';
                    UPDATE careerops.action_intents AS intent
                    SET status = 'reconciliation_required',
                        updated_at = v_now
                    WHERE intent.id = v_expired.action_intent_id;
                    INSERT INTO careerops.gmail_send_reconciliation_jobs (
                        outbox_event_id, status, available_at, last_error_code
                    ) VALUES (
                        v_expired.id, 'queued', v_now, 'GMAIL_SEND_PREPARED_LEASE_EXPIRED'
                    )
                    ON CONFLICT (outbox_event_id) DO UPDATE
                    SET status = 'queued',
                        available_at = v_now,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_until = NULL,
                        last_error_code = EXCLUDED.last_error_code,
                        updated_at = v_now;
                END LOOP;
                RETURN QUERY
                WITH candidates AS (
                    SELECT event.id
                    FROM careerops.outbox_events AS event
                    WHERE event.status = 'pending'
                      AND event.available_at <= v_now
                      AND event.event_key LIKE 'gmail-send:%'
                    ORDER BY event.available_at, event.id
                    LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100)
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE careerops.outbox_events AS event
                    SET status = 'leased',
                        lease_owner = p_lease_owner,
                        lease_token = v_lease_token,
                        lease_until = v_now + make_interval(secs => LEAST(GREATEST(COALESCE(p_lease_seconds, 300), 1), 3600)),
                        attempt_count = event.attempt_count + 1,
                        last_error_code = NULL
                    FROM candidates
                    WHERE event.id = candidates.id
                    RETURNING event.*
                )
                SELECT updated.id, updated.event_key, updated.action_intent_id,
                       updated.payload_version_id, updated.event_type::text,
                       updated.available_at, updated.attempt_count,
                       updated.lease_token, updated.lease_until
                FROM updated
                ORDER BY updated.available_at, updated.id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_CLAIM_OUTBOX_FUNCTION}{_CLAIM_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.prepare_gmail_send_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid
            )
            RETURNS TABLE (
                event_id uuid,
                event_key text,
                action_intent_id uuid,
                payload_version_id uuid,
                reservation_key text,
                reconciliation_key text,
                send_account_id uuid,
                send_credential_handle text,
                readonly_credential_handle text,
                account_subject text,
                payload_json jsonb,
                prepare_state text,
                reason_code text,
                existing_provider_message_id text,
                existing_provider_thread_id text,
                existing_rfc_message_id text,
                existing_provider_state text,
                existing_received_at timestamp with time zone
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_prepare record;
                v_send_handle text;
                v_readonly_handle text;
                v_payload jsonb;
                v_receipt careerops.provider_receipts%ROWTYPE;
            BEGIN
                SELECT *
                INTO v_prepare
                FROM careerops.gmail_send_prepare_outbox_event(p_event_id, p_lease_owner, p_lease_token);
                IF v_prepare.event_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send event prepare returned no row'
                        USING ERRCODE = '23503';
                END IF;
                IF v_prepare.credential_reference_id IS NOT NULL THEN
                    SELECT credential.secret_handle
                    INTO v_send_handle
                    FROM careerops.oauth_credential_references AS credential
                    WHERE credential.id = v_prepare.credential_reference_id
                      AND credential.provider = 'gmail_send'
                      AND credential.status = 'active'
                    FOR KEY SHARE;
                END IF;
                SELECT readonly_credential.secret_handle
                INTO v_readonly_handle
                FROM careerops.gmail_send_accounts AS send_account
                JOIN careerops.gmail_accounts AS readonly_account
                  ON readonly_account.id = send_account.reconciliation_gmail_account_id
                JOIN careerops.oauth_credential_references AS readonly_credential
                  ON readonly_credential.id = readonly_account.oauth_credential_reference_id
                WHERE send_account.id = v_prepare.account_id
                  AND readonly_account.status = 'active'
                  AND readonly_account.account_subject = send_account.account_subject
                  AND readonly_credential.provider = 'gmail'
                  AND readonly_credential.status = 'active'
                  AND readonly_credential.account_subject = readonly_account.account_subject
                  AND readonly_credential.granted_scopes = jsonb_build_array(
                      'https://www.googleapis.com/auth/gmail.readonly'
                  )
                FOR KEY SHARE;
                SELECT payload.payload
                INTO v_payload
                FROM careerops.action_payload_versions AS payload
                WHERE payload.id = v_prepare.payload_version_id
                  AND payload.action_intent_id = v_prepare.action_intent_id
                FOR KEY SHARE;
                SELECT *
                INTO v_receipt
                FROM careerops.provider_receipts AS receipt
                WHERE receipt.provider = 'gmail'
                  AND receipt.reconciliation_key = v_prepare.reconciliation_key
                ORDER BY receipt.received_at DESC
                LIMIT 1;
                RETURN QUERY
                SELECT v_prepare.event_id::uuid,
                       v_prepare.event_key::text,
                       v_prepare.action_intent_id::uuid,
                       v_prepare.payload_version_id::uuid,
                       v_prepare.reservation_key::text,
                       v_prepare.reconciliation_key::text,
                       v_prepare.account_id::uuid,
                       v_send_handle,
                       v_readonly_handle,
                       v_prepare.account_subject::text,
                       v_payload,
                       CASE
                           WHEN v_prepare.prepare_state <> 'ready' THEN v_prepare.prepare_state::text
                           WHEN v_send_handle IS NULL THEN 'stopped'
                           WHEN v_readonly_handle IS NULL THEN 'stopped'
                           WHEN v_payload IS NULL THEN 'stopped'
                           ELSE 'ready'
                       END,
                       CASE
                           WHEN v_prepare.reason_code IS NOT NULL THEN v_prepare.reason_code::text
                           WHEN v_send_handle IS NULL THEN 'GMAIL_SEND_CREDENTIAL_NOT_ACTIVE'
                           WHEN v_readonly_handle IS NULL THEN 'GMAIL_READONLY_RECONCILIATION_NOT_CONFIGURED'
                           WHEN v_payload IS NULL THEN 'GMAIL_SEND_PAYLOAD_NOT_FOUND'
                           ELSE NULL::text
                       END,
                       v_receipt.provider_resource_id,
                       v_receipt.receipt_metadata ->> 'provider_thread_id',
                       v_receipt.receipt_metadata ->> 'rfc_message_id',
                       v_receipt.receipt_metadata ->> 'provider_state',
                       v_receipt.received_at;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_PREPARE_OUTBOX_FUNCTION}{_PREPARE_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )

    for sql in (
        """
        CREATE FUNCTION careerops.mark_gmail_send_outbox_published(
            p_event_id uuid, p_lease_owner text, p_lease_token uuid
        )
        RETURNS void
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, careerops
        AS $function$
            WITH settings AS (
                SELECT
                    set_config('careerops.outbox_lease_owner', p_lease_owner, true),
                    set_config('careerops.outbox_lease_token', p_lease_token::text, true)
            )
            UPDATE careerops.outbox_events
            SET status = 'published', lease_owner = NULL, lease_token = NULL,
                lease_until = NULL, published_at = CURRENT_TIMESTAMP, last_error_code = NULL
            FROM settings
            WHERE id = p_event_id
              AND lease_owner = p_lease_owner
              AND lease_token = p_lease_token
              AND event_key LIKE 'gmail-send:%'
        $function$
        """,
        """
        CREATE FUNCTION careerops.release_gmail_send_outbox_event(
            p_event_id uuid, p_lease_owner text, p_lease_token uuid,
            p_retry_at timestamp with time zone, p_error_code text, p_terminal boolean
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, careerops
        AS $function$
        DECLARE
            v_now timestamp with time zone := CURRENT_TIMESTAMP;
            v_event careerops.outbox_events%ROWTYPE;
            v_attempt careerops.side_effect_attempts%ROWTYPE;
            v_requires_reconciliation boolean := false;
        BEGIN
            IF p_event_id IS NULL
               OR p_lease_token IS NULL
               OR p_retry_at IS NULL
               OR p_terminal IS NULL THEN
                RAISE EXCEPTION 'gmail send release requires event, lease, retry, and terminal values'
                    USING ERRCODE = '22004';
            END IF;
            IF p_lease_owner IS NULL
               OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
               OR p_error_code IS NULL
               OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                RAISE EXCEPTION 'gmail send release payload is invalid'
                    USING ERRCODE = '22023';
            END IF;

            SELECT *
            INTO v_event
            FROM careerops.outbox_events
            WHERE id = p_event_id
            FOR UPDATE;
            IF v_event.id IS NOT NULL
               AND v_event.status = 'failed'
               AND v_event.event_key LIKE 'gmail-send:%'
               AND v_event.last_error_code = p_error_code
               AND EXISTS (
                   SELECT 1
                   FROM careerops.side_effect_attempts AS attempt
                   WHERE attempt.action_intent_id = v_event.action_intent_id
                     AND attempt.outbox_event_id = v_event.id
                     AND attempt.state = 'reconciliation_required'
               )
               AND EXISTS (
                   SELECT 1
                   FROM careerops.gmail_send_reconciliation_jobs AS job
                   WHERE job.outbox_event_id = v_event.id
               ) THEN
                RETURN;
            END IF;
            IF v_event.id IS NULL
               OR v_event.status <> 'leased'
               OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
               OR v_event.lease_token IS DISTINCT FROM p_lease_token
               OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                RAISE EXCEPTION 'gmail send release lease is invalid'
                    USING ERRCODE = '55000';
            END IF;

            SELECT *
            INTO v_attempt
            FROM careerops.side_effect_attempts AS attempt
            WHERE attempt.action_intent_id = v_event.action_intent_id
              AND attempt.outbox_event_id = v_event.id
            ORDER BY attempt.ordinal DESC
            LIMIT 1
            FOR UPDATE;

            IF p_terminal
               AND v_attempt.id IS NOT NULL
               AND (
                   v_attempt.state = 'reconciliation_required'
                   OR (
                       v_attempt.state = 'processing'
                       AND p_error_code = 'GMAIL_SEND_AMBIGUITY_RECORD_FAILED'
                   )
               ) THEN
                v_requires_reconciliation := true;
                UPDATE careerops.side_effect_attempts
                SET state = 'reconciliation_required',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object(
                        'release_error_code', p_error_code,
                        'post_boundary_ambiguous', true
                    )
                WHERE id = v_attempt.id;
            ELSIF v_attempt.id IS NOT NULL
                  AND v_attempt.state = 'processing' THEN
                UPDATE careerops.side_effect_attempts
                SET state = 'failed',
                    finished_at = v_now,
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object(
                        'release_error_code', p_error_code,
                        'post_boundary_ambiguous', false
                    )
                WHERE id = v_attempt.id;
            END IF;

            PERFORM set_config('careerops.outbox_lease_owner', p_lease_owner, true);
            PERFORM set_config('careerops.outbox_lease_token', p_lease_token::text, true);
            UPDATE careerops.outbox_events
            SET status = CASE WHEN p_terminal THEN 'failed' ELSE 'pending' END,
                available_at = p_retry_at, lease_owner = NULL, lease_token = NULL,
                lease_until = NULL, last_error_code = p_error_code
            WHERE id = p_event_id
              AND lease_owner = p_lease_owner
              AND lease_token = p_lease_token
              AND event_key LIKE 'gmail-send:%';

            UPDATE careerops.action_intents
            SET status = CASE
                    WHEN v_requires_reconciliation THEN 'reconciliation_required'
                    WHEN p_terminal THEN 'failed'
                    ELSE 'eligible'
                END,
                updated_at = v_now
            WHERE id = v_event.action_intent_id;

            IF v_requires_reconciliation THEN
                INSERT INTO careerops.gmail_send_reconciliation_jobs (
                    outbox_event_id, status, available_at, last_error_code
                ) VALUES (
                    v_event.id, 'queued', v_now, p_error_code
                )
                ON CONFLICT (outbox_event_id) DO UPDATE
                SET status = 'queued',
                    available_at = v_now,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = EXCLUDED.last_error_code,
                    updated_at = v_now;
            END IF;
        END
        $function$
        """,
    ):
        op.execute(sa.text(sql))
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_MARK_PUBLISHED_FUNCTION}{_MARK_PUBLISHED_SIGNATURE} FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RELEASE_OUTBOX_FUNCTION}{_RELEASE_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.record_gmail_send_outbox_receipt(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_provider_message_id text,
                p_provider_thread_id text,
                p_rfc_message_id text,
                p_provider_state text,
                p_received_at timestamp with time zone,
                p_receipt_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_event careerops.outbox_events%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
                v_reconciliation_key text;
                v_expected_rfc_message_id text;
            BEGIN
                IF p_event_id IS NULL
                   OR p_lease_token IS NULL
                   OR p_receipt_id IS NULL
                   OR p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$'
                   OR p_provider_message_id IS NULL
                   OR p_provider_message_id !~ '^[A-Za-z0-9._:@/-]{1,200}$'
                   OR p_provider_thread_id IS NULL
                   OR p_provider_thread_id !~ '^[A-Za-z0-9._:@/-]{1,200}$'
                   OR p_rfc_message_id IS NULL
                   OR btrim(p_rfc_message_id) = ''
                   OR char_length(p_rfc_message_id) > 320
                   OR p_rfc_message_id ~ '[[:cntrl:]]'
                   OR p_provider_state <> 'sent_confirmed'
                   OR p_received_at IS NULL THEN
                    RAISE EXCEPTION 'gmail send receipt payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events AS event
                WHERE event.id = p_event_id
                FOR UPDATE;
                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now
                   OR v_event.event_key NOT LIKE 'gmail-send:%' THEN
                    RAISE EXCEPTION 'gmail send receipt lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                SELECT *
                INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state = 'processing'
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send receipt requires one processing attempt'
                        USING ERRCODE = '23514';
                END IF;

                SELECT reservation.reconciliation_key,
                       payload.payload ->> 'message_id_header'
                INTO v_reconciliation_key, v_expected_rfc_message_id
                FROM careerops.autopilot_cap_reservations AS reservation
                JOIN careerops.action_payload_versions AS payload
                  ON payload.action_intent_id = reservation.action_intent_id
                 AND payload.id = reservation.payload_version_id
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.channel = 'gmail:send'
                  AND reservation.reservation_key = substring(v_event.event_key FROM char_length('gmail-send:') + 1)
                FOR KEY SHARE;
                IF v_reconciliation_key IS NULL
                   OR v_expected_rfc_message_id IS DISTINCT FROM p_rfc_message_id THEN
                    RAISE EXCEPTION 'gmail send receipt does not match reviewed payload'
                        USING ERRCODE = '23514';
                END IF;

                INSERT INTO careerops.provider_receipts (
                    id, side_effect_attempt_id, provider, provider_resource_id,
                    reconciliation_key, final_state, provider_timestamp,
                    received_at, receipt_metadata
                ) VALUES (
                    p_receipt_id, v_attempt.id, 'gmail', p_provider_message_id,
                    v_reconciliation_key, 'confirmed', p_received_at,
                    v_now, jsonb_build_object(
                        'provider_thread_id', p_provider_thread_id,
                        'rfc_message_id', p_rfc_message_id,
                        'provider_state', p_provider_state
                    )
                );

                UPDATE careerops.side_effect_attempts
                SET state = 'confirmed',
                    finished_at = v_now,
                    error_code = NULL,
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'gmail',
                        'provider_message_id', p_provider_message_id,
                        'provider_thread_id', p_provider_thread_id,
                        'rfc_message_id', p_rfc_message_id,
                        'receipt_id', p_receipt_id::text
                    )
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'confirmed', updated_at = v_now
                WHERE id = v_event.action_intent_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_OUTBOX_RECEIPT_FUNCTION}{_RECORD_OUTBOX_RECEIPT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.record_gmail_send_outbox_ambiguity(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_error_code text
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                PERFORM careerops.gmail_send_record_ambiguity(p_event_id, p_lease_owner, p_lease_token, p_error_code);
                INSERT INTO careerops.gmail_send_reconciliation_jobs (
                    outbox_event_id, status, available_at, last_error_code
                ) VALUES (
                    p_event_id, 'queued', CURRENT_TIMESTAMP, p_error_code
                )
                ON CONFLICT (outbox_event_id) DO UPDATE
                SET status = 'queued',
                    available_at = CURRENT_TIMESTAMP,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = EXCLUDED.last_error_code,
                    updated_at = CURRENT_TIMESTAMP;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_OUTBOX_AMBIGUITY_FUNCTION}{_RECORD_OUTBOX_AMBIGUITY_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.claim_gmail_send_reconciliation_jobs(
                p_lease_owner text,
                p_lease_seconds integer,
                p_limit integer
            )
            RETURNS TABLE (
                event_id uuid,
                event_key text,
                action_intent_id uuid,
                payload_version_id uuid,
                reservation_key text,
                reconciliation_key text,
                send_account_id uuid,
                readonly_credential_handle text,
                account_subject text,
                payload_json jsonb,
                lease_owner text,
                lease_token uuid,
                attempt_count integer
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_lease_token uuid := gen_random_uuid();
            BEGIN
                IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
                    RAISE EXCEPTION 'gmail send reconciliation lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;
                UPDATE careerops.gmail_send_reconciliation_jobs
                SET status = 'queued',
                    available_at = v_now,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = COALESCE(last_error_code, 'GMAIL_SEND_RECONCILIATION_LEASE_EXPIRED'),
                    updated_at = v_now
                WHERE status = 'leased'
                  AND lease_until <= v_now;
                RETURN QUERY
                WITH candidates AS (
                    SELECT job.outbox_event_id
                    FROM careerops.gmail_send_reconciliation_jobs AS job
                    WHERE job.status = 'queued'
                      AND job.available_at <= v_now
                    ORDER BY job.available_at, job.outbox_event_id
                    LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100)
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE careerops.gmail_send_reconciliation_jobs AS job
                    SET status = 'leased',
                        lease_owner = p_lease_owner,
                        lease_token = v_lease_token,
                        lease_until = v_now + make_interval(secs => LEAST(GREATEST(COALESCE(p_lease_seconds, 300), 1), 3600)),
                        attempt_count = job.attempt_count + 1,
                        updated_at = v_now
                    FROM candidates
                    WHERE job.outbox_event_id = candidates.outbox_event_id
                    RETURNING job.*
                )
                SELECT event.id, event.event_key, event.action_intent_id,
                       event.payload_version_id, reservation.reservation_key,
                       reservation.reconciliation_key, send_account.id,
                       readonly_credential.secret_handle, send_account.account_subject,
                       payload.payload, updated.lease_owner, updated.lease_token,
                       updated.attempt_count
                FROM updated
                JOIN careerops.outbox_events AS event
                  ON event.id = updated.outbox_event_id
                 AND event.event_key LIKE 'gmail-send:%'
                JOIN careerops.autopilot_cap_reservations AS reservation
                  ON reservation.action_intent_id = event.action_intent_id
                 AND reservation.payload_version_id = event.payload_version_id
                 AND reservation.channel = 'gmail:send'
                 AND reservation.target_host = 'gmail.googleapis.com'
                 AND reservation.reservation_key = substring(
                     event.event_key FROM char_length('gmail-send:') + 1
                 )
                JOIN careerops.gmail_send_command_receipts AS reserve_receipt
                  ON reserve_receipt.outbox_event_id = event.id
                 AND reserve_receipt.command_kind = 'reserve_and_enqueue'
                 AND reserve_receipt.action_intent_id = event.action_intent_id
                 AND reserve_receipt.response_json ->> 'reservation_id' = reservation.id::text
                JOIN LATERAL (
                    SELECT evidence.account_id
                    FROM careerops.gmail_send_review_evidence AS evidence
                    WHERE evidence.action_intent_id = event.action_intent_id
                      AND evidence.payload_version_id = event.payload_version_id
                      AND evidence.payload_hash = reservation.payload_hash
                      AND evidence.owner_user_id = reserve_receipt.owner_user_id
                      AND evidence.reviewed_by_user_id = reserve_receipt.owner_user_id
                      AND evidence.account_id = reserve_receipt.account_id
                      AND evidence.recipient_sha256 = reservation.company_key
                    ORDER BY evidence.created_at DESC, evidence.id
                    LIMIT 1
                ) AS send_evidence ON true
                JOIN careerops.gmail_send_accounts AS send_account
                  ON send_account.id = send_evidence.account_id
                 AND send_account.owner_user_id = reserve_receipt.owner_user_id
                JOIN careerops.gmail_accounts AS readonly_account
                  ON readonly_account.id = send_account.reconciliation_gmail_account_id
                 AND readonly_account.owner_user_id = reserve_receipt.owner_user_id
                 AND readonly_account.status = 'active'
                JOIN careerops.oauth_credential_references AS readonly_credential
                  ON readonly_credential.id = readonly_account.oauth_credential_reference_id
                 AND readonly_credential.provider = 'gmail'
                 AND readonly_credential.status = 'active'
                 AND readonly_credential.account_subject = readonly_account.account_subject
                 AND readonly_credential.granted_scopes = jsonb_build_array(
                     'https://www.googleapis.com/auth/gmail.readonly'
                 )
                JOIN careerops.action_payload_versions AS payload
                  ON payload.id = event.payload_version_id
                 AND payload.action_intent_id = event.action_intent_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_CLAIM_RECONCILIATION_FUNCTION}{_CLAIM_RECONCILIATION_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.reconcile_gmail_send_outbox_event(
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
                UPDATE careerops.side_effect_attempts
                SET response_metadata = response_metadata || COALESCE(p_sent_metadata_json, '{}'::jsonb) || jsonb_build_object(
                    'provider', 'gmail',
                    'provider_message_id', p_provider_message_id,
                    'provider_thread_id', p_provider_thread_id,
                    'rfc_message_id', p_rfc_message_id,
                    'reconciled_at', p_reconciled_at::text
                )
                WHERE id = v_attempt.id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECONCILE_OUTBOX_FUNCTION}{_RECONCILE_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.reconcile_ambiguous_gmail_send_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_provider_message_id text,
                p_provider_thread_id text,
                p_rfc_message_id text,
                p_sent_metadata_json jsonb,
                p_reconciled_at timestamp with time zone,
                p_receipt_id uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_job careerops.gmail_send_reconciliation_jobs%ROWTYPE;
                v_event careerops.outbox_events%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
                v_reconciliation_key text;
                v_payload jsonb;
            BEGIN
                IF p_event_id IS NULL
                   OR p_lease_token IS NULL
                   OR p_receipt_id IS NULL
                   OR p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$'
                   OR p_provider_message_id IS NULL
                   OR p_provider_message_id !~ '^[A-Za-z0-9._:@/-]{1,200}$'
                   OR p_provider_thread_id IS NULL
                   OR p_provider_thread_id !~ '^[A-Za-z0-9._:@/-]{1,200}$'
                   OR p_rfc_message_id IS NULL
                   OR btrim(p_rfc_message_id) = ''
                   OR char_length(p_rfc_message_id) > 320
                   OR p_rfc_message_id ~ '[[:cntrl:]]'
                   OR p_sent_metadata_json IS NULL
                   OR jsonb_typeof(p_sent_metadata_json) <> 'object'
                   OR p_reconciled_at IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconciliation receipt payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_job
                FROM careerops.gmail_send_reconciliation_jobs AS job
                WHERE job.outbox_event_id = p_event_id
                  AND job.status = 'leased'
                  AND job.lease_owner = p_lease_owner
                  AND job.lease_token = p_lease_token
                  AND job.lease_until > v_now
                FOR UPDATE;
                IF v_job.outbox_event_id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconciliation job lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events AS event
                WHERE event.id = p_event_id
                  AND event.status = 'failed'
                  AND event.event_key LIKE 'gmail-send:%'
                FOR UPDATE;
                IF v_event.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconciliation requires a failed send event'
                        USING ERRCODE = '23514';
                END IF;

                SELECT *
                INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state = 'reconciliation_required'
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconciliation requires an ambiguous attempt'
                        USING ERRCODE = '23514';
                END IF;

                SELECT reservation.reconciliation_key
                INTO v_reconciliation_key
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.channel = 'gmail:send'
                  AND reservation.reservation_key = substring(v_event.event_key FROM char_length('gmail-send:') + 1)
                FOR KEY SHARE;
                IF v_reconciliation_key IS NULL THEN
                    RAISE EXCEPTION 'gmail send reconciliation reservation is missing'
                        USING ERRCODE = '23503';
                END IF;

                SELECT payload.payload
                INTO v_payload
                FROM careerops.action_payload_versions AS payload
                WHERE payload.id = v_event.payload_version_id
                  AND payload.action_intent_id = v_event.action_intent_id
                FOR KEY SHARE;
                IF v_payload IS NULL
                   OR p_rfc_message_id IS DISTINCT FROM v_payload ->> 'message_id_header'
                   OR p_sent_metadata_json ->> 'provider_message_id' IS DISTINCT FROM p_provider_message_id
                   OR p_sent_metadata_json ->> 'provider_thread_id' IS DISTINCT FROM p_provider_thread_id
                   OR NOT COALESCE(p_sent_metadata_json -> 'label_ids' @> jsonb_build_array('SENT'), false)
                   OR lower(btrim(COALESCE(
                       p_sent_metadata_json #>> '{headers,from}',
                       p_sent_metadata_json #>> '{headers,From}'
                   ))) IS DISTINCT FROM lower(btrim(v_payload ->> 'sender'))
                   OR lower(btrim(COALESCE(
                       p_sent_metadata_json #>> '{headers,to}',
                       p_sent_metadata_json #>> '{headers,To}'
                   ))) IS DISTINCT FROM lower(btrim(v_payload ->> 'recipient'))
                   OR COALESCE(
                       p_sent_metadata_json #>> '{headers,subject}',
                       p_sent_metadata_json #>> '{headers,Subject}'
                   ) IS DISTINCT FROM v_payload ->> 'subject'
                   OR COALESCE(
                       p_sent_metadata_json #>> '{headers,message-id}',
                       p_sent_metadata_json #>> '{headers,Message-ID}'
                   ) IS DISTINCT FROM p_rfc_message_id THEN
                    RAISE EXCEPTION 'gmail send reconciliation metadata does not match reviewed payload'
                        USING ERRCODE = '23514';
                END IF;

                INSERT INTO careerops.provider_receipts (
                    id, side_effect_attempt_id, provider, provider_resource_id,
                    reconciliation_key, final_state, provider_timestamp,
                    received_at, receipt_metadata
                ) VALUES (
                    p_receipt_id, v_attempt.id, 'gmail', p_provider_message_id,
                    v_reconciliation_key, 'confirmed', p_reconciled_at,
                    v_now, p_sent_metadata_json || jsonb_build_object(
                        'provider_thread_id', p_provider_thread_id,
                        'rfc_message_id', p_rfc_message_id,
                        'provider_state', 'sent_confirmed',
                        'reconciled_at', p_reconciled_at::text
                    )
                );

                UPDATE careerops.side_effect_attempts
                SET state = 'confirmed',
                    finished_at = v_now,
                    error_code = NULL,
                    response_metadata = response_metadata || p_sent_metadata_json || jsonb_build_object(
                        'provider', 'gmail',
                        'provider_message_id', p_provider_message_id,
                        'provider_thread_id', p_provider_thread_id,
                        'rfc_message_id', p_rfc_message_id,
                        'receipt_id', p_receipt_id::text,
                        'reconciled_at', p_reconciled_at::text
                    )
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'confirmed', updated_at = v_now
                WHERE id = v_event.action_intent_id;
                PERFORM set_config(
                    'careerops.outbox_reconciliation_event_id',
                    v_event.id::text,
                    true
                );
                UPDATE careerops.outbox_events
                SET status = 'published',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    published_at = COALESCE(published_at, v_now),
                    last_error_code = NULL
                WHERE id = v_event.id;
                UPDATE careerops.gmail_send_reconciliation_jobs
                SET status = 'resolved',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = NULL,
                    updated_at = v_now
                WHERE outbox_event_id = v_job.outbox_event_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECONCILE_AMBIGUOUS_FUNCTION}{_RECONCILE_AMBIGUOUS_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.keep_gmail_send_reconciliation_ambiguous(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_error_code text
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF p_event_id IS NULL
                   OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$'
                   OR p_error_code IS NULL
                   OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                    RAISE EXCEPTION 'gmail send ambiguous reconciliation payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                UPDATE careerops.gmail_send_reconciliation_jobs
                SET status = 'queued',
                    available_at = CURRENT_TIMESTAMP + interval '5 minutes',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = p_error_code,
                    updated_at = CURRENT_TIMESTAMP
                WHERE outbox_event_id = p_event_id
                  AND status = 'leased'
                  AND lease_owner = p_lease_owner
                  AND lease_token = p_lease_token
                  AND lease_until > CURRENT_TIMESTAMP;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'gmail send reconciliation job lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_KEEP_AMBIGUOUS_FUNCTION}{_KEEP_AMBIGUOUS_SIGNATURE} FROM PUBLIC"
        )
    )


def _drop_functions() -> None:
    for function, signature in (
        (_LIST_FUNCTION, _LIST_SIGNATURE),
        (_ACCOUNT_STATUS_FUNCTION, _ACCOUNT_STATUS_SIGNATURE),
        (_CREATE_EXACT_DRAFT_FUNCTION, _CREATE_EXACT_DRAFT_SIGNATURE),
        (_REVIEW_EXACT_DRAFT_FUNCTION, _REVIEW_EXACT_DRAFT_SIGNATURE),
        (_LIST_DRAFTS_FUNCTION, _LIST_DRAFTS_SIGNATURE),
        (_RESERVE_REVIEWED_FUNCTION, _RESERVE_REVIEWED_SIGNATURE),
        (_LIST_RESERVATIONS_FUNCTION, _LIST_RESERVATIONS_SIGNATURE),
        (_STATUS_FUNCTION, _STATUS_SIGNATURE),
        (_KEEP_AMBIGUOUS_FUNCTION, _KEEP_AMBIGUOUS_SIGNATURE),
        (_RECONCILE_AMBIGUOUS_FUNCTION, _RECONCILE_AMBIGUOUS_SIGNATURE),
        (_CLAIM_RECONCILIATION_FUNCTION, _CLAIM_RECONCILIATION_SIGNATURE),
        (_RECORD_OUTBOX_AMBIGUITY_FUNCTION, _RECORD_OUTBOX_AMBIGUITY_SIGNATURE),
        (_RECORD_OUTBOX_RECEIPT_FUNCTION, _RECORD_OUTBOX_RECEIPT_SIGNATURE),
        (_RECONCILE_OUTBOX_FUNCTION, _RECONCILE_OUTBOX_SIGNATURE),
        (_RELEASE_OUTBOX_FUNCTION, _RELEASE_OUTBOX_SIGNATURE),
        (_MARK_PUBLISHED_FUNCTION, _MARK_PUBLISHED_SIGNATURE),
        (_CLAIM_OUTBOX_FUNCTION, _CLAIM_OUTBOX_SIGNATURE),
        (_PREPARE_OUTBOX_FUNCTION, _PREPARE_OUTBOX_SIGNATURE),
        (_RECONCILE_FUNCTION, _RECONCILE_SIGNATURE),
        (_AMBIGUOUS_FUNCTION, _AMBIGUOUS_SIGNATURE),
        (_RECORD_FUNCTION, _RECORD_SIGNATURE),
        (_PREPARE_FUNCTION, _PREPARE_SIGNATURE),
        (_RESERVE_FUNCTION, _RESERVE_SIGNATURE),
        (_REGISTRATION_RECEIPT_FUNCTION, _REGISTRATION_RECEIPT_SIGNATURE),
        (_REGISTER_FUNCTION, _REGISTER_ACCOUNT_WITH_READONLY_SIGNATURE),
        (_REGISTER_FUNCTION, _REGISTER_SIGNATURE),
        (_REVIEW_DRAFT_FUNCTION, _REVIEW_DRAFT_SIGNATURE),
        (_CREATE_DRAFT_FUNCTION, _CREATE_DRAFT_SIGNATURE),
    ):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}{signature}"))


def upgrade() -> None:
    op.create_table(
        "gmail_send_accounts",
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
        sa.Column(
            "reconciliation_gmail_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_accounts.id", ondelete="RESTRICT"),
        ),
        sa.Column("provider", sa.String(16), server_default="gmail_send", nullable=False),
        sa.Column("account_subject", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default="disabled", nullable=False),
        sa.Column("daily_send_limit", sa.Integer(), nullable=False),
        sa.Column("credential_store_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("release_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("last_error_code", sa.Text()),
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
            name="uq_gmail_send_accounts_owner_provider_subject",
        ),
        sa.UniqueConstraint(
            "owner_user_id", "idempotency_key", name="uq_gmail_send_accounts_owner_idempotency_key"
        ),
        sa.CheckConstraint("provider = 'gmail_send'", name="provider_values"),
        sa.CheckConstraint(
            "status IN ('disabled', 'active', 'paused', 'revoked', 'blocked')", name="status_values"
        ),
        sa.CheckConstraint("daily_send_limit BETWEEN 1 AND 500", name="daily_send_limit_bounds"),
        sa.CheckConstraint(
            "credential_store_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="credential_store_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "release_evidence_sha256 ~ '^[a-f0-9]{64}$'", name="release_evidence_sha256_format"
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint(
            "btrim(account_subject) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_send_accounts_owner_updated_at",
        "gmail_send_accounts",
        ["owner_user_id", "updated_at"],
        schema="careerops",
    )

    op.create_table(
        "gmail_send_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gmail_send_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_send_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewed_intent_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.action_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("requested_for", sa.Text(), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("recipient_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("subject_sha256", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("attachment_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending_review", nullable=False),
        sa.Column("review_reason", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("review_idempotency_key", sa.Text()),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_gmail_send_drafts_owner_idempotency_key",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "review_idempotency_key",
            name="uq_gmail_send_drafts_owner_review_idempotency_key",
        ),
        sa.CheckConstraint(
            "requested_for = 'gmail_send_exact_payload'",
            name="requested_for_exact_payload",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "(status = 'pending_review' AND reviewed_at IS NULL) OR "
            "(status <> 'pending_review' AND reviewed_at IS NOT NULL)",
            name="review_timestamp_consistent",
        ),
        sa.CheckConstraint(
            "recipient_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="recipient_snapshot_sha256_format",
        ),
        sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
        sa.CheckConstraint("body_sha256 ~ '^[a-f0-9]{64}$'", name="body_sha256_format"),
        sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
        sa.CheckConstraint(
            "attachment_manifest_sha256 ~ '^[a-f0-9]{64}$'",
            name="attachment_manifest_sha256_format",
        ),
        sa.CheckConstraint(
            "btrim(recipient_email) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_send_drafts_account_created_at",
        "gmail_send_drafts",
        ["gmail_send_account_id", "created_at"],
        schema="careerops",
    )

    op.create_table(
        "gmail_send_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "gmail_send_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_send_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewed_intent_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.action_intents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.Text(), nullable=False),
        sa.Column("recipient_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("subject_sha256", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("approval_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("attachment_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_gmail_send_reservations_owner_idempotency_key",
        ),
        sa.UniqueConstraint(
            "reviewed_intent_id",
            "payload_sha256",
            name="uq_gmail_send_reservations_intent_payload",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'already_reserved', 'blocked')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "recipient_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="recipient_snapshot_sha256_format",
        ),
        sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
        sa.CheckConstraint("body_sha256 ~ '^[a-f0-9]{64}$'", name="body_sha256_format"),
        sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
        sa.CheckConstraint(
            "approval_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="approval_snapshot_sha256_format",
        ),
        sa.CheckConstraint(
            "attachment_manifest_sha256 ~ '^[a-f0-9]{64}$'",
            name="attachment_manifest_sha256_format",
        ),
        sa.CheckConstraint(
            "btrim(recipient_email) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_send_reservations_account_created_at",
        "gmail_send_reservations",
        ["gmail_send_account_id", "created_at"],
        schema="careerops",
    )

    op.create_table(
        "gmail_send_reconciliation_jobs",
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'resolved', 'blocked')",
            name="status_values",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL) OR "
            "(status <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_until IS NULL)",
            name="lease_state_consistent",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_send_reconciliation_jobs_available_at",
        "gmail_send_reconciliation_jobs",
        ["status", "available_at"],
        schema="careerops",
    )

    op.create_table(
        "gmail_send_review_evidence",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_send_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "release_qualification_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.release_qualifications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column(
            "approval_request_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.approval_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewed_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("recipient_sha256", sa.String(64), nullable=False),
        sa.Column("subject_sha256", sa.String(64), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column(
            "attachment_sha256s",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("review_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("review_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_gmail_send_review_evidence_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_gmail_send_review_evidence_owner_idempotency",
        ),
        sa.CheckConstraint("payload_hash ~ '^[a-f0-9]{64}$'", name="payload_hash_format"),
        sa.CheckConstraint("recipient_sha256 ~ '^[a-f0-9]{64}$'", name="recipient_sha256_format"),
        sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
        sa.CheckConstraint("body_sha256 ~ '^[a-f0-9]{64}$'", name="body_sha256_format"),
        sa.CheckConstraint(
            "review_evidence_sha256 ~ '^[a-f0-9]{64}$'", name="review_evidence_sha256_format"
        ),
        sa.CheckConstraint(
            "review_snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="review_snapshot_sha256_format"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attachment_sha256s) = 'array'", name="attachment_sha256s_array"
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''", name="text_nonempty"
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_gmail_send_review_evidence_action_payload",
        "gmail_send_review_evidence",
        ["action_intent_id", "payload_version_id", "created_at"],
        schema="careerops",
    )

    op.create_table(
        "gmail_send_command_receipts",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_send_accounts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.action_intents.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
        ),
        sa.Column("command_kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "command_kind",
            "idempotency_key",
            name="uq_gmail_send_command_receipts_identity",
        ),
        sa.CheckConstraint(
            "command_kind IN ('create_draft', 'review_draft', 'register_account', 'reserve_and_enqueue')",
            name="command_kind_values",
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''", name="text_nonempty"
        ),
        schema="careerops",
    )

    _install_append_only_guards()
    _install_outbox_lifecycle_guard(allow_reconciled_gmail=True)
    _install_grant_revocation_state_lock()
    _install_cap_reservation_insert_guard()
    _install_cap_reservation_execution_guard()
    _install_draft_functions()
    _install_register_function()
    _install_reserve_function()
    _install_outbox_functions()
    _install_read_functions()
    _install_operator_api_functions()
    _install_worker_adapter_functions()
    for statement in _REVOKES:
        op.execute(sa.text(statement))
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_mail_sender", _MAIL_SENDER_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role(
        "careerops_readonly",
        tuple(
            f"REVOKE ALL ON careerops.{table_name} FROM careerops_readonly"
            for table_name in _GMAIL_SEND_TABLES
        ),
    )
    _run_for_role("careerops_mail_sender", _MAIL_SENDER_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_grant_revocation_state_lock()
    _drop_functions()
    _install_outbox_lifecycle_guard(allow_reconciled_gmail=False)
    _drop_append_only_guards()
    op.drop_index(
        "ix_gmail_send_review_evidence_action_payload",
        table_name="gmail_send_review_evidence",
        schema="careerops",
    )
    op.drop_table("gmail_send_command_receipts", schema="careerops")
    op.drop_table("gmail_send_review_evidence", schema="careerops")
    op.drop_index(
        "ix_gmail_send_reconciliation_jobs_available_at",
        table_name="gmail_send_reconciliation_jobs",
        schema="careerops",
    )
    op.drop_table("gmail_send_reconciliation_jobs", schema="careerops")
    op.drop_index(
        "ix_gmail_send_reservations_account_created_at",
        table_name="gmail_send_reservations",
        schema="careerops",
    )
    op.drop_table("gmail_send_reservations", schema="careerops")
    op.drop_index(
        "ix_gmail_send_drafts_account_created_at",
        table_name="gmail_send_drafts",
        schema="careerops",
    )
    op.drop_table("gmail_send_drafts", schema="careerops")
    op.drop_index(
        "ix_gmail_send_accounts_owner_updated_at",
        table_name="gmail_send_accounts",
        schema="careerops",
    )
    op.drop_table("gmail_send_accounts", schema="careerops")
