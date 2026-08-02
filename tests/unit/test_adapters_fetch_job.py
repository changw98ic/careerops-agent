"""Unit tests for ``RawJobRecord`` value semantics.

Phase C (Task 11) removed the hard-coded Greenhouse / Ashby / JsonLd detail
and list adapters — every source type is now driven declaratively by a
recipe executed through :class:`careerops.recipes.engine.RecipeEngine`, and
the engine + recipe parity suites cover the field-mapping behaviour the
removed ``TestGreenhouseDetailAdapterFetchJob`` /
``TestAshbyDetailAdapterFetchJob`` / ``TestJsonLdAdapterDescriptionMapping``
classes used to pin. This module keeps the value-type tests for
:class:`RawJobRecord`, which remains the shared parser-layer contract.
"""

from __future__ import annotations

from careerops.adapters import RawJobRecord


class TestRawJobRecordDescription:
    def test_description_defaults_to_empty_string(self) -> None:
        record = RawJobRecord(external_id="gh-123", title="Engineer")

        assert record.description == ""

    def test_description_is_distinct_from_raw_data(self) -> None:
        record = RawJobRecord(
            external_id="gh-123",
            title="Engineer",
            description="<p>JD body</p>",
            raw_data={"content": "<p>JD body</p>"},
        )

        assert record.description == "<p>JD body</p>"
        assert record.raw_data == {"content": "<p>JD body</p>"}
