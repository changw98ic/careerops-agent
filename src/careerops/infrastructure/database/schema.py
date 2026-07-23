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
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
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

evidence_items = sa.Table(
    "evidence_items",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("name", sa.Text(), nullable=False),
    sa.Column("description", sa.Text(), server_default="", nullable=False),
    sa.Column("repository", sa.Text(), server_default="", nullable=False),
    sa.Column("commit_sha", sa.Text(), server_default="", nullable=False),
    sa.Column("path", sa.Text(), server_default="", nullable=False),
    sa.Column("symbol", sa.Text(), server_default="", nullable=False),
    sa.Column("content_hash", sa.String(64), server_default="", nullable=False),
    sa.Column("source_url", sa.Text(), server_default="", nullable=False),
    sa.Column("verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "kind IN ('skill', 'project', 'experience', 'certification', 'education')",
        name="kind_values",
    ),
    sa.UniqueConstraint(
        "candidate_id",
        "repository",
        "commit_sha",
        "path",
        "symbol",
        "content_hash",
        name="uq_evidence_items_idempotency",
    ),
)

match_results = sa.Table(
    "match_results",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("tier", sa.String(32), nullable=False),
    sa.Column("overall_score", sa.Numeric(5, 4), nullable=False),
    sa.Column(
        "requirement_matches",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("geographic_blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("remote_verdict", sa.String(32), server_default="unknown", nullable=False),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column("input_hash", sa.String(64), server_default="", nullable=False),
    sa.Column("output_hash", sa.String(64), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "tier IN ('apply_now', 'strong_candidate', 'worth_exploring', "
        "'stretch', 'not_recommended', 'blocked')",
        name="tier_values",
    ),
    sa.CheckConstraint(
        "remote_verdict IN ('eligible', 'not_eligible', 'review_required', 'unknown')",
        name="remote_verdict_values",
    ),
    sa.CheckConstraint("overall_score >= 0 AND overall_score <= 1", name="score_range"),
)

sa.Index(
    "ix_match_results_candidate_job",
    match_results.c.candidate_id,
    match_results.c.canonical_job_id,
)

compensation_records = sa.Table(
    "compensation_records",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("currency", sa.String(8), server_default="", nullable=False),
    sa.Column("amount_min", sa.Numeric(14, 2)),
    sa.Column("amount_max", sa.Numeric(14, 2)),
    sa.Column("period", sa.String(16), server_default="", nullable=False),
    sa.Column("fx_rate", sa.Numeric(14, 8)),
    sa.Column("fx_effective_date", sa.Date()),
    sa.Column("normalized_amount_min", sa.Numeric(14, 2)),
    sa.Column("normalized_amount_max", sa.Numeric(14, 2)),
    sa.Column("normalized_currency", sa.String(8), server_default="CNY", nullable=False),
    sa.Column("score", sa.String(24), server_default="unknown", nullable=False),
    sa.Column("source_text", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "score IN ('unknown', 'high', 'competitive', 'moderate', 'below_market', 'neutral')",
        name="score_values",
    ),
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
    sa.Column("decision", sa.String(24), nullable=False),
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
        "decision IN ('deny', 'require_approval', 'allow')",
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
        "event_type IN ('workflow_signal', 'internal_notification', 'provider_write')",
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

# ---------------------------------------------------------------------------
# M3: Contacts, Applications, Resume Versions, Packages, Follow-ups
# ---------------------------------------------------------------------------

contacts = sa.Table(
    "contacts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "company_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.companies.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("email", sa.Text(), nullable=False),
    sa.Column("name", sa.Text(), server_default="", nullable=False),
    sa.Column("role", sa.Text(), server_default="", nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("source_url", sa.Text(), nullable=False),
    sa.Column("source_text", sa.Text(), server_default="", nullable=False),
    sa.Column("publicly_listed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    sa.Column("domain_match", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    sa.Column("confidence", sa.String(16), server_default="high", nullable=False),
    sa.Column(
        "allowed_actions",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[\"display\"]'::jsonb"),
        nullable=False,
    ),
    sa.Column("verified_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("company_id", "email", name="uq_contacts_company_email"),
    sa.CheckConstraint(
        "source IN ('job_page', 'careers_page', 'ats_listing', "
        "'official_recruiting_page', 'established_thread')",
        name="source_values",
    ),
    sa.CheckConstraint(
        "confidence IN ('high', 'medium', 'low')",
        name="confidence_values",
    ),
    sa.CheckConstraint("publicly_listed = true", name="publicly_listed_required"),
)

applications = sa.Table(
    "applications",
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
        nullable=False,
    ),
    sa.Column("state", sa.String(24), server_default="favorited", nullable=False),
    sa.Column("apply_url", sa.Text(), server_default="", nullable=False),
    sa.Column("submitted_at", sa.DateTime(timezone=True)),
    sa.Column("follow_up_due_at", sa.DateTime(timezone=True)),
    sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("candidate_id", "canonical_job_id", name="uq_applications_candidate_job"),
    sa.CheckConstraint(
        "state IN ('favorited', 'ignored', 'preparing', 'submitted', "
        "'interviewing', 'offer', 'rejected', 'withdrawn', 'on_hold')",
        name="state_values",
    ),
    sa.CheckConstraint("version > 0", name="version_positive"),
)

sa.Index(
    "ix_applications_candidate_state",
    applications.c.candidate_id,
    applications.c.state,
)

application_lifecycle_events = sa.Table(
    "application_lifecycle_events",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("from_state", sa.String(24)),
    sa.Column("to_state", sa.String(24)),
    sa.Column("source", sa.String(24), nullable=False),
    sa.Column("actor_id", sa.Text(), server_default="", nullable=False),
    sa.Column("note", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "event_data",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "event_type IN ('created', 'state_changed', 'submitted_manually', "
        "'note_added', 'package_attached', 'follow_up_scheduled', "
        "'follow_up_cancelled', 'follow_up_snoozed', 'follow_up_rescheduled')",
        name="event_type_values",
    ),
    sa.CheckConstraint(
        "source IN ('user', 'system', 'workflow')",
        name="source_values",
    ),
)

sa.Index(
    "ix_application_lifecycle_events_app_occurred",
    application_lifecycle_events.c.application_id,
    application_lifecycle_events.c.occurred_at,
)

resume_versions = sa.Table(
    "resume_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version_number", sa.Integer(), nullable=False),
    sa.Column("file_reference", sa.Text(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("target_type", sa.Text(), server_default="general", nullable=False),
    sa.Column("human_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "candidate_id", "version_number", name="uq_resume_versions_candidate_version"
    ),
    sa.CheckConstraint("version_number > 0", name="version_number_positive"),
    sa.CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
)

application_packages = sa.Table(
    "application_packages",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    sa.Column(
        "resume_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.resume_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("cover_letter_text", sa.Text(), server_default="", nullable=False),
    sa.Column("notes", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "answers",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "claims",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "approval_state IN ('draft', 'pending_review', 'approved', 'rejected')",
        name="approval_state_values",
    ),
)

follow_up_reminders = sa.Table(
    "follow_up_reminders",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("rule_version", sa.Text(), nullable=False),
    sa.Column("state", sa.String(16), server_default="active", nullable=False),
    sa.Column("due_at", sa.DateTime(timezone=True)),
    sa.Column("snoozed_until", sa.DateTime(timezone=True)),
    sa.Column("cancelled_reason", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "state IN ('active', 'snoozed', 'cancelled', 'completed')",
        name="state_values",
    ),
)

sa.Index(
    "ix_follow_up_reminders_app_rule_active",
    follow_up_reminders.c.application_id,
    follow_up_reminders.c.rule_version,
    postgresql_where=sa.and_(
        follow_up_reminders.c.state == "active",
    ),
    unique=True,
)

# ---------------------------------------------------------------------------
# M4: Gmail read-only sync, classification, internal drafts, approval
# ---------------------------------------------------------------------------

email_accounts = sa.Table(
    "email_accounts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("email_address", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "credential_reference_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.oauth_credential_references.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("status", sa.String(16), server_default="active", nullable=False),
    sa.Column("history_id", sa.Text(), server_default="", nullable=False),
    sa.Column("watch_expiration", sa.DateTime(timezone=True)),
    sa.Column("last_sync_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('active', 'revoked', 'error')",
        name="status_values",
    ),
)

email_threads = sa.Table(
    "email_threads",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider_thread_id", sa.Text(), nullable=False),
    sa.Column("subject", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "account_id", "provider_thread_id", name="uq_email_threads_account_provider"
    ),
)

email_messages = sa.Table(
    "email_messages",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "thread_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_threads.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider_message_id", sa.Text(), nullable=False),
    sa.Column("category", sa.String(24), server_default="unknown", nullable=False),
    sa.Column("state", sa.String(24), server_default="synced", nullable=False),
    sa.Column("sender_email", sa.Text(), server_default="", nullable=False),
    sa.Column("sender_name", sa.Text(), server_default="", nullable=False),
    sa.Column("subject", sa.Text(), server_default="", nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True)),
    sa.Column("snippet", sa.Text(), server_default="", nullable=False),
    sa.Column("body_persisted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "account_id", "provider_message_id", name="uq_email_messages_account_provider"
    ),
    sa.CheckConstraint(
        "category IN ('recruitment', 'non_recruitment', 'unknown')",
        name="category_values",
    ),
    sa.CheckConstraint(
        "state IN ('synced', 'classified', 'extracted', 'drafted', 'archived')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "(category = 'non_recruitment' AND body_persisted = false) OR "
        "category <> 'non_recruitment'",
        name="non_recruitment_body_not_persisted",
    ),
)

sa.Index(
    "ix_email_messages_thread_received",
    email_messages.c.thread_id,
    email_messages.c.received_at,
)

email_extractions = sa.Table(
    "email_extractions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "message_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_messages.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("extraction_type", sa.String(32), nullable=False),
    sa.Column(
        "extracted_data",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
    sa.Column("model_version", sa.Text(), server_default="", nullable=False),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
)

reply_drafts = sa.Table(
    "reply_drafts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "message_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_messages.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "thread_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_threads.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
    ),
    sa.Column("status", sa.String(24), server_default="draft", nullable=False),
    sa.Column("to_address", sa.Text(), server_default="", nullable=False),
    sa.Column("subject", sa.Text(), server_default="", nullable=False),
    sa.Column("body_text", sa.Text(), server_default="", nullable=False),
    sa.Column("references_header", sa.Text(), server_default="", nullable=False),
    sa.Column("in_reply_to_header", sa.Text(), server_default="", nullable=False),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("approved_at", sa.DateTime(timezone=True)),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('draft', 'pending_approval', 'approved', 'rejected', 'expired')",
        name="status_values",
    ),
    sa.CheckConstraint("char_length(payload_hash) = 64", name="payload_hash_length"),
    sa.CheckConstraint(
        "(status = 'approved' AND approved_at IS NOT NULL) OR "
        "(status <> 'approved' AND approved_at IS NULL)",
        name="approval_timestamp_consistent",
    ),
)

attachment_quarantine = sa.Table(
    "attachment_quarantine",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "message_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_messages.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("filename", sa.Text(), server_default="", nullable=False),
    sa.Column("declared_mime", sa.Text(), server_default="", nullable=False),
    sa.Column("detected_mime", sa.Text(), server_default="", nullable=False),
    sa.Column("byte_size", sa.BigInteger(), server_default="0", nullable=False),
    sa.Column("status", sa.String(16), server_default="quarantined", nullable=False),
    sa.Column("deny_reason", sa.Text(), server_default="", nullable=False),
    sa.Column("content_hash", sa.String(64), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "status IN ('quarantined', 'cleared', 'denied', 'expired')",
        name="status_values",
    ),
    sa.CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
    sa.CheckConstraint(
        "(status IN ('cleared', 'denied', 'expired') AND resolved_at IS NOT NULL) OR "
        "(status = 'quarantined' AND resolved_at IS NULL)",
        name="resolution_timestamp_consistent",
    ),
)

# ---------------------------------------------------------------------------
# M5B: Calendar scheduling and interview management
# ---------------------------------------------------------------------------

schedule_proposals = sa.Table(
    "schedule_proposals",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "candidate_slots",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("selected_slot", postgresql.JSONB(astext_type=sa.Text())),
    sa.Column("status", sa.String(24), server_default="draft", nullable=False),
    sa.Column("rules_version", sa.Text(), nullable=False),
    sa.Column(
        "scheduling_rules",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "calendar_ids_checked",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("recruiter_confirmed_at", sa.DateTime(timezone=True)),
    sa.Column("user_action_at", sa.DateTime(timezone=True)),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="SET NULL"),
    ),
    sa.Column("payload_hash", sa.String(64)),
    sa.Column("freebusy_queried_at", sa.DateTime(timezone=True)),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("conflict_reason", sa.Text()),
    sa.Column("created_by", sa.Text(), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('draft', 'pending_recruiter', 'recruiter_confirmed', "
        "'pending_user_action', 'executing', 'confirmed', 'conflict_review', "
        "'cancelled', 'expired')",
        name="status_values",
    ),
    sa.CheckConstraint(
        "payload_hash IS NULL OR char_length(payload_hash) = 64",
        name="payload_hash_length",
    ),
    sa.CheckConstraint(
        "(status = 'confirmed' AND action_intent_id IS NOT NULL) OR status <> 'confirmed'",
        name="confirmed_requires_intent",
    ),
)

sa.Index(
    "ix_schedule_proposals_application_status",
    schedule_proposals.c.application_id,
    schedule_proposals.c.status,
)

calendar_events = sa.Table(
    "calendar_events",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "schedule_proposal_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider_event_id", sa.Text()),
    sa.Column("calendar_id", sa.Text(), nullable=False),
    sa.Column("reconciliation_key", sa.Text(), nullable=False, unique=True),
    sa.Column("title", sa.Text(), nullable=False),
    sa.Column(
        "slot_data",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("meeting_link", sa.Text(), server_default="", nullable=False),
    sa.Column("status", sa.String(24), server_default="pending", nullable=False),
    sa.Column("conflict_detected_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'created', 'conflict_detected', 'cancelled')",
        name="status_values",
    ),
    sa.CheckConstraint(
        "(status = 'conflict_detected' AND conflict_detected_at IS NOT NULL) OR "
        "status <> 'conflict_detected'",
        name="conflict_timestamp_consistent",
    ),
)

sa.Index(
    "ix_calendar_events_proposal_status",
    calendar_events.c.schedule_proposal_id,
    calendar_events.c.status,
)

interview_records = sa.Table(
    "interview_records",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "calendar_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.calendar_events.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "schedule_proposal_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("status", sa.String(24), server_default="scheduled", nullable=False),
    sa.Column("interviewer_name", sa.Text(), server_default="", nullable=False),
    sa.Column("interviewer_email", sa.Text(), server_default="", nullable=False),
    sa.Column("interview_type", sa.Text(), server_default="video", nullable=False),
    sa.Column("meeting_link", sa.Text(), server_default="", nullable=False),
    sa.Column("notes", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "evidence_summary",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('scheduled', 'completed', 'cancelled', 'rescheduled')",
        name="status_values",
    ),
)

sa.Index(
    "ix_interview_records_application_status",
    interview_records.c.application_id,
    interview_records.c.status,
)

conflict_reviews = sa.Table(
    "conflict_reviews",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "schedule_proposal_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.schedule_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "calendar_event_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.calendar_events.id", ondelete="SET NULL"),
    ),
    sa.Column("conflict_type", sa.String(32), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column("resolution", sa.Text()),
    sa.Column(
        "confirmation_email_blocked",
        sa.Boolean(),
        server_default=sa.text("true"),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "conflict_type IN ('freebusy_race', 'duplicate_event', 'buffer_violation', "
        "'reconciliation_failure')",
        name="conflict_type_values",
    ),
    sa.CheckConstraint(
        "confirmation_email_blocked = true",
        name="confirmation_email_always_blocked",
    ),
    sa.CheckConstraint(
        "(resolved_at IS NOT NULL AND resolution IS NOT NULL) OR "
        "(resolved_at IS NULL AND resolution IS NULL)",
        name="resolution_consistent",
    ),
)

sa.Index(
    "ix_conflict_reviews_proposal_unresolved",
    conflict_reviews.c.schedule_proposal_id,
    postgresql_where=conflict_reviews.c.resolved_at.is_(None),
)

# ---------------------------------------------------------------------------
# M6: Gmail Send through the M5A authorization chain
# ---------------------------------------------------------------------------

send_attempts = sa.Table(
    "send_attempts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("account_email", sa.Text(), nullable=False),
    sa.Column("recipient", sa.Text(), nullable=False),
    sa.Column("subject", sa.Text(), nullable=False),
    sa.Column("body_hash", sa.String(64), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("in_reply_to", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "references_list",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("category", sa.String(48), nullable=False),
    sa.Column("status", sa.String(32), server_default="pending", nullable=False),
    sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
    sa.Column("reconciliation_key", sa.Text(), nullable=False),
    sa.Column("provider_message_id", sa.Text()),
    sa.Column("error_code", sa.Text()),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'in_progress', 'succeeded', 'failed', 'reconciliation_required')",
        name="send_attempts_status_values",
    ),
    sa.CheckConstraint(
        "category IN ('delivery_confirmation', 'recruiter_contact_ack', "
        "'assessment_receipt_ack', 'confirmed_time_ack', 'thanks_no_questions', "
        "'scheduling_options', 'follow_up', 'resume_or_link', "
        "'work_authorization', 'deadline_commitment', "
        "'salary', 'offer', 'visa', 'relocation', 'tax', "
        "'background_check', 'identity_or_bank', 'withdrawal', 'unknown')",
        name="send_attempts_category_values",
    ),
    sa.CheckConstraint(
        "char_length(body_hash) = 64",
        name="send_attempts_body_hash_length",
    ),
    sa.CheckConstraint(
        "char_length(idempotency_key) = 64",
        name="send_attempts_idempotency_key_length",
    ),
)

sa.Index(
    "ix_send_attempts_intent_status",
    send_attempts.c.action_intent_id,
    send_attempts.c.status,
)

sa.Index(
    "ix_send_attempts_reconciliation_key",
    send_attempts.c.reconciliation_key,
)

send_receipts = sa.Table(
    "send_receipts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "send_attempt_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.send_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("provider_message_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("reconciliation_key", sa.Text(), nullable=False, unique=True),
    sa.Column("final_state", sa.String(24), nullable=False),
    sa.Column("provider_timestamp", sa.DateTime(timezone=True)),
    sa.Column(
        "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "final_state IN ('succeeded', 'failed', 'revoked')",
        name="send_receipts_final_state_values",
    ),
)

sa.Index(
    "ix_send_receipts_attempt",
    send_receipts.c.send_attempt_id,
)

reconciliation_records = sa.Table(
    "reconciliation_records",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "send_attempt_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.send_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "action_intent_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.action_intents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("reconciliation_key", sa.Text(), nullable=False),
    sa.Column("status", sa.String(32), server_default="pending", nullable=False),
    sa.Column("window_minutes", sa.Integer(), server_default="15", nullable=False),
    sa.Column(
        "attempts_within_window",
        sa.Integer(),
        server_default="0",
        nullable=False,
    ),
    sa.Column(
        "auto_retry_disabled",
        sa.Boolean(),
        server_default=sa.text("true"),
        nullable=False,
    ),
    sa.Column("escalated_at", sa.DateTime(timezone=True)),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column("resolution_note", sa.Text()),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'confirmed_sent', 'confirmed_not_sent', "
        "'ambiguous', 'escalated_manual')",
        name="reconciliation_records_status_values",
    ),
    sa.CheckConstraint(
        "auto_retry_disabled = true",
        name="reconciliation_auto_retry_always_disabled",
    ),
    sa.CheckConstraint(
        "window_minutes > 0",
        name="reconciliation_window_positive",
    ),
    sa.CheckConstraint(
        "(resolved_at IS NOT NULL AND resolution_note IS NOT NULL) OR "
        "(resolved_at IS NULL AND resolution_note IS NULL)",
        name="reconciliation_resolution_consistent",
    ),
)

sa.Index(
    "ix_reconciliation_records_intent_status",
    reconciliation_records.c.action_intent_id,
    reconciliation_records.c.status,
)

sa.Index(
    "ix_reconciliation_records_unresolved",
    reconciliation_records.c.status,
    postgresql_where=reconciliation_records.c.resolved_at.is_(None),
)

APPEND_ONLY_TABLES = (
    "action_payload_versions",
    "application_events",
    "application_lifecycle_events",
    "audit_events",
    "email_extractions",
    "evidence_records",
    "job_merge_decisions",
    "job_posting_versions",
    "policy_decisions",
    "provider_receipts",
)
