from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from careerops.evaluation.d0_leakage import (
    D0LeakageError,
    D0LeakageFinding,
    analyze_d0_leakage,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _row(split: str, **values: Any) -> dict[str, Any]:
    return {"split": split, "synthetic": False, **values}


def test_passes_when_repeated_provenance_stays_within_one_split() -> None:
    rows = [
        _row("development", source_url="https://example.test/a"),
        {
            **_row("development", source_url="https://example.test/a"),
            "synthetic": True,
            "nested": {"source_url": "https://example.test/a"},
        },
        _row("holdout", source_url="https://example.test/b"),
    ]

    report = analyze_d0_leakage(rows)

    assert report.passed is True
    assert report.checked_row_count == 3
    assert report.findings == ()


def test_reports_cross_split_same_key_value_without_leaking_values_or_ids() -> None:
    rows = [
        _row(
            "development",
            sample_id="dev-secret-id",
            metadata={"source_url": "https://private.example/thread/1"},
        ),
        _row(
            "holdout",
            sample_id="holdout-secret-id",
            metadata={"source_url": "https://private.example/thread/1"},
        ),
    ]

    report = analyze_d0_leakage(rows)

    assert report.passed is False
    assert report.findings == (
        D0LeakageFinding(key="source_url", splits=("development", "holdout"), collision_count=1),
    )
    rendered = repr(report.findings)
    assert "private.example" not in rendered
    assert "secret-id" not in rendered


def test_canonicalizes_urls_across_field_names() -> None:
    rows = [
        _row(
            "development",
            source_url="HTTPS://xn--bcher-kva.Example:443/a/./b/../%7euser#first",
        ),
        _row(
            "holdout",
            canonical_url="https://xn--bcher-kva.example/a/~user#second",
        ),
    ]

    assert analyze_d0_leakage(rows).findings == (
        D0LeakageFinding(key="source_url", splits=("development", "holdout"), collision_count=1),
    )


def test_discovers_nested_source_url_and_official_careers_entry_urls() -> None:
    rows = [
        _row(
            "development",
            discovery={"source": {"url": "https://example.test/company/careers"}},
        ),
        _row(
            "validation",
            official_careers_entry="https://example.test/company/careers",
        ),
    ]

    assert analyze_d0_leakage(rows).findings == (
        D0LeakageFinding(
            key="source_url",
            splits=("development", "validation"),
            collision_count=1,
        ),
    )


def test_canonicalizes_empty_path_trailing_dns_dot_and_reserved_percent_case() -> None:
    rows = [
        _row("development", source_url="http://EXAMPLE.test.:80?next=%2fprivate"),
        _row("validation", url_or_thread_ref="http://example.test/?next=%2Fprivate"),
    ]

    assert analyze_d0_leakage(rows).findings == (
        D0LeakageFinding(key="source_url", splits=("development", "validation"), collision_count=1),
    )


def test_canonicalizes_unicode_path_to_utf8_percent_encoding() -> None:
    rows = [
        _row("development", source_url="https://example.test/café"),
        _row("holdout", canonical_url="https://example.test/caf%C3%A9"),
    ]

    assert analyze_d0_leakage(rows).findings == (
        D0LeakageFinding(key="source_url", splits=("development", "holdout"), collision_count=1),
    )


def test_rejects_unicode_hosts_instead_of_idna2003_merging_fass() -> None:
    with pytest.raises(D0LeakageError, match="malformed URL"):
        analyze_d0_leakage([_row("development", source_url="https://faß.de/jobs")])


def test_keeps_modern_alabel_distinct_from_ascii_lookalike_host() -> None:
    rows = [
        _row("development", source_url="https://xn--fa-hia.de/jobs"),
        _row("holdout", source_url="https://fass.de/jobs"),
    ]

    assert analyze_d0_leakage(rows).findings == ()


def test_allows_controlled_thread_refs_only_for_url_or_thread_ref() -> None:
    rows = [
        _row(
            "development",
            source={"kind": "established_thread", "url_or_thread_ref": "18f0abc123"},
        ),
        _row(
            "holdout",
            source={"kind": "established_thread", "url_or_thread_ref": "18f0abc123"},
        ),
    ]

    assert analyze_d0_leakage(rows).findings == (
        D0LeakageFinding(key="thread_ref", splits=("development", "holdout"), collision_count=1),
    )


def test_rejects_uncontrolled_thread_like_refs_without_url_bypass() -> None:
    for value in ("relative/path", "thread_/../escape", "thread_https://example.test"):
        with pytest.raises(D0LeakageError, match="malformed URL"):
            analyze_d0_leakage(
                [
                    _row(
                        "development",
                        source={"kind": "established_thread", "url_or_thread_ref": value},
                    )
                ]
            )


def test_non_thread_source_cannot_smuggle_opaque_ref() -> None:
    with pytest.raises(D0LeakageError, match="malformed URL"):
        analyze_d0_leakage(
            [
                _row(
                    "development",
                    source={"kind": "job_page", "url_or_thread_ref": "18f0abc123"},
                )
            ]
        )


def test_merges_content_and_snippet_hashes_but_not_payload_hashes() -> None:
    content_report = analyze_d0_leakage(
        [
            _row("development", content_sha256=SHA_A),
            _row("holdout", snippet_sha256=SHA_A.upper()),
        ]
    )
    unrelated_report = analyze_d0_leakage(
        [
            _row("development", content_sha256=SHA_A),
            _row("holdout", payload_sha256=SHA_A),
        ]
    )

    assert content_report.findings == (
        D0LeakageFinding(
            key="content_sha256",
            splits=("development", "holdout"),
            collision_count=1,
        ),
    )
    assert unrelated_report.findings == ()


def test_recurses_through_nested_lists_and_deduplicates_pairs() -> None:
    rows = [
        _row(
            "development",
            events=[
                {"payload_sha256": SHA_A},
                {"payload_sha256": SHA_A},
                {"source": {"payload_sha256": SHA_B}},
            ],
        ),
        _row(
            "validation",
            payloads=[{"payload_sha256": SHA_A}, {"payload_sha256": SHA_B}],
        ),
    ]

    report = analyze_d0_leakage(rows)

    assert report.findings == (
        D0LeakageFinding(
            key="payload_sha256",
            splits=("development", "validation"),
            collision_count=2,
        ),
    )


def test_bounds_findings_and_marks_truncated() -> None:
    rows = [
        _row("development", content_sha256=SHA_A, source_url="https://example.test/a"),
        _row("holdout", content_sha256=SHA_A, source_url="https://example.test/a"),
    ]

    report = analyze_d0_leakage(rows, max_findings=1)

    assert len(report.findings) == 1
    assert report.truncated is True
    assert report.passed is False


def test_ignores_empty_signal_values() -> None:
    rows = [
        _row("development", external_id="", source_url=""),
        _row("holdout", external_id=" ", source_url=" "),
    ]

    assert analyze_d0_leakage(rows).findings == ()


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ("relative/path", "source_url"),
        ("https://", "canonical_url"),
        ("https://user:password@example.test/a", "source_url"),
        ("https://example.test:99999/a", "source_url"),
        ("https://example.test:/a", "source_url"),
        ("https://example.test/%", "source_url"),
        ("https://example.test/%GG", "source_url"),
        ("https://example.test/%00", "source_url"),
        ("https://example.test/a#%0a", "source_url"),
        ("https://example.test/a\\b", "source_url"),
        ("https://example.test/a\nprivate", "source_url"),
        ("https://example.test/a{b}", "source_url"),
        ("https://example.test/a?next={private}", "source_url"),
        ("https://-invalid.example/a", "source_url"),
        ("https://invalid_host.example/a", "source_url"),
    ],
)
def test_rejects_malformed_urls_without_echoing_them(value: str, field: str) -> None:
    with pytest.raises(D0LeakageError, match="malformed URL") as caught:
        analyze_d0_leakage([_row("development", **{field: value})])

    assert value not in str(caught.value)
    assert "password" not in str(caught.value)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ([], "row must be an object"),
        ({"split": "", "synthetic": False}, "split must be a non-empty string"),
        ({"split": "secret-split", "synthetic": False}, "unrecognized split"),
        ({"split": "holdout", "synthetic": True}, "synthetic rows are only allowed"),
        ({"split": "development", "synthetic": "false"}, "synthetic must be a boolean"),
        (
            {"split": "development", "synthetic": False, "nested": {1: "bad"}},
            "object keys must be strings",
        ),
        (
            {"split": "development", "synthetic": False, "source_url": ["bad"]},
            "URL provenance value must be a string",
        ),
        (
            {"split": "development", "synthetic": False, "external_id": True},
            "provenance value must not be boolean",
        ),
        (
            {"split": "development", "synthetic": False, "content_sha256": "short"},
            "malformed SHA-256",
        ),
        (
            {"split": "development", "synthetic": False, "external_id": "secret\nvalue"},
            "control characters",
        ),
    ],
)
def test_rejects_hostile_or_malformed_rows_without_echoing_values(row: Any, expected: str) -> None:
    with pytest.raises(D0LeakageError, match=expected) as caught:
        analyze_d0_leakage([row])

    rendered = str(caught.value)
    assert "secret-split" not in rendered
    assert "secret\nvalue" not in rendered


