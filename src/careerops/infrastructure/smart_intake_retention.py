"""Scheduled retention worker for short-lived smart-intake preview values."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import sqlalchemy as sa

from careerops.api.contracts import ErrorCode
from careerops.config import get_settings
from careerops.infrastructure.database.engine import create_database_engine


def purge_once(engine: sa.Engine, *, now: datetime | None = None) -> int:
    """Invoke only the retention SECURITY DEFINER function for one sweep."""
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL ROLE careerops_retention"))
        purged = connection.scalar(
            sa.text("SELECT careerops.purge_smart_intake_previews(CAST(:now AS timestamptz))"),
            {"now": now or datetime.now(UTC)},
        )
    return int(purged or 0)


def main() -> int:
    settings = get_settings()
    interval = max(
        60,
        int(os.environ.get("CAREEROPS_SMART_INTAKE_RETENTION_INTERVAL_SECONDS", "3600")),
    )
    engine = create_database_engine(settings, enforce_role=True)
    try:
        while True:
            try:
                purged = purge_once(engine)
                print(f"purged smart-intake preview values: {purged}", flush=True)
            except Exception:
                # Keep the loop alive for the next bounded retry, without
                # printing credentials or raw database/provider details.
                print(
                    f"smart-intake retention failed: {ErrorCode.DEPENDENCY_NOT_READY.value}",
                    flush=True,
                )
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
