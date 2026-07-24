"""Job source adapters for ATS platforms and web scraping."""

from careerops.adapters.job_sources import (
    ALL_ADAPTERS,
    AdapterFetchResult,
    AshbyAdapter,
    AshbyDetailAdapter,
    DetailJobSourceAdapter,
    GreenhouseAdapter,
    GreenhouseDetailAdapter,
    JobSourceAdapter,
    JsonLdAdapter,
    LeverAdapter,
    RawJobRecord,
    SitemapAdapter,
    StaticHtmlAdapter,
)

__all__ = [
    "ALL_ADAPTERS",
    "AdapterFetchResult",
    "AshbyAdapter",
    "AshbyDetailAdapter",
    "DetailJobSourceAdapter",
    "GreenhouseAdapter",
    "GreenhouseDetailAdapter",
    "JobSourceAdapter",
    "JsonLdAdapter",
    "LeverAdapter",
    "RawJobRecord",
    "SitemapAdapter",
    "StaticHtmlAdapter",
]
