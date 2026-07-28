"""remove raw decision values from the append-only metadata rows"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION careerops.reject_smart_intake_decision_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = careerops, pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'smart intake decisions are append-only'
        USING ERRCODE = '55000';
END;
$function$;
DROP TRIGGER IF EXISTS smart_intake_decisions_append_only
    ON careerops.smart_intake_decisions;
CREATE TRIGGER smart_intake_decisions_append_only
BEFORE UPDATE OR DELETE ON careerops.smart_intake_decisions
FOR EACH ROW EXECUTE FUNCTION careerops.reject_smart_intake_decision_mutation();
"""


def upgrade() -> None:
    """Scrub values written by the pre-0028 implementation in-place.

    The trigger is suspended only inside this transactional migration so the
    append-only invariant is restored before the database is usable again.
    New application writes store only paths, decisions, bounded reasons, and
    value digests; draft patches are reconstructed from the request and the
    short-lived preview instead of being persisted in the decision row.
    """
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS smart_intake_decisions_append_only
                ON careerops.smart_intake_decisions;
            UPDATE careerops.smart_intake_decisions AS decision_row
            SET decisions = COALESCE(
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'path', entry.item ->> 'path',
                                'decision', entry.item ->> 'decision',
                                'reason', COALESCE(entry.item ->> 'reason', ''),
                                'value_digest', COALESCE(entry.item ->> 'value_digest', '')
                            ) ORDER BY entry.ordinality
                        )
                        FROM jsonb_array_elements(decision_row.decisions)
                            WITH ORDINALITY AS entry(item, ordinality)
                    ),
                    '[]'::jsonb
                ),
                draft_patch = '{}'::jsonb;
            """
        )
    )
    op.execute(sa.text(_TRIGGER_SQL))


def downgrade() -> None:
    # Redaction is intentionally irreversible; the append-only invariant stays
    # installed when the migration chain is downgraded.
    pass
