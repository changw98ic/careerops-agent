"""add owner-bound versioned candidate profiles and materials

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-22
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "candidate_material_bundles",
    "candidate_material_versions",
    "candidate_profile_decisions",
    "candidate_profile_versions",
)

_TABLES = APPEND_ONLY_TABLES
_RUNTIME_ROLES = (
    "careerops_api",
    "careerops_workflow",
    "careerops_mailbox",
    "careerops_outbox",
    "careerops_mail_sender",
    "careerops_greenhouse_sender",
    "careerops_retention",
    "careerops_readonly",
)

_IMPORT_FUNCTION = "careerops.candidate_profile_import"
_IMPORT_SIGNATURE = "(uuid,uuid,text,uuid,uuid,jsonb,jsonb,jsonb,text,text,text,text,text,text)"
_DECIDE_FUNCTION = "careerops.candidate_profile_decide"
_DECIDE_SIGNATURE = "(uuid,uuid,uuid,text,text,text,text,text)"
_GET_FUNCTION = "careerops.candidate_profile_get"
_GET_SIGNATURE = "(uuid,uuid,uuid)"
_LIST_FUNCTION = "careerops.candidate_profile_list"
_LIST_SIGNATURE = "(uuid,uuid,integer)"
_APPROVED_FUNCTION = "careerops.candidate_profile_get_approved"
_APPROVED_SIGNATURE = "(uuid,uuid)"
_PUBLIC_RECORD_FUNCTION = "careerops._candidate_profile_public_record"
_PUBLIC_RECORD_SIGNATURE = "(uuid)"
_MATERIALS_ACTIVE_FUNCTION = "careerops._candidate_profile_materials_active"
_MATERIALS_ACTIVE_SIGNATURE = "(uuid)"
_CANDIDATE_OWNER_FUNCTION = "careerops.fill_candidate_owner"
_CANDIDATE_OWNER_SIGNATURE = "()"

_API_GRANTS = (
    f"GRANT EXECUTE ON FUNCTION {_IMPORT_FUNCTION}{_IMPORT_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_DECIDE_FUNCTION}{_DECIDE_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_GET_FUNCTION}{_GET_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_LIST_FUNCTION}{_LIST_SIGNATURE} TO careerops_api",
    f"GRANT EXECUTE ON FUNCTION {_APPROVED_FUNCTION}{_APPROVED_SIGNATURE} TO careerops_api",
)
_WORKFLOW_GRANTS = (
    f"GRANT EXECUTE ON FUNCTION {_APPROVED_FUNCTION}{_APPROVED_SIGNATURE} TO careerops_workflow",
)
_READONLY_GRANTS = (
    "GRANT SELECT ON careerops.candidate_material_bundles, "
    "careerops.candidate_material_versions, careerops.candidate_profile_versions, "
    "careerops.candidate_profile_decisions TO careerops_readonly",
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


def _install_candidate_ownership() -> None:
    op.add_column(
        "candidates",
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        schema="careerops",
    )
    op.execute(
        sa.text(
            """
DO $block$
DECLARE
    v_ambiguous_candidate uuid;
    v_default_owner uuid;
BEGIN
    SELECT binding.candidate_id
    INTO v_ambiguous_candidate
    FROM (
        SELECT candidate_id, owner_user_id FROM careerops.gmail_accounts
        UNION ALL
        SELECT candidate_id, owner_user_id FROM careerops.gmail_send_accounts
        UNION ALL
        SELECT candidate_id, owner_user_id FROM careerops.greenhouse_submit_accounts
    ) AS binding
    GROUP BY binding.candidate_id
    HAVING count(DISTINCT binding.owner_user_id) > 1
    LIMIT 1;
    IF v_ambiguous_candidate IS NOT NULL THEN
        RAISE EXCEPTION 'candidate has conflicting owner bindings: %', v_ambiguous_candidate
            USING ERRCODE = '23514';
    END IF;

    UPDATE careerops.candidates AS candidate
    SET owner_user_id = binding.owner_user_id
    FROM (
        SELECT DISTINCT ON (candidate_id) candidate_id, owner_user_id
        FROM (
            SELECT candidate_id, owner_user_id FROM careerops.gmail_accounts
            UNION ALL
            SELECT candidate_id, owner_user_id FROM careerops.gmail_send_accounts
            UNION ALL
            SELECT candidate_id, owner_user_id FROM careerops.greenhouse_submit_accounts
        ) AS all_bindings
        ORDER BY candidate_id, owner_user_id
    ) AS binding
    WHERE candidate.id = binding.candidate_id;

    SELECT id INTO v_default_owner
    FROM careerops.console_users
    WHERE disabled_at IS NULL
    ORDER BY created_at, id
    LIMIT 1;

    UPDATE careerops.candidates
    SET owner_user_id = v_default_owner
    WHERE owner_user_id IS NULL AND v_default_owner IS NOT NULL;

    IF EXISTS (SELECT 1 FROM careerops.candidates WHERE owner_user_id IS NULL) THEN
        RAISE EXCEPTION 'candidate owner backfill requires one active console user'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT candidate_id, owner_user_id FROM careerops.gmail_accounts
            UNION ALL
            SELECT candidate_id, owner_user_id FROM careerops.gmail_send_accounts
            UNION ALL
            SELECT candidate_id, owner_user_id FROM careerops.greenhouse_submit_accounts
        ) AS binding
        JOIN careerops.candidates AS candidate ON candidate.id = binding.candidate_id
        WHERE candidate.owner_user_id <> binding.owner_user_id
    ) THEN
        RAISE EXCEPTION 'candidate owner backfill does not match a channel binding'
            USING ERRCODE = '23514';
    END IF;
