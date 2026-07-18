from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import careerops.evaluation.d0_agreement as agreement
from careerops.evaluation import (
    AdjudicationStatus,
    D0LabelReviewError,
    LabelEvidence,
    calculate_agreement,
    load_label_review_evidence,
    parse_label_review_evidence,
)


def _write(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _label(annotator_id: str, label: str, value: Any) -> dict[str, Any]:
    return {"annotator_id": annotator_id, "label": label, "value": value}


def _adjudication(
    *,
    final_label: str = "contact",
    final_value: Any = None,
) -> dict[str, Any]:
    return {
        "adjudicator_id": "adjudicator-1",
        "final_label": final_label,
        "final_value": final_value,
        "reason": "Resolved against the frozen labeling guide.",
        "evidence_ref": "datasets/reviews/adjudication-s-2.json",
        "evidence_sha256": "a" * 64,
    }


def test_calculates_metrics_from_strict_rows_and_ignores_declared_metrics(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        {
            "rows": [
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "contact", {"b": 2, "a": [1, True]}),
                    "secondary": _label("annotator-b", "contact", {"a": [1, True], "b": 2}),
                    "adjudication_status": "not_required",
                },
                {
                    "sample_id": "s-2",
                    "primary": _label("annotator-a", "review", {"needs_review": True}),
                    "secondary": _label("annotator-b", "contact", {"needs_review": False}),
                    "adjudication_status": "adjudicated",
                    "adjudication": _adjudication(final_value={"needs_review": False}),
                },
                {
                    "sample_id": "s-3",
                    "primary": _label("annotator-a", "review", {"needs_review": True}),
                    "adjudication_status": "pending",
                },
            ],
            "metrics": {
                "double_labeled_count": 999,
                "categorical_kappa": 1.0,
                "structured_exact_agreement": 1.0,
            },
        },
    )

    evidence = load_label_review_evidence(path)
    metrics = calculate_agreement(evidence)

    assert evidence[0].primary_annotator_id == "annotator-a"
    assert evidence[0].secondary_annotator_id == "annotator-b"
    assert evidence[0].adjudication_status is AdjudicationStatus.NOT_REQUIRED
    assert evidence[1].adjudicator_id == "adjudicator-1"
    assert evidence[1].final_value == {"needs_review": False}
    assert metrics.sample_count == 3
    assert metrics.double_labeled_count == 2
    assert metrics.categorical_kappa == 0.0
    assert metrics.structured_exact_agreement == 0.5
    assert metrics.disagreement_count == 1
    assert metrics.all_disagreements_adjudicated is True


def test_loads_jsonl_rows(tmp_path: Path) -> None:
    path = tmp_path / "review.jsonl"
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", {"x": 1}),
            "secondary": _label("annotator-b", "a", {"x": 1}),
            "adjudication_status": "not_required",
        },
        {
            "sample_id": "s-2",
            "primary": _label("annotator-a", "b", {"x": 2}),
            "adjudication_status": "pending",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    metrics = calculate_agreement(load_label_review_evidence(path))

    assert metrics.sample_count == 2
    assert metrics.double_labeled_count == 1
    assert metrics.structured_exact_agreement == 1.0


def test_structured_exact_agreement_treats_double_labeled_null_as_a_value() -> None:
    evidence = parse_label_review_evidence(
        json.dumps(
            [
                {
                    "sample_id": "s-null",
                    "primary": _label("annotator-a", "empty", None),
                    "secondary": _label("annotator-b", "empty", None),
                    "adjudication_status": "not_required",
                }
            ]
        )
    )

    metrics = calculate_agreement(evidence)

    assert metrics.structured_exact_agreement == 1.0
    assert metrics.disagreement_count == 0


def test_no_double_labels_has_undefined_agreement_metrics(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "contact", {"ok": True}),
                "adjudication_status": "pending",
            }
        ],
    )

    metrics = calculate_agreement(load_label_review_evidence(path))

    assert metrics.double_labeled_count == 0
    assert metrics.categorical_kappa is None
    assert metrics.structured_exact_agreement is None
    assert metrics.all_disagreements_adjudicated is True


def test_degenerate_expected_agreement_has_undefined_kappa(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "same", {"x": 1}),
                "secondary": _label("annotator-b", "same", {"x": 1}),
                "adjudication_status": "not_required",
            },
            {
                "sample_id": "s-2",
                "primary": _label("annotator-a", "same", {"x": 2}),
                "secondary": _label("annotator-b", "same", {"x": 2}),
                "adjudication_status": "not_required",
            },
        ],
    )

    metrics = calculate_agreement(load_label_review_evidence(path))

    assert metrics.categorical_kappa is None
    assert metrics.structured_exact_agreement == 1.0


