"""Model client factory.

Creates the appropriate ``StructuredModelClient`` from user-supplied Settings.
``disabled`` (the safe default) returns the ``DisabledModelAdapter``; any other
value creates an ``AnthropicCompatClient`` pointed at the user's endpoint.

The user configures ``model_base_url``, ``model_api_key``, and ``model_name``
in Settings (or via environment variables ``CAREEROPS_MODEL_BASE_URL``,
``CAREEROPS_MODEL_API_KEY``, ``CAREEROPS_MODEL_NAME``).  No provider-specific
hardcoding — any Anthropic-compatible endpoint works.
"""

from __future__ import annotations

from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
    LLMUsageRecorder,
)
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    StructuredModelClient,
)


class ModelProviderNotConfigured(RuntimeError):
    """Raised when a requested provider lacks required config."""


def create_model_client(
    provider: str,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    usage_recorder: LLMUsageRecorder | None = None,
) -> StructuredModelClient:
    """Create a model client for the given provider.

    ``disabled`` returns the safe DisabledModelAdapter.  Any other value
    creates an Anthropic-compatible client from the supplied ``base_url``,
    ``api_key``, and ``model``.

    The caller (``runtime.py``) reads these values from ``Settings`` and
    passes them in — this function has no Settings dependency.
    """
    if provider in ("", "disabled"):
        return DisabledModelAdapter()

    missing = [
        name
        for name, value in (
            ("base_url", base_url),
            ("api_key", api_key),
            ("model", model),
        )
        if not value
    ]
    if missing:
        raise ModelProviderNotConfigured(
            f"model_provider='{provider}' requires: {', '.join(missing)}"
        )

    return AnthropicCompatClient(
        AnthropicCompatConfig(base_url=base_url, api_key=api_key, model=model),
        usage_recorder=usage_recorder,
    )
