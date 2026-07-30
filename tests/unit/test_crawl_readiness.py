"""Unit tests for Phase 8.1: crawl readiness gate.

Tests that check_crawl_readiness returns ready=False until all four
preconditions pass: migrations, crawler, permissions, budgets.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text

from careerops.application.crawl_readiness import CrawlReadiness, check_crawl_readiness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_with_tables(*table_names: str) -> MagicMock:
    """Create a mock engine whose inspector returns the given table names."""
    engine = MagicMock()
    inspector = MagicMock()
    inspector.get_table_names.return_value = list(table_names)
    # Patch sa_inspect to return our mock inspector.
    return engine, inspector


# ---------------------------------------------------------------------------
# 8.1 readiness gate tests
# ---------------------------------------------------------------------------


class TestCrawlReadinessGate:
    """8.1: readiness gate remains false until all preconditions pass."""

    def test_all_checks_pass_returns_ready(self) -> None:
        """When all four checks pass, ready=True and no failures."""
        engine, inspector = _make_engine_with_tables(
            "crawl_source_attempts", "crawl_source_permissions"
        )
        # We need to mock sa_inspect at the module level.
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=3,
                budget_daily_action_budget=500,
                permission_repository=object(),
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is True
        assert result.failures == ()
        assert all(result.checks.values())

    def test_missing_attempts_table_fails(self) -> None:
        """Missing crawl_source_attempts table causes failure."""
        engine, inspector = _make_engine_with_tables("crawl_source_permissions")
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=3,
                budget_daily_action_budget=500,
                permission_repository=object(),
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        assert any("crawl_source_attempts" in f for f in result.failures)

    def test_missing_permissions_table_fails(self) -> None:
        """Missing crawl_source_permissions table causes failure."""
        engine, inspector = _make_engine_with_tables("crawl_source_attempts")
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=3,
                budget_daily_action_budget=500,
                permission_repository=object(),
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        assert any("crawl_source_permissions" in f for f in result.failures)

    def test_no_permission_repository_fails(self) -> None:
        """None permission_repository causes failure."""
        engine, inspector = _make_engine_with_tables(
            "crawl_source_attempts", "crawl_source_permissions"
        )
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=3,
                budget_daily_action_budget=500,
                permission_repository=None,
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        assert any("PermissionRepository" in f for f in result.failures)

    def test_zero_concurrent_slots_fails(self) -> None:
        """Zero max_concurrent_slots causes budget failure."""
        engine, inspector = _make_engine_with_tables(
            "crawl_source_attempts", "crawl_source_permissions"
        )
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=0,
                budget_daily_action_budget=500,
                permission_repository=object(),
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        assert any("budgets" in f for f in result.failures)

    def test_zero_daily_budget_fails(self) -> None:
        """Zero daily_action_budget causes budget failure."""
        engine, inspector = _make_engine_with_tables(
            "crawl_source_attempts", "crawl_source_permissions"
        )
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=3,
                budget_daily_action_budget=0,
                permission_repository=object(),
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        assert any("budgets" in f for f in result.failures)

    def test_all_checks_fail_reports_all_failures(self) -> None:
        """When every non-importable check fails, failure reasons are reported."""
        engine, inspector = _make_engine_with_tables()  # no tables
        from careerops.application import crawl_readiness

        original_inspect = crawl_readiness.sa_inspect
        crawl_readiness.sa_inspect = lambda _: inspector
        try:
            result = check_crawl_readiness(
                engine,
                budget_max_concurrent_slots=0,
                budget_daily_action_budget=0,
                permission_repository=None,
            )
        finally:
            crawl_readiness.sa_inspect = original_inspect

        assert result.ready is False
        # crawler_available passes (module is importable), so 3 failures.
        assert len(result.failures) == 3
        assert result.checks[readiness_module().ReadinessCheck.MIGRATIONS_APPLIED] is False
        assert result.checks[readiness_module().ReadinessCheck.CRAWLER_AVAILABLE] is True
        assert result.checks[readiness_module().ReadinessCheck.PERMISSION_APIS] is False
        assert result.checks[readiness_module().ReadinessCheck.BUDGETS_CONFIGURED] is False


def readiness_module():
    from careerops.application import crawl_readiness
    return crawl_readiness
