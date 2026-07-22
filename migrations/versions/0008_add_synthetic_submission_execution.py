"""add durable synthetic submission execution boundary

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("autopilot_kill_switch_events",)

_KILL_SWITCH_TABLE = "autopilot_kill_switch_events"
_PREPARE_FUNCTION = "careerops.prepare_synthetic_submission_outbox_event"
_PREPARE_SIGNATURE = "(uuid, text, uuid)"
_RECORD_FUNCTION = "careerops.record_synthetic_submission_outbox_receipt"
_RECORD_SIGNATURE = "(uuid, text, uuid, text, text, text, text, timestamp with time zone, uuid)"
_AMBIGUOUS_FUNCTION = "careerops.record_synthetic_submission_outbox_ambiguity"
_AMBIGUOUS_SIGNATURE = "(uuid, text, uuid, text)"

_OUTBOX_GRANTS = (
    f"GRANT EXECUTE ON FUNCTION {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE} TO careerops_outbox",
    f"GRANT EXECUTE ON FUNCTION {_RECORD_FUNCTION}{_RECORD_SIGNATURE} TO careerops_outbox",
    f"GRANT EXECUTE ON FUNCTION {_AMBIGUOUS_FUNCTION}{_AMBIGUOUS_SIGNATURE} TO careerops_outbox",
)

_API_GRANTS = (
    "GRANT INSERT (release_evidence_hash, release_evidence_expires_at) "
    "ON careerops.autopilot_cap_reservations TO careerops_api",
    "GRANT SELECT ON careerops.autopilot_kill_switch_events TO careerops_api",
    "GRANT INSERT (id, scope_type, campaign_id, provider, active, reason, actor_user_id, "
    "idempotency_key, trace_id) ON careerops.autopilot_kill_switch_events TO careerops_api",
)

_READONLY_GRANTS = ("GRANT SELECT ON careerops.autopilot_kill_switch_events TO careerops_readonly",)

_API_REVOKES = ("REVOKE ALL ON careerops.autopilot_kill_switch_events FROM careerops_api",)

_OUTBOX_REVOKES = (
    f"REVOKE ALL ON FUNCTION {_AMBIGUOUS_FUNCTION}{_AMBIGUOUS_SIGNATURE} FROM careerops_outbox",
    f"REVOKE ALL ON FUNCTION {_RECORD_FUNCTION}{_RECORD_SIGNATURE} FROM careerops_outbox",
    f"REVOKE ALL ON FUNCTION {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE} FROM careerops_outbox",
)

_OUTBOX_TABLE_REVOKES = (
    "REVOKE ALL ON careerops.side_effect_attempts FROM careerops_outbox",
    "REVOKE ALL ON careerops.provider_receipts FROM careerops_outbox",
)

_READONLY_REVOKES = (
    "REVOKE ALL ON careerops.autopilot_kill_switch_events FROM careerops_readonly",
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


def _install_kill_switch_serialization_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.serialize_autopilot_kill_switch_event_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                scope_lock_key text;
            BEGIN
                scope_lock_key := CASE NEW.scope_type
                    WHEN 'global' THEN 'careerops:autopilot-kill-switch:global'
                    WHEN 'campaign' THEN
                        'careerops:autopilot-kill-switch:campaign:'
                        || COALESCE(NEW.campaign_id::text, '<missing>')
                    WHEN 'provider' THEN
                        'careerops:autopilot-kill-switch:provider:'
                        || COALESCE(NEW.provider, '<missing>')
                    ELSE 'careerops:autopilot-kill-switch:invalid:' || NEW.scope_type
                END;

                PERFORM pg_catalog.pg_advisory_xact_lock(
                    pg_catalog.hashtextextended(scope_lock_key, 0)
                );
                NEW.sequence := pg_catalog.nextval(
                    pg_catalog.pg_get_serial_sequence(
                        'careerops.autopilot_kill_switch_events',
                        'sequence'
                    )::regclass
                );
                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "careerops.serialize_autopilot_kill_switch_event_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_kill_switch_events_serialize_insert "
            "BEFORE INSERT ON careerops.autopilot_kill_switch_events "
            "FOR EACH ROW EXECUTE FUNCTION "
            "careerops.serialize_autopilot_kill_switch_event_insert()"
        )
    )


def _drop_kill_switch_serialization_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_kill_switch_events_serialize_insert "
            "ON careerops.autopilot_kill_switch_events"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.serialize_autopilot_kill_switch_event_insert()")
    )


def _install_reservation_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_autopilot_cap_reservation_execution_boundary()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                intent_payload_version_id uuid;
                latest_kill_active boolean;
            BEGIN
                IF NEW.release_evidence_hash IS NULL
                   OR NEW.release_evidence_hash !~ '^[0-9a-f]{64}$'
                   OR NEW.release_evidence_expires_at IS NULL
                   OR NEW.release_evidence_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'cap reservation requires current release evidence'
                        USING ERRCODE = '23514';
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:global',
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:campaign:' || NEW.campaign_id::text,
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:provider:synthetic',
                        0
                    )
                );

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
                  AND provider = 'synthetic'
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
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "careerops.enforce_autopilot_cap_reservation_execution_boundary() "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_cap_reservations_execution_boundary "
            "BEFORE INSERT ON careerops.autopilot_cap_reservations "
            "FOR EACH ROW EXECUTE FUNCTION "
            "careerops.enforce_autopilot_cap_reservation_execution_boundary()"
        )
    )


def _drop_reservation_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_cap_reservations_execution_boundary "
            "ON careerops.autopilot_cap_reservations"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "careerops.enforce_autopilot_cap_reservation_execution_boundary()"
        )
    )


def _install_prepare_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.prepare_synthetic_submission_outbox_event(
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
                existing_provider text,
                existing_provider_resource_id text,
                existing_provider_state text,
                existing_received_at timestamp with time zone,
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
                v_intent_status text;
                v_auth_outcome text;
                v_auth_expires_at timestamp with time zone;
                v_grant_expires_at timestamp with time zone;
                v_grant_release_version text;
                v_latest_kill_active boolean;
                v_attempt_id uuid;
                v_existing_attempt careerops.side_effect_attempts%ROWTYPE;
                v_receipt careerops.provider_receipts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                    RAISE EXCEPTION 'synthetic submission prepare requires event and lease token'
                        USING ERRCODE = '22004';
                END IF;

                IF p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
                    RAISE EXCEPTION 'synthetic submission lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;

                IF v_event.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic submission event % is missing', p_event_id
                        USING ERRCODE = '23503';
                END IF;

                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now THEN
                    RAISE EXCEPTION 'synthetic submission lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                IF v_event.event_type <> 'workflow_signal'
                   OR v_event.event_key NOT LIKE 'synthetic-dispatch:%' THEN
                    RAISE EXCEPTION 'synthetic submission event has invalid type or key'
                        USING ERRCODE = '23514';
                END IF;

                SELECT *
                INTO v_reservation
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.reservation_key = substring(
                      v_event.event_key FROM char_length('synthetic-dispatch:') + 1
                  )
                FOR UPDATE;

                IF v_reservation.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic submission event is not bound to a reservation'
                        USING ERRCODE = '23503';
                END IF;

                SELECT *
                INTO v_receipt
                FROM careerops.provider_receipts AS receipt
                WHERE receipt.provider = 'synthetic'
                  AND receipt.reconciliation_key = v_reservation.reconciliation_key;

                IF v_receipt.id IS NOT NULL THEN
                    IF v_receipt.final_state = 'ambiguous' THEN
                        RETURN QUERY
                        SELECT
                            v_event.id,
                            v_event.event_key,
                            v_event.action_intent_id,
                            v_event.payload_version_id,
                            v_reservation.reservation_key,
                            v_reservation.reconciliation_key,
                            NULL::text,
                            NULL::text,
                            NULL::text,
                            NULL::timestamp with time zone,
                            'reconciliation_required'::text,
                            'SYNTHETIC_PROVIDER_STATE_AMBIGUOUS'::text;
                        RETURN;
                    END IF;

                    RETURN QUERY
                    SELECT
                        v_event.id,
                        v_event.event_key,
                        v_event.action_intent_id,
                        v_event.payload_version_id,
                        v_reservation.reservation_key,
                        v_reservation.reconciliation_key,
                        v_receipt.provider::text,
                        v_receipt.provider_resource_id,
                        v_receipt.final_state::text,
                        v_receipt.received_at,
                        'already_confirmed'::text,
                        NULL::text;
                    RETURN;
                END IF;

                IF v_reservation.release_evidence_hash IS NULL
                   OR v_reservation.release_evidence_hash !~ '^[0-9a-f]{64}$'
                   OR v_reservation.release_evidence_expires_at IS NULL
                   OR v_reservation.release_evidence_expires_at <= v_now THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'SYNTHETIC_RELEASE_EVIDENCE_EXPIRED'::text;
                    RETURN;
                END IF;

                SELECT authz.authorization_outcome, authz.expires_at
                INTO v_auth_outcome, v_auth_expires_at
                FROM careerops.autopilot_intent_authorizations AS authz
                WHERE authz.id = v_reservation.authorization_id
                  AND authz.campaign_id = v_reservation.campaign_id
                  AND authz.grant_version_id = v_reservation.grant_version_id
                  AND authz.action_intent_id = v_reservation.action_intent_id
                  AND authz.payload_version_id = v_reservation.payload_version_id
                  AND authz.payload_hash = v_reservation.payload_hash
                FOR KEY SHARE;

                IF v_auth_outcome <> 'allow_autopilot_submission'
                   OR v_auth_expires_at <= v_now THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'SYNTHETIC_AUTHORIZATION_NOT_CURRENT'::text;
                    RETURN;
                END IF;

                SELECT grant_version.expires_at, grant_version.release_version
                INTO v_grant_expires_at, v_grant_release_version
                FROM careerops.autopilot_grant_versions AS grant_version
                WHERE grant_version.id = v_reservation.grant_version_id
                  AND grant_version.campaign_id = v_reservation.campaign_id
                FOR KEY SHARE;

                IF v_grant_expires_at <= v_now
                   OR v_grant_release_version IS DISTINCT FROM v_reservation.release_version
                   OR EXISTS (
                       SELECT 1
                       FROM careerops.autopilot_grant_revocations AS revocation
                       WHERE revocation.grant_version_id = v_reservation.grant_version_id
                   ) THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'SYNTHETIC_GRANT_NOT_CURRENT'::text;
                    RETURN;
                END IF;

                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:global',
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:campaign:'
                        || v_reservation.campaign_id::text,
                        0
                    )
                );
                PERFORM pg_catalog.pg_advisory_xact_lock_shared(
                    pg_catalog.hashtextextended(
                        'careerops:autopilot-kill-switch:provider:synthetic',
                        0
                    )
                );

                SELECT active
                INTO v_latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'global'
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(v_latest_kill_active, false) THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'GLOBAL_KILL_SWITCH_ACTIVE'::text;
                    RETURN;
                END IF;

                SELECT active
                INTO v_latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'campaign'
                  AND campaign_id = v_reservation.campaign_id
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(v_latest_kill_active, false) THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'CAMPAIGN_KILL_SWITCH_ACTIVE'::text;
                    RETURN;
                END IF;

                SELECT active
                INTO v_latest_kill_active
                FROM careerops.autopilot_kill_switch_events
                WHERE scope_type = 'provider'
                  AND provider = 'synthetic'
                ORDER BY sequence DESC
                LIMIT 1;
                IF COALESCE(v_latest_kill_active, false) THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'PROVIDER_KILL_SWITCH_ACTIVE'::text;
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

                IF v_existing_attempt.id IS NOT NULL THEN
                    UPDATE careerops.side_effect_attempts
                    SET state = 'failed',
                        finished_at = v_now,
                        error_code = 'SYNTHETIC_SUBMISSION_RECONCILIATION_REQUIRED',
                        response_metadata = jsonb_build_object(
                            'reason', 'leased event replayed before receipt was recorded'
                        )
                    WHERE id = v_existing_attempt.id;

                    UPDATE careerops.action_intents
                    SET status = 'reconciliation_required',
                        updated_at = v_now
                    WHERE id = v_event.action_intent_id;

                    PERFORM *
                    FROM careerops.append_audit_event(
                        gen_random_uuid(),
                        v_now,
                        'worker',
                        'careerops_outbox',
                        'synthetic_submission_reconciliation_required',
                        'action_intent',
                        v_event.action_intent_id,
                        v_reservation.reconciliation_key,
                        jsonb_build_object(
                            'outbox_event_id', v_event.id::text,
                            'reservation_id', v_reservation.id::text,
                            'attempt_id', v_existing_attempt.id::text
                        )
                    );

                    RETURN QUERY
                    SELECT
                        v_event.id,
                        v_event.event_key,
                        v_event.action_intent_id,
                        v_event.payload_version_id,
                        v_reservation.reservation_key,
                        v_reservation.reconciliation_key,
                        NULL::text,
                        NULL::text,
                        NULL::text,
                        NULL::timestamp with time zone,
                        'reconciliation_required'::text,
                        'SYNTHETIC_SUBMISSION_RECONCILIATION_REQUIRED'::text;
                    RETURN;
                END IF;

                SELECT status
                INTO v_intent_status
                FROM careerops.action_intents
                WHERE id = v_event.action_intent_id
                FOR UPDATE;

                IF v_intent_status IS DISTINCT FROM 'eligible' THEN
                    RETURN QUERY
                    SELECT
                        v_event.id, v_event.event_key, v_event.action_intent_id,
                        v_event.payload_version_id, v_reservation.reservation_key,
                        v_reservation.reconciliation_key, NULL::text, NULL::text,
                        NULL::text, NULL::timestamp with time zone, 'stopped'::text,
                        'SYNTHETIC_INTENT_NOT_ELIGIBLE'::text;
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
                    1,
                    'processing',
                    encode(
                        sha256(
                            convert_to(
                                jsonb_build_object(
                                    'event_id', v_event.id::text,
                                    'event_key', v_event.event_key,
                                    'payload_hash', v_reservation.payload_hash,
                                    'reconciliation_key', v_reservation.reconciliation_key,
                                    'reservation_key', v_reservation.reservation_key
                                )::text,
                                'UTF8'
                            )
                        ),
                        'hex'
                    ),
                    v_now,
                    jsonb_build_object(
                        'provider', 'synthetic',
                        'reservation_id', v_reservation.id::text,
                        'release_evidence_hash', v_reservation.release_evidence_hash
                    )
                );

                UPDATE careerops.action_intents
                SET status = 'processing',
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;

                RETURN QUERY
                SELECT
                    v_event.id,
                    v_event.event_key,
                    v_event.action_intent_id,
                    v_event.payload_version_id,
                    v_reservation.reservation_key,
                    v_reservation.reconciliation_key,
                    NULL::text,
                    NULL::text,
                    NULL::text,
                    NULL::timestamp with time zone,
                    'ready'::text,
                    NULL::text;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE} FROM PUBLIC")
    )


def _install_record_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.record_synthetic_submission_outbox_receipt(
                p_event_id uuid,
                p_lease_owner text,
                p_lease_token uuid,
                p_provider text,
                p_provider_resource_id text,
                p_reconciliation_key text,
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
                v_reservation careerops.autopilot_cap_reservations%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
                v_terminal_intent_status text;
                v_receipt_state text;
                v_error_code text;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL OR p_receipt_id IS NULL THEN
                    RAISE EXCEPTION
                        'synthetic receipt recording requires event, lease, and receipt ids'
                        USING ERRCODE = '22004';
                END IF;

                IF p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
                    RAISE EXCEPTION 'synthetic receipt lease owner is invalid'
                        USING ERRCODE = '22023';
                END IF;

                IF p_provider <> 'synthetic'
                   OR p_provider_resource_id IS NULL
                   OR p_provider_resource_id !~ '^[A-Za-z0-9._:/-]{1,160}$'
                   OR p_reconciliation_key IS NULL
                   OR p_reconciliation_key !~ '^[A-Za-z0-9._:/-]{1,160}$'
                   OR p_provider_state NOT IN ('confirmed', 'ambiguous')
                   OR p_received_at IS NULL THEN
                    RAISE EXCEPTION 'synthetic receipt payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;

                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now THEN
                    RAISE EXCEPTION 'synthetic receipt lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                SELECT *
                INTO v_reservation
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.reservation_key = substring(
                      v_event.event_key FROM char_length('synthetic-dispatch:') + 1
                  )
                  AND reservation.reconciliation_key = p_reconciliation_key
                FOR UPDATE;

                IF v_reservation.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic receipt is not bound to the reservation'
                        USING ERRCODE = '23503';
                END IF;

                IF v_reservation.release_evidence_hash IS NULL
                   OR v_reservation.release_evidence_expires_at IS NULL THEN
                    RAISE EXCEPTION 'synthetic receipt release evidence is missing'
                        USING ERRCODE = '23514';
                END IF;

                SELECT *
                INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                  AND attempt.state = 'processing'
                FOR UPDATE;

                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic receipt requires one processing attempt'
                        USING ERRCODE = '23514';
                END IF;

                v_receipt_state := CASE
                    WHEN p_provider_state = 'confirmed' THEN 'confirmed'
                    ELSE 'ambiguous'
                END;
                v_terminal_intent_status := CASE
                    WHEN p_provider_state = 'confirmed' THEN 'confirmed'
                    ELSE 'reconciliation_required'
                END;
                v_error_code := CASE
                    WHEN p_provider_state = 'confirmed' THEN NULL
                    ELSE 'SYNTHETIC_PROVIDER_STATE_AMBIGUOUS'
                END;

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
                    'synthetic',
                    p_provider_resource_id,
                    p_reconciliation_key,
                    v_receipt_state,
                    p_received_at,
                    v_now,
                    jsonb_build_object(
                        'provider_state', p_provider_state,
                        'reservation_id', v_reservation.id::text
                    )
                );

                UPDATE careerops.side_effect_attempts
                SET state = v_receipt_state,
                    finished_at = v_now,
                    error_code = v_error_code,
                    response_metadata = jsonb_build_object(
                        'provider', 'synthetic',
                        'provider_resource_id', p_provider_resource_id,
                        'provider_state', p_provider_state,
                        'receipt_id', p_receipt_id::text
                    )
                WHERE id = v_attempt.id;

                UPDATE careerops.action_intents
                SET status = v_terminal_intent_status,
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;

                PERFORM *
                FROM careerops.append_audit_event(
                    gen_random_uuid(),
                    v_now,
                    'worker',
                    'careerops_outbox',
                    'synthetic_submission_receipt_recorded',
                    'action_intent',
                    v_event.action_intent_id,
                    p_reconciliation_key,
                    jsonb_build_object(
                        'outbox_event_id', v_event.id::text,
                        'reservation_id', v_reservation.id::text,
                        'attempt_id', v_attempt.id::text,
                        'receipt_id', p_receipt_id::text,
                        'provider_state', p_provider_state
                    )
                );
            END
            $function$
            """
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_RECORD_FUNCTION}{_RECORD_SIGNATURE} FROM PUBLIC"))


