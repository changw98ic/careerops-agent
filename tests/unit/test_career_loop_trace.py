"""Trace-backed product metric instrumentation tests (task 16.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prometheus_client import CollectorRegistry, generate_latest

from careerops.observability.career_loop_metrics import CareerLoopMetrics
from careerops.observability.career_loop_trace import (
    CareerLoopTrace,
    CareerTraceMetric,
    bind_trace_id,
    current_trace_id,
)


def test_trace_binds_id_and_records_product_signals() -> None:
    registry = CollectorRegistry(auto_describe=True)
    trace = CareerLoopTrace(CareerLoopMetrics(registry=registry))
    first_seen = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    shortlisted = first_seen + timedelta(minutes=12)

    with bind_trace_id("shortlist-trace-1"):
        assert current_trace_id() == "shortlist-trace-1"
        trace.record_trusted_shortlist(
            first_seen_at=first_seen,
            shortlisted_at=shortlisted,
        )
        trace.record_user_correction(stage="package")
        trace.record_confirmed_submission(channel="external_form")
        trace.record_mail_linkage(outcome="linked")
        trace.record_follow_up(event="completed")

    events = trace.snapshot()
    assert [event.metric for event in events] == [
        CareerTraceMetric.TRUSTED_SHORTLIST,
        CareerTraceMetric.USER_CORRECTION,
        CareerTraceMetric.CONFIRMED_SUBMISSION,
        CareerTraceMetric.MAIL_LINKAGE,
        CareerTraceMetric.FOLLOW_UP,
    ]
    assert all(event.trace_id == "shortlist-trace-1" for event in events)

    body = generate_latest(registry).decode()
    assert "careerops_trusted_shortlist_seconds_count 1.0" in body
    assert 'careerops_user_corrections_total{stage="package"} 1.0' in body
    assert 'careerops_confirmed_submissions_total{channel="external_form"} 1.0' in body
    assert 'careerops_mail_linkage_total{outcome="linked"} 1.0' in body
    assert 'careerops_follow_up_total{event="completed"} 1.0' in body


def test_trace_drops_unbounded_values_and_invalid_durations() -> None:
    registry = CollectorRegistry(auto_describe=True)
    trace = CareerLoopTrace(CareerLoopMetrics(registry=registry))
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)

    with bind_trace_id("safe-trace"):
        trace.record_user_correction(stage="secret@example.com")
        trace.record_confirmed_submission(channel="gmail-token")
        trace.record_mail_linkage(outcome="raw-message-body")
        trace.record_trusted_shortlist(first_seen_at=now, shortlisted_at=now - timedelta(seconds=1))

    assert trace.snapshot() == ()
    body = generate_latest(registry).decode()
    assert "secret@example.com" not in body
    assert "gmail-token" not in body
    assert "raw-message-body" not in body
    assert "careerops_trusted_shortlist_seconds_count 0.0" in body
