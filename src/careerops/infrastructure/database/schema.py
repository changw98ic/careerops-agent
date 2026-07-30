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
    sa.Column("executor_mode", sa.String(16), server_default="http", nullable=False),
    sa.Column("state", sa.String(24), server_default="pending_review", nullable=False),
    sa.Column("verified_at", sa.DateTime(timezone=True)),
    sa.Column("last_discovery_at", sa.DateTime(timezone=True)),
    # 4.2/4.3 (end-to-end-career-application-loop Section 4): additive source
    # control columns. Sources declare scope only; the crawl policy layer stays
    # authoritative (Iron Rule 6). ``enabled`` is the user on/off toggle;
    # ``state`` above is the operational lifecycle. ``last_run_metadata`` is a
    # bounded safe summary (counts, next eligible time) — never raw sensitive
    # content.
    sa.Column("trust_status", sa.String(16), server_default="unknown", nullable=False),
    sa.Column("terms_status", sa.String(16), server_default="unknown", nullable=False),
    sa.Column("robots_status", sa.String(16), server_default="unknown", nullable=False),
    sa.Column("adapter_version", sa.Text(), server_default="", nullable=False),
    sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    sa.Column("last_run_at", sa.DateTime(timezone=True)),
    sa.Column(
        "last_run_metadata",
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
    sa.CheckConstraint(
        "trust_status IN ('unknown', 'allowed', 'blocked')",
        name="trust_status_values",
    ),
    sa.CheckConstraint(
        "terms_status IN ('unknown', 'allowed', 'blocked')",
        name="terms_status_values",
    ),
    sa.CheckConstraint(
        "robots_status IN ('unknown', 'allowed', 'blocked')",
        name="robots_status_values",
    ),
    sa.CheckConstraint(
        "executor_mode IN ('http', 'ego')",
        name="executor_mode_values",
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
    # Section 5 provenance (migration 0016): link each version to the crawl
    # run and plan-version snapshot that produced it. Both nullable for
    # existing pre-Section-5 data; SET NULL on delete so a deleted run/plan
    # does not cascade into posting version loss.
    sa.Column(
        "crawl_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawl_runs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column(
        "plan_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawl_plan_versions.id", ondelete="SET NULL"),
        nullable=True,
    ),
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
    # 2.5 resume-derived evidence columns (additive). The repository/commit/
    # path/symbol path stays untouched; resume-extracted claims additionally
    # carry a bounded source_span, extractor_version, confirmation_status,
    # evidence_hash, and an optional link to the resume version they came from.
    sa.Column("extractor_version", sa.Text(), server_default="", nullable=False),
    sa.Column("source_span", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "confirmation_status",
        sa.String(16),
        server_default="unconfirmed",
        nullable=False,
    ),
    sa.Column("evidence_hash", sa.String(64), server_default="", nullable=False),
    sa.Column(
        "resume_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.resume_versions.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "kind IN ('skill', 'project', 'experience', 'certification', 'education')",
        name="kind_values",
    ),
    sa.CheckConstraint(
        "confirmation_status IN ('unconfirmed', 'confirmed', 'rejected')",
        name="confirmation_status_values",
    ),
    sa.CheckConstraint(
        "char_length(source_span) <= 8192",
        name="source_span_bounded",
    ),
    sa.CheckConstraint(
        "evidence_hash = '' OR char_length(evidence_hash) = 64",
        name="evidence_hash_length",
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
        "trusted_facts",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "evidence_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "untrusted_claims",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
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
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="RESTRICT"),
    ),
    # 2.1: the active candidate is resolved from the authenticated user. The
    # column stays nullable so bootstrap can create both rows in one txn, but
    # once set it is unique (single-user runtime). PostgreSQL UNIQUE allows
    # multiple NULLs, so a plain constraint preserves the nullable bootstrap
    # window while forbidding two users from claiming the same candidate.
    sa.UniqueConstraint("candidate_id", name="uq_console_users_candidate_id"),
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
    # 2.6: link to the active application cycle. Nullable for backfill; the
    # service layer makes it required for new rows in a later stage. The
    # candidate/job UQ below stays as a safety net (design Decision 8) while
    # one-active-cycle is enforced at the service layer.
    sa.Column(
        "cycle_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.application_cycles.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("state", sa.String(24), server_default="favorited", nullable=False),
    sa.Column("apply_url", sa.Text(), server_default="", nullable=False),
    # 2.7: submission channel / package version / payload / provider linkage.
    # All nullable and additive. submission_channel mirrors the Phase 0
    # SubmissionChannel enum; payload_hash binds the approved package version
    # (any mutation invalidates the prior approval — design Decision 4/5).
    sa.Column("submission_channel", sa.String(16), nullable=True),
    sa.Column("package_version_id", sa.Uuid(), nullable=True),
    sa.Column("payload_hash", sa.String(64), nullable=True),
    sa.Column("provider_kind", sa.String(24), nullable=True),
    sa.Column("provider_message_id", sa.Text(), nullable=True),
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
    sa.CheckConstraint(
        "submission_channel IS NULL OR submission_channel IN ('email', 'external_form', 'manual')",
        name="submission_channel_values",
    ),
    sa.CheckConstraint(
        "payload_hash IS NULL OR char_length(payload_hash) = 64",
        name="payload_hash_length",
    ),
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
    # 2.4 lifecycle columns (additive; defaults keep existing rows valid).
    sa.Column(
        "parse_status",
        sa.String(16),
        server_default="pending",
        nullable=False,
    ),
    sa.Column("parse_error", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "confirmation_status",
        sa.String(16),
        server_default="unconfirmed",
        nullable=False,
    ),
    sa.Column("source_reference", sa.Text(), server_default="", nullable=False),
    sa.Column("parsed_at", sa.DateTime(timezone=True)),
    sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "candidate_id", "version_number", name="uq_resume_versions_candidate_version"
    ),
    sa.CheckConstraint("version_number > 0", name="version_number_positive"),
    sa.CheckConstraint("char_length(content_hash) = 64", name="content_hash_length"),
    sa.CheckConstraint(
        "parse_status IN ('pending', 'parsed', 'failed')",
        name="parse_status_values",
    ),
    sa.CheckConstraint(
        "confirmation_status IN ('unconfirmed', 'confirmed', 'rejected')",
        name="confirmation_status_values",
    ),
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


