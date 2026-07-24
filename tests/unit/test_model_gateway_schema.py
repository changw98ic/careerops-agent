"""Unit tests for Stage 0b schema validation in the model gateway.

Covers v0.4 plan §2.9: when ``StructuredModelRequest.schema`` is set, the
Anthropic-compatible adapter validates parsed output with ``jsonschema``;
a single repair attempt covers both JSON-parse and schema-validation errors,
and persistent failure raises ``ModelInvocationError`` (never silent).

No real model calls are made: ``AnthropicCompatClient._call_messages`` is
replaced with a deterministic fake per test.
"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
    ModelInvocationError,
)
from careerops.model_gateway.base import StructuredModelRequest

# Load the real packaged schema so the tests also cover the artifact on disk.
SCHEMA = json.loads(
    files("careerops.model_gateway").joinpath("schemas", "job_match.json").read_text()
)

# A fully-valid result matching the job_match schema.
VALID_RESULT: dict[str, object] = {
    "match_score": 75,
    "tier": "partial",
    "matched_requirements": ["Python"],
    "gaps": ["Rust"],
    "transferable_skills": ["Go"],
    "seniority_fit": "match",
    "remote_compatible": True,
    "reasoning": "Solid backend overlap with one core gap.",
    "recommendation": "consider",
    "confidence": 0.7,
}


def _make_client(
    responses: list[str],
) -> tuple[AnthropicCompatClient, dict[str, int]]:
    """Build a client whose _call_messages returns canned responses in order.

    Returns the client and a mutable call-counter so tests can assert how many
    provider calls happened (1 = no repair, 2 = one repair).
    """
    config = AnthropicCompatConfig(
        base_url="https://example.test",
        api_key="test-key",
        model="test-model[1m]",
    )
    client = AnthropicCompatClient(config)
    calls = {"count": 0}

    def fake_call(
        *,
        system: str,
        user: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        idx = calls["count"]
        calls["count"] += 1
        return responses[idx]

    client._call_messages = fake_call  # type: ignore[method-assign]
    return client, calls


def _request(**overrides: object) -> StructuredModelRequest:
    base: dict[str, object] = {
        "task_type": "job_match",
        "user_prompt": "Analyze this candidate against the job.",
        "schema": SCHEMA,
    }
    base.update(overrides)
    return StructuredModelRequest(**base)  # type: ignore[arg-type]


class TestSchemaValidation:
    """Schema-gated invoke behavior (Stage 0b)."""

    def test_valid_json_passes_without_repair(self) -> None:
        client, calls = _make_client([json.dumps(VALID_RESULT)])

        response = client.invoke(_request())

        assert response.result == VALID_RESULT
        assert response.repair_attempted is False
        assert response.is_review_only is True
        assert calls["count"] == 1  # no repair call

    def test_field_misaligned_json_raises_after_one_repair(self) -> None:
        # Fields misaligned vs the schema: match_score is a string (wrong type),
        # and a required field (recommendation) is missing. Still valid JSON, so
        # without schema validation it would silently pass.
        misaligned = dict(VALID_RESULT)
        misaligned["match_score"] = "seventy-five"  # wrong type
        misaligned.pop("recommendation")  # missing required
        client, calls = _make_client([json.dumps(misaligned), json.dumps(misaligned)])

        with pytest.raises(ModelInvocationError):
            client.invoke(_request())

        # Exactly one repair attempt, then hard failure.
        assert calls["count"] == 2

    def test_valid_after_one_repair_marks_repair_attempted(self) -> None:
        broken = dict(VALID_RESULT)
        broken.pop("recommendation")  # schema-invalid on first pass
        client, calls = _make_client([json.dumps(broken), json.dumps(VALID_RESULT)])

        response = client.invoke(_request())

        assert response.repair_attempted is True
        assert response.result == VALID_RESULT
        assert response.confidence == pytest.approx(0.7)
        assert calls["count"] == 2  # initial + one repair

    def test_schema_none_skips_validation_backward_compat(self) -> None:
        # A payload that would fail the schema, but schema is None -> no
        # validation, no repair, returned as-is.
        shapeless = {"completely_unrelated": "shape", "n": 1}
        client, calls = _make_client([json.dumps(shapeless)])

        response = client.invoke(_request(schema=None))

        assert response.result == shapeless
        assert response.repair_attempted is False
        assert calls["count"] == 1

    def test_unparseable_json_then_valid_passes_with_repair(self) -> None:
        # Pre-schema failure: the first response isn't even valid JSON; the
        # single repair path must still cover this and then validate.
        client, calls = _make_client(["<<<not json at all>>>", json.dumps(VALID_RESULT)])

        response = client.invoke(_request())

        assert response.repair_attempted is True
        assert response.result == VALID_RESULT
        assert calls["count"] == 2
