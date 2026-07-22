from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

DATABASE_SCHEMA = "careerops"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = sa.MetaData(schema=DATABASE_SCHEMA, naming_convention=NAMING_CONVENTION)

candidates = sa.Table(
    "candidates",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("owner_user_id", "id", name="uq_candidates_owner_id"),
)

companies = sa.Table(
    "companies",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("normalized_name", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "official_domains",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("terms_status", sa.String(16), server_default="unknown", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "terms_status IN ('unknown', 'allowed', 'blocked')",
        name="terms_status_values",
    ),
)

job_sources = sa.Table(
    "job_sources",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "company_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.companies.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_type", sa.String(32), nullable=False),
    sa.Column("source_identifier", sa.Text(), nullable=False),
    sa.Column("base_url", sa.Text(), nullable=False),
    sa.Column("state", sa.String(24), server_default="pending_review", nullable=False),
    sa.Column("verified_at", sa.DateTime(timezone=True)),
    sa.Column("last_discovery_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "company_id",
        "source_type",
        "source_identifier",
        name="uq_job_sources_company_type_identifier",
    ),
    sa.CheckConstraint(
        "state IN ('pending_review', 'active', 'paused', 'blocked')",
        name="state_values",
    ),
)

content_blobs = sa.Table(
    "content_blobs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("sha256", sa.String(64), nullable=False, unique=True),
    sa.Column("object_key", sa.Text(), nullable=False, unique=True),
    sa.Column("byte_size", sa.BigInteger(), nullable=False),
    sa.Column("deletion_state", sa.String(16), server_default="active", nullable=False),
    sa.Column("delete_lease_owner", sa.Text()),
    sa.Column("delete_lease_token", sa.Uuid()),
    sa.Column("delete_lease_until", sa.DateTime(timezone=True)),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    sa.Column("delete_result", sa.String(24)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
    sa.CheckConstraint(
        "object_key ~ '^sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$' "
        "AND right(object_key, 64) = sha256",
        name="object_key_matches_sha256",
    ),
    sa.CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
    sa.CheckConstraint(
        "deletion_state IN ('active', 'deleting', 'deleted')",
        name="deletion_state_values",
    ),
    sa.CheckConstraint(
        "delete_result IS NULL OR delete_result IN ('deleted', 'already_missing')",
        name="delete_result_values",
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
        name="deletion_state_consistent",
    ),
)

sa.Index(
    "ix_content_blobs_deletion_claim",
    content_blobs.c.deletion_state,
    content_blobs.c.delete_lease_until,
)

content_objects = sa.Table(
    "content_objects",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "blob_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.content_blobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("media_type", sa.Text(), nullable=False),
    sa.Column("classification", sa.String(32), nullable=False),
    sa.Column("owner_resource_type", sa.String(32), nullable=False),
    sa.Column("owner_resource_id", sa.Uuid(), nullable=False),
    sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expired_at", sa.DateTime(timezone=True)),
    sa.Column("retired_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "classification IN ('raw_webpage', 'raw_headers', 'recruiting_email', "
        "'accepted_attachment', 'quarantined_attachment', 'decision_evidence')",
        name="classification_values",
    ),
    sa.CheckConstraint(
        "btrim(media_type) <> '' AND btrim(owner_resource_type) <> ''",
        name="metadata_nonempty",
    ),
    sa.CheckConstraint(
        "classification <> 'quarantined_attachment' OR "
        "retention_until <= created_at + interval '24 hours'",
        name="quarantine_retention_limit",
    ),
)

sa.Index(
    "ix_content_objects_retention_pending_delete",
    content_objects.c.retention_until,
    postgresql_where=sa.and_(
        content_objects.c.expired_at.is_(None),
        content_objects.c.retired_at.is_(None),
    ),
)

sa.Index("ix_content_objects_blob_id", content_objects.c.blob_id)

canonical_jobs = sa.Table(
    "canonical_jobs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "company_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.companies.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("canonical_title", sa.Text(), nullable=False),
    sa.Column("normalized_title", sa.Text(), nullable=False),
    sa.Column("aggregate_state", sa.String(16), server_default="active", nullable=False),
    sa.Column("primary_posting_id", sa.Uuid()),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "aggregate_state IN ('active', 'closed', 'archived')",
        name="aggregate_state_values",
    ),
    sa.ForeignKeyConstraint(
        ["id", "primary_posting_id"],
        [
            f"{DATABASE_SCHEMA}.job_posting_assignments.canonical_job_id",
            f"{DATABASE_SCHEMA}.job_posting_assignments.job_posting_id",
        ],
        name="fk_canonical_jobs_primary_assignment",
        use_alter=True,
        ondelete="RESTRICT",
    ),
)

job_postings = sa.Table(
    "job_postings",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_sources.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("external_id", sa.Text(), nullable=False),
    sa.Column("canonical_url", sa.Text(), nullable=False),
    sa.Column("source_state", sa.String(16), server_default="active", nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("closed_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("source_id", "external_id", name="uq_job_postings_source_external_id"),
    sa.CheckConstraint(
        "source_state IN ('active', 'closed', 'expired', 'unknown')",
        name="source_state_values",
    ),
)

sa.Index(
    "ix_job_postings_source_state_last_seen",
    job_postings.c.source_state,
    job_postings.c.last_seen_at,
)

job_posting_versions = sa.Table(
    "job_posting_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "raw_snapshot_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.content_objects.id", ondelete="SET NULL"),
    ),
    sa.Column("content_hash", sa.String(64), nullable=False),
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
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "job_posting_id",
        "content_hash",
        name="uq_job_posting_versions_posting_hash",
    ),
    sa.CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
)

job_merge_decisions = sa.Table(
    "job_merge_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "from_canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "to_canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
    ),
    sa.Column("decision_kind", sa.String(16), nullable=False),
    sa.Column("rule", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("score", sa.Numeric(6, 5)),
    sa.Column("actor_type", sa.String(16), nullable=False),
    sa.Column("actor_id", sa.Text()),
    sa.Column("algorithm_version", sa.Text()),
    sa.Column(
        "supersedes_decision_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_merge_decisions.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "decision_kind IN ('merge', 'split', 'rollback')",
        name="decision_kind_values",
    ),
    sa.CheckConstraint(
        "actor_type IN ('rule', 'human', 'migration')",
        name="actor_type_values",
    ),
    sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score_range"),
    sa.UniqueConstraint(
        "job_posting_id",
        "to_canonical_job_id",
        "id",
        name="uq_job_merge_decisions_posting_target_id",
    ),
)

job_posting_assignments = sa.Table(
    "job_posting_assignments",
    metadata,
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "decision_id",
        sa.Uuid(),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["job_posting_id", "canonical_job_id", "decision_id"],
        [
            f"{DATABASE_SCHEMA}.job_merge_decisions.job_posting_id",
            f"{DATABASE_SCHEMA}.job_merge_decisions.to_canonical_job_id",
            f"{DATABASE_SCHEMA}.job_merge_decisions.id",
        ],
        name="fk_job_posting_assignments_decision_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "canonical_job_id",
        "job_posting_id",
        name="uq_job_posting_assignments_job_posting",
    ),
)

job_aliases = sa.Table(
    "job_aliases",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("alias_type", sa.String(16), nullable=False),
    sa.Column("normalized_value", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "canonical_job_id",
        "alias_type",
        "normalized_value",
        name="uq_job_aliases_job_type_value",
    ),
    sa.CheckConstraint(
        "alias_type IN ('title', 'location', 'url')",
        name="alias_type_values",
    ),
)

evidence_records = sa.Table(
    "evidence_records",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("resource_type", sa.String(32), nullable=False),
    sa.Column("resource_id", sa.Uuid(), nullable=False),
    sa.Column(
        "content_object_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.content_objects.id", ondelete="SET NULL"),
    ),
    sa.Column("source_url", sa.Text()),
    sa.Column("provider_id", sa.Text()),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("sanitized_span", sa.Text(), nullable=False),
    sa.Column("span_hash", sa.String(64), nullable=False),
    sa.Column("extractor_version", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "source_url IS NOT NULL OR provider_id IS NOT NULL",
        name="source_reference_present",
    ),
    sa.CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
    sa.CheckConstraint("char_length(span_hash) = 64", name="span_hash_length"),
)

application_events = sa.Table(
    "application_events",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
    ),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("source_type", sa.String(24), nullable=False),
    sa.Column("source_id", sa.Text()),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "event_data",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
)

oauth_credential_references = sa.Table(
    "oauth_credential_references",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("provider", sa.String(24), nullable=False),
    sa.Column("account_subject", sa.Text(), nullable=False),
    sa.Column("secret_handle", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "granted_scopes",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("status", sa.String(16), server_default="active", nullable=False),
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "provider",
        "account_subject",
        name="uq_oauth_credential_references_provider_subject",
    ),
    sa.CheckConstraint("status IN ('active', 'revoked', 'invalid')", name="status_values"),
)

gmail_accounts = sa.Table(
    "gmail_accounts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "oauth_credential_reference_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.oauth_credential_references.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column("provider", sa.String(16), server_default="gmail", nullable=False),
    sa.Column("account_subject", sa.Text(), nullable=False),
    sa.Column("sync_mode", sa.String(16), server_default="polling", nullable=False),
    sa.Column("dedicated_account_attested", sa.Boolean(), nullable=False),
    sa.Column("byo_account_attested", sa.Boolean(), nullable=False),
    sa.Column("publishing_status", sa.String(16), nullable=False),
    sa.Column("publishing_evidence_sha256", sa.String(64)),
    sa.Column("credential_store_evidence_sha256", sa.String(64), nullable=False),
    sa.Column(
        "in_production_attested", sa.Boolean(), server_default=sa.text("false"), nullable=False
    ),
    sa.Column("last_history_id", sa.Text()),
    sa.Column("next_page_token", sa.Text()),
    sa.Column("anchor_received_at", sa.DateTime(timezone=True)),
    sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    sa.Column("last_full_sync_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.String(24), server_default="active", nullable=False),
    sa.Column("version", sa.BigInteger(), server_default="1", nullable=False),
    sa.Column("fencing_token", sa.Uuid(), nullable=False),
    sa.Column("last_error_code", sa.Text()),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["owner_user_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.owner_user_id", f"{DATABASE_SCHEMA}.candidates.id"],
        name="fk_gmail_accounts_owner_candidate",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "provider",
        "account_subject",
        name="uq_gmail_accounts_owner_provider_subject",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "idempotency_key",
        name="uq_gmail_accounts_owner_idempotency_key",
    ),
    sa.CheckConstraint("provider = 'gmail'", name="provider_values"),
    sa.CheckConstraint("sync_mode = 'polling'", name="sync_mode_readonly_polling_only"),
    sa.CheckConstraint("dedicated_account_attested IS TRUE", name="dedicated_account_required"),
    sa.CheckConstraint("byo_account_attested IS TRUE", name="byo_account_required"),
    sa.CheckConstraint(
        "publishing_status IN ('testing', 'in_production')", name="publishing_status_values"
    ),
    sa.CheckConstraint(
        "publishing_evidence_sha256 IS NULL OR publishing_evidence_sha256 ~ '^[a-f0-9]{64}$'",
        name="publishing_evidence_sha256_format",
    ),
    sa.CheckConstraint(
        "status IN ('active', 'paused', 'sync_required', 'revoked', 'blocked')",
        name="status_values",
    ),
    sa.CheckConstraint(
        "credential_store_evidence_sha256 ~ '^[a-f0-9]{64}$'",
        name="credential_store_evidence_sha256_format",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
)

gmail_sync_runs = sa.Table(
    "gmail_sync_runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "gmail_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), server_default="queued", nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("fencing_token", sa.Uuid(), nullable=False),
    sa.Column("lease_owner", sa.Text()),
    sa.Column("lease_token", sa.Uuid()),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("failed_at", sa.DateTime(timezone=True)),
    sa.Column("history_start_id", sa.Text()),
    sa.Column("history_end_id", sa.Text()),
    sa.Column("next_page_token", sa.Text()),
    sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
    sa.Column("last_error_code", sa.Text()),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "gmail_account_id",
        "idempotency_key",
        name="uq_gmail_sync_runs_account_idempotency_key",
    ),
    sa.CheckConstraint(
        "reason IN ('manual', 'history_expired', 'scheduled', 'recovery')", name="reason_values"
    ),
    sa.CheckConstraint(
        "status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')", name="status_values"
    ),
    sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    sa.CheckConstraint("message_count >= 0", name="message_count_nonnegative"),
)