def _install_ambiguity_function() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.record_synthetic_submission_outbox_ambiguity(
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
                v_reservation careerops.autopilot_cap_reservations%ROWTYPE;
                v_attempt careerops.side_effect_attempts%ROWTYPE;
            BEGIN
                IF p_event_id IS NULL OR p_lease_token IS NULL THEN
                    RAISE EXCEPTION 'synthetic ambiguity recording requires event and lease ids'
                        USING ERRCODE = '22004';
                END IF;

                IF p_lease_owner IS NULL
                   OR p_lease_owner !~ '^[A-Za-z0-9._:-]{1,128}$'
                   OR p_error_code IS NULL
                   OR p_error_code !~ '^[A-Z0-9_]{1,64}$' THEN
                    RAISE EXCEPTION 'synthetic ambiguity payload is invalid'
                        USING ERRCODE = '22023';
                END IF;

                SELECT *
                INTO v_event
                FROM careerops.outbox_events
                WHERE id = p_event_id
                FOR UPDATE;

                IF v_event.status <> 'leased'
                   OR v_event.lease_owner IS DISTINCT FROM p_lease_owner
                   OR v_event.lease_token IS DISTINCT FROM p_lease_token
                   OR v_event.lease_until <= v_now THEN
                    RAISE EXCEPTION 'synthetic ambiguity lease is invalid'
                        USING ERRCODE = '55000';
                END IF;

                SELECT *
                INTO v_reservation
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.action_intent_id = v_event.action_intent_id
                  AND reservation.payload_version_id = v_event.payload_version_id
                  AND reservation.reservation_key = substring(
                      v_event.event_key FROM char_length('synthetic-dispatch:') + 1
                  )
                FOR UPDATE;

                IF v_reservation.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic ambiguity is not bound to a reservation'
                        USING ERRCODE = '23503';
                END IF;

                SELECT *
                INTO v_attempt
                FROM careerops.side_effect_attempts AS attempt
                WHERE attempt.action_intent_id = v_event.action_intent_id
                  AND attempt.outbox_event_id = v_event.id
                ORDER BY attempt.ordinal DESC
                LIMIT 1
                FOR UPDATE;

                IF v_attempt.id IS NULL THEN
                    RAISE EXCEPTION 'synthetic ambiguity requires an existing attempt'
                        USING ERRCODE = '23514';
                END IF;

                IF v_attempt.state = 'reconciliation_required' THEN
                    RETURN;
                END IF;

                UPDATE careerops.side_effect_attempts
                SET state = 'reconciliation_required',
                    finished_at = COALESCE(finished_at, v_now),
                    error_code = p_error_code,
                    response_metadata = response_metadata || jsonb_build_object(
                        'provider', 'synthetic',
                        'provider_state', 'ambiguous',
                        'error_code', p_error_code
                    )
                WHERE id = v_attempt.id;

                UPDATE careerops.action_intents
                SET status = 'reconciliation_required',
                    updated_at = v_now
                WHERE id = v_event.action_intent_id;

                PERFORM *
                FROM careerops.append_audit_event(
                    gen_random_uuid(),
                    v_now,
                    'worker',
                    'careerops_outbox',
                    'synthetic_submission_reconciliation_required',
                    'action_intent',
                    v_event.action_intent_id,
                    v_reservation.reconciliation_key,
                    jsonb_build_object(
                        'outbox_event_id', v_event.id::text,
                        'reservation_id', v_reservation.id::text,
                        'attempt_id', v_attempt.id::text,
                        'error_code', p_error_code
                    )
                );
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {_AMBIGUOUS_FUNCTION}{_AMBIGUOUS_SIGNATURE} FROM PUBLIC")
    )