END
$block$;
"""
        )
    )
    op.alter_column(
        "candidates",
        "owner_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
        schema="careerops",
    )
    op.create_foreign_key(
        "fk_candidates_owner_user_id_console_users",
        "candidates",
        "console_users",
        ["owner_user_id"],
        ["id"],
        source_schema="careerops",
        referent_schema="careerops",
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_candidates_owner_id",
        "candidates",
        ["owner_user_id", "id"],
        schema="careerops",
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.fill_candidate_owner()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_active_owner uuid;
BEGIN
    SELECT id INTO v_active_owner
    FROM careerops.console_users
    WHERE disabled_at IS NULL
    ORDER BY created_at, id
    LIMIT 1;
    IF v_active_owner IS NULL THEN
        RAISE EXCEPTION 'candidate insert requires one active console user'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.owner_user_id IS NULL THEN
        NEW.owner_user_id := v_active_owner;
    ELSIF NEW.owner_user_id <> v_active_owner THEN
        RAISE EXCEPTION 'candidate owner does not match the active console user'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_CANDIDATE_OWNER_FUNCTION}{_CANDIDATE_OWNER_SIGNATURE} FROM PUBLIC"))
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_candidates_fill_owner "
            "BEFORE INSERT ON careerops.candidates FOR EACH ROW "
            "EXECUTE FUNCTION careerops.fill_candidate_owner()"
        )
    )
    for table_name in ("gmail_accounts", "gmail_send_accounts", "greenhouse_submit_accounts"):
        constraint_name = f"fk_{table_name}_owner_candidate"
        op.execute(
            sa.text(
                f"ALTER TABLE careerops.{table_name} ADD CONSTRAINT {constraint_name} "
                "FOREIGN KEY (owner_user_id, candidate_id) "
                "REFERENCES careerops.candidates (owner_user_id, id) "
                "ON DELETE RESTRICT NOT VALID"
            )
        )
        op.execute(
            sa.text(f"ALTER TABLE careerops.{table_name} VALIDATE CONSTRAINT {constraint_name}")
        )


def _create_tables() -> None:
    op.create_table(
        "candidate_material_bundles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.String(48), nullable=False),
        sa.Column("canonicalization_version", sa.String(48), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("material_count", sa.SmallInteger(), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "candidate_id"],
            ["careerops.candidates.owner_user_id", "careerops.candidates.id"],
            name="fk_candidate_material_bundles_owner_candidate",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "owner_user_id", "candidate_id", name="uq_candidate_material_bundles_identity"),
        sa.UniqueConstraint("id", "owner_user_id", "candidate_id", "bundle_sha256", name="uq_candidate_material_bundles_hash_identity"),
        sa.UniqueConstraint("owner_user_id", "candidate_id", "version", name="uq_candidate_material_bundles_candidate_version"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("schema_version = 'candidate-material-bundle.v1'", name="schema_version_exact"),
        sa.CheckConstraint("canonicalization_version = 'careerops-json-c14n.v1'", name="canonicalization_version_exact"),
        sa.CheckConstraint("jsonb_typeof(manifest_json) = 'array'", name="manifest_json_array"),
        sa.CheckConstraint("material_count BETWEEN 1 AND 10", name="material_count_bounds"),
        sa.CheckConstraint("jsonb_array_length(manifest_json) = material_count", name="manifest_count_matches"),
        sa.CheckConstraint("bundle_sha256 ~ '^[a-f0-9]{64}$'", name="bundle_sha256_format"),
        schema="careerops",
    )
    op.create_table(
        "candidate_material_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("material_kind", sa.String(24), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("content_object_id", sa.Uuid(), sa.ForeignKey("careerops.content_objects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["bundle_id", "owner_user_id", "candidate_id"],
            ["careerops.candidate_material_bundles.id", "careerops.candidate_material_bundles.owner_user_id", "careerops.candidate_material_bundles.candidate_id"],
            name="fk_candidate_material_versions_bundle_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("bundle_id", "ordinal", name="uq_candidate_material_versions_bundle_ordinal"),
        sa.UniqueConstraint("bundle_id", "content_object_id", name="uq_candidate_material_versions_bundle_content_object"),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ordinal_bounds"),
        sa.CheckConstraint("material_kind IN ('resume', 'cover_letter', 'portfolio', 'certificate', 'other')", name="material_kind_values"),
        sa.CheckConstraint("btrim(label) <> '' AND char_length(label) <= 160", name="label_bounded"),
        sa.CheckConstraint("btrim(filename) <> '' AND char_length(filename) <= 255 AND position('/' in filename) = 0 AND position(chr(92) in filename) = 0 AND filename NOT IN ('.', '..')", name="filename_safe"),
        sa.CheckConstraint("btrim(media_type) <> ''", name="media_type_nonempty"),
        sa.CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'", name="sha256_format"),
        sa.CheckConstraint("object_key ~ '^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$' AND right(object_key, 64) = sha256", name="object_key_matches_sha256"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 10485760", name="byte_size_bounds"),
        schema="careerops",
    )
    op.create_table(
        "candidate_profile_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"), nullable=False),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id", "candidate_id"],
            ["careerops.candidates.owner_user_id", "careerops.candidates.id"],
            name="fk_candidate_profile_versions_owner_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_bundle_id", "owner_user_id", "candidate_id", "material_bundle_sha256"],
            ["careerops.candidate_material_bundles.id", "careerops.candidate_material_bundles.owner_user_id", "careerops.candidate_material_bundles.candidate_id", "careerops.candidate_material_bundles.bundle_sha256"],
            name="fk_candidate_profile_versions_bundle_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "owner_user_id", "candidate_id", "snapshot_sha256", name="uq_candidate_profile_versions_hash_identity"),
        sa.UniqueConstraint("owner_user_id", "candidate_id", "version", name="uq_candidate_profile_versions_candidate_version"),
        sa.UniqueConstraint("owner_user_id", "import_idempotency_key", name="uq_candidate_profile_versions_owner_idempotency"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("profile_schema_version = 'candidate-profile.v1'", name="profile_schema_version_exact"),
        sa.CheckConstraint("preferences_schema_version = 'candidate-job-preferences.v1'", name="preferences_schema_version_exact"),
        sa.CheckConstraint("snapshot_schema_version = 'candidate-profile-snapshot.v1'", name="snapshot_schema_version_exact"),
        sa.CheckConstraint("canonicalization_version = 'careerops-json-c14n.v1'", name="canonicalization_version_exact"),
        sa.CheckConstraint("jsonb_typeof(profile_json) = 'object'", name="profile_json_object"),
        sa.CheckConstraint("jsonb_typeof(preferences_json) = 'object'", name="preferences_json_object"),
        sa.CheckConstraint("material_bundle_sha256 ~ '^[a-f0-9]{64}$'", name="material_bundle_sha256_format"),
        sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint("import_idempotency_key ~ '^[A-Za-z0-9._:@/-]{1,160}$' AND trace_id ~ '^[A-Za-z0-9._:@/-]{1,160}$'", name="command_identifiers_bounded"),
        schema="careerops",
    )
    op.create_table(
        "candidate_profile_decisions",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_version_id", "owner_user_id", "candidate_id", "snapshot_sha256"],
            ["careerops.candidate_profile_versions.id", "careerops.candidate_profile_versions.owner_user_id", "careerops.candidate_profile_versions.candidate_id", "careerops.candidate_profile_versions.snapshot_sha256"],
            name="fk_candidate_profile_decisions_profile_identity",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_candidate_profile_decisions_owner_idempotency"),
        sa.CheckConstraint("reviewed_by_user_id = owner_user_id", name="reviewer_is_owner"),
        sa.CheckConstraint("decision IN ('approve', 'reject')", name="decision_values"),
        sa.CheckConstraint("btrim(reason) <> '' AND char_length(reason) <= 4000", name="reason_bounded"),
        sa.CheckConstraint("snapshot_sha256 ~ '^[a-f0-9]{64}$'", name="snapshot_sha256_format"),
        sa.CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'", name="request_sha256_format"),
        sa.CheckConstraint("idempotency_key ~ '^[A-Za-z0-9._:@/-]{1,160}$' AND trace_id ~ '^[A-Za-z0-9._:@/-]{1,160}$'", name="command_identifiers_bounded"),
        schema="careerops",
    )
    op.create_index(
        "ix_candidate_profile_versions_candidate_created_at",
        "candidate_profile_versions",
        ["owner_user_id", "candidate_id", "created_at"],
        schema="careerops",
    )
    op.create_index(
        "ix_candidate_profile_decisions_approved",
        "candidate_profile_decisions",
        ["owner_user_id", "candidate_id", sa.text("sequence DESC")],
        unique=False,
        schema="careerops",
        postgresql_where=sa.text("decision = 'approve'"),
    )
    op.create_index(
        "uq_candidate_material_versions_one_resume",
        "candidate_material_versions",
        ["bundle_id"],
        unique=True,
        schema="careerops",
        postgresql_where=sa.text("material_kind = 'resume'"),
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


def _install_read_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._candidate_profile_public_record(p_profile_version_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
SELECT jsonb_build_object(
    'owner_user_id', profile.owner_user_id,
    'candidate_id', profile.candidate_id,
    'profile_version_id', profile.id,
    'profile_version', profile.version,
    'material_bundle_id', bundle.id,
    'material_bundle_version', bundle.version,
    'profile', profile.profile_json,
    'preferences', profile.preferences_json,
    'materials', COALESCE((
        SELECT jsonb_agg(
            jsonb_build_object(
                'material_id', material.id,
                'content_object_id', material.content_object_id,
                'kind', material.material_kind,
                'label', material.label,
                'filename', material.filename,
                'media_type', material.media_type,
                'sha256', material.sha256,
                'object_key', material.object_key,
                'byte_size', material.byte_size
            ) ORDER BY material.ordinal
        )
        FROM careerops.candidate_material_versions AS material
        WHERE material.bundle_id = bundle.id
    ), '[]'::jsonb),
    'material_bundle_sha256', profile.material_bundle_sha256,
    'snapshot_sha256', profile.snapshot_sha256,
    'decision', decision.decision,
    'created_at', profile.created_at,
    'newly_created', false
)
FROM careerops.candidate_profile_versions AS profile
JOIN careerops.candidate_material_bundles AS bundle
  ON bundle.id = profile.material_bundle_id
 AND bundle.owner_user_id = profile.owner_user_id
 AND bundle.candidate_id = profile.candidate_id
 AND bundle.bundle_sha256 = profile.material_bundle_sha256
LEFT JOIN careerops.candidate_profile_decisions AS decision
  ON decision.profile_version_id = profile.id
 AND decision.owner_user_id = profile.owner_user_id
 AND decision.candidate_id = profile.candidate_id
 AND decision.snapshot_sha256 = profile.snapshot_sha256
WHERE profile.id = p_profile_version_id
$function$;
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._candidate_profile_materials_active(p_profile_version_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
SELECT COALESCE((
    SELECT
        count(material.id) = bundle.material_count
        AND count(material.id) > 0
        AND bool_and(
            material.id IS NOT NULL
            AND content.id IS NOT NULL
            AND blob.id IS NOT NULL
            AND content.classification = 'accepted_attachment'
            AND content.owner_resource_type = 'candidate'
            AND content.owner_resource_id = profile.candidate_id
            AND content.expired_at IS NULL
            AND content.retired_at IS NULL
            AND content.retention_until > CURRENT_TIMESTAMP
            AND blob.deletion_state = 'active'
            AND content.media_type = material.media_type
            AND blob.sha256 = material.sha256
            AND blob.object_key = material.object_key
            AND blob.byte_size = material.byte_size
        )
    FROM careerops.candidate_profile_versions AS profile
    JOIN careerops.candidate_material_bundles AS bundle
      ON bundle.id = profile.material_bundle_id
     AND bundle.owner_user_id = profile.owner_user_id
     AND bundle.candidate_id = profile.candidate_id
     AND bundle.bundle_sha256 = profile.material_bundle_sha256
    LEFT JOIN careerops.candidate_material_versions AS material
      ON material.bundle_id = bundle.id
    LEFT JOIN careerops.content_objects AS content
      ON content.id = material.content_object_id
    LEFT JOIN careerops.content_blobs AS blob
      ON blob.id = content.blob_id
    WHERE profile.id = p_profile_version_id
    GROUP BY bundle.material_count
), false)
$function$;
"""
        )
    )
    for function, signature in (
        (_PUBLIC_RECORD_FUNCTION, _PUBLIC_RECORD_SIGNATURE),
        (_MATERIALS_ACTIVE_FUNCTION, _MATERIALS_ACTIVE_SIGNATURE),
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {function}{signature} FROM PUBLIC"))


def _install_import_function() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.candidate_profile_import(
    p_actor_id uuid,
    p_candidate_id uuid,
    p_display_name text,
    p_profile_version_id uuid,
    p_material_bundle_id uuid,
    p_profile_json jsonb,
    p_preferences_json jsonb,
    p_materials_json jsonb,
    p_material_bundle_sha256 text,
    p_snapshot_sha256 text,
    p_material_bundle_canonical_json text,
    p_snapshot_canonical_json text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_existing careerops.candidate_profile_versions%ROWTYPE;
    v_request_sha256 text := encode(public.digest(jsonb_build_object(
        'actor_id', p_actor_id,
        'candidate_id', p_candidate_id,
        'display_name', p_display_name,
        'profile_version_id', p_profile_version_id,
        'material_bundle_id', p_material_bundle_id,
        'profile', p_profile_json,
        'preferences', p_preferences_json,
        'materials', p_materials_json,
        'material_bundle_sha256', p_material_bundle_sha256,
        'snapshot_sha256', p_snapshot_sha256,
        'material_bundle_canonical_json', p_material_bundle_canonical_json,
        'snapshot_canonical_json', p_snapshot_canonical_json,
        'idempotency_key', p_idempotency_key,
        'trace_id', p_trace_id
    )::text, 'sha256'), 'hex');
    v_material_bundle_version bigint;
    v_profile_version bigint;
    v_semantic_materials jsonb;
    v_expected_bundle_envelope jsonb;
    v_expected_snapshot_envelope jsonb;
    v_result jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_candidate_id IS NULL OR p_profile_version_id IS NULL
       OR p_material_bundle_id IS NULL
       OR p_display_name IS NULL OR btrim(p_display_name) = '' OR char_length(p_display_name) > 160
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:@/-]{1,160}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:@/-]{1,160}$'
       OR p_material_bundle_sha256 IS NULL OR p_material_bundle_sha256 !~ '^[a-f0-9]{64}$'
       OR p_snapshot_sha256 IS NULL OR p_snapshot_sha256 !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'candidate profile import command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM careerops.console_users
        WHERE id = p_actor_id AND disabled_at IS NULL
    ) THEN
        RAISE EXCEPTION 'candidate profile actor is unavailable' USING ERRCODE = '23503';
    END IF;

    SELECT * INTO v_existing
    FROM careerops.candidate_profile_versions
    WHERE owner_user_id = p_actor_id AND import_idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.request_sha256 <> v_request_sha256 THEN
            RAISE EXCEPTION 'candidate profile import idempotency conflict'
                USING ERRCODE = '23505';
        END IF;
        v_result := careerops._candidate_profile_public_record(v_existing.id);
        RETURN v_result || jsonb_build_object('newly_created', false);
    END IF;

    IF p_profile_json IS NULL OR jsonb_typeof(p_profile_json) <> 'object'
       OR p_profile_json->>'schema_version' <> 'candidate-profile.v1'
       OR jsonb_typeof(p_profile_json->'skills') <> 'array'
       OR jsonb_typeof(p_profile_json->'role_titles') <> 'array'
       OR jsonb_array_length(p_profile_json->'skills') + jsonb_array_length(p_profile_json->'role_titles') < 1
       OR (p_profile_json - ARRAY[
           'schema_version', 'skills', 'role_titles', 'seniority', 'years_experience',
           'industries', 'languages', 'work_authorization', 'requires_sponsorship'
       ]::text[]) <> '{}'::jsonb THEN
        RAISE EXCEPTION 'candidate profile document is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_preferences_json IS NULL OR jsonb_typeof(p_preferences_json) <> 'object'
       OR p_preferences_json->>'schema_version' <> 'candidate-job-preferences.v1'
       OR jsonb_typeof(p_preferences_json->'target_titles') <> 'array'
       OR jsonb_array_length(p_preferences_json->'target_titles') < 1
       OR (p_preferences_json - ARRAY[
           'schema_version', 'target_titles', 'required_skills', 'preferred_skills',
           'excluded_skills', 'required_keywords', 'preferred_keywords',
           'excluded_keywords', 'allowed_locations', 'excluded_locations', 'work_modes',
           'employment_types', 'seniority_levels', 'minimum_salary', 'salary_currency',
           'sponsorship_allowed', 'allowed_companies', 'excluded_companies',
           'allowed_industries', 'excluded_industries', 'minimum_match_score'
       ]::text[]) <> '{}'::jsonb THEN
        RAISE EXCEPTION 'candidate job preferences document is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_materials_json IS NULL OR jsonb_typeof(p_materials_json) <> 'array'
       OR jsonb_array_length(p_materials_json) NOT BETWEEN 1 AND 10 THEN
        RAISE EXCEPTION 'candidate material manifest is invalid' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_materials_json) AS material(item)
        WHERE jsonb_typeof(item) <> 'object'
           OR item->>'material_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR item->>'content_object_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR item->>'kind' NOT IN ('resume', 'cover_letter', 'portfolio', 'certificate', 'other')
           OR btrim(COALESCE(item->>'label', '')) = '' OR char_length(item->>'label') > 160
           OR btrim(COALESCE(item->>'filename', '')) = '' OR char_length(item->>'filename') > 255
           OR position('/' in item->>'filename') > 0
           OR position(chr(92) in item->>'filename') > 0
           OR btrim(COALESCE(item->>'media_type', '')) = ''
           OR item->>'sha256' !~ '^[a-f0-9]{64}$'
           OR item->>'object_key' !~ '^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$'
           OR right(item->>'object_key', 64) <> item->>'sha256'
           OR (item->>'byte_size') !~ '^[0-9]{1,8}$'
           OR (item->>'byte_size')::bigint NOT BETWEEN 1 AND 10485760
    ) THEN
        RAISE EXCEPTION 'candidate material manifest item is invalid' USING ERRCODE = '22023';
    END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(p_materials_json) AS material(item) WHERE item->>'kind' = 'resume') <> 1
       OR (SELECT count(DISTINCT item->>'material_id') FROM jsonb_array_elements(p_materials_json) AS material(item)) <> jsonb_array_length(p_materials_json)
       OR (SELECT count(DISTINCT item->>'content_object_id') FROM jsonb_array_elements(p_materials_json) AS material(item)) <> jsonb_array_length(p_materials_json) THEN
        RAISE EXCEPTION 'candidate material manifest requires unique ids and exactly one resume'
            USING ERRCODE = '23514';
    END IF;

    SELECT jsonb_agg(
        jsonb_build_object(
            'kind', item->>'kind',
            'label', item->>'label',
            'filename', item->>'filename',
            'media_type', item->>'media_type',
            'sha256', item->>'sha256',
            'byte_size', (item->>'byte_size')::bigint
        ) ORDER BY item->>'kind', item->>'sha256', item->>'filename', item->>'label',
                   item->>'media_type', (item->>'byte_size')::bigint
    ) INTO v_semantic_materials
    FROM jsonb_array_elements(p_materials_json) AS material(item);
    v_expected_bundle_envelope := jsonb_build_object(
        'schema_version', 'candidate-material-bundle.v1',
        'canonicalization_version', 'careerops-json-c14n.v1',
        'owner_user_id', p_actor_id::text,
        'candidate_id', p_candidate_id::text,
        'materials', v_semantic_materials
    );
    v_expected_snapshot_envelope := jsonb_build_object(
        'schema_version', 'candidate-profile-snapshot.v1',
        'canonicalization_version', 'careerops-json-c14n.v1',
        'owner_user_id', p_actor_id::text,
        'candidate_id', p_candidate_id::text,
        'profile', p_profile_json,
        'preferences', p_preferences_json,
        'material_bundle_sha256', p_material_bundle_sha256
    );
    BEGIN
        IF p_material_bundle_canonical_json::jsonb <> v_expected_bundle_envelope
           OR encode(public.digest(p_material_bundle_canonical_json, 'sha256'), 'hex') <> p_material_bundle_sha256
           OR p_snapshot_canonical_json::jsonb <> v_expected_snapshot_envelope
           OR encode(public.digest(p_snapshot_canonical_json, 'sha256'), 'hex') <> p_snapshot_sha256 THEN
            RAISE EXCEPTION 'candidate profile canonical hash chain is invalid'
                USING ERRCODE = '23514';
        END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'candidate profile canonical JSON is invalid' USING ERRCODE = '22023';
    END;

    INSERT INTO careerops.candidates (id, owner_user_id, display_name, created_at, updated_at)
    VALUES (p_candidate_id, p_actor_id, btrim(p_display_name), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    ON CONFLICT (id) DO NOTHING;
    PERFORM 1
    FROM careerops.candidates
    WHERE id = p_candidate_id AND owner_user_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'candidate is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    UPDATE careerops.candidates
    SET display_name = btrim(p_display_name), updated_at = CURRENT_TIMESTAMP
    WHERE id = p_candidate_id AND owner_user_id = p_actor_id;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_materials_json) AS material(item)
        LEFT JOIN careerops.content_objects AS content
          ON content.id = (item->>'content_object_id')::uuid
        LEFT JOIN careerops.content_blobs AS blob ON blob.id = content.blob_id
        WHERE content.id IS NULL OR blob.id IS NULL
           OR content.classification <> 'accepted_attachment'
           OR content.owner_resource_type <> 'candidate'
           OR content.owner_resource_id <> p_candidate_id
           OR content.expired_at IS NOT NULL OR content.retired_at IS NOT NULL
           OR content.retention_until <= CURRENT_TIMESTAMP
           OR blob.deletion_state <> 'active'
           OR content.media_type <> item->>'media_type'
           OR blob.sha256 <> item->>'sha256'
           OR blob.object_key <> item->>'object_key'
           OR blob.byte_size <> (item->>'byte_size')::bigint
    ) THEN
        RAISE EXCEPTION 'candidate material does not match an active owned CAS object'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(version), 0) + 1 INTO v_material_bundle_version
    FROM careerops.candidate_material_bundles
    WHERE owner_user_id = p_actor_id AND candidate_id = p_candidate_id;
    SELECT COALESCE(max(version), 0) + 1 INTO v_profile_version
    FROM careerops.candidate_profile_versions
    WHERE owner_user_id = p_actor_id AND candidate_id = p_candidate_id;

    INSERT INTO careerops.candidate_material_bundles (
        id, owner_user_id, candidate_id, version, schema_version,
        canonicalization_version, manifest_json, material_count, bundle_sha256
    ) VALUES (
        p_material_bundle_id, p_actor_id, p_candidate_id, v_material_bundle_version,
        'candidate-material-bundle.v1', 'careerops-json-c14n.v1', p_materials_json,
        jsonb_array_length(p_materials_json), p_material_bundle_sha256
    );
    INSERT INTO careerops.candidate_material_versions (
        id, bundle_id, owner_user_id, candidate_id, ordinal, material_kind, label,
        filename, media_type, content_object_id, sha256, object_key, byte_size
    )
    SELECT
        (item->>'material_id')::uuid, p_material_bundle_id, p_actor_id, p_candidate_id,
        ordinal::smallint, item->>'kind', item->>'label', item->>'filename',
        item->>'media_type', (item->>'content_object_id')::uuid, item->>'sha256',
        item->>'object_key', (item->>'byte_size')::bigint
    FROM jsonb_array_elements(p_materials_json) WITH ORDINALITY AS material(item, ordinal);
    INSERT INTO careerops.candidate_profile_versions (
        id, owner_user_id, candidate_id, version, profile_schema_version,
        preferences_schema_version, snapshot_schema_version, canonicalization_version,
        profile_json, preferences_json, material_bundle_id, material_bundle_sha256,
        snapshot_sha256, request_sha256, import_idempotency_key, trace_id
    ) VALUES (
        p_profile_version_id, p_actor_id, p_candidate_id, v_profile_version,
        'candidate-profile.v1', 'candidate-job-preferences.v1',
        'candidate-profile-snapshot.v1', 'careerops-json-c14n.v1', p_profile_json,
        p_preferences_json, p_material_bundle_id, p_material_bundle_sha256,
        p_snapshot_sha256, v_request_sha256, p_idempotency_key, p_trace_id
    );
    v_result := careerops._candidate_profile_public_record(p_profile_version_id);
    RETURN v_result || jsonb_build_object('newly_created', true);
