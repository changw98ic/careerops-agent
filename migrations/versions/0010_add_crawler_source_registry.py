"""add configurable crawler source registry and scheduler

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "crawler_source_registries",
    "crawler_source_runs",
    "crawler_job_deduplication_keys",
    "crawler_job_ingestion_evidence",
)

_API_GRANTS = (
    "GRANT SELECT ON careerops.crawler_source_registries, "
    "careerops.crawler_source_registry_sources, careerops.crawler_source_runs, "
    "careerops.crawler_job_deduplication_keys, careerops.crawler_job_ingestion_evidence "
    "TO careerops_api",
    "GRANT INSERT (id, kind, manifest_path, manifest_sha256, registry_sha256, source_count, "
    "created_by, generated_at, manifest_json) ON careerops.crawler_source_registries "
    "TO careerops_api",
    "GRANT INSERT (id, registry_id, source_id, adapter, enabled, input_artifact, output_dir, "
    "dependency_source_ids, dependency_artifacts, command_sha256, source_sha256, source_json, "
    "cursor, cadence_seconds, retry_rounds, budget_json, rate_limit_json, robots_terms_policy, "
    "canonical_ingestion_policy, provenance_policy, dedupe_policy, last_run_at, next_run_at, "
    "last_result, last_error) ON careerops.crawler_source_registry_sources TO careerops_api",
    "GRANT INSERT (id, dedupe_policy, dedupe_key_sha256, company_id, canonical_job_id, rule, "
    "algorithm_version) ON careerops.crawler_job_deduplication_keys TO careerops_api",
    "GRANT INSERT (id, source_row_id, run_id, run_event_id, job_posting_id, "
    "job_posting_version_id, canonical_job_id, source_url, content_hash, parser_version, "
    "dedupe_policy, dedupe_rule, dedupe_key_sha256, provenance_json, captured_at) "
    "ON careerops.crawler_job_ingestion_evidence TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.crawler_source_controls_are_safe("
    "text, jsonb, jsonb) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.claim_due_crawler_source(uuid, text, uuid, integer) "
    "TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.claim_crawler_source(uuid, text, text, uuid, integer) "
    "TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.complete_crawler_source_run("
    "uuid, text, uuid, text, text, text) TO careerops_api",
    "GRANT EXECUTE ON FUNCTION careerops.fail_crawler_source_run(uuid, text, uuid, text) "
    "TO careerops_api",
)

_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.crawler_source_registries, "
    "careerops.crawler_source_registry_sources, careerops.crawler_source_runs, "
    "careerops.crawler_job_deduplication_keys, careerops.crawler_job_ingestion_evidence "
    "TO careerops_readonly",
)

_API_REVOKES = (
    "REVOKE ALL ON careerops.crawler_source_registries FROM careerops_api",
    "REVOKE ALL ON careerops.crawler_source_registry_sources FROM careerops_api",
    "REVOKE ALL ON careerops.crawler_source_runs FROM careerops_api",
    "REVOKE ALL ON careerops.crawler_job_deduplication_keys FROM careerops_api",
    "REVOKE ALL ON careerops.crawler_job_ingestion_evidence FROM careerops_api",
    "REVOKE EXECUTE ON FUNCTION careerops.crawler_source_controls_are_safe("
    "text, jsonb, jsonb) FROM careerops_api",
    "REVOKE EXECUTE ON FUNCTION careerops.claim_due_crawler_source(uuid, text, uuid, integer) "
    "FROM careerops_api",
    "REVOKE EXECUTE ON FUNCTION careerops.claim_crawler_source(uuid, text, text, uuid, integer) "
    "FROM careerops_api",
    "REVOKE EXECUTE ON FUNCTION careerops.complete_crawler_source_run("
    "uuid, text, uuid, text, text, text) FROM careerops_api",
    "REVOKE EXECUTE ON FUNCTION careerops.fail_crawler_source_run(uuid, text, uuid, text) "
    "FROM careerops_api",
)

_READONLY_REVOKES = (
    "REVOKE ALL ON careerops.crawler_source_registries FROM careerops_readonly",
    "REVOKE ALL ON careerops.crawler_source_registry_sources FROM careerops_readonly",
    "REVOKE ALL ON careerops.crawler_source_runs FROM careerops_readonly",
    "REVOKE ALL ON careerops.crawler_job_deduplication_keys FROM careerops_readonly",
    "REVOKE ALL ON careerops.crawler_job_ingestion_evidence FROM careerops_readonly",
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


def _install_crawler_scheduler_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops._claim_crawler_source(
    p_registry_id uuid,
    p_source_id text,
    p_worker_id text,
    p_lease_token uuid,
    p_lease_seconds integer
)
RETURNS TABLE (
    run_id uuid,
    id uuid,
    registry_id uuid,
    source_id text,
    adapter text,
    enabled boolean,
    input_artifact text,
    output_dir text,
    dependency_source_ids jsonb,
    dependency_artifacts jsonb,
    command_sha256 varchar(64),
    source_sha256 varchar(64),
    source_json jsonb,
    cursor text,
    cadence_seconds integer,
    retry_rounds integer,
    budget_json jsonb,
    rate_limit_json jsonb,
    robots_terms_policy text,
    canonical_ingestion_policy text,
    provenance_policy text,
    dedupe_policy text,
    last_run_at timestamptz,
    next_run_at timestamptz,
    last_result text,
    last_error text,
    lease_owner text,
    lease_token uuid,
    lease_until timestamptz,
    active_run_id uuid,
    attempt_count integer,
    consecutive_failures integer,
    completed_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_lease_until timestamptz;
    v_run_id uuid := gen_random_uuid();
    v_source careerops.crawler_source_registry_sources%ROWTYPE;
    v_attempt integer;
BEGIN
    IF p_lease_seconds < 30 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'crawler source lease_seconds must be between 30 and 3600'
            USING ERRCODE = '22023';
    END IF;
    IF p_worker_id IS NULL OR p_worker_id !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
        RAISE EXCEPTION 'crawler source worker_id is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_source_id IS NOT NULL AND p_source_id !~ '^[a-z][a-z0-9-]{0,63}$' THEN
        RAISE EXCEPTION 'crawler source_id is invalid' USING ERRCODE = '22023';
    END IF;

    v_lease_until := v_now + make_interval(secs => p_lease_seconds);

    SELECT s.*
    INTO v_source
    FROM careerops.crawler_source_registry_sources AS s
    WHERE s.registry_id = p_registry_id
      AND (p_source_id IS NULL OR s.source_id = p_source_id)
      AND s.enabled IS TRUE
      AND s.robots_terms_policy = 'respect'
      AND (s.next_run_at IS NULL OR s.next_run_at <= v_now)
      AND (s.lease_token IS NULL OR s.lease_until < v_now)
    ORDER BY COALESCE(s.next_run_at, s.created_at), s.source_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    v_attempt := v_source.attempt_count;
    IF v_source.lease_token IS NOT NULL
       AND v_source.lease_until < v_now
       AND v_source.active_run_id IS NOT NULL THEN
        v_attempt := v_source.attempt_count + 1;
        INSERT INTO careerops.crawler_source_runs (
            run_id, source_row_id, registry_id, source_id, worker_id, lease_token,
            event_kind, cursor_before, cursor_after, result, error, output_manifest_sha256,
            attempt_number, claimed_at, lease_until, completed_at
        )
        VALUES (
            v_source.active_run_id, v_source.id, v_source.registry_id, v_source.source_id,
            v_source.lease_owner, v_source.lease_token, 'expired', v_source.cursor, NULL,
            'expired', 'lease_expired', NULL, v_attempt, v_source.last_run_at,
            v_source.lease_until, v_now
        );
    END IF;

    UPDATE careerops.crawler_source_registry_sources AS s
    SET lease_owner = p_worker_id,
        lease_token = p_lease_token,
        lease_until = v_lease_until,
        active_run_id = v_run_id,
        attempt_count = v_attempt,
        consecutive_failures = CASE
            WHEN v_source.lease_token IS NOT NULL
             AND v_source.lease_until < v_now
             AND v_source.active_run_id IS NOT NULL
            THEN v_source.consecutive_failures + 1
            ELSE v_source.consecutive_failures
        END,
        last_run_at = v_now,
        next_run_at = v_lease_until,
        last_result = 'leased',
        last_error = NULL,
        updated_at = v_now
    WHERE s.id = v_source.id;

    INSERT INTO careerops.crawler_source_runs (
        run_id, source_row_id, registry_id, source_id, worker_id, lease_token,
        event_kind, cursor_before, cursor_after, result, error, output_manifest_sha256,
        attempt_number, claimed_at, lease_until, completed_at
    )
    VALUES (
        v_run_id, v_source.id, v_source.registry_id, v_source.source_id, p_worker_id,
        p_lease_token, 'claimed', v_source.cursor, NULL, 'leased', NULL, NULL,
        v_attempt, v_now, v_lease_until, NULL
    );

    RETURN QUERY
    SELECT
        v_run_id,
        s.id,
        s.registry_id,
        s.source_id,
        s.adapter,
        s.enabled,
        s.input_artifact,
        s.output_dir,
        s.dependency_source_ids,
        s.dependency_artifacts,
        s.command_sha256,
        s.source_sha256,
        s.source_json,
        s.cursor,
        s.cadence_seconds,
        s.retry_rounds,
        s.budget_json,
        s.rate_limit_json,
        s.robots_terms_policy,
        s.canonical_ingestion_policy,
        s.provenance_policy,
        s.dedupe_policy,
        s.last_run_at,
        s.next_run_at,
        s.last_result,
        s.last_error,
        s.lease_owner,
        s.lease_token,
        s.lease_until,
        s.active_run_id,
        s.attempt_count,
        s.consecutive_failures,
        s.completed_at,
        s.created_at,
        s.updated_at
    FROM careerops.crawler_source_registry_sources AS s
    WHERE s.id = v_source.id;
END;
$$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.claim_due_crawler_source(
    p_registry_id uuid,
    p_worker_id text,
    p_lease_token uuid,
    p_lease_seconds integer
)
RETURNS TABLE (
    run_id uuid,
    id uuid,
    registry_id uuid,
    source_id text,
    adapter text,
    enabled boolean,
    input_artifact text,
    output_dir text,
    dependency_source_ids jsonb,
    dependency_artifacts jsonb,
    command_sha256 varchar(64),
    source_sha256 varchar(64),
    source_json jsonb,
    cursor text,
    cadence_seconds integer,
    retry_rounds integer,
    budget_json jsonb,
    rate_limit_json jsonb,
    robots_terms_policy text,
    canonical_ingestion_policy text,
    provenance_policy text,
    dedupe_policy text,
    last_run_at timestamptz,
    next_run_at timestamptz,
    last_result text,
    last_error text,
    lease_owner text,
    lease_token uuid,
    lease_until timestamptz,
    active_run_id uuid,
    attempt_count integer,
    consecutive_failures integer,
    completed_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
    SELECT * FROM careerops._claim_crawler_source(
        p_registry_id, NULL, p_worker_id, p_lease_token, p_lease_seconds
    );
$$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.claim_crawler_source(
    p_registry_id uuid,
    p_source_id text,
    p_worker_id text,
    p_lease_token uuid,
    p_lease_seconds integer
)
RETURNS TABLE (
    run_id uuid,
    id uuid,
    registry_id uuid,
    source_id text,
    adapter text,
    enabled boolean,
    input_artifact text,
    output_dir text,
    dependency_source_ids jsonb,
    dependency_artifacts jsonb,
    command_sha256 varchar(64),
    source_sha256 varchar(64),
    source_json jsonb,
    cursor text,
    cadence_seconds integer,
    retry_rounds integer,
    budget_json jsonb,
    rate_limit_json jsonb,
    robots_terms_policy text,
    canonical_ingestion_policy text,
    provenance_policy text,
    dedupe_policy text,
    last_run_at timestamptz,
    next_run_at timestamptz,
    last_result text,
    last_error text,
    lease_owner text,
    lease_token uuid,
    lease_until timestamptz,
    active_run_id uuid,
    attempt_count integer,
    consecutive_failures integer,
    completed_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
    SELECT * FROM careerops._claim_crawler_source(
        p_registry_id, p_source_id, p_worker_id, p_lease_token, p_lease_seconds
    );
$$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.complete_crawler_source_run(
    p_source_row_id uuid,
    p_worker_id text,
    p_lease_token uuid,
    p_cursor text,
    p_result text,
    p_output_manifest_sha256 text
)
RETURNS TABLE (run_id uuid, source_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_source careerops.crawler_source_registry_sources%ROWTYPE;
    v_cursor_after text;
BEGIN
    IF p_worker_id IS NULL OR p_worker_id !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
        RAISE EXCEPTION 'crawler source worker_id is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_result IS NULL OR btrim(p_result) = '' OR char_length(p_result) > 160
       OR p_result LIKE 'failed:%' THEN
        RAISE EXCEPTION 'crawler source completion result is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_cursor IS NOT NULL AND char_length(p_cursor) > 160 THEN
        RAISE EXCEPTION 'crawler source cursor is too long' USING ERRCODE = '22023';
    END IF;
    IF p_output_manifest_sha256 IS NOT NULL
       AND p_output_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'crawler source output_manifest_sha256 is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT s.*
    INTO v_source
    FROM careerops.crawler_source_registry_sources AS s
    WHERE s.id = p_source_row_id
      AND s.lease_owner = p_worker_id
      AND s.lease_token = p_lease_token
      AND s.lease_until >= v_now
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'crawler source completion did not match an active lease'
            USING ERRCODE = '55000';
    END IF;
    IF v_source.canonical_ingestion_policy = 'canonical_job_ingestion'
       AND p_output_manifest_sha256 IS NULL THEN
        RAISE EXCEPTION
            'canonical crawler source completion requires output manifest evidence'
            USING ERRCODE = '22023';
    END IF;

    v_cursor_after := COALESCE(p_cursor, v_source.cursor);

    UPDATE careerops.crawler_source_registry_sources AS s
    SET cursor = v_cursor_after,
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        active_run_id = NULL,
        attempt_count = 0,
        consecutive_failures = 0,
        completed_at = v_now,
        last_run_at = v_now,
        next_run_at = v_now + make_interval(secs => v_source.cadence_seconds),
        last_result = p_result,
        last_error = NULL,
        updated_at = v_now
    WHERE s.id = v_source.id;

    INSERT INTO careerops.crawler_source_runs (
        run_id, source_row_id, registry_id, source_id, worker_id, lease_token,
        event_kind, cursor_before, cursor_after, result, error, output_manifest_sha256,
        attempt_number, claimed_at, lease_until, completed_at
    )
    VALUES (
        v_source.active_run_id, v_source.id, v_source.registry_id, v_source.source_id,
        p_worker_id, p_lease_token, 'succeeded', v_source.cursor, v_cursor_after, p_result,
        NULL, p_output_manifest_sha256, v_source.attempt_count, v_source.last_run_at,
        v_source.lease_until, v_now
    );

    RETURN QUERY SELECT v_source.active_run_id, v_source.source_id;
END;
$$
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.fail_crawler_source_run(
    p_source_row_id uuid,
    p_worker_id text,
    p_lease_token uuid,
    p_error text
)
RETURNS TABLE (run_id uuid, source_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_source careerops.crawler_source_registry_sources%ROWTYPE;
    v_attempt integer;
    v_retry_seconds integer;
BEGIN
    IF p_worker_id IS NULL OR p_worker_id !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
        RAISE EXCEPTION 'crawler source worker_id is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_error IS NULL OR btrim(p_error) = '' OR char_length(p_error) > 500 THEN
        RAISE EXCEPTION 'crawler source failure error is required' USING ERRCODE = '22023';
    END IF;

    SELECT s.*
    INTO v_source
    FROM careerops.crawler_source_registry_sources AS s
    WHERE s.id = p_source_row_id
      AND s.lease_owner = p_worker_id
      AND s.lease_token = p_lease_token
      AND s.lease_until >= v_now
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'crawler source failure did not match an active lease'
            USING ERRCODE = '55000';
    END IF;

    v_attempt := v_source.attempt_count + 1;
    IF v_attempt <= v_source.retry_rounds THEN
        v_retry_seconds := LEAST(3600, 30 * (2 ^ GREATEST(v_attempt - 1, 0))::integer);
    ELSE
        v_retry_seconds := v_source.cadence_seconds;
    END IF;

    UPDATE careerops.crawler_source_registry_sources AS s
    SET lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        active_run_id = NULL,
        attempt_count = v_attempt,
        consecutive_failures = v_source.consecutive_failures + 1,
        last_run_at = v_now,
        next_run_at = v_now + make_interval(secs => v_retry_seconds),
        last_result = 'failed',
        last_error = left(p_error, 1000),
        updated_at = v_now
    WHERE s.id = v_source.id;

    INSERT INTO careerops.crawler_source_runs (
        run_id, source_row_id, registry_id, source_id, worker_id, lease_token,
        event_kind, cursor_before, cursor_after, result, error, output_manifest_sha256,
        attempt_number, claimed_at, lease_until, completed_at
    )
    VALUES (
        v_source.active_run_id, v_source.id, v_source.registry_id, v_source.source_id,
        p_worker_id, p_lease_token, 'failed', v_source.cursor, NULL, 'failed',
        left(p_error, 1000), NULL, v_attempt, v_source.last_run_at, v_source.lease_until, v_now
    );

    RETURN QUERY SELECT v_source.active_run_id, v_source.source_id;
END;
$$
"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops._claim_crawler_source("
            "uuid, text, text, uuid, integer) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.claim_due_crawler_source("
            "uuid, text, uuid, integer) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.claim_crawler_source("
            "uuid, text, text, uuid, integer) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.complete_crawler_source_run("
            "uuid, text, uuid, text, text, text) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.fail_crawler_source_run("
            "uuid, text, uuid, text) FROM PUBLIC"
        )
    )


def _drop_crawler_scheduler_functions() -> None:
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.fail_crawler_source_run(uuid, text, uuid, text)")
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS careerops.complete_crawler_source_run("
            "uuid, text, uuid, text, text, text)"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS careerops.claim_crawler_source("
            "uuid, text, text, uuid, integer)"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS careerops.claim_due_crawler_source(uuid, text, uuid, integer)"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS careerops._claim_crawler_source("
            "uuid, text, text, uuid, integer)"
        )
    )


def _install_crawler_registry_validation_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.crawler_source_controls_are_safe(
    p_adapter text,
    p_budget jsonb,
    p_rate_limits jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    WITH budget_bounds(key, min_value, max_value, int_only) AS (
        SELECT v.key, v.min_value, v.max_value, v.int_only
        FROM (
            VALUES
                ('recruitment.sitemap_discovery', 'timeout_seconds', 1.0, 60.0, false),
                ('recruitment.sitemap_discovery', 'max_sitemap_bytes', 1.0, 10000000.0, true),
                ('recruitment.sitemap_discovery', 'retry_rounds', 0.0, 4.0, true),
                ('recruitment.commoncrawl_discovery', 'timeout_seconds', 1.0, 60.0, false),
                ('recruitment.commoncrawl_discovery', 'max_indexes', 1.0, 3.0, true),
                ('recruitment.commoncrawl_discovery', 'max_results_per_query', 1.0, 500.0, true),
                ('recruitment.commoncrawl_discovery', 'max_discovered_urls', 1.0, 50000.0, true),
                ('recruitment.commoncrawl_discovery', 'retry_rounds', 0.0, 4.0, true),
                ('recruitment.public_ats_feed', 'timeout_seconds', 1.0, 60.0, false),
                ('recruitment.public_ats_feed', 'max_feed_bytes', 1.0, 10000000.0, true),
                ('recruitment.public_ats_feed', 'retry_rounds', 0.0, 4.0, true),
                ('recruitment.page_collection', 'timeout_seconds', 1.0, 60.0, false),
                ('recruitment.page_collection', 'max_stored_bytes', 1.0, 10737418240.0, true),
                ('recruitment.page_collection', 'max_response_bytes', 1.0, 5000000.0, true),
                ('recruitment.page_collection', 'depth', 0.0, 1.0, true),
                ('recruitment.page_collection', 'retry_rounds', 0.0, 4.0, true)
        ) AS v(adapter, key, min_value, max_value, int_only)
        WHERE adapter = p_adapter
    ),
    rate_bounds(key, min_value, max_value, int_only) AS (
        SELECT v.key, v.min_value, v.max_value, v.int_only
        FROM (
            VALUES
                ('recruitment.sitemap_discovery', 'concurrency', 1.0, 8.0, true),
                ('recruitment.commoncrawl_discovery', 'concurrency', 1.0, 4.0, true),
                (
                    'recruitment.commoncrawl_discovery',
                    'provider_circuit_breaker_failures',
                    1.0,
                    10.0,
                    true
                ),
                ('recruitment.commoncrawl_discovery', 'retry_backoff_seconds', 0.1, 5.0, false),
                (
                    'recruitment.commoncrawl_discovery',
                    'max_retry_backoff_seconds',
                    0.1,
                    30.0,
                    false
                ),
                ('recruitment.public_ats_feed', 'concurrency', 1.0, 8.0, true),
                ('recruitment.page_collection', 'concurrency', 1.0, 8.0, true),
                ('recruitment.page_collection', 'max_concurrency_per_host', 1.0, 2.0, true),
                ('recruitment.page_collection', 'host_failure_cooldown_threshold', 1.0, 10.0, true),
                ('recruitment.page_collection', 'host_failure_cooldown_seconds', 0.0, 300.0, false)
        ) AS v(adapter, key, min_value, max_value, int_only)
        WHERE adapter = p_adapter
    ),
    budget_values AS (
        SELECT item.key,
               item.value,
               item.value #>> '{}' AS raw_value,
               bounds.min_value,
               bounds.max_value,
               bounds.int_only
        FROM jsonb_each(p_budget) AS item(key, value)
        LEFT JOIN budget_bounds AS bounds ON bounds.key = item.key
    ),
    rate_values AS (
        SELECT item.key,
               item.value,
               item.value #>> '{}' AS raw_value,
               bounds.min_value,
               bounds.max_value,
               bounds.int_only
        FROM jsonb_each(p_rate_limits) AS item(key, value)
        LEFT JOIN rate_bounds AS bounds ON bounds.key = item.key
    )
    SELECT p_adapter IN (
            'recruitment.sitemap_discovery',
            'recruitment.commoncrawl_discovery',
            'recruitment.public_ats_feed',
            'recruitment.page_collection'
       )
       AND jsonb_typeof(p_budget) = 'object'
       AND jsonb_typeof(p_rate_limits) = 'object'
       AND (SELECT count(*) FROM budget_values) = (SELECT count(*) FROM budget_bounds)
       AND (SELECT count(*) FROM rate_values) = (SELECT count(*) FROM rate_bounds)
       AND NOT EXISTS (
           SELECT 1
           FROM budget_values
           WHERE min_value IS NULL
              OR jsonb_typeof(value) <> 'number'
              OR (int_only AND raw_value !~ '^-?[0-9]+$')
              OR raw_value::numeric < min_value
              OR raw_value::numeric > max_value
       )
       AND NOT EXISTS (
           SELECT 1
           FROM rate_values
           WHERE min_value IS NULL
              OR jsonb_typeof(value) <> 'number'
              OR (int_only AND raw_value !~ '^-?[0-9]+$')
              OR raw_value::numeric < min_value
              OR raw_value::numeric > max_value
       );
$$
"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops.crawler_source_controls_are_safe("
            "text, jsonb, jsonb) FROM PUBLIC"
        )
    )


def _drop_crawler_registry_validation_functions() -> None:
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS careerops.crawler_source_controls_are_safe(text, jsonb, jsonb)"
        )
    )


def _install_crawler_ingestion_evidence_guard() -> None:
    op.execute(
        sa.text(
            """
