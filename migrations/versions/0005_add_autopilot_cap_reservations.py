"""add append-only synthetic autopilot cap reservations

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("autopilot_cap_reservations",)

_API_GRANTS = (
    "GRANT SELECT ON careerops.autopilot_cap_reservations TO careerops_api",
    "GRANT INSERT (id, campaign_id, grant_version_id, authorization_id, action_intent_id, "
    "payload_version_id, payload_hash, target_host, channel, release_version, company_key, "
    "adapter_id, fixture_id, reservation_key, reconciliation_key) "
    "ON careerops.autopilot_cap_reservations TO careerops_api",
)

_READONLY_GRANTS = ("GRANT SELECT ON careerops.autopilot_cap_reservations TO careerops_readonly",)

_API_REVOKES = ("REVOKE ALL ON careerops.autopilot_cap_reservations FROM careerops_api",)

_READONLY_REVOKES = ("REVOKE ALL ON careerops.autopilot_cap_reservations FROM careerops_readonly",)


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


def _install_reservation_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_autopilot_cap_reservation_insert()
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
                -- Capacity time is owned by the database. Callers cannot backdate a retry
                -- into an earlier daily bucket or schedule a future reservation.
                NEW.reserved_at := CURRENT_TIMESTAMP;
                NEW.reservation_date := CURRENT_DATE;

                -- Serialize both trusted callers and direct inserts on the same
                -- grant without giving careerops_api UPDATE authority merely to
                -- acquire a row lock. The application takes this same lock before
                -- its idempotency lookup; acquiring it again here is transaction-
                -- reentrant and preserves the database-owned safety boundary.
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
                    RAISE EXCEPTION 'autopilot grant % is missing for cap reservation',
                        NEW.grant_version_id
                        USING ERRCODE = '23503';
                END IF;

                IF grant_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot grant % is expired for cap reservation',
                        NEW.grant_version_id
                        USING ERRCODE = '23514';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM careerops.autopilot_grant_revocations AS revocation
                    WHERE revocation.grant_version_id = NEW.grant_version_id
                ) THEN
                    RAISE EXCEPTION 'autopilot grant % is revoked for cap reservation',
                        NEW.grant_version_id
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
                    RAISE EXCEPTION 'autopilot authorization % does not match reservation',
                        NEW.authorization_id
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

                SELECT
                    intent.action_kind,
                    payload.target,
                    payload.attachment_refs
                INTO
                    intent_action_kind,
                    payload_target,
                    payload_attachment_refs
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

                IF intent_action_kind <> 'submit_application'
                   OR NOT COALESCE(
                       grant_allowed_action_kinds @> jsonb_build_array(intent_action_kind),
                       false
                   ) THEN
                    RAISE EXCEPTION 'cap reservation requires granted application submission action'
                        USING ERRCODE = '23514';
                END IF;

                IF right(lower(NEW.target_host), 5) <> '.test'
                   OR NEW.channel NOT LIKE 'synthetic:%'
                   OR NEW.channel <> ('synthetic:' || NEW.adapter_id) THEN
                    RAISE EXCEPTION 'cap reservation must remain in the synthetic sandbox'
                        USING ERRCODE = '23514';
                END IF;

                IF NOT COALESCE(
                    grant_allowed_channels @> jsonb_build_array(NEW.channel),
                    false
                ) OR NOT COALESCE(
                    grant_allowed_target_hosts @> jsonb_build_array(NEW.target_host),
                    false
                ) THEN
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
                           grant_material_hashes @> jsonb_build_array(
                               attachment.value ->> 'sha256'
                           ),
                           false
                       )
                ) THEN
                    RAISE EXCEPTION 'cap reservation includes material outside grant scope'
                        USING ERRCODE = '23514';
                END IF;

                SELECT count(*)
                INTO total_reserved
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.grant_version_id = NEW.grant_version_id;

                IF total_reserved >= grant_max_total THEN
                    RAISE EXCEPTION 'autopilot grant total cap exhausted'
                        USING ERRCODE = '23514';
                END IF;

                SELECT count(*)
                INTO daily_reserved
                FROM careerops.autopilot_cap_reservations AS reservation
                WHERE reservation.grant_version_id = NEW.grant_version_id
                  AND reservation.reservation_date = NEW.reservation_date;

                IF daily_reserved >= grant_max_daily THEN
                    RAISE EXCEPTION 'autopilot grant daily cap exhausted'
                        USING ERRCODE = '23514';
                END IF;

                SELECT count(*)
                INTO company_reserved
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
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.enforce_autopilot_cap_reservation_insert() "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_cap_reservations_insert_guard "
            "BEFORE INSERT ON careerops.autopilot_cap_reservations "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_autopilot_cap_reservation_insert()"
        )
    )


def _drop_reservation_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_cap_reservations_insert_guard "
            "ON careerops.autopilot_cap_reservations"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_autopilot_cap_reservation_insert()")
    )


def upgrade() -> None:
    op.create_table(
        "autopilot_cap_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("grant_version_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("target_host", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("release_version", sa.Text(), nullable=False),
        sa.Column("company_key", sa.Text(), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("fixture_id", sa.Text(), nullable=False),
        sa.Column("reservation_key", sa.Text(), nullable=False),
        sa.Column("reconciliation_key", sa.Text(), nullable=False),
        sa.Column(
            "reservation_date", sa.Date(), server_default=sa.text("CURRENT_DATE"), nullable=False
        ),
        sa.Column(
            "reserved_at",
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
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_autopilot_cap_reservations_payload_hash_format"),
        ),
        sa.CheckConstraint(
            "btrim(target_host) <> ''",
            name=op.f("ck_autopilot_cap_reservations_target_host_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(channel) <> ''",
            name=op.f("ck_autopilot_cap_reservations_channel_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(release_version) <> ''",
            name=op.f("ck_autopilot_cap_reservations_release_version_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(company_key) <> ''",
            name=op.f("ck_autopilot_cap_reservations_company_key_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(adapter_id) <> ''",
            name=op.f("ck_autopilot_cap_reservations_adapter_id_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(fixture_id) <> ''",
            name=op.f("ck_autopilot_cap_reservations_fixture_id_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(reservation_key) <> ''",
            name=op.f("ck_autopilot_cap_reservations_reservation_key_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(reconciliation_key) <> ''",
            name=op.f("ck_autopilot_cap_reservations_reconciliation_key_nonempty"),
        ),
        sa.CheckConstraint(
            "reservation_date = reserved_at::date",
            name=op.f("ck_autopilot_cap_reservations_reservation_date_matches_reserved_at"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_autopilot_cap_reservations_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
            ],
            name="fk_autopilot_cap_reservations_payload_version_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_autopilot_cap_reservations_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["careerops.autopilot_intent_authorizations.id"],
            name=op.f(
                "fk_autopilot_cap_reservations_authorization_id_autopilot_intent_authorizations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "grant_version_id"],
            [
                "careerops.autopilot_grant_versions.campaign_id",
                "careerops.autopilot_grant_versions.id",
            ],
            name="fk_autopilot_cap_reservations_grant_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_cap_reservations")),
        sa.UniqueConstraint(
            "authorization_id",
            name=op.f("uq_autopilot_cap_reservations_authorization_id"),
        ),
        sa.UniqueConstraint(
            "reservation_key",
            name=op.f("uq_autopilot_cap_reservations_reservation_key"),
        ),
        sa.UniqueConstraint(
            "reconciliation_key",
            name=op.f("uq_autopilot_cap_reservations_reconciliation_key"),
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_cap_reservations_grant_date",
        "autopilot_cap_reservations",
        ["grant_version_id", "reservation_date"],
        unique=False,
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_cap_reservations_grant_company",
        "autopilot_cap_reservations",
        ["grant_version_id", "company_key"],
        unique=False,
        schema="careerops",
    )
    _install_append_only_guards()
    _install_reservation_guard()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_reservation_guard()
    _drop_append_only_guards()
    op.drop_index(
        "ix_autopilot_cap_reservations_grant_company",
        table_name="autopilot_cap_reservations",
        schema="careerops",
    )
    op.drop_index(
        "ix_autopilot_cap_reservations_grant_date",
        table_name="autopilot_cap_reservations",
        schema="careerops",
    )
    op.drop_table("autopilot_cap_reservations", schema="careerops")