END
$function$;
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_IMPORT_FUNCTION}{_IMPORT_SIGNATURE} FROM PUBLIC"))


def _install_decision_function() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.candidate_profile_decide(
    p_actor_id uuid,
    p_candidate_id uuid,
    p_profile_version_id uuid,
    p_snapshot_sha256 text,
    p_decision text,
    p_reason text,
    p_idempotency_key text,
    p_trace_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_profile careerops.candidate_profile_versions%ROWTYPE;
    v_existing careerops.candidate_profile_decisions%ROWTYPE;
    v_request_sha256 text := encode(public.digest(jsonb_build_object(
        'actor_id', p_actor_id,
        'candidate_id', p_candidate_id,
        'profile_version_id', p_profile_version_id,
        'snapshot_sha256', p_snapshot_sha256,
        'decision', p_decision,
        'reason', p_reason,
        'idempotency_key', p_idempotency_key,
        'trace_id', p_trace_id
    )::text, 'sha256'), 'hex');
    v_result jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_candidate_id IS NULL OR p_profile_version_id IS NULL
       OR p_snapshot_sha256 IS NULL OR p_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_decision NOT IN ('approve', 'reject')
       OR p_reason IS NULL OR btrim(p_reason) = '' OR char_length(p_reason) > 4000
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9._:@/-]{1,160}$'
       OR p_trace_id IS NULL OR p_trace_id !~ '^[A-Za-z0-9._:@/-]{1,160}$' THEN
        RAISE EXCEPTION 'candidate profile decision command is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM careerops.console_users
        WHERE id = p_actor_id AND disabled_at IS NULL
    ) THEN
        RAISE EXCEPTION 'candidate profile actor is unavailable' USING ERRCODE = '23503';
    END IF;
    PERFORM 1
    FROM careerops.candidates AS candidate
    WHERE candidate.id = p_candidate_id
      AND candidate.owner_user_id = p_actor_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'candidate is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    SELECT * INTO v_existing
    FROM careerops.candidate_profile_decisions
    WHERE owner_user_id = p_actor_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_existing.request_sha256 <> v_request_sha256 THEN
            RAISE EXCEPTION 'candidate profile decision idempotency conflict'
                USING ERRCODE = '23505';
        END IF;
        v_result := careerops._candidate_profile_public_record(v_existing.profile_version_id);
        RETURN v_result || jsonb_build_object('newly_created', false);
    END IF;

    SELECT * INTO v_profile
    FROM careerops.candidate_profile_versions
    WHERE id = p_profile_version_id
      AND owner_user_id = p_actor_id
      AND candidate_id = p_candidate_id
      AND snapshot_sha256 = p_snapshot_sha256
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'candidate profile snapshot is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    IF EXISTS (
        SELECT 1 FROM careerops.candidate_profile_decisions
        WHERE profile_version_id = p_profile_version_id
    ) THEN
        RAISE EXCEPTION 'candidate profile snapshot already has a decision'
            USING ERRCODE = '23514';
    END IF;
    IF p_decision = 'approve'
       AND NOT careerops._candidate_profile_materials_active(p_profile_version_id) THEN
        RAISE EXCEPTION 'candidate profile materials are not active for approval'
            USING ERRCODE = '23514';
    END IF;
    INSERT INTO careerops.candidate_profile_decisions (
        id, profile_version_id, owner_user_id, candidate_id, reviewed_by_user_id,
        decision, reason, snapshot_sha256, request_sha256, idempotency_key, trace_id
    ) VALUES (
        gen_random_uuid(), p_profile_version_id, p_actor_id, p_candidate_id, p_actor_id,
        p_decision, btrim(p_reason), p_snapshot_sha256, v_request_sha256,
        p_idempotency_key, p_trace_id
    );
    v_result := careerops._candidate_profile_public_record(p_profile_version_id);
    RETURN v_result || jsonb_build_object('newly_created', true);
