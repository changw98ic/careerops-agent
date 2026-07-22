from __future__ import annotations

import runpy
from pathlib import Path
from typing import cast

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0020_narrow_gmail_mailbox_read_grants.py"
)


def test_mailbox_read_grant_repair_is_column_scoped_and_reversible() -> None:
    module = runpy.run_path(str(MIGRATION_PATH))
    upgrades = cast("tuple[str, ...]", module["_UPGRADE_STATEMENTS"])
    downgrades = cast("tuple[str, ...]", module["_DOWNGRADE_STATEMENTS"])

    assert module["revision"] == "0020"
    assert module["down_revision"] == "0019"
    assert upgrades == (
        "REVOKE SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs "
        "FROM careerops_mailbox",
        "GRANT SELECT (id, provider, status) ON careerops.gmail_accounts TO careerops_mailbox",
        "GRANT SELECT (gmail_account_id, status) ON careerops.gmail_sync_runs TO careerops_mailbox",
    )
    assert downgrades[-1] == (
        "GRANT SELECT ON careerops.gmail_accounts, careerops.gmail_sync_runs TO careerops_mailbox"
    )
    assert "oauth_credential_references" not in " ".join(upgrades)
    assert not any(operation.startswith("GRANT SELECT ON") for operation in upgrades)
