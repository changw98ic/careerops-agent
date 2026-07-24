"""Unit tests for LLM token + apply/interview business metrics (plan v0.4 §3).

Stage 3.5 observability:

- ``careerops_llm_tokens_total`` records aggregate token COUNTS only. ADR 0006
  forbids any prompt/response content in metrics; these tests seed canary
  strings in the prompt and the model response and assert they NEVER appear in
  the rendered metrics output, while the aggregate token counts do.
- ``careerops_apply_submitted_total`` / ``careerops_interview_received_total``
  are v1 instrumentation counters. Conversion analysis lands once real
  apply/interview data flows in (data-availability caveat documented inline).

No real network: ``urllib.request.urlopen`` is monkeypatched.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from careerops.config import Settings
from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
)
from careerops.model_gateway.base import StructuredModelRequest
from careerops.observability.metrics import Metrics


def _metrics() -> Metrics:
    return Metrics(version="test", settings=Settings())


def _config() -> AnthropicCompatConfig:
    return AnthropicCompatConfig(
        base_url="https://provider.test",
        api_key="test-key",
        model="test-model",
    )


def _messages_body(text: str, *, input_tokens: int = 0, output_tokens: int = 0) -> bytes:
    return json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    ).encode("utf-8")


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _sample_value(rendered: str, metric: str, **labels: str) -> float:
    """Return the sample value for a metric+labels from rendered Prometheus text.

    Handles both labeled series (``metric{k="v"} value``) and label-less
    counters (``metric value``), skipping ``#`` comment lines.
    """
    label_substrings = [f'{k}="{v}"' for k, v in labels.items()]
    candidate = metric + "{" if labels else metric + " "
    for line in rendered.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(candidate) and all(sub in line for sub in label_substrings):
            return float(line.rsplit(" ", 1)[-1])
    raise AssertionError(f"no sample for {metric} {labels} in output")


# ---------------------------------------------------------------------------
# LLM token aggregate (ADR 0006: counts only)
# ---------------------------------------------------------------------------


class TestLLMTokenAggregate:
    def test_records_input_and_output_token_counts(self) -> None:
        metrics = _metrics()
        metrics.record_llm_tokens(input_tokens=10, output_tokens=25)
        rendered = metrics.render().decode()

        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 10.0
        )
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="output"
            )
            == 25.0
        )

    def test_accumulates_across_calls(self) -> None:
        metrics = _metrics()
        metrics.record_llm_tokens(input_tokens=10, output_tokens=4)
        metrics.record_llm_tokens(input_tokens=5, output_tokens=6)
        rendered = metrics.render().decode()

        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 15.0
        )
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="output"
            )
            == 10.0
        )

    def test_zero_counts_are_noop(self) -> None:
        metrics = _metrics()
        metrics.record_llm_tokens(input_tokens=0, output_tokens=0)
        rendered = metrics.render().decode()
        # Pre-seeded series exist at 0; no increment occurred.
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 0.0
        )

    def test_negative_counts_are_ignored(self) -> None:
        metrics = _metrics()
        # A malformed usage block must never decrement the aggregate.
        metrics.record_llm_tokens(input_tokens=-50, output_tokens=-1)
        rendered = metrics.render().decode()
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 0.0
        )


class TestLLMMetricsNoContentLeak:
    """ADR 0006: aggregate counts only; raw prompt/response content never flows."""

    def test_only_token_counts_recorded_never_prompt_or_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Canary values that would be catastrophic if they leaked as content.
        prompt_canary = "PROMPT-CANARY-9f8e7d6c"
        response_canary = "RESPONSE-CANARY-3a2b1c00"
        body = _messages_body(
            f'{{"v": 1, "echo": "{response_canary}"}}',
            input_tokens=11,
            output_tokens=22,
        )
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0.0: _FakeResp(body))

        metrics = _metrics()
        client = AnthropicCompatClient(_config(), usage_recorder=metrics)
        request = StructuredModelRequest(
            task_type="test",
            system_prompt=prompt_canary,
            user_prompt="analyze",
        )
        response = client.invoke(request)

        # Sanity: the canary content really did flow through the adapter, so a
        # content leak would have had the opportunity to be recorded.
        assert response.result.get("echo") == response_canary
        assert response.is_review_only is True

        rendered = metrics.render().decode()
        # The prompt and response text must NEVER appear in metrics output.
        assert prompt_canary not in rendered
        assert response_canary not in rendered
        # The aggregate counts do appear.
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 11.0
        )
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="output"
            )
            == 22.0
        )

    def test_client_without_recorder_does_not_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default construction (no usage_recorder) must not widen the gate or
        # attempt any recording. v1 factory constructs clients this way.
        body = _messages_body('{"v": 0}', input_tokens=99, output_tokens=99)
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0.0: _FakeResp(body))
        metrics = _metrics()
        client = AnthropicCompatClient(_config())  # no recorder wired

        client.invoke(StructuredModelRequest(task_type="test", user_prompt="q"))

        rendered = metrics.render().decode()
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 0.0
        )

    def test_missing_usage_block_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Provider returned no usage -> nothing to record (per plan: "if the
        # provider returns it").
        body = json.dumps({"content": [{"type": "text", "text": '{"v": 1}'}]}).encode("utf-8")
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0.0: _FakeResp(body))
        metrics = _metrics()
        client = AnthropicCompatClient(_config(), usage_recorder=metrics)

        client.invoke(StructuredModelRequest(task_type="test", user_prompt="q"))

        rendered = metrics.render().decode()
        assert (
            _sample_value(
                rendered, "careerops_llm_tokens_total", component="provider", token_kind="input"
            )
            == 0.0
        )


# ---------------------------------------------------------------------------
# Business counters (apply -> interview conversion)
# ---------------------------------------------------------------------------


class TestApplyInterviewCounters:
    """v1 instrumentation pipeline: counters exist and accumulate.

    Data-availability caveat (plan v0.4 §3 Stage 3.5): real apply/interview
    events are wired once the apply and reply ingestion flows land; until then
    these counters stay at 0, which is the correct "no data yet" state.
    """

    def test_apply_submitted_increments(self) -> None:
        metrics = _metrics()
        metrics.record_apply_submitted()
        metrics.record_apply_submitted(3)
        rendered = metrics.render().decode()
        assert "careerops_apply_submitted_total" in rendered
        assert _sample_value(rendered, "careerops_apply_submitted_total") == 4.0

    def test_interview_received_increments(self) -> None:
        metrics = _metrics()
        metrics.record_interview_received(2)
        rendered = metrics.render().decode()
        assert "careerops_interview_received_total" in rendered
        assert _sample_value(rendered, "careerops_interview_received_total") == 2.0

    def test_zero_or_negative_amount_is_noop(self) -> None:
        metrics = _metrics()
        metrics.record_apply_submitted(0)
        metrics.record_interview_received(-5)
        metrics.record_apply_submitted(-1)
        rendered = metrics.render().decode()
        # Counters registered (series present) but never incremented.
        assert _sample_value(rendered, "careerops_apply_submitted_total") == 0.0
        assert _sample_value(rendered, "careerops_interview_received_total") == 0.0

    def test_counters_start_at_zero_before_any_data(self) -> None:
        # v1 "no data yet" state.
        rendered = _metrics().render().decode()
        assert _sample_value(rendered, "careerops_apply_submitted_total") == 0.0
        assert _sample_value(rendered, "careerops_interview_received_total") == 0.0
