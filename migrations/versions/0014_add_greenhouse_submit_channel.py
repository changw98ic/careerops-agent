"""add reviewed greenhouse submit channel boundary

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-21
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GREENHOUSE_SCOPE = "greenhouse:applications.create"
GREENHOUSE_HOST = "boards-api.greenhouse.io"
GREENHOUSE_CHANNEL = "greenhouse:job-board"
GREENHOUSE_ADAPTER = "greenhouse-job-board"
GREENHOUSE_FIXTURE = "greenhouse-submit.v1"
GREENHOUSE_RELEASE = "greenhouse-submit-release.v1"

APPEND_ONLY_TABLES = (
    "greenhouse_submit_command_receipts",
    "greenhouse_submit_reconciliation_evidence",
    "greenhouse_submit_reconciliation_reviews",
    "greenhouse_submit_review_evidence",
)

_GREENHOUSE_TABLES = (
    "greenhouse_submit_accounts",
    "greenhouse_submit_command_receipts",
    "greenhouse_submit_reconciliation_evidence",
    "greenhouse_submit_reconciliation_jobs",
    "greenhouse_submit_reconciliation_reviews",
    "greenhouse_submit_review_evidence",
)

_REGISTER_FUNCTION = "careerops.greenhouse_submit_register_account"
_REGISTER_SIGNATURE = "(uuid, uuid, text, text, text, text, text, uuid, text, integer, text, text, timestamp with time zone, timestamp with time zone, text, text, text, text, integer, text, text)"
_CREATE_DRAFT_FUNCTION = "careerops.greenhouse_submit_create_draft"
_CREATE_DRAFT_SIGNATURE = "(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)"
_REVIEW_DRAFT_FUNCTION = "careerops.greenhouse_submit_review_draft"
_REVIEW_DRAFT_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, text, text, text, text, uuid, uuid, text, text, timestamp with time zone, text, text)"
_RESERVE_FUNCTION = "careerops.greenhouse_submit_reserve_and_enqueue"
_RESERVE_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, text, text, text, text, uuid, text, text, uuid, uuid, text, text, text, text, text)"
_LIST_ACCOUNTS_FUNCTION = "careerops.list_greenhouse_submit_accounts"
_LIST_ACCOUNTS_SIGNATURE = "(uuid)"
_GET_ACCOUNT_FUNCTION = "careerops.get_greenhouse_submit_account"
_GET_ACCOUNT_SIGNATURE = "(uuid, uuid)"
_LIST_CASES_FUNCTION = "careerops.list_greenhouse_submit_reconciliation_cases"
_LIST_CASES_SIGNATURE = "(uuid)"
_GET_CASE_FUNCTION = "careerops.get_greenhouse_submit_reconciliation_case"
_GET_CASE_SIGNATURE = "(uuid, uuid)"
_PREPARE_FUNCTION = "careerops.greenhouse_submit_prepare_outbox_event"
_PREPARE_SIGNATURE = "(uuid, text, uuid)"
_CLAIM_OUTBOX_FUNCTION = "careerops.claim_greenhouse_submit_outbox_events"
_CLAIM_OUTBOX_SIGNATURE = "(text, integer, integer)"
_MARK_OUTBOX_PUBLISHED_FUNCTION = "careerops.mark_greenhouse_submit_outbox_published"
_MARK_OUTBOX_PUBLISHED_SIGNATURE = "(uuid, text, uuid)"
_RELEASE_OUTBOX_FUNCTION = "careerops.release_greenhouse_submit_outbox_event"
_RELEASE_OUTBOX_SIGNATURE = "(uuid, text, uuid, timestamp with time zone, text, boolean)"
_RECORD_RECEIPT_FUNCTION = "careerops.greenhouse_submit_record_accepted_unverified"
_RECORD_RECEIPT_SIGNATURE = "(uuid, text, uuid, text, timestamp with time zone, uuid, integer, text, text, text, text, text, text, text, text, bigint, text)"
_RECORD_AMBIGUITY_FUNCTION = "careerops.greenhouse_submit_record_ambiguity"
_RECORD_AMBIGUITY_SIGNATURE = "(uuid, text, uuid, text, text, text, text, text, text, text, text, text, text, bigint, text, text, text, text)"
_RECORD_REJECTED_FUNCTION = "careerops.greenhouse_submit_record_rejected"
_RECORD_REJECTED_SIGNATURE = "(uuid, text, uuid, text, text[], text, integer, text, text, text, text, text, text, text, text, bigint, text, text, text, text)"
_RECORD_PREPOST_FAILURE_FUNCTION = "careerops.greenhouse_submit_record_prepost_failure"
_RECORD_PREPOST_FAILURE_SIGNATURE = "(uuid, text, uuid, text)"
_SUBMIT_RECONCILIATION_EVIDENCE_FUNCTION = (
    "careerops.submit_greenhouse_submit_reconciliation_evidence"
)
_SUBMIT_RECONCILIATION_EVIDENCE_SIGNATURE = (
    "(uuid, uuid, text, text, text, timestamp with time zone, text, text, text)"
)
_REVIEW_RECONCILIATION_EVIDENCE_FUNCTION = (
    "careerops.review_greenhouse_submit_reconciliation_evidence"
)
_REVIEW_RECONCILIATION_EVIDENCE_SIGNATURE = (
    "(uuid, uuid, uuid, text, text, text, text, text, uuid, text, text, text, text)"
)
_RECORD_AMBIGUITY_REVOKE_PUBLIC_SQL = """
DO $$
DECLARE
    v_target regprocedure;
    v_count integer;
BEGIN
    SELECT count(*), max(proc.oid::regprocedure)
    INTO v_count, v_target
    FROM pg_proc AS proc
    JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname = 'careerops'
      AND proc.proname = 'greenhouse_submit_record_ambiguity';
    IF v_count <> 1 OR v_target IS NULL THEN
        RAISE EXCEPTION 'expected exactly one greenhouse ambiguity function, found %', v_count;
    END IF;
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', v_target);
END $$;
"""
_RECORD_AMBIGUITY_GRANT_SENDER_SQL = """
DO $$
DECLARE
    v_target regprocedure;
    v_count integer;
BEGIN
    SELECT count(*), max(proc.oid::regprocedure)
    INTO v_count, v_target
    FROM pg_proc AS proc
    JOIN pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname = 'careerops'
      AND proc.proname = 'greenhouse_submit_record_ambiguity';
    IF v_count <> 1 OR v_target IS NULL THEN
        RAISE EXCEPTION 'expected exactly one greenhouse ambiguity function, found %', v_count;
    END IF;
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO careerops_greenhouse_sender', v_target);
END $$;
"""

_API_GRANTS = (
    f"GRANT EXECUTE ON FUNCTION {_REGISTER_FUNCTION}{_REGISTER_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_CREATE_DRAFT_FUNCTION}{_CREATE_DRAFT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_REVIEW_DRAFT_FUNCTION}{_REVIEW_DRAFT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_RESERVE_FUNCTION}{_RESERVE_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_LIST_ACCOUNTS_FUNCTION}{_LIST_ACCOUNTS_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_GET_ACCOUNT_FUNCTION}{_GET_ACCOUNT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_LIST_CASES_FUNCTION}{_LIST_CASES_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_GET_CASE_FUNCTION}{_GET_CASE_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_SUBMIT_RECONCILIATION_EVIDENCE_FUNCTION}{_SUBMIT_RECONCILIATION_EVIDENCE_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_REVIEW_RECONCILIATION_EVIDENCE_FUNCTION}{_REVIEW_RECONCILIATION_EVIDENCE_SIGNATURE} TO careerops_api",
)

_SENDER_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_CLAIM_OUTBOX_FUNCTION}{_CLAIM_OUTBOX_SIGNATURE} TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_MARK_OUTBOX_PUBLISHED_FUNCTION}{_MARK_OUTBOX_PUBLISHED_SIGNATURE} TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_RELEASE_OUTBOX_FUNCTION}{_RELEASE_OUTBOX_SIGNATURE} TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE} TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECORD_RECEIPT_FUNCTION}{_RECORD_RECEIPT_SIGNATURE} TO careerops_greenhouse_sender",
    f"GRANT EXECUTE ON FUNCTION {_RECORD_REJECTED_FUNCTION}{_RECORD_REJECTED_SIGNATURE} TO careerops_greenhouse_sender",
    _RECORD_AMBIGUITY_GRANT_SENDER_SQL,
    f"GRANT EXECUTE ON FUNCTION {_RECORD_PREPOST_FAILURE_FUNCTION}{_RECORD_PREPOST_FAILURE_SIGNATURE} TO careerops_greenhouse_sender",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.greenhouse_submit_command_receipts, careerops.greenhouse_submit_reconciliation_jobs TO careerops_readonly",
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


def _install_greenhouse_sender_role() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_greenhouse_sender') THEN
                    CREATE ROLE careerops_greenhouse_sender NOLOGIN;
                END IF;
            END $$;
            """
        )
    )


