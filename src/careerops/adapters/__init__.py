"""Job source adapter contracts.

The concrete legacy adapter classes were removed in Task 11 (Phase C); every
source type is now driven declaratively by a YAML recipe executed by
:class:`careerops.recipes.engine.RecipeEngine`. This package re-exports the
shared value types and Protocol contracts the rest of the crawl pipeline
depends on; recipe-driven crawls import ``RecipeEngine`` from
``careerops.recipes`` directly.
"""

from careerops.adapters.job_sources import (
    AdapterFetchResult,
    DetailJobSourceAdapter,
    JobSourceAdapter,
    RawJobRecord,
)

__all__ = [
    "AdapterFetchResult",
    "DetailJobSourceAdapter",
    "JobSourceAdapter",
    "RawJobRecord",
]
