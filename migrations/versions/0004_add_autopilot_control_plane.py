"""add append-only autopilot control plane

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "autopilot_campaigns",
    "autopilot_grant_versions",
    "autopilot_grant_revocations",
    "autopilot_intent_authorizations",
    "autopilot_review_items",
)

_API_GRANTS = (
    "GRANT SELECT ON careerops.autopilot_campaigns, "
    "careerops.autopilot_grant_versions, careerops.autopilot_grant_revocations, "
    "careerops.autopilot_intent_authorizations, careerops.autopilot_review_items "
    "TO careerops_api",
    "GRANT INSERT (id, owner_user_id, name, objective, criteria, exclusions, created_by) "
    "ON careerops.autopilot_campaigns TO careerops_api",
    "GRANT INSERT (id, campaign_id, version, subject_actor, allowed_action_kinds, "
    "allowed_channels, allowed_target_hosts, material_hashes, max_total_submissions, "
    "max_daily_submissions, max_per_company, policy_ruleset_version, release_version, "
    "expires_at) ON careerops.autopilot_grant_versions TO careerops_api",
    "GRANT INSERT (id, grant_version_id, revoked_by_user_id, "
    "superseded_by_grant_version_id, reason) "
    "ON careerops.autopilot_grant_revocations TO careerops_api",
    "GRANT INSERT (id, campaign_id, grant_version_id, action_intent_id, "
    "payload_version_id, payload_hash, policy_decision_id, authorization_outcome, "
    "reason_codes, authorized_at, expires_at) "
    "ON careerops.autopilot_intent_authorizations TO careerops_api",
    "GRANT INSERT (id, authorization_id, review_kind, resolution_mode, reason_codes, "
    "snapshot, created_by) ON careerops.autopilot_review_items TO careerops_api",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.autopilot_campaigns, "
    "careerops.autopilot_grant_versions, careerops.autopilot_grant_revocations, "
    "careerops.autopilot_intent_authorizations, careerops.autopilot_review_items "
    "TO careerops_readonly",
)

_API_REVOKES = tuple(
    f"REVOKE ALL ON careerops.{table_name} FROM careerops_api" for table_name in APPEND_ONLY_TABLES
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


def _allow_autopilot_submission_policy_decision() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE careerops.policy_decisions "
            "ALTER COLUMN decision TYPE character varying(32)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE careerops.policy_decisions "
            "DROP CONSTRAINT ck_policy_decisions_decision_values"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE careerops.policy_decisions "
            "ADD CONSTRAINT ck_policy_decisions_decision_values "
            "CHECK (decision IN ("
            "'deny', 'require_approval', 'allow', 'allow_autopilot_submission'"
            "))"
        )
    )


def _restore_policy_decision_values() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE careerops.policy_decisions "
            "DROP CONSTRAINT ck_policy_decisions_decision_values"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE careerops.policy_decisions "
            "ADD CONSTRAINT ck_policy_decisions_decision_values "
            "CHECK (decision IN ('deny', 'require_approval', 'allow')) NOT VALID"
        )
    )


def _install_authorization_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_autopilot_authorization_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                grant_created_at timestamp with time zone;
                grant_expires_at timestamp with time zone;
                grant_policy_ruleset_version text;
                policy_decision text;
                policy_expires_at timestamp with time zone;
                policy_payload_hash text;
                policy_ruleset_version text;
            BEGIN
                SELECT
                    grant_version.created_at,
                    grant_version.expires_at,
                    grant_version.policy_ruleset_version
                INTO grant_created_at, grant_expires_at, grant_policy_ruleset_version
                FROM careerops.autopilot_grant_versions AS grant_version
                WHERE grant_version.id = NEW.grant_version_id
                  AND grant_version.campaign_id = NEW.campaign_id
                FOR KEY SHARE;

                IF grant_expires_at IS NULL THEN
                    RAISE EXCEPTION 'autopilot grant % is missing for campaign %',
                        NEW.grant_version_id, NEW.campaign_id
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.authorized_at < grant_created_at THEN
                    RAISE EXCEPTION 'autopilot authorization predates grant creation'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.authorized_at > CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot authorization timestamp is in the future'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.authorized_at >= grant_expires_at THEN
                    RAISE EXCEPTION 'autopilot grant % is expired at %',
                        NEW.grant_version_id, NEW.authorized_at
                        USING ERRCODE = '23514';
                END IF;

                IF grant_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot grant % is expired at insert time',
                        NEW.grant_version_id
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.expires_at > grant_expires_at THEN
                    RAISE EXCEPTION 'autopilot authorization expiry exceeds grant expiry'
                        USING ERRCODE = '23514';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM careerops.autopilot_grant_revocations AS revocation
                    WHERE revocation.grant_version_id = NEW.grant_version_id
                ) THEN
                    RAISE EXCEPTION 'autopilot grant % is revoked at insert time',
                        NEW.grant_version_id
                        USING ERRCODE = '23514';
                END IF;

                SELECT
                    decision.decision,
                    decision.expires_at,
                    decision.payload_hash,
                    decision.ruleset_version
                INTO
                    policy_decision,
                    policy_expires_at,
                    policy_payload_hash,
                    policy_ruleset_version
                FROM careerops.policy_decisions AS decision
                WHERE decision.id = NEW.policy_decision_id
                  AND decision.action_intent_id = NEW.action_intent_id
                  AND decision.payload_version_id = NEW.payload_version_id
                FOR KEY SHARE;

                IF policy_decision IS NULL THEN
                    RAISE EXCEPTION 'autopilot policy decision % does not match intent/payload',
                        NEW.policy_decision_id
                        USING ERRCODE = '23503';
                END IF;

                IF policy_payload_hash <> NEW.payload_hash THEN
                    RAISE EXCEPTION 'autopilot authorization payload hash mismatch'
                        USING ERRCODE = '23514';
                END IF;

                IF policy_ruleset_version <> grant_policy_ruleset_version THEN
                    RAISE EXCEPTION 'autopilot policy decision ruleset does not match grant'
                        USING ERRCODE = '23514';
                END IF;

                IF policy_expires_at IS NULL THEN
                    RAISE EXCEPTION 'autopilot policy decision requires explicit expiry'
                        USING ERRCODE = '23514';
                END IF;

                IF policy_expires_at <= CURRENT_TIMESTAMP THEN
                    RAISE EXCEPTION 'autopilot policy decision is expired at insert time'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.expires_at > policy_expires_at THEN
                    RAISE EXCEPTION 'autopilot authorization expiry exceeds policy decision expiry'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.authorization_outcome = 'allow_autopilot_submission'
                   AND policy_decision <> 'allow_autopilot_submission' THEN
                    RAISE EXCEPTION
                        'autopilot authorization requires autopilot policy decision'
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
            "REVOKE ALL ON FUNCTION careerops.enforce_autopilot_authorization_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_intent_authorizations_insert_guard "
            "BEFORE INSERT ON careerops.autopilot_intent_authorizations "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_autopilot_authorization_insert()"
        )
    )


def _install_grant_revocation_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_autopilot_grant_revocation_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                grant_campaign_id uuid;
                grant_owner_user_id uuid;
                superseding_campaign_id uuid;
            BEGIN
                SELECT grant_version.campaign_id, campaign.owner_user_id
                INTO grant_campaign_id, grant_owner_user_id
                FROM careerops.autopilot_grant_versions AS grant_version
                JOIN careerops.autopilot_campaigns AS campaign
                  ON campaign.id = grant_version.campaign_id
                WHERE grant_version.id = NEW.grant_version_id
                FOR KEY SHARE;

                IF grant_campaign_id IS NULL THEN
                    RAISE EXCEPTION 'autopilot grant % is missing for revocation',
                        NEW.grant_version_id
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.revoked_by_user_id <> grant_owner_user_id THEN
                    RAISE EXCEPTION 'autopilot grant revocation requires campaign owner'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.superseded_by_grant_version_id IS NOT NULL THEN
                    SELECT superseding_grant.campaign_id
                    INTO superseding_campaign_id
                    FROM careerops.autopilot_grant_versions AS superseding_grant
                    WHERE superseding_grant.id = NEW.superseded_by_grant_version_id
                    FOR KEY SHARE;

                    IF superseding_campaign_id IS NULL THEN
                        RAISE EXCEPTION 'superseding autopilot grant % is missing',
                            NEW.superseded_by_grant_version_id
                            USING ERRCODE = '23503';
                    END IF;

                    IF superseding_campaign_id <> grant_campaign_id THEN
                        RAISE EXCEPTION 'superseding autopilot grant must belong to same campaign'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.enforce_autopilot_grant_revocation_insert() "
            "FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_grant_revocations_insert_guard "
            "BEFORE INSERT ON careerops.autopilot_grant_revocations "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_autopilot_grant_revocation_insert()"
        )
    )


def _drop_grant_revocation_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_grant_revocations_insert_guard "
            "ON careerops.autopilot_grant_revocations"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_autopilot_grant_revocation_insert()")
    )


def _drop_authorization_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_intent_authorizations_insert_guard "
            "ON careerops.autopilot_intent_authorizations"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_autopilot_authorization_insert()")
    )


def _install_grant_version_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_autopilot_grant_version_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                campaign_owner_user_id uuid;
            BEGIN
                SELECT campaign.owner_user_id
                INTO campaign_owner_user_id
                FROM careerops.autopilot_campaigns AS campaign
                WHERE campaign.id = NEW.campaign_id
                FOR KEY SHARE;

                IF campaign_owner_user_id IS NULL THEN
                    RAISE EXCEPTION 'autopilot campaign % is missing for grant', NEW.campaign_id
                        USING ERRCODE = '23503';
                END IF;

                IF NEW.subject_actor <> campaign_owner_user_id::text THEN
                    RAISE EXCEPTION 'autopilot grant subject actor must match campaign owner'
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
            "REVOKE ALL ON FUNCTION careerops.enforce_autopilot_grant_version_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_autopilot_grant_versions_insert_guard "
            "BEFORE INSERT ON careerops.autopilot_grant_versions "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_autopilot_grant_version_insert()"
        )
    )


def _drop_grant_version_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_autopilot_grant_versions_insert_guard "
            "ON careerops.autopilot_grant_versions"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_autopilot_grant_version_insert()")
    )


def upgrade() -> None:
    _allow_autopilot_submission_policy_decision()

    op.create_table(
        "autopilot_campaigns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "exclusions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_autopilot_campaigns_name_nonempty")),
        sa.CheckConstraint(
            "btrim(objective) <> ''",
            name=op.f("ck_autopilot_campaigns_objective_nonempty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(criteria) = 'object'",
            name=op.f("ck_autopilot_campaigns_criteria_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exclusions) = 'array'",
            name=op.f("ck_autopilot_campaigns_exclusions_array"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_autopilot_campaigns_owner_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_campaigns")),
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_campaigns_owner_created_at",
        "autopilot_campaigns",
        ["owner_user_id", "created_at"],
        unique=False,
        schema="careerops",
    )

    op.create_table(
        "autopilot_grant_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("subject_actor", sa.Text(), nullable=False),
        sa.Column(
            "allowed_action_kinds",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_channels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "allowed_target_hosts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "material_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("max_total_submissions", sa.Integer(), nullable=False),
        sa.Column("max_daily_submissions", sa.Integer(), nullable=False),
        sa.Column("max_per_company", sa.Integer(), nullable=False),
        sa.Column("policy_ruleset_version", sa.Text(), nullable=False),
        sa.Column("release_version", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_autopilot_grant_versions_version_positive")
        ),
        sa.CheckConstraint(
            "btrim(subject_actor) <> ''",
            name=op.f("ck_autopilot_grant_versions_subject_actor_nonempty"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_action_kinds) = 'array'",
            name=op.f("ck_autopilot_grant_versions_allowed_action_kinds_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_channels) = 'array'",
            name=op.f("ck_autopilot_grant_versions_allowed_channels_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_target_hosts) = 'array'",
            name=op.f("ck_autopilot_grant_versions_allowed_target_hosts_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(material_hashes) = 'array'",
            name=op.f("ck_autopilot_grant_versions_material_hashes_array"),
        ),
        sa.CheckConstraint(
            "max_total_submissions > 0",
            name=op.f("ck_autopilot_grant_versions_max_total_submissions_positive"),
        ),
        sa.CheckConstraint(
            "max_daily_submissions > 0",
            name=op.f("ck_autopilot_grant_versions_max_daily_submissions_positive"),
        ),
        sa.CheckConstraint(
            "max_per_company > 0",
            name=op.f("ck_autopilot_grant_versions_max_per_company_positive"),
        ),
        sa.CheckConstraint(
            "btrim(policy_ruleset_version) <> ''",
            name=op.f("ck_autopilot_grant_versions_policy_ruleset_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(release_version) <> ''",
            name=op.f("ck_autopilot_grant_versions_release_version_nonempty"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_autopilot_grant_versions_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["careerops.autopilot_campaigns.id"],
            name=op.f("fk_autopilot_grant_versions_campaign_id_autopilot_campaigns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_grant_versions")),
        sa.UniqueConstraint(
            "campaign_id",
            "id",
            name="uq_autopilot_grant_versions_campaign_id",
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "version",
            name="uq_autopilot_grant_versions_campaign_version",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_grant_versions_campaign_created_at",
        "autopilot_grant_versions",
        ["campaign_id", "created_at"],
        unique=False,
        schema="careerops",
    )

    op.create_table(
        "autopilot_grant_revocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_version_id", sa.Uuid(), nullable=False),
        sa.Column("revoked_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("superseded_by_grant_version_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_autopilot_grant_revocations_reason_nonempty"),
        ),
        sa.CheckConstraint(
            "superseded_by_grant_version_id IS NULL "
            "OR superseded_by_grant_version_id <> grant_version_id",
            name=op.f("ck_autopilot_grant_revocations_not_self_superseded"),
        ),
        sa.ForeignKeyConstraint(
            ["grant_version_id"],
            ["careerops.autopilot_grant_versions.id"],
            name=op.f("fk_autopilot_grant_revocations_grant_version_id_autopilot_grant_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["careerops.console_users.id"],
            name=op.f("fk_autopilot_grant_revocations_revoked_by_user_id_console_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_grant_version_id"],
            ["careerops.autopilot_grant_versions.id"],
            name=op.f(
                "fk_autopilot_grant_revocations_superseded_by_grant_version_id_"
                "autopilot_grant_versions"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_grant_revocations")),
        sa.UniqueConstraint(
            "grant_version_id",
            name=op.f("uq_autopilot_grant_revocations_grant_version_id"),
        ),
        schema="careerops",
    )

    op.create_table(
        "autopilot_intent_authorizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("grant_version_id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_decision_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "authorization_outcome IN ("
            "'allow_autopilot_submission', 'require_human_approval', "
            "'manual_only', 'deny')",
            name=op.f("ck_autopilot_intent_authorizations_authorization_outcome_values"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array'",
            name=op.f("ck_autopilot_intent_authorizations_reason_codes_array"),
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_autopilot_intent_authorizations_payload_hash_format"),
        ),
        sa.CheckConstraint(
            "expires_at > authorized_at",
            name=op.f("ck_autopilot_intent_authorizations_expiry_after_authorization"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_autopilot_intent_authorizations_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "grant_version_id"],
            [
                "careerops.autopilot_grant_versions.campaign_id",
                "careerops.autopilot_grant_versions.id",
            ],
            name="fk_autopilot_intent_authorizations_grant_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_autopilot_intent_authorizations_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "policy_decision_id"],
            [
                "careerops.policy_decisions.action_intent_id",
                "careerops.policy_decisions.payload_version_id",
                "careerops.policy_decisions.id",
            ],
            name="fk_autopilot_intent_authorizations_policy_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_intent_authorizations")),
        sa.UniqueConstraint(
            "action_intent_id",
            "payload_version_id",
            "policy_decision_id",
            name="uq_autopilot_intent_authorizations_policy_binding",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_intent_authorizations_campaign_created_at",
        "autopilot_intent_authorizations",
        ["campaign_id", "created_at"],
        unique=False,
        schema="careerops",
    )

    op.create_table(
        "autopilot_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("review_kind", sa.String(length=24), nullable=False),
        sa.Column("resolution_mode", sa.String(length=24), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_kind IN ('pending', 'exceptions', 'recovery')",
            name=op.f("ck_autopilot_review_items_review_kind_values"),
        ),
        sa.CheckConstraint(
            "resolution_mode IN ('agent_approvable', 'manual_only', 'remediation_required')",
            name=op.f("ck_autopilot_review_items_resolution_mode_values"),
        ),
        sa.CheckConstraint(
            "NOT (review_kind = 'pending' AND resolution_mode = 'manual_only')",
            name=op.f("ck_autopilot_review_items_manual_only_not_pending"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes) = 'array'",
            name=op.f("ck_autopilot_review_items_reason_codes_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name=op.f("ck_autopilot_review_items_snapshot_object"),
        ),
        sa.CheckConstraint(
            "btrim(created_by) <> ''",
            name=op.f("ck_autopilot_review_items_created_by_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["careerops.autopilot_intent_authorizations.id"],
            name=op.f("fk_autopilot_review_items_authorization_id_autopilot_intent_authorizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_review_items")),
        sa.UniqueConstraint(
            "authorization_id",
            "review_kind",
            name="uq_autopilot_review_items_authorization_kind",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_autopilot_review_items_resolution_created_at",
        "autopilot_review_items",
        ["resolution_mode", "created_at"],
        unique=False,
        schema="careerops",
    )

    _install_grant_version_guard()
    _install_authorization_guard()
    _install_grant_revocation_guard()
    _install_append_only_guards()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_append_only_guards()
    _drop_grant_revocation_guard()
    _drop_authorization_guard()
    _drop_grant_version_guard()
    op.drop_index(
        "ix_autopilot_review_items_resolution_created_at",
        table_name="autopilot_review_items",
        schema="careerops",
    )
    op.drop_table("autopilot_review_items", schema="careerops")
    op.drop_index(
        "ix_autopilot_intent_authorizations_campaign_created_at",
        table_name="autopilot_intent_authorizations",
        schema="careerops",
    )
    op.drop_table("autopilot_intent_authorizations", schema="careerops")
    op.drop_table("autopilot_grant_revocations", schema="careerops")
    op.drop_index(
        "ix_autopilot_grant_versions_campaign_created_at",
        table_name="autopilot_grant_versions",
        schema="careerops",
    )
    op.drop_table("autopilot_grant_versions", schema="careerops")
    op.drop_index(
        "ix_autopilot_campaigns_owner_created_at",
        table_name="autopilot_campaigns",
        schema="careerops",
    )
    op.drop_table("autopilot_campaigns", schema="careerops")
    _restore_policy_decision_values()