application_package_versions = sa.Table(
    "application_package_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("version_number", sa.Integer(), nullable=False),
    sa.Column(
        "resume_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.resume_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "job_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_posting_versions.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column(
        "profile_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.profile_versions.id", ondelete="RESTRICT"),
        nullable=True,
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
    sa.Column(
        "attachments",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "diff",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "requirement_gaps",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("payload_hash", sa.String(64), nullable=True),
    sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("approved_by", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "application_id",
        "version_number",
        name="uq_application_package_versions_app_version",
    ),
    sa.CheckConstraint(
        "approval_state IN ('draft', 'pending_review', 'approved', 'rejected')",
        name="ck_application_package_versions_approval_state",
    ),
    sa.CheckConstraint(
        "payload_hash IS NULL OR char_length(payload_hash) = 64",
        name="ck_application_package_versions_payload_hash_length",
    ),
)

sa.Index(
    "ix_application_package_versions_app",
    application_package_versions.c.application_id,
    application_package_versions.c.version_number,
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
# End-to-end career loop: versioned profile preferences + application cycles
# (end-to-end-career-application-loop, Section 2). Additive; the matching and
# application projections above continue to work unchanged.
# ---------------------------------------------------------------------------

profile_versions = sa.Table(
    "profile_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column(
        "is_active",
        sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    ),
    # Structured preference columns (jsonb). Shape guards below keep each
    # column unambiguous so the service layer can decode them into the frozen
    # domain types in careerops/domain/profiles.py without type fallbacks.
    sa.Column(
        "target_roles",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "locations",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "remote_rules",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "compensation",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("seniority", sa.Text(), server_default="", nullable=False),
    # Column is ``authorization_rules`` (not ``authorization``) because
    # ``AUTHORIZATION`` is a PostgreSQL keyword and parsing it unquoted inside
    # CHECK expressions fails (jsonb_typeof(authorization) is ambiguous). The
    # name mirrors the sibling jsonb-rules columns remote_rules / hard_exclusions.
    sa.Column(
        "authorization_rules",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "include_keywords",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "exclude_keywords",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "hard_exclusions",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("candidate_id", "version", name="uq_profile_versions_candidate_version"),
    # 2.3 shape guards backing service-level contradictory-preference /
    # invalid-compensation validation (service layer arrives in stage 3).
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("jsonb_typeof(target_roles) = 'array'", name="target_roles_array"),
    sa.CheckConstraint("jsonb_typeof(locations) = 'array'", name="locations_array"),
    sa.CheckConstraint("jsonb_typeof(remote_rules) = 'object'", name="remote_rules_object"),
    sa.CheckConstraint("jsonb_typeof(compensation) = 'object'", name="compensation_object"),
    sa.CheckConstraint(
        "jsonb_typeof(authorization_rules) = 'object'", name="authorization_rules_object"
    ),
    sa.CheckConstraint("jsonb_typeof(include_keywords) = 'array'", name="include_keywords_array"),
    sa.CheckConstraint("jsonb_typeof(exclude_keywords) = 'array'", name="exclude_keywords_array"),
    sa.CheckConstraint("jsonb_typeof(hard_exclusions) = 'object'", name="hard_exclusions_object"),
    sa.CheckConstraint(
        "jsonb_typeof(compensation->'amount_min') IS DISTINCT FROM 'number' "
        "OR jsonb_typeof(compensation->'amount_max') IS DISTINCT FROM 'number' "
        "OR (compensation->'amount_min')::numeric <= (compensation->'amount_max')::numeric",
        name="compensation_range",
    ),
)

sa.Index(
    "ix_profile_versions_candidate_active",
    profile_versions.c.candidate_id,
    unique=True,
    postgresql_where=profile_versions.c.is_active == sa.true(),
)

application_cycles = sa.Table(
    "application_cycles",
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
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column(
        "active",
        sa.Boolean(),
        server_default=sa.text("true"),
        nullable=False,
    ),
    # Self-reference: an explicit re-application opens a new cycle linked to
    # the prior one, preserving history (design Decision 8). Inline self-FK
    # (PostgreSQL handles it); use_alter=True silently dropped the constraint
    # under op.create_table and broke alembic check.
    sa.Column("prior_cycle_id", sa.Uuid()),
    sa.Column("reason", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("closed_at", sa.DateTime(timezone=True)),
    sa.ForeignKeyConstraint(
        ["prior_cycle_id"],
        [f"{DATABASE_SCHEMA}.application_cycles.id"],
        name="fk_application_cycles_prior_cycle",
        ondelete="SET NULL",
    ),
    sa.CheckConstraint(
        "(active = true AND closed_at IS NULL) OR (active = false AND closed_at IS NOT NULL)",
        name="active_closed_consistent",
    ),
)

sa.Index(
    "ix_application_cycles_candidate_job_active",
    application_cycles.c.candidate_id,
    application_cycles.c.canonical_job_id,
    unique=True,
    postgresql_where=application_cycles.c.active == sa.true(),
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
    # Section 11 (additive): dedicated-account connection state. ``candidate_id``
    # scopes the account server-side (Iron Rule 2 — the legacy table was
    # unscoped). ``granted_scopes`` stores the validated readonly scope set
    # (JSONB array; always a subset of the gmail.readonly allowlist).
    # ``connection_state`` is the explicit lifecycle (disconnected/connected/
    # revoked/error) separate from the legacy ``status`` tri-state.
    # ``last_error_code`` carries a bounded machine code for ERROR/REVOKED.
    sa.Column("candidate_id", sa.Uuid(), nullable=True),
    sa.Column(
        "granted_scopes",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("connection_state", sa.String(16), server_default="disconnected", nullable=False),
    sa.Column("last_error_code", sa.Text(), server_default="", nullable=False),
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
    sa.CheckConstraint(
        "connection_state IN ('disconnected', 'connected', 'revoked', 'error')",
        name="ck_email_accounts_connection_state",
    ),
)

sa.Index("ix_email_accounts_candidate", email_accounts.c.candidate_id)

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
# Section 11: Gmail read sync — durable sync runs + thread association links
# ---------------------------------------------------------------------------

email_sync_runs = sa.Table(
    "email_sync_runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column("status", sa.String(16), server_default="pending", nullable=False),
    sa.Column("direction", sa.String(16), server_default="incremental", nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("messages_processed", sa.Integer(), server_default="0", nullable=False),
    sa.Column("messages_skipped", sa.Integer(), server_default="0", nullable=False),
    sa.Column("history_id_start", sa.Text(), server_default="", nullable=False),
    sa.Column("history_id_end", sa.Text(), server_default="", nullable=False),
    sa.Column("error_code", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "status IN ('pending', 'running', 'completed', 'partial', 'failed')",
        name="ck_email_sync_runs_status",
    ),
    sa.CheckConstraint(
        "direction IN ('full', 'incremental', 'backfill')",
        name="ck_email_sync_runs_direction",
    ),
    sa.CheckConstraint("messages_processed >= 0", name="ck_email_sync_runs_processed"),
    sa.CheckConstraint("messages_skipped >= 0", name="ck_email_sync_runs_skipped"),
)

sa.Index(
    "ix_email_sync_runs_account_created",
    email_sync_runs.c.account_id,
    email_sync_runs.c.created_at,
)

email_sync_cursors = sa.Table(
    "email_sync_cursors",
    metadata,
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("history_id", sa.Text(), server_default="", nullable=False),
    sa.Column("watch_expiration", sa.DateTime(timezone=True)),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
)

email_thread_links = sa.Table(
    "email_thread_links",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "thread_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_threads.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("account_id", sa.Uuid(), nullable=False),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
    ),
    sa.Column("status", sa.String(16), server_default="unlinked", nullable=False),
    sa.Column("confidence", sa.String(24), server_default="none", nullable=False),
    sa.Column(
        "candidate_application_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "evidence_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint("thread_id", name="uq_email_thread_links_thread"),
    sa.CheckConstraint(
        "status IN ('unlinked', 'linked', 'unresolved', 'confirmed')",
        name="ck_email_thread_links_status",
    ),
    sa.CheckConstraint(
        "confidence IN ('provider_id', 'sent_message', 'trusted_domain', 'subject_source', 'none')",
        name="ck_email_thread_links_confidence",
    ),
)

sa.Index(
    "ix_email_thread_links_candidate_status",
    email_thread_links.c.candidate_id,
    email_thread_links.c.status,
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

# ---------------------------------------------------------------------------
# M6B: Gmail send receipt store (provider-level, durable across restarts)
# ---------------------------------------------------------------------------

gmail_send_receipts = sa.Table(
    "gmail_send_receipts",
    metadata,
    sa.Column("reconciliation_key", sa.Text(), primary_key=True),
    sa.Column("provider_message_id", sa.Text(), nullable=False),
    sa.Column("thread_id", sa.Text(), nullable=False),
    sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
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
    "agent_stage_events",
)

# ---------------------------------------------------------------------------
# End-to-end career loop Section 4: crawl sources control columns (additive,
# declared above on ``job_sources``) + versioned crawl plans + run records.
# (crawl-plan-management spec, tasks 4.1-4.4.) Plan versions are immutable
# copy-on-write (design Decision 2); runs bind to one plan-version snapshot
# + source set + run identity (Iron Rule 4). The crawl policy layer stays
# authoritative — these tables declare scope only (Iron Rule 6).
# ---------------------------------------------------------------------------

crawl_plan_versions = sa.Table(
    "crawl_plan_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    # ``owner_id`` is the server-resolved candidate id (Iron Rule 2). Named
    # ``owner_id`` (not ``owner``) to avoid the PostgreSQL keyword and match
    # the FK ``*_id`` convention used elsewhere; semantically identical to
    # ``candidate_id`` on the sibling ``profile_versions`` table.
    sa.Column(
        "owner_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column(
        "is_active",
        sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    ),
    sa.Column(
        "sources",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "themes",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "include_keywords",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "exclude_keywords",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "role_families",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "locations",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "remote_rules",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "seniority",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "compensation",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("content_scope", sa.Text(), server_default="", nullable=False),
    # Schedule (Iron Rule 5: bounded intervals + IANA timezone validated by the
    # service layer in task 4.6; the DB stores the declared values).
    sa.Column("interval_seconds", sa.Integer(), nullable=False),
    sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
    sa.Column(
        "per_run_limits",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.UniqueConstraint("owner_id", "version", name="uq_crawl_plan_versions_owner_version"),
    # Shape guards mirroring ``profile_versions`` (Section 2) so the jsonb
    # columns decode unambiguously into the frozen domain types in
    # ``careerops.domain.crawl_plans``. Short names expand to
    # ``ck_crawl_plan_versions_<short>`` via the naming convention.
    sa.CheckConstraint("version > 0", name="version_positive"),
    sa.CheckConstraint("interval_seconds >= 0", name="interval_seconds_nonnegative"),
    sa.CheckConstraint("jsonb_typeof(sources) = 'array'", name="sources_array"),
    sa.CheckConstraint("jsonb_typeof(themes) = 'array'", name="themes_array"),
    sa.CheckConstraint("jsonb_typeof(include_keywords) = 'array'", name="include_keywords_array"),
    sa.CheckConstraint("jsonb_typeof(exclude_keywords) = 'array'", name="exclude_keywords_array"),
    sa.CheckConstraint("jsonb_typeof(role_families) = 'array'", name="role_families_array"),
    sa.CheckConstraint("jsonb_typeof(locations) = 'array'", name="locations_array"),
    sa.CheckConstraint("jsonb_typeof(remote_rules) = 'object'", name="remote_rules_object"),
    sa.CheckConstraint("jsonb_typeof(seniority) = 'array'", name="seniority_array"),
    sa.CheckConstraint("jsonb_typeof(compensation) = 'object'", name="compensation_object"),
    sa.CheckConstraint("jsonb_typeof(per_run_limits) = 'object'", name="per_run_limits_object"),
)

# Exactly one active plan version per owner (partial unique index). The plan
# repo performs the deactivate-prior + activate-new pair inside a single
# transaction so this index is never transiently violated (mirrors
# ``ix_profile_versions_candidate_active``).
sa.Index(
    "ix_crawl_plan_versions_owner_active",
    crawl_plan_versions.c.owner_id,
    unique=True,
    postgresql_where=crawl_plan_versions.c.is_active == sa.true(),
)

crawl_runs = sa.Table(
    "crawl_runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "plan_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawl_plan_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    # ``run_identity`` is the idempotency key (Iron Rule 4): a worker retry
    # that reuses the same identity MUST NOT create duplicate postings or
    # versions. The unique constraint below makes the DB the final authority.
    sa.Column("run_identity", sa.Text(), nullable=False, unique=True),
    sa.Column(
        "source_set",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "state",
        sa.String(24),
        server_default="pending",
        nullable=False,
    ),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("ended_at", sa.DateTime(timezone=True)),
    sa.Column(
        "limits",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "counters",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    # ``error_category`` is a SAFE, non-sensitive label (e.g. ``terms_blocked``,
    # ``rate_limited``, ``parser_drift``); the service layer never persists raw
    # error payloads or PII here (crawl-plan-management spec: "Operational
    # failures SHALL be actionable without exposing credentials or raw
    # sensitive content").
    sa.Column("error_category", sa.Text(), server_default="", nullable=False),
    sa.Column("next_eligible_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.CheckConstraint(
        "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'timeout')",
        name="state_values",
    ),
    sa.CheckConstraint("jsonb_typeof(source_set) = 'array'", name="source_set_array"),
    sa.CheckConstraint("jsonb_typeof(limits) = 'object'", name="limits_object"),
    sa.CheckConstraint("jsonb_typeof(counters) = 'object'", name="counters_object"),
)

# Ownership of a run is transitive via its plan version; list-for-owner queries
# join through ``plan_version_id``. Index the FK column + state so the "active
# runs for owner" and "terminal history for plan" projections stay cheap.
sa.Index(
    "ix_crawl_runs_plan_version_state",
    crawl_runs.c.plan_version_id,
    crawl_runs.c.state,
)
sa.Index(
    "ix_crawl_runs_created_at",
    crawl_runs.c.created_at,
)

# ---------------------------------------------------------------------------
# real-autonomous-career-loop Phase 5 (tasks 5.1/5.2): durable per-source
# attempt outcomes and source-specific crawl permissions. Both are NEW tables
# (additive); they reference job_sources via ON DELETE CASCADE so retiring a
# source removes its attempt/permission history without dangling FKs.
# ---------------------------------------------------------------------------

crawl_source_attempts = sa.Table(
    "crawl_source_attempts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # Nullable: a Tier 1 probe may classify a source without binding to a full
    # crawl run; the attempt outcome is still recorded.
    sa.Column(
        "crawl_run_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.crawl_runs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("attempt_no", sa.Integer(), nullable=False),
    # Canonical Tier 1 outcome (domain enum CrawlAttemptOutcome). Replaces the
    # fragmented non-canonical outcome signals in crawl_runs.error_category.
    sa.Column("outcome", sa.String(32), nullable=False),
    sa.Column("executor_mode", sa.String(16), server_default="http", nullable=False),
    # Browser actions consumed by this attempt (Tier 2 budget accounting, 7.x).
    sa.Column("action_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    # BOUNDED safe summary: status code, signal flags, counts, next-eligible
    # hint. NEVER raw page content, credentials, or PII.
    sa.Column(
        "evidence_summary",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("next_eligible_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "source_id", "attempt_no", name="uq_crawl_source_attempts_source_attempt_no"
    ),
    sa.CheckConstraint(
        "outcome IN ('postings_found','verified_empty','not_job_source',"
        "'transient_failure','auth_required','dynamic_or_unsupported','policy_denied')",
        name="outcome_values",
    ),
    sa.CheckConstraint("executor_mode IN ('http', 'ego')", name="executor_mode_values"),
    sa.CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
    sa.CheckConstraint("action_count >= 0", name="action_count_nonnegative"),
    sa.CheckConstraint(
        "jsonb_typeof(evidence_summary) = 'object'", name="evidence_summary_object"
    ),
)

sa.Index(
    "ix_crawl_source_attempts_source_created",
    crawl_source_attempts.c.source_id,
    crawl_source_attempts.c.created_at,
)
sa.Index(
    "ix_crawl_source_attempts_next_eligible",
    crawl_source_attempts.c.next_eligible_at,
)

crawl_source_permissions = sa.Table(
    "crawl_source_permissions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "source_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.job_sources.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("domain_scope", sa.Text(), server_default="", nullable=False),
    sa.Column("state", sa.String(16), server_default="pending", nullable=False),
    # Disclosed purpose / requested frequency / action+time limits (bounded
    # JSONB object). This is the consent contract the user grants against.
    sa.Column(
        "disclosed_terms",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    # Decision timestamps — each set when the matching transition is persisted.
    sa.Column("requested_at", sa.DateTime(timezone=True)),
    sa.Column("granted_at", sa.DateTime(timezone=True)),
    sa.Column("denied_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("expired_at", sa.DateTime(timezone=True)),
    # Configured grant deadline (distinct from expired_at = when it lapsed).
    sa.Column("expires_at", sa.DateTime(timezone=True)),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.CheckConstraint(
        "state IN ('pending', 'granted', 'denied', 'revoked', 'expired')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(disclosed_terms) = 'object'", name="disclosed_terms_object"
    ),
    # HARD CONSTRAINT: at most one PENDING request per source (spec scenario
    # "Repeated crawl sees an existing pending request -> reuses the existing
    # request instead of creating repeated prompts"). Enforced at the DB so the
    # Phase-6 permission service cannot create duplicate prompts under concurrency.
)

sa.Index(
    "ix_crawl_source_permissions_source_state",
    crawl_source_permissions.c.source_id,
    crawl_source_permissions.c.state,
)
# Partial unique index: only one pending permission per source at a time.
sa.Index(
    "uq_crawl_source_permissions_pending_source",
    crawl_source_permissions.c.source_id,
    postgresql_where=crawl_source_permissions.c.state == "pending",
)

# ---------------------------------------------------------------------------
# End-to-end career loop Section 6: inbox filter decisions + requirement matches
# ---------------------------------------------------------------------------

filter_decisions = sa.Table(
    "filter_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "canonical_job_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.canonical_jobs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "profile_version_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.profile_versions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("verdict", sa.String(16), nullable=False),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "semantic_ranking_status",
        sa.String(16),
        server_default="unavailable",
        nullable=False,
    ),
    sa.Column("semantic_ranking_score", sa.Numeric(5, 4), nullable=True),
    sa.Column("semantic_ranking_reason", sa.Text(), server_default="", nullable=False),
    sa.Column("semantic_model_version", sa.String(128), server_default="", nullable=False),
    sa.Column(
        "blocking_reasons",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "evidence_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "canonical_job_id",
        "candidate_id",
        "profile_version_id",
        name="uq_filter_decisions_job_candidate_profile",
    ),
    sa.CheckConstraint(
        "verdict IN ('recommended', 'excluded')",
        name="verdict_values",
    ),
    sa.CheckConstraint(
        "semantic_ranking_status IN ('available', 'unavailable', 'disabled')",
        name="semantic_ranking_status_values",
    ),
    sa.CheckConstraint(
        "semantic_ranking_score IS NULL OR "
        "(semantic_ranking_score >= 0 AND semantic_ranking_score <= 1)",
        name="semantic_ranking_score_range",
    ),
    sa.CheckConstraint("jsonb_typeof(blocking_reasons) = 'array'", name="blocking_reasons_array"),
    sa.CheckConstraint("jsonb_typeof(evidence_refs) = 'object'", name="evidence_refs_object"),
)

sa.Index(
    "ix_filter_decisions_candidate_verdict",
    filter_decisions.c.candidate_id,
    filter_decisions.c.verdict,
)

requirement_match_results = sa.Table(
    "requirement_match_results",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "filter_decision_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.filter_decisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("requirement_name", sa.Text(), nullable=False),
    sa.Column("match_level", sa.String(24), nullable=False),
    sa.Column(
        "evidence_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
    sa.Column("reason", sa.Text(), server_default="", nullable=False),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column("model_version", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.CheckConstraint(
        "match_level IN ('strong', 'partial', 'transferable', 'unsupported')",
        name="match_level_values",
    ),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="evidence_ids_array"),
)

sa.Index(
    "ix_requirement_match_results_decision",
    requirement_match_results.c.filter_decision_id,
)

# ---------------------------------------------------------------------------
# End-to-end career loop Section 6: inbox snooze records
# ---------------------------------------------------------------------------

inbox_snoozes = sa.Table(
    "inbox_snoozes",
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
    sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "candidate_id",
        "canonical_job_id",
        name="uq_inbox_snoozes_candidate_job",
    ),
)

sa.Index(
    "ix_inbox_snoozes_candidate_until",
    inbox_snoozes.c.candidate_id,
    inbox_snoozes.c.snoozed_until,
)


# Section 12 — recruiting mail intelligence: durable EmailEventProposal store.
# Additive (Iron Rule 8): one new table; no existing table is modified. The
# proposal links a message/thread to at most one application (nullable for the
# unresolved-link case, task 12.10). The extraction is stored as an immutable
# JSONB snapshot; only the decision state is mutable (pending -> terminal).
# Server-side candidate scoping is enforced via ``candidate_id`` (the
# server-resolved owner); a unique idempotency_key makes extraction idempotent.
email_event_proposals = sa.Table(
    "email_event_proposals",
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
        nullable=True,
    ),
    sa.Column(
        "account_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.email_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("category", sa.String(24), nullable=False),
    sa.Column("proposed_state", sa.String(24), nullable=True),
    sa.Column(
        "extraction",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("confidence", sa.Numeric(5, 4), server_default="0", nullable=False),
    sa.Column(
        "high_risk",
        sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    ),
    sa.Column(
        "review_required",
        sa.Boolean(),
        server_default=sa.text("true"),
        nullable=False,
    ),
    sa.Column(
        "prompt_injection_detected",
        sa.Boolean(),
        server_default=sa.text("false"),
        nullable=False,
    ),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("state", sa.String(16), server_default="pending", nullable=False),
    sa.Column("extraction_source", sa.String(8), server_default="rules", nullable=False),
    sa.Column("rules_version", sa.Text(), server_default="", nullable=False),
    sa.Column("model_version", sa.Text(), server_default="", nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decided_by", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
    sa.UniqueConstraint("idempotency_key", name="uq_email_event_proposals_idempotency"),
    sa.CheckConstraint(
        "state IN ('pending', 'accepted', 'rejected', 'superseded')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "extraction_source IN ('rules', 'model')",
        name="extraction_source_values",
    ),
    sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    sa.CheckConstraint(
        "(state IN ('accepted', 'rejected') AND decided_at IS NOT NULL) OR "
        "(state IN ('pending', 'superseded') AND decided_at IS NULL)",
        name="decision_timestamp_consistent",
    ),
)

sa.Index(
    "ix_email_event_proposals_candidate_state",
    email_event_proposals.c.candidate_id,
    email_event_proposals.c.state,
)
sa.Index(
    "ix_email_event_proposals_message",
    email_event_proposals.c.message_id,
)

# ---------------------------------------------------------------------------
# Section 13: reply draft versions — immutable, candidate-owned reply drafts
# (end-to-end-career-application-loop, tasks 13.4-13.5). Additive; the legacy
# M4 ``reply_drafts`` table above stays for backfill/back-compat. Server-side
# candidate ownership (candidate_id required); high-risk categories are never
# system-sent (draft/review only).
# ---------------------------------------------------------------------------

reply_draft_versions = sa.Table(
    "reply_draft_versions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("candidate_id", sa.Uuid(), nullable=False),
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
    sa.Column("account_id", sa.Uuid(), nullable=True),
    sa.Column(
        "application_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.applications.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("version_number", sa.Integer(), nullable=False),
    # Immutable trusted reply target (task 13.4).
    sa.Column("recipient", sa.Text(), server_default="", nullable=False),
    sa.Column("in_reply_to_header", sa.Text(), server_default="", nullable=False),
    sa.Column("references_header", sa.Text(), server_default="", nullable=False),
    # Editable content (body edits -> new version).
    sa.Column("subject", sa.Text(), server_default="", nullable=False),
    sa.Column("body_text", sa.Text(), server_default="", nullable=False),
    sa.Column("intent", sa.String(24), server_default="acknowledge", nullable=False),
    sa.Column("risk_category", sa.String(24), server_default="low_risk", nullable=False),
    sa.Column("mail_category", sa.Text(), server_default="", nullable=False),
    # Context + claims + validation findings (JSONB).
    sa.Column(
        "context",
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
    sa.Column(
        "validation_issues",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("payload_hash", sa.String(64), nullable=False),
    sa.Column("approval_state", sa.String(24), server_default="draft", nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decided_by", sa.Text(), server_default="", nullable=False),
    sa.Column("send_intent_id", sa.Uuid(), nullable=True),
    sa.Column("send_phase", sa.String(24), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "thread_id",
        "candidate_id",
        "version_number",
        name="uq_reply_draft_versions_thread_candidate_version",
    ),
    sa.CheckConstraint(
        "approval_state IN "
        "('draft', 'pending_review', 'approved', 'rejected', 'expired', 'superseded')",
        name="ck_reply_draft_versions_approval_state",
    ),
    sa.CheckConstraint(
        "char_length(payload_hash) = 64",
        name="ck_reply_draft_versions_payload_hash_length",
    ),
    sa.CheckConstraint(
        "(approval_state IN ('approved', 'rejected') AND decided_at IS NOT NULL) "
        "OR (approval_state NOT IN ('approved', 'rejected') AND decided_at IS NULL)",
        name="ck_reply_draft_versions_decision_timestamp_consistent",
    ),
)

sa.Index(
    "ix_reply_draft_versions_candidate_state",
    reply_draft_versions.c.candidate_id,
    reply_draft_versions.c.approval_state,
)
sa.Index(
    "ix_reply_draft_versions_application",
    reply_draft_versions.c.application_id,
)
sa.Index(
    "ix_reply_draft_versions_thread",
    reply_draft_versions.c.thread_id,
    reply_draft_versions.c.candidate_id,
)

# ---------------------------------------------------------------------------
# LLM Agent runs — candidate-scoped, review-only durable execution metadata.
# Raw prompts/responses and credentials are deliberately absent.  ``result``
# is a bounded structured proposal; input_identities/evidence_ids make stale
# checks and audit views possible without persisting private source material.
# ---------------------------------------------------------------------------

agent_runs = sa.Table(
    "agent_runs",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("capability", sa.String(32), nullable=False),
    sa.Column("state", sa.String(24), server_default="pending", nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("input_hash", sa.String(64), nullable=False),
    sa.Column(
        "input_identities",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "evidence_ids",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("schema_version", sa.String(64), server_default="", nullable=False),
    sa.Column("prompt_version", sa.String(64), server_default="", nullable=False),
    sa.Column("model_id", sa.String(128), server_default="", nullable=False),
    sa.Column("trace_id", sa.String(128), server_default="", nullable=False),
    sa.Column(
        "result",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("error_category", sa.String(64), server_default="", nullable=False),
    sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
    sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
    sa.Column("review_decision", sa.String(16), nullable=True),
    sa.Column("reviewed_by", sa.String(128), server_default="", nullable=False),
    sa.Column("review_note", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("execution_state", sa.Text(), server_default="queued", nullable=False),
    sa.Column("capability_state", sa.Text(), server_default="enabled", nullable=False),
    sa.Column("review_state", sa.Text(), server_default="not_required", nullable=False),
    sa.Column("legacy_state", sa.Text(), nullable=True),
    sa.Column("context_id", sa.Uuid(), nullable=True),
    sa.Column("current_attempt", sa.Integer(), server_default="0", nullable=False),
    sa.UniqueConstraint(
        "candidate_id",
        "capability",
        "idempotency_key",
        name="uq_agent_runs_candidate_capability_key",
    ),
    sa.CheckConstraint(
        "capability IN ('job_matching', 'resume_review', 'interview_preparation')",
        name="capability_values",
    ),
    sa.CheckConstraint(
        "state IN ('pending', 'running', 'succeeded', 'failed', 'unavailable', "
        "'abstained', 'stale', 'cancelled', 'reviewed')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "review_decision IS NULL OR review_decision IN ('accepted', 'rejected', 'edited')",
        name="review_decision_values",
    ),
    sa.CheckConstraint("char_length(input_hash) = 64", name="input_hash_length"),
    sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="usage_nonnegative"),
    sa.CheckConstraint("jsonb_typeof(input_identities) = 'object'", name="input_object"),
    sa.CheckConstraint("jsonb_typeof(evidence_ids) = 'array'", name="evidence_array"),
    sa.CheckConstraint("jsonb_typeof(result) = 'object'", name="result_object"),
    sa.ForeignKeyConstraint(
        ["context_id", "candidate_id"],
        [
            f"{DATABASE_SCHEMA}.agent_contexts.id",
            f"{DATABASE_SCHEMA}.agent_contexts.candidate_id",
        ],
        name="fk_agent_runs_context_candidate",
        ondelete="SET NULL",
    ),
    sa.CheckConstraint(
        "execution_state IN ('queued', 'running', 'waiting_review', 'succeeded', 'failed', "
        "'cancel_requested', 'cancelled', 'stale', 'blocked')",
        name="execution_state",
    ),
    sa.CheckConstraint(
        "capability_state IN ('enabled', 'disabled_by_policy', 'not_configured', "
        "'dependency_not_ready', 'blocked_by_prerequisite', 'stale', 'failed')",
        name="capability_state",
    ),
    sa.CheckConstraint(
        "review_state IN ('not_required', 'pending', 'accepted', 'rejected', 'edited')",
        name="review_state",
    ),
)

sa.Index("ix_agent_runs_candidate_state", agent_runs.c.candidate_id, agent_runs.c.state)
sa.Index("ix_agent_runs_candidate_created", agent_runs.c.candidate_id, agent_runs.c.created_at)
sa.Index(
    "uq_agent_runs_id_candidate",
    agent_runs.c.id,
    agent_runs.c.candidate_id,
    unique=True,
)

agent_run_reviews = sa.Table(
    "agent_run_reviews",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("actor_id", sa.String(128), nullable=False),
    sa.Column("note", sa.Text(), server_default="", nullable=False),
    sa.Column(
        "edited_result",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("idempotency_key", sa.String(128), nullable=True),
    sa.Column("field_decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column("request_hash", sa.String(64), nullable=True),
    sa.ForeignKeyConstraint(
        ["run_id", "candidate_id"],
        [
            f"{DATABASE_SCHEMA}.agent_runs.id",
            f"{DATABASE_SCHEMA}.agent_runs.candidate_id",
        ],
        name="fk_agent_run_reviews_run_candidate",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "request_hash IS NULL OR char_length(request_hash) = 64",
        name="request_hash",
    ),
    sa.CheckConstraint(
        "field_decisions IS NULL OR jsonb_typeof(field_decisions) = 'array'",
        name="field_decisions",
    ),
    sa.CheckConstraint(
        "decision IN ('accepted', 'rejected', 'edited')",
        name="decision_values",
    ),
    sa.CheckConstraint("jsonb_typeof(edited_result) = 'object'", name="edited_object"),
)

sa.Index(
    "ix_agent_run_reviews_candidate_run",
    agent_run_reviews.c.candidate_id,
    agent_run_reviews.c.run_id,
)
sa.Index(
    "uq_agent_run_reviews_candidate_key",
    agent_run_reviews.c.candidate_id,
    agent_run_reviews.c.run_id,
    agent_run_reviews.c.idempotency_key,
    unique=True,
    postgresql_where=agent_run_reviews.c.idempotency_key.is_not(None),
)

# ---------------------------------------------------------------------------
# Agent console orchestration (migration 0030)
# ---------------------------------------------------------------------------

agent_contexts = sa.Table(
    "agent_contexts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("operation", sa.String(32), nullable=False),
    sa.Column("schema_version", sa.String(64), nullable=False),
    sa.Column("digest_algorithm", sa.String(16), nullable=False),
    sa.Column(
        "source_version_snapshot",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column(
        "source_digests",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("allowed_operation", sa.String(32), nullable=False),
    sa.Column("state", sa.String(16), server_default="active", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("invalidation_reason", sa.String(64), nullable=True),
    sa.UniqueConstraint("id", "candidate_id", name="uq_agent_contexts_id_candidate"),
    sa.CheckConstraint(
        "digest_algorithm = 'sha256'",
        name="ck_agent_contexts_digest_algorithm",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_version_snapshot) = 'object'",
        name="ck_agent_contexts_source_version_snapshot_object",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_digests) = 'object'",
        name="ck_agent_contexts_source_digests_object",
    ),
    sa.CheckConstraint(
        "state IN ('active', 'stale', 'expired', 'revoked')",
        name="ck_agent_contexts_state",
    ),
    sa.CheckConstraint(
        "operation = allowed_operation",
        name="ck_agent_contexts_operation_match",
    ),
    sa.CheckConstraint(
        "expires_at > created_at",
        name="ck_agent_contexts_expires_after_created",
    ),
)

sa.Index(
    "ix_agent_contexts_candidate_state",
    agent_contexts.c.candidate_id,
    agent_contexts.c.state,
)

agent_actions = sa.Table(
    "agent_actions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("action_key", sa.String(160), nullable=False),
    sa.Column("source_event_key", sa.Uuid(), nullable=False),
    sa.Column("queue_version", sa.BigInteger(), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("reason_code", sa.String(64), nullable=False),
    sa.Column("deterministic_rank", sa.Integer(), nullable=False),
    sa.Column(
        "source_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("context_id", sa.Uuid(), nullable=True),
    sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "candidate_id",
        "action_key",
        "source_event_key",
        name="uq_agent_actions_candidate_key_event",
    ),
    sa.ForeignKeyConstraint(
        ["context_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.agent_contexts.id", f"{DATABASE_SCHEMA}.agent_contexts.candidate_id"],
        name="fk_agent_actions_context_candidate",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "queue_version > 0",
        name="ck_agent_actions_queue_version_positive",
    ),
    sa.CheckConstraint(
        (
            "state IN ('proposed', 'accepted', 'snoozed', 'dismissed', 'completed', "
            "'expired', 'blocked')"
        ),
        name="ck_agent_actions_state",
    ),
    sa.CheckConstraint(
        "deterministic_rank >= 0",
        name="ck_agent_actions_rank_nonnegative",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_refs) = 'array'",
        name="ck_agent_actions_source_refs_array",
    ),
)

sa.Index(
    "ix_agent_actions_candidate_state",
    agent_actions.c.candidate_id,
    agent_actions.c.state,
)

agent_attempts = sa.Table(
    "agent_attempts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("attempt_no", sa.Integer(), nullable=False),
    sa.Column("workflow_id", sa.String(220), nullable=False),
    sa.Column("worker_id", sa.Uuid(), nullable=True),
    sa.Column("lease_epoch", sa.BigInteger(), server_default="0", nullable=False),
    sa.Column("cancel_epoch", sa.BigInteger(), server_default="0", nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("retry_budget", sa.Integer(), nullable=False),
    sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("dead_letter_reason", sa.String(64), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint(
        "run_id",
        "attempt_no",
        name="uq_agent_attempts_run_attempt_no",
    ),
    sa.UniqueConstraint(
        "id",
        "run_id",
        "candidate_id",
        name="uq_agent_attempts_id_run_candidate",
    ),
    sa.ForeignKeyConstraint(
        ["run_id", "candidate_id"],
        [f"{DATABASE_SCHEMA}.agent_runs.id", f"{DATABASE_SCHEMA}.agent_runs.candidate_id"],
        name="fk_agent_attempts_run_candidate",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "attempt_no > 0",
        name="ck_agent_attempts_attempt_no_positive",
    ),
    sa.CheckConstraint(
        "lease_epoch >= 0",
        name="ck_agent_attempts_lease_epoch_nonnegative",
    ),
    sa.CheckConstraint(
        "cancel_epoch >= 0",
        name="ck_agent_attempts_cancel_epoch_nonnegative",
    ),
    sa.CheckConstraint(
        "state IN ('queued', 'running', 'waiting_review', 'succeeded', 'failed', "
        "'cancel_requested', 'cancelled', 'stale', 'blocked')",
        name="ck_agent_attempts_state",
    ),
    sa.CheckConstraint(
        "retry_budget >= 0",
        name="ck_agent_attempts_retry_budget_nonnegative",
    ),
)

sa.Index(
    "ix_agent_attempts_run_candidate",
    agent_attempts.c.run_id,
    agent_attempts.c.candidate_id,
)

agent_stage_events = sa.Table(
    "agent_stage_events",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column("attempt_id", sa.Uuid(), nullable=False),
    sa.Column("run_id", sa.Uuid(), nullable=False),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("event_key", sa.Uuid(), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("schema_version", sa.String(64), nullable=False),
    sa.Column("stage", sa.String(64), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("cause", sa.String(64), nullable=True),
    sa.Column("terminal", sa.Boolean(), server_default="false", nullable=False),
    sa.Column("provider_state", sa.String(32), nullable=True),
    sa.Column("retryable", sa.Boolean(), server_default="false", nullable=False),
    sa.Column(
        "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("duration_ms", sa.Integer(), nullable=True),
    sa.Column(
        "redacted_payload",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
    ),
    sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("event_key", name="uq_agent_stage_events_event_key"),
    sa.UniqueConstraint(
        "attempt_id",
        "sequence",
        name="uq_agent_stage_events_attempt_sequence",
    ),
    sa.ForeignKeyConstraint(
        ["attempt_id", "run_id", "candidate_id"],
        [
            f"{DATABASE_SCHEMA}.agent_attempts.id",
            f"{DATABASE_SCHEMA}.agent_attempts.run_id",
            f"{DATABASE_SCHEMA}.agent_attempts.candidate_id",
        ],
        name="fk_agent_stage_events_attempt_run_candidate",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "sequence > 0",
        name="ck_agent_stage_events_sequence_positive",
    ),
    sa.CheckConstraint(
        "status IN ('started', 'completed', 'blocked', 'failed', 'cancelled')",
        name="ck_agent_stage_events_status",
    ),
    sa.CheckConstraint(
        "duration_ms IS NULL OR duration_ms >= 0",
        name="ck_agent_stage_events_duration_nonnegative",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(redacted_payload) = 'object'",
        name="ck_agent_stage_events_payload_object",
    ),
)

sa.Index(
    "ix_agent_stage_events_attempt_sequence",
    agent_stage_events.c.attempt_id,
    agent_stage_events.c.sequence,
)
sa.Index(
    "ix_agent_stage_events_run_candidate",
    agent_stage_events.c.run_id,
    agent_stage_events.c.candidate_id,
)

agent_idempotency_receipts = sa.Table(
    "agent_idempotency_receipts",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "actor_id",
        sa.String(128),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("resource_type", sa.String(64), nullable=False),
    sa.Column("resource_id", sa.Uuid(), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("canonical_body_hash", sa.String(64), nullable=False),
    sa.Column("response_status", sa.SmallInteger(), nullable=False),
    sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint(
        "actor_id",
        "candidate_id",
        "resource_type",
        "resource_id",
        "operation",
        "idempotency_key",
        name="uq_agent_idempotency_receipts_identity",
    ),
    sa.CheckConstraint(
        "char_length(canonical_body_hash) = 64",
        name="body_hash",
    ),
    sa.CheckConstraint(
        "response_status BETWEEN 200 AND 499",
        name="status",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(receipt) = 'object'",
        name="receipt_object",
    ),
    sa.CheckConstraint(
        "resource_id <> '00000000-0000-0000-0000-000000000000'::uuid "
        "OR resource_type IN ('agent_context', 'agent_action_queue', 'crawl_plan')",
        name="zero_resource_scope",
    ),
)

# ---------------------------------------------------------------------------
# Smart form intake (review-only, short-lived, candidate-owned)
# ---------------------------------------------------------------------------

smart_intake_previews = sa.Table(
    "smart_intake_previews",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        sa.ForeignKey(f"{DATABASE_SCHEMA}.candidates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("target", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    sa.Column("request_fingerprint", sa.String(64), nullable=False),
    sa.Column("input_digest", sa.String(64), nullable=False),
    sa.Column("context_digest", sa.String(64), nullable=False),
    sa.Column("input_text", sa.Text(), nullable=False),
    sa.Column(
        "context_refs",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "fields",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column("state", sa.String(24), server_default="unavailable", nullable=False),
    sa.Column("claim_state", sa.String(16), server_default="finalized", nullable=False),
    sa.Column("claim_token", sa.String(64), server_default="", nullable=False),
    sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("schema_version", sa.String(64), server_default="", nullable=False),
    sa.Column("prompt_version", sa.String(64), server_default="", nullable=False),
    sa.Column("model_id", sa.String(128), server_default="", nullable=False),
    sa.Column("capability_state", sa.String(64), server_default="", nullable=False),
    sa.Column("trace_id", sa.String(128), server_default="", nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint(
        "candidate_id",
        "target",
        "idempotency_key",
        name="uq_smart_intake_previews_candidate_target_key",
    ),
    sa.UniqueConstraint(
        "id",
        "candidate_id",
        name="uq_smart_intake_previews_id_candidate",
    ),
    sa.CheckConstraint(
        "target IN ('profile', 'interview_context')",
        name="target_values",
    ),
    sa.CheckConstraint(
        "state IN ('unavailable', 'ready', 'abstained', 'invalid', 'stale', 'expired', 'revoked')",
        name="state_values",
    ),
    sa.CheckConstraint(
        "claim_state IN ('pending', 'finalized', 'reclaimed')",
        name="claim_state_values",
    ),
    sa.CheckConstraint("char_length(request_fingerprint) = 64", name="request_hash_length"),
    sa.CheckConstraint("char_length(input_digest) = 64", name="input_hash_length"),
    sa.CheckConstraint("char_length(context_digest) = 64", name="context_hash_length"),
    sa.CheckConstraint("char_length(input_text) <= 12000", name="input_length"),
    sa.CheckConstraint("jsonb_typeof(context_refs) = 'object'", name="context_object"),
    sa.CheckConstraint("jsonb_typeof(fields) = 'array'", name="fields_array"),
)

sa.Index(
    "ix_smart_intake_previews_candidate_created",
    smart_intake_previews.c.candidate_id,
    smart_intake_previews.c.created_at,
)
sa.Index(
    "ix_smart_intake_previews_expiry",
    smart_intake_previews.c.expires_at,
    smart_intake_previews.c.purged_at,
)

smart_intake_decisions = sa.Table(
    "smart_intake_decisions",
    metadata,
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "preview_id",
        sa.Uuid(),
        nullable=False,
    ),
    sa.Column(
        "candidate_id",
        sa.Uuid(),
        nullable=False,
    ),
    sa.ForeignKeyConstraint(
        ["preview_id", "candidate_id"],
        [
            f"{DATABASE_SCHEMA}.smart_intake_previews.id",
            f"{DATABASE_SCHEMA}.smart_intake_previews.candidate_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["candidate_id"],
        [f"{DATABASE_SCHEMA}.candidates.id"],
        ondelete="CASCADE",
    ),
    sa.Column("apply_idempotency_key", sa.String(128), nullable=False),
    sa.Column("decision_set_hash", sa.String(64), nullable=False),
    sa.Column(
        "decisions",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    ),
    sa.Column(
        "draft_patch",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    ),
    sa.Column("actor_id", sa.String(128), nullable=False),
    sa.Column("trace_id", sa.String(128), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    ),
    sa.UniqueConstraint(
        "preview_id",
        "apply_idempotency_key",
        name="uq_smart_intake_decisions_preview_apply_key",
    ),
    sa.CheckConstraint("char_length(decision_set_hash) = 64", name="decision_hash_length"),
    sa.CheckConstraint("jsonb_typeof(decisions) = 'array'", name="decisions_array"),
    sa.CheckConstraint("jsonb_typeof(draft_patch) = 'object'", name="patch_object"),
)

sa.Index(
    "ix_smart_intake_decisions_candidate_created",
    smart_intake_decisions.c.candidate_id,
    smart_intake_decisions.c.created_at,
)

# ---------------------------------------------------------------------------
# Tier 2 budget persistence (real-autonomous-career-loop Phase 7.1)
# ---------------------------------------------------------------------------

tier2_budget_daily = sa.Table(
    "tier2_budget_daily",
    metadata,
    # Single-row-per-day table keyed by the UTC date.
    sa.Column("day", sa.Date(), primary_key=True),
    sa.Column(
        "consumed",
        sa.Integer(),
        server_default=sa.text("0"),
        nullable=False,
    ),
    sa.CheckConstraint("consumed >= 0", name="consumed_nonnegative"),
)

tier2_budget_leases = sa.Table(
    "tier2_budget_leases",
    metadata,
    sa.Column("lease_id", sa.String(128), primary_key=True),
    sa.Column("source_id", sa.String(128), nullable=False),
    sa.Column(
        "acquired_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    ),
)
sa.Index(
    "ix_tier2_budget_leases_source",
    tier2_budget_leases.c.source_id,
)
