"""harden smart-intake decision append-only enforcement"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def _role_exists(role_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _apply_retention(statement: str) -> None:
    """Apply retention grants when bootstrap roles already exist.

    Some migration tests intentionally create capability roles after Alembic
    reaches head.  The application bootstrap reapplies the role grants later;
    a migration must therefore remain valid in both ordering schemes.
    """
    if not op.get_context().as_sql and not _role_exists("careerops_retention"):
        return
    op.execute(sa.text(statement))


def upgrade() -> None:
    """Repair the trigger for databases that already applied 0026."""
    _apply_retention(
        """
            REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
                ON careerops.smart_intake_previews, careerops.smart_intake_decisions
                FROM careerops_retention;
            GRANT SELECT ON careerops.smart_intake_previews, careerops.smart_intake_decisions
                TO careerops_retention;
            """
    )
    op.execute(
        sa.text(
            """
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
        )
    )


def downgrade() -> None:
    # Restore the append-only trigger that 0026 installed.  Downgrading this
    # repair migration must not silently remove the original invariant.
    op.execute(
        sa.text(
            """
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
        )
    )
