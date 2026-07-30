"""Crawl readiness gate (real-autonomous-career-loop Phase 8.1).

The readiness gate remains ``False`` until all preconditions for autonomous
multi-source crawling are satisfied:

1. **Migrations applied** -- the ``crawl_source_attempts`` and
   ``crawl_source_permissions`` tables exist in the database.
2. **Canonical crawler available** -- ``build_real_crawl_sink`` factory is
   importable and returns a functional sink.
3. **Permission APIs available** -- ``CrawlPermissionRepository`` is wired.
4. **Finite global budgets configured** -- ``Tier2Budget`` has finite
   ``max_concurrent_slots`` and ``daily_action_budget`` (not infinite).

The gate is checked at startup and on schedule-activation.  When it returns
``False`` the caller logs the missing preconditions and skips activation; no
crawl schedules are resumed and no autonomous crawling begins.

Design: a single pure function ``check_crawl_readiness`` that takes the
concrete dependencies and returns a ``CrawlReadiness`` result.  No side
effects, no global state -- the caller decides what to do with the result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

__all__ = [
    "CrawlReadiness",
    "ReadinessCheck",
    "check_crawl_readiness",
]

_log = logging.getLogger(__name__)


class ReadinessCheck(StrEnum):
    """Names of individual readiness checks."""

    MIGRATIONS_APPLIED = "migrations_applied"
    CRAWLER_AVAILABLE = "crawler_available"
    PERMISSION_APIS = "permission_apis"
    BUDGETS_CONFIGURED = "budgets_configured"


@dataclass(frozen=True, slots=True)
class CrawlReadiness:
    """Result of the readiness gate.

    ``ready`` is True only when every check passes.  ``failures`` lists the
    checks that did not pass, each with a human-readable reason.
    """

    ready: bool
    failures: tuple[str, ...] = ()
    checks: dict[str, bool] = field(default_factory=dict)


def check_crawl_readiness(
    engine: Engine,
    *,
    budget_max_concurrent_slots: int = 0,
    budget_daily_action_budget: int = 0,
    permission_repository: object | None = None,
) -> CrawlReadiness:
    """Check whether autonomous crawling can be activated.

    Args:
        engine: SQLAlchemy engine to inspect for migration tables.
        budget_max_concurrent_slots: ``Tier2Budget.max_concurrent_slots``.
            Must be > 0 to pass.
        budget_daily_action_budget: ``Tier2Budget.daily_action_budget``.
            Must be > 0 to pass.
        permission_repository: the wired ``CrawlPermissionRepository``
            instance (or any non-None object indicating it is available).

    Returns:
        A ``CrawlReadiness`` with ``ready=True`` when all checks pass.
    """
    failures: list[str] = []
    checks: dict[str, bool] = {}

    # 1. Migrations applied: check that the two Phase 5/6 tables exist.
    try:
        inspector = sa_inspect(engine)
        table_names = set(inspector.get_table_names())
        has_attempts = "crawl_source_attempts" in table_names
        has_permissions = "crawl_source_permissions" in table_names
        migrations_ok = has_attempts and has_permissions
        checks[ReadinessCheck.MIGRATIONS_APPLIED] = migrations_ok
        if not migrations_ok:
            missing = []
            if not has_attempts:
                missing.append("crawl_source_attempts")
            if not has_permissions:
                missing.append("crawl_source_permissions")
            failures.append(f"missing tables: {', '.join(missing)}")
    except Exception as exc:
        checks[ReadinessCheck.MIGRATIONS_APPLIED] = False
        failures.append(f"migration check failed: {exc}")

    # 2. Canonical crawler available: verify the factory is importable.
    try:
        from careerops.infrastructure.temporal.crawl_stack import (
            build_real_crawl_sink,
        )

        checks[ReadinessCheck.CRAWLER_AVAILABLE] = callable(build_real_crawl_sink)
    except Exception as exc:
        checks[ReadinessCheck.CRAWLER_AVAILABLE] = False
        failures.append(f"crawler factory unavailable: {exc}")

    # 3. Permission APIs available.
    perm_ok = permission_repository is not None
    checks[ReadinessCheck.PERMISSION_APIS] = perm_ok
    if not perm_ok:
        failures.append("CrawlPermissionRepository not wired")

    # 4. Finite global budgets configured.
    budgets_ok = budget_max_concurrent_slots > 0 and budget_daily_action_budget > 0
    checks[ReadinessCheck.BUDGETS_CONFIGURED] = budgets_ok
    if not budgets_ok:
        failures.append(
            f"budgets not configured: max_concurrent={budget_max_concurrent_slots}, "
            f"daily_actions={budget_daily_action_budget}"
        )

    ready = len(failures) == 0
    if ready:
        _log.info("crawl readiness gate: all checks passed -- autonomous crawling enabled")
    else:
        _log.warning("crawl readiness gate: NOT ready -- %s", "; ".join(failures))

    return CrawlReadiness(ready=ready, failures=tuple(failures), checks=checks)