def _drop_greenhouse_sender_role_if_empty() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_greenhouse_sender')
                   AND NOT EXISTS (
                       SELECT 1
                       FROM pg_auth_members
                       WHERE roleid = 'careerops_greenhouse_sender'::regrole
                   ) THEN
                    DROP ROLE careerops_greenhouse_sender;
                END IF;
            END $$;
            """
        )
    )


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


def _install_cap_reservation_insert_guard(*, include_greenhouse: bool) -> None:
    greenhouse_branch = (
        f"""
                ELSIF NEW.channel = '{GREENHOUSE_CHANNEL}'
                   OR NEW.target_host = '{GREENHOUSE_HOST}'
                   OR NEW.adapter_id = '{GREENHOUSE_ADAPTER}'
                   OR NEW.fixture_id = '{GREENHOUSE_FIXTURE}' THEN
                    IF intent_action_kind <> 'submit_application'
                       OR NEW.channel <> '{GREENHOUSE_CHANNEL}'
                       OR NEW.target_host <> '{GREENHOUSE_HOST}'
                       OR NEW.adapter_id <> '{GREENHOUSE_ADAPTER}'
                       OR NEW.fixture_id <> '{GREENHOUSE_FIXTURE}' THEN
                        RAISE EXCEPTION 'greenhouse submit reservation must use exact greenhouse channel'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NOT COALESCE(grant_allowed_action_kinds @> jsonb_build_array('submit_application'), false)
                       OR NOT COALESCE(grant_allowed_channels @> jsonb_build_array('{GREENHOUSE_CHANNEL}'), false)
                       OR NOT COALESCE(grant_allowed_target_hosts @> jsonb_build_array('{GREENHOUSE_HOST}'), false) THEN
                        RAISE EXCEPTION 'greenhouse submit reservation exceeds grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                    IF payload_target ->> 'target_host' IS DISTINCT FROM '{GREENHOUSE_HOST}'
                       OR payload_target ->> 'channel' IS DISTINCT FROM '{GREENHOUSE_CHANNEL}'
                       OR COALESCE((payload_target ->> 'job_id_sha256') !~ '^[a-f0-9]{{64}}$', true)
                       OR COALESCE(NEW.company_key !~ '^[a-f0-9]{{64}}$', true)
                       OR payload_target ->> 'board_token_sha256' IS NULL
                       OR payload_target ->> 'schema_sha256' IS NULL
                       OR payload_target ->> 'candidate_material_sha256' IS NULL THEN
                        RAISE EXCEPTION 'greenhouse submit reservation target does not match payload'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NOT COALESCE(grant_material_hashes @> jsonb_build_array(payload_target ->> 'schema_sha256'), false)
                       OR NOT COALESCE(grant_material_hashes @> jsonb_build_array(payload_target ->> 'candidate_material_sha256'), false) THEN
                        RAISE EXCEPTION 'greenhouse submit required material is outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                    IF jsonb_typeof(payload_attachment_refs) <> 'array' THEN
                        RAISE EXCEPTION 'greenhouse submit attachment refs must be an array'
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
                        RAISE EXCEPTION 'greenhouse submit includes material outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
        """
        if include_greenhouse
        else ""
    )
    op.execute(
        sa.text(
            f"""
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

                SELECT authz.authorization_outcome, authz.payload_hash,
                       authz.expires_at, decision_record.decision
                INTO authorization_outcome, authorized_payload_hash,
                     authorized_expires_at, policy_decision
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
                    IF NOT COALESCE(grant_material_hashes @> jsonb_build_array(payload_target ->> 'body_sha256'), false)
                       OR COALESCE((payload_target ->> 'grant_material_hash') !~ '^[a-f0-9]{{64}}$', true)
                       OR NOT COALESCE(grant_material_hashes @> jsonb_build_array(payload_target ->> 'grant_material_hash'), false) THEN
                        RAISE EXCEPTION 'gmail send required material is outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                    IF jsonb_typeof(payload_attachment_refs) <> 'array'
                       OR EXISTS (
                           SELECT 1
                           FROM jsonb_array_elements(payload_attachment_refs) AS attachment(value)
                           WHERE jsonb_typeof(attachment.value) <> 'object'
                              OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                              OR NOT COALESCE(grant_material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'), false)
                       ) THEN
                        RAISE EXCEPTION 'gmail send reservation includes material outside grant scope'
                            USING ERRCODE = '23514';
                    END IF;
                {greenhouse_branch}
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
                       OR jsonb_array_length(payload_attachment_refs) = 0
                       OR EXISTS (
                           SELECT 1
                           FROM jsonb_array_elements(payload_attachment_refs) AS attachment(value)
                           WHERE jsonb_typeof(attachment.value) <> 'object'
                              OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                              OR NOT COALESCE(grant_material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'), false)
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


def _drop_functions() -> None:
    for function, signature in (
        (_GET_CASE_FUNCTION, _GET_CASE_SIGNATURE),
        (_LIST_CASES_FUNCTION, _LIST_CASES_SIGNATURE),
        (_GET_ACCOUNT_FUNCTION, _GET_ACCOUNT_SIGNATURE),
        (_LIST_ACCOUNTS_FUNCTION, _LIST_ACCOUNTS_SIGNATURE),
        (_REVIEW_RECONCILIATION_EVIDENCE_FUNCTION, _REVIEW_RECONCILIATION_EVIDENCE_SIGNATURE),
        (_SUBMIT_RECONCILIATION_EVIDENCE_FUNCTION, _SUBMIT_RECONCILIATION_EVIDENCE_SIGNATURE),
        (_RECORD_PREPOST_FAILURE_FUNCTION, _RECORD_PREPOST_FAILURE_SIGNATURE),
        (_RECORD_REJECTED_FUNCTION, _RECORD_REJECTED_SIGNATURE),
        (_RELEASE_OUTBOX_FUNCTION, _RELEASE_OUTBOX_SIGNATURE),
        (_MARK_OUTBOX_PUBLISHED_FUNCTION, _MARK_OUTBOX_PUBLISHED_SIGNATURE),
        (_CLAIM_OUTBOX_FUNCTION, _CLAIM_OUTBOX_SIGNATURE),
        (_RECORD_AMBIGUITY_FUNCTION, _RECORD_AMBIGUITY_SIGNATURE),
        (_RECORD_RECEIPT_FUNCTION, _RECORD_RECEIPT_SIGNATURE),
        (_PREPARE_FUNCTION, _PREPARE_SIGNATURE),
        (_RESERVE_FUNCTION, _RESERVE_SIGNATURE),
        (_REVIEW_DRAFT_FUNCTION, _REVIEW_DRAFT_SIGNATURE),
        (_CREATE_DRAFT_FUNCTION, _CREATE_DRAFT_SIGNATURE),
        (_REGISTER_FUNCTION, _REGISTER_SIGNATURE),
    ):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}{signature}"))


def _create_tables() -> None:
    op.create_table(
        "greenhouse_submit_accounts",
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
        sa.Column("provider", sa.String(24), server_default="greenhouse_submit", nullable=False),
        sa.Column("account_subject", sa.Text(), nullable=False),
        sa.Column("opaque_broker_handle", sa.Text(), nullable=False, unique=True),
        sa.Column("authorized_integration_source", sa.Text(), nullable=False),
        sa.Column("employer_id", sa.Text(), nullable=False),
        sa.Column("board_token_sha256", sa.String(64), nullable=False),
        sa.Column(
            "operator_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("credential_profile_id", sa.Text(), nullable=False),
        sa.Column("credential_profile_version", sa.Integer(), nullable=False),
        sa.Column("credential_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("credential_profile_status", sa.Text(), nullable=False),
        sa.Column("credential_profile_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credential_profile_revoked_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), server_default="disabled", nullable=False),
        sa.Column("daily_submit_limit", sa.Integer(), nullable=False),
        sa.Column("employer_authorization_evidence_sha256", sa.String(64), nullable=False),
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
            name="uq_greenhouse_submit_accounts_owner_provider_subject",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_greenhouse_submit_accounts_owner_idempotency_key",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "employer_id",
            "credential_profile_id",
            "credential_profile_version",
            name="uq_greenhouse_submit_accounts_owner_profile_version",
        ),
        sa.CheckConstraint("provider = 'greenhouse_submit'", name="provider_values"),
        sa.CheckConstraint(
            "authorized_integration_source IN ('employer_api_profile', 'partner_integration')",
            name="authorized_source_values",
        ),
        sa.CheckConstraint("btrim(employer_id) <> ''", name="employer_id_nonempty"),
        sa.CheckConstraint(
            "board_token_sha256 ~ '^[a-f0-9]{64}$'", name="board_token_sha256_format"
        ),
        sa.CheckConstraint(
            "credential_profile_version > 0", name="credential_profile_version_positive"
        ),
        sa.CheckConstraint(
            "credential_fingerprint_sha256 ~ '^[a-f0-9]{64}$'",
            name="credential_fingerprint_sha256_format",
        ),
        sa.CheckConstraint(
            "credential_profile_status IN ('active', 'revoked', 'expired', 'disabled')",
            name="credential_profile_status_values",
        ),
        sa.CheckConstraint(
            "status IN ('disabled', 'active', 'paused', 'revoked', 'blocked')",
            name="status_values",
        ),
        sa.CheckConstraint(
            "daily_submit_limit BETWEEN 1 AND 500", name="daily_submit_limit_bounds"
        ),
        sa.CheckConstraint(
            "employer_authorization_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="employer_authorization_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "credential_store_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="credential_store_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "release_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="release_evidence_sha256_format",
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint(
            "btrim(account_subject) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_greenhouse_submit_accounts_owner_updated_at",
        "greenhouse_submit_accounts",
        ["owner_user_id", "updated_at"],
        schema="careerops",
    )
    op.create_table(
        "greenhouse_submit_review_evidence",
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
            sa.ForeignKey("careerops.greenhouse_submit_accounts.id", ondelete="RESTRICT"),
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
            "candidate_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.candidates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
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
        sa.Column("board_token_sha256", sa.String(64), nullable=False),
        sa.Column("account_subject", sa.Text(), nullable=False),
        sa.Column("authorized_integration_source", sa.Text(), nullable=False),
        sa.Column("employer_id", sa.Text(), nullable=False),
        sa.Column("opaque_broker_handle_sha256", sa.String(64), nullable=False),
        sa.Column("credential_profile_id", sa.Text(), nullable=False),
        sa.Column("credential_profile_version", sa.Integer(), nullable=False),
        sa.Column("credential_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("credential_profile_status", sa.Text(), nullable=False),
        sa.Column("credential_profile_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_post_id", sa.Text(), nullable=False),
        sa.Column("internal_job_id", sa.Text(), nullable=False),
        sa.Column("job_id_sha256", sa.String(64), nullable=False),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("raw_response_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_schema_sha256", sa.String(64), nullable=False),
        sa.Column("job_updated_at", sa.Text(), nullable=False),
        sa.Column("application_deadline", sa.Text()),
        sa.Column("job_identity_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_material_sha256", sa.String(64), nullable=False),
        sa.Column("answer_sha256", sa.String(64), nullable=False),
        sa.Column(
            "attachment_sha256s",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("review_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("review_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("grant_version_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("release_version", sa.Text(), nullable=False),
        sa.Column("release_evidence_sha256", sa.String(64), nullable=False),
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
            name="fk_greenhouse_submit_review_evidence_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_greenhouse_submit_review_evidence_owner_idempotency",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "board_token_sha256",
            "job_post_id",
            name="uq_greenhouse_submit_review_evidence_candidate_board_job",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "board_token_sha256",
            "internal_job_id",
            name="uq_greenhouse_submit_review_evidence_candidate_board_internal",
        ),
        sa.CheckConstraint("payload_hash ~ '^[a-f0-9]{64}$'", name="payload_hash_format"),
        sa.CheckConstraint(
            "board_token_sha256 ~ '^[a-f0-9]{64}$'", name="board_token_sha256_format"
        ),
        sa.CheckConstraint(
            "authorized_integration_source IN ('employer_api_profile', 'partner_integration')",
            name="authorized_source_values",
        ),
        sa.CheckConstraint("btrim(employer_id) <> ''", name="employer_id_nonempty"),
        sa.CheckConstraint(
            "opaque_broker_handle_sha256 ~ '^[a-f0-9]{64}$'",
            name="opaque_broker_handle_sha256_format",
        ),
        sa.CheckConstraint(
            "credential_profile_version > 0", name="credential_profile_version_positive"
        ),
        sa.CheckConstraint(
            "credential_fingerprint_sha256 ~ '^[a-f0-9]{64}$'",
            name="credential_fingerprint_sha256_format",
        ),
        sa.CheckConstraint(
            "credential_profile_status = 'active'",
            name="credential_profile_status_active",
        ),
        sa.CheckConstraint("btrim(job_post_id) <> ''", name="job_post_id_nonempty"),
        sa.CheckConstraint("btrim(internal_job_id) <> ''", name="internal_job_id_nonempty"),
        sa.CheckConstraint("job_id_sha256 ~ '^[a-f0-9]{64}$'", name="job_id_sha256_format"),
        sa.CheckConstraint("schema_sha256 ~ '^[a-f0-9]{64}$'", name="schema_sha256_format"),
        sa.CheckConstraint(
            "raw_response_sha256 ~ '^[a-f0-9]{64}$'",
            name="raw_response_sha256_format",
        ),
        sa.CheckConstraint(
            "normalized_schema_sha256 ~ '^[a-f0-9]{64}$'",
            name="normalized_schema_sha256_format",
        ),
        sa.CheckConstraint("btrim(job_updated_at) <> ''", name="job_updated_at_nonempty"),
        sa.CheckConstraint(
            "application_deadline IS NULL OR btrim(application_deadline) <> ''",
            name="application_deadline_optional_nonempty",
        ),
        sa.CheckConstraint(
            "job_identity_sha256 ~ '^[a-f0-9]{64}$'",
            name="job_identity_sha256_format",
        ),
        sa.CheckConstraint(
            "candidate_material_sha256 ~ '^[a-f0-9]{64}$'",
            name="candidate_material_sha256_format",
        ),
        sa.CheckConstraint("answer_sha256 ~ '^[a-f0-9]{64}$'", name="answer_sha256_format"),
        sa.CheckConstraint(
            "review_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="review_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "review_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="review_snapshot_sha256_format",
        ),
        sa.CheckConstraint(
            f"adapter_id = '{GREENHOUSE_ADAPTER}' AND adapter_version = '{GREENHOUSE_FIXTURE}'",
            name="adapter_identity_exact",
        ),
        sa.CheckConstraint(
            f"release_version = '{GREENHOUSE_RELEASE}'",
            name="release_version_exact",
        ),
        sa.CheckConstraint(
            "release_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="release_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attachment_sha256s) = 'array'",
            name="attachment_sha256s_array",
        ),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_greenhouse_submit_review_evidence_action_payload",
        "greenhouse_submit_review_evidence",
        ["action_intent_id", "payload_version_id", "created_at"],
        schema="careerops",
    )
    op.create_table(
        "greenhouse_submit_reconciliation_jobs",
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(16), server_default="required", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("resolution_evidence_sha256", sa.String(64)),
        sa.Column("resolution_reviewed_by_user_id", sa.Uuid()),
        sa.Column("resolution_source", sa.Text()),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('required', 'confirmed', 'resolved_absent', 'blocked')",
            name="status_values",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        sa.CheckConstraint(
            "lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL",
            name="lease_state_consistent",
        ),
        sa.CheckConstraint(
            "resolution_evidence_sha256 IS NULL OR resolution_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="resolution_evidence_sha256_format",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_greenhouse_submit_reconciliation_jobs_available_at",
        "greenhouse_submit_reconciliation_jobs",
        ["status", "available_at"],
        schema="careerops",
    )
    op.create_table(
        "greenhouse_submit_reconciliation_evidence",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("evidence_source", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("observed_status", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_greenhouse_reconciliation_evidence_owner_idempotency",
        ),
        sa.CheckConstraint(
            "evidence_source IN ('employer_admin', 'recruiting_webhook', 'manual_employer_system')",
            name="evidence_source_values",
        ),
        sa.CheckConstraint("evidence_sha256 ~ '^[a-f0-9]{64}$'", name="evidence_sha256_format"),
        sa.CheckConstraint(
            "observed_status IN ('accepted_unverified', 'ambiguous', 'provider_rejected', 'reconciliation_required')",
            name="observed_status_values",
        ),
        sa.CheckConstraint("reason_code ~ '^[A-Z0-9_]{1,64}$'", name="reason_code_format"),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        schema="careerops",
    )
    op.create_table(
        "greenhouse_submit_reconciliation_reviews",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "evidence_id",
            sa.Uuid(),
            sa.ForeignKey(
                "careerops.greenhouse_submit_reconciliation_evidence.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reviewed_employer_authorization_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("review_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_greenhouse_reconciliation_reviews_owner_idempotency",
        ),
        sa.CheckConstraint("evidence_sha256 ~ '^[a-f0-9]{64}$'", name="evidence_sha256_format"),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'resolved_absent', 'blocked')",
            name="decision_values",
        ),
        sa.CheckConstraint(
            "reviewed_employer_authorization_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="reviewed_employer_authorization_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "review_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="review_snapshot_sha256_format",
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        schema="careerops",
    )
    op.create_table(
        "greenhouse_submit_command_receipts",
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
            sa.ForeignKey("careerops.greenhouse_submit_accounts.id", ondelete="RESTRICT"),
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
            name="uq_greenhouse_submit_command_receipts_identity",
        ),
        sa.CheckConstraint(
            "command_kind IN ('create_draft', 'review_draft', 'register_account', 'reserve_and_enqueue')",
            name="command_kind_values",
        ),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
        sa.CheckConstraint(
            "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name="text_nonempty",
        ),
        schema="careerops",
    )


def _install_register_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.greenhouse_submit_register_account(
                p_owner_user_id uuid,
                p_candidate_id uuid,
                p_account_subject text,
                p_opaque_broker_handle text,
                p_authorized_integration_source text,
                p_employer_id text,
                p_board_token text,
                p_operator_user_id uuid,
                p_credential_profile_id text,
                p_credential_profile_version integer,
                p_credential_fingerprint_sha256 text,
                p_credential_profile_status text,
                p_credential_profile_expires_at timestamp with time zone,
                p_credential_profile_revoked_at timestamp with time zone,
                p_employer_authorization_evidence_sha256 text,
                p_credential_store_evidence_sha256 text,
                p_release_evidence_sha256 text,
                p_status text,
                p_daily_submit_limit integer,
                p_idempotency_key text,
                p_trace_id text
            )
            RETURNS TABLE (account_id uuid, status text, receipt_state text)
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_now timestamp with time zone := CURRENT_TIMESTAMP;
                v_request_sha256 text;
                v_existing careerops.greenhouse_submit_accounts%ROWTYPE;
                v_account_id uuid;
                v_board_token_sha256 text;
            BEGIN
                IF p_owner_user_id IS NULL OR p_candidate_id IS NULL
                   OR p_account_subject IS NULL OR btrim(p_account_subject) = ''
                   OR p_opaque_broker_handle IS NULL OR btrim(p_opaque_broker_handle) = ''
                   OR p_authorized_integration_source NOT IN ('employer_api_profile', 'partner_integration')
                   OR p_employer_id IS NULL OR btrim(p_employer_id) = ''
                   OR p_board_token IS NULL OR btrim(p_board_token) = ''
                   OR lower(btrim(p_board_token)) LIKE 'internal%'
                   OR p_operator_user_id IS NULL
                   OR p_operator_user_id <> p_owner_user_id
                   OR p_credential_profile_id IS NULL OR btrim(p_credential_profile_id) = ''
                   OR p_credential_profile_version <= 0
                   OR p_credential_fingerprint_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_credential_profile_status <> 'active'
                   OR p_credential_profile_expires_at IS NULL
                   OR p_credential_profile_expires_at <= v_now
                   OR p_credential_profile_revoked_at IS NOT NULL
                   OR p_status NOT IN ('disabled', 'active')
                   OR p_daily_submit_limit <= 0
                   OR p_employer_authorization_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_credential_store_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_release_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'greenhouse submit registration payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                v_board_token_sha256 := encode(sha256(convert_to(btrim(p_board_token), 'UTF8')), 'hex');
                v_request_sha256 := encode(sha256(convert_to(jsonb_build_object(
                    'owner_user_id', p_owner_user_id::text,
                    'candidate_id', p_candidate_id::text,
                    'account_subject', p_account_subject,
                    'opaque_broker_handle', p_opaque_broker_handle,
                    'authorized_integration_source', p_authorized_integration_source,
                    'employer_id', p_employer_id,
                    'board_token_sha256', v_board_token_sha256,
                    'operator_user_id', p_operator_user_id::text,
                    'credential_profile_id', p_credential_profile_id,
                    'credential_profile_version', p_credential_profile_version,
                    'credential_fingerprint_sha256', p_credential_fingerprint_sha256,
                    'credential_profile_status', p_credential_profile_status,
                    'credential_profile_expires_at', p_credential_profile_expires_at::text,
                    'employer_authorization_evidence_sha256', p_employer_authorization_evidence_sha256,
                    'credential_store_evidence_sha256', p_credential_store_evidence_sha256,
                    'release_evidence_sha256', p_release_evidence_sha256,
                    'status', p_status,
                    'daily_submit_limit', p_daily_submit_limit
                )::text, 'UTF8')), 'hex');
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_accounts AS account
                WHERE account.owner_user_id = p_owner_user_id
                  AND account.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'greenhouse submit registration idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT v_existing.id, v_existing.status::text, 'replayed'::text;
                    RETURN;
                END IF;
                v_account_id := gen_random_uuid();
                INSERT INTO careerops.greenhouse_submit_accounts (
                    id, owner_user_id, candidate_id, account_subject,
                    opaque_broker_handle, authorized_integration_source,
                    employer_id, board_token_sha256, operator_user_id,
                    credential_profile_id, credential_profile_version,
                    credential_fingerprint_sha256, credential_profile_status,
                    credential_profile_expires_at, credential_profile_revoked_at,
                    status, daily_submit_limit, employer_authorization_evidence_sha256,
                    credential_store_evidence_sha256, release_evidence_sha256,
                    request_sha256, idempotency_key, trace_id
                ) VALUES (
                    v_account_id, p_owner_user_id, p_candidate_id,
                    p_account_subject, p_opaque_broker_handle,
                    p_authorized_integration_source, p_employer_id,
                    v_board_token_sha256, p_operator_user_id,
                    p_credential_profile_id, p_credential_profile_version,
                    p_credential_fingerprint_sha256, p_credential_profile_status,
                    p_credential_profile_expires_at,
                    p_credential_profile_revoked_at, p_status,
                    p_daily_submit_limit, p_employer_authorization_evidence_sha256,
                    p_credential_store_evidence_sha256, p_release_evidence_sha256, v_request_sha256,
                    p_idempotency_key, p_trace_id
                );
                INSERT INTO careerops.greenhouse_submit_command_receipts (
                    id, owner_user_id, account_id, command_kind, idempotency_key,
                    request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, v_account_id, 'register_account',
                    p_idempotency_key, v_request_sha256,
                    jsonb_build_object('account_id', v_account_id::text, 'status', p_status),
                    p_trace_id
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


def _install_create_review_functions() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.list_greenhouse_submit_accounts(p_owner_user_id uuid)
            RETURNS TABLE (
                account_id uuid,
                candidate_id uuid,
                account_subject text,
                employer_id text,
                board_token_sha256 text,
                status text,
                credential_profile_id text,
                credential_profile_version integer,
                credential_fingerprint_sha256 text,
                credential_profile_status text,
                credential_profile_expires_at timestamp with time zone,
                employer_authorization_evidence_sha256 text,
                daily_submit_limit integer,
                created_at timestamp with time zone,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT account.id, account.candidate_id, account.account_subject, account.employer_id,
                       account.board_token_sha256, account.status,
                       account.credential_profile_id, account.credential_profile_version,
                       account.credential_fingerprint_sha256,
                       account.credential_profile_status,
                       account.credential_profile_expires_at,
                       account.employer_authorization_evidence_sha256,
                       account.daily_submit_limit,
                       account.created_at, account.updated_at
                FROM careerops.greenhouse_submit_accounts AS account
                WHERE account.owner_user_id = p_owner_user_id
                ORDER BY account.updated_at DESC, account.id;
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_LIST_ACCOUNTS_FUNCTION}{_LIST_ACCOUNTS_SIGNATURE} FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.get_greenhouse_submit_account(
                p_owner_user_id uuid,
                p_account_id uuid
            )
            RETURNS TABLE (
                account_id uuid,
                candidate_id uuid,
                account_subject text,
                employer_id text,
                board_token_sha256 text,
                status text,
                credential_profile_id text,
                credential_profile_version integer,
                credential_fingerprint_sha256 text,
                credential_profile_status text,
                credential_profile_expires_at timestamp with time zone,
                employer_authorization_evidence_sha256 text,
                daily_submit_limit integer,
                created_at timestamp with time zone,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT account.id, account.candidate_id, account.account_subject, account.employer_id,
                       account.board_token_sha256, account.status,
                       account.credential_profile_id, account.credential_profile_version,
                       account.credential_fingerprint_sha256,
                       account.credential_profile_status,
                       account.credential_profile_expires_at,
                       account.employer_authorization_evidence_sha256,
                       account.daily_submit_limit,
                       account.created_at, account.updated_at
                FROM careerops.greenhouse_submit_accounts AS account
                WHERE account.owner_user_id = p_owner_user_id
                  AND account.id = p_account_id;
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_GET_ACCOUNT_FUNCTION}{_GET_ACCOUNT_SIGNATURE} FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.list_greenhouse_submit_reconciliation_cases(p_owner_user_id uuid)
            RETURNS TABLE (
                reconciliation_case_id uuid,
                account_id uuid,
                action_intent_id uuid,
                payload_version_id uuid,
                reservation_key text,
                reconciliation_key text,
                status text,
                evidence_sha256 text,
                evidence_source text,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT job.outbox_event_id, review.account_id,
                       event.action_intent_id, event.payload_version_id,
                       reservation.reservation_key, reservation.reconciliation_key,
                       CASE
                           WHEN job.status = 'required' THEN
                               COALESCE(submitted.observed_status, 'reconciliation_required')
                           ELSE job.status
                       END,
                       COALESCE(job.resolution_evidence_sha256, submitted.evidence_sha256),
                       COALESCE(job.resolution_source, submitted.evidence_source),
                       job.updated_at
                FROM careerops.greenhouse_submit_reconciliation_jobs AS job
                JOIN careerops.outbox_events AS event ON event.id = job.outbox_event_id
                JOIN LATERAL (
                    SELECT evidence.account_id
                    FROM careerops.greenhouse_submit_review_evidence AS evidence
                    WHERE evidence.action_intent_id = event.action_intent_id
                      AND evidence.payload_version_id = event.payload_version_id
                      AND evidence.owner_user_id = p_owner_user_id
                    ORDER BY evidence.sequence DESC
                    LIMIT 1
                ) AS review ON true
                JOIN careerops.autopilot_cap_reservations AS reservation
                  ON reservation.action_intent_id = event.action_intent_id
                 AND reservation.payload_version_id = event.payload_version_id
                 AND reservation.channel = 'greenhouse:job-board'
                LEFT JOIN LATERAL (
                    SELECT submitted_evidence.observed_status,
                           submitted_evidence.evidence_sha256,
                           submitted_evidence.evidence_source
                    FROM careerops.greenhouse_submit_reconciliation_evidence AS submitted_evidence
                    WHERE submitted_evidence.outbox_event_id = job.outbox_event_id
                      AND submitted_evidence.owner_user_id = p_owner_user_id
                    ORDER BY submitted_evidence.sequence DESC
                    LIMIT 1
                ) AS submitted ON true
                ORDER BY job.updated_at DESC, job.outbox_event_id;
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_LIST_CASES_FUNCTION}{_LIST_CASES_SIGNATURE} FROM PUBLIC")
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.get_greenhouse_submit_reconciliation_case(
                p_owner_user_id uuid,
                p_event_id uuid
            )
            RETURNS TABLE (
                reconciliation_case_id uuid,
                account_id uuid,
                action_intent_id uuid,
                payload_version_id uuid,
                reservation_key text,
                reconciliation_key text,
                status text,
                evidence_sha256 text,
                evidence_source text,
                updated_at timestamp with time zone
            )
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
                SELECT job.outbox_event_id, review.account_id,
                       event.action_intent_id, event.payload_version_id,
                       reservation.reservation_key, reservation.reconciliation_key,
                       CASE
                           WHEN job.status = 'required' THEN
                               COALESCE(submitted.observed_status, 'reconciliation_required')
                           ELSE job.status
                       END,
                       COALESCE(job.resolution_evidence_sha256, submitted.evidence_sha256),
                       COALESCE(job.resolution_source, submitted.evidence_source),
                       job.updated_at
                FROM careerops.greenhouse_submit_reconciliation_jobs AS job
                JOIN careerops.outbox_events AS event ON event.id = job.outbox_event_id
                JOIN LATERAL (
                    SELECT evidence.account_id
                    FROM careerops.greenhouse_submit_review_evidence AS evidence
                    WHERE evidence.action_intent_id = event.action_intent_id
                      AND evidence.payload_version_id = event.payload_version_id
                      AND evidence.owner_user_id = p_owner_user_id
                    ORDER BY evidence.sequence DESC
                    LIMIT 1
                ) AS review ON true
                JOIN careerops.autopilot_cap_reservations AS reservation
                  ON reservation.action_intent_id = event.action_intent_id
                 AND reservation.payload_version_id = event.payload_version_id
                 AND reservation.channel = 'greenhouse:job-board'
                LEFT JOIN LATERAL (
                    SELECT submitted_evidence.observed_status,
                           submitted_evidence.evidence_sha256,
                           submitted_evidence.evidence_source
                    FROM careerops.greenhouse_submit_reconciliation_evidence AS submitted_evidence
                    WHERE submitted_evidence.outbox_event_id = job.outbox_event_id
                      AND submitted_evidence.owner_user_id = p_owner_user_id
                    ORDER BY submitted_evidence.sequence DESC
                    LIMIT 1
                ) AS submitted ON true
                WHERE job.outbox_event_id = p_event_id;
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_GET_CASE_FUNCTION}{_GET_CASE_SIGNATURE} FROM PUBLIC")
    )

    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION careerops.greenhouse_submit_create_draft(
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
                v_existing careerops.greenhouse_submit_command_receipts%ROWTYPE;
            BEGIN
                IF p_owner_user_id IS NULL OR p_candidate_id IS NULL OR p_resource_id IS NULL
                   OR p_target IS NULL OR p_payload IS NULL OR p_attachment_refs IS NULL
                   OR p_ruleset_version IS NULL OR btrim(p_ruleset_version) = ''
                   OR p_decision_rule_reference !~ '^[a-f0-9]{{64}}$'
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = ''
                   OR p_expires_at IS NULL OR p_expires_at <= v_now THEN
                    RAISE EXCEPTION 'greenhouse submit draft payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF jsonb_typeof(p_target) <> 'object'
                   OR jsonb_typeof(p_payload) <> 'object'
                   OR jsonb_typeof(p_attachment_refs) <> 'array'
                   OR p_target ->> 'target_host' IS DISTINCT FROM '{GREENHOUSE_HOST}'
                   OR p_target ->> 'channel' IS DISTINCT FROM '{GREENHOUSE_CHANNEL}'
                   OR p_target ->> 'adapter_id' IS DISTINCT FROM '{GREENHOUSE_ADAPTER}'
                   OR p_target ->> 'fixture_id' IS DISTINCT FROM '{GREENHOUSE_FIXTURE}'
                   OR (p_target ->> 'board_token_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'job_id_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'schema_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'raw_response_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'normalized_schema_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'job_identity_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_target ->> 'candidate_material_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_payload ->> 'answer_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (p_payload ->> 'payload_hash') !~ '^[a-f0-9]{{64}}$'
                   OR (p_payload ->> 'material_hash') !~ '^[a-f0-9]{{64}}$'
                   OR (p_payload ->> 'submission_identity_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR p_target ->> 'job_post_id' IS NULL
                   OR btrim(p_target ->> 'job_post_id') = ''
                   OR p_target ->> 'internal_job_id' IS NULL
                   OR btrim(p_target ->> 'internal_job_id') = ''
                   OR p_target ->> 'job_updated_at' IS NULL
                   OR btrim(p_target ->> 'job_updated_at') = ''
                   OR (
                       p_target ? 'application_deadline'
                       AND btrim(p_target ->> 'application_deadline') = ''
                   )
                   OR p_payload ->> 'board_token_sha256' IS DISTINCT FROM p_target ->> 'board_token_sha256'
                   OR p_payload ->> 'job_id_sha256' IS DISTINCT FROM p_target ->> 'job_id_sha256'
                   OR p_payload ->> 'schema_sha256' IS DISTINCT FROM p_target ->> 'schema_sha256'
                   OR p_payload ->> 'raw_response_sha256' IS DISTINCT FROM p_target ->> 'raw_response_sha256'
                   OR p_payload ->> 'normalized_schema_sha256' IS DISTINCT FROM p_target ->> 'normalized_schema_sha256'
                   OR p_payload ->> 'job_identity_sha256' IS DISTINCT FROM p_target ->> 'job_identity_sha256'
                   OR p_payload ->> 'job_post_id' IS DISTINCT FROM p_target ->> 'job_post_id'
                   OR p_payload ->> 'internal_job_id' IS DISTINCT FROM p_target ->> 'internal_job_id'
                   OR p_payload ->> 'job_updated_at' IS DISTINCT FROM p_target ->> 'job_updated_at'
                   OR p_payload ->> 'application_deadline' IS DISTINCT FROM p_target ->> 'application_deadline' THEN
                    RAISE EXCEPTION 'greenhouse submit draft target is not exact'
                        USING ERRCODE = '22023';
                END IF;
                p_payload_hash := COALESCE(p_payload_hash, p_payload ->> 'payload_hash');
                IF p_payload ->> 'payload_hash' IS DISTINCT FROM p_payload_hash THEN
                    RAISE EXCEPTION 'greenhouse submit draft payload hash mismatch'
                        USING ERRCODE = '23514';
                END IF;
                IF p_decision_rule_reference IS DISTINCT FROM encode(sha256(convert_to(
                    'greenhouse-submit-review-snapshot.v1' || chr(10)
                    || (p_payload ->> 'payload_hash') || chr(10)
                    || (p_payload ->> 'material_hash') || chr(10)
                    || (p_payload ->> 'submission_identity_sha256') || chr(10)
                    || (p_target ->> 'schema_sha256') || chr(10)
                    || (p_target ->> 'job_identity_sha256'),
                    'UTF8'
                )), 'hex') THEN
                    RAISE EXCEPTION 'greenhouse submit review snapshot hash mismatch'
                        USING ERRCODE = '23514';
                END IF;
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'create_draft'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> p_payload_hash THEN
                        RAISE EXCEPTION 'greenhouse submit draft idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT
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
                    id, action_kind, resource_type, resource_id, idempotency_key,
                    status, current_payload_version_id, created_by
                ) VALUES (
                    v_action_intent_id, 'submit_application', 'candidate', p_resource_id,
                    'greenhouse-submit-draft:' || p_idempotency_key,
                    'awaiting_approval', NULL, p_owner_user_id::text
                );
                INSERT INTO careerops.action_payload_versions (
                    id, action_intent_id, version, target, payload, attachment_refs, payload_hash
                ) VALUES (
                    v_payload_version_id, v_action_intent_id, 1,
                    p_target, p_payload, p_attachment_refs, p_payload_hash
                );
                UPDATE careerops.action_intents
                SET current_payload_version_id = v_payload_version_id
                WHERE id = v_action_intent_id;
                INSERT INTO careerops.policy_decisions (
                    id, action_intent_id, payload_version_id, ruleset_version,
                    decision, reason_codes, payload_hash, expires_at
                ) VALUES (
                    v_policy_decision_id, v_action_intent_id, v_payload_version_id,
                    p_ruleset_version, 'require_approval',
                    jsonb_build_array('GREENHOUSE_SUBMIT_REQUIRES_EXACT_HUMAN_REVIEW'),
                    p_payload_hash, p_expires_at
                );
                INSERT INTO careerops.approval_requests (
                    id, action_intent_id, payload_version_id, policy_decision_id,
                    requested_for, decision, decision_rule_reference, expires_at
                ) VALUES (
                    v_approval_request_id, v_action_intent_id, v_payload_version_id,
                    v_policy_decision_id, 'greenhouse_submit_exact_payload',
                    'pending', p_decision_rule_reference, p_expires_at
                );
                INSERT INTO careerops.greenhouse_submit_command_receipts (
                    id, owner_user_id, action_intent_id, command_kind,
                    idempotency_key, request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, v_action_intent_id,
                    'create_draft', p_idempotency_key, p_payload_hash,
                    jsonb_build_object(
                        'action_intent_id', v_action_intent_id::text,
                        'payload_version_id', v_payload_version_id::text,
                        'payload_hash', p_payload_hash,
                        'policy_decision_id', v_policy_decision_id::text,
                        'approval_request_id', v_approval_request_id::text
                    ),
                    p_trace_id
                );
                RETURN QUERY SELECT v_action_intent_id, v_payload_version_id,
                       p_payload_hash, v_policy_decision_id, v_approval_request_id,
                       'created'::text;
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
            f"""
            CREATE FUNCTION careerops.greenhouse_submit_review_draft(
                p_owner_user_id uuid,
                p_action_intent_id uuid,
                p_payload_version_id uuid,
                p_approval_request_id uuid,
                p_campaign_id uuid,
                p_grant_version_id uuid,
                p_payload_hash text,
                p_material_hash text,
                p_submission_identity_sha256 text,
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
                v_existing careerops.greenhouse_submit_command_receipts%ROWTYPE;
                v_request_sha256 text;
                v_payload_target jsonb;
                v_payload_payload jsonb;
                v_payload_attachment_refs jsonb;
                v_grant careerops.autopilot_grant_versions%ROWTYPE;
                v_policy_decision_id uuid;
            BEGIN
                IF p_owner_user_id IS NULL OR p_action_intent_id IS NULL
                   OR p_payload_version_id IS NULL OR p_approval_request_id IS NULL
                   OR p_campaign_id IS NULL OR p_grant_version_id IS NULL
                   OR p_reviewed_by_user_id IS NULL OR p_authorization_id IS NULL
                   OR p_payload_hash !~ '^[a-f0-9]{{64}}$'
                   OR p_material_hash !~ '^[a-f0-9]{{64}}$'
                   OR p_submission_identity_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_review_snapshot_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_decision NOT IN ('approved', 'rejected')
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = ''
                   OR p_reason IS NULL OR btrim(p_reason) = ''
                   OR p_authorization_expires_at IS NULL
                   OR p_authorization_expires_at <= v_now THEN
                    RAISE EXCEPTION 'greenhouse submit review payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF p_reviewed_by_user_id <> p_owner_user_id THEN
                    RAISE EXCEPTION 'greenhouse submit reviewer must match owner'
                        USING ERRCODE = '23514';
                END IF;
                v_request_sha256 := encode(sha256(convert_to(jsonb_build_object(
                    'owner_user_id', p_owner_user_id::text,
                    'action_intent_id', p_action_intent_id::text,
                    'payload_version_id', p_payload_version_id::text,
                    'approval_request_id', p_approval_request_id::text,
                    'campaign_id', p_campaign_id::text,
                    'grant_version_id', p_grant_version_id::text,
                    'payload_hash', p_payload_hash,
                    'material_hash', p_material_hash,
                    'submission_identity_sha256', p_submission_identity_sha256,
                    'decision', p_decision,
                    'reviewed_by_user_id', p_reviewed_by_user_id::text,
                    'authorization_id', p_authorization_id::text,
                    'review_snapshot_sha256', p_review_snapshot_sha256,
                    'authorization_expires_at', p_authorization_expires_at::text,
                    'reason', p_reason
                )::text, 'UTF8')), 'hex');
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'review_draft'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'greenhouse submit review idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT p_approval_request_id,
                        (v_existing.response_json ->> 'policy_decision_id')::uuid,
                        NULLIF(v_existing.response_json ->> 'authorization_id', '')::uuid,
                        p_decision, 'replayed'::text;
                    RETURN;
                END IF;
                SELECT payload.target, payload.payload, payload.attachment_refs
                INTO v_payload_target, v_payload_payload, v_payload_attachment_refs
                FROM careerops.action_payload_versions AS payload
                JOIN careerops.action_intents AS intent
                  ON intent.id = payload.action_intent_id
                WHERE payload.action_intent_id = p_action_intent_id
                  AND payload.id = p_payload_version_id
                  AND payload.payload_hash = p_payload_hash
                  AND intent.created_by = p_owner_user_id::text
                  AND intent.action_kind = 'submit_application'
                  AND EXISTS (
                      SELECT 1
                      FROM careerops.greenhouse_submit_command_receipts AS create_receipt
                      WHERE create_receipt.owner_user_id = p_owner_user_id
                        AND create_receipt.command_kind = 'create_draft'
                        AND create_receipt.action_intent_id = p_action_intent_id
                        AND create_receipt.response_json ->> 'payload_version_id' = p_payload_version_id::text
                        AND create_receipt.response_json ->> 'payload_hash' = p_payload_hash
                  )
                FOR UPDATE OF intent, payload;
                IF v_payload_target IS NULL
                   OR v_payload_target ->> 'target_host' IS DISTINCT FROM '{GREENHOUSE_HOST}'
                   OR v_payload_target ->> 'channel' IS DISTINCT FROM '{GREENHOUSE_CHANNEL}'
                   OR v_payload_target ->> 'adapter_id' IS DISTINCT FROM '{GREENHOUSE_ADAPTER}'
                   OR v_payload_target ->> 'fixture_id' IS DISTINCT FROM '{GREENHOUSE_FIXTURE}'
                   OR v_payload_payload ->> 'payload_hash' IS DISTINCT FROM p_payload_hash
                   OR v_payload_payload ->> 'material_hash' IS DISTINCT FROM p_material_hash
                   OR v_payload_payload ->> 'submission_identity_sha256' IS DISTINCT FROM p_submission_identity_sha256 THEN
                    RAISE EXCEPTION 'greenhouse submit review target is not exact'
                        USING ERRCODE = '23514';
                END IF;
                SELECT grant_version.* INTO v_grant
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
                   OR NOT COALESCE(v_grant.allowed_action_kinds @> jsonb_build_array('submit_application'), false)
                   OR NOT COALESCE(v_grant.allowed_channels @> jsonb_build_array('{GREENHOUSE_CHANNEL}'), false)
                   OR NOT COALESCE(v_grant.allowed_target_hosts @> jsonb_build_array('{GREENHOUSE_HOST}'), false)
                   OR NOT COALESCE(v_grant.material_hashes @> jsonb_build_array(v_payload_target ->> 'schema_sha256'), false)
                   OR NOT COALESCE(v_grant.material_hashes @> jsonb_build_array(v_payload_target ->> 'candidate_material_sha256'), false)
                   OR EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements(v_payload_attachment_refs) AS attachment(value)
                       WHERE jsonb_typeof(attachment.value) <> 'object'
                          OR jsonb_typeof(attachment.value -> 'sha256') <> 'string'
                          OR NOT COALESCE(v_grant.material_hashes @> jsonb_build_array(attachment.value ->> 'sha256'), false)
                   ) THEN
                    RAISE EXCEPTION 'greenhouse submit review exceeds grant scope'
                        USING ERRCODE = '23514';
                END IF;
                v_policy_decision_id := gen_random_uuid();
                INSERT INTO careerops.policy_decisions (
                    id, action_intent_id, payload_version_id, ruleset_version,
                    decision, reason_codes, payload_hash, expires_at
                ) VALUES (
                    v_policy_decision_id, p_action_intent_id, p_payload_version_id,
                    v_grant.policy_ruleset_version,
                    CASE WHEN p_decision = 'approved' THEN 'allow_autopilot_submission' ELSE 'deny' END,
                    jsonb_build_array(CASE WHEN p_decision = 'approved' THEN 'GREENHOUSE_SUBMIT_EXACT_PAYLOAD_APPROVED' ELSE 'GREENHOUSE_SUBMIT_EXACT_PAYLOAD_REJECTED' END),
                    p_payload_hash, p_authorization_expires_at
                );
                UPDATE careerops.approval_requests AS approval
                SET decision = p_decision,
                    decided_at = v_now,
                    policy_decision_id = v_policy_decision_id
                WHERE approval.id = p_approval_request_id
                  AND approval.action_intent_id = p_action_intent_id
                  AND approval.payload_version_id = p_payload_version_id
                  AND approval.requested_for = 'greenhouse_submit_exact_payload'
                  AND approval.decision = 'pending'
                  AND approval.decision_rule_reference = p_review_snapshot_sha256
                  AND approval.expires_at > v_now;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'greenhouse submit review requires pending exact approval'
                        USING ERRCODE = '23514';
                END IF;
                IF p_decision = 'approved' THEN
                    INSERT INTO careerops.autopilot_intent_authorizations (
                        id, campaign_id, grant_version_id, action_intent_id,
                        payload_version_id, payload_hash, policy_decision_id,
                        authorization_outcome, reason_codes, authorized_at, expires_at
                    ) VALUES (
                        p_authorization_id, p_campaign_id, p_grant_version_id,
                        p_action_intent_id, p_payload_version_id, p_payload_hash,
                        v_policy_decision_id, 'allow_autopilot_submission',
                        jsonb_build_array('GREENHOUSE_SUBMIT_EXACT_PAYLOAD_APPROVED'),
                        v_now, p_authorization_expires_at
                    );
                    UPDATE careerops.action_intents
                    SET status = 'eligible', updated_at = v_now
                    WHERE id = p_action_intent_id;
                ELSE
                    UPDATE careerops.action_intents
                    SET status = 'denied', updated_at = v_now
                    WHERE id = p_action_intent_id;
                END IF;
                INSERT INTO careerops.greenhouse_submit_command_receipts (
                    id, owner_user_id, action_intent_id, command_kind,
                    idempotency_key, request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_action_intent_id,
                    'review_draft', p_idempotency_key, v_request_sha256,
                    jsonb_build_object(
                        'approval_request_id', p_approval_request_id::text,
                        'policy_decision_id', v_policy_decision_id::text,
                        'authorization_id', CASE WHEN p_decision = 'approved' THEN p_authorization_id::text ELSE '' END,
                        'decision', p_decision
                    ),
                    p_trace_id
                );
                RETURN QUERY SELECT p_approval_request_id, v_policy_decision_id,
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


def _install_reserve_function() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION careerops.greenhouse_submit_reserve_and_enqueue(
                p_owner_user_id uuid,
                p_account_id uuid,
                p_campaign_id uuid,
                p_grant_version_id uuid,
                p_authorization_id uuid,
                p_action_intent_id uuid,
                p_payload_version_id uuid,
                p_payload_hash text,
                p_material_hash text,
                p_submission_identity_sha256 text,
                p_board_token_sha256 text,
                p_job_id_sha256 text,
                p_schema_sha256 text,
                p_approval_request_id uuid,
                p_review_evidence_sha256 text,
                p_review_snapshot_sha256 text,
                p_reviewed_by_user_id uuid,
                p_release_qualification_id uuid,
                p_reservation_key text,
                p_reconciliation_key text,
                p_event_key text,
                p_idempotency_key text,
                p_trace_id text
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
                v_account careerops.greenhouse_submit_accounts%ROWTYPE;
                v_existing careerops.greenhouse_submit_command_receipts%ROWTYPE;
                v_request_sha256 text;
                v_payload_target jsonb;
                v_payload jsonb;
                v_payload_attachment_refs jsonb;
                v_authorization_expires_at timestamp with time zone;
                v_authorization_policy_decision_id uuid;
                v_release_evidence_hash text;
                v_release_evidence_expires_at timestamp with time zone;
                v_reservation_id uuid;
                v_outbox_event_id uuid;
                v_latest_kill_active boolean;
                v_candidate_id uuid;
                v_account_daily_reserved bigint;
                v_company_key text;
            BEGIN
                IF p_owner_user_id IS NULL OR p_account_id IS NULL
                   OR p_campaign_id IS NULL OR p_grant_version_id IS NULL
                   OR p_authorization_id IS NULL OR p_action_intent_id IS NULL
                   OR p_payload_version_id IS NULL OR p_approval_request_id IS NULL
                   OR p_reviewed_by_user_id IS NULL OR p_release_qualification_id IS NULL THEN
                    RAISE EXCEPTION 'greenhouse submit reservation requires all identity ids'
                        USING ERRCODE = '22004';
                END IF;
                IF p_reviewed_by_user_id <> p_owner_user_id THEN
                    RAISE EXCEPTION 'greenhouse submit reviewer must match owner'
                        USING ERRCODE = '23514';
                END IF;
                IF p_payload_hash !~ '^[a-f0-9]{{64}}$'
                   OR p_material_hash !~ '^[a-f0-9]{{64}}$'
                   OR p_submission_identity_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_board_token_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_job_id_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_schema_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_review_evidence_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_review_snapshot_sha256 !~ '^[a-f0-9]{{64}}$'
                   OR p_reservation_key IS NULL OR btrim(p_reservation_key) = ''
                   OR p_reconciliation_key IS NULL OR btrim(p_reconciliation_key) = ''
                   OR p_event_key IS NULL OR p_event_key <> ('greenhouse-submit:' || p_reservation_key)
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'greenhouse submit reservation payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                v_request_sha256 := encode(sha256(convert_to(jsonb_build_object(
                    'owner_user_id', p_owner_user_id::text,
                    'account_id', p_account_id::text,
                    'campaign_id', p_campaign_id::text,
                    'grant_version_id', p_grant_version_id::text,
                    'authorization_id', p_authorization_id::text,
                    'action_intent_id', p_action_intent_id::text,
                    'payload_version_id', p_payload_version_id::text,
                    'payload_hash', p_payload_hash,
                    'material_hash', p_material_hash,
                    'submission_identity_sha256', p_submission_identity_sha256,
                    'board_token_sha256', p_board_token_sha256,
                    'job_id_sha256', p_job_id_sha256,
                    'schema_sha256', p_schema_sha256,
                    'approval_request_id', p_approval_request_id::text,
                    'review_evidence_sha256', p_review_evidence_sha256,
                    'review_snapshot_sha256', p_review_snapshot_sha256,
                    'reviewed_by_user_id', p_reviewed_by_user_id::text,
                    'release_qualification_id', p_release_qualification_id::text,
                    'reservation_key', p_reservation_key,
                    'reconciliation_key', p_reconciliation_key,
                    'event_key', p_event_key
                )::text, 'UTF8')), 'hex');
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_command_receipts AS receipt
                WHERE receipt.owner_user_id = p_owner_user_id
                  AND receipt.command_kind = 'reserve_and_enqueue'
                  AND receipt.idempotency_key = p_idempotency_key
                FOR UPDATE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'greenhouse submit reserve idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT p_account_id,
                        (v_existing.response_json ->> 'reservation_id')::uuid,
                        (v_existing.response_json ->> 'outbox_event_id')::uuid,
                        'replayed'::text;
                    RETURN;
                END IF;
                SELECT * INTO v_account
                FROM careerops.greenhouse_submit_accounts AS account
                WHERE account.id = p_account_id
                  AND account.owner_user_id = p_owner_user_id
                FOR UPDATE;
                IF v_account.id IS NULL
                   OR v_account.status <> 'active'
                   OR v_account.credential_profile_status <> 'active'
                   OR v_account.credential_profile_expires_at <= v_now
                   OR v_account.credential_profile_revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'greenhouse submit account must be active before enqueue'
                        USING ERRCODE = '23514';
                END IF;
                SELECT count(*) INTO v_account_daily_reserved
                FROM careerops.greenhouse_submit_review_evidence AS evidence
                WHERE evidence.account_id = p_account_id
                  AND evidence.created_at >= date_trunc('day', v_now)
                  AND evidence.created_at < date_trunc('day', v_now) + interval '1 day';
                IF v_account_daily_reserved >= v_account.daily_submit_limit THEN
                    RAISE EXCEPTION 'greenhouse submit account daily cap exhausted'
                        USING ERRCODE = '23514';
                END IF;
                v_company_key := encode(
                    sha256(convert_to(v_account.employer_id, 'UTF8')),
                    'hex'
                );
                SELECT payload.target, payload.payload, payload.attachment_refs
                INTO v_payload_target, v_payload, v_payload_attachment_refs
                FROM careerops.action_payload_versions AS payload
                JOIN careerops.action_intents AS intent
                  ON intent.id = payload.action_intent_id
                WHERE payload.action_intent_id = p_action_intent_id
                  AND payload.id = p_payload_version_id
                  AND payload.payload_hash = p_payload_hash
                  AND intent.created_by = p_owner_user_id::text
                  AND intent.action_kind = 'submit_application'
                  AND EXISTS (
                      SELECT 1
                      FROM careerops.greenhouse_submit_command_receipts AS create_receipt
                      WHERE create_receipt.owner_user_id = p_owner_user_id
                        AND create_receipt.command_kind = 'create_draft'
                        AND create_receipt.action_intent_id = p_action_intent_id
                        AND create_receipt.response_json ->> 'payload_version_id' = p_payload_version_id::text
                        AND create_receipt.response_json ->> 'payload_hash' = p_payload_hash
                  )
                FOR KEY SHARE OF payload, intent;
                IF v_payload_target IS NULL
                   OR v_payload_target ->> 'target_host' IS DISTINCT FROM '{GREENHOUSE_HOST}'
                   OR v_payload_target ->> 'channel' IS DISTINCT FROM '{GREENHOUSE_CHANNEL}'
                   OR v_payload_target ->> 'board_token_sha256' IS DISTINCT FROM p_board_token_sha256
                   OR v_account.board_token_sha256 IS DISTINCT FROM p_board_token_sha256
                   OR v_payload_target ->> 'job_id_sha256' IS DISTINCT FROM p_job_id_sha256
                   OR v_payload_target ->> 'schema_sha256' IS DISTINCT FROM p_schema_sha256
                   OR (v_payload_target ->> 'raw_response_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (v_payload_target ->> 'normalized_schema_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR (v_payload_target ->> 'job_identity_sha256') !~ '^[a-f0-9]{{64}}$'
                   OR v_payload_target ->> 'job_post_id' IS NULL
                   OR btrim(v_payload_target ->> 'job_post_id') = ''
                   OR v_payload_target ->> 'internal_job_id' IS NULL
                   OR btrim(v_payload_target ->> 'internal_job_id') = ''
                   OR v_payload_target ->> 'job_updated_at' IS NULL
                   OR btrim(v_payload_target ->> 'job_updated_at') = ''
                   OR (
                       v_payload_target ? 'application_deadline'
                       AND btrim(v_payload_target ->> 'application_deadline') = ''
                   )
                   OR v_payload ->> 'board_token_sha256' IS DISTINCT FROM p_board_token_sha256
                   OR v_payload ->> 'job_id_sha256' IS DISTINCT FROM p_job_id_sha256
                   OR v_payload ->> 'schema_sha256' IS DISTINCT FROM p_schema_sha256
                   OR v_payload ->> 'material_hash' IS DISTINCT FROM p_material_hash
                   OR v_payload ->> 'submission_identity_sha256' IS DISTINCT FROM p_submission_identity_sha256
                   OR v_payload_target ->> 'candidate_material_sha256' IS DISTINCT FROM p_material_hash
                   OR v_payload ->> 'raw_response_sha256' IS DISTINCT FROM v_payload_target ->> 'raw_response_sha256'
                   OR v_payload ->> 'normalized_schema_sha256' IS DISTINCT FROM v_payload_target ->> 'normalized_schema_sha256'
                   OR v_payload ->> 'job_identity_sha256' IS DISTINCT FROM v_payload_target ->> 'job_identity_sha256'
                   OR v_payload ->> 'job_post_id' IS DISTINCT FROM v_payload_target ->> 'job_post_id'
                   OR v_payload ->> 'internal_job_id' IS DISTINCT FROM v_payload_target ->> 'internal_job_id'
                   OR v_payload ->> 'job_updated_at' IS DISTINCT FROM v_payload_target ->> 'job_updated_at'
                   OR v_payload ->> 'application_deadline' IS DISTINCT FROM v_payload_target ->> 'application_deadline'
                   OR (v_payload ->> 'answer_sha256') !~ '^[a-f0-9]{{64}}$' THEN
                    RAISE EXCEPTION 'greenhouse submit payload target is not exact'
                        USING ERRCODE = '23514';
                END IF;
                SELECT intent.resource_id INTO v_candidate_id
                FROM careerops.action_intents AS intent
                WHERE intent.id = p_action_intent_id
                  AND intent.resource_type = 'candidate'
                  AND intent.created_by = p_owner_user_id::text
                FOR KEY SHARE;
                IF v_candidate_id IS NULL THEN
                    RAISE EXCEPTION 'greenhouse submit candidate binding is missing'
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
                    RAISE EXCEPTION 'greenhouse submit requires current bounded authorization'
                        USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.approval_requests AS approval
                    WHERE approval.id = p_approval_request_id
                      AND approval.action_intent_id = p_action_intent_id
                      AND approval.payload_version_id = p_payload_version_id
                      AND approval.policy_decision_id = v_authorization_policy_decision_id
                      AND approval.requested_for = 'greenhouse_submit_exact_payload'
                      AND approval.decision = 'approved'
                      AND approval.decision_rule_reference = p_review_snapshot_sha256
                      AND approval.expires_at > v_now
                    FOR KEY SHARE
                ) THEN
                    RAISE EXCEPTION 'greenhouse submit requires a current exact approved review'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:' || p_release_qualification_id::text,
                        0
                    )
                );
                SELECT decision.evidence_sha256, LEAST(qualification.expires_at, v_authorization_expires_at)
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
                  AND qualification.capability = 'greenhouse_submit'
                  AND qualification.action_name = 'submit_application'
                  AND qualification.rollout_mode = 'review_required'
                  AND qualification.provider = 'greenhouse'
                  AND qualification.adapter_id = '{GREENHOUSE_ADAPTER}'
                  AND qualification.adapter_version = '{GREENHOUSE_FIXTURE}'
                  AND qualification.migration_revision = '0014'
                  AND qualification.oauth_scope_hash = encode(sha256(convert_to('{GREENHOUSE_SCOPE}', 'UTF8')), 'hex')
                  AND qualification.credential_ref_hash = v_account.credential_store_evidence_sha256
                  AND qualification.expires_at > v_now
                  AND decision.to_status = 'qualified'
                  AND decision.evidence_sha256 ~ '^[a-f0-9]{{64}}$'
                FOR KEY SHARE OF qualification;
                IF v_release_evidence_hash IS NULL OR v_release_evidence_expires_at <= v_now THEN
                    RAISE EXCEPTION 'greenhouse submit requires a qualified current release'
                        USING ERRCODE = '23514';
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended('careerops:autopilot-kill-switch:global', 0)
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:campaign:' || p_campaign_id::text,
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended('careerops:autopilot-kill-switch:provider:greenhouse', 0)
                );
                IF EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'global'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) OR EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'campaign'
                          AND kill.campaign_id = p_campaign_id
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) OR EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'provider'
                          AND kill.provider = 'greenhouse'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) THEN
                    RAISE EXCEPTION 'greenhouse submit kill switch is active'
                        USING ERRCODE = '55000';
                END IF;
                v_reservation_id := gen_random_uuid();
                INSERT INTO careerops.autopilot_cap_reservations (
                    id, campaign_id, grant_version_id, authorization_id,
                    action_intent_id, payload_version_id, payload_hash,
                    target_host, channel, release_version, company_key,
                    adapter_id, fixture_id, reservation_key, reconciliation_key,
                    release_evidence_hash, release_evidence_expires_at
                )
                SELECT v_reservation_id, p_campaign_id, p_grant_version_id,
                       p_authorization_id, p_action_intent_id, p_payload_version_id,
                       p_payload_hash, '{GREENHOUSE_HOST}', '{GREENHOUSE_CHANNEL}',
                       grant_version.release_version, v_company_key,
                       '{GREENHOUSE_ADAPTER}', '{GREENHOUSE_FIXTURE}',
                       p_reservation_key, p_reconciliation_key,
                       v_release_evidence_hash, v_release_evidence_expires_at
                FROM careerops.autopilot_grant_versions AS grant_version
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = grant_version.campaign_id
                 AND campaign.owner_user_id = p_owner_user_id
                WHERE grant_version.id = p_grant_version_id
                  AND grant_version.campaign_id = p_campaign_id
                  AND grant_version.subject_actor = p_owner_user_id::text;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'greenhouse submit grant is not owned by reservation owner'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO careerops.greenhouse_submit_review_evidence (
                    id, owner_user_id, account_id, release_qualification_id,
                    action_intent_id, payload_version_id, payload_hash,
                    candidate_id, approval_request_id, reviewed_by_user_id,
                    board_token_sha256, account_subject,
                    authorized_integration_source, employer_id,
                    opaque_broker_handle_sha256, credential_profile_id,
                    credential_profile_version, credential_fingerprint_sha256,
                    credential_profile_status, credential_profile_expires_at,
                    job_post_id, internal_job_id, job_id_sha256, schema_sha256,
                    raw_response_sha256, normalized_schema_sha256,
                    job_updated_at, application_deadline, job_identity_sha256,
                    candidate_material_sha256, answer_sha256, attachment_sha256s,
                    review_evidence_sha256, review_snapshot_sha256,
                    grant_version_id, authorization_id, adapter_id, adapter_version,
                    release_version, release_evidence_sha256,
                    expires_at, idempotency_key, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_account_id,
                    p_release_qualification_id, p_action_intent_id,
                    p_payload_version_id, p_payload_hash, v_candidate_id,
                    p_approval_request_id, p_reviewed_by_user_id,
                    p_board_token_sha256, v_account.account_subject,
                    v_account.authorized_integration_source, v_account.employer_id,
                    encode(sha256(convert_to(v_account.opaque_broker_handle, 'UTF8')), 'hex'),
                    v_account.credential_profile_id, v_account.credential_profile_version,
                    v_account.credential_fingerprint_sha256,
                    v_account.credential_profile_status,
                    v_account.credential_profile_expires_at,
                    v_payload_target ->> 'job_post_id',
                    v_payload_target ->> 'internal_job_id', p_job_id_sha256,
                    p_schema_sha256, v_payload_target ->> 'raw_response_sha256',
                    v_payload_target ->> 'normalized_schema_sha256',
                    v_payload_target ->> 'job_updated_at',
                    v_payload_target ->> 'application_deadline',
                    v_payload_target ->> 'job_identity_sha256',
                    v_payload_target ->> 'candidate_material_sha256',
                    v_payload ->> 'answer_sha256',
                    COALESCE((
                        SELECT jsonb_agg(attachment.value ->> 'sha256' ORDER BY attachment.value ->> 'sha256')
                        FROM jsonb_array_elements(v_payload_attachment_refs) AS attachment(value)
                    ), '[]'::jsonb),
                    p_review_evidence_sha256, p_review_snapshot_sha256,
                    p_grant_version_id, p_authorization_id, '{GREENHOUSE_ADAPTER}',
                    '{GREENHOUSE_FIXTURE}', '{GREENHOUSE_RELEASE}', v_release_evidence_hash,
                    v_release_evidence_expires_at, p_idempotency_key, p_trace_id
                );
                v_outbox_event_id := gen_random_uuid();
                INSERT INTO careerops.outbox_events (
                    id, event_key, action_intent_id, payload_version_id,
                    event_type, status, available_at
                ) VALUES (
                    v_outbox_event_id, p_event_key, p_action_intent_id,
                    p_payload_version_id, 'workflow_signal', 'pending', v_now
                );
                INSERT INTO careerops.greenhouse_submit_command_receipts (
                    id, owner_user_id, account_id, action_intent_id,
                    outbox_event_id, command_kind, idempotency_key,
                    request_sha256, response_json, trace_id
                ) VALUES (
                    gen_random_uuid(), p_owner_user_id, p_account_id,
                    p_action_intent_id, v_outbox_event_id, 'reserve_and_enqueue',
                    p_idempotency_key, v_request_sha256,
                    jsonb_build_object(
                        'reservation_id', v_reservation_id::text,
                        'outbox_event_id', v_outbox_event_id::text,
                        'job_id_sha256', p_job_id_sha256
                    ),
                    p_trace_id
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
            CREATE FUNCTION careerops.claim_greenhouse_submit_outbox_events(
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
            BEGIN
                IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
                    RAISE EXCEPTION 'greenhouse outbox lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;
                RETURN QUERY
                WITH candidates AS (
                    SELECT event.id
                    FROM careerops.outbox_events AS event
                    WHERE event.event_type = 'workflow_signal'
                      AND event.event_key LIKE 'greenhouse-submit:%'
                      AND (
                          (event.status = 'pending' AND event.available_at <= v_now)
                          OR (event.status = 'leased' AND event.lease_until <= v_now)
                      )
                    ORDER BY event.available_at, event.id
                    LIMIT LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100)
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE careerops.outbox_events AS event
                    SET status = 'leased',
                        lease_owner = p_lease_owner,
                        lease_token = v_lease_token,
                        lease_until = v_now + make_interval(secs => LEAST(GREATEST(COALESCE(p_lease_seconds, 300), 1), 600)),
                        attempt_count = event.attempt_count + 1,
                        last_error_code = NULL
                    FROM candidates
                    WHERE event.id = candidates.id
                    RETURNING event.*
                )
                SELECT updated.id, updated.event_key::text, updated.action_intent_id,
                       updated.payload_version_id, updated.event_type::text,
                       updated.available_at, updated.attempt_count,
                       updated.lease_token, updated.lease_until
                FROM updated;
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
            CREATE FUNCTION careerops.mark_greenhouse_submit_outbox_published(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
                    RAISE EXCEPTION 'greenhouse mark published payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                PERFORM set_config('careerops.outbox_lease_owner', p_lease_owner, true);
                PERFORM set_config('careerops.outbox_lease_token', p_lease_token::text, true);
                UPDATE careerops.outbox_events
                SET status = 'published',
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    published_at = CURRENT_TIMESTAMP,
                    last_error_code = NULL
                WHERE id = p_event_id
                  AND event_key LIKE 'greenhouse-submit:%'
                  AND status = 'leased'
                  AND lease_owner = p_lease_owner
                  AND lease_token = p_lease_token
                  AND lease_until > CURRENT_TIMESTAMP;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'greenhouse outbox lease is invalid for publish'
                        USING ERRCODE = '55000';
                END IF;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_MARK_OUTBOX_PUBLISHED_FUNCTION}{_MARK_OUTBOX_PUBLISHED_SIGNATURE} FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.release_greenhouse_submit_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_retry_at timestamp with time zone,
                p_error_code text,
                p_terminal boolean
            )
            RETURNS void
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:@/-]{1,160}$'
                   OR p_retry_at IS NULL
                   OR p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$'
                   OR p_terminal IS NULL THEN
                    RAISE EXCEPTION 'greenhouse release payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                PERFORM set_config('careerops.outbox_lease_owner', p_lease_owner, true);
                PERFORM set_config('careerops.outbox_lease_token', p_lease_token::text, true);
                UPDATE careerops.outbox_events
                SET status = CASE WHEN p_terminal THEN 'failed' ELSE 'pending' END,
                    available_at = p_retry_at,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = p_error_code
                WHERE id = p_event_id
                  AND event_key LIKE 'greenhouse-submit:%'
                  AND status = 'leased'
                  AND lease_owner = p_lease_owner
                  AND lease_token = p_lease_token
                  AND lease_until > CURRENT_TIMESTAMP;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'greenhouse outbox lease is invalid for release'
                        USING ERRCODE = '55000';
                END IF;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RELEASE_OUTBOX_FUNCTION}{_RELEASE_OUTBOX_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            f"""
            CREATE FUNCTION careerops.greenhouse_submit_prepare_outbox_event(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid
            )
            RETURNS TABLE (
                event_id uuid,
                event_key text,
                account_id uuid,
                opaque_broker_handle text,
                account_subject text,
                owner_user_id uuid,
                employer_id text,
                credential_profile_version integer,
                credential_fingerprint_sha256 text,
                credential_profile_status text,
                credential_profile_expires_at timestamp with time zone,
                raw_response_sha256 text,
                schema_json jsonb,
                payload_json jsonb,
                attachment_refs_json jsonb,
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
                v_evidence careerops.greenhouse_submit_review_evidence%ROWTYPE;
                v_account careerops.greenhouse_submit_accounts%ROWTYPE;
                v_payload careerops.action_payload_versions%ROWTYPE;
                v_release_evidence_hash text;
                v_attempt_id uuid;
                v_existing_attempt careerops.side_effect_attempts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                    RAISE EXCEPTION 'greenhouse submit prepare requires event and lease token'
                        USING ERRCODE = '22004';
                END IF;
                IF p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:-]{{1,128}}$' THEN
                    RAISE EXCEPTION 'greenhouse submit lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;
                SELECT * INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;
                IF v_event.id IS NULL OR v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now
                   OR v_event.event_key NOT LIKE 'greenhouse-submit:%' THEN
                    RAISE EXCEPTION 'greenhouse submit lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                SELECT * INTO v_reservation
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.reservation_key = substring(v_event.event_key FROM char_length('greenhouse-submit:') + 1)
                  AND reservation.channel = '{GREENHOUSE_CHANNEL}'
                  AND reservation.target_host = '{GREENHOUSE_HOST}'
                FOR UPDATE;
                IF v_reservation.id IS NULL THEN
                    RAISE EXCEPTION 'greenhouse submit event is not bound to a reservation'
                        USING ERRCODE = '23503';
                END IF;
                IF v_reservation.release_evidence_expires_at <= v_now THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_RELEASE_EVIDENCE_EXPIRED'::text;
                    RETURN;
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-grant-state:' || v_reservation.grant_version_id::text,
                        0
                    )
                );
                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.autopilot_grant_versions AS grant_version
                    JOIN careerops.autopilot_campaigns AS campaign
                      ON campaign.id = grant_version.campaign_id
                    WHERE grant_version.id = v_reservation.grant_version_id
                      AND grant_version.campaign_id = v_reservation.campaign_id
                      AND grant_version.subject_actor = (
                          SELECT intent.created_by
                          FROM careerops.action_intents AS intent
                          WHERE intent.id = v_event.action_intent_id
                      )
                      AND grant_version.expires_at > v_now
                      AND grant_version.release_version = v_reservation.release_version
                      AND NOT EXISTS (
                          SELECT 1
                          FROM careerops.autopilot_grant_revocations AS revocation
                          WHERE revocation.grant_version_id = grant_version.id
                      )
                    FOR KEY SHARE OF grant_version, campaign
                ) THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_GRANT_NOT_CURRENT'::text;
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
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_AUTHORIZATION_EXPIRED'::text;
                    RETURN;
                END IF;
                SELECT evidence.* INTO v_evidence
                FROM careerops.greenhouse_submit_review_evidence AS evidence
                JOIN careerops.greenhouse_submit_command_receipts AS reserve_receipt
                  ON reserve_receipt.outbox_event_id = v_event.id
                 AND reserve_receipt.command_kind = 'reserve_and_enqueue'
                 AND reserve_receipt.action_intent_id = v_event.action_intent_id
                 AND reserve_receipt.account_id = evidence.account_id
                 AND reserve_receipt.owner_user_id = evidence.owner_user_id
                 AND reserve_receipt.response_json ->> 'reservation_id' = v_reservation.id::text
                JOIN careerops.approval_requests AS approval
                  ON approval.id = evidence.approval_request_id
                 AND approval.action_intent_id = v_event.action_intent_id
                 AND approval.payload_version_id = v_event.payload_version_id
                 AND approval.requested_for = 'greenhouse_submit_exact_payload'
                 AND approval.decision = 'approved'
                 AND approval.decision_rule_reference = evidence.review_snapshot_sha256
                 AND approval.expires_at > v_now
                WHERE evidence.action_intent_id = v_event.action_intent_id
                  AND evidence.payload_version_id = v_event.payload_version_id
                  AND evidence.payload_hash = v_reservation.payload_hash
                  AND encode(sha256(convert_to(evidence.employer_id, 'UTF8')), 'hex') = v_reservation.company_key
                  AND evidence.reviewed_by_user_id = reserve_receipt.owner_user_id
                  AND evidence.expires_at > v_now
                ORDER BY evidence.created_at DESC
                LIMIT 1;
                IF v_evidence.id IS NULL THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_REVIEW_BINDING_INVALID'::text;
                    RETURN;
                END IF;
                SELECT * INTO v_account
                FROM careerops.greenhouse_submit_accounts AS account
                WHERE account.id = v_evidence.account_id
                  AND account.owner_user_id = v_evidence.owner_user_id
                FOR UPDATE;
                IF v_account.status <> 'active'
                   OR v_account.credential_profile_status <> 'active'
                   OR v_account.credential_profile_expires_at <= v_now
                   OR v_account.credential_profile_revoked_at IS NOT NULL THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_ACCOUNT_NOT_ACTIVE'::text;
                    RETURN;
                END IF;
                SELECT * INTO v_payload
                FROM careerops.action_payload_versions AS payload
                WHERE payload.action_intent_id = v_event.action_intent_id
                  AND payload.id = v_event.payload_version_id
                  AND payload.payload_hash = v_reservation.payload_hash
                FOR KEY SHARE;
                IF v_payload.id IS NULL
                   OR v_payload.target -> 'schema_snapshot' IS NULL THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb, v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_PAYLOAD_SCHEMA_SNAPSHOT_MISSING'::text;
                    RETURN;
                END IF;
                IF v_evidence.account_subject IS DISTINCT FROM v_account.account_subject
                   OR v_evidence.authorized_integration_source IS DISTINCT FROM v_account.authorized_integration_source
                   OR v_evidence.employer_id IS DISTINCT FROM v_account.employer_id
                   OR v_evidence.opaque_broker_handle_sha256 IS DISTINCT FROM encode(sha256(convert_to(v_account.opaque_broker_handle, 'UTF8')), 'hex')
                   OR v_evidence.credential_profile_id IS DISTINCT FROM v_account.credential_profile_id
                   OR v_evidence.credential_profile_version IS DISTINCT FROM v_account.credential_profile_version
                   OR v_evidence.credential_fingerprint_sha256 IS DISTINCT FROM v_account.credential_fingerprint_sha256
                   OR v_evidence.credential_profile_status IS DISTINCT FROM v_account.credential_profile_status
                   OR v_evidence.credential_profile_expires_at IS DISTINCT FROM v_account.credential_profile_expires_at
                   OR v_evidence.board_token_sha256 IS DISTINCT FROM v_account.board_token_sha256
                   OR v_evidence.grant_version_id IS DISTINCT FROM v_reservation.grant_version_id
                   OR v_evidence.authorization_id IS DISTINCT FROM v_reservation.authorization_id
                   OR v_evidence.release_evidence_sha256 IS DISTINCT FROM v_reservation.release_evidence_hash
                   OR v_evidence.adapter_id IS DISTINCT FROM v_reservation.adapter_id
                   OR v_evidence.adapter_version IS DISTINCT FROM v_reservation.fixture_id
                   OR v_evidence.release_version IS DISTINCT FROM v_reservation.release_version
                   OR v_evidence.job_id_sha256 IS DISTINCT FROM v_payload.target ->> 'job_id_sha256'
                   OR v_evidence.schema_sha256 IS DISTINCT FROM v_payload.target ->> 'schema_sha256'
                   OR v_evidence.raw_response_sha256 IS DISTINCT FROM v_payload.target ->> 'raw_response_sha256'
                   OR v_evidence.normalized_schema_sha256 IS DISTINCT FROM v_payload.target ->> 'normalized_schema_sha256'
                   OR v_evidence.job_identity_sha256 IS DISTINCT FROM v_payload.target ->> 'job_identity_sha256'
                   OR v_evidence.candidate_material_sha256 IS DISTINCT FROM v_payload.payload ->> 'material_hash'
                   OR v_payload.payload ->> 'submission_identity_sha256' IS NULL THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_IMMUTABLE_BINDING_DRIFT'::text;
                    RETURN;
                END IF;
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:release-qualification-decision:' || v_evidence.release_qualification_id::text,
                        0
                    )
                );
                SELECT decision.evidence_sha256 INTO v_release_evidence_hash
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
                  AND qualification.capability = 'greenhouse_submit'
                  AND qualification.action_name = 'submit_application'
                  AND qualification.rollout_mode = 'review_required'
                  AND qualification.provider = 'greenhouse'
                  AND qualification.adapter_id = '{GREENHOUSE_ADAPTER}'
                  AND qualification.adapter_version = '{GREENHOUSE_FIXTURE}'
                  AND qualification.migration_revision = '0014'
                  AND qualification.oauth_scope_hash = encode(sha256(convert_to('{GREENHOUSE_SCOPE}', 'UTF8')), 'hex')
                  AND qualification.credential_ref_hash = v_account.credential_store_evidence_sha256
                  AND qualification.expires_at > v_now
                  AND qualification.expires_at >= v_reservation.release_evidence_expires_at
                  AND decision.to_status = 'qualified'
                  AND decision.evidence_sha256 = v_reservation.release_evidence_hash
                FOR KEY SHARE OF qualification;
                IF v_release_evidence_hash IS NULL THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_RELEASE_NOT_QUALIFIED'::text;
                    RETURN;
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM careerops.provider_receipts AS receipt
                    WHERE receipt.provider = 'greenhouse'
                      AND receipt.reconciliation_key = v_reservation.reconciliation_key
                ) THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, v_account.id, v_account.opaque_broker_handle, v_account.account_subject,
                        v_account.owner_user_id, v_account.employer_id,
                        v_account.credential_profile_version,
                        v_account.credential_fingerprint_sha256::text,
                        v_account.credential_profile_status,
                        v_account.credential_profile_expires_at,
                        v_evidence.raw_response_sha256::text,
                        v_payload.target -> 'schema_snapshot',
                        v_payload.payload, v_payload.attachment_refs,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'already_terminal'::text, NULL::text;
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
                    pg_catalog.hashtextextended('careerops:autopilot-kill-switch:provider:greenhouse', 0)
                );
                IF EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'global'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) OR EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'campaign'
                          AND kill.campaign_id = v_reservation.campaign_id
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) OR EXISTS (
                    SELECT 1 FROM (
                        SELECT kill.active
                        FROM careerops.autopilot_kill_switch_events AS kill
                        WHERE kill.scope_type = 'provider'
                          AND kill.provider = 'greenhouse'
                        ORDER BY kill.sequence DESC
                        LIMIT 1
                    ) AS latest WHERE latest.active
                ) THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, NULL::uuid, NULL::text, NULL::text,
                        NULL::uuid, NULL::text, NULL::integer, NULL::text, NULL::text,
                        NULL::timestamp with time zone, NULL::text, NULL::jsonb, NULL::jsonb,
                        NULL::jsonb,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'stopped'::text, 'GREENHOUSE_SUBMIT_KILL_SWITCH_ACTIVE'::text;
                    RETURN;
                END IF;
                SELECT * INTO v_existing_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;
                IF v_existing_attempt.id IS NOT NULL
                   AND v_existing_attempt.state = 'rejected' THEN
                    RETURN QUERY SELECT v_event.id, v_event.event_key, v_account.id, v_account.opaque_broker_handle, v_account.account_subject,
                        v_account.owner_user_id, v_account.employer_id,
                        v_account.credential_profile_version,
                        v_account.credential_fingerprint_sha256::text,
                        v_account.credential_profile_status,
                        v_account.credential_profile_expires_at,
                        v_evidence.raw_response_sha256::text,
                        v_payload.target -> 'schema_snapshot',
                        v_payload.payload, v_payload.attachment_refs,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'already_terminal'::text, NULL::text;
                    RETURN;
                END IF;
                IF v_existing_attempt.id IS NOT NULL
                   AND v_existing_attempt.state <> 'failed' THEN
                    UPDATE careerops.side_effect_attempts
                    SET state = 'reconciliation_required',
                        finished_at = v_now,
                        error_code = 'GREENHOUSE_SUBMIT_REPLAY_RECONCILIATION_REQUIRED',
                        response_metadata = response_metadata || jsonb_build_object('reason', 'leased event replayed before receipt')
                    WHERE id = v_existing_attempt.id;
                    UPDATE careerops.action_intents
                    SET status = 'reconciliation_required', updated_at = v_now
                    WHERE id = v_event.action_intent_id;
                    INSERT INTO careerops.greenhouse_submit_reconciliation_jobs (
                        outbox_event_id, status, available_at, last_error_code
                    ) VALUES (
                        v_event.id, 'required', v_now,
                        'GREENHOUSE_SUBMIT_REPLAY_RECONCILIATION_REQUIRED'
                    )
                    ON CONFLICT (outbox_event_id) DO UPDATE
                    SET status = 'required', available_at = v_now,
                        lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                        last_error_code = EXCLUDED.last_error_code,
                        updated_at = v_now;
                    RETURN QUERY SELECT v_event.id, v_event.event_key, v_account.id, v_account.opaque_broker_handle, v_account.account_subject,
                        v_account.owner_user_id, v_account.employer_id,
                        v_account.credential_profile_version,
                        v_account.credential_fingerprint_sha256::text,
                        v_account.credential_profile_status,
                        v_account.credential_profile_expires_at,
                        v_evidence.raw_response_sha256::text,
                        v_payload.target -> 'schema_snapshot',
                        v_payload.payload, v_payload.attachment_refs,
                        v_event.action_intent_id, v_event.payload_version_id,
                        v_reservation.reservation_key, v_reservation.reconciliation_key,
                        'already_terminal'::text, NULL::text;
                    RETURN;
                END IF;
                v_attempt_id := gen_random_uuid();
                INSERT INTO careerops.side_effect_attempts (
                    id, action_intent_id, outbox_event_id, ordinal, state,
                    request_fingerprint, started_at, response_metadata
                ) VALUES (
                    v_attempt_id, v_event.action_intent_id, v_event.id,
                    COALESCE(v_existing_attempt.ordinal, 0) + 1, 'processing',
                    encode(sha256(convert_to(jsonb_build_object(
                        'event_id', v_event.id::text,
                        'event_key', v_event.event_key,
                        'payload_hash', v_reservation.payload_hash,
                        'reconciliation_key', v_reservation.reconciliation_key,
                        'account_id', v_account.id::text
                    )::text, 'UTF8')), 'hex'),
                    v_now,
                    jsonb_build_object(
                        'provider', 'greenhouse',
                        'account_id', v_account.id::text,
                        'broker_profile_id', v_account.credential_profile_id,
                        'release_evidence_hash', v_reservation.release_evidence_hash
                    )
                );
                UPDATE careerops.action_intents
                SET status = 'processing', updated_at = v_now
                WHERE id = v_event.action_intent_id;
                RETURN QUERY SELECT v_event.id, v_event.event_key, v_account.id, v_account.opaque_broker_handle, v_account.account_subject,
                    v_account.owner_user_id, v_account.employer_id,
                    v_account.credential_profile_version,
                    v_account.credential_fingerprint_sha256::text,
                    v_account.credential_profile_status,
                    v_account.credential_profile_expires_at,
                    v_evidence.raw_response_sha256::text,
                    v_payload.target -> 'schema_snapshot',
                    v_payload.payload, v_payload.attachment_refs,
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
            CREATE FUNCTION careerops.greenhouse_submit_record_accepted_unverified(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_reconciliation_key text,
                p_provider_timestamp timestamp with time zone,
                p_receipt_id uuid,
                p_http_status integer,
                p_broker_request_sha256 text,
                p_broker_response_sha256 text,
                p_provider_request_sha256 text,
                p_provider_response_sha256 text,
                p_observed_raw_response_sha256 text,
                p_observed_normalized_schema_sha256 text,
                p_journal_receipt_sha256 text,
                p_journal_state text,
                p_journal_sequence bigint,
                p_evidence_sha256 text
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
                v_evidence careerops.greenhouse_submit_review_evidence%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL OR p_receipt_id IS NULL
                   OR p_reconciliation_key IS NULL OR btrim(p_reconciliation_key) = ''
                   OR p_provider_timestamp IS NULL
                   OR p_http_status < 200 OR p_http_status > 299
                   OR p_broker_request_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_broker_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_provider_request_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_provider_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_observed_raw_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_observed_normalized_schema_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_journal_receipt_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_journal_state IS DISTINCT FROM 'response_observed'
                   OR p_journal_sequence IS NULL OR p_journal_sequence <= 0
                   OR p_evidence_sha256 !~ '^[a-f0-9]{64}$' THEN
                    RAISE EXCEPTION 'greenhouse accepted-unverified receipt payload is invalid'
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
                   OR v_event.event_key NOT LIKE 'greenhouse-submit:%' THEN
                    RAISE EXCEPTION 'greenhouse receipt lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                SELECT * INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state = 'processing'
                FOR UPDATE;
                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'greenhouse receipt requires one processing attempt'
                        USING ERRCODE = '23514';
                END IF;
                SELECT evidence.* INTO v_evidence
                FROM careerops.greenhouse_submit_review_evidence AS evidence
                WHERE evidence.action_intent_id = v_event.action_intent_id
                  AND evidence.payload_version_id = v_event.payload_version_id
                ORDER BY evidence.created_at DESC
                LIMIT 1;
                IF v_evidence.id IS NULL
                   OR p_reconciliation_key IS DISTINCT FROM (
                       SELECT reservation.reconciliation_key
                       FROM careerops.autopilot_cap_reservations AS reservation
                       WHERE reservation.action_intent_id = v_event.action_intent_id
                         AND reservation.payload_version_id = v_event.payload_version_id
                       ORDER BY reservation.reserved_at DESC
                       LIMIT 1
                   )
                   OR p_observed_raw_response_sha256 IS DISTINCT FROM v_evidence.raw_response_sha256
                   OR p_observed_normalized_schema_sha256 IS DISTINCT FROM v_evidence.normalized_schema_sha256 THEN
                    RAISE EXCEPTION 'greenhouse accepted-unverified evidence is not review-bound'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO careerops.provider_receipts (
                    id, side_effect_attempt_id, provider, provider_resource_id,
                    reconciliation_key, final_state, provider_timestamp, received_at,
                    receipt_metadata
                ) VALUES (
                    p_receipt_id, v_attempt.id, 'greenhouse',
                    'accepted-unverified:' || p_event_id::text,
                    p_reconciliation_key, 'accepted_unverified', p_provider_timestamp, v_now,
                    jsonb_build_object(
                        'provider_state', 'accepted_unverified',
                        'verification_required', true,
                        'synthetic_resource_id_authoritative', false,
                        'http_status', p_http_status,
                        'broker_request_sha256', p_broker_request_sha256,
                        'broker_response_sha256', p_broker_response_sha256,
                        'provider_request_sha256', p_provider_request_sha256,
                        'provider_response_sha256', p_provider_response_sha256,
                        'observed_raw_response_sha256', p_observed_raw_response_sha256,
                        'observed_normalized_schema_sha256', p_observed_normalized_schema_sha256,
                        'journal_receipt_sha256', p_journal_receipt_sha256,
                        'journal_state', p_journal_state,
                        'journal_sequence', p_journal_sequence,
                        'evidence_sha256', p_evidence_sha256,
                        'confirmation_source', 'manual_review_or_employer_evidence_only'
                    )
                );
                UPDATE careerops.side_effect_attempts
                SET state = 'reconciliation_required',
                    finished_at = v_now,
                    error_code = 'GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED',
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'greenhouse',
                        'receipt_id', p_receipt_id::text,
                        'provider_state', 'accepted_unverified'
                    )
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'reconciliation_required', updated_at = v_now
                WHERE id = v_event.action_intent_id;
                INSERT INTO careerops.greenhouse_submit_reconciliation_jobs (
                    outbox_event_id, status, available_at, last_error_code
                ) VALUES (
                    p_event_id, 'required', v_now, 'GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED'
                )
                ON CONFLICT (outbox_event_id) DO UPDATE
                SET status = 'required',
                    available_at = v_now,
                    lease_owner = NULL,
                    lease_token = NULL,
                    lease_until = NULL,
                    last_error_code = EXCLUDED.last_error_code,
                    updated_at = v_now;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_RECEIPT_FUNCTION}{_RECORD_RECEIPT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.greenhouse_submit_record_ambiguity(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_error_code text,
                p_reconciliation_key text,
                p_broker_request_sha256 text,
                p_broker_response_sha256 text,
                p_provider_request_sha256 text,
                p_provider_response_sha256 text,
                p_observed_raw_response_sha256 text,
                p_observed_normalized_schema_sha256 text,
                p_journal_receipt_sha256 text,
                p_journal_state text,
                p_journal_sequence bigint,
                p_evidence_sha256 text,
                p_submission_identity text,
                p_payload_hash text,
                p_material_hash text
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
                IF p_event_id IS NULL OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                    RAISE EXCEPTION 'greenhouse ambiguity payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF p_evidence_sha256 IS NULL THEN
                    IF p_reconciliation_key IS NOT NULL
                       OR p_broker_request_sha256 IS NOT NULL
                       OR p_broker_response_sha256 IS NOT NULL
                       OR p_provider_request_sha256 IS NOT NULL
                       OR p_provider_response_sha256 IS NOT NULL
                       OR p_observed_raw_response_sha256 IS NOT NULL
                       OR p_observed_normalized_schema_sha256 IS NOT NULL
                       OR p_journal_receipt_sha256 IS NOT NULL
                       OR p_journal_state IS NOT NULL
                       OR p_journal_sequence IS NOT NULL
                       OR p_submission_identity IS NOT NULL
                       OR p_payload_hash IS NOT NULL
                       OR p_material_hash IS NOT NULL THEN
                        RAISE EXCEPTION 'greenhouse ambiguity partial evidence is invalid'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF p_reconciliation_key IS NULL OR btrim(p_reconciliation_key) = ''
                   OR p_broker_request_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_broker_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_submission_identity !~ '^[a-f0-9]{64}$'
                   OR p_payload_hash !~ '^[a-f0-9]{64}$'
                   OR p_material_hash !~ '^[a-f0-9]{64}$'
                   OR ((p_provider_request_sha256 IS NULL) <> (p_provider_response_sha256 IS NULL))
                   OR (p_provider_request_sha256 IS NOT NULL AND (
                       p_provider_request_sha256 !~ '^[a-f0-9]{64}$'
                       OR p_provider_response_sha256 !~ '^[a-f0-9]{64}$'
                   ))
                   OR ((p_observed_raw_response_sha256 IS NULL) <> (p_observed_normalized_schema_sha256 IS NULL))
                   OR (p_observed_raw_response_sha256 IS NOT NULL AND (
                       p_observed_raw_response_sha256 !~ '^[a-f0-9]{64}$'
                       OR p_observed_normalized_schema_sha256 !~ '^[a-f0-9]{64}$'
                   ))
                   OR ((p_journal_receipt_sha256 IS NULL)::integer
                       + (p_journal_state IS NULL)::integer
                       + (p_journal_sequence IS NULL)::integer) NOT IN (0, 3)
                   OR (p_journal_receipt_sha256 IS NOT NULL AND (
                       p_journal_receipt_sha256 !~ '^[a-f0-9]{64}$'
                       OR p_journal_state NOT IN ('post_started', 'response_observed', 'ambiguous')
                       OR p_journal_sequence <= 0
                   )) THEN
                    RAISE EXCEPTION 'greenhouse ambiguity evidence payload is invalid'
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
                   OR v_event.event_key NOT LIKE 'greenhouse-submit:%' THEN
                    RAISE EXCEPTION 'greenhouse ambiguity lease is invalid'
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
                    RAISE EXCEPTION 'greenhouse ambiguity requires an attempt'
                        USING ERRCODE = '23514';
                END IF;
                IF p_evidence_sha256 IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM careerops.action_payload_versions AS payload
                    JOIN careerops.autopilot_cap_reservations AS reservation
                      ON reservation.action_intent_id = payload.action_intent_id
                     AND reservation.payload_version_id = payload.id
                     AND reservation.channel = 'greenhouse:job-board'
                    JOIN careerops.greenhouse_submit_review_evidence AS evidence
                      ON evidence.action_intent_id = payload.action_intent_id
                     AND evidence.payload_version_id = payload.id
                    WHERE payload.action_intent_id = v_event.action_intent_id
                      AND payload.id = v_event.payload_version_id
                      AND reservation.reconciliation_key = p_reconciliation_key
                      AND payload.payload_hash = p_payload_hash
                      AND payload.payload ->> 'payload_hash' = p_payload_hash
                      AND payload.payload ->> 'material_hash' = p_material_hash
                      AND payload.payload ->> 'submission_identity_sha256' = p_submission_identity
                      AND (
                          p_observed_raw_response_sha256 IS NULL
                          OR p_observed_raw_response_sha256 = evidence.raw_response_sha256
                      )
                      AND (
                          p_observed_normalized_schema_sha256 IS NULL
                          OR p_observed_normalized_schema_sha256 = evidence.normalized_schema_sha256
                      )
                ) THEN
                    RAISE EXCEPTION 'greenhouse ambiguity evidence is not review-bound'
                        USING ERRCODE = '23514';
                END IF;
                UPDATE careerops.side_effect_attempts
                SET state = 'reconciliation_required',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object('provider', 'greenhouse', 'error_code', p_error_code)
                        || jsonb_strip_nulls(jsonb_build_object(
                            'broker_request_sha256', p_broker_request_sha256,
                            'broker_response_sha256', p_broker_response_sha256,
                            'provider_request_sha256', p_provider_request_sha256,
                            'provider_response_sha256', p_provider_response_sha256,
                            'observed_raw_response_sha256', p_observed_raw_response_sha256,
                            'observed_normalized_schema_sha256', p_observed_normalized_schema_sha256,
                            'journal_receipt_sha256', p_journal_receipt_sha256,
                            'journal_state', p_journal_state,
                            'journal_sequence', p_journal_sequence,
                            'evidence_sha256', p_evidence_sha256,
                            'reconciliation_key', p_reconciliation_key,
                            'submission_identity', p_submission_identity,
                            'payload_hash', p_payload_hash,
                            'material_hash', p_material_hash
                        ))
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'reconciliation_required', updated_at = v_now
                WHERE id = v_event.action_intent_id;
                INSERT INTO careerops.greenhouse_submit_reconciliation_jobs (
                    outbox_event_id, status, available_at, last_error_code
                ) VALUES (
                    p_event_id, 'required', v_now, p_error_code
                )
                ON CONFLICT (outbox_event_id) DO UPDATE
                SET status = 'required', available_at = v_now,
                    lease_owner = NULL, lease_token = NULL, lease_until = NULL,
                    last_error_code = EXCLUDED.last_error_code,
                    updated_at = v_now;
            END
            $function$
            """
        )
    )
    op.execute(sa.text(_RECORD_AMBIGUITY_REVOKE_PUBLIC_SQL))

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.greenhouse_submit_record_rejected(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_error_code text,
                p_reason_codes text[],
                p_reconciliation_key text,
                p_http_status integer,
                p_broker_request_sha256 text,
                p_broker_response_sha256 text,
                p_provider_request_sha256 text,
                p_provider_response_sha256 text,
                p_observed_raw_response_sha256 text,
                p_observed_normalized_schema_sha256 text,
                p_journal_receipt_sha256 text,
                p_journal_state text,
                p_journal_sequence bigint,
                p_evidence_sha256 text,
                p_submission_identity text,
                p_payload_hash text,
                p_material_hash text
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
                IF p_event_id IS NULL OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$'
                   OR p_reason_codes IS NULL OR cardinality(p_reason_codes) < 1
                   OR EXISTS (
                       SELECT 1
                       FROM unnest(p_reason_codes) AS reason(code)
                       WHERE reason.code !~ '^[A-Z0-9_]{1,64}$'
                   ) THEN
                    RAISE EXCEPTION 'greenhouse rejected payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                IF p_evidence_sha256 IS NULL THEN
                    IF p_reconciliation_key IS NOT NULL
                       OR p_http_status IS NOT NULL
                       OR p_broker_request_sha256 IS NOT NULL
                       OR p_broker_response_sha256 IS NOT NULL
                       OR p_provider_request_sha256 IS NOT NULL
                       OR p_provider_response_sha256 IS NOT NULL
                       OR p_observed_raw_response_sha256 IS NOT NULL
                       OR p_observed_normalized_schema_sha256 IS NOT NULL
                       OR p_journal_receipt_sha256 IS NOT NULL
                       OR p_journal_state IS NOT NULL
                       OR p_journal_sequence IS NOT NULL
                       OR p_submission_identity IS NOT NULL
                       OR p_payload_hash IS NOT NULL
                       OR p_material_hash IS NOT NULL THEN
                        RAISE EXCEPTION 'greenhouse rejected partial evidence is invalid'
                            USING ERRCODE = '22023';
                    END IF;
                ELSIF p_reconciliation_key IS NULL OR btrim(p_reconciliation_key) = ''
                   OR NOT (
                       (p_http_status IN (400, 422) AND p_error_code = 'GREENHOUSE_PROVIDER_VALIDATION_REJECTED')
                       OR (p_http_status IN (401, 403) AND p_error_code = 'GREENHOUSE_PROVIDER_AUTH_REJECTED')
                       OR (p_http_status = 404 AND p_error_code = 'GREENHOUSE_PROVIDER_JOB_NOT_FOUND')
                       OR (p_http_status = 413 AND p_error_code = 'GREENHOUSE_PROVIDER_REQUEST_TOO_LARGE')
                       OR (p_http_status = 415 AND p_error_code = 'GREENHOUSE_PROVIDER_MEDIA_TYPE_REJECTED')
                   )
                   OR NOT (p_reason_codes @> ARRAY[p_error_code])
                   OR p_broker_request_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_broker_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_provider_request_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_provider_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_observed_raw_response_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_observed_normalized_schema_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_journal_receipt_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_journal_state IS DISTINCT FROM 'response_observed'
                   OR p_journal_sequence IS NULL OR p_journal_sequence <= 0
                   OR p_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_submission_identity !~ '^[a-f0-9]{64}$'
                   OR p_payload_hash !~ '^[a-f0-9]{64}$'
                   OR p_material_hash !~ '^[a-f0-9]{64}$' THEN
                    RAISE EXCEPTION 'greenhouse rejected provider evidence is invalid'
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
                   OR v_event.event_key NOT LIKE 'greenhouse-submit:%' THEN
                    RAISE EXCEPTION 'greenhouse rejected lease is invalid'
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
                    RAISE EXCEPTION 'greenhouse rejected requires an attempt'
                        USING ERRCODE = '23514';
                END IF;
                IF p_evidence_sha256 IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM careerops.action_payload_versions AS payload
                    JOIN careerops.autopilot_cap_reservations AS reservation
                      ON reservation.action_intent_id = payload.action_intent_id
                     AND reservation.payload_version_id = payload.id
                     AND reservation.channel = 'greenhouse:job-board'
                    JOIN careerops.greenhouse_submit_review_evidence AS evidence
                      ON evidence.action_intent_id = payload.action_intent_id
                     AND evidence.payload_version_id = payload.id
                    WHERE payload.action_intent_id = v_event.action_intent_id
                      AND payload.id = v_event.payload_version_id
                      AND reservation.reconciliation_key = p_reconciliation_key
                      AND payload.payload_hash = p_payload_hash
                      AND payload.payload ->> 'payload_hash' = p_payload_hash
                      AND payload.payload ->> 'material_hash' = p_material_hash
                      AND payload.payload ->> 'submission_identity_sha256' = p_submission_identity
                      AND evidence.raw_response_sha256 = p_observed_raw_response_sha256
                      AND evidence.normalized_schema_sha256 = p_observed_normalized_schema_sha256
                ) THEN
                    RAISE EXCEPTION 'greenhouse rejected evidence is not review-bound'
                        USING ERRCODE = '23514';
                END IF;
                IF p_evidence_sha256 IS NOT NULL THEN
                    INSERT INTO careerops.provider_receipts (
                        id, side_effect_attempt_id, provider, provider_resource_id,
                        reconciliation_key, final_state, provider_timestamp, received_at,
                        receipt_metadata
                    ) VALUES (
                        (
                            substr(p_evidence_sha256, 1, 8) || '-' ||
                            substr(p_evidence_sha256, 9, 4) || '-' ||
                            substr(p_evidence_sha256, 13, 4) || '-' ||
                            substr(p_evidence_sha256, 17, 4) || '-' ||
                            substr(p_evidence_sha256, 21, 12)
                        )::uuid,
                        v_attempt.id, 'greenhouse',
                        'provider-rejected:' || p_event_id::text,
                        p_reconciliation_key, 'provider_rejected', v_now, v_now,
                        jsonb_build_object(
                            'provider_state', 'provider_rejected',
                            'http_status', p_http_status,
                            'reason_code', p_error_code,
                            'broker_request_sha256', p_broker_request_sha256,
                            'broker_response_sha256', p_broker_response_sha256,
                            'provider_request_sha256', p_provider_request_sha256,
                            'provider_response_sha256', p_provider_response_sha256,
                            'observed_raw_response_sha256', p_observed_raw_response_sha256,
                            'observed_normalized_schema_sha256', p_observed_normalized_schema_sha256,
                            'journal_receipt_sha256', p_journal_receipt_sha256,
                            'journal_state', p_journal_state,
                            'journal_sequence', p_journal_sequence,
                            'evidence_sha256', p_evidence_sha256
                        )
                    );
                END IF;
                UPDATE careerops.side_effect_attempts
                SET state = 'rejected',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'greenhouse',
                        'error_code', p_error_code,
                        'reason_codes', to_jsonb(p_reason_codes),
                        'reconciliation_key', p_reconciliation_key,
                        'http_status', p_http_status
                    ) || jsonb_strip_nulls(jsonb_build_object(
                        'broker_request_sha256', p_broker_request_sha256,
                        'broker_response_sha256', p_broker_response_sha256,
                        'provider_request_sha256', p_provider_request_sha256,
                        'provider_response_sha256', p_provider_response_sha256,
                        'observed_raw_response_sha256', p_observed_raw_response_sha256,
                        'observed_normalized_schema_sha256', p_observed_normalized_schema_sha256,
                        'journal_receipt_sha256', p_journal_receipt_sha256,
                        'journal_state', p_journal_state,
                        'journal_sequence', p_journal_sequence,
                        'evidence_sha256', p_evidence_sha256,
                        'submission_identity', p_submission_identity,
                        'payload_hash', p_payload_hash,
                        'material_hash', p_material_hash
                    ))
                WHERE id = v_attempt.id;
                UPDATE careerops.action_intents
                SET status = 'failed', updated_at = v_now
                WHERE id = v_event.action_intent_id;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_REJECTED_FUNCTION}{_RECORD_REJECTED_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.greenhouse_submit_record_prepost_failure(
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
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL
                   OR p_lease_owner IS NULL OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_error_code IS NULL OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                    RAISE EXCEPTION 'greenhouse prepost payload is invalid'
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
                   OR v_event.event_key NOT LIKE 'greenhouse-submit:%' THEN
                    RAISE EXCEPTION 'greenhouse prepost lease is invalid'
                        USING ERRCODE = '55000';
                END IF;
                UPDATE careerops.side_effect_attempts
                SET state = 'failed',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'greenhouse',
                        'prepost_failure', true,
                        'error_code', p_error_code
                    )
                WHERE action_intent_id = v_event.action_intent_id
                  AND outbox_event_id = v_event.id
                  AND state = 'processing';
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_PREPOST_FAILURE_FUNCTION}{_RECORD_PREPOST_FAILURE_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.submit_greenhouse_submit_reconciliation_evidence(
                p_owner_user_id uuid,
                p_event_id uuid,
                p_evidence_source text,
                p_evidence_sha256 text,
                p_observed_status text,
                p_observed_at timestamp with time zone,
                p_reason_code text,
                p_idempotency_key text,
                p_trace_id text
            )
            RETURNS TABLE (
                reconciliation_case_id uuid,
                evidence_review_id uuid,
                reconciliation_status text,
                evidence_sha256 text,
                evidence_source text,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_id uuid := gen_random_uuid();
                v_existing careerops.greenhouse_submit_reconciliation_evidence%ROWTYPE;
                v_request_sha256 text;
            BEGIN
                IF p_owner_user_id IS NULL OR p_event_id IS NULL
                   OR p_evidence_source NOT IN ('employer_admin', 'recruiting_webhook', 'manual_employer_system')
                   OR p_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_observed_status NOT IN ('accepted_unverified', 'ambiguous', 'provider_rejected', 'reconciliation_required')
                   OR p_observed_at IS NULL
                   OR p_reason_code IS NULL OR p_reason_code !~ '^[A-Z0-9_]{1,64}$'
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'greenhouse reconciliation evidence payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                v_request_sha256 := encode(sha256(convert_to(jsonb_build_object(
                    'owner_user_id', p_owner_user_id::text,
                    'outbox_event_id', p_event_id::text,
                    'evidence_source', p_evidence_source,
                    'evidence_sha256', p_evidence_sha256,
                    'observed_status', p_observed_status,
                    'observed_at', p_observed_at::text,
                    'reason_code', p_reason_code
                )::text, 'UTF8')), 'hex');
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_reconciliation_evidence
                WHERE owner_user_id = p_owner_user_id
                  AND idempotency_key = p_idempotency_key
                FOR KEY SHARE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'greenhouse reconciliation evidence idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    RETURN QUERY SELECT
                        v_existing.outbox_event_id,
                        v_existing.id,
                        v_existing.observed_status::text,
                        v_existing.evidence_sha256::text,
                        v_existing.evidence_source::text,
                        'replayed'::text;
                    RETURN;
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.greenhouse_submit_reconciliation_jobs AS job
                    JOIN careerops.outbox_events AS event
                      ON event.id = job.outbox_event_id
                     AND event.event_key LIKE 'greenhouse-submit:%'
                    JOIN careerops.greenhouse_submit_review_evidence AS review
                      ON review.action_intent_id = event.action_intent_id
                     AND review.payload_version_id = event.payload_version_id
                     AND review.owner_user_id = p_owner_user_id
                    WHERE job.outbox_event_id = p_event_id
                      AND job.status = 'required'
                ) THEN
                    RAISE EXCEPTION 'greenhouse reconciliation case is missing or not reviewable'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO careerops.greenhouse_submit_reconciliation_evidence (
                    id, owner_user_id, outbox_event_id, evidence_source,
                    evidence_sha256, observed_status, observed_at, reason_code,
                    request_sha256, idempotency_key, trace_id
                ) VALUES (
                    v_id, p_owner_user_id, p_event_id, p_evidence_source,
                    p_evidence_sha256, p_observed_status, p_observed_at, p_reason_code,
                    v_request_sha256, p_idempotency_key, p_trace_id
                );
                RETURN QUERY SELECT
                    p_event_id,
                    v_id,
                    p_observed_status::text,
                    p_evidence_sha256::text,
                    p_evidence_source::text,
                    'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_SUBMIT_RECONCILIATION_EVIDENCE_FUNCTION}{_SUBMIT_RECONCILIATION_EVIDENCE_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.review_greenhouse_submit_reconciliation_evidence(
                p_owner_user_id uuid,
                p_event_id uuid,
                p_evidence_review_id uuid,
                p_evidence_sha256 text,
                p_reviewed_evidence_source text,
                p_reviewed_observed_status text,
                p_decision text,
                p_reviewed_employer_authorization_evidence_sha256 text,
                p_reviewed_by_user_id uuid,
                p_review_snapshot_sha256 text,
                p_reason text,
                p_idempotency_key text,
                p_trace_id text
            )
            RETURNS TABLE (
                reconciliation_case_id uuid,
                evidence_review_id uuid,
                reconciliation_status text,
                reviewed_evidence_source text,
                reviewed_observed_status text,
                reviewed_employer_authorization_evidence_sha256 text,
                receipt_state text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                v_review_id uuid := gen_random_uuid();
                v_evidence careerops.greenhouse_submit_reconciliation_evidence%ROWTYPE;
                v_existing careerops.greenhouse_submit_reconciliation_reviews%ROWTYPE;
                v_request_sha256 text;
            BEGIN
                IF p_owner_user_id IS NULL OR p_event_id IS NULL OR p_evidence_review_id IS NULL
                   OR p_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_reviewed_evidence_source NOT IN ('employer_admin', 'recruiting_webhook', 'manual_employer_system')
                   OR p_reviewed_observed_status NOT IN ('accepted_unverified', 'ambiguous', 'provider_rejected', 'reconciliation_required')
                   OR p_decision NOT IN ('confirmed', 'resolved_absent', 'blocked')
                   OR p_reviewed_employer_authorization_evidence_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_reviewed_by_user_id IS NULL OR p_reviewed_by_user_id <> p_owner_user_id
                   OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
                   OR p_reason IS NULL OR btrim(p_reason) = ''
                   OR p_idempotency_key IS NULL OR btrim(p_idempotency_key) = ''
                   OR p_trace_id IS NULL OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'greenhouse reconciliation review payload is invalid'
                        USING ERRCODE = '22023';
                END IF;
                v_request_sha256 := encode(sha256(convert_to(jsonb_build_object(
                    'owner_user_id', p_owner_user_id::text,
                    'outbox_event_id', p_event_id::text,
                    'evidence_review_id', p_evidence_review_id::text,
                    'evidence_sha256', p_evidence_sha256,
                    'reviewed_evidence_source', p_reviewed_evidence_source,
                    'reviewed_observed_status', p_reviewed_observed_status,
                    'decision', p_decision,
                    'reviewed_employer_authorization_evidence_sha256',
                    p_reviewed_employer_authorization_evidence_sha256,
                    'reviewed_by_user_id', p_reviewed_by_user_id::text,
                    'review_snapshot_sha256', p_review_snapshot_sha256,
                    'reason', p_reason
                )::text, 'UTF8')), 'hex');
                SELECT * INTO v_existing
                FROM careerops.greenhouse_submit_reconciliation_reviews
                WHERE owner_user_id = p_owner_user_id
                  AND idempotency_key = p_idempotency_key
                FOR KEY SHARE;
                IF v_existing.id IS NOT NULL THEN
                    IF v_existing.request_sha256 <> v_request_sha256 THEN
                        RAISE EXCEPTION 'greenhouse reconciliation review idempotency key conflicts'
                            USING ERRCODE = '23505';
                    END IF;
                    SELECT * INTO v_evidence
                    FROM careerops.greenhouse_submit_reconciliation_evidence
                    WHERE id = v_existing.evidence_id
                    FOR KEY SHARE;
                    RETURN QUERY SELECT
                        v_existing.outbox_event_id,
                        v_existing.evidence_id,
                        v_existing.decision,
                        v_evidence.evidence_source,
                        v_evidence.observed_status,
                        v_existing.reviewed_employer_authorization_evidence_sha256,
                        'replayed'::text;
                    RETURN;
                END IF;
                SELECT * INTO v_evidence
                FROM careerops.greenhouse_submit_reconciliation_evidence
                WHERE id = p_evidence_review_id
                  AND owner_user_id = p_owner_user_id
                  AND outbox_event_id = p_event_id
                  AND evidence_sha256 = p_evidence_sha256
                  AND evidence_source = p_reviewed_evidence_source
                  AND observed_status = p_reviewed_observed_status
                FOR KEY SHARE;
                IF v_evidence.id IS NULL THEN
                    RAISE EXCEPTION 'greenhouse reconciliation evidence is missing'
                        USING ERRCODE = '23503';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM careerops.outbox_events AS event
                    JOIN careerops.greenhouse_submit_review_evidence AS review
                      ON review.action_intent_id = event.action_intent_id
                     AND review.payload_version_id = event.payload_version_id
                     AND review.owner_user_id = p_owner_user_id
                    JOIN careerops.greenhouse_submit_accounts AS account
                      ON account.id = review.account_id
                     AND account.owner_user_id = p_owner_user_id
                    WHERE event.id = v_evidence.outbox_event_id
                      AND account.employer_authorization_evidence_sha256 = p_reviewed_employer_authorization_evidence_sha256
                ) THEN
                    RAISE EXCEPTION 'greenhouse reconciliation review is not bound to employer authorization'
                        USING ERRCODE = '23514';
                END IF;
                INSERT INTO careerops.greenhouse_submit_reconciliation_reviews (
                    id, owner_user_id, evidence_id, outbox_event_id,
                    evidence_sha256, decision,
                    reviewed_employer_authorization_evidence_sha256,
                    reviewed_by_user_id, review_snapshot_sha256, reason,
                    request_sha256, idempotency_key, trace_id
                ) VALUES (
                    v_review_id, p_owner_user_id, v_evidence.id, v_evidence.outbox_event_id,
                    p_evidence_sha256, p_decision,
                    p_reviewed_employer_authorization_evidence_sha256,
                    p_reviewed_by_user_id, p_review_snapshot_sha256, p_reason,
                    v_request_sha256, p_idempotency_key, p_trace_id
                );
                UPDATE careerops.greenhouse_submit_reconciliation_jobs
                SET status = p_decision,
                    resolution_evidence_sha256 = p_evidence_sha256,
                    resolution_reviewed_by_user_id = p_reviewed_by_user_id,
                    resolution_source = v_evidence.evidence_source,
                    resolution_reason = p_reason,
                    resolved_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE outbox_event_id = v_evidence.outbox_event_id
                  AND status = 'required';
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'greenhouse reconciliation case is not required'
                        USING ERRCODE = '23514';
                END IF;
                UPDATE careerops.action_intents AS intent
                SET status = CASE WHEN p_decision = 'confirmed' THEN 'confirmed' ELSE 'failed' END,
                    updated_at = CURRENT_TIMESTAMP
                FROM careerops.outbox_events AS event
                WHERE event.id = v_evidence.outbox_event_id
                  AND intent.id = event.action_intent_id;
                RETURN QUERY SELECT
                    v_evidence.outbox_event_id,
                    v_evidence.id,
                    p_decision,
                    v_evidence.evidence_source,
                    v_evidence.observed_status,
                    p_reviewed_employer_authorization_evidence_sha256,
                    'created'::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_REVIEW_RECONCILIATION_EVIDENCE_FUNCTION}{_REVIEW_RECONCILIATION_EVIDENCE_SIGNATURE} FROM PUBLIC"
        )
    )


def upgrade() -> None:
    _install_greenhouse_sender_role()
    _create_tables()
    _install_append_only_guards()
    _install_cap_reservation_insert_guard(include_greenhouse=True)
    _install_register_function()
    _install_create_review_functions()
    _install_reserve_function()
    _install_outbox_functions()
    for statement in (
        *(f"REVOKE ALL ON careerops.{table_name} FROM PUBLIC" for table_name in _GREENHOUSE_TABLES),
        *(
            f"REVOKE ALL ON careerops.{table_name} FROM careerops_api, careerops_workflow, careerops_mailbox, careerops_outbox"
            for table_name in _GREENHOUSE_TABLES
        ),
    ):
        op.execute(sa.text(statement))
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_greenhouse_sender", _SENDER_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role(
        "careerops_readonly",
        tuple(
            statement.replace("GRANT SELECT", "REVOKE SELECT").replace(
                " TO careerops_readonly", " FROM careerops_readonly"
            )
            for statement in _READONLY_GRANTS
        ),
    )
    _run_for_role(
        "careerops_greenhouse_sender",
        tuple(
            statement.replace("GRANT EXECUTE", "REVOKE EXECUTE")
            .replace("GRANT USAGE", "REVOKE USAGE")
            .replace(
                " TO careerops_greenhouse_sender",
                " FROM careerops_greenhouse_sender",
            )
            for statement in _SENDER_GRANTS
        ),
    )
    _run_for_role(
        "careerops_api",
        tuple(
            statement.replace("GRANT EXECUTE", "REVOKE EXECUTE").replace(
                " TO careerops_api", " FROM careerops_api"
            )
            for statement in _API_GRANTS
        ),
    )
    _drop_functions()
    _install_cap_reservation_insert_guard(include_greenhouse=False)
    _drop_append_only_guards()
    for table_name in reversed(_GREENHOUSE_TABLES):
        op.drop_table(table_name, schema="careerops")
    _drop_greenhouse_sender_role_if_empty()
