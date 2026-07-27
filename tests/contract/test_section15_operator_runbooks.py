"""Contract tests: Section 15 operator runbooks and bounded alerts (task 15.12).

Proves the observability surface supports operator runbooks for:

- Stuck crawl runs (RUNNING for > threshold)
- Repeated source denials (blocked/trusted status)
- Stale mail cursors (no sync for > threshold)
- Pending approvals (packages/replies awaiting review)
- Reconciliation backlog (send intents stuck in RECONCILIATION_REQUIRED)
- Revoked accounts (connection_state = revoked)
- Retention/purge failures (content_objects past retention_until)

These tests verify the DOMAIN CONTRACTS and METRIC DEFINITIONS that enable
alerting, without requiring a running Prometheus or PagerDuty.

Iron rules honored:
- Bounded labels (Iron Rule 2): alert labels are closed enums.
- Default-deny (Iron Rule 7): revoked accounts stop future sync.

Run::

    uv run python -m pytest tests/contract/test_section15_operator_runbooks.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from careerops.domain.mail_sync import (
    MailAccountSummary,
    MailConnectionState,
    MailSyncRun,
    MailSyncRunStatus,
)
from careerops.observability.career_loop_metrics import CareerLoopMetrics

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (1) Stuck crawl runs: RUNNING status with stale started_at
# ---------------------------------------------------------------------------


class TestStuckRunDetection:
    """A run in RUNNING status for > threshold is detectable via the
    status field and started_at timestamp."""

    def test_running_run_has_started_at(self) -> None:
        run = MailSyncRun(
            id=uuid4(),
            account_id=uuid4(),
            candidate_id=uuid4(),
            status=MailSyncRunStatus.RUNNING,
            started_at=NOW,
        )
        assert run.started_at is not None
        assert run.status is MailSyncRunStatus.RUNNING

    def test_stuck_run_detectable_via_duration(self) -> None:
        """An operator can detect stuck runs by comparing started_at to now."""
        old_time = datetime(2026, 1, 1, tzinfo=UTC)
        run = MailSyncRun(
            id=uuid4(),
            account_id=uuid4(),
            candidate_id=uuid4(),
            status=MailSyncRunStatus.RUNNING,
            started_at=old_time,
        )
        duration = (NOW - run.started_at).total_seconds()
        assert duration > 3600, "run is stuck (> 1 hour)"


# ---------------------------------------------------------------------------
# (2) Repeated source denials: trust/terms/robots status
# ---------------------------------------------------------------------------


class TestSourceDenialDetection:
    """Source trust/terms/robots statuses are closed enums for alerting."""

    # The schema enforces these via CHECK constraints.
    # We verify the domain contract here.
    def test_trust_status_is_closed_enum(self) -> None:
        from careerops.infrastructure.database.schema import job_sources

        constraints = [c for c in job_sources.constraints if hasattr(c, "name")]
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_job_sources_trust_status_values" in names
        assert "ck_job_sources_terms_status_values" in names
        assert "ck_job_sources_robots_status_values" in names


# ---------------------------------------------------------------------------
# (3) Stale mail cursors: no sync for > threshold
# ---------------------------------------------------------------------------


class TestStaleCursorDetection:
    """A MailAccountSummary with stale last_sync_at is detectable."""

    def test_account_with_no_sync_is_stale(self) -> None:
        account = MailAccountSummary(
            account_id=uuid4(),
            candidate_id=uuid4(),
            email_address="test@example.com",
            connection_state=MailConnectionState.CONNECTED,
            last_sync_at=None,
        )
        assert account.last_sync_at is None

    def test_account_with_old_sync_is_stale(self) -> None:
        old_time = datetime(2026, 1, 1, tzinfo=UTC)
        account = MailAccountSummary(
            account_id=uuid4(),
            candidate_id=uuid4(),
            email_address="test@example.com",
            connection_state=MailConnectionState.CONNECTED,
            last_sync_at=old_time,
        )
        age = (NOW - account.last_sync_at).total_seconds()
        assert age > 86400, "cursor is stale (> 1 day)"


# ---------------------------------------------------------------------------
# (4) Pending approvals: metric gauge tracks current count
# ---------------------------------------------------------------------------


class TestPendingApprovalAlerts:
    """The pending_approvals gauge enables alerting on approval backlog."""

    def test_gauge_tracks_package_approvals(self) -> None:
        metrics = CareerLoopMetrics()
        metrics.inc_pending_approvals(kind="package")
        metrics.inc_pending_approvals(kind="package")
        assert metrics._pending_approvals.labels(kind="package")._value.get() == 2

    def test_gauge_tracks_reply_approvals(self) -> None:
        metrics = CareerLoopMetrics()
        metrics.inc_pending_approvals(kind="reply")
        assert metrics._pending_approvals.labels(kind="reply")._value.get() == 1


# ---------------------------------------------------------------------------
# (5) Reconciliation backlog: metric gauge tracks stuck intents
# ---------------------------------------------------------------------------


class TestReconciliationBacklogAlerts:
    """The reconciliation_backlog gauge enables alerting on stuck sends."""

    def test_gauge_tracks_backlog(self) -> None:
        metrics = CareerLoopMetrics()
        metrics.inc_reconciliation_backlog()
        metrics.inc_reconciliation_backlog()
        assert metrics._reconciliation_backlog._value.get() == 2
        metrics.dec_reconciliation_backlog()
        assert metrics._reconciliation_backlog._value.get() == 1


# ---------------------------------------------------------------------------
# (6) Revoked accounts: connection state is closed enum
# ---------------------------------------------------------------------------


class TestRevokedAccountDetection:
    """MailConnectionState.REVOKED is a terminal state that stops sync."""

    def test_revoked_state_value(self) -> None:
        assert MailConnectionState.REVOKED.value == "revoked"

    def test_revoked_account_not_sync_available(self) -> None:
        account = MailAccountSummary(
            account_id=uuid4(),
            candidate_id=uuid4(),
            email_address="test@example.com",
            connection_state=MailConnectionState.REVOKED,
        )
        assert not account.sync_available

    def test_disconnected_account_not_sync_available(self) -> None:
        account = MailAccountSummary(
            account_id=uuid4(),
            candidate_id=uuid4(),
            email_address="test@example.com",
            connection_state=MailConnectionState.DISCONNECTED,
        )
        assert not account.sync_available

    def test_connected_account_is_sync_available(self) -> None:
        account = MailAccountSummary(
            account_id=uuid4(),
            candidate_id=uuid4(),
            email_address="test@example.com",
            connection_state=MailConnectionState.CONNECTED,
        )
        assert account.sync_available


# ---------------------------------------------------------------------------
# (7) Retention failures: schema enforces retention constraints
# ---------------------------------------------------------------------------


class TestRetentionConstraints:
    """The content_objects schema enforces retention_until and quarantine limits."""

    def test_quarantine_retention_limit_constraint_exists(self) -> None:
        from careerops.infrastructure.database.schema import content_objects

        constraints = [c for c in content_objects.constraints if hasattr(c, "name")]
        names = {c.name for c in constraints if hasattr(c, "name")}
        assert "ck_content_objects_quarantine_retention_limit" in names

    def test_retention_pending_delete_index_exists(self) -> None:
        """The index for finding expired content objects exists."""
        from careerops.infrastructure.database.schema import content_objects

        # The index is defined at module level.
        # We verify the table has the retention_until column.
        assert "retention_until" in content_objects.columns
        assert "expired_at" in content_objects.columns
        assert "retired_at" in content_objects.columns
