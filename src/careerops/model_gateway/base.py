"""Structured model client port and disabled adapter.

Safety invariants:
- MODEL_PROVIDER=disabled is the default and tested state.
- The disabled adapter never calls an external provider.
- All results from the disabled adapter are review_only/unknown.
- Untrusted content is isolated in the prompt envelope; model has no tool binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ModelProviderDisabled(Exception):
    """Raised when a model call is attempted while MODEL_PROVIDER=disabled."""


@dataclass(frozen=True, slots=True)
class StructuredModelRequest:
    """A request to the structured model client.

    The prompt_envelope isolates untrusted content. The model has no tool binding;
    instructions within untrusted_content are treated as data only.

    ``schema`` is an optional JSON Schema dict; when set, the adapter validates
    the parsed output against it (one repair attempt, then ModelInvocationError).
    When None, no schema validation is applied (backward-compatible).
    """

    task_type: str
    system_prompt: str = ""
    user_prompt: str = ""
    untrusted_content: str = ""
    schema_name: str = ""
    schema: dict[str, object] | None = None
    timeout_seconds: float = 30.0
    max_tokens: int = 1024
    trace_id: str = ""
    metadata: dict[str, str] = field(default_factory=lambda: {})

    def __post_init__(self) -> None:
        if self.timeout_seconds < 0 or self.timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120")
        if self.max_tokens < 1 or self.max_tokens > 8192:
            raise ValueError("max_tokens must be between 1 and 8192")


@dataclass(frozen=True, slots=True)
class StructuredModelResponse:
    """A validated response from the structured model client."""

    task_type: str
    result: dict[str, Any] = field(default_factory=lambda: {})
    confidence: float = 0.0
    model_id: str = ""
    prompt_version: str = ""
    is_review_only: bool = True
    repair_attempted: bool = False
    trace_id: str = ""
    # Usage is returned as bounded metadata so durable Agent runs can record
    # cost/diagnostic facts without retaining prompts or provider responses.
    input_tokens: int = 0
    output_tokens: int = 0


class StructuredModelClient(Protocol):
    """Port for structured model inference.

    Implementations must:
    - Validate output against the expected schema.
    - Allow at most one structure repair attempt.
    - Respect timeout and token budget.
    - Redact sensitive fields before logging.
    - Never grant tool binding to the model.
    """

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        """Invoke the model with a structured request and return validated response."""
        ...

    @property
    def is_enabled(self) -> bool:
        """Whether this adapter can actually call an external provider."""
        ...


class DisabledModelAdapter:
    """Default adapter when MODEL_PROVIDER=disabled.

    Never calls an external provider. Returns review_only responses with
    unknown/empty results. This is the safe default state.
    """

    @property
    def is_enabled(self) -> bool:
        return False

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        """Return a review_only response without calling any provider."""
        return StructuredModelResponse(
            task_type=request.task_type,
            result={},
            confidence=0.0,
            model_id="disabled",
            prompt_version="none",
            is_review_only=True,
            repair_attempted=False,
            trace_id=request.trace_id,
        )
