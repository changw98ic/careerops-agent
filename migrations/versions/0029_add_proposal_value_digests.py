"""add bounded proposal digests to smart-intake decision metadata"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
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
    """Normalize existing metadata rows without restoring any raw values."""
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS smart_intake_decisions_append_only
                ON careerops.smart_intake_decisions;
            UPDATE careerops.smart_intake_decisions AS decision_row
            SET decisions = COALESCE(
                    (
                        SELECT jsonb_agg(
                            entry.item || jsonb_build_object(
                                'proposal_value_digest',
                                COALESCE(entry.item ->> 'proposal_value_digest', '')
                            ) ORDER BY entry.ordinality
                        )
                        FROM jsonb_array_elements(decision_row.decisions)
                            WITH ORDINALITY AS entry(item, ordinality)
                    ),
                    '[]'::jsonb
                );
            """
        )
    )
    op.execute(sa.text(_TRIGGER_SQL))


def downgrade() -> None:
    # The metadata key is additive and its removal would make old rows
    # structurally inconsistent with newer application writes.
    pass
