#!/usr/bin/env python3
"""Run the retention-role purge for expired smart-intake preview values.

Schedule this command at least daily. It invokes only the SECURITY DEFINER
retention function; the retention role has no direct DELETE privilege and
decision metadata is never removed by this path.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from pydantic import SecretStr

from careerops.api.contracts import ErrorCode
from careerops.config import get_settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.smart_intake_retention import purge_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="", help="PostgreSQL URL; defaults to settings")
    args = parser.parse_args()
    settings = get_settings()
    if args.database_url:
        settings = settings.model_copy(update={"database_url": SecretStr(args.database_url)})
    engine = create_database_engine(settings, enforce_role=True)
    try:
        print(f"purged smart-intake preview values: {purge_once(engine, now=datetime.now(UTC))}")
        return 0
    except Exception:
        # Do not print connection strings or provider/database exception text.
        print(f"smart-intake retention failed: {ErrorCode.DEPENDENCY_NOT_READY.value}")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