CREATE OR REPLACE FUNCTION careerops.enforce_crawler_job_ingestion_evidence_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = careerops, pg_temp
AS $$
DECLARE
    v_output_artifact_sha256 text;
BEGIN
    v_output_artifact_sha256 := NEW.provenance_json ->> 'output_artifact_sha256';
    IF v_output_artifact_sha256 IS NULL
       OR v_output_artifact_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'crawler ingestion evidence requires output_artifact_sha256 provenance'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM careerops.crawler_source_runs AS run_event
        JOIN careerops.crawler_source_registry_sources AS source_row
          ON source_row.id = NEW.source_row_id
        WHERE run_event.event_id = NEW.run_event_id
          AND run_event.run_id = NEW.run_id
          AND run_event.source_row_id = NEW.source_row_id
          AND run_event.event_kind = 'succeeded'
          AND run_event.output_manifest_sha256 IS NOT NULL
          AND run_event.output_manifest_sha256 = v_output_artifact_sha256
          AND source_row.canonical_ingestion_policy = 'canonical_job_ingestion'
          AND NEW.dedupe_policy = source_row.dedupe_policy
          AND NEW.provenance_json ->> 'provenance_policy' = source_row.provenance_policy
    ) THEN
        RAISE EXCEPTION
            'crawler ingestion evidence must bind matching source policy and succeeded run'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$
