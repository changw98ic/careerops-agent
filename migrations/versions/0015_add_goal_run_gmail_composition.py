"""add goal run gmail composition wrappers

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-21
"""

# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPOSITION_TABLE = "goal_run_gmail_compositions"

RECORD_MATCH_FUNCTION = "careerops.goal_run_record_gmail_match"
RECORD_MATCH_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, numeric, jsonb, text)"
PREPARE_DRAFT_FUNCTION = "careerops.goal_run_prepare_gmail"
PREPARE_DRAFT_SIGNATURE = "(uuid, uuid, jsonb, jsonb, jsonb, text)"
DISPATCH_FUNCTION = "careerops.goal_run_dispatch_gmail"
DISPATCH_SIGNATURE = "(uuid, uuid)"
INSPECT_FUNCTION = "careerops.goal_run_inspect_gmail"
INSPECT_SIGNATURE = "(uuid, uuid)"
_RECORD_MATCH_INTERNAL_FUNCTION = "careerops._goal_run_record_gmail_match"
_RECORD_MATCH_INTERNAL_SIGNATURE = (
    "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, uuid, numeric, text, text, text)"
)
_PREPARE_DRAFT_INTERNAL_FUNCTION = "careerops._goal_run_prepare_gmail_draft"
_PREPARE_DRAFT_INTERNAL_SIGNATURE = "(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)"
_DISPATCH_INTERNAL_FUNCTION = "careerops._goal_run_dispatch_reviewed_gmail"
_DISPATCH_INTERNAL_SIGNATURE = "(uuid, uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text, text, timestamp with time zone)"

_WORKFLOW_GRANTS = (
    "GRANT USAGE ON SCHEMA careerops TO careerops_workflow",
    f"GRANT EXECUTE ON FUNCTION {RECORD_MATCH_FUNCTION}{RECORD_MATCH_SIGNATURE} TO careerops_workflow",
    f"GRANT EXECUTE ON FUNCTION {PREPARE_DRAFT_FUNCTION}{PREPARE_DRAFT_SIGNATURE} TO careerops_workflow",
    f"GRANT EXECUTE ON FUNCTION {DISPATCH_FUNCTION}{DISPATCH_SIGNATURE} TO careerops_workflow",
    f"GRANT EXECUTE ON FUNCTION {INSPECT_FUNCTION}{INSPECT_SIGNATURE} TO careerops_workflow",
)

_READONLY_GRANTS = (f"GRANT SELECT ON careerops.{COMPOSITION_TABLE} TO careerops_readonly",)


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


def _create_table() -> None:
    op.create_table(
        COMPOSITION_TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "goal_run_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.goal_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.console_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_row_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.crawler_source_registry_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("crawler_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "crawler_run_event_id",
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
        sa.Column(
            "gmail_send_account_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.gmail_send_accounts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "campaign_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.autopilot_campaigns.id", ondelete="RESTRICT"),
        ),
        sa.Column("grant_version_id", sa.Uuid()),
        sa.Column(
            "release_qualification_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.release_qualifications.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "action_intent_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.action_intents.id", ondelete="RESTRICT"),
        ),
        sa.Column("payload_version_id", sa.Uuid()),
        sa.Column("policy_decision_id", sa.Uuid()),
        sa.Column(
            "approval_request_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.approval_requests.id", ondelete="RESTRICT"),
        ),
        sa.Column("authorization_id", sa.Uuid()),
        sa.Column(
            "outbox_event_id",
            sa.Uuid(),
            sa.ForeignKey("careerops.outbox_events.id", ondelete="RESTRICT"),
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
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("draft_prepared_at", sa.DateTime(timezone=True)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "payload_hash"],
            [
                "careerops.action_payload_versions.action_intent_id",
                "careerops.action_payload_versions.id",
                "careerops.action_payload_versions.payload_hash",
            ],
            name="fk_goal_run_gmail_compositions_payload_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id", "grant_version_id"],
            [
                "careerops.autopilot_grant_versions.campaign_id",
                "careerops.autopilot_grant_versions.id",
            ],
            name="fk_goal_run_gmail_compositions_grant_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_intent_id", "payload_version_id", "policy_decision_id"],
            [
                "careerops.policy_decisions.action_intent_id",
                "careerops.policy_decisions.payload_version_id",
                "careerops.policy_decisions.id",
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
            "match_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="match_snapshot_sha256_format",
        ),
        sa.CheckConstraint(
            "recipient_snapshot_sha256 IS NULL OR recipient_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="recipient_snapshot_sha256_format",
        ),
        sa.CheckConstraint(
            "subject_sha256 IS NULL OR subject_sha256 ~ '^[a-f0-9]{64}$'",
            name="subject_sha256_format",
        ),
        sa.CheckConstraint(
            "body_sha256 IS NULL OR body_sha256 ~ '^[a-f0-9]{64}$'",
            name="body_sha256_format",
        ),
        sa.CheckConstraint(
            "payload_hash IS NULL OR payload_hash ~ '^[a-f0-9]{64}$'",
            name="payload_hash_format",
        ),
        sa.CheckConstraint(
            "attachment_manifest_sha256 IS NULL OR attachment_manifest_sha256 ~ '^[a-f0-9]{64}$'",
            name="attachment_manifest_sha256_format",
        ),
        sa.CheckConstraint(
            "approval_snapshot_sha256 IS NULL OR approval_snapshot_sha256 ~ '^[a-f0-9]{64}$'",
            name="approval_snapshot_sha256_format",
        ),
        sa.CheckConstraint(
            "review_evidence_sha256 IS NULL OR review_evidence_sha256 ~ '^[a-f0-9]{64}$'",
            name="review_evidence_sha256_format",
        ),
        sa.CheckConstraint(
            "(state = 'matched' AND action_intent_id IS NULL AND outbox_event_id IS NULL) OR "
            "(state = 'draft_prepared' AND action_intent_id IS NOT NULL AND payload_version_id IS NOT NULL "
            "AND payload_hash IS NOT NULL AND approval_request_id IS NOT NULL AND outbox_event_id IS NULL) OR "
            "(state = 'dispatch_enqueued' AND action_intent_id IS NOT NULL AND payload_version_id IS NOT NULL "
            "AND payload_hash IS NOT NULL AND approval_request_id IS NOT NULL AND authorization_id IS NOT NULL "
            "AND outbox_event_id IS NOT NULL)",
            name="state_required_fields",
        ),
        schema="careerops",
    )
    op.create_index(
        "ix_goal_run_gmail_compositions_actor_updated_at",
        COMPOSITION_TABLE,
        ["actor_user_id", "updated_at"],
        schema="careerops",
    )
    op.create_index(
        "ix_goal_run_gmail_compositions_outbox_event",
        COMPOSITION_TABLE,
        ["outbox_event_id"],
        schema="careerops",
    )