def test_unadjudicated_disagreement_is_reported_incomplete(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 1}),
                "adjudication_status": "pending",
            }
        ],
    )

    metrics = calculate_agreement(load_label_review_evidence(path))

    assert metrics.disagreement_count == 1
    assert metrics.all_disagreements_adjudicated is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            [
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "a", {}),
                    "adjudication_status": "pending",
                },
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "a", {}),
                    "adjudication_status": "pending",
                },
            ],
            "duplicate sample_id",
        ),
        (
            [
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "a", {}),
                    "secondary": {"annotator_id": "annotator-b", "label": "a"},
                    "adjudication_status": "pending",
                }
            ],
            "must contain exactly annotator_id, label, and value",
        ),
        (
            [
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "a", {}),
                    "adjudication_status": "complete",
                }
            ],
            "unrecognized adjudication_status",
        ),
        (
            [
                {
                    "sample_id": "s-1",
                    "primary": _label("annotator-a", "a", {}),
                    "adjudication_status": "pending",
                    "categorical_kappa": 1.0,
                }
            ],
            "unexpected keys",
        ),
    ],
)
def test_rejects_hostile_or_malformed_rows(tmp_path: Path, payload: Any, expected: str) -> None:
    path = _write(tmp_path / "review.json", payload)

    with pytest.raises(D0LabelReviewError, match=expected):
        load_label_review_evidence(path)


def test_rejects_forged_secondary_copied_from_primary(tmp_path: Path) -> None:
    copied = _label("annotator-a", "contact", {"ok": True})
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-sensitive-copy",
                "primary": copied,
                "secondary": dict(copied),
                "adjudication_status": "not_required",
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="must differ") as raised:
        load_label_review_evidence(path)

    assert "s-sensitive-copy" not in str(raised.value)


def test_rejects_same_actor_even_when_labels_differ(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-sensitive-actor",
                "primary": _label("annotator-a", "contact", {"ok": True}),
                "secondary": _label(" annotator-a ", "review", {"ok": False}),
                "adjudication_status": "pending",
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="must differ") as raised:
        load_label_review_evidence(path)

    assert "s-sensitive-actor" not in str(raised.value)


def test_rejects_missing_adjudication_evidence(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-sensitive-adjudication",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "adjudicated",
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="requires adjudication evidence") as raised:
        load_label_review_evidence(path)

    assert "s-sensitive-adjudication" not in str(raised.value)


def test_rejects_partial_adjudication_evidence(tmp_path: Path) -> None:
    evidence = _adjudication(final_label="b", final_value={"x": 2})
    del evidence["reason"]
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "adjudicated",
                "adjudication": evidence,
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="must contain exactly adjudicator_id"):
        load_label_review_evidence(path)


def test_rejects_invalid_adjudication_evidence_hash(tmp_path: Path) -> None:
    evidence = _adjudication(final_label="b", final_value={"x": 2})
    evidence["evidence_sha256"] = "A" * 64
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "adjudicated",
                "adjudication": evidence,
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="lowercase SHA-256"):
        load_label_review_evidence(path)


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        "/absolute/review.json",
        "../outside.json",
        "reviews/../outside.json",
        "C:/review.json",
        "reviews\\review.json",
        "reviews/review\x00.json",
    ],
)
def test_rejects_unsafe_adjudication_evidence_ref(tmp_path: Path, unsafe_ref: str) -> None:
    evidence = _adjudication(final_label="b", final_value={"x": 2})
    evidence["evidence_ref"] = unsafe_ref
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-sensitive-ref",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "adjudicated",
                "adjudication": evidence,
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="safe repository-relative path") as raised:
        load_label_review_evidence(path)

    assert "s-sensitive-ref" not in str(raised.value)


def test_non_adjudicated_row_cannot_carry_completion_evidence(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "pending",
                "adjudication": _adjudication(final_label="b", final_value={"x": 2}),
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="must not contain adjudication evidence"):
        load_label_review_evidence(path)


def test_adjudicated_status_cannot_be_used_without_a_disagreement(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "a", {"x": 1}),
                "adjudication_status": "adjudicated",
                "adjudication": _adjudication(final_label="a", final_value={"x": 1}),
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="requires a double-labeled disagreement"):
        load_label_review_evidence(path)


def test_disagreement_cannot_claim_adjudication_is_not_required(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": "s-1",
                "primary": _label("annotator-a", "a", {"x": 1}),
                "secondary": _label("annotator-b", "b", {"x": 2}),
                "adjudication_status": "not_required",
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="cannot use not_required"):
        load_label_review_evidence(path)


