"""Model gateway: structured model client port and disabled adapter.

M2.2: MODEL_PROVIDER=disabled is the default and tested state.
The disabled adapter always returns review_required/unknown results.
"""

from careerops.model_gateway.base import (
    DisabledModelAdapter,
    ModelProviderDisabled,
    StructuredModelClient,
    StructuredModelRequest,
    StructuredModelResponse,
)

__all__ = [
    "DisabledModelAdapter",
    "ModelProviderDisabled",
    "StructuredModelClient",
    "StructuredModelRequest",
    "StructuredModelResponse",
]
