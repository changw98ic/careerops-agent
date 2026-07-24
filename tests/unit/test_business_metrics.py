"""Unit tests for business metrics wiring.

Verifies that ``careerops_llm_tokens_total``, ``careerops_apply_submitted_total``,
and ``careerops_interview_received_total`` counters are correctly incremented and
rendered.
"""

from __future__ import annotations

from careerops.config import Settings
from careerops.observability.metrics import Metrics


def _metrics() -> Metrics:
    settings = Settings()  # type: ignore[call-arg]
    return Metrics(version="test", settings=settings)


def test_llm_tokens_counter_increments() -> None:
    m = _metrics()
    m.record_llm_tokens(input_tokens=100, output_tokens=50)
    m.record_llm_tokens(input_tokens=200, output_tokens=75)
    rendered = m.render().decode()
    assert "careerops_llm_tokens_total" in rendered
    # input tokens: 300 total
    assert 'careerops_llm_tokens_total{component="provider",token_kind="input"} 300.0' in rendered
    # output tokens: 125 total
    assert 'careerops_llm_tokens_total{component="provider",token_kind="output"} 125.0' in rendered


def test_llm_tokens_noop_on_zero() -> None:
    m = _metrics()
    m.record_llm_tokens(input_tokens=0, output_tokens=0)
    rendered = m.render().decode()
    # Counters should still be at 0.0 (registered but not incremented).
    assert 'careerops_llm_tokens_total{component="provider",token_kind="input"} 0.0' in rendered


def test_apply_submitted_counter_increments() -> None:
    m = _metrics()
    m.record_apply_submitted()
    m.record_apply_submitted(amount=3)
    rendered = m.render().decode()
    assert "careerops_apply_submitted_total" in rendered
    assert "careerops_apply_submitted_total 4.0" in rendered


def test_apply_submitted_noop_on_nonpositive() -> None:
    m = _metrics()
    m.record_apply_submitted(amount=0)
    rendered = m.render().decode()
    assert "careerops_apply_submitted_total 0.0" in rendered


def test_interview_received_counter_increments() -> None:
    m = _metrics()
    m.record_interview_received()
    m.record_interview_received(amount=2)
    rendered = m.render().decode()
    assert "careerops_interview_received_total" in rendered
    assert "careerops_interview_received_total 3.0" in rendered


def test_interview_received_noop_on_nonpositive() -> None:
    m = _metrics()
    m.record_interview_received(amount=-1)
    rendered = m.render().decode()
    assert "careerops_interview_received_total 0.0" in rendered


def test_operations_counter_registered() -> None:
    m = _metrics()
    rendered = m.render().decode()
    assert "careerops_operations_total" in rendered


def test_http_requests_counter_registered() -> None:
    m = _metrics()
    rendered = m.render().decode()
    assert "careerops_http_requests_total" in rendered