END
$function$;
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {_DECIDE_FUNCTION}{_DECIDE_SIGNATURE} FROM PUBLIC"))


def _install_query_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.candidate_profile_get(
    p_actor_id uuid,
    p_candidate_id uuid,
    p_profile_version_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM careerops.candidate_profile_versions
        WHERE id = p_profile_version_id
          AND owner_user_id = p_actor_id
          AND candidate_id = p_candidate_id
    ) THEN
        RAISE EXCEPTION 'candidate profile snapshot is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    v_result := careerops._candidate_profile_public_record(p_profile_version_id);
    RETURN v_result;
END
$function$;
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.candidate_profile_list(
    p_actor_id uuid,
    p_candidate_id uuid,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'candidate profile list limit is invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM careerops.candidates
        WHERE id = p_candidate_id AND owner_user_id = p_actor_id
    ) THEN
        RAISE EXCEPTION 'candidate is unavailable for actor' USING ERRCODE = '23503';
    END IF;
    SELECT jsonb_build_object(
        'items', COALESCE(
            jsonb_agg(
                careerops._candidate_profile_public_record(selected.id)
                ORDER BY selected.version DESC
            ),
            '[]'::jsonb
        ),
        'count', count(*)
    ) INTO v_result
    FROM (
        SELECT id, version
        FROM careerops.candidate_profile_versions
        WHERE owner_user_id = p_actor_id AND candidate_id = p_candidate_id
        ORDER BY version DESC
        LIMIT p_limit
    ) AS selected;
    RETURN v_result;
