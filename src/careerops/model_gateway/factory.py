"""Model client factory.

Creates the appropriate ``StructuredModelClient`` for a named provider, reading
connection config from the environment. ``disabled`` (the safe default) returns
the ``DisabledModelAdapter``; ``xiaomi`` / ``zhipu`` return an
``AnthropicCompatClient`` pointed at the provider's Anthropic-compatible endpoint.

Provider connection is read from environment variables (not the app ``Settings``
startup gate, which keeps ``model_provider=disabled`` as the production-safe
default per ADR 0006). A capability is only enabled when its ADR 0006 addendum
records the privacy qualification.
"""

from __future__ import annotations

import os

from careerops.config import get_settings
from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
)
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    StructuredModelClient,
)

# provider name -> (base_url env, api_key env, model env)
_PROVIDER_ENV: dict[str, tuple[str, str, str]] = {
    "xiaomi": ("XIAOMI_BASE_URL", "XIAOMI_API_KEY", "XIAOMI_MODEL"),
    "zhipu": ("ZHIPU_BASE_URL", "ZHIPU_API_KEY", "ZHIPU_MODEL"),
}


class ModelProviderNotConfigured(RuntimeError):
    """Raised when a requested provider lacks required environment config."""


def create_model_client(provider: str) -> StructuredModelClient:
    """Create a model client for the given provider name.

    ``disabled`` returns the safe DisabledModelAdapter. ``xiaomi``/``zhipu``
    return an Anthropic-compatible client configured from the environment.
    """
    if provider in ("", "disabled"):
        return DisabledModelAdapter()

    env_names = _PROVIDER_ENV.get(provider)
    if env_names is None:
        raise ModelProviderNotConfigured(
            f"unknown model provider '{provider}'; supported: "
            f"{', '.join(['disabled', *_PROVIDER_ENV])}"
        )

    base_url_env, api_key_env, model_env = env_names
    base_url = os.environ.get(base_url_env, "")
    api_key = os.environ.get(api_key_env, "")
    model = os.environ.get(model_env, "")
    missing = [
        name
        for name, value in (
            (base_url_env, base_url),
            (api_key_env, api_key),
            (model_env, model),
        )
        if not value
    ]
    if missing:
        raise ModelProviderNotConfigured(
            f"provider '{provider}' is missing env config: {', '.join(missing)}"
        )

    settings = get_settings()
    if not settings.model_qualification_artifact or not settings.model_qualification_version:
        raise ModelProviderNotConfigured(
            f"provider '{provider}' requires model_qualification_artifact and "
            f"model_qualification_version to be set (ADR 0006 qualification)"
        )

    return AnthropicCompatClient(
        AnthropicCompatConfig(base_url=base_url, api_key=api_key, model=model)
    )