def test_rejects_duplicate_json_object_keys_without_echoing_sample_id(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(
        '[{"sample_id":"private-sample-value","sample_id":"forged",'
        '"primary":{"annotator_id":"a","label":"x","value":{}},'
        '"adjudication_status":"pending"}]',
        encoding="utf-8",
    )

    with pytest.raises(D0LabelReviewError, match="duplicate JSON object key") as raised:
        load_label_review_evidence(path)

    assert "private-sample-value" not in str(raised.value)
    assert "forged" not in str(raised.value)


def test_invalid_status_does_not_echo_a_reused_sample_id(tmp_path: Path) -> None:
    private_id = "private-sample-value"
    path = _write(
        tmp_path / "review.json",
        [
            {
                "sample_id": private_id,
                "primary": _label("annotator-a", "a", {}),
                "adjudication_status": private_id,
            }
        ],
    )

    with pytest.raises(D0LabelReviewError, match="unrecognized adjudication_status") as raised:
        load_label_review_evidence(path)

    assert private_id not in str(raised.value)


def test_rejects_nan_and_infinity_in_strict_json(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(
        '[{"sample_id":"s-1",'
        '"primary":{"annotator_id":"annotator-a","label":"a","value":NaN},'
        '"adjudication_status":"pending"}]',
        encoding="utf-8",
    )

    with pytest.raises(D0LabelReviewError, match="non-standard JSON numeric constant"):
        load_label_review_evidence(path)


def test_rejects_non_object_jsonl_row(tmp_path: Path) -> None:
    path = tmp_path / "review.jsonl"
    first_row = {
        "sample_id": "s-1",
        "primary": _label("annotator-a", "a", {}),
        "adjudication_status": "pending",
    }
    path.write_text(json.dumps(first_row) + "\n[]\n", encoding="utf-8")

    with pytest.raises(D0LabelReviewError, match="JSONL row must be an object"):
        load_label_review_evidence(path)


def test_parse_rejects_oversized_text_before_json_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agreement, "_MAX_PARSE_CHARS", 10)

    with pytest.raises(D0LabelReviewError, match="exceeds maximum size"):
        parse_label_review_evidence("not json and too long")


def test_load_rejects_oversized_file_before_full_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "review.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(agreement, "_MAX_PARSE_BYTES", 1)

    with pytest.raises(D0LabelReviewError, match="file exceeds maximum size"):
        load_label_review_evidence(path)


def test_load_wraps_utf8_decode_errors(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_bytes(b"\xff")

    with pytest.raises(D0LabelReviewError, match="must be UTF-8"):
        load_label_review_evidence(path)


def test_load_wraps_os_errors(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"

    with pytest.raises(D0LabelReviewError, match="unable to read"):
        load_label_review_evidence(path)


def test_rejects_more_than_maximum_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agreement, "_MAX_ROWS", 1)
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", {}),
            "adjudication_status": "pending",
        },
        {
            "sample_id": "s-2",
            "primary": _label("annotator-a", "a", {}),
            "adjudication_status": "pending",
        },
    ]

    with pytest.raises(D0LabelReviewError, match="row count exceeds maximum"):
        parse_label_review_evidence(json.dumps(rows))


def test_rejects_excessive_json_depth_without_recursion_error() -> None:
    value: Any = "leaf"
    for _ in range(agreement._MAX_JSON_DEPTH + 1):
        value = [value]
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", value),
            "adjudication_status": "pending",
        }
    ]

    with pytest.raises(D0LabelReviewError, match="maximum JSON depth"):
        parse_label_review_evidence(json.dumps(rows))


def test_rejects_excessive_json_node_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agreement, "_MAX_JSON_NODES", 8)
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", {"items": [1, 2, 3]}),
            "adjudication_status": "pending",
        }
    ]

    with pytest.raises(D0LabelReviewError, match="maximum JSON node count"):
        parse_label_review_evidence(json.dumps(rows))


def test_rejects_excessive_single_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agreement, "_MAX_STRING_CHARS", 128)
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", "x" * 129),
            "adjudication_status": "pending",
        }
    ]

    with pytest.raises(D0LabelReviewError, match="string exceeding maximum length"):
        parse_label_review_evidence(json.dumps(rows))


def test_rejects_excessive_total_string_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agreement, "_MAX_TOTAL_STRING_CHARS", 20)
    rows = [
        {
            "sample_id": "s-1",
            "primary": _label("annotator-a", "a", {}),
            "adjudication_status": "pending",
        }
    ]

    with pytest.raises(D0LabelReviewError, match="maximum total string length"):
        parse_label_review_evidence(json.dumps(rows))


def test_label_evidence_rejects_cyclic_api_values_without_recursion_error() -> None:
    cyclic_value: list[Any] = []
    cyclic_value.append(cyclic_value)

    with pytest.raises(D0LabelReviewError, match="cyclic JSON container"):
        LabelEvidence(
            sample_id="s-1",
            primary_annotator_id="annotator-a",
            primary_label="a",
            primary_value=cyclic_value,
            secondary_annotator_id=None,
            secondary_label=None,
            secondary_value=None,
            adjudication_status=AdjudicationStatus.PENDING,
            adjudicator_id=None,
            final_label=None,
            final_value=None,
            reason=None,
            evidence_ref=None,
            evidence_sha256=None,
        )