def _drop_functions() -> None:
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_AMBIGUOUS_FUNCTION}{_AMBIGUOUS_SIGNATURE}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_RECORD_FUNCTION}{_RECORD_SIGNATURE}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_PREPARE_FUNCTION}{_PREPARE_SIGNATURE}"))


def upgrade() -> None:
    op.add_column(
        "autopilot_cap_reservations",
        sa.Column("release_evidence_hash", sa.String(length=64), nullable=True),
        schema="careerops",
    )
    op.add_column(
        "autopilot_cap_reservations",
        sa.Column("release_evidence_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema="careerops",
    )
    op.create_check_constraint(
        op.f("ck_autopilot_cap_reservations_release_evidence_hash_format"),
        "autopilot_cap_reservations",
        "release_evidence_hash IS NULL OR release_evidence_hash ~ '^[0-9a-f]{64}$'",
        schema="careerops",
    )
    op.create_check_constraint(
        op.f("ck_autopilot_cap_reservations_release_evidence_expiry_current"),
        "autopilot_cap_reservations",
        "(release_evidence_hash IS NULL) = (release_evidence_expires_at IS NULL) "
        "AND (release_evidence_expires_at IS NULL OR release_evidence_expires_at > reserved_at)",
        schema="careerops",
    )

    op.create_table(
        _KILL_SWITCH_TABLE,
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=24), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_autopilot_kill_switch_events_actor_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_autopilot_kill_switch_events_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "sequence",
            name=op.f("uq_autopilot_kill_switch_events_sequence"),
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'campaign', 'provider')",
            name=op.f("ck_autopilot_kill_switch_events_scope_type_values"),
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND campaign_id IS NULL AND provider IS NULL) OR "
            "(scope_type = 'campaign' AND campaign_id IS NOT NULL AND provider IS NULL) OR "
            "(scope_type = 'provider' AND campaign_id IS NULL AND provider IS NOT NULL)",
            name=op.f("ck_autopilot_kill_switch_events_scope_binding"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
            name=op.f("ck_autopilot_kill_switch_events_text_nonempty"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_kill_switch_events")),
        schema="careerops",
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_autopilot_kill_switch_events_latest "
            "ON careerops.autopilot_kill_switch_events "
            "(scope_type, campaign_id, provider, sequence DESC)"
        )
    )
    _install_kill_switch_serialization_guard()
    _install_append_only_guards()
    _install_reservation_guard()
    _install_prepare_function()
    _install_record_function()
    _install_ambiguity_function()
    _run_for_role("careerops_outbox", _OUTBOX_TABLE_REVOKES)
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_outbox", _OUTBOX_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_outbox", _OUTBOX_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_functions()
    _drop_reservation_guard()
    _drop_append_only_guards()
    _drop_kill_switch_serialization_guard()
    op.drop_index(
        "ix_autopilot_kill_switch_events_latest",
        table_name=_KILL_SWITCH_TABLE,
        schema="careerops",
    )
    op.drop_table(_KILL_SWITCH_TABLE, schema="careerops")
    op.drop_constraint(
        op.f("ck_autopilot_cap_reservations_release_evidence_expiry_current"),
        "autopilot_cap_reservations",
        schema="careerops",
    )
    op.drop_constraint(
        op.f("ck_autopilot_cap_reservations_release_evidence_hash_format"),
        "autopilot_cap_reservations",
        schema="careerops",
    )
    op.drop_column(
        "autopilot_cap_reservations",
        "release_evidence_expires_at",
        schema="careerops",
    )
    op.drop_column("autopilot_cap_reservations", "release_evidence_hash", schema="careerops")
