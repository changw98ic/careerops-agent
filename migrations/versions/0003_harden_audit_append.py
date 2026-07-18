"""harden audit append behind database-owned function

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _api_role_exists() -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api')")
        )
    )


def _run_for_api_role(statement: str) -> None:
    if op.get_context().as_sql:
        op.execute(sa.text(statement))
        return
    if _api_role_exists():
        op.execute(sa.text(statement))


def _revoke_audit_sequence_from_api() -> None:
    op.execute(
        sa.text(
            """
            DO $block$
            DECLARE
                sequence_name regclass;
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api') THEN
                    sequence_name := pg_get_serial_sequence(
                        'careerops.audit_events', 'sequence'
                    )::regclass;
                    IF sequence_name IS NOT NULL THEN
                        EXECUTE format(
                            'REVOKE USAGE, SELECT ON SEQUENCE %s FROM careerops_api',
                            sequence_name
                        );
                    END IF;
                END IF;
            END
            $block$
            """
        )
    )


def _grant_audit_sequence_to_api() -> None:
    op.execute(
        sa.text(
            """
            DO $block$
            DECLARE
                sequence_name regclass;
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'careerops_api') THEN
                    sequence_name := pg_get_serial_sequence(
                        'careerops.audit_events', 'sequence'
                    )::regclass;
                    IF sequence_name IS NOT NULL THEN
                        EXECUTE format(
                            'GRANT USAGE, SELECT ON SEQUENCE %s TO careerops_api',
                            sequence_name
                        );
                    END IF;
                END IF;
            END
            $block$
            """
        )
    )


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.append_audit_event(
                p_event_id uuid,
                p_occurred_at timestamp with time zone,
                p_actor_type text,
                p_actor_id text,
                p_event_type text,
                p_resource_type text,
                p_resource_id uuid,
                p_trace_id text,
                p_event_data jsonb DEFAULT '{}'::jsonb
            )
            RETURNS TABLE (
                sequence bigint,
                event_id uuid,
                previous_hash text,
                event_hash text
            )
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                audit_chain_lock_id bigint := 4846240942507737921;
                normalized_event_data jsonb := COALESCE(p_event_data, '{}'::jsonb);
                normalized_occurred_at text;
                tail_hash text;
                computed_hash text;
                inserted_sequence bigint;
            BEGIN
                IF p_event_id IS NULL THEN
                    RAISE EXCEPTION 'audit event_id must not be null'
                        USING ERRCODE = '23502';
                END IF;
                IF p_occurred_at IS NULL THEN
                    RAISE EXCEPTION 'audit occurred_at must not be null'
                        USING ERRCODE = '23502';
                END IF;
                IF p_actor_type IS NULL OR btrim(p_actor_type) = '' THEN
                    RAISE EXCEPTION 'audit actor_type must not be blank'
                        USING ERRCODE = '22023';
                END IF;
                IF p_event_type IS NULL OR btrim(p_event_type) = '' THEN
                    RAISE EXCEPTION 'audit event_type must not be blank'
                        USING ERRCODE = '22023';
                END IF;
                IF p_resource_type IS NULL OR btrim(p_resource_type) = '' THEN
                    RAISE EXCEPTION 'audit resource_type must not be blank'
                        USING ERRCODE = '22023';
                END IF;
                IF p_resource_id IS NULL THEN
                    RAISE EXCEPTION 'audit resource_id must not be null'
                        USING ERRCODE = '23502';
                END IF;
                IF p_trace_id IS NULL OR btrim(p_trace_id) = '' THEN
                    RAISE EXCEPTION 'audit trace_id must not be blank'
                        USING ERRCODE = '22023';
                END IF;

                PERFORM pg_advisory_xact_lock(audit_chain_lock_id);

                SELECT careerops.audit_events.event_hash
                INTO tail_hash
                FROM careerops.audit_events
                ORDER BY careerops.audit_events.sequence DESC
                LIMIT 1;

                normalized_occurred_at := to_char(
                    p_occurred_at AT TIME ZONE 'UTC',
                    'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
                );
                computed_hash := encode(
                    sha256(
                        convert_to(
                            jsonb_build_object(
                                'actor_id', p_actor_id,
                                'actor_type', p_actor_type,
                                'event_data', normalized_event_data,
                                'event_id', p_event_id::text,
                                'event_type', p_event_type,
                                'occurred_at', normalized_occurred_at,
                                'previous_hash', tail_hash,
                                'resource_id', p_resource_id::text,
                                'resource_type', p_resource_type,
                                'trace_id', p_trace_id
                            )::text,
                            'UTF8'
                        )
                    ),
                    'hex'
                );

                INSERT INTO careerops.audit_events (
                    event_id,
                    occurred_at,
                    actor_type,
                    actor_id,
                    event_type,
                    resource_type,
                    resource_id,
                    trace_id,
                    event_data,
                    previous_hash,
                    event_hash
                ) VALUES (
                    p_event_id,
                    p_occurred_at,
                    p_actor_type,
                    p_actor_id,
                    p_event_type,
                    p_resource_type,
                    p_resource_id,
                    p_trace_id,
                    normalized_event_data,
                    tail_hash,
                    computed_hash
                )
                RETURNING careerops.audit_events.sequence INTO inserted_sequence;

                RETURN QUERY SELECT inserted_sequence, p_event_id, tail_hash, computed_hash;
            END
            $function$
            """
        )
    )
    op.execute(sa.text("REVOKE ALL ON FUNCTION careerops.append_audit_event FROM PUBLIC"))
    _run_for_api_role("REVOKE INSERT ON careerops.audit_events FROM careerops_api")
    _revoke_audit_sequence_from_api()
    _run_for_api_role("GRANT EXECUTE ON FUNCTION careerops.append_audit_event TO careerops_api")


def downgrade() -> None:
    _run_for_api_role("REVOKE EXECUTE ON FUNCTION careerops.append_audit_event FROM careerops_api")
    op.execute(sa.text("DROP FUNCTION careerops.append_audit_event"))
    _run_for_api_role("GRANT INSERT ON careerops.audit_events TO careerops_api")
    _grant_audit_sequence_to_api()
