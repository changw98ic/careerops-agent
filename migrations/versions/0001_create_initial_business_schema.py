"""create initial business schema

Revision ID: 0001
Revises:
Create Date: 2026-07-17 15:49:23.128607
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "action_payload_versions",
    "application_events",
    "audit_events",
    "evidence_records",
    "job_merge_decisions",
    "job_posting_versions",
    "policy_decisions",
    "provider_receipts",
)

ROLE_GRANTS = {
    "careerops_api": (
        "GRANT USAGE ON SCHEMA careerops TO careerops_api",
        "GRANT SELECT ON careerops.candidates, careerops.companies, "
        "careerops.job_sources, careerops.content_blobs, careerops.content_objects, "
        "careerops.canonical_jobs, "
        "careerops.job_postings, careerops.job_posting_versions, "
        "careerops.job_merge_decisions, careerops.job_posting_assignments, "
        "careerops.job_aliases, careerops.evidence_records, careerops.application_events, "
        "careerops.action_intents, careerops.action_payload_versions, "
        "careerops.policy_decisions, careerops.approval_requests, "
        "careerops.outbox_events, careerops.audit_events TO careerops_api",
        "GRANT INSERT ON careerops.candidates, careerops.companies, "
        "careerops.job_sources, "
        "careerops.canonical_jobs, "
        "careerops.job_postings, careerops.job_posting_versions, "
        "careerops.job_merge_decisions, careerops.job_posting_assignments, "
        "careerops.job_aliases, careerops.evidence_records, careerops.application_events, "
        "careerops.action_payload_versions, "
        "careerops.policy_decisions, "
        "careerops.audit_events TO careerops_api",
        "GRANT INSERT (id, action_kind, resource_type, resource_id, idempotency_key, created_by) "
        "ON careerops.action_intents TO careerops_api",
        "GRANT INSERT (id, sha256, object_key, byte_size) "
        "ON careerops.content_blobs TO careerops_api",
        "GRANT INSERT (id, blob_id, media_type, classification, owner_resource_type, "
        "owner_resource_id, retention_until) ON careerops.content_objects TO careerops_api",
        "GRANT INSERT (id, action_intent_id, payload_version_id, policy_decision_id, "
        "requested_for, expires_at) ON careerops.approval_requests TO careerops_api",
        "GRANT INSERT (id, event_key, action_intent_id, payload_version_id, event_type, "
        "available_at) ON careerops.outbox_events TO careerops_api",
        "GRANT UPDATE (display_name, updated_at) ON careerops.candidates TO careerops_api",
        "GRANT UPDATE (name, official_domains, terms_status, updated_at) "
        "ON careerops.companies TO careerops_api",
        "GRANT UPDATE (state, verified_at, last_discovery_at, updated_at) "
        "ON careerops.job_sources TO careerops_api",
        "GRANT UPDATE (aggregate_state, primary_posting_id, updated_at) "
        "ON careerops.canonical_jobs TO careerops_api",
        "GRANT UPDATE (source_state, last_seen_at, closed_at, updated_at) "
        "ON careerops.job_postings TO careerops_api",
        "GRANT UPDATE (canonical_job_id, decision_id, assigned_at) "
        "ON careerops.job_posting_assignments TO careerops_api",
        "GRANT UPDATE (status, current_payload_version_id, updated_at) "
        "ON careerops.action_intents TO careerops_api",
        "GRANT UPDATE (decision, decision_rule_reference, decided_at) "
        "ON careerops.approval_requests TO careerops_api",
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA careerops TO careerops_api",
    ),
    "careerops_retention": (
        "GRANT USAGE ON SCHEMA careerops TO careerops_retention",
        "GRANT INSERT (id, sha256, object_key, byte_size, deletion_state, "
        "delete_lease_owner, delete_lease_token, delete_lease_until) "
        "ON careerops.content_blobs TO careerops_retention",
        "GRANT SELECT ON careerops.content_blobs, careerops.content_objects, "
        "careerops.evidence_records, careerops.job_posting_versions "
        "TO careerops_retention",
        "GRANT UPDATE (expired_at) ON careerops.content_objects TO careerops_retention",
        "GRANT UPDATE (deletion_state, delete_lease_owner, delete_lease_token, "
        "delete_lease_until, deleted_at, delete_result) ON careerops.content_blobs "
        "TO careerops_retention",
    ),
    "careerops_outbox": (
        "GRANT USAGE ON SCHEMA careerops TO careerops_outbox",
        "GRANT SELECT ON careerops.outbox_events TO careerops_outbox",
        "GRANT UPDATE (status, available_at, lease_owner, lease_token, lease_until, "
        "attempt_count, published_at, last_error_code) ON careerops.outbox_events "
        "TO careerops_outbox",
    ),
    "careerops_mailbox": (),
    "careerops_side_effect": (),
    "careerops_readonly": (
        "GRANT USAGE ON SCHEMA careerops TO careerops_readonly",
        "GRANT SELECT ON careerops.candidates, careerops.companies, "
        "careerops.job_sources, careerops.content_blobs, careerops.content_objects, "
        "careerops.canonical_jobs, "
        "careerops.job_postings, careerops.job_posting_versions, "
        "careerops.job_merge_decisions, careerops.job_posting_assignments, "
        "careerops.job_aliases, careerops.evidence_records, careerops.application_events, "
        "careerops.action_intents, careerops.action_payload_versions, "
        "careerops.policy_decisions, careerops.approval_requests, "
        "careerops.outbox_events, careerops.side_effect_attempts, "
        "careerops.provider_receipts, careerops.audit_events TO careerops_readonly",
        "GRANT SELECT (id, provider, account_subject, granted_scopes, status, issued_at, "
        "revoked_at, created_at, updated_at) ON careerops.oauth_credential_references "
        "TO careerops_readonly",
    ),
}


def _install_append_only_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.reject_append_only_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                RAISE EXCEPTION 'append-only table %.% rejects %',
                    TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
                    USING ERRCODE = '55000';
            END
            $function$
            """
        )
    )
    for table_name in APPEND_ONLY_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_append_only "
                f"BEFORE UPDATE OR DELETE ON careerops.{table_name} "
                "FOR EACH ROW EXECUTE FUNCTION careerops.reject_append_only_mutation()"
            )
        )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.reject_append_only_mutation() FROM PUBLIC")
    )


