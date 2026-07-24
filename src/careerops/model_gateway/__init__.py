"""Model gateway: structured model client port, adapters, and factory.

M2.2: MODEL_PROVIDER=disabled is the default and tested state.
The disabled adapter always returns review_required/unknown results.

Enabled providers (xiaomi/zhipu) use the Anthropic-compatible adapter and are
qualification-bound per ADR 0006; see docs/adr/0006-model-privacy.md.
"""

from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
    LLMUsageRecorder,
    ModelInvocationError,
    normalize_model_name,
)
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    ModelProviderDisabled,
    StructuredModelClient,
    StructuredModelRequest,
    StructuredModelResponse,
)
from careerops.model_gateway.factory import (
    ModelProviderNotConfigured,
    create_model_client,
)

__all__ = [
    "AnthropicCompatClient",
    "AnthropicCompatConfig",
    "DisabledModelAdapter",
    "LLMUsageRecorder",
    "ModelInvocationError",
    "ModelProviderDisabled",
    "ModelProviderNotConfigured",
    "StructuredModelClient",
    "StructuredModelRequest",
    "StructuredModelResponse",
    "create_model_client",
    "normalize_model_name",
]