gmail_message_signals = sa.Table(
    "gmail_message_signals",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "gmail_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "gmail_sync_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_sync_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider_message_id", sa.Text(), nullable=False),
    sa.Column("provider_thread_id", sa.Text(), nullable=False),
    sa.Column("provider_history_id", sa.Text(), nullable=False),
    sa.Column("message_sha256", sa.String(64), nullable=False),
    sa.Column("thread_sha256", sa.String(64), nullable=False),
    sa.Column("sender_sha256", sa.String(64)),
    sa.Column("subject_sha256", sa.String(64), nullable=False),
    sa.Column("snippet_sha256", sa.String(64), nullable=False),
    sa.Column("redacted_excerpt", sa.Text(), nullable=False),
    sa.Column("label_ids_hash", sa.String(64), nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True)),
    sa.Column("classification", sa.String(32), nullable=False),
    sa.Column("relevance", sa.String(16), nullable=False),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
    sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("signal_sha256", sa.String(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "gmail_account_id",
        "provider_message_id",
        name="uq_gmail_message_signals_account_message",
    ),
    sa.UniqueConstraint(
        "gmail_account_id",
        "signal_sha256",
        name="uq_gmail_message_signals_account_signal_sha256",
    ),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.CheckConstraint(
        "classification IN ("
        "'application_acknowledgement', 'assessment', 'document_request', "
        "'interview_invitation', 'offer', 'rejection', 'recruiter_question', "
        "'suspicious', 'unknown', 'unrelated')",
        name="classification_values",
    ),
    sa.CheckConstraint(
        "relevance IN ('relevant', 'uncertain', 'non_relevant')",
        name="relevance_values",
    ),
    sa.CheckConstraint("message_sha256 ~ '^[a-f0-9]{64}$'", name="message_sha256_format"),
    sa.CheckConstraint("thread_sha256 ~ '^[a-f0-9]{64}$'", name="thread_sha256_format"),
    sa.CheckConstraint(
        "sender_sha256 IS NULL OR sender_sha256 ~ '^[a-f0-9]{64}$'", name="sender_sha256_format"
    ),
    sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
    sa.CheckConstraint("snippet_sha256 ~ '^[a-f0-9]{64}$'", name="snippet_sha256_format"),
    sa.CheckConstraint("label_ids_hash ~ '^[a-f0-9]{64}$'", name="label_ids_hash_format"),
    sa.CheckConstraint("signal_sha256 ~ '^[a-f0-9]{64}$'", name="signal_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(provenance_json) = 'object'", name="provenance_json_object"),
)

gmail_signal_proposals = sa.Table(
    "gmail_signal_proposals",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "gmail_message_signal_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_message_signals.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "gmail_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("proposal_kind", sa.Text(), nullable=False),
    sa.Column("review_priority", sa.Text(), nullable=False),
    sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("status", sa.Text(), server_default="pending_review", nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "owner_user_id", "idempotency_key", name="uq_gmail_signal_proposals_owner_idempotency"
    ),
    sa.CheckConstraint(
        "proposal_kind IN ("
        "'application_status_update', 'follow_up_draft', "
        "'review_only', 'reconciliation_update')",
        name="proposal_kind_values",
    ),
    sa.CheckConstraint("review_priority IN ('normal', 'high')", name="review_priority_values"),
    sa.CheckConstraint("status = 'pending_review'", name="proposal_rows_are_immutable_pending"),
    sa.CheckConstraint("jsonb_typeof(payload_json) = 'object'", name="payload_json_object"),
    sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
)

gmail_signal_review_decisions = sa.Table(
    "gmail_signal_review_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "proposal_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_signal_proposals.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("decision", sa.Text(), nullable=False),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
    ),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "idempotency_key",
        name="uq_gmail_signal_review_decisions_owner_idempotency",
    ),
    sa.CheckConstraint("decision IN ('approve', 'reject')", name="decision_values"),
    sa.CheckConstraint(
        "btrim(reason) <> '' AND char_length(reason) <= 4000", name="reason_bounded"
    ),
    sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
)

gmail_command_receipts = sa.Table(
    "gmail_command_receipts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "gmail_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_accounts.id", ondelete="RESTRICT"),
    ),
    sa.Column("command_kind", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "gmail_account_id",
        "command_kind",
        "idempotency_key",
        name="uq_gmail_command_receipts_identity",
    ),
    sa.CheckConstraint(
        "command_kind IN ("
        "'register_account', 'request_sync', 'review_proposal', "
        "'reset_history', 'revoke_account')",
        name="command_kind_values",
    ),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
)

sa.Index(
    "ix_gmail_accounts_owner_updated_at",
    gmail_accounts.c.owner_user_id,
    gmail_accounts.c.updated_at,
)
sa.Index(
    "ix_gmail_sync_runs_status_available",
    gmail_sync_runs.c.status,
    gmail_sync_runs.c.created_at,
)
sa.Index(
    "ix_gmail_message_signals_account_history",
    gmail_message_signals.c.gmail_account_id,
    gmail_message_signals.c.provider_history_id,
)

gmail_send_accounts = sa.Table(
    "gmail_send_accounts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "oauth_credential_reference_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.oauth_credential_references.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "reconciliation_gmail_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_accounts.id", ondelete="RESTRICT"),
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
    sa.ForeignKeyConstraint(
        ["owner_user_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.owner_user_id", f"{DATABASE_SCHEMA}.candidates.id"],
        name="fk_gmail_send_accounts_owner_candidate",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "provider",
        "account_subject",
        name="uq_gmail_send_accounts_owner_provider_subject",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "idempotency_key",
        name="uq_gmail_send_accounts_owner_idempotency_key",
    ),
    sa.CheckConstraint("provider = 'gmail_send'", name="provider_values"),
    sa.CheckConstraint(
        "status IN ('disabled', 'active', 'paused', 'revoked', 'blocked')",
        name="status_values",
    ),
    sa.CheckConstraint("daily_send_limit BETWEEN 1 AND 500", name="daily_send_limit_bounds"),
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
)

gmail_send_drafts = sa.Table(
    "gmail_send_drafts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "gmail_send_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_send_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "reviewed_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
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
    sa.Column(
        "reviewed_at",
        sa.DateTime(timezone=True),
    ),
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
        "recipient_snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="recipient_snapshot_sha256_format"
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
)