def test_rejects_non_finite_numeric_provenance() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(D0LeakageError, match="provenance value must be finite"):
            analyze_d0_leakage([_row("development", external_id=value)])


def test_explicit_stack_turns_extreme_nesting_into_bounded_error() -> None:
    nested: dict[str, Any] = {"external_id": "never-reached"}
    for _ in range(5_000):
        nested = {"nested": nested}

    with pytest.raises(D0LeakageError, match="nesting depth limit exceeded"):
        analyze_d0_leakage([_row("development", nested=nested)], max_depth=32)


def test_bounds_rows_without_consuming_unbounded_iterable() -> None:
    consumed = 0

    def rows() -> Iterator[dict[str, Any]]:
        nonlocal consumed
        while True:
            consumed += 1
            yield _row("development")

    with pytest.raises(D0LeakageError, match="row limit exceeded"):
        analyze_d0_leakage(rows(), max_rows=3)

    assert consumed == 4


def test_bounds_traversed_nodes() -> None:
    with pytest.raises(D0LeakageError, match="traversal node limit exceeded"):
        analyze_d0_leakage([_row("development", nested=[{}, {}, {}])], max_nodes=2)


def test_bounds_unique_signals() -> None:
    rows = [
        _row("development", external_id="one"),
        _row("development", external_id="two"),
    ]

    with pytest.raises(D0LeakageError, match="unique provenance signal limit exceeded"):
        analyze_d0_leakage(rows, max_unique_signals=1)


def test_bounds_per_signal_and_url_lengths() -> None:
    with pytest.raises(D0LeakageError, match="provenance value length limit exceeded"):
        analyze_d0_leakage([_row("development", external_id="x" * 4_097)])

    with pytest.raises(D0LeakageError, match="URL provenance value length limit exceeded"):
        analyze_d0_leakage([_row("development", source_url=f"https://example.test/{'x' * 8_200}")])

    with pytest.raises(D0LeakageError, match="malformed URL"):
        analyze_d0_leakage(
            [_row("development", source_url=f"https://example.test/?q={'x' * 4_097}")]
        )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("max_findings", 0),
        ("max_rows", 0),
        ("max_nodes", 0),
        ("max_depth", -1),
        ("max_unique_signals", 0),
        ("max_findings", True),
    ],
)
def test_rejects_invalid_resource_limits(argument: str, value: Any) -> None:
    with pytest.raises(D0LeakageError, match=argument):
        analyze_d0_leakage([], **{argument: value})