"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION "
            "careerops.enforce_crawler_job_ingestion_evidence_insert() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_crawler_job_ingestion_evidence_insert_guard "
            "BEFORE INSERT ON careerops.crawler_job_ingestion_evidence "
            "FOR EACH ROW EXECUTE FUNCTION "
            "careerops.enforce_crawler_job_ingestion_evidence_insert()"
        )
    )


def _drop_crawler_ingestion_evidence_guard() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_crawler_job_ingestion_evidence_insert_guard "
            "ON careerops.crawler_job_ingestion_evidence"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_crawler_job_ingestion_evidence_insert()")
    )


def upgrade() -> None:
    _install_crawler_registry_validation_functions()
    op.create_table(
        "crawler_source_registries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("manifest_path", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("registry_sha256", sa.String(64), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "btrim(kind) <> ''", name=op.f("ck_crawler_source_registries_kind_nonempty")
        ),
        sa.CheckConstraint(
            "btrim(manifest_path) <> ''",
            name=op.f("ck_crawler_source_registries_manifest_path_nonempty"),
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_source_registries_manifest_sha256_format"),
        ),
        sa.CheckConstraint(
            "registry_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_source_registries_registry_sha256_format"),
        ),
        sa.CheckConstraint(
            "source_count >= 1", name=op.f("ck_crawler_source_registries_source_count_positive")
        ),
        sa.CheckConstraint(
            "btrim(created_by) <> ''", name=op.f("ck_crawler_source_registries_created_by_nonempty")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'",
            name=op.f("ck_crawler_source_registries_manifest_json_object"),
        ),
        sa.CheckConstraint(
            "generated_at <= created_at + interval '1 day'",
            name=op.f("ck_crawler_source_registries_generated_at_reasonable"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_source_registry_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "registry_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawler_source_registries.id", ondelete="RESTRICT"),
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
        sa.Column(
            "source_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
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
            "registry_id", "source_id", name=op.f("uq_crawler_source_registry_source")
        ),
        sa.CheckConstraint(
            "source_id ~ '^[a-z][a-z0-9-]{0,63}$'",
            name=op.f("ck_crawler_source_registry_sources_source_id_format"),
        ),
        sa.CheckConstraint(
            "btrim(adapter) <> ''", name=op.f("ck_crawler_source_registry_sources_adapter_nonempty")
        ),
        sa.CheckConstraint(
            "btrim(input_artifact) <> ''",
            name=op.f("ck_crawler_source_registry_sources_input_artifact_nonempty"),
        ),
        sa.CheckConstraint(
            "btrim(output_dir) <> ''",
            name=op.f("ck_crawler_source_registry_sources_output_dir_nonempty"),
        ),
        sa.CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_source_registry_sources_command_sha256_format"),
        ),
        sa.CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_source_registry_sources_source_sha256_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dependency_source_ids) = 'array'",
            name=op.f("ck_crawler_source_registry_sources_dependency_source_ids_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dependency_artifacts) = 'array'",
            name=op.f("ck_crawler_source_registry_sources_dependency_artifacts_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_json) = 'object'",
            name=op.f("ck_crawler_source_registry_sources_source_json_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget_json) = 'object'",
            name=op.f("ck_crawler_source_registry_sources_budget_json_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rate_limit_json) = 'object'",
            name=op.f("ck_crawler_source_registry_sources_rate_limit_json_object"),
        ),
        sa.CheckConstraint(
            "careerops.crawler_source_controls_are_safe(adapter, budget_json, rate_limit_json)",
            name=op.f("ck_crawler_source_registry_sources_controls_adapter_safe"),
        ),
        sa.CheckConstraint(
            "cursor IS NULL OR char_length(cursor) <= 160",
            name=op.f("ck_crawler_source_registry_sources_cursor_length"),
        ),
        sa.CheckConstraint(
            "cadence_seconds BETWEEN 1 AND 604800",
            name=op.f("ck_crawler_source_registry_sources_cadence_seconds_bounds"),
        ),
        sa.CheckConstraint(
            "retry_rounds BETWEEN 0 AND 10",
            name=op.f("ck_crawler_source_registry_sources_retry_rounds_bounds"),
        ),
        sa.CheckConstraint(
            "adapter IN ('recruitment.sitemap_discovery', "
            "'recruitment.commoncrawl_discovery', 'recruitment.public_ats_feed', "
            "'recruitment.page_collection')",
            name=op.f("ck_crawler_source_registry_sources_adapter_allowlist"),
        ),
        sa.CheckConstraint(
            "robots_terms_policy IN ('respect', 'review_required', 'blocked')",
            name=op.f("ck_crawler_source_registry_sources_robots_terms_policy_values"),
        ),
        sa.CheckConstraint(
            "canonical_ingestion_policy IN ('canonical_job_ingestion', 'dedupe_only')",
            name=op.f("ck_crawler_source_registry_sources_canonical_ingestion_policy_values"),
        ),
        sa.CheckConstraint(
            "canonical_ingestion_policy <> 'canonical_job_ingestion' "
            "OR adapter = 'recruitment.public_ats_feed'",
            name=op.f("ck_crawler_source_registry_sources_canonical_ingestion_adapter"),
        ),
        sa.CheckConstraint(
            "provenance_policy IN ('preserve', 'compact')",
            name=op.f("ck_crawler_source_registry_sources_provenance_policy_values"),
        ),
        sa.CheckConstraint(
            "dedupe_policy IN ('hash', 'url', 'hybrid')",
            name=op.f("ck_crawler_source_registry_sources_dedupe_policy_values"),
        ),
        sa.CheckConstraint(
            "input_artifact ~ '^[A-Za-z0-9._/-]+$' "
            "AND left(input_artifact, 1) <> '/' "
            "AND input_artifact !~ '(^|/)\\.\\.(/|$)'",
            name=op.f("ck_crawler_source_registry_sources_input_artifact_safe_relative"),
        ),
        sa.CheckConstraint(
            "output_dir ~ '^[A-Za-z0-9._/-]+$' "
            "AND left(output_dir, 1) <> '/' "
            "AND output_dir !~ '(^|/)\\.\\.(/|$)'",
            name=op.f("ck_crawler_source_registry_sources_output_dir_safe_relative"),
        ),
        sa.CheckConstraint(
            "next_run_at IS NULL OR last_run_at IS NULL OR next_run_at >= last_run_at",
            name=op.f("ck_crawler_source_registry_sources_schedule_state_temporal_order"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND consecutive_failures >= 0",
            name=op.f("ck_crawler_source_registry_sources_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL "
            "AND active_run_id IS NULL) OR "
            "(lease_owner IS NOT NULL AND btrim(lease_owner) <> '' "
            "AND lease_token IS NOT NULL AND lease_until IS NOT NULL "
            "AND active_run_id IS NOT NULL)",
            name=op.f("ck_crawler_source_registry_sources_lease_state_consistent"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_source_runs",
        sa.Column(
            "event_id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_row_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawler_source_registry_sources.id", ondelete="RESTRICT"),
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
            name=op.f("ck_crawler_source_runs_event_kind_values"),
        ),
        sa.CheckConstraint(
            "btrim(source_id) <> '' AND btrim(worker_id) <> ''",
            name=op.f("ck_crawler_source_runs_identifiers_nonempty"),
        ),
        sa.CheckConstraint(
            "attempt_number >= 0", name=op.f("ck_crawler_source_runs_attempt_number_nonnegative")
        ),
        sa.CheckConstraint(
            "output_manifest_sha256 IS NULL OR output_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_source_runs_output_manifest_sha256_format"),
        ),
        sa.CheckConstraint(
            "(event_kind = 'claimed' AND completed_at IS NULL) OR "
            "(event_kind IN ('succeeded', 'failed', 'expired') AND completed_at IS NOT NULL)",
            name=op.f("ck_crawler_source_runs_completion_event_state"),
        ),
        sa.CheckConstraint(
            "NOT (event_kind IN ('failed', 'expired') AND cursor_after IS NOT NULL)",
            name=op.f("ck_crawler_source_runs_failed_cursor_after_null"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_job_deduplication_keys",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("dedupe_policy", sa.Text(), nullable=False),
        sa.Column("dedupe_key_sha256", sa.String(64), nullable=False),
        sa.Column(
            "company_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.companies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="RESTRICT"),
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
            name=op.f("uq_crawler_job_deduplication_keys_policy_key"),
        ),
        sa.CheckConstraint(
            "dedupe_policy IN ('hash', 'url', 'hybrid')",
            name=op.f("ck_crawler_job_deduplication_keys_policy_values"),
        ),
        sa.CheckConstraint(
            "dedupe_key_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_job_deduplication_keys_key_format"),
        ),
        sa.CheckConstraint(
            "btrim(rule) <> '' AND rule <> 'semantic_similarity_only' "
            "AND btrim(algorithm_version) <> ''",
            name=op.f("ck_crawler_job_deduplication_keys_rule_nonsemantic"),
        ),
        schema="careerops",
    )
    op.create_table(
        "crawler_job_ingestion_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "source_row_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawler_source_registry_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "run_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawler_source_runs.event_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_posting_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.job_postings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_posting_version_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.job_posting_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "canonical_job_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.canonical_jobs.id", ondelete="RESTRICT"),
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
            name=op.f("uq_crawler_job_ingestion_evidence_run_version_key"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_job_ingestion_evidence_content_hash_format"),
        ),
        sa.CheckConstraint(
            "dedupe_key_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_crawler_job_ingestion_evidence_dedupe_key_sha256_format"),
        ),
        sa.CheckConstraint(
            "dedupe_policy IN ('hash', 'url', 'hybrid')",
            name=op.f("ck_crawler_job_ingestion_evidence_policy_values"),
        ),
        sa.CheckConstraint(
            "btrim(source_url) <> '' AND btrim(parser_version) <> '' "
            "AND btrim(dedupe_rule) <> '' AND dedupe_rule <> 'semantic_similarity_only'",
            name=op.f("ck_crawler_job_ingestion_evidence_nonempty_nonsemantic"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provenance_json) = 'object'",
            name=op.f("ck_crawler_job_ingestion_evidence_provenance_object"),
        ),
        schema="careerops",
    )
    op.create_index(
        op.f("ix_crawler_source_registry_sources_due"),
        "crawler_source_registry_sources",
        ["enabled", "next_run_at", "lease_until", "source_id"],
        postgresql_where=sa.text("enabled IS TRUE AND robots_terms_policy = 'respect'"),
        schema="careerops",
    )
    op.create_index(
        op.f("ix_crawler_source_runs_run_id"),
        "crawler_source_runs",
        ["run_id", "event_kind", "created_at"],
        schema="careerops",
    )
    op.create_index(
        op.f("ix_crawler_job_ingestion_evidence_run"),
        "crawler_job_ingestion_evidence",
        ["run_id", "source_row_id"],
        schema="careerops",
    )
    _install_crawler_scheduler_functions()
    _install_crawler_ingestion_evidence_guard()
    _install_append_only_guards()
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role("careerops_readonly", _READONLY_REVOKES)
    _run_for_role("careerops_api", _API_REVOKES)
    _drop_crawler_scheduler_functions()
    _drop_crawler_ingestion_evidence_guard()
    _drop_append_only_guards()
    op.drop_index(
        op.f("ix_crawler_job_ingestion_evidence_run"),
        table_name="crawler_job_ingestion_evidence",
        schema="careerops",
    )
    op.drop_index(
        op.f("ix_crawler_source_runs_run_id"),
        table_name="crawler_source_runs",
        schema="careerops",
    )
    op.drop_index(
        op.f("ix_crawler_source_registry_sources_due"),
        table_name="crawler_source_registry_sources",
        schema="careerops",
    )
    op.drop_table("crawler_job_ingestion_evidence", schema="careerops")
    op.drop_table("crawler_job_deduplication_keys", schema="careerops")
    op.drop_table("crawler_source_runs", schema="careerops")
    op.drop_table("crawler_source_registry_sources", schema="careerops")
    op.drop_table("crawler_source_registries", schema="careerops")
    _drop_crawler_registry_validation_functions()