gmail_send_reservations = sa.Table(
    "gmail_send_reservations",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "gmail_send_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_send_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "reviewed_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
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
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
        "recipient_snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="recipient_snapshot_sha256_format"
    ),
    sa.CheckConstraint("subject_sha256 ~ '^[a-f0-9]{64}$'", name="subject_sha256_format"),
    sa.CheckConstraint("body_sha256 ~ '^[a-f0-9]{64}$'", name="body_sha256_format"),
    sa.CheckConstraint("payload_sha256 ~ '^[a-f0-9]{64}$'", name="payload_sha256_format"),
    sa.CheckConstraint(
        "approval_snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="approval_snapshot_sha256_format"
    ),
    sa.CheckConstraint(
        "attachment_manifest_sha256 ~ '^[a-f0-9]{64}$'",
        name="attachment_manifest_sha256_format",
    ),
    sa.CheckConstraint(
        "btrim(recipient_email) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

gmail_send_reconciliation_jobs = sa.Table(
    "gmail_send_reconciliation_jobs",
    metadata,
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
)

gmail_send_review_evidence = sa.Table(
    "gmail_send_review_evidence",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_send_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "release_qualification_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.release_qualifications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("action_intent_id", sa.Uuid(), nullable=False),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column(
        "approval_request_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.approval_requests.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "reviewed_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
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
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
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
        "review_evidence_sha256 ~ '^[a-f0-9]{64}$'",
        name="review_evidence_sha256_format",
    ),
    sa.CheckConstraint(
        "review_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
        name="review_snapshot_sha256_format",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(attachment_sha256s) = 'array'", name="attachment_sha256s_array"
    ),
    sa.CheckConstraint(
        "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

gmail_send_command_receipts = sa.Table(
    "gmail_send_command_receipts",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_send_accounts.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
        "command_kind IN ('create_draft', 'review_draft', "
        "'register_account', 'reserve_and_enqueue')",
        name="command_kind_values",
    ),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
    sa.CheckConstraint(
        "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

sa.Index(
    "ix_gmail_send_accounts_owner_updated_at",
    gmail_send_accounts.c.owner_user_id,
    gmail_send_accounts.c.updated_at,
)

sa.Index(
    "ix_gmail_send_drafts_account_created_at",
    gmail_send_drafts.c.gmail_send_account_id,
    gmail_send_drafts.c.created_at,
)

sa.Index(
    "ix_gmail_send_reservations_account_created_at",
    gmail_send_reservations.c.gmail_send_account_id,
    gmail_send_reservations.c.created_at,
)

sa.Index(
    "ix_gmail_send_reconciliation_jobs_available_at",
    gmail_send_reconciliation_jobs.c.status,
    gmail_send_reconciliation_jobs.c.available_at,
)

sa.Index(
    "ix_gmail_send_review_evidence_action_payload",
    gmail_send_review_evidence.c.action_intent_id,
    gmail_send_review_evidence.c.payload_version_id,
    gmail_send_review_evidence.c.created_at,
)

greenhouse_submit_accounts = sa.Table(
    "greenhouse_submit_accounts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
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
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
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
    sa.ForeignKeyConstraint(
        ["owner_user_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.owner_user_id", f"{DATABASE_SCHEMA}.candidates.id"],
        name="fk_greenhouse_submit_accounts_owner_candidate",
        ondelete="RESTRICT",
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
    sa.CheckConstraint("board_token_sha256 ~ '^[a-f0-9]{64}$'", name="board_token_sha256_format"),
    sa.CheckConstraint(
        "credential_profile_version > 0",
        name="credential_profile_version_positive",
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
    sa.CheckConstraint("daily_submit_limit BETWEEN 1 AND 500", name="daily_submit_limit_bounds"),
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
)

greenhouse_submit_review_evidence = sa.Table(
    "greenhouse_submit_review_evidence",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.greenhouse_submit_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "release_qualification_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.release_qualifications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("action_intent_id", sa.Uuid(), nullable=False),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "approval_request_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.approval_requests.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "reviewed_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
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
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
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
    sa.CheckConstraint("board_token_sha256 ~ '^[a-f0-9]{64}$'", name="board_token_sha256_format"),
    sa.CheckConstraint("btrim(job_post_id) <> ''", name="job_post_id_nonempty"),
    sa.CheckConstraint("btrim(internal_job_id) <> ''", name="internal_job_id_nonempty"),
    sa.CheckConstraint("job_id_sha256 ~ '^[a-f0-9]{64}$'", name="job_id_sha256_format"),
    sa.CheckConstraint("schema_sha256 ~ '^[a-f0-9]{64}$'", name="schema_sha256_format"),
    sa.CheckConstraint("raw_response_sha256 ~ '^[a-f0-9]{64}$'", name="raw_response_sha256_format"),
    sa.CheckConstraint(
        "normalized_schema_sha256 ~ '^[a-f0-9]{64}$'",
        name="normalized_schema_sha256_format",
    ),
    sa.CheckConstraint("btrim(job_updated_at) <> ''", name="job_updated_at_nonempty"),
    sa.CheckConstraint(
        "application_deadline IS NULL OR btrim(application_deadline) <> ''",
        name="application_deadline_optional_nonempty",
    ),
    sa.CheckConstraint("job_identity_sha256 ~ '^[a-f0-9]{64}$'", name="job_identity_sha256_format"),
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
        "credential_profile_status = 'active'", name="credential_profile_status_active"
    ),
    sa.CheckConstraint(
        "adapter_id = 'greenhouse-job-board' AND adapter_version = 'greenhouse-submit.v1'",
        name="adapter_identity_exact",
    ),
    sa.CheckConstraint(
        "release_version = 'greenhouse-submit-release.v1'",
        name="release_version_exact",
    ),
    sa.CheckConstraint(
        "release_evidence_sha256 ~ '^[a-f0-9]{64}$'",
        name="release_evidence_sha256_format",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(attachment_sha256s) = 'array'", name="attachment_sha256s_array"
    ),
    sa.CheckConstraint(
        "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

greenhouse_submit_reconciliation_jobs = sa.Table(
    "greenhouse_submit_reconciliation_jobs",
    metadata,
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
)

greenhouse_submit_reconciliation_evidence = sa.Table(
    "greenhouse_submit_reconciliation_evidence",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("owner_user_id", sa.Uuid(), nullable=False),
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
        "observed_status IN ('accepted_unverified', 'ambiguous', "
        "'provider_rejected', 'reconciliation_required')",
        name="observed_status_values",
    ),
    sa.CheckConstraint("reason_code ~ '^[A-Z0-9_]{1,64}$'", name="reason_code_format"),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
)

greenhouse_submit_reconciliation_reviews = sa.Table(
    "greenhouse_submit_reconciliation_reviews",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("owner_user_id", sa.Uuid(), nullable=False),
    sa.Column(
        "evidence_id",
        sa.Uuid(),
        sa.ForeignKey(
            f"{DATABASE_SCHEMA}.greenhouse_submit_reconciliation_evidence.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    ),
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
)

greenhouse_submit_command_receipts = sa.Table(
    "greenhouse_submit_command_receipts",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.greenhouse_submit_accounts.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
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
        "command_kind IN ("
        "'create_draft', 'review_draft', 'register_account', "
        "'reserve_and_enqueue')",
        name="command_kind_values",
    ),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
    sa.CheckConstraint(
        "btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

sa.Index(
    "ix_greenhouse_submit_accounts_owner_updated_at",
    greenhouse_submit_accounts.c.owner_user_id,
    greenhouse_submit_accounts.c.updated_at,
)

sa.Index(
    "ix_greenhouse_submit_reconciliation_jobs_available_at",
    greenhouse_submit_reconciliation_jobs.c.status,
    greenhouse_submit_reconciliation_jobs.c.available_at,
)

sa.Index(
    "ix_greenhouse_submit_review_evidence_action_payload",
    greenhouse_submit_review_evidence.c.action_intent_id,
    greenhouse_submit_review_evidence.c.payload_version_id,
    greenhouse_submit_review_evidence.c.created_at,
)

action_intents = sa.Table(
    "action_intents",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("action_kind", sa.String(32), nullable=False),
    sa.Column("resource_type", sa.String(32), nullable=False),
    sa.Column("resource_id", sa.Uuid(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
    sa.Column("status", sa.String(32), server_default="proposed", nullable=False),
    sa.Column("current_payload_version_id", sa.Uuid()),
    sa.Column("created_by", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('proposed', 'denied', 'awaiting_approval', 'eligible', "
        "'processing', 'confirmed', 'failed', 'reconciliation_required')",
        name="status_values",
    ),
    sa.ForeignKeyConstraint(
        ["id", "current_payload_version_id"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
        ],
        name="fk_action_intents_current_payload_identity",
        use_alter=True,
        ondelete="RESTRICT",
    ),
)

action_payload_versions = sa.Table(
    "action_payload_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "attachment_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "version",
        name="uq_action_payload_versions_intent_version",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "payload_hash",
        name="uq_action_payload_versions_intent_hash",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "id",
        name="uq_action_payload_versions_intent_id",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "id",
        "payload_hash",
        name="uq_action_payload_versions_intent_id_hash",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("char_length(payload_hash) = 64", name="payload_hash_length"),
)

policy_decisions = sa.Table(
    "policy_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("ruleset_version", sa.Text(), nullable=False),
    sa.Column("decision", sa.String(32), nullable=False),
    sa.Column(
        "reason_codes",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "decision IN ('deny', 'require_approval', 'allow', 'allow_autopilot_submission')",
        name="decision_values",
    ),
    sa.CheckConstraint("char_length(payload_hash) = 64", name="payload_hash_length"),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "payload_hash"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
        ],
        name="fk_policy_decisions_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "payload_version_id",
        "id",
        name="uq_policy_decisions_intent_payload_id",
    ),
)

approval_requests = sa.Table(
    "approval_requests",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("policy_decision_id", sa.Uuid(), nullable=False),
    sa.Column("requested_for", sa.Text(), nullable=False),
    sa.Column("decision", sa.String(16), server_default="pending", nullable=False),
    sa.Column("decision_rule_reference", sa.Text()),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "decision IN ('pending', 'approved', 'rejected', 'expired')",
        name="decision_values",
    ),
    sa.CheckConstraint(
        "(decision = 'pending' AND decided_at IS NULL) OR "
        "(decision <> 'pending' AND decided_at IS NOT NULL)",
        name="decision_timestamp_consistent",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
        ],
        name="fk_approval_requests_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "policy_decision_id"],
        [
            f"{DATABASE_SCHEMA}.policy_decisions.action_intent_id",
            f"{DATABASE_SCHEMA}.policy_decisions.payload_version_id",
            f"{DATABASE_SCHEMA}.policy_decisions.id",
        ],
        name="fk_approval_requests_policy_identity",
        ondelete="RESTRICT",
    ),
)

outbox_events = sa.Table(
    "outbox_events",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("event_key", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("status", sa.String(16), server_default="pending", nullable=False),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lease_owner", sa.Text()),
    sa.Column("lease_token", sa.Uuid()),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.Column("last_error_code", sa.Text()),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'leased', 'published', 'failed')",
        name="status_values",
    ),
    sa.CheckConstraint(
        "event_type IN ('workflow_signal', 'internal_notification')",
        name="event_type_values",
    ),
    sa.CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
    sa.CheckConstraint(
        "(status = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL "
        "AND lease_until IS NOT NULL) OR "
        "(status <> 'leased' AND lease_owner IS NULL AND lease_token IS NULL "
        "AND lease_until IS NULL)",
        name="lease_state_consistent",
    ),
    sa.CheckConstraint(
        "(status = 'published' AND published_at IS NOT NULL) OR "
        "(status <> 'published' AND published_at IS NULL)",
        name="published_state_consistent",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
        ],
        name="fk_outbox_events_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "id",
        name="uq_outbox_events_intent_id",
    ),
)

sa.Index(
    "ix_outbox_events_status_available_at",
    outbox_events.c.status,
    outbox_events.c.available_at,
)

side_effect_attempts = sa.Table(
    "side_effect_attempts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("request_fingerprint", sa.String(64), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("error_code", sa.Text()),
    sa.Column(
        "response_metadata",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "ordinal",
        name="uq_side_effect_attempts_intent_ordinal",
    ),
    sa.CheckConstraint("ordinal > 0", name="ordinal_positive"),
    sa.CheckConstraint("char_length(request_fingerprint) = 64", name="fingerprint_length"),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "outbox_event_id"],
        [
            f"{DATABASE_SCHEMA}.outbox_events.action_intent_id",
            f"{DATABASE_SCHEMA}.outbox_events.id",
        ],
        name="fk_side_effect_attempts_outbox_identity",
        ondelete="RESTRICT",
    ),
)

provider_receipts = sa.Table(
    "provider_receipts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "side_effect_attempt_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.side_effect_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider", sa.String(24), nullable=False),
    sa.Column("provider_resource_id", sa.Text(), nullable=False),
    sa.Column("reconciliation_key", sa.Text(), nullable=False),
    sa.Column("final_state", sa.String(24), nullable=False),
    sa.Column("provider_timestamp", sa.DateTime(timezone=True)),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "receipt_metadata",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "provider",
        "reconciliation_key",
        name="uq_provider_receipts_provider_reconciliation_key",
    ),
)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("actor_type", sa.String(24), nullable=False),
    sa.Column("actor_id", sa.Text()),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("resource_type", sa.String(32), nullable=False),
    sa.Column("resource_id", sa.Uuid(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "event_data",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("previous_hash", sa.String(64)),
    sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "previous_hash IS NULL OR char_length(previous_hash) = 64",
        name="previous_hash_length",
    ),
    sa.CheckConstraint("char_length(event_hash) = 64", name="event_hash_length"),
)

console_users = sa.Table(
    "console_users",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("singleton_key", sa.SmallInteger(), server_default="1", nullable=False, unique=True),
    sa.Column("username", sa.String(64), nullable=False, unique=True),
    sa.Column("password_hash", sa.Text(), nullable=False),
    sa.Column("password_algorithm", sa.String(16), server_default="argon2id", nullable=False),
    sa.Column(
        "password_parameters",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("disabled_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("singleton_key = 1", name="singleton_key_value"),
    sa.CheckConstraint(
        "username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
        name="username_canonical",
    ),
    sa.CheckConstraint("password_algorithm = 'argon2id'", name="password_algorithm_value"),
    sa.CheckConstraint("password_hash LIKE '$argon2id$%'", name="password_hash_format"),
)

candidate_material_bundles = sa.Table(
    "candidate_material_bundles",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column("version", sa.BigInteger(), nullable=False),
    sa.Column("schema_version", sa.String(48), nullable=False),
    sa.Column("canonicalization_version", sa.String(48), nullable=False),
    sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("material_count", sa.SmallInteger(), nullable=False),
    sa.Column("bundle_sha256", sa.String(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["owner_user_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.owner_user_id", f"{DATABASE_SCHEMA}.candidates.id"],
        name="fk_candidate_material_bundles_owner_candidate",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "id",
        "owner_user_id",
        "candidate_id",
        name="uq_candidate_material_bundles_identity",
    ),
    sa.UniqueConstraint(
        "id",
        "owner_user_id",
        "candidate_id",
        "bundle_sha256",
        name="uq_candidate_material_bundles_hash_identity",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "candidate_id",
        "version",
        name="uq_candidate_material_bundles_candidate_version",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint(
        "schema_version = 'candidate-material-bundle.v1'",
        name="schema_version_exact",
    ),
    sa.CheckConstraint(
        "canonicalization_version = 'careerops-json-c14n.v1'",
        name="canonicalization_version_exact",
    ),
    sa.CheckConstraint("jsonb_typeof(manifest_json) = 'array'", name="manifest_json_array"),
    sa.CheckConstraint("material_count BETWEEN 1 AND 10", name="material_count_bounds"),
    sa.CheckConstraint(
        "jsonb_array_length(manifest_json) = material_count",
        name="manifest_count_matches",
    ),
    sa.CheckConstraint("bundle_sha256 ~ '^[a-f0-9]{64}$'", name="bundle_sha256_format"),
)

candidate_material_versions = sa.Table(
    "candidate_material_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("bundle_id", sa.Uuid(), nullable=False),
    sa.Column("owner_user_id", sa.Uuid(), nullable=False),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column("ordinal", sa.SmallInteger(), nullable=False),
    sa.Column("material_kind", sa.String(24), nullable=False),
    sa.Column("label", sa.Text(), nullable=False),
    sa.Column("filename", sa.Text(), nullable=False),
    sa.Column("media_type", sa.Text(), nullable=False),
    sa.Column(
        "content_object_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.content_objects.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("object_key", sa.Text(), nullable=False),
    sa.Column("byte_size", sa.BigInteger(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["bundle_id", "owner_user_id", "candidate_id"],
        [
            f"{DATABASE_SCHEMA}.candidate_material_bundles.id",
            f"{DATABASE_SCHEMA}.candidate_material_bundles.owner_user_id",
            f"{DATABASE_SCHEMA}.candidate_material_bundles.candidate_id",
        ],
        name="fk_candidate_material_versions_bundle_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "bundle_id",
        "ordinal",
        name="uq_candidate_material_versions_bundle_ordinal",
    ),
    sa.UniqueConstraint(
        "bundle_id",
        "content_object_id",
        name="uq_candidate_material_versions_bundle_content_object",
    ),
    sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ordinal_bounds"),
    sa.CheckConstraint(
        "material_kind IN ('resume', 'cover_letter', 'portfolio', 'certificate', 'other')",
        name="material_kind_values",
    ),
    sa.CheckConstraint(
        "btrim(label) <> '' AND char_length(label) <= 160",
        name="label_bounded",
    ),
    sa.CheckConstraint(
        "btrim(filename) <> '' AND char_length(filename) <= 255 "
        "AND position('/' in filename) = 0 AND position(chr(92) in filename) = 0 "
        "AND filename NOT IN ('.', '..')",
        name="filename_safe",
    ),
    sa.CheckConstraint("btrim(media_type) <> ''", name="media_type_nonempty"),
    sa.CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'", name="sha256_format"),
    sa.CheckConstraint(
        "object_key ~ '^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$' "
        "AND right(object_key, 64) = sha256",
        name="object_key_matches_sha256",
    ),
    sa.CheckConstraint("byte_size BETWEEN 1 AND 10485760", name="byte_size_bounds"),
)

candidate_profile_versions = sa.Table(
    "candidate_profile_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column("version", sa.BigInteger(), nullable=False),
    sa.Column("profile_schema_version", sa.String(48), nullable=False),
    sa.Column("preferences_schema_version", sa.String(48), nullable=False),
    sa.Column("snapshot_schema_version", sa.String(48), nullable=False),
    sa.Column("canonicalization_version", sa.String(48), nullable=False),
    sa.Column("profile_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("preferences_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("material_bundle_id", sa.Uuid(), nullable=False),
    sa.Column("material_bundle_sha256", sa.String(64), nullable=False),
    sa.Column("snapshot_sha256", sa.String(64), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("import_idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["owner_user_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.owner_user_id", f"{DATABASE_SCHEMA}.candidates.id"],
        name="fk_candidate_profile_versions_owner_candidate",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        [
            "material_bundle_id",
            "owner_user_id",
            "candidate_id",
            "material_bundle_sha256",
        ],
        [
            f"{DATABASE_SCHEMA}.candidate_material_bundles.id",
            f"{DATABASE_SCHEMA}.candidate_material_bundles.owner_user_id",
            f"{DATABASE_SCHEMA}.candidate_material_bundles.candidate_id",
            f"{DATABASE_SCHEMA}.candidate_material_bundles.bundle_sha256",
        ],
        name="fk_candidate_profile_versions_bundle_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "id",
        "owner_user_id",
        "candidate_id",
        "snapshot_sha256",
        name="uq_candidate_profile_versions_hash_identity",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "candidate_id",
        "version",
        name="uq_candidate_profile_versions_candidate_version",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "import_idempotency_key",
        name="uq_candidate_profile_versions_owner_idempotency",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint(
        "profile_schema_version = 'candidate-profile.v1'",
        name="profile_schema_version_exact",
    ),
    sa.CheckConstraint(
        "preferences_schema_version = 'candidate-job-preferences.v1'",
        name="preferences_schema_version_exact",
    ),
    sa.CheckConstraint(
        "snapshot_schema_version = 'candidate-profile-snapshot.v1'",
        name="snapshot_schema_version_exact",
    ),
    sa.CheckConstraint(
        "canonicalization_version = 'careerops-json-c14n.v1'",
        name="canonicalization_version_exact",
    ),
    sa.CheckConstraint("jsonb_typeof(profile_json) = 'object'", name="profile_json_object"),
    sa.CheckConstraint(
        "jsonb_typeof(preferences_json) = 'object'",
        name="preferences_json_object",
    ),
    sa.CheckConstraint(
        "material_bundle_sha256 ~ '^[a-f0-9]{64}$'",
        name="material_bundle_sha256_format",
    ),
    sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint(
        "import_idempotency_key ~ '^[A-Za-z0-9._:@/-]{1,160}$' "
        "AND trace_id ~ '^[A-Za-z0-9._:@/-]{1,160}$'",
        name="command_identifiers_bounded",
    ),
)

candidate_profile_decisions = sa.Table(
    "candidate_profile_decisions",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("profile_version_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("owner_user_id", sa.Uuid(), nullable=False),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column(
        "reviewed_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("snapshot_sha256", sa.String(64), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["profile_version_id", "owner_user_id", "candidate_id", "snapshot_sha256"],
        [
            f"{DATABASE_SCHEMA}.candidate_profile_versions.id",
            f"{DATABASE_SCHEMA}.candidate_profile_versions.owner_user_id",
            f"{DATABASE_SCHEMA}.candidate_profile_versions.candidate_id",
            f"{DATABASE_SCHEMA}.candidate_profile_versions.snapshot_sha256",
        ],
        name="fk_candidate_profile_decisions_profile_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "owner_user_id",
        "idempotency_key",
        name="uq_candidate_profile_decisions_owner_idempotency",
    ),
    sa.CheckConstraint("reviewed_by_user_id = owner_user_id", name="reviewer_is_owner"),
    sa.CheckConstraint("decision IN ('approve', 'reject')", name="decision_values"),
    sa.CheckConstraint(
        "btrim(reason) <> '' AND char_length(reason) <= 4000",
        name="reason_bounded",
    ),
    sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint(
        "idempotency_key ~ '^[A-Za-z0-9._:@/-]{1,160}$' "
        "AND trace_id ~ '^[A-Za-z0-9._:@/-]{1,160}$'",
        name="command_identifiers_bounded",
    ),
)

sa.Index(
    "ix_candidate_profile_versions_candidate_created_at",
    candidate_profile_versions.c.owner_user_id,
    candidate_profile_versions.c.candidate_id,
    candidate_profile_versions.c.created_at,
)

sa.Index(
    "uq_candidate_material_versions_one_resume",
    candidate_material_versions.c.bundle_id,
    unique=True,
    postgresql_where=candidate_material_versions.c.material_kind == "resume",
)

sa.Index(
    "ix_candidate_profile_decisions_approved",
    candidate_profile_decisions.c.owner_user_id,
    candidate_profile_decisions.c.candidate_id,
    candidate_profile_decisions.c.sequence.desc(),
    postgresql_where=candidate_profile_decisions.c.decision == "approve",
)

bootstrap_tokens = sa.Table(
    "bootstrap_tokens",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
    sa.Column(
        "used_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
    ),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
    sa.CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),
    sa.CheckConstraint(
        "(used_at IS NULL AND used_by_user_id IS NULL) OR "
        "(used_at IS NOT NULL AND used_by_user_id IS NOT NULL)",
        name="use_identity_consistent",
    ),
    sa.CheckConstraint(
        "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
        name="use_or_revoke",
    ),
    sa.CheckConstraint(
        "used_at IS NULL OR (used_at >= issued_at AND used_at < expires_at)",
        name="use_within_lifetime",
    ),
    sa.CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= issued_at",
        name="revocation_after_issue",
    ),
)

console_sessions = sa.Table(
    "console_sessions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="CASCADE"),
    ),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("csrf_token_hash", sa.String(64), nullable=False),
    sa.Column("client_fingerprint", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("rotated_to_session_id", sa.Uuid()),
    sa.ForeignKeyConstraint(
        ["rotated_to_session_id"],
        [f"{DATABASE_SCHEMA}.console_sessions.id"],
        name="fk_console_sessions_rotated_to_session",
        use_alter=True,
        ondelete="SET NULL",
    ),
    sa.CheckConstraint(
        "state IN ('preauth', 'authenticated', 'revoked')",
        name="state_values",
    ),
    sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
    sa.CheckConstraint("csrf_token_hash ~ '^[0-9a-f]{64}$'", name="csrf_hash_format"),
    sa.CheckConstraint(
        "client_fingerprint ~ '^[0-9a-f]{64}$'",
        name="client_fingerprint_format",
    ),
    sa.CheckConstraint(
        "(state = 'preauth' AND user_id IS NULL AND revoked_at IS NULL "
        "AND rotated_to_session_id IS NULL) OR "
        "(state = 'authenticated' AND user_id IS NOT NULL AND revoked_at IS NULL "
        "AND rotated_to_session_id IS NULL) OR "
        "(state = 'revoked' AND revoked_at IS NOT NULL)",
        name="state_consistent",
    ),
    sa.CheckConstraint(
        "created_at <= last_seen_at AND last_seen_at < idle_expires_at "
        "AND idle_expires_at <= absolute_expires_at",
        name="lifetime_ordered",
    ),
    sa.CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= created_at",
        name="revocation_after_creation",
    ),
)

sa.Index(
    "ix_bootstrap_tokens_active_expiry",
    bootstrap_tokens.c.expires_at,
    postgresql_where=sa.and_(
        bootstrap_tokens.c.used_at.is_(None),
        bootstrap_tokens.c.revoked_at.is_(None),
    ),
)

sa.Index(
    "ix_console_sessions_user_state",
    console_sessions.c.user_id,
    console_sessions.c.state,
)

sa.Index(
    "ix_console_sessions_expiry",
    console_sessions.c.idle_expires_at,
    console_sessions.c.absolute_expires_at,
    postgresql_where=console_sessions.c.revoked_at.is_(None),
)

autopilot_campaigns = sa.Table(
    "autopilot_campaigns",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
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
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("btrim(name) <> ''", name="name_nonempty"),
    sa.CheckConstraint("btrim(objective) <> ''", name="objective_nonempty"),
    sa.CheckConstraint("jsonb_typeof(criteria) = 'object'", name="criteria_object"),
    sa.CheckConstraint("jsonb_typeof(exclusions) = 'array'", name="exclusions_array"),
)

autopilot_grant_versions = sa.Table(
    "autopilot_grant_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "campaign_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.autopilot_campaigns.id", ondelete="RESTRICT"),
        nullable=False,
    ),
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
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "campaign_id",
        "version",
        name="uq_autopilot_grant_versions_campaign_version",
    ),
    sa.UniqueConstraint(
        "campaign_id",
        "id",
        name="uq_autopilot_grant_versions_campaign_id",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("btrim(subject_actor) <> ''", name="subject_actor_nonempty"),
    sa.CheckConstraint(
        "jsonb_typeof(allowed_action_kinds) = 'array'",
        name="allowed_action_kinds_array",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(allowed_channels) = 'array'",
        name="allowed_channels_array",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(allowed_target_hosts) = 'array'",
        name="allowed_target_hosts_array",
    ),
    sa.CheckConstraint("jsonb_typeof(material_hashes) = 'array'", name="material_hashes_array"),
    sa.CheckConstraint("max_total_submissions > 0", name="max_total_submissions_positive"),
    sa.CheckConstraint("max_daily_submissions > 0", name="max_daily_submissions_positive"),
    sa.CheckConstraint("max_per_company > 0", name="max_per_company_positive"),
    sa.CheckConstraint("btrim(policy_ruleset_version) <> ''", name="policy_ruleset_nonempty"),
    sa.CheckConstraint("btrim(release_version) <> ''", name="release_version_nonempty"),
    sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)

autopilot_grant_revocations = sa.Table(
    "autopilot_grant_revocations",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "grant_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.autopilot_grant_versions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "revoked_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "superseded_by_grant_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.autopilot_grant_versions.id", ondelete="RESTRICT"),
    ),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("btrim(reason) <> ''", name="reason_nonempty"),
    sa.CheckConstraint(
        "superseded_by_grant_version_id IS NULL "
        "OR superseded_by_grant_version_id <> grant_version_id",
        name="not_self_superseded",
    ),
)

autopilot_intent_authorizations = sa.Table(
    "autopilot_intent_authorizations",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("campaign_id", sa.Uuid(), nullable=False),
    sa.Column("grant_version_id", sa.Uuid(), nullable=False),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("policy_decision_id", sa.Uuid(), nullable=False),
    sa.Column("authorization_outcome", sa.String(32), nullable=False),
    sa.Column(
        "reason_codes",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["campaign_id", "grant_version_id"],
        [
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.campaign_id",
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.id",
        ],
        name="fk_autopilot_intent_authorizations_grant_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "payload_hash"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
        ],
        name="fk_autopilot_intent_authorizations_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "policy_decision_id"],
        [
            f"{DATABASE_SCHEMA}.policy_decisions.action_intent_id",
            f"{DATABASE_SCHEMA}.policy_decisions.payload_version_id",
            f"{DATABASE_SCHEMA}.policy_decisions.id",
        ],
        name="fk_autopilot_intent_authorizations_policy_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "payload_version_id",
        "policy_decision_id",
        name="uq_autopilot_intent_authorizations_policy_binding",
    ),
    sa.CheckConstraint(
        "authorization_outcome IN ("
        "'allow_autopilot_submission', 'require_human_approval', "
        "'manual_only', 'deny')",
        name="authorization_outcome_values",
    ),
    sa.CheckConstraint("jsonb_typeof(reason_codes) = 'array'", name="reason_codes_array"),
    sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_format"),
    sa.CheckConstraint("expires_at > authorized_at", name="expiry_after_authorization"),
)

autopilot_review_items = sa.Table(
    "autopilot_review_items",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "authorization_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.autopilot_intent_authorizations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("review_kind", sa.String(24), nullable=False),
    sa.Column("resolution_mode", sa.String(24), nullable=False),
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
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "authorization_id",
        "review_kind",
        name="uq_autopilot_review_items_authorization_kind",
    ),
    sa.CheckConstraint(
        "review_kind IN ('pending', 'exceptions', 'recovery')",
        name="review_kind_values",
    ),
    sa.CheckConstraint(
        "resolution_mode IN ('agent_approvable', 'manual_only', 'remediation_required')",
        name="resolution_mode_values",
    ),
    sa.CheckConstraint(
        "NOT (review_kind = 'pending' AND resolution_mode = 'manual_only')",
        name="manual_only_not_pending",
    ),
    sa.CheckConstraint("jsonb_typeof(reason_codes) = 'array'", name="reason_codes_array"),
    sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
    sa.CheckConstraint("btrim(created_by) <> ''", name="created_by_nonempty"),
)

autopilot_cap_reservations = sa.Table(
    "autopilot_cap_reservations",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("campaign_id", sa.Uuid(), nullable=False),
    sa.Column("grant_version_id", sa.Uuid(), nullable=False),
    sa.Column(
        "authorization_id",
        sa.Uuid(),
        sa.ForeignKey(
            f"{DATABASE_SCHEMA}.autopilot_intent_authorizations.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("target_host", sa.Text(), nullable=False),
    sa.Column("channel", sa.Text(), nullable=False),
    sa.Column("release_version", sa.Text(), nullable=False),
    sa.Column("company_key", sa.Text(), nullable=False),
    sa.Column("adapter_id", sa.Text(), nullable=False),
    sa.Column("fixture_id", sa.Text(), nullable=False),
    sa.Column("reservation_key", sa.Text(), nullable=False, unique=True),
    sa.Column("reconciliation_key", sa.Text(), nullable=False, unique=True),
    sa.Column("release_evidence_hash", sa.String(64)),
    sa.Column("release_evidence_expires_at", sa.DateTime(timezone=True)),
    sa.Column(
        "reservation_date", sa.Date(), server_default=sa.text("CURRENT_DATE"), nullable=False
    ),
    sa.Column(
        "reserved_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["campaign_id", "grant_version_id"],
        [
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.campaign_id",
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.id",
        ],
        name="fk_autopilot_cap_reservations_grant_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "payload_hash"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
        ],
        name="fk_autopilot_cap_reservations_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
        ],
        name="fk_autopilot_cap_reservations_payload_version_identity",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_format"),
    sa.CheckConstraint("btrim(target_host) <> ''", name="target_host_nonempty"),
    sa.CheckConstraint("btrim(channel) <> ''", name="channel_nonempty"),
    sa.CheckConstraint("btrim(release_version) <> ''", name="release_version_nonempty"),
    sa.CheckConstraint("btrim(company_key) <> ''", name="company_key_nonempty"),
    sa.CheckConstraint("btrim(adapter_id) <> ''", name="adapter_id_nonempty"),
    sa.CheckConstraint("btrim(fixture_id) <> ''", name="fixture_id_nonempty"),
    sa.CheckConstraint("btrim(reservation_key) <> ''", name="reservation_key_nonempty"),
    sa.CheckConstraint(
        "btrim(reconciliation_key) <> ''",
        name="reconciliation_key_nonempty",
    ),
    sa.CheckConstraint(
        "release_evidence_hash IS NULL OR release_evidence_hash ~ '^[0-9a-f]{64}$'",
        name="release_evidence_hash_format",
    ),
    sa.CheckConstraint(
        "(release_evidence_hash IS NULL) = (release_evidence_expires_at IS NULL) "
        "AND (release_evidence_expires_at IS NULL OR release_evidence_expires_at > reserved_at)",
        name="release_evidence_expiry_current",
    ),
    sa.CheckConstraint(
        "reservation_date = reserved_at::date",
        name="reservation_date_matches_reserved_at",
    ),
)

sa.Index(
    "ix_autopilot_cap_reservations_grant_date",
    autopilot_cap_reservations.c.grant_version_id,
    autopilot_cap_reservations.c.reservation_date,
)

sa.Index(
    "ix_autopilot_cap_reservations_grant_company",
    autopilot_cap_reservations.c.grant_version_id,
    autopilot_cap_reservations.c.company_key,
)

sa.Index(
    "ix_autopilot_campaigns_owner_created_at",
    autopilot_campaigns.c.owner_user_id,
    autopilot_campaigns.c.created_at,
)

sa.Index(
    "ix_autopilot_grant_versions_campaign_created_at",
    autopilot_grant_versions.c.campaign_id,
    autopilot_grant_versions.c.created_at,
)

sa.Index(
    "ix_autopilot_intent_authorizations_campaign_created_at",
    autopilot_intent_authorizations.c.campaign_id,
    autopilot_intent_authorizations.c.created_at,
)

sa.Index(
    "ix_autopilot_review_items_resolution_created_at",
    autopilot_review_items.c.resolution_mode,
    autopilot_review_items.c.created_at,
)

crawler_execution_requests = sa.Table(
    "crawler_execution_requests",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "owner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("payload_version_id", sa.Uuid(), nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("manifest_path", sa.Text(), nullable=False),
    sa.Column("request_artifact_path", sa.Text(), nullable=False),
    sa.Column("manifest_sha256", sa.String(64), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("reviewed_plan_sha256", sa.String(64), nullable=False),
    sa.Column(
        "source_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
        ],
        name="fk_crawler_execution_requests_payload_version_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "payload_hash"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
        ],
        name="fk_crawler_execution_requests_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "action_intent_id",
        "payload_version_id",
        name="uq_crawler_execution_requests_intent_payload",
    ),
    sa.UniqueConstraint(
        "id",
        "action_intent_id",
        name="uq_crawler_execution_requests_id_intent",
    ),
    sa.CheckConstraint("payload_hash ~ '^[0-9a-f]{64}$'", name="payload_hash_format"),
    sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="manifest_sha256_format"),
    sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint(
        "reviewed_plan_sha256 ~ '^[0-9a-f]{64}$'",
        name="reviewed_plan_sha256_format",
    ),
    sa.CheckConstraint(
        "manifest_path ~ '^[A-Za-z0-9._/-]+$' "
        "AND left(manifest_path, 1) <> '/' "
        "AND manifest_path !~ '(^|/)\\.\\.(/|$)'",
        name="manifest_path_safe_relative",
    ),
    sa.CheckConstraint(
        "request_artifact_path ~ '^[A-Za-z0-9._/-]+$' "
        "AND left(request_artifact_path, 1) <> '/' "
        "AND request_artifact_path !~ '(^|/)\\.\\.(/|$)'",
        name="request_artifact_path_safe_relative",
    ),
    sa.CheckConstraint("jsonb_typeof(source_ids) = 'array'", name="source_ids_array"),
    sa.CheckConstraint("btrim(reason) <> ''", name="reason_nonempty"),
    sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)

crawler_execution_approvals = sa.Table(
    "crawler_execution_approvals",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "request_id",
        sa.Uuid(),
        sa.ForeignKey(
            f"{DATABASE_SCHEMA}.crawler_execution_requests.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "decided_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("decision_reason", sa.Text(), nullable=False),
    sa.Column("approval_artifact_path", sa.Text()),
    sa.Column("approval_artifact_sha256", sa.String(64)),
    sa.Column(
        "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("decision IN ('approved', 'rejected')", name="decision_values"),
    sa.CheckConstraint("btrim(decision_reason) <> ''", name="decision_reason_nonempty"),
    sa.CheckConstraint(
        "(decision = 'approved' AND approval_artifact_path IS NOT NULL "
        "AND approval_artifact_sha256 IS NOT NULL) OR "
        "(decision = 'rejected' AND approval_artifact_path IS NULL "
        "AND approval_artifact_sha256 IS NULL)",
        name="approval_artifact_matches_decision",
    ),
    sa.CheckConstraint(
        "approval_artifact_sha256 IS NULL OR approval_artifact_sha256 ~ '^[0-9a-f]{64}$'",
        name="approval_artifact_sha256_format",
    ),
    sa.CheckConstraint(
        "approval_artifact_path IS NULL OR ("
        "approval_artifact_path ~ '^[A-Za-z0-9._/-]+$' "
        "AND left(approval_artifact_path, 1) <> '/' "
        "AND approval_artifact_path !~ '(^|/)\\.\\.(/|$)')",
        name="approval_artifact_path_safe_relative",
    ),
)

crawler_execution_dispatches = sa.Table(
    "crawler_execution_dispatches",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("request_id", sa.Uuid(), nullable=False),
    sa.Column("action_intent_id", sa.Uuid(), nullable=False),
    sa.Column("outbox_event_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("execution_key", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "dispatched_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["request_id", "action_intent_id"],
        [
            f"{DATABASE_SCHEMA}.crawler_execution_requests.id",
            f"{DATABASE_SCHEMA}.crawler_execution_requests.action_intent_id",
        ],
        name="fk_crawler_execution_dispatches_request_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "outbox_event_id"],
        [
            f"{DATABASE_SCHEMA}.outbox_events.action_intent_id",
            f"{DATABASE_SCHEMA}.outbox_events.id",
        ],
        name="fk_crawler_execution_dispatches_outbox_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "request_id",
        name="uq_crawler_execution_dispatches_request_id",
    ),
    sa.CheckConstraint("btrim(execution_key) <> ''", name="execution_key_nonempty"),
)

crawler_execution_results = sa.Table(
    "crawler_execution_results",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("action_intent_id", sa.Uuid(), nullable=False),
    sa.Column("outbox_event_id", sa.Uuid(), nullable=False, unique=True),
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("error_code", sa.String(64)),
    sa.Column(
        "completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["request_id", "action_intent_id"],
        [
            f"{DATABASE_SCHEMA}.crawler_execution_requests.id",
            f"{DATABASE_SCHEMA}.crawler_execution_requests.action_intent_id",
        ],
        name="fk_crawler_execution_results_request_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "outbox_event_id"],
        [
            f"{DATABASE_SCHEMA}.outbox_events.action_intent_id",
            f"{DATABASE_SCHEMA}.outbox_events.id",
        ],
        name="fk_crawler_execution_results_outbox_identity",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "outcome IN ('succeeded', 'failed', 'reconciliation_required')",
        name="outcome_values",
    ),
    sa.CheckConstraint(
        "(outcome = 'succeeded' AND error_code IS NULL) OR "
        "(outcome IN ('failed', 'reconciliation_required') AND error_code IS NOT NULL)",
        name="error_code_matches_outcome",
    ),
    sa.CheckConstraint(
        "error_code IS NULL OR error_code ~ '^[A-Z0-9_]{1,64}$'",
        name="error_code_format",
    ),
)

release_qualifications = sa.Table(
    "release_qualifications",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("capability", sa.Text(), nullable=False),
    sa.Column("action_name", sa.Text(), nullable=False),
    sa.Column("rollout_mode", sa.String(24), nullable=False),
    sa.Column("provider", sa.Text(), nullable=False),
    sa.Column("adapter_id", sa.Text(), nullable=False),
    sa.Column("adapter_version", sa.Text(), nullable=False),
    sa.Column("implementation_hash", sa.String(64), nullable=False),
    sa.Column("config_hash", sa.String(64), nullable=False),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("dataset_hash", sa.String(64), nullable=False),
    sa.Column("git_commit", sa.String(64), nullable=False),
    sa.Column("image_digest", sa.Text()),
    sa.Column("migration_revision", sa.Text(), nullable=False),
    sa.Column("oauth_scope_hash", sa.String(64), nullable=False),
    sa.Column("credential_ref_hash", sa.String(64), nullable=False),
    sa.Column("network_policy_hash", sa.String(64), nullable=False),
    sa.Column("reconcile_policy_hash", sa.String(64), nullable=False),
    sa.Column("hard_stop_hash", sa.String(64), nullable=False),
    sa.Column("sensitive_field_hash", sa.String(64), nullable=False),
    sa.Column("kill_switch_hash", sa.String(64), nullable=False),
    sa.Column("fixture_manifest_sha256", sa.String(64), nullable=False),
    sa.Column("fault_manifest_sha256", sa.String(64), nullable=False),
    sa.Column("holdout_manifest_sha256", sa.String(64)),
    sa.Column("live_sample_manifest_sha256", sa.String(64)),
    sa.Column("status", sa.String(24), server_default="draft", nullable=False),
    sa.Column(
        "requested_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("created_by", sa.Text(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "rollout_mode IN ('shadow', 'review_required', 'synthetic_sandbox', "
        "'limited_autopilot', 'expanded_autopilot')",
        name="rollout_mode_values",
    ),
    sa.CheckConstraint("status = 'draft'", name="status_initial_draft"),
    sa.CheckConstraint(
        "implementation_hash ~ '^[0-9a-f]{64}$' "
        "AND config_hash ~ '^[0-9a-f]{64}$' "
        "AND policy_hash ~ '^[0-9a-f]{64}$' "
        "AND dataset_hash ~ '^[0-9a-f]{64}$' "
        "AND oauth_scope_hash ~ '^[0-9a-f]{64}$' "
        "AND credential_ref_hash ~ '^[0-9a-f]{64}$' "
        "AND network_policy_hash ~ '^[0-9a-f]{64}$' "
        "AND reconcile_policy_hash ~ '^[0-9a-f]{64}$' "
        "AND hard_stop_hash ~ '^[0-9a-f]{64}$' "
        "AND sensitive_field_hash ~ '^[0-9a-f]{64}$' "
        "AND kill_switch_hash ~ '^[0-9a-f]{64}$' "
        "AND fixture_manifest_sha256 ~ '^[0-9a-f]{64}$' "
        "AND fault_manifest_sha256 ~ '^[0-9a-f]{64}$' "
        "AND (holdout_manifest_sha256 IS NULL "
        "OR holdout_manifest_sha256 ~ '^[0-9a-f]{64}$') "
        "AND (live_sample_manifest_sha256 IS NULL "
        "OR live_sample_manifest_sha256 ~ '^[0-9a-f]{64}$')",
        name="hashes_sha256",
    ),
    sa.CheckConstraint("git_commit ~ '^[0-9a-f]{7,64}$'", name="git_commit_format"),
    sa.CheckConstraint(
        "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
        name="image_digest_format",
    ),
    sa.CheckConstraint(
        "btrim(capability) <> '' AND btrim(action_name) <> '' "
        "AND btrim(provider) <> '' AND btrim(adapter_id) <> '' "
        "AND btrim(adapter_version) <> '' AND btrim(migration_revision) <> '' "
        "AND btrim(created_by) <> ''",
        name="text_nonempty",
    ),
    sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
)

release_qualification_evidence = sa.Table(
    "release_qualification_evidence",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "qualification_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.release_qualifications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("evidence_kind", sa.String(32), nullable=False),
    sa.Column("artifact_uri", sa.Text(), nullable=False),
    sa.Column("artifact_sha256", sa.String(64), nullable=False),
    sa.Column("run_id", sa.Text(), nullable=False),
    sa.Column(
        "runner_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("runner_actor", sa.Text(), nullable=False),
    sa.Column(
        "metrics",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("sample_manifest_sha256", sa.String(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "evidence_kind IN ('fixture', 'fault_injection', 'holdout', "
        "'live_sample', 'metrics', 'audit_drill')",
        name="kind_values",
    ),
    sa.CheckConstraint(
        "artifact_sha256 ~ '^[0-9a-f]{64}$' AND sample_manifest_sha256 ~ '^[0-9a-f]{64}$'",
        name="hashes_sha256",
    ),
    sa.CheckConstraint("jsonb_typeof(metrics) = 'object'", name="metrics_object"),
    sa.CheckConstraint(
        "artifact_uri ~ '^[A-Za-z0-9._:/-]+$' "
        "AND artifact_uri !~ '(^|/)\\.\\.(/|$)' "
        "AND btrim(run_id) <> '' AND btrim(runner_actor) <> ''",
        name="text_safe_nonempty",
    ),
    sa.UniqueConstraint(
        "qualification_id",
        "evidence_kind",
        "artifact_sha256",
        name="uq_release_qualification_evidence_kind_artifact",
    ),
)

release_qualification_decisions = sa.Table(
    "release_qualification_decisions",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "qualification_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.release_qualifications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("from_status", sa.String(32), nullable=False),
    sa.Column("to_status", sa.String(32), nullable=False),
    sa.Column("decision_role", sa.String(16), nullable=False),
    sa.Column("actor_id", sa.Text(), nullable=False),
    sa.Column(
        "decided_by_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
    ),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("evidence_sha256", sa.String(64), nullable=False),
    sa.Column(
        "evidence_ids",
        postgresql.ARRAY(sa.Uuid()),
        server_default=sa.text("ARRAY[]::uuid[]"),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "from_status IN ('draft', 'evaluating', 'pending_independent_review', 'qualified')",
        name="from_status_values",
    ),
    sa.CheckConstraint(
        "to_status IN ('evaluating', 'pending_independent_review', 'qualified', "
        "'rejected', 'expired', 'revoked')",
        name="to_status_values",
    ),
    sa.CheckConstraint(
        "decision_role IN ('runner', 'reviewer', 'operator')",
        name="role_values",
    ),
    sa.CheckConstraint(
        "array_position(evidence_ids, NULL) IS NULL",
        name="evidence_ids_no_nulls",
    ),
    sa.CheckConstraint(
        "btrim(actor_id) <> '' AND (decision_role <> 'operator' OR decided_by_user_id IS NOT NULL)",
        name="actor_binding",
    ),
    sa.CheckConstraint(
        "evidence_sha256 ~ '^[0-9a-f]{64}$'",
        name="evidence_sha256_format",
    ),
    sa.CheckConstraint("btrim(reason) <> ''", name="reason_nonempty"),
    sa.UniqueConstraint(
        "qualification_id",
        "to_status",
        name="uq_release_qualification_decisions_status_once",
    ),
)

crawler_source_registries = sa.Table(
    "crawler_source_registries",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("kind", sa.Text(), nullable=False),
    sa.Column("manifest_path", sa.Text(), nullable=False),
    sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
    sa.Column("registry_sha256", sa.String(64), nullable=False),
    sa.Column("source_count", sa.Integer(), nullable=False),
    sa.Column("created_by", sa.Text(), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("btrim(kind) <> ''", name="kind_nonempty"),
    sa.CheckConstraint("btrim(manifest_path) <> ''", name="manifest_path_nonempty"),
    sa.CheckConstraint(
        "manifest_sha256 ~ '^[0-9a-f]{64}$'",
        name="manifest_sha256_format",
    ),
    sa.CheckConstraint(
        "registry_sha256 ~ '^[0-9a-f]{64}$'",
        name="registry_sha256_format",
    ),
    sa.CheckConstraint("source_count >= 1", name="source_count_positive"),
    sa.CheckConstraint("btrim(created_by) <> ''", name="created_by_nonempty"),
    sa.CheckConstraint(
        "jsonb_typeof(manifest_json) = 'object'",
        name="manifest_json_object",
    ),
    sa.CheckConstraint(
        "generated_at <= created_at + interval '1 day'",
        name="generated_at_reasonable",
    ),
)

crawler_source_registry_sources = sa.Table(
    "crawler_source_registry_sources",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "registry_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_registries.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_id", sa.Text(), nullable=False),
    sa.Column("adapter", sa.Text(), nullable=False),
    sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    sa.Column("input_artifact", sa.Text(), nullable=False),
    sa.Column("output_dir", sa.Text(), nullable=False),
    sa.Column(
        "dependency_source_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "dependency_artifacts",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("command_sha256", sa.String(64), nullable=False),
    sa.Column("source_sha256", sa.String(64), nullable=False),
    sa.Column("source_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("cursor", sa.Text()),
    sa.Column("cadence_seconds", sa.Integer(), nullable=False),
    sa.Column("retry_rounds", sa.Integer(), nullable=False),
    sa.Column(
        "budget_json",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "rate_limit_json",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("robots_terms_policy", sa.Text(), nullable=False),
    sa.Column("canonical_ingestion_policy", sa.Text(), nullable=False),
    sa.Column("provenance_policy", sa.Text(), nullable=False),
    sa.Column("dedupe_policy", sa.Text(), nullable=False),
    sa.Column("last_run_at", sa.DateTime(timezone=True)),
    sa.Column("next_run_at", sa.DateTime(timezone=True)),
    sa.Column("last_result", sa.Text()),
    sa.Column("last_error", sa.Text()),
    sa.Column("lease_owner", sa.Text()),
    sa.Column("lease_token", sa.Uuid()),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("active_run_id", sa.Uuid()),
    sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "registry_id",
        "source_id",
        name="uq_crawler_source_registry_source",
    ),
    sa.CheckConstraint(
        "source_id ~ '^[a-z][a-z0-9-]{0,63}$'",
        name="source_id_format",
    ),
    sa.CheckConstraint("btrim(adapter) <> ''", name="adapter_nonempty"),
    sa.CheckConstraint("btrim(input_artifact) <> ''", name="input_artifact_nonempty"),
    sa.CheckConstraint("btrim(output_dir) <> ''", name="output_dir_nonempty"),
    sa.CheckConstraint(
        "command_sha256 ~ '^[0-9a-f]{64}$'",
        name="command_sha256_format",
    ),
    sa.CheckConstraint(
        "source_sha256 ~ '^[0-9a-f]{64}$'",
        name="source_sha256_format",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(dependency_source_ids) = 'array'",
        name="dependency_source_ids_array",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(dependency_artifacts) = 'array'",
        name="dependency_artifacts_array",
    ),
    sa.CheckConstraint("jsonb_typeof(source_json) = 'object'", name="source_json_object"),
    sa.CheckConstraint("jsonb_typeof(budget_json) = 'object'", name="budget_json_object"),
    sa.CheckConstraint(
        "jsonb_typeof(rate_limit_json) = 'object'",
        name="rate_limit_json_object",
    ),
    sa.CheckConstraint(
        "careerops.crawler_source_controls_are_safe(adapter, budget_json, rate_limit_json)",
        name="controls_adapter_safe",
    ),
    sa.CheckConstraint(
        "cursor IS NULL OR char_length(cursor) <= 160",
        name="cursor_length",
    ),
    sa.CheckConstraint(
        "cadence_seconds BETWEEN 1 AND 604800",
        name="cadence_seconds_bounds",
    ),
    sa.CheckConstraint("retry_rounds BETWEEN 0 AND 10", name="retry_rounds_bounds"),
    sa.CheckConstraint(
        "adapter IN ('recruitment.sitemap_discovery', 'recruitment.commoncrawl_discovery', "
        "'recruitment.public_ats_feed', 'recruitment.page_collection')",
        name="adapter_allowlist",
    ),
    sa.CheckConstraint(
        "robots_terms_policy IN ('respect', 'review_required', 'blocked')",
        name="robots_terms_policy_values",
    ),
    sa.CheckConstraint(
        "canonical_ingestion_policy IN ('canonical_job_ingestion', 'dedupe_only')",
        name="canonical_ingestion_policy_values",
    ),
    sa.CheckConstraint(
        "canonical_ingestion_policy <> 'canonical_job_ingestion' "
        "OR adapter = 'recruitment.public_ats_feed'",
        name="canonical_ingestion_adapter",
    ),
    sa.CheckConstraint(
        "provenance_policy IN ('preserve', 'compact')",
        name="provenance_policy_values",
    ),
    sa.CheckConstraint(
        "dedupe_policy IN ('hash', 'url', 'hybrid')",
        name="dedupe_policy_values",
    ),
    sa.CheckConstraint(
        "input_artifact ~ '^[A-Za-z0-9._/-]+$' "
        "AND left(input_artifact, 1) <> '/' "
        "AND input_artifact !~ '(^|/)\\.\\.(/|$)'",
        name="input_artifact_safe_relative",
    ),
    sa.CheckConstraint(
        "output_dir ~ '^[A-Za-z0-9._/-]+$' "
        "AND left(output_dir, 1) <> '/' "
        "AND output_dir !~ '(^|/)\\.\\.(/|$)'",
        name="output_dir_safe_relative",
    ),
    sa.CheckConstraint(
        "next_run_at IS NULL OR last_run_at IS NULL OR next_run_at >= last_run_at",
        name="schedule_state_temporal_order",
    ),
    sa.CheckConstraint(
        "attempt_count >= 0 AND consecutive_failures >= 0",
        name="attempts_nonnegative",
    ),
    sa.CheckConstraint(
        "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL "
        "AND active_run_id IS NULL) OR "
        "(lease_owner IS NOT NULL AND btrim(lease_owner) <> '' "
        "AND lease_token IS NOT NULL AND lease_until IS NOT NULL AND active_run_id IS NOT NULL)",
        name="lease_state_consistent",
    ),
)

sa.Index(
    "ix_crawler_source_registry_sources_due",
    crawler_source_registry_sources.c.enabled,
    crawler_source_registry_sources.c.next_run_at,
    crawler_source_registry_sources.c.lease_until,
    crawler_source_registry_sources.c.source_id,
    postgresql_where=sa.and_(
        crawler_source_registry_sources.c.enabled.is_(True),
        crawler_source_registry_sources.c.robots_terms_policy == "respect",
    ),
)

crawler_source_runs = sa.Table(
    "crawler_source_runs",
    metadata,
    sa.Column("event_id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "source_row_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_registry_sources.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("registry_id", sa.Uuid(), nullable=False),
    sa.Column("source_id", sa.Text(), nullable=False),
    sa.Column("worker_id", sa.Text(), nullable=False),
    sa.Column("lease_token", sa.Uuid(), nullable=False),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column("cursor_before", sa.Text()),
    sa.Column("cursor_after", sa.Text()),
    sa.Column("result", sa.Text()),
    sa.Column("error", sa.Text()),
    sa.Column("output_manifest_sha256", sa.String(64)),
    sa.Column("attempt_number", sa.Integer(), nullable=False),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "event_kind IN ('claimed', 'succeeded', 'failed', 'expired')",
        name="event_kind_values",
    ),
    sa.CheckConstraint(
        "btrim(source_id) <> '' AND btrim(worker_id) <> ''",
        name="identifiers_nonempty",
    ),
    sa.CheckConstraint("attempt_number >= 0", name="attempt_number_nonnegative"),
    sa.CheckConstraint(
        "output_manifest_sha256 IS NULL OR output_manifest_sha256 ~ '^[0-9a-f]{64}$'",
        name="output_manifest_sha256_format",
    ),
    sa.CheckConstraint(
        "(event_kind = 'claimed' AND completed_at IS NULL) OR "
        "(event_kind IN ('succeeded', 'failed', 'expired') AND completed_at IS NOT NULL)",
        name="completion_event_state",
    ),
    sa.CheckConstraint(
        "NOT (event_kind IN ('failed', 'expired') AND cursor_after IS NOT NULL)",
        name="failed_cursor_after_null",
    ),
)

sa.Index(
    "ix_crawler_source_runs_run_id",
    crawler_source_runs.c.run_id,
    crawler_source_runs.c.event_kind,
    crawler_source_runs.c.created_at,
)

crawler_job_deduplication_keys = sa.Table(
    "crawler_job_deduplication_keys",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("dedupe_policy", sa.Text(), nullable=False),
    sa.Column("dedupe_key_sha256", sa.String(64), nullable=False),
    sa.Column(
        "company_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.companies.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("rule", sa.Text(), nullable=False),
    sa.Column("algorithm_version", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "dedupe_policy",
        "dedupe_key_sha256",
        name="uq_crawler_job_deduplication_keys_policy_key",
    ),
    sa.CheckConstraint("dedupe_policy IN ('hash', 'url', 'hybrid')", name="policy_values"),
    sa.CheckConstraint(
        "dedupe_key_sha256 ~ '^[0-9a-f]{64}$'",
        name="key_format",
    ),
    sa.CheckConstraint(
        "btrim(rule) <> '' AND rule <> 'semantic_similarity_only' "
        "AND btrim(algorithm_version) <> ''",
        name="rule_nonsemantic",
    ),
)

crawler_job_ingestion_evidence = sa.Table(
    "crawler_job_ingestion_evidence",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column(
        "source_row_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_registry_sources.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "run_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_runs.event_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "job_posting_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_posting_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("source_url", sa.Text(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("parser_version", sa.Text(), nullable=False),
    sa.Column("dedupe_policy", sa.Text(), nullable=False),
    sa.Column("dedupe_rule", sa.Text(), nullable=False),
    sa.Column("dedupe_key_sha256", sa.String(64), nullable=False),
    sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "run_id",
        "job_posting_version_id",
        "dedupe_key_sha256",
        name="uq_crawler_job_ingestion_evidence_run_version_key",
    ),
    sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    sa.CheckConstraint(
        "dedupe_key_sha256 ~ '^[0-9a-f]{64}$'",
        name="dedupe_key_sha256_format",
    ),
    sa.CheckConstraint("dedupe_policy IN ('hash', 'url', 'hybrid')", name="policy_values"),
    sa.CheckConstraint(
        "btrim(source_url) <> '' AND btrim(parser_version) <> '' "
        "AND btrim(dedupe_rule) <> '' AND dedupe_rule <> 'semantic_similarity_only'",
        name="nonempty_nonsemantic",
    ),
    sa.CheckConstraint("jsonb_typeof(provenance_json) = 'object'", name="provenance_object"),
)

sa.Index(
    "ix_crawler_job_ingestion_evidence_run",
    crawler_job_ingestion_evidence.c.run_id,
    crawler_job_ingestion_evidence.c.source_row_id,
)

goal_runs = sa.Table(
    "goal_runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("goal_kind", sa.Text(), nullable=False),
    sa.Column("goal", sa.Text(), nullable=False),
    sa.Column("context_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "registry_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_registries.id", ondelete="RESTRICT"),
    ),
    sa.Column("source_id", sa.Text()),
    sa.Column("max_records", sa.Integer(), nullable=False),
    sa.Column("temporal_workflow_id", sa.Text()),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("phase", sa.Text(), nullable=False),
    sa.Column("version", sa.BigInteger(), nullable=False),
    sa.Column("fencing_token", sa.Uuid(), nullable=False),
    sa.Column("checkpoint_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column("review_item_id", sa.Uuid()),
    sa.Column("review_snapshot_sha256", sa.String(64)),
    sa.Column("review_decision", sa.Text()),
    sa.Column("review_kind", sa.Text()),
    sa.Column("review_payload", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("last_error_code", sa.Text()),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("failed_at", sa.DateTime(timezone=True)),
    sa.Column("cancelled_at", sa.DateTime(timezone=True)),
    sa.Column("blocked_at", sa.DateTime(timezone=True)),
    sa.Column("reconciliation_required_at", sa.DateTime(timezone=True)),
    sa.Column("rejected_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "actor_user_id",
        "idempotency_key",
        name="uq_goal_runs_actor_idempotency_key",
    ),
    sa.CheckConstraint(
        "status IN ('starting', 'running', 'waiting_review', 'blocked', "
        "'reconciliation_required', 'completed', 'failed', 'cancelled', 'rejected')",
        name="status_values",
    ),
    sa.CheckConstraint(
        "phase IN ('initializing', 'selecting_source', 'claiming_source', 'discovery', "
        "'complete_source', 'canonical_ingest', 'matching', 'draft_preparation', "
        "'review', 'dispatch', 'reconciliation', 'completed', 'blocked', 'failed', "
        "'cancelled', 'rejected')",
        name="phase_values",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("max_records > 0", name="max_records_positive"),
    sa.CheckConstraint("jsonb_typeof(context_json) = 'object'", name="context_json_object"),
    sa.CheckConstraint("jsonb_typeof(checkpoint_json) = 'object'", name="checkpoint_json_object"),
    sa.CheckConstraint(
        "review_decision IS NULL OR review_decision IN ('approve', 'reject')",
        name="review_decision_values",
    ),
)

goal_run_checkpoints = sa.Table(
    "goal_run_checkpoints",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_version", sa.BigInteger(), nullable=False),
    sa.Column("phase", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("outcome", sa.Text(), nullable=False),
    sa.Column("checkpoint_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("checkpoint_sha256", sa.String(64), nullable=False),
    sa.Column("last_error_code", sa.Text()),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("fencing_token", sa.Uuid(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "goal_run_id",
        "idempotency_key",
        name="uq_goal_run_checkpoints_goal_run_id_idempotency",
    ),
    sa.CheckConstraint("run_version > 0", name="run_version_positive"),
    sa.CheckConstraint(
        "outcome IN ('progress', 'waiting_review', 'blocked', "
        "'reconciliation_required', 'finalize')",
        name="outcome_values",
    ),
    sa.CheckConstraint("jsonb_typeof(checkpoint_json) = 'object'", name="checkpoint_json_object"),
    sa.CheckConstraint("checkpoint_sha256 ~ '^[a-f0-9]{64}$'", name="checkpoint_sha256_format"),
)

goal_run_events = sa.Table(
    "goal_run_events",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("event_kind", sa.Text(), nullable=False),
    sa.Column("from_status", sa.Text()),
    sa.Column("to_status", sa.Text(), nullable=False),
    sa.Column("from_phase", sa.Text()),
    sa.Column("to_phase", sa.Text(), nullable=False),
    sa.Column("version", sa.BigInteger(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("fencing_token", sa.Uuid(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text()),
    sa.Column("event_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "event_kind IN ('created', 'checkpoint_recorded', 'review_requested', "
        "'review_decided', 'cancelled', 'resumed')",
        name="event_kind_values",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("jsonb_typeof(event_json) = 'object'", name="event_json_object"),
)

goal_run_review_items = sa.Table(
    "goal_run_review_items",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_version", sa.BigInteger(), nullable=False),
    sa.Column("review_kind", sa.Text(), nullable=False),
    sa.Column("review_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("snapshot_sha256", sa.String(64), nullable=False),
    sa.Column("snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "goal_run_id",
        "idempotency_key",
        name="uq_goal_run_review_items_goal_run_id_idempotency",
    ),
    sa.CheckConstraint("run_version > 0", name="run_version_positive"),
    sa.CheckConstraint("jsonb_typeof(review_payload) = 'object'", name="payload_object"),
    sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(snapshot_json) = 'object'", name="snapshot_object"),
)

goal_run_review_decisions = sa.Table(
    "goal_run_review_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "review_item_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_run_review_items.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("run_version", sa.BigInteger(), nullable=False),
    sa.Column("decision", sa.Text(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column("snapshot_sha256", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "goal_run_id",
        "idempotency_key",
        name="uq_goal_run_review_decisions_goal_run_id_idempotency",
    ),
    sa.CheckConstraint("run_version > 0", name="run_version_positive"),
    sa.CheckConstraint("decision IN ('approve', 'reject')", name="decision_values"),
    sa.CheckConstraint(
        "btrim(reason) <> '' AND char_length(reason) <= 4000",
        name="reason_bounded",
    ),
    sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
)

goal_run_command_receipts = sa.Table(
    "goal_run_command_receipts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("command_kind", sa.Text(), nullable=False),
    sa.Column("idempotency_key", sa.Text(), nullable=False),
    sa.Column("request_sha256", sa.String(64), nullable=False),
    sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "actor_user_id",
        "goal_run_id",
        "command_kind",
        "idempotency_key",
        name="uq_goal_run_command_receipts_identity",
    ),
    sa.CheckConstraint(
        "command_kind IN ('create', 'checkpoint', 'request_review', "
        "'record_review_decision', 'cancel', 'resume')",
        name="command_kind_values",
    ),
    sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
    sa.CheckConstraint("jsonb_typeof(response_json) = 'object'", name="response_json_object"),
)

goal_run_gmail_compositions = sa.Table(
    "goal_run_gmail_compositions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "goal_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.goal_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "source_row_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_registry_sources.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("crawler_run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "crawler_run_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawler_source_runs.event_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "job_posting_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_postings.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "job_posting_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_posting_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "gmail_send_account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.gmail_send_accounts.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "campaign_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.autopilot_campaigns.id", ondelete="RESTRICT"),
    ),
    sa.Column("grant_version_id", sa.Uuid()),
    sa.Column(
        "release_qualification_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.release_qualifications.id", ondelete="RESTRICT"),
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
    ),
    sa.Column("payload_version_id", sa.Uuid()),
    sa.Column("policy_decision_id", sa.Uuid()),
    sa.Column(
        "approval_request_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.approval_requests.id", ondelete="RESTRICT"),
    ),
    sa.Column("authorization_id", sa.Uuid()),
    sa.Column(
        "outbox_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.outbox_events.id", ondelete="RESTRICT"),
    ),
    sa.Column("match_score", sa.Numeric(6, 5), nullable=False),
    sa.Column("match_reason", sa.Text(), nullable=False),
    sa.Column("recipient_snapshot_sha256", sa.String(64)),
    sa.Column("subject_sha256", sa.String(64)),
    sa.Column("body_sha256", sa.String(64)),
    sa.Column("payload_hash", sa.String(64)),
    sa.Column("attachment_manifest_sha256", sa.String(64)),
    sa.Column("approval_snapshot_sha256", sa.String(64)),
    sa.Column("review_evidence_sha256", sa.String(64)),
    sa.Column("reservation_key", sa.Text()),
    sa.Column("reconciliation_key", sa.Text()),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("match_snapshot_sha256", sa.String(64), nullable=False),
    sa.Column("draft_idempotency_key", sa.Text()),
    sa.Column("dispatch_idempotency_key", sa.Text()),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "matched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("draft_prepared_at", sa.DateTime(timezone=True)),
    sa.Column("dispatched_at", sa.DateTime(timezone=True)),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "payload_hash"],
        [
            f"{DATABASE_SCHEMA}.action_payload_versions.action_intent_id",
            f"{DATABASE_SCHEMA}.action_payload_versions.id",
            f"{DATABASE_SCHEMA}.action_payload_versions.payload_hash",
        ],
        name="fk_goal_run_gmail_compositions_payload_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["campaign_id", "grant_version_id"],
        [
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.campaign_id",
            f"{DATABASE_SCHEMA}.autopilot_grant_versions.id",
        ],
        name="fk_goal_run_gmail_compositions_grant_identity",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["action_intent_id", "payload_version_id", "policy_decision_id"],
        [
            f"{DATABASE_SCHEMA}.policy_decisions.action_intent_id",
            f"{DATABASE_SCHEMA}.policy_decisions.payload_version_id",
            f"{DATABASE_SCHEMA}.policy_decisions.id",
        ],
        name="fk_goal_run_gmail_compositions_policy_identity",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint("goal_run_id", name="uq_goal_run_gmail_compositions_goal_run"),
    sa.UniqueConstraint(
        "action_intent_id",
        "payload_hash",
        name="uq_goal_run_gmail_compositions_intent_payload",
    ),
    sa.CheckConstraint("match_score >= 0 AND match_score <= 1", name="match_score_bounds"),
    sa.CheckConstraint("btrim(match_reason) <> ''", name="match_reason_nonempty"),
    sa.CheckConstraint(
        "state IN ('matched', 'draft_prepared', 'dispatch_enqueued')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "match_snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="match_snapshot_sha256_format"
    ),
    sa.CheckConstraint(
        "payload_hash IS NULL OR payload_hash ~ '^[a-f0-9]{64}$'",
        name="payload_hash_format",
    ),
    sa.CheckConstraint(
        "approval_snapshot_sha256 IS NULL OR approval_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
        name="approval_snapshot_sha256_format",
    ),
    sa.CheckConstraint(
        "(state = 'matched' AND action_intent_id IS NULL AND outbox_event_id IS NULL) OR "
        "(state = 'draft_prepared' AND action_intent_id IS NOT NULL "
        "AND payload_version_id IS NOT NULL AND payload_hash IS NOT NULL "
        "AND approval_request_id IS NOT NULL AND outbox_event_id IS NULL) OR "
        "(state = 'dispatch_enqueued' AND action_intent_id IS NOT NULL "
        "AND payload_version_id IS NOT NULL AND payload_hash IS NOT NULL "
        "AND approval_request_id IS NOT NULL AND authorization_id IS NOT NULL "
        "AND outbox_event_id IS NOT NULL)",
        name="state_required_fields",
    ),
)

sa.Index(
    "ix_goal_run_gmail_compositions_actor_updated_at",
    goal_run_gmail_compositions.c.actor_user_id,
    goal_run_gmail_compositions.c.updated_at,
)

sa.Index(
    "ix_goal_run_gmail_compositions_outbox_event",
    goal_run_gmail_compositions.c.outbox_event_id,
)

sa.Index(
    "ix_goal_runs_actor_updated_at",
    goal_runs.c.actor_user_id,
    goal_runs.c.updated_at,
)

sa.Index(
    "ix_goal_run_events_goal_run_sequence",
    goal_run_events.c.goal_run_id,
    goal_run_events.c.sequence,
)

sa.Index(
    "ix_goal_run_checkpoints_goal_run_version",
    goal_run_checkpoints.c.goal_run_id,
    goal_run_checkpoints.c.run_version,
)

sa.Index(
    "ix_goal_run_review_items_goal_run_created_at",
    goal_run_review_items.c.goal_run_id,
    goal_run_review_items.c.created_at,
)

sa.Index(
    "ix_goal_run_review_decisions_goal_run_created_at",
    goal_run_review_decisions.c.goal_run_id,
    goal_run_review_decisions.c.created_at,
)

autopilot_kill_switch_events = sa.Table(
    "autopilot_kill_switch_events",
    metadata,
    sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("scope_type", sa.String(16), nullable=False),
    sa.Column("campaign_id", sa.Uuid()),
    sa.Column("provider", sa.String(24)),
    sa.Column("active", sa.Boolean(), nullable=False),
    sa.Column("reason", sa.Text(), nullable=False),
    sa.Column(
        "actor_user_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.console_users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
    sa.Column("trace_id", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "scope_type IN ('global', 'campaign', 'provider')",
        name="scope_type_values",
    ),
    sa.CheckConstraint(
        "(scope_type = 'global' AND campaign_id IS NULL AND provider IS NULL) OR "
        "(scope_type = 'campaign' AND campaign_id IS NOT NULL AND provider IS NULL) OR "
        "(scope_type = 'provider' AND campaign_id IS NULL AND provider IS NOT NULL)",
        name="scope_binding",
    ),
    sa.CheckConstraint(
        "btrim(reason) <> '' AND btrim(idempotency_key) <> '' AND btrim(trace_id) <> ''",
        name="text_nonempty",
    ),
)

sa.Index(
    "ix_autopilot_kill_switch_events_latest",
    autopilot_kill_switch_events.c.scope_type,
    autopilot_kill_switch_events.c.campaign_id,
    autopilot_kill_switch_events.c.provider,
    autopilot_kill_switch_events.c.sequence.desc(),
)

sa.Index(
    "ix_release_qualifications_binding_created_at",
    release_qualifications.c.capability,
    release_qualifications.c.action_name,
    release_qualifications.c.rollout_mode,
    release_qualifications.c.provider,
    release_qualifications.c.adapter_id,
    release_qualifications.c.created_at,
)

sa.Index(
    "ix_release_qualification_evidence_qualification_created_at",
    release_qualification_evidence.c.qualification_id,
    release_qualification_evidence.c.created_at,
)

sa.Index(
    "ix_release_qualification_decisions_qualification_sequence",
    release_qualification_decisions.c.qualification_id,
    release_qualification_decisions.c.sequence,
)

sa.Index(
    "ix_crawler_execution_requests_owner_created_at",
    crawler_execution_requests.c.owner_user_id,
    crawler_execution_requests.c.created_at,
)

sa.Index(
    "ix_crawler_execution_requests_expires_at",
    crawler_execution_requests.c.expires_at,
)

sa.Index(
    "ix_crawler_execution_approvals_decision_created_at",
    crawler_execution_approvals.c.decision,
    crawler_execution_approvals.c.created_at,
)

APPEND_ONLY_TABLES = (
    "action_payload_versions",
    "application_events",
    "audit_events",
    "autopilot_campaigns",
    "autopilot_cap_reservations",
    "autopilot_grant_revocations",
    "autopilot_grant_versions",
    "autopilot_intent_authorizations",
    "autopilot_review_items",
    "candidate_material_bundles",
    "candidate_material_versions",
    "candidate_profile_decisions",
    "candidate_profile_versions",
    "crawler_execution_approvals",
    "crawler_execution_dispatches",
    "crawler_execution_requests",
    "crawler_execution_results",
    "crawler_job_deduplication_keys",
    "crawler_job_ingestion_evidence",
    "crawler_source_runs",
    "crawler_source_registries",
    "evidence_records",
    "goal_run_checkpoints",
    "goal_run_command_receipts",
    "goal_run_events",
    "goal_run_review_decisions",
    "goal_run_review_items",
    "gmail_command_receipts",
    "gmail_message_signals",
    "gmail_send_command_receipts",
    "gmail_send_reservations",
    "gmail_send_review_evidence",
    "greenhouse_submit_command_receipts",
    "greenhouse_submit_reconciliation_evidence",
    "greenhouse_submit_reconciliation_reviews",
    "greenhouse_submit_review_evidence",
    "gmail_signal_proposals",
    "gmail_signal_review_decisions",
    "job_merge_decisions",
    "job_posting_versions",
    "policy_decisions",
    "provider_receipts",
    "autopilot_kill_switch_events",
    "release_qualifications",
    "release_qualification_evidence",
    "release_qualification_decisions",
)