def _install_content_blob_guards() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.require_active_content_blob()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            DECLARE
                target_blob_id uuid;
            BEGIN
                IF TG_TABLE_NAME = 'content_objects' THEN
                    target_blob_id := NEW.blob_id;
                ELSIF TG_TABLE_NAME = 'evidence_records' THEN
                    IF NEW.content_object_id IS NULL THEN
                        RETURN NEW;
                    END IF;
                    SELECT blob_id INTO target_blob_id
                    FROM careerops.content_objects
                    WHERE id = NEW.content_object_id;
                ELSIF TG_TABLE_NAME = 'job_posting_versions' THEN
                    IF NEW.raw_snapshot_id IS NULL THEN
                        RETURN NEW;
                    END IF;
                    SELECT blob_id INTO target_blob_id
                    FROM careerops.content_objects
                    WHERE id = NEW.raw_snapshot_id;
                END IF;

                IF target_blob_id IS NULL THEN
                    RETURN NEW;
                END IF;
                PERFORM 1
                FROM careerops.content_blobs
                WHERE id = target_blob_id AND deletion_state = 'active'
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'content blob % is not active', target_blob_id
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
    )
    for table_name in ("content_objects", "evidence_records", "job_posting_versions"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_active_blob "
                f"BEFORE INSERT OR UPDATE ON careerops.{table_name} "
                "FOR EACH ROW EXECUTE FUNCTION careerops.require_active_content_blob()"
            )
        )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_content_blob_lifecycle()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.deletion_state = 'active'
                       AND NEW.delete_lease_owner IS NULL
                       AND NEW.delete_lease_token IS NULL
                       AND NEW.delete_lease_until IS NULL
                       AND NEW.deleted_at IS NULL
                       AND NEW.delete_result IS NULL THEN
                        RETURN NEW;
                    END IF;
                    IF NEW.deletion_state = 'deleting'
                       AND NEW.delete_lease_owner = 'write-orphan-reconciler'
                       AND NEW.delete_lease_token IS NOT NULL
                       AND NEW.delete_lease_until IS NOT NULL
                       AND NEW.deleted_at IS NULL
                       AND NEW.delete_result IS NULL THEN
                        RETURN NEW;
                    END IF;
                    RAISE EXCEPTION 'new content blob must start active or as a claimed orphan'
                        USING ERRCODE = '55000';
                END IF;

                IF OLD.deletion_state = 'active' AND NEW.deletion_state = 'deleting' THEN
                    IF NOT EXISTS (
                        SELECT 1 FROM careerops.content_objects
                        WHERE blob_id = OLD.id
                    ) THEN
                        RAISE EXCEPTION 'tracked content blob % has no logical references', OLD.id
                            USING ERRCODE = '55000';
                    END IF;
                    IF EXISTS (
                        SELECT 1 FROM careerops.content_objects
                        WHERE blob_id = OLD.id AND retired_at IS NULL
                    ) THEN
                        RAISE EXCEPTION 'content blob % still has unretired references', OLD.id
                            USING ERRCODE = '55000';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM careerops.content_objects AS object
                        JOIN careerops.evidence_records AS evidence
                          ON evidence.content_object_id = object.id
                        WHERE object.blob_id = OLD.id
                          AND (
                              ((evidence.source_url IS NULL OR btrim(evidence.source_url) = '')
                               AND (evidence.provider_id IS NULL
                                    OR btrim(evidence.provider_id) = ''))
                              OR btrim(evidence.sanitized_span) = ''
                              OR btrim(evidence.extractor_version) = ''
                              OR evidence.content_hash <> OLD.sha256
                          )
                    ) OR EXISTS (
                        SELECT 1
                        FROM careerops.content_objects AS object
                        JOIN careerops.job_posting_versions AS version
                          ON version.raw_snapshot_id = object.id
                        WHERE object.blob_id = OLD.id
                          AND (
                              btrim(version.source_url) = ''
                              OR btrim(version.parser_version) = ''
                              OR version.content_hash <> OLD.sha256
                          )
                    ) THEN
                        RAISE EXCEPTION 'content blob % lacks replay-safe evidence', OLD.id
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.deletion_state = 'deleting' AND NEW.deletion_state = 'deleting' THEN
                    IF OLD.delete_lease_until > CURRENT_TIMESTAMP
                       OR NEW.delete_lease_token IS NOT DISTINCT FROM OLD.delete_lease_token
                       OR NEW.delete_lease_until <= OLD.delete_lease_until THEN
                        RAISE EXCEPTION 'content deletion lease is not reclaimable'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                IF OLD.deletion_state = 'deleting' AND NEW.deletion_state = 'deleted' THEN
                    IF current_setting(
                        'careerops.content_delete_lease_token', true
                    ) IS DISTINCT FROM OLD.delete_lease_token::text THEN
                        RAISE EXCEPTION 'content deletion fencing token was not presented'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END IF;

                RAISE EXCEPTION 'invalid content blob lifecycle transition % -> %',
                    OLD.deletion_state, NEW.deletion_state
                    USING ERRCODE = '55000';
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_content_blobs_lifecycle "
            "BEFORE INSERT OR UPDATE ON careerops.content_blobs "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_content_blob_lifecycle()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.require_active_blob_reference()
            RETURNS trigger
            LANGUAGE plpgsql
            SECURITY DEFINER
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF NEW.deletion_state = 'active' AND NOT EXISTS (
                    SELECT 1 FROM careerops.content_objects WHERE blob_id = NEW.id
                ) THEN
                    RAISE EXCEPTION 'active content blob % requires a logical reference', NEW.id
                        USING ERRCODE = '55000';
                END IF;
                RETURN NULL;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE CONSTRAINT TRIGGER trg_content_blobs_require_reference "
            "AFTER INSERT OR UPDATE ON careerops.content_blobs "
            "DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION careerops.require_active_blob_reference()"
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_content_object_lifecycle()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, careerops
            AS $function$
            BEGIN
                IF OLD.expired_at IS NULL AND NEW.expired_at IS NOT NULL THEN
                    IF NEW.expired_at < OLD.retention_until THEN
                        RAISE EXCEPTION 'content object cannot expire before retention deadline'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF NEW.expired_at IS DISTINCT FROM OLD.expired_at THEN
                    RAISE EXCEPTION 'content expiry is monotonic'
                        USING ERRCODE = '55000';
                END IF;

                IF OLD.retired_at IS NULL AND NEW.retired_at IS NOT NULL THEN
                    IF NEW.expired_at IS NULL THEN
                        RAISE EXCEPTION 'content object must expire before retirement'
                            USING ERRCODE = '55000';
                    END IF;
                ELSIF NEW.retired_at IS DISTINCT FROM OLD.retired_at THEN
                    RAISE EXCEPTION 'content retirement is monotonic'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_content_objects_lifecycle "
            "BEFORE UPDATE OF expired_at, retired_at ON careerops.content_objects "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_content_object_lifecycle()"
        )
    )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.require_active_content_blob() FROM PUBLIC")
    )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.enforce_content_blob_lifecycle() FROM PUBLIC")
    )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.require_active_blob_reference() FROM PUBLIC")
    )
    op.execute(
        sa.text("REVOKE ALL ON FUNCTION careerops.enforce_content_object_lifecycle() FROM PUBLIC")
    )