END
$function$;
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.candidate_profile_get_approved(
    p_actor_id uuid,
    p_candidate_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_profile_version_id uuid;
    v_result jsonb;
BEGIN
    SELECT profile.id INTO v_profile_version_id
    FROM careerops.candidate_profile_versions AS profile
    JOIN careerops.candidate_profile_decisions AS decision
      ON decision.profile_version_id = profile.id
     AND decision.owner_user_id = profile.owner_user_id
     AND decision.candidate_id = profile.candidate_id
     AND decision.snapshot_sha256 = profile.snapshot_sha256
    WHERE profile.owner_user_id = p_actor_id
      AND profile.candidate_id = p_candidate_id
      AND decision.decision = 'approve'
      AND careerops._candidate_profile_materials_active(profile.id)
    ORDER BY profile.version DESC
    LIMIT 1;
    IF v_profile_version_id IS NULL THEN
        RAISE EXCEPTION 'approved candidate profile is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    v_result := careerops._candidate_profile_public_record(v_profile_version_id);
    RETURN v_result;
END
$function$;
"""
        )
    )
    for function, signature in (
        (_GET_FUNCTION, _GET_SIGNATURE),
        (_LIST_FUNCTION, _LIST_SIGNATURE),
        (_APPROVED_FUNCTION, _APPROVED_SIGNATURE),
    ):
        op.execute(sa.text(f"REVOKE ALL ON FUNCTION {function}{signature} FROM PUBLIC"))


def _install_permissions() -> None:
    for table_name in _TABLES:
        op.execute(sa.text(f"REVOKE ALL ON careerops.{table_name} FROM PUBLIC"))
    for role_name in _RUNTIME_ROLES:
        _run_for_role(
            role_name,
            tuple(f"REVOKE ALL ON careerops.{table_name} FROM {role_name}" for table_name in _TABLES),
        )
    _run_for_role("careerops_api", _API_GRANTS)
    _run_for_role("careerops_workflow", _WORKFLOW_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def _drop_functions() -> None:
    for function, signature in (
        (_APPROVED_FUNCTION, _APPROVED_SIGNATURE),
        (_LIST_FUNCTION, _LIST_SIGNATURE),
        (_GET_FUNCTION, _GET_SIGNATURE),
        (_DECIDE_FUNCTION, _DECIDE_SIGNATURE),
        (_IMPORT_FUNCTION, _IMPORT_SIGNATURE),
        (_MATERIALS_ACTIVE_FUNCTION, _MATERIALS_ACTIVE_SIGNATURE),
        (_PUBLIC_RECORD_FUNCTION, _PUBLIC_RECORD_SIGNATURE),
    ):
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}{signature}"))


def upgrade() -> None:
    _install_candidate_ownership()
    _create_tables()
    _install_append_only_guards()
    _install_read_functions()
    _install_import_function()
    _install_decision_function()
    _install_query_functions()
    _install_permissions()


def downgrade() -> None:
    _drop_functions()
    op.drop_table("candidate_profile_decisions", schema="careerops")
    op.drop_table("candidate_profile_versions", schema="careerops")
    op.drop_table("candidate_material_versions", schema="careerops")
    op.drop_table("candidate_material_bundles", schema="careerops")
    for table_name in ("gmail_accounts", "gmail_send_accounts", "greenhouse_submit_accounts"):
        op.drop_constraint(
            f"fk_{table_name}_owner_candidate",
            table_name,
            schema="careerops",
            type_="foreignkey",
        )
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_candidates_fill_owner ON careerops.candidates"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_CANDIDATE_OWNER_FUNCTION}{_CANDIDATE_OWNER_SIGNATURE}"))
    op.drop_constraint(
        "uq_candidates_owner_id",
        "candidates",
        schema="careerops",
        type_="unique",
    )
    op.drop_constraint(
        "fk_candidates_owner_user_id_console_users",
        "candidates",
        schema="careerops",
        type_="foreignkey",
    )
    op.drop_column("candidates", "owner_user_id", schema="careerops")
