from __future__ import annotations

import runpy
from pathlib import Path
from typing import cast

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0019_repair_gmail_mailbox_schema_usage.py"
)


def test_mailbox_schema_usage_repair_is_narrow_and_reversible() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))
    upgrade_grants = cast("tuple[str, ...]", module["_UPGRADE_GRANTS"])
    downgrade_revokes = cast("tuple[str, ...]", module["_DOWNGRADE_REVOKES"])

    assert module["revision"] == "0019"
    assert module["down_revision"] == "0018"
    assert upgrade_grants == ("GRANT USAGE ON SCHEMA careerops TO careerops_mailbox",)
    assert downgrade_revokes == ("REVOKE USAGE ON SCHEMA careerops FROM careerops_mailbox",)
    assert "careerops_api" not in " ".join(upgrade_grants)
    assert "GRANT CREATE" not in " ".join(upgrade_grants)
