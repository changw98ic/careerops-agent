"""Contract tests: Section 15 career-loop bounded metrics (task 15.7).

Proves the new ``CareerLoopMetrics`` expose the business signals named in task
15.7 (inbox decisions, package approvals, send intents, provider receipts,
reconciliation, mail proposals, review latency, follow-ups) with a BOUNDED,
non-sensitive label vocabulary. Mirrors the style of
``tests/contract/test_metrics.py``.

Iron rules honored:
- Bounded labels (Iron Rule 2): every label is a closed enum-aligned value;
  free-form / unbounded strings (URLs, email, error payloads, PII) are dropped
  and never become a metric label series.
- No content leakage (ADR 0006): only structural identifiers are recorded.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from careerops.observability.career_loop_metrics import CareerLoopMetrics


def _metrics() -> tuple[CareerLoopMetrics, CollectorRegistry]:
    registry = CollectorRegistry(auto_describe=True)
    metrics = CareerLoopMetrics(registry=registry)
    return metrics, registry


def _text(registry: CollectorRegistry) -> str:
    from prometheus_client import generate_latest

    return generate_latest(registry).decode()


class TestMetricDefinitions:
    def test_all_seeded_labels_present_on_first_scrape(self) -> None:
        _, registry = _metrics()
        body = _text(registry)
        # Every counter is scrapeable before any business event fires because
        # the closed label vocabulary is pre-seeded.
        for line in (
            'careerops_inbox_decisions_total{decision="favorite"}',
            'careerops_inbox_decisions_total{decision="ignore"}',
            'careerops_inbox_decisions_total{decision="snooze"}',
            "careerops_package_approvals_total",
            "careerops_send_intents_total",
            "careerops_provider_receipts_total",
            "careerops_reconciliation_total",
            "careerops_mail_proposals_total",
            "careerops_review_latency_seconds",
            "careerops_follow_up_total",
            "careerops_pending_approvals",
            "careerops_reconciliation_backlog",
        ):
            assert line in body, f"metric family missing: {line}"


class TestBoundedLabelVocabulary:
    """Free-form / unbounded / PII values are dropped, never exported."""

    def test_inbox_decision_unknown_value_is_dropped(self) -> None:
        metrics, registry = _metrics()
        # PII / unbounded string must NOT create a label series.
        metrics.record_inbox_decision(decision="user@example.com")
        metrics.record_inbox_decision(decision="<script>alert(1)</script>")
        body = _text(registry)
        assert "user@example.com" not in body
        assert "<script>" not in body
        # Closed vocab still counts.
        metrics.record_inbox_decision(decision="favorite")
        body = _text(registry)
        assert 'careerops_inbox_decisions_total{decision="favorite"} 1.0' in body

    def test_mail_proposal_unbounded_source_dropped(self) -> None:
        metrics, registry = _metrics()
        metrics.record_mail_proposal(decision="accepted", source="gpt-4-long-output")
        body = _text(registry)
        assert "gpt-4-long-output" not in body
        metrics.record_mail_proposal(decision="accepted", source="model")
        body = _text(registry)
        assert 'careerops_mail_proposals_total{decision="accepted",source="model"} 1.0' in body

    def test_send_intent_phase_closed_vocab(self) -> None:
        metrics, registry = _metrics()
        for phase in ("pending", "sent", "failed", "reconciliation_required"):
            metrics.record_send_intent(phase=phase)
        # Unknown phase dropped.
        metrics.record_send_intent(phase="auto_sent_without_approval")
        body = _text(registry)
        assert 'careerops_send_intents_total{phase="sent"} 1.0' in body
        assert "auto_sent_without_approval" not in body

    def test_review_latency_non_positive_dropped(self) -> None:
        metrics, registry = _metrics()
        metrics.observe_review_latency(review_kind="mail_proposal", seconds=-5.0)
        metrics.observe_review_latency(review_kind="mail_proposal", seconds=0.0)
        metrics.observe_review_latency(review_kind="mail_proposal", seconds=42.0)
        body = _text(registry)
        # The bucket covering 42s (60s bucket) has exactly one observation.
        assert "careerops_review_latency_seconds_count" in body
        assert 'review_kind="mail_proposal"' in body
        # Negative/zero observations must not create spurious series.
        assert 'le="-5' not in body


class TestGauges:
    def test_pending_approvals_and_backlog_track_state(self) -> None:
        metrics, registry = _metrics()
        metrics.inc_pending_approvals(kind="package")
        metrics.inc_pending_approvals(kind="package")
        metrics.inc_pending_approvals(kind="reply")
        metrics.inc_reconciliation_backlog()
        body = _text(registry)
        assert 'careerops_pending_approvals{kind="package"} 2.0' in body
        assert 'careerops_pending_approvals{kind="reply"} 1.0' in body
        assert "careerops_reconciliation_backlog 1.0" in body
        metrics.dec_pending_approvals(kind="package")
        metrics.dec_reconciliation_backlog()
        body = _text(registry)
        assert 'careerops_pending_approvals{kind="package"} 1.0' in body
        assert "careerops_reconciliation_backlog 0.0" in body

    def test_pending_kind_closed_vocab(self) -> None:
        metrics, registry = _metrics()
        metrics.inc_pending_approvals(kind="secret_kind_with_pii@x")
        body = _text(registry)
        assert "secret_kind_with_pii" not in body