def _install_identity_guard() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.enforce_goal_run_gmail_composition_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, careerops
AS $function$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.goal_run_id IS DISTINCT FROM OLD.goal_run_id
       OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
       OR NEW.source_row_id IS DISTINCT FROM OLD.source_row_id
       OR NEW.crawler_run_id IS DISTINCT FROM OLD.crawler_run_id
       OR NEW.crawler_run_event_id IS DISTINCT FROM OLD.crawler_run_event_id
       OR NEW.job_posting_id IS DISTINCT FROM OLD.job_posting_id
       OR NEW.job_posting_version_id IS DISTINCT FROM OLD.job_posting_version_id
       OR NEW.canonical_job_id IS DISTINCT FROM OLD.canonical_job_id
       OR NEW.match_score IS DISTINCT FROM OLD.match_score
       OR NEW.match_reason IS DISTINCT FROM OLD.match_reason
       OR NEW.match_snapshot_sha256 IS DISTINCT FROM OLD.match_snapshot_sha256
       OR NEW.matched_at IS DISTINCT FROM OLD.matched_at THEN
        RAISE EXCEPTION 'goal run gmail composition identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.state = 'dispatch_enqueued' THEN
        RAISE EXCEPTION 'dispatched goal run gmail composition is immutable'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.state = 'matched' AND NEW.state NOT IN ('matched', 'draft_prepared') THEN
        RAISE EXCEPTION 'goal run gmail composition cannot skip draft preparation'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.state = 'draft_prepared' AND NEW.state NOT IN ('draft_prepared', 'dispatch_enqueued') THEN
        RAISE EXCEPTION 'goal run gmail composition cannot regress'
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
            "REVOKE ALL ON FUNCTION careerops.enforce_goal_run_gmail_composition_update() FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_goal_run_gmail_compositions_identity_update "
            f"BEFORE UPDATE ON careerops.{COMPOSITION_TABLE} "
            "FOR EACH ROW EXECUTE FUNCTION careerops.enforce_goal_run_gmail_composition_update()"
        )
    )


