#!/usr/bin/env python3
"""Dry-run or explicitly revoke candidate smart-intake preview values.

The command is intentionally opt-in. Disabling the feature flag blocks new
requests; an operator can then run ``--apply`` to clear all unapplied preview
values while retaining bounded tombstones and audit records.
"""

from __future__ import annotations

import argparse
from datetime import datetime

import sqlalchemy as sa
from pydantic import SecretStr

from careerops.api.contracts import ErrorCode
from careerops.application.smart_intake import SmartIntakeService
from careerops.auth.contracts import AuthAction
from careerops.config import get_settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.schema import smart_intake_decisions, smart_intake_previews


class _UnusedLimiter:
    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="", help="PostgreSQL URL; defaults to settings")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="revoke and purge unapplied values; without this flag the command is read-only",
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.database_url:
        settings = settings.model_copy(update={"database_url": SecretStr(args.database_url)})
    is_compose_or_prod = (
        settings.environment.value == "production"
        or settings.deployment_mode.value == "compose_loopback"
    )
    engine = create_database_engine(settings, enforce_role=is_compose_or_prod)
    try:
        if not args.apply:
            with engine.begin() as conn:
                count = conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(smart_intake_previews)
                    .where(
                        smart_intake_previews.c.revoked_at.is_(None),
                        smart_intake_previews.c.purged_at.is_(None),
                        ~sa.exists(
                            sa.select(smart_intake_decisions.c.id).where(
                                smart_intake_decisions.c.preview_id == smart_intake_previews.c.id
                            )
                        ),
                    )
                )
            print(f"unapplied smart-intake previews: {int(count or 0)}")
            return 0

        service = SmartIntakeService(
            engine,
            profile_repository=None,
            job_repository=None,
            resume_repository=None,
            evidence_repository=None,
            model_client=None,
            rate_limiter=_UnusedLimiter(),
        )
        print(f"revoked smart-intake previews: {service.revoke_unapplied()}")
        return 0
    except Exception:
        # Do not print connection strings or provider/database exception text.
        print(f"smart-intake rollback failed: {ErrorCode.DEPENDENCY_NOT_READY.value}")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