def _install_outbox_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION careerops.enforce_outbox_lifecycle()
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

                RAISE EXCEPTION 'invalid outbox lifecycle transition % -> %',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END
            $function$
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_outbox_events_lifecycle "
            "BEFORE INSERT OR UPDATE ON careerops.outbox_events "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_outbox_lifecycle()"
        )
    )
    op.execute(sa.text("REVOKE ALL ON FUNCTION careerops.enforce_outbox_lifecycle() FROM PUBLIC"))


def _grant_capability_roles() -> None:
    if op.get_context().as_sql:
        for statements in ROLE_GRANTS.values():
            for statement in statements:
                op.execute(sa.text(statement))
        return

    existing_roles = set(op.get_bind().execute(sa.text("SELECT rolname FROM pg_roles")).scalars())
    for role_name, statements in ROLE_GRANTS.items():
        if role_name in existing_roles:
            for statement in statements:
                op.execute(sa.text(statement))


def upgrade() -> None:
    op.execute(sa.text("CREATE SCHEMA careerops"))
    op.execute(sa.text("REVOKE ALL ON SCHEMA careerops FROM PUBLIC"))
    op.execute(
        sa.text(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA careerops REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        )
    )
    op.create_table(
        "action_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="proposed", nullable=False),
        sa.Column("current_payload_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'denied', 'awaiting_approval', 'eligible', "
            "'processing', 'confirmed', 'failed', 'reconciliation_required')",
            name=op.f("ck_action_intents_status_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_intents")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_action_intents_idempotency_key")),
        schema="careerops",
    )
    op.create_table(
        "audit_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=24), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(event_hash) = 64", name=op.f("ck_audit_events_event_hash_length")
        ),
        sa.CheckConstraint(
            "previous_hash IS NULL OR char_length(previous_hash) = 64",
            name=op.f("ck_audit_events_previous_hash_length"),
        ),
        sa.PrimaryKeyConstraint("sequence", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("event_hash", name=op.f("uq_audit_events_event_hash")),
        sa.UniqueConstraint("event_id", name=op.f("uq_audit_events_event_id")),
        schema="careerops",
    )
    op.create_table(
        "candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        schema="careerops",
    )
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column(
            "official_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("terms_status", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "terms_status IN ('unknown', 'allowed', 'blocked')",
            name=op.f("ck_companies_terms_status_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("normalized_name", name=op.f("uq_companies_normalized_name")),
        schema="careerops",
    )
    op.create_table(
        "content_blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("deletion_state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("delete_lease_owner", sa.Text(), nullable=True),
        sa.Column("delete_lease_token", sa.Uuid(), nullable=True),
        sa.Column("delete_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_result", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_content_blobs_sha256_format"),
        ),
        sa.CheckConstraint(
            "object_key ~ '^sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$' "
            "AND right(object_key, 64) = sha256",
            name=op.f("ck_content_blobs_object_key_matches_sha256"),
        ),
        sa.CheckConstraint("byte_size >= 0", name=op.f("ck_content_blobs_byte_size_nonnegative")),
        sa.CheckConstraint(
            "deletion_state IN ('active', 'deleting', 'deleted')",
            name=op.f("ck_content_blobs_deletion_state_values"),
        ),
        sa.CheckConstraint(
            "delete_result IS NULL OR delete_result IN ('deleted', 'already_missing')",
            name=op.f("ck_content_blobs_delete_result_values"),
        ),
        sa.CheckConstraint(
            "(deletion_state = 'active' AND delete_lease_owner IS NULL "
            "AND delete_lease_token IS NULL AND delete_lease_until IS NULL "
            "AND deleted_at IS NULL AND delete_result IS NULL) OR "
            "(deletion_state = 'deleting' AND delete_lease_owner IS NOT NULL "
            "AND delete_lease_token IS NOT NULL AND delete_lease_until IS NOT NULL "
            "AND deleted_at IS NULL AND delete_result IS NULL) OR "
            "(deletion_state = 'deleted' AND delete_lease_owner IS NULL "
            "AND delete_lease_token IS NULL AND delete_lease_until IS NULL "
            "AND deleted_at IS NOT NULL AND delete_result IS NOT NULL)",
            name=op.f("ck_content_blobs_deletion_state_consistent"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_blobs")),
        sa.UniqueConstraint("object_key", name=op.f("uq_content_blobs_object_key")),
        sa.UniqueConstraint("sha256", name=op.f("uq_content_blobs_sha256")),
        schema="careerops",
    )
    op.create_index(
        "ix_content_blobs_deletion_claim",
        "content_blobs",
        ["deletion_state", "delete_lease_until"],
        unique=False,
        schema="careerops",
    )
    op.create_table(
        "content_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("blob_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("owner_resource_type", sa.String(length=32), nullable=False),
        sa.Column("owner_resource_id", sa.Uuid(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification IN ('raw_webpage', 'raw_headers', 'recruiting_email', "
            "'accepted_attachment', 'quarantined_attachment', 'decision_evidence')",
            name=op.f("ck_content_objects_classification_values"),
        ),
        sa.CheckConstraint(
            "btrim(media_type) <> '' AND btrim(owner_resource_type) <> ''",
            name=op.f("ck_content_objects_metadata_nonempty"),
        ),
        sa.CheckConstraint(
            "classification <> 'quarantined_attachment' OR "
            "retention_until <= created_at + interval '24 hours'",
            name=op.f("ck_content_objects_quarantine_retention_limit"),
        ),
        sa.ForeignKeyConstraint(
            ["blob_id"],
            ["careerops.content_blobs.id"],
            name=op.f("fk_content_objects_blob_id_content_blobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_objects")),
        schema="careerops",
    )
    op.create_index(
        "ix_content_objects_retention_pending_delete",
        "content_objects",
        ["retention_until"],
        unique=False,
        schema="careerops",
        postgresql_where=sa.text("expired_at IS NULL AND retired_at IS NULL"),
    )
    op.create_index(
        "ix_content_objects_blob_id",
        "content_objects",
        ["blob_id"],
        unique=False,
        schema="careerops",
    )
    op.create_table(
        "oauth_credential_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("account_subject", sa.Text(), nullable=False),
        sa.Column("secret_handle", sa.Text(), nullable=False),
        sa.Column(
            "granted_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'invalid')",
            name=op.f("ck_oauth_credential_references_status_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_credential_references")),
        sa.UniqueConstraint(
            "provider", "account_subject", name="uq_oauth_credential_references_provider_subject"
        ),
        sa.UniqueConstraint(
            "secret_handle", name=op.f("uq_oauth_credential_references_secret_handle")
        ),
        schema="careerops",
    )
    op.create_table(
        "action_payload_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "attachment_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(payload_hash) = 64",
            name=op.f("ck_action_payload_versions_payload_hash_length"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_action_payload_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_action_payload_versions_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_payload_versions")),
        sa.UniqueConstraint(
            "action_intent_id",
            "id",
            "payload_hash",
            name="uq_action_payload_versions_intent_id_hash",
        ),
        sa.UniqueConstraint("action_intent_id", "id", name="uq_action_payload_versions_intent_id"),
        sa.UniqueConstraint(
            "action_intent_id", "payload_hash", name="uq_action_payload_versions_intent_hash"
        ),
        sa.UniqueConstraint(
            "action_intent_id", "version", name="uq_action_payload_versions_intent_version"
        ),
        schema="careerops",
    )
    op.create_table(
        "canonical_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("aggregate_state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("primary_posting_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "aggregate_state IN ('active', 'closed', 'archived')",
            name=op.f("ck_canonical_jobs_aggregate_state_values"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["careerops.companies.id"],
            name=op.f("fk_canonical_jobs_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_canonical_jobs")),
        schema="careerops",
    )
    op.create_table(
        "evidence_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("content_object_id", sa.Uuid(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("provider_id", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("sanitized_span", sa.Text(), nullable=False),
        sa.Column("span_hash", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name=op.f("ck_evidence_records_content_hash_length")
        ),
        sa.CheckConstraint(
            "char_length(span_hash) = 64", name=op.f("ck_evidence_records_span_hash_length")
        ),
        sa.CheckConstraint(
            "source_url IS NOT NULL OR provider_id IS NOT NULL",
            name=op.f("ck_evidence_records_source_reference_present"),
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["careerops.content_objects.id"],
            name=op.f("fk_evidence_records_content_object_id_content_objects"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_records")),
        schema="careerops",
    )
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_identifier", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending_review", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending_review', 'active', 'paused', 'blocked')",
            name=op.f("ck_job_sources_state_values"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["careerops.companies.id"],
            name=op.f("fk_job_sources_company_id_companies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_sources")),
        sa.UniqueConstraint(
            "company_id",
            "source_type",
            "source_identifier",
            name="uq_job_sources_company_type_identifier",
        ),
        schema="careerops",
    )
    op.create_table(
        "job_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=False),
        sa.Column("alias_type", sa.String(length=16), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "alias_type IN ('title', 'location', 'url')",
            name=op.f("ck_job_aliases_alias_type_values"),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_job_id"],
            ["careerops.canonical_jobs.id"],
            name=op.f("fk_job_aliases_canonical_job_id_canonical_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_aliases")),
        sa.UniqueConstraint(
            "canonical_job_id",
            "alias_type",
            "normalized_value",
            name="uq_job_aliases_job_type_value",
        ),
        schema="careerops",
    )
    op.create_table(
        "job_postings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("source_state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_state IN ('active', 'closed', 'expired', 'unknown')",
            name=op.f("ck_job_postings_source_state_values"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["careerops.job_sources.id"],
            name=op.f("fk_job_postings_source_id_job_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_postings")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_job_postings_source_external_id"),
        schema="careerops",
    )
    op.create_index(
        "ix_job_postings_source_state_last_seen",
        "job_postings",
        ["source_state", "last_seen_at"],
        unique=False,
        schema="careerops",
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'published', 'failed')",
            name=op.f("ck_outbox_events_status_values"),
        ),
        sa.CheckConstraint(
            "event_type IN ('workflow_signal', 'internal_notification')",
            name=op.f("ck_outbox_events_event_type_values"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_outbox_events_attempt_count_nonnegative")
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_until IS NOT NULL) OR "
            "(status <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL "
            "AND lease_until IS NULL)",
            name=op.f("ck_outbox_events_lease_state_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR "
            "(status <> 'published' AND published_at IS NULL)",
            name=op.f("ck_outbox_events_published_state_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
            ],
            name="fk_outbox_events_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_outbox_events_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("action_intent_id", "id", name="uq_outbox_events_intent_id"),
        sa.UniqueConstraint("event_key", name=op.f("uq_outbox_events_event_key")),
        schema="careerops",
    )
    op.create_index(
        "ix_outbox_events_status_available_at",
        "outbox_events",
        ["status", "available_at"],
        unique=False,
        schema="careerops",
    )
    op.create_table(
        "policy_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_version", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('deny', 'require_approval', 'allow')",
            name=op.f("ck_policy_decisions_decision_values"),
        ),
        sa.CheckConstraint(
            "char_length(payload_hash) = 64", name=op.f("ck_policy_decisions_payload_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_policy_decisions_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_policy_decisions_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_decisions")),
        sa.UniqueConstraint(
            "action_intent_id",
            "payload_version_id",
            "id",
            name="uq_policy_decisions_intent_payload_id",
        ),
        schema="careerops",
    )
    op.create_table(
        "application_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("job_posting_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["careerops.candidates.id"],
            name=op.f("fk_application_events_candidate_id_candidates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_job_id"],
            ["careerops.canonical_jobs.id"],
            name=op.f("fk_application_events_canonical_job_id_canonical_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["careerops.job_postings.id"],
            name=op.f("fk_application_events_job_posting_id_job_postings"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_events")),
        schema="careerops",
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("payload_version_id", sa.Uuid(), nullable=False),
        sa.Column("policy_decision_id", sa.Uuid(), nullable=False),
        sa.Column("requested_for", sa.Text(), nullable=False),
        sa.Column("decision", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("decision_rule_reference", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(decision = 'pending' AND decided_at IS NULL) OR "
            "(decision <> 'pending' AND decided_at IS NOT NULL)",
            name=op.f("ck_approval_requests_decision_timestamp_consistent"),
        ),
        sa.CheckConstraint(
            "decision IN ('pending', 'approved', 'rejected', 'expired')",
            name=op.f("ck_approval_requests_decision_values"),
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "policy_decision_id"],
            [
                "careerops.policy_decisions.action_intent_id",
                "careerops.policy_decisions.payload_version_id",
                "careerops.policy_decisions.id",
            ],
            name="fk_approval_requests_policy_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
            ],
            name="fk_approval_requests_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_approval_requests_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_requests")),
        schema="careerops",
    )
    op.create_table(
        "job_merge_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("from_canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("to_canonical_job_id", sa.Uuid(), nullable=True),
        sa.Column("decision_kind", sa.String(length=16), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(precision=6, scale=5), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("algorithm_version", sa.Text(), nullable=True),
        sa.Column("supersedes_decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type IN ('rule', 'human', 'migration')",
            name=op.f("ck_job_merge_decisions_actor_type_values"),
        ),
        sa.CheckConstraint(
            "decision_kind IN ('merge', 'split', 'rollback')",
            name=op.f("ck_job_merge_decisions_decision_kind_values"),
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name=op.f("ck_job_merge_decisions_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["from_canonical_job_id"],
            ["careerops.canonical_jobs.id"],
            name=op.f("fk_job_merge_decisions_from_canonical_job_id_canonical_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["careerops.job_postings.id"],
            name=op.f("fk_job_merge_decisions_job_posting_id_job_postings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["careerops.job_merge_decisions.id"],
            name=op.f("fk_job_merge_decisions_supersedes_decision_id_job_merge_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_canonical_job_id"],
            ["careerops.canonical_jobs.id"],
            name=op.f("fk_job_merge_decisions_to_canonical_job_id_canonical_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_merge_decisions")),
        sa.UniqueConstraint(
            "job_posting_id",
            "to_canonical_job_id",
            "id",
            name="uq_job_merge_decisions_posting_target_id",
        ),
        schema="careerops",
    )
    op.create_table(
        "job_posting_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("raw_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column(
            "structured_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "changed_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64",
            name=op.f("ck_job_posting_versions_content_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["careerops.job_postings.id"],
            name=op.f("fk_job_posting_versions_job_posting_id_job_postings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["raw_snapshot_id"],
            ["careerops.content_objects.id"],
            name=op.f("fk_job_posting_versions_raw_snapshot_id_content_objects"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_posting_versions")),
        sa.UniqueConstraint(
            "job_posting_id", "content_hash", name="uq_job_posting_versions_posting_hash"
        ),
        schema="careerops",
    )
    op.create_table(
        "side_effect_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_intent_id", sa.Uuid(), nullable=False),
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "response_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_side_effect_attempts_fingerprint_length"),
        ),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_side_effect_attempts_ordinal_positive")),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "outbox_event_id"],
            ["careerops.outbox_events.action_intent_id", "careerops.outbox_events.id"],
            name="fk_side_effect_attempts_outbox_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id"],
            ["careerops.action_intents.id"],
            name=op.f("fk_side_effect_attempts_action_intent_id_action_intents"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_side_effect_attempts")),
        sa.UniqueConstraint(
            "action_intent_id", "ordinal", name="uq_side_effect_attempts_intent_ordinal"
        ),
        schema="careerops",
    )
    op.create_table(
        "job_posting_assignments",
        sa.Column("job_posting_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_job_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canonical_job_id"],
            ["careerops.canonical_jobs.id"],
            name=op.f("fk_job_posting_assignments_canonical_job_id_canonical_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id", "canonical_job_id", "decision_id"],
            [
                "careerops.job_merge_decisions.job_posting_id",
                "careerops.job_merge_decisions.to_canonical_job_id",
                "careerops.job_merge_decisions.id",
            ],
            name="fk_job_posting_assignments_decision_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_posting_id"],
            ["careerops.job_postings.id"],
            name=op.f("fk_job_posting_assignments_job_posting_id_job_postings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_posting_id", name=op.f("pk_job_posting_assignments")),
        sa.UniqueConstraint(
            "canonical_job_id", "job_posting_id", name="uq_job_posting_assignments_job_posting"
        ),
        sa.UniqueConstraint("decision_id", name=op.f("uq_job_posting_assignments_decision_id")),
        schema="careerops",
    )
    op.create_table(
        "provider_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("side_effect_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("provider_resource_id", sa.Text(), nullable=False),
        sa.Column("reconciliation_key", sa.Text(), nullable=False),
        sa.Column("final_state", sa.String(length=24), nullable=False),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "receipt_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["side_effect_attempt_id"],
            ["careerops.side_effect_attempts.id"],
            name=op.f("fk_provider_receipts_side_effect_attempt_id_side_effect_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_receipts")),
        sa.UniqueConstraint(
            "provider",
            "reconciliation_key",
            name="uq_provider_receipts_provider_reconciliation_key",
        ),
        schema="careerops",
    )
    op.create_foreign_key(
        "fk_action_intents_current_payload_identity",
        "action_intents",
        "action_payload_versions",
        ["id", "current_payload_version_id"],
        ["action_intent_id", "id"],
        source_schema="careerops",
        referent_schema="careerops",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_canonical_jobs_primary_assignment",
        "canonical_jobs",
        "job_posting_assignments",
        ["id", "primary_posting_id"],
        ["canonical_job_id", "job_posting_id"],
        source_schema="careerops",
        referent_schema="careerops",
        ondelete="RESTRICT",
    )
    _install_content_blob_guards()
    _install_outbox_guard()
    _install_append_only_guards()
    _grant_capability_roles()


def downgrade() -> None:
    op.drop_constraint(
        "fk_canonical_jobs_primary_assignment",
        "canonical_jobs",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_action_intents_current_payload_identity",
        "action_intents",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_table("provider_receipts", schema="careerops")
    op.drop_table("job_posting_assignments", schema="careerops")
    op.drop_table("side_effect_attempts", schema="careerops")
    op.drop_table("job_posting_versions", schema="careerops")
    op.drop_table("job_merge_decisions", schema="careerops")
    op.drop_table("approval_requests", schema="careerops")
    op.drop_table("application_events", schema="careerops")
    op.drop_table("policy_decisions", schema="careerops")
    op.drop_index(
        "ix_outbox_events_status_available_at", table_name="outbox_events", schema="careerops"
    )
    op.drop_table("outbox_events", schema="careerops")
    op.drop_index(
        "ix_job_postings_source_state_last_seen", table_name="job_postings", schema="careerops"
    )
    op.drop_table("job_postings", schema="careerops")
    op.drop_table("job_aliases", schema="careerops")
    op.drop_table("job_sources", schema="careerops")
    op.drop_table("evidence_records", schema="careerops")
    op.drop_table("canonical_jobs", schema="careerops")
    op.drop_table("action_payload_versions", schema="careerops")
    op.drop_table("oauth_credential_references", schema="careerops")
    op.drop_table("content_objects", schema="careerops")
    op.drop_index("ix_content_blobs_deletion_claim", table_name="content_blobs", schema="careerops")
    op.drop_table("content_blobs", schema="careerops")
    op.drop_table("companies", schema="careerops")
    op.drop_table("candidates", schema="careerops")
    op.drop_table("audit_events", schema="careerops")
    op.drop_table("action_intents", schema="careerops")
    op.execute(sa.text("DROP FUNCTION careerops.reject_append_only_mutation()"))
    op.execute(sa.text("DROP FUNCTION careerops.enforce_outbox_lifecycle()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops.enforce_content_object_lifecycle()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS careerops.require_active_blob_reference()"))
    op.execute(sa.text("DROP FUNCTION careerops.enforce_content_blob_lifecycle()"))
    op.execute(sa.text("DROP FUNCTION careerops.require_active_content_blob()"))
    op.execute(sa.text("DROP SCHEMA careerops"))