def _install_functions() -> None:
    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_record_gmail_match(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_source_row_id uuid,
    p_crawler_run_id uuid,
    p_crawler_run_event_id uuid,
    p_job_posting_id uuid,
    p_job_posting_version_id uuid,
    p_canonical_job_id uuid,
    p_match_score numeric,
    p_match_reason text,
    p_match_snapshot_sha256 text,
    p_trace_id text
)
RETURNS TABLE (
    composition_id uuid,
    state text,
    receipt_state text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_run careerops.goal_runs%ROWTYPE;
    v_existing careerops.goal_run_gmail_compositions%ROWTYPE;
    v_composition_id uuid := gen_random_uuid();
BEGIN
    IF p_actor_id IS NULL
       OR p_goal_run_id IS NULL
       OR p_source_row_id IS NULL
       OR p_crawler_run_id IS NULL
       OR p_crawler_run_event_id IS NULL
       OR p_job_posting_id IS NULL
       OR p_job_posting_version_id IS NULL
       OR p_canonical_job_id IS NULL
       OR p_match_score IS NULL
       OR p_match_score < 0
       OR p_match_score > 1
       OR p_match_reason IS NULL
       OR btrim(p_match_reason) = ''
       OR p_match_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_trace_id IS NULL
       OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$' THEN
        RAISE EXCEPTION 'goal run gmail match payload is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    IF v_run.status NOT IN ('running', 'waiting_review')
       OR v_run.phase NOT IN ('canonical_ingest', 'matching', 'draft_preparation', 'review') THEN
        RAISE EXCEPTION 'goal run is not in a matchable phase'
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1
    FROM careerops.crawler_source_registry_sources AS source_row
    JOIN careerops.crawler_source_runs AS run_event
      ON run_event.event_id = p_crawler_run_event_id
     AND run_event.source_row_id = source_row.id
     AND run_event.run_id = p_crawler_run_id
     AND run_event.event_kind = 'succeeded'
    JOIN careerops.crawler_job_ingestion_evidence AS evidence
      ON evidence.source_row_id = source_row.id
     AND evidence.run_id = p_crawler_run_id
     AND evidence.run_event_id = run_event.event_id
     AND evidence.job_posting_id = p_job_posting_id
     AND evidence.job_posting_version_id = p_job_posting_version_id
     AND evidence.canonical_job_id = p_canonical_job_id
    WHERE source_row.id = p_source_row_id
      AND source_row.registry_id = v_run.registry_id
      AND (v_run.source_id IS NULL OR source_row.source_id = v_run.source_id);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run gmail match is not backed by exact crawler provenance'
            USING ERRCODE = '23514';
    END IF;

    SELECT * INTO v_existing
    FROM careerops.goal_run_gmail_compositions AS composition
    WHERE composition.goal_run_id = p_goal_run_id
    FOR UPDATE;
    IF v_existing.id IS NOT NULL THEN
        IF v_existing.actor_user_id <> p_actor_id
           OR v_existing.source_row_id <> p_source_row_id
           OR v_existing.crawler_run_id <> p_crawler_run_id
           OR v_existing.crawler_run_event_id <> p_crawler_run_event_id
           OR v_existing.job_posting_id <> p_job_posting_id
           OR v_existing.job_posting_version_id <> p_job_posting_version_id
           OR v_existing.canonical_job_id <> p_canonical_job_id
           OR v_existing.match_score <> p_match_score
           OR v_existing.match_reason <> p_match_reason
           OR v_existing.match_snapshot_sha256 <> p_match_snapshot_sha256 THEN
            RAISE EXCEPTION 'goal run gmail match conflicts with existing composition'
                USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_existing.id, v_existing.state::text, 'replayed'::text;
        RETURN;
    END IF;

    INSERT INTO careerops.goal_run_gmail_compositions (
        id, goal_run_id, actor_user_id, source_row_id, crawler_run_id,
        crawler_run_event_id, job_posting_id, job_posting_version_id,
        canonical_job_id, match_score, match_reason, state,
        match_snapshot_sha256, trace_id
    ) VALUES (
        v_composition_id, p_goal_run_id, p_actor_id, p_source_row_id, p_crawler_run_id,
        p_crawler_run_event_id, p_job_posting_id, p_job_posting_version_id,
        p_canonical_job_id, p_match_score, p_match_reason, 'matched',
        p_match_snapshot_sha256, p_trace_id
    );
    RETURN QUERY SELECT v_composition_id, 'matched'::text, 'created'::text;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_RECORD_MATCH_INTERNAL_FUNCTION}{_RECORD_MATCH_INTERNAL_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_prepare_gmail_draft(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_account_id uuid,
    p_target jsonb,
    p_payload jsonb,
    p_attachment_refs jsonb,
    p_payload_hash text,
    p_ruleset_version text,
    p_idempotency_key text,
    p_trace_id text,
    p_decision_rule_reference text,
    p_expires_at timestamp with time zone
)
RETURNS TABLE (
    composition_id uuid,
    action_intent_id uuid,
    payload_version_id uuid,
    payload_hash text,
    approval_request_id uuid,
    receipt_state text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_composition careerops.goal_run_gmail_compositions%ROWTYPE;
    v_run careerops.goal_runs%ROWTYPE;
    v_account careerops.gmail_send_accounts%ROWTYPE;
    v_draft record;
    v_payload_hash text;
    v_recipient_snapshot_sha256 text;
    v_subject_sha256 text;
    v_body_sha256 text;
    v_attachment_manifest_sha256 text;
BEGIN
    IF p_actor_id IS NULL
       OR p_goal_run_id IS NULL
       OR p_account_id IS NULL
       OR p_target IS NULL
       OR jsonb_typeof(p_target) <> 'object'
       OR p_payload IS NULL
       OR jsonb_typeof(p_payload) <> 'object'
       OR p_attachment_refs IS NULL
       OR jsonb_typeof(p_attachment_refs) <> 'array'
       OR p_idempotency_key IS NULL
       OR p_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL
       OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_expires_at IS NULL
       OR p_expires_at <= CURRENT_TIMESTAMP THEN
        RAISE EXCEPTION 'goal run gmail draft payload is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_target ->> 'target_host' IS DISTINCT FROM 'gmail.googleapis.com'
       OR p_target ->> 'channel' IS DISTINCT FROM ('gmail' || chr(58) || 'send')
       OR (p_target ->> 'recipient_sha256') !~ '^[a-f0-9]{64}$'
       OR (p_target ->> 'body_sha256') !~ '^[a-f0-9]{64}$'
       OR (p_target ->> 'subject_sha256') !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'goal run gmail draft target is not exact'
            USING ERRCODE = '23514';
    END IF;
    v_payload_hash := COALESCE(
        p_payload_hash,
        encode(sha256(convert_to(jsonb_build_object(
            'target', p_target,
            'payload', p_payload,
            'attachment_refs', p_attachment_refs
        )::text, 'UTF8')), 'hex')
    );
    v_recipient_snapshot_sha256 := p_target ->> 'recipient_sha256';
    v_subject_sha256 := p_target ->> 'subject_sha256';
    v_body_sha256 := p_target ->> 'body_sha256';
    v_attachment_manifest_sha256 := COALESCE(
        NULLIF(p_target ->> 'attachment_manifest_sha256', ''),
        encode(sha256(convert_to(p_attachment_refs::text, 'UTF8')), 'hex')
    );
    IF v_payload_hash !~ '^[a-f0-9]{64}$'
       OR v_attachment_manifest_sha256 !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'goal run gmail draft hashes are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    IF v_run.phase NOT IN ('matching', 'draft_preparation', 'review')
       OR v_run.status NOT IN ('running', 'waiting_review') THEN
        RAISE EXCEPTION 'goal run is not in draft preparation phase'
            USING ERRCODE = '23514';
    END IF;

    SELECT * INTO v_composition
    FROM careerops.goal_run_gmail_compositions AS composition
    WHERE composition.goal_run_id = p_goal_run_id
      AND composition.actor_user_id = p_actor_id
    FOR UPDATE;
    IF v_composition.id IS NULL THEN
        RAISE EXCEPTION 'goal run gmail composition is missing'
            USING ERRCODE = '23503';
    END IF;
    IF v_composition.state <> 'matched' THEN
        IF v_composition.gmail_send_account_id = p_account_id
           AND v_composition.payload_hash = v_payload_hash
           AND v_composition.draft_idempotency_key = p_idempotency_key THEN
            RETURN QUERY SELECT
                v_composition.id,
                v_composition.action_intent_id,
                v_composition.payload_version_id,
                v_composition.payload_hash::text,
                v_composition.approval_request_id,
                'replayed'::text;
            RETURN;
        END IF;
        RAISE EXCEPTION 'goal run gmail draft was already prepared differently'
            USING ERRCODE = '23505';
    END IF;

    SELECT * INTO v_account
    FROM careerops.gmail_send_accounts AS account
    WHERE account.id = p_account_id
      AND account.owner_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_account.id IS NULL THEN
        RAISE EXCEPTION 'gmail send account is unavailable for goal run owner'
            USING ERRCODE = '23503';
    END IF;

    SELECT *
    INTO v_draft
    FROM careerops.gmail_send_create_draft(
        p_actor_id,
        v_account.candidate_id,
        v_account.candidate_id,
        p_target,
        p_payload,
        p_attachment_refs,
        v_payload_hash,
        p_ruleset_version,
        p_idempotency_key,
        p_trace_id,
        p_decision_rule_reference,
        p_expires_at
    );
    IF v_draft.action_intent_id IS NULL THEN
        RAISE EXCEPTION 'gmail send draft wrapper returned no action intent'
            USING ERRCODE = '23503';
    END IF;

    UPDATE careerops.goal_run_gmail_compositions
    SET gmail_send_account_id = p_account_id,
        action_intent_id = v_draft.action_intent_id,
        payload_version_id = v_draft.payload_version_id,
        policy_decision_id = v_draft.policy_decision_id,
        approval_request_id = v_draft.approval_request_id,
        payload_hash = v_draft.payload_hash,
        recipient_snapshot_sha256 = v_recipient_snapshot_sha256,
        subject_sha256 = v_subject_sha256,
        body_sha256 = v_body_sha256,
        attachment_manifest_sha256 = v_attachment_manifest_sha256,
        draft_idempotency_key = p_idempotency_key,
        state = 'draft_prepared',
        draft_prepared_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP,
        trace_id = p_trace_id
    WHERE id = v_composition.id;

    RETURN QUERY SELECT
        v_composition.id,
        v_draft.action_intent_id,
        v_draft.payload_version_id,
        v_draft.payload_hash,
        v_draft.approval_request_id,
        v_draft.receipt_state;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_PREPARE_DRAFT_INTERNAL_FUNCTION}{_PREPARE_DRAFT_INTERNAL_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_dispatch_reviewed_gmail(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_account_id uuid,
    p_campaign_id uuid,
    p_grant_version_id uuid,
    p_authorization_id uuid,
    p_reviewed_by_user_id uuid,
    p_release_qualification_id uuid,
    p_review_evidence_sha256 text,
    p_review_snapshot_sha256 text,
    p_reservation_key text,
    p_reconciliation_key text,
    p_event_key text,
    p_review_idempotency_key text,
    p_reserve_idempotency_key text,
    p_trace_id text,
    p_review_reason text,
    p_approval_snapshot_sha256 text,
    p_authorization_expires_at timestamp with time zone
)
RETURNS TABLE (
    composition_id uuid,
    reservation_id uuid,
    outbox_event_id uuid,
    receipt_state text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_composition careerops.goal_run_gmail_compositions%ROWTYPE;
    v_run careerops.goal_runs%ROWTYPE;
    v_review record;
    v_reserve record;
BEGIN
    IF p_actor_id IS NULL
       OR p_goal_run_id IS NULL
       OR p_account_id IS NULL
       OR p_campaign_id IS NULL
       OR p_grant_version_id IS NULL
       OR p_authorization_id IS NULL
       OR p_reviewed_by_user_id <> p_actor_id
       OR p_release_qualification_id IS NULL
       OR p_review_evidence_sha256 !~ '^[a-f0-9]{64}$'
       OR p_review_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_approval_snapshot_sha256 !~ '^[a-f0-9]{64}$'
       OR p_authorization_expires_at IS NULL
       OR p_authorization_expires_at <= CURRENT_TIMESTAMP
       OR p_reservation_key IS NULL
       OR btrim(p_reservation_key) = ''
       OR p_reconciliation_key IS NULL
       OR btrim(p_reconciliation_key) = ''
       OR p_event_key <> ('gmail-send' || chr(58) || p_reservation_key)
       OR p_review_idempotency_key IS NULL
       OR p_review_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_reserve_idempotency_key IS NULL
       OR p_reserve_idempotency_key !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_trace_id IS NULL
       OR p_trace_id !~ '^[A-Za-z0-9._:-]{1,128}$'
       OR p_review_reason IS NULL
       OR btrim(p_review_reason) = '' THEN
        RAISE EXCEPTION 'goal run gmail dispatch payload is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    IF v_run.phase NOT IN ('review', 'dispatch')
       OR v_run.status NOT IN ('waiting_review', 'running') THEN
        RAISE EXCEPTION 'goal run is not dispatchable'
            USING ERRCODE = '23514';
    END IF;

    SELECT * INTO v_composition
    FROM careerops.goal_run_gmail_compositions AS composition
    WHERE composition.goal_run_id = p_goal_run_id
      AND composition.actor_user_id = p_actor_id
    FOR UPDATE;
    IF v_composition.id IS NULL OR v_composition.state <> 'draft_prepared' THEN
        IF v_composition.state = 'dispatch_enqueued'
           AND v_composition.dispatch_idempotency_key = p_reserve_idempotency_key THEN
            RETURN QUERY SELECT
                v_composition.id,
                NULL::uuid,
                v_composition.outbox_event_id,
                'replayed'::text;
            RETURN;
        END IF;
        RAISE EXCEPTION 'goal run gmail composition is not ready to dispatch'
            USING ERRCODE = '23514';
    END IF;
    IF v_composition.gmail_send_account_id <> p_account_id THEN
        RAISE EXCEPTION 'goal run gmail dispatch account mismatch'
            USING ERRCODE = '23514';
    END IF;

    PERFORM 1
    FROM careerops.goal_run_review_items AS item
    JOIN careerops.goal_run_review_decisions AS decision
      ON decision.review_item_id = item.id
     AND decision.goal_run_id = item.goal_run_id
     AND decision.actor_user_id = item.actor_user_id
     AND decision.snapshot_sha256 = item.snapshot_sha256
     AND decision.decision = 'approve'
    JOIN careerops.action_payload_versions AS payload_version
      ON payload_version.id = v_composition.payload_version_id
     AND payload_version.action_intent_id = v_composition.action_intent_id
     AND payload_version.payload_hash = v_composition.payload_hash
    WHERE item.goal_run_id = p_goal_run_id
      AND item.actor_user_id = p_actor_id
      AND item.snapshot_sha256 = p_review_snapshot_sha256
      AND item.review_kind = 'goal_run_application_review.v1'
      AND item.review_payload ->> 'version' = 'goal_run_application_review.v1'
      AND item.review_payload ->> 'review_kind' = 'goal_run_application_review.v1'
      AND item.review_payload ->> 'composition_id' = v_composition.id::text
      AND item.review_payload ->> 'approval_request_id' = v_composition.approval_request_id::text
      AND item.review_payload ->> 'selected_application_id' = v_composition.canonical_job_id::text
      AND item.review_payload ->> 'channel' = ('gmail' || chr(58) || 'send')
      AND item.review_payload @> jsonb_build_object(
          'action_intent_id', v_composition.action_intent_id::text,
          'payload_version_id', v_composition.payload_version_id::text,
          'payload_hash', v_composition.payload_hash
      )
      AND item.review_payload -> 'exact_payload' @> jsonb_build_object(
          'action_payload_hash', v_composition.payload_hash,
          'recipient_sha256', v_composition.recipient_snapshot_sha256,
          'subject_sha256', v_composition.subject_sha256,
          'body_sha256', v_composition.body_sha256,
          'attachment_manifest_sha256', v_composition.attachment_manifest_sha256
      )
      AND item.review_payload -> 'snapshots' @> jsonb_build_object(
          'match_snapshot_sha256', v_composition.match_snapshot_sha256
      )
      AND encode(
          sha256(convert_to(lower(btrim(item.review_payload #>> '{email,recipient}')), 'UTF8')),
          'hex'
      ) = v_composition.recipient_snapshot_sha256
      AND encode(
          sha256(convert_to(item.review_payload #>> '{email,subject}', 'UTF8')),
          'hex'
      ) = v_composition.subject_sha256
      AND encode(
          sha256(convert_to(item.review_payload #>> '{email,text_body}', 'UTF8')),
          'hex'
      ) = v_composition.body_sha256
      AND item.review_payload -> 'attachments' = (
          SELECT COALESCE(
              jsonb_agg(
                  jsonb_build_object(
                      'filename', attachment.value ->> 'filename',
                      'content_type', attachment.value ->> 'content_type',
                      'size_bytes', (attachment.value ->> 'size_bytes')::bigint,
                      'sha256', attachment.value ->> 'sha256'
                  ) ORDER BY attachment.ordinality
              ),
              '[]'::jsonb
          )
          FROM jsonb_array_elements(payload_version.attachment_refs)
               WITH ORDINALITY AS attachment(value, ordinality)
      )
      AND item.review_payload -> 'safety' @> jsonb_build_object(
          'contains_credentials', false,
          'external_io_performed', false,
          'human_review_required', true,
          'model_output_is_not_execution_authority', true
      );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run dispatch requires exact approved review snapshot'
            USING ERRCODE = '23514';
    END IF;

    SELECT *
    INTO v_review
    FROM careerops.gmail_send_review_draft(
        p_actor_id,
        v_composition.action_intent_id,
        v_composition.payload_version_id,
        v_composition.approval_request_id,
        p_campaign_id,
        p_grant_version_id,
        v_composition.payload_hash,
        'approved',
        p_reviewed_by_user_id,
        p_authorization_id,
        p_review_snapshot_sha256,
        p_review_idempotency_key,
        p_authorization_expires_at,
        p_trace_id,
        p_review_reason
    );
    IF v_review.authorization_id IS NULL THEN
        RAISE EXCEPTION 'gmail send review did not produce authorization'
            USING ERRCODE = '23514';
    END IF;

    SELECT *
    INTO v_reserve
    FROM careerops.gmail_send_reserve_and_enqueue(
        p_actor_id,
        p_account_id,
        p_campaign_id,
        p_grant_version_id,
        p_authorization_id,
        v_composition.action_intent_id,
        v_composition.payload_version_id,
        v_composition.payload_hash,
        v_composition.recipient_snapshot_sha256,
        v_composition.approval_request_id,
        p_review_evidence_sha256,
        p_review_snapshot_sha256,
        p_reviewed_by_user_id,
        p_release_qualification_id,
        p_reservation_key,
        p_reconciliation_key,
        p_event_key,
        p_reserve_idempotency_key,
        p_trace_id,
        v_composition.subject_sha256,
        v_composition.body_sha256
    );
    IF v_reserve.outbox_event_id IS NULL THEN
        RAISE EXCEPTION 'gmail send reserve did not create outbox event'
            USING ERRCODE = '23514';
    END IF;

    UPDATE careerops.goal_run_gmail_compositions
    SET campaign_id = p_campaign_id,
        grant_version_id = p_grant_version_id,
        authorization_id = p_authorization_id,
        release_qualification_id = p_release_qualification_id,
        outbox_event_id = v_reserve.outbox_event_id,
        review_evidence_sha256 = p_review_evidence_sha256,
        approval_snapshot_sha256 = p_approval_snapshot_sha256,
        reservation_key = p_reservation_key,
        reconciliation_key = p_reconciliation_key,
        dispatch_idempotency_key = p_reserve_idempotency_key,
        state = 'dispatch_enqueued',
        dispatched_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP,
        trace_id = p_trace_id
    WHERE id = v_composition.id;

    RETURN QUERY SELECT
        v_composition.id,
        v_reserve.reservation_id,
        v_reserve.outbox_event_id,
        v_reserve.receipt_state;
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {_DISPATCH_INTERNAL_FUNCTION}{_DISPATCH_INTERNAL_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops._goal_run_gmail_composition_public_record(p_composition_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, careerops
AS $function$
    SELECT jsonb_build_object(
        'composition_id', composition.id,
        'goal_run_id', composition.goal_run_id,
        'actor_user_id', composition.actor_user_id,
        'source_row_id', composition.source_row_id,
        'crawler_run_id', composition.crawler_run_id,
        'crawler_run_event_id', composition.crawler_run_event_id,
        'job_posting_id', composition.job_posting_id,
        'job_posting_version_id', composition.job_posting_version_id,
        'canonical_job_id', composition.canonical_job_id,
        'gmail_send_account_id', composition.gmail_send_account_id,
        'campaign_id', composition.campaign_id,
        'grant_version_id', composition.grant_version_id,
        'release_qualification_id', composition.release_qualification_id,
        'action_intent_id', composition.action_intent_id,
        'payload_version_id', composition.payload_version_id,
        'approval_request_id', composition.approval_request_id,
        'authorization_id', composition.authorization_id,
        'outbox_event_id', composition.outbox_event_id,
        'payload_hash', composition.payload_hash,
        'recipient_snapshot_sha256', composition.recipient_snapshot_sha256,
        'subject_sha256', composition.subject_sha256,
        'body_sha256', composition.body_sha256,
        'attachment_manifest_sha256', composition.attachment_manifest_sha256,
        'approval_snapshot_sha256', composition.approval_snapshot_sha256,
        'review_evidence_sha256', composition.review_evidence_sha256,
        'reservation_key', composition.reservation_key,
        'reconciliation_key', composition.reconciliation_key,
        'state', composition.state,
        'match_snapshot_sha256', composition.match_snapshot_sha256,
        'matched_at', composition.matched_at,
        'draft_prepared_at', composition.draft_prepared_at,
        'dispatched_at', composition.dispatched_at,
        'updated_at', composition.updated_at
    )
    FROM careerops.goal_run_gmail_compositions AS composition
    WHERE composition.id = p_composition_id
$function$
"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION careerops._goal_run_gmail_composition_public_record(uuid) FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_record_gmail_match(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_source_row_id uuid,
    p_crawler_run_id uuid,
    p_canonical_job_id uuid,
    p_job_posting_id uuid,
    p_job_posting_version_id uuid,
    p_match_score numeric,
    p_match_reasons jsonb,
    p_match_snapshot_sha256 text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_run careerops.goal_runs%ROWTYPE;
    v_crawler_run_event_id uuid;
    v_match_reason text;
    v_match record;
BEGIN
    IF p_match_reasons IS NULL OR jsonb_typeof(p_match_reasons) NOT IN ('array', 'object') THEN
        RAISE EXCEPTION 'goal run gmail match reasons are invalid'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    SELECT run_event.event_id
    INTO v_crawler_run_event_id
    FROM careerops.crawler_source_runs AS run_event
    WHERE run_event.source_row_id = p_source_row_id
      AND run_event.run_id = p_crawler_run_id
      AND run_event.event_kind = 'succeeded'
    ORDER BY run_event.completed_at DESC, run_event.created_at DESC, run_event.event_id
    LIMIT 1;
    IF v_crawler_run_event_id IS NULL THEN
        RAISE EXCEPTION 'goal run gmail match requires a succeeded crawler run'
            USING ERRCODE = '23514';
    END IF;

    v_match_reason := left(p_match_reasons::text, 4000);
    SELECT *
    INTO v_match
    FROM careerops._goal_run_record_gmail_match(
        p_actor_id,
        p_goal_run_id,
        p_source_row_id,
        p_crawler_run_id,
        v_crawler_run_event_id,
        p_job_posting_id,
        p_job_posting_version_id,
        p_canonical_job_id,
        p_match_score,
        v_match_reason,
        p_match_snapshot_sha256,
        COALESCE(NULLIF(v_run.trace_id, ''), 'goal-run-gmail-match')
    );
    RETURN careerops._goal_run_gmail_composition_public_record(v_match.composition_id)
        || jsonb_build_object('receipt_state', v_match.receipt_state);
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {RECORD_MATCH_FUNCTION}{RECORD_MATCH_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_prepare_gmail(
    p_actor_id uuid,
    p_goal_run_id uuid,
    p_target jsonb,
    p_payload jsonb,
    p_attachment_refs jsonb,
    p_payload_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_run careerops.goal_runs%ROWTYPE;
    v_account_id uuid;
    v_candidate_id uuid;
    v_sender text;
    v_draft record;
BEGIN
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    v_account_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'account_id', '')::uuid;
    IF v_account_id IS NULL THEN
        RAISE EXCEPTION 'goal run gmail context is missing gmail_dispatch.account_id'
            USING ERRCODE = '23514';
    END IF;
    v_candidate_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'candidate_id', '')::uuid;
    v_sender := lower(btrim(COALESCE(v_run.context_json -> 'gmail_dispatch' ->> 'sender', '')));
    IF v_candidate_id IS NULL THEN
        RAISE EXCEPTION 'goal run gmail context is missing gmail_dispatch.candidate_id'
            USING ERRCODE = '23514';
    END IF;
    IF v_sender = '' THEN
        RAISE EXCEPTION 'goal run gmail context is missing gmail_dispatch.sender'
            USING ERRCODE = '23514';
    END IF;
    PERFORM 1
    FROM careerops.gmail_send_accounts AS account
    WHERE account.id = v_account_id
      AND account.owner_user_id = p_actor_id
      AND account.candidate_id = v_candidate_id
      AND lower(account.account_subject) = v_sender
    FOR KEY SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal run gmail context sender/candidate does not match account'
            USING ERRCODE = '23514';
    END IF;

    SELECT *
    INTO v_draft
    FROM careerops._goal_run_prepare_gmail_draft(
        p_actor_id,
        p_goal_run_id,
        v_account_id,
        p_target,
        p_payload,
        p_attachment_refs,
        p_payload_hash,
        COALESCE(NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'ruleset_version', ''), 'goal-run-gmail.v1'),
        'goal-run-gmail-draft' || chr(58) || p_goal_run_id::text || chr(58) || COALESCE(p_payload_hash, 'computed'),
        COALESCE(NULLIF(v_run.trace_id, ''), 'goal-run-gmail-prepare'),
        COALESCE(NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'decision_rule_reference', ''), 'goal-run-simple-review'),
        (v_run.context_json -> 'gmail_dispatch' ->> 'draft_expires_at')::timestamptz
    );
    RETURN careerops._goal_run_gmail_composition_public_record(v_draft.composition_id)
        || jsonb_build_object('receipt_state', v_draft.receipt_state);
END
$function$
"""
        )
    )
    op.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {PREPARE_DRAFT_FUNCTION}{PREPARE_DRAFT_SIGNATURE} FROM PUBLIC"
        )
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_dispatch_gmail(
    p_actor_id uuid,
    p_goal_run_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
DECLARE
    v_run careerops.goal_runs%ROWTYPE;
    v_composition careerops.goal_run_gmail_compositions%ROWTYPE;
    v_dispatch record;
    v_account_id uuid;
    v_campaign_id uuid;
    v_grant_version_id uuid;
    v_release_qualification_id uuid;
    v_authorization_id uuid;
    v_authorization_expires_at timestamp with time zone;
    v_review_evidence_sha256 text;
    v_approval_snapshot_sha256 text;
    v_review_reason text;
    v_reservation_key text;
    v_reconciliation_key text;
BEGIN
    SELECT * INTO v_run
    FROM careerops.goal_runs AS run
    WHERE run.id = p_goal_run_id
      AND run.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_run.id IS NULL THEN
        RAISE EXCEPTION 'goal run is unavailable for actor'
            USING ERRCODE = '23503';
    END IF;
    IF v_run.review_decision <> 'approve' OR v_run.review_snapshot_sha256 !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'goal run dispatch requires an approved goal-run review snapshot'
            USING ERRCODE = '23514';
    END IF;

    SELECT * INTO v_composition
    FROM careerops.goal_run_gmail_compositions AS composition
    WHERE composition.goal_run_id = p_goal_run_id
      AND composition.actor_user_id = p_actor_id
    FOR KEY SHARE;
    IF v_composition.id IS NULL THEN
        RAISE EXCEPTION 'goal run gmail composition is missing'
            USING ERRCODE = '23503';
    END IF;

    v_account_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'account_id', '')::uuid;
    v_campaign_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'campaign_id', '')::uuid;
    v_grant_version_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'grant_version_id', '')::uuid;
    v_release_qualification_id := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'release_qualification_id', '')::uuid;
    v_authorization_id := COALESCE(
        v_composition.authorization_id,
        NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'authorization_id', '')::uuid,
        (
            substr(md5(p_goal_run_id::text || chr(58) || v_composition.payload_hash || chr(58) || 'gmail-authorization'), 1, 8)
            || '-' || substr(md5(p_goal_run_id::text || chr(58) || v_composition.payload_hash || chr(58) || 'gmail-authorization'), 9, 4)
            || '-' || substr(md5(p_goal_run_id::text || chr(58) || v_composition.payload_hash || chr(58) || 'gmail-authorization'), 13, 4)
            || '-' || substr(md5(p_goal_run_id::text || chr(58) || v_composition.payload_hash || chr(58) || 'gmail-authorization'), 17, 4)
            || '-' || substr(md5(p_goal_run_id::text || chr(58) || v_composition.payload_hash || chr(58) || 'gmail-authorization'), 21, 12)
        )::uuid
    );
    v_authorization_expires_at := NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'authorization_expires_at', '')::timestamptz;
    v_review_evidence_sha256 := COALESCE(NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'review_evidence_sha256', ''), v_run.review_snapshot_sha256);
    v_approval_snapshot_sha256 := COALESCE(NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'approval_snapshot_sha256', ''), v_run.review_snapshot_sha256);
    v_review_reason := COALESCE(NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'review_reason', ''), 'GoalRun simple review approved exact Gmail payload');
    v_reservation_key := COALESCE(
        NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'reservation_key', ''),
        'goal-run-gmail' || chr(58) || p_goal_run_id::text || chr(58) || v_composition.payload_hash
    );
    v_reconciliation_key := COALESCE(
        NULLIF(v_run.context_json -> 'gmail_dispatch' ->> 'reconciliation_key', ''),
        v_reservation_key || chr(58) || 'reconcile'
    );
    IF v_account_id IS NULL
       OR v_campaign_id IS NULL
       OR v_grant_version_id IS NULL
       OR v_release_qualification_id IS NULL
       OR v_authorization_id IS NULL
       OR v_authorization_expires_at IS NULL
       OR v_authorization_expires_at <= CURRENT_TIMESTAMP
       OR v_review_evidence_sha256 !~ '^[a-f0-9]{64}$'
       OR v_approval_snapshot_sha256 !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'goal run gmail context is missing dispatch identities'
            USING ERRCODE = '23514';
    END IF;

    SELECT *
    INTO v_dispatch
    FROM careerops._goal_run_dispatch_reviewed_gmail(
        p_actor_id,
        p_goal_run_id,
        v_account_id,
        v_campaign_id,
        v_grant_version_id,
        v_authorization_id,
        p_actor_id,
        v_release_qualification_id,
        v_review_evidence_sha256,
        v_run.review_snapshot_sha256,
        v_reservation_key,
        v_reconciliation_key,
        'gmail-send' || chr(58) || v_reservation_key,
        'goal-run-gmail-review' || chr(58) || p_goal_run_id::text || chr(58) || v_composition.payload_hash,
        'goal-run-gmail-reserve' || chr(58) || p_goal_run_id::text || chr(58) || v_composition.payload_hash,
        COALESCE(NULLIF(v_run.trace_id, ''), 'goal-run-gmail-dispatch'),
        v_review_reason,
        v_approval_snapshot_sha256,
        v_authorization_expires_at
    );
    RETURN careerops._goal_run_gmail_composition_public_record(v_dispatch.composition_id)
        || jsonb_build_object(
            'reservation_id', v_dispatch.reservation_id,
            'receipt_state', v_dispatch.receipt_state
        );
END
$function$
"""
        )
    )
    op.execute(
        sa.text(f"REVOKE ALL ON FUNCTION {DISPATCH_FUNCTION}{DISPATCH_SIGNATURE} FROM PUBLIC")
    )

    op.execute(
        sa.text(
            """
CREATE FUNCTION careerops.goal_run_inspect_gmail(
    p_actor_id uuid,
    p_goal_run_id uuid
)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, careerops
AS $function$
    SELECT jsonb_build_object(
        'goal_run_id', composition.goal_run_id,
        'state', composition.state,
        'action_intent_id', composition.action_intent_id,
        'payload_version_id', composition.payload_version_id,
        'payload_hash', composition.payload_hash,
        'outbox_event_id', composition.outbox_event_id,
        'outbox_status', outbox.status,
        'provider_state', COALESCE(
            receipt.receipt_metadata ->> 'provider_state',
            receipt.final_state
        ),
        'provider_message_id', receipt.provider_resource_id,
        'provider_thread_id', receipt.receipt_metadata ->> 'provider_thread_id',
        'receipt_received_at', receipt.received_at,
        'reconciliation_status', reconciliation.status,
        'reconciliation_last_error_code', reconciliation.last_error_code,
        'composition', careerops._goal_run_gmail_composition_public_record(composition.id)
    )
    FROM careerops.goal_run_gmail_compositions AS composition
    LEFT JOIN careerops.outbox_events AS outbox
      ON outbox.id = composition.outbox_event_id
    LEFT JOIN LATERAL (
        SELECT current_attempt.*
        FROM careerops.side_effect_attempts AS current_attempt
        WHERE current_attempt.action_intent_id = composition.action_intent_id
          AND current_attempt.outbox_event_id = composition.outbox_event_id
        ORDER BY current_attempt.ordinal DESC,
                 current_attempt.started_at DESC,
                 current_attempt.id DESC
        LIMIT 1
    ) AS attempt ON true
    LEFT JOIN LATERAL (
        SELECT terminal_receipt.*
        FROM careerops.provider_receipts AS terminal_receipt
        WHERE terminal_receipt.side_effect_attempt_id = attempt.id
          AND terminal_receipt.provider = 'gmail'
          AND terminal_receipt.reconciliation_key = composition.reconciliation_key
        ORDER BY terminal_receipt.received_at DESC, terminal_receipt.id DESC
        LIMIT 1
    ) AS receipt ON true
    LEFT JOIN LATERAL (
        SELECT latest_reconciliation.*
        FROM careerops.gmail_send_reconciliation_jobs AS latest_reconciliation
        WHERE latest_reconciliation.outbox_event_id = composition.outbox_event_id
        ORDER BY latest_reconciliation.updated_at DESC,
                 latest_reconciliation.created_at DESC
        LIMIT 1
    ) AS reconciliation ON true
    WHERE composition.goal_run_id = p_goal_run_id
      AND composition.actor_user_id = p_actor_id
$function$
"""
        )
    )
    op.execute(sa.text(f"REVOKE ALL ON FUNCTION {INSPECT_FUNCTION}{INSPECT_SIGNATURE} FROM PUBLIC"))


def _drop_functions() -> None:
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {INSPECT_FUNCTION}{INSPECT_SIGNATURE}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {DISPATCH_FUNCTION}{DISPATCH_SIGNATURE}"))
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {PREPARE_DRAFT_FUNCTION}{PREPARE_DRAFT_SIGNATURE}")
    )
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {RECORD_MATCH_FUNCTION}{RECORD_MATCH_SIGNATURE}"))
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops._goal_run_gmail_composition_public_record(uuid)")
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION IF EXISTS {_DISPATCH_INTERNAL_FUNCTION}{_DISPATCH_INTERNAL_SIGNATURE}"
        )
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION IF EXISTS {_PREPARE_DRAFT_INTERNAL_FUNCTION}{_PREPARE_DRAFT_INTERNAL_SIGNATURE}"
        )
    )
    op.execute(
        sa.text(
            f"DROP FUNCTION IF EXISTS {_RECORD_MATCH_INTERNAL_FUNCTION}{_RECORD_MATCH_INTERNAL_SIGNATURE}"
        )
    )


def upgrade() -> None:
    _create_table()
    _install_identity_guard()
    _install_functions()
    for statement in (
        f"REVOKE ALL ON careerops.{COMPOSITION_TABLE} FROM PUBLIC",
        f"REVOKE ALL ON careerops.{COMPOSITION_TABLE} FROM careerops_api, careerops_workflow, careerops_mailbox, careerops_outbox, careerops_mail_sender, careerops_greenhouse_sender",
    ):
        op.execute(sa.text(statement))
    _run_for_role("careerops_workflow", _WORKFLOW_GRANTS)
    _run_for_role("careerops_readonly", _READONLY_GRANTS)


def downgrade() -> None:
    _run_for_role(
        "careerops_readonly",
        tuple(
            statement.replace("GRANT SELECT", "REVOKE SELECT").replace(
                " TO careerops_readonly", " FROM careerops_readonly"
            )
            for statement in _READONLY_GRANTS
        ),
    )
    _run_for_role(
        "careerops_workflow",
        tuple(
            statement.replace("GRANT EXECUTE", "REVOKE EXECUTE")
            .replace("GRANT USAGE", "REVOKE USAGE")
            .replace(" TO careerops_workflow", " FROM careerops_workflow")
            for statement in _WORKFLOW_GRANTS
        ),
    )
    _drop_functions()
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_goal_run_gmail_compositions_identity_update "
            f"ON careerops.{COMPOSITION_TABLE}"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS careerops.enforce_goal_run_gmail_composition_update()")
    )
    op.drop_index(
        "ix_goal_run_gmail_compositions_outbox_event",
        table_name=COMPOSITION_TABLE,
        schema="careerops",
    )
    op.drop_index(
        "ix_goal_run_gmail_compositions_actor_updated_at",
        table_name=COMPOSITION_TABLE,
        schema="careerops",
    )
    op.drop_table(COMPOSITION_TABLE, schema="careerops")
