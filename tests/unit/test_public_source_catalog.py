from careerops.application.public_source_catalog import (
    PUBLIC_JOB_SOURCES,
    validate_public_source_catalog,
)


def test_catalog_has_at_least_one_hundred_unique_public_sources() -> None:
    validate_public_source_catalog()

    identities = {
        (source.source_type, source.source_identifier)
        for source in PUBLIC_JOB_SOURCES
    }
    assert len(PUBLIC_JOB_SOURCES) >= 100
    assert len(identities) == len(PUBLIC_JOB_SOURCES)


def test_catalog_uses_supported_public_ats_endpoints() -> None:
    for source in PUBLIC_JOB_SOURCES:
        assert source.source_type in {"greenhouse", "lever", "ashby"}
        assert source.base_url.startswith("https://")
