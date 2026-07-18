from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from careerops.evaluation.d0_scan import (
    REPORT_VERSION,
    RULE_SET_SHA256,
    RULE_SET_VERSION,
    SCAN_SCOPE,
    TOOL_NAME,
    TOOL_VERSION,
)


def _load_verify_m1_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "verify_m1.py"
    spec = importlib.util.spec_from_file_location("verify_m1", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contact_row(sample_id: str, group_id: str, split: str, *, synthetic: bool) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "group_id": group_id,
        "split": split,
        "synthetic": synthetic,
        "source": {
            "kind": "job_page",
            "url_or_thread_ref": f"https://example.test/jobs/{sample_id}",
            "captured_at": "2026-07-17T00:00:00Z",
            "evidence_text": "Public recruiting contact listed on the job page.",
            "content_sha256": hashlib.sha256(sample_id.encode("utf-8")).hexdigest(),
        },
        "candidate": {
            "address": f"recruiting-contact-{sample_id}",
            "display_name": "Recruiting Team",
            "domain_matches_company": True,
        },
        "gold": {
            "classification": "recruiting_contact",
            "publicly_listed": True,
            "guessed": False,
            "allowed_actions": ["display", "review", "draft"],
            "reason": "The contact is listed on an official job page.",
        },
    }


def _write_artifact(root: Path, rows: list[dict[str, Any]]) -> str:
    artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    return _sha256(artifact_path)


def _copy_schema(root: Path, name: str) -> None:
    source = Path(__file__).parents[2] / "datasets" / "schemas" / name
    target = root / "datasets" / "schemas" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _base_root(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = tmp_path
    for relative, text in {
        ".omx/plans/careerops-agent-mvp-plan.md": "plan",
        ".omx/plans/careerops-agent-risk-closure.md": "risks",
        "docs/source/contact-source.md": "lawful source",
        "docs/source/contact-consent.md": "consent",
        "docs/source/contact-retention.md": "retention",
        "datasets/labeling-guides/contact.md": "guide",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for schema_name in (
        "contact.schema.json",
        "d0_dataset_manifest.schema.json",
        "d0_label_review.schema.json",
        "d0_scan_report.schema.json",
    ):
        _copy_schema(root, schema_name)

    rows = [
        _contact_row("c-1", "g-1", "development", synthetic=False),
        _contact_row("c-2", "g-2", "holdout", synthetic=False),
        _contact_row("c-3", "g-3", "development", synthetic=True),
    ]
    rows[1]["gold"]["classification"] = "review_required"
    artifact_hash = _write_artifact(root, rows)

    review_path = root / "datasets/reviews/contact-v1.review.json"
    _write_json(
        review_path,
        [
            {
                "sample_id": row["sample_id"],
                "primary": {
                    "annotator_id": "impl",
                    "label": row["gold"]["classification"],
                    "value": row["gold"],
                },
                **(
                    {
                        "secondary": {
                            "annotator_id": "reviewer",
                            "label": row["gold"]["classification"],
                            "value": row["gold"],
                        }
                    }
                    if index < 2
                    else {}
                ),
                "adjudication_status": "not_required" if index < 2 else "pending",
            }
            for index, row in enumerate(rows)
        ],
    )

    scan_report = {
        "version": REPORT_VERSION,
        "scan_scope": SCAN_SCOPE,
        "dataset_id": "contact",
        "dataset_version": "contact-pilot-v1",
        "artifact_sha256": artifact_hash,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "rule_set_sha256": RULE_SET_SHA256,
        "scanned_at": "2026-07-17T00:00:00Z",
        "unsuppressed_findings_count": 0,
        "unsuppressed_findings": [],
        "reviewed_suppressions": [],
    }
    scan_path = root / "datasets/scans/contact-v1.scan.json"
    _write_json(scan_path, scan_report)

    gate = {
        "version": 1,
        "status": "contract_ready_pilot_pending",
        "authoritative_plan": ".omx/plans/careerops-agent-mvp-plan.md",
        "risk_decisions": ".omx/plans/careerops-agent-risk-closure.md",
        "contracts": {},
        "required_metric_ids": ["contact.precision.v1"],
        "dataset_contracts": ["contact"],
        "full_gate_requires": {
            "pilot_manifest_status": "completed",
            "independent_reviewer_identity": True,
            "safety_double_label_fraction": 1.0,
            "other_double_label_fraction_min": 0.2,
            "categorical_kappa_min": 0.8,
            "structured_exact_agreement_min": 0.95,
            "pii_scan_passed": True,
        },
    }
    pilot = {
        "version": 1,
        "status": "completed",
        "split_policy": {
            "development": 0.6,
            "validation": 0.2,
            "holdout": 0.2,
            "group_aware": True,
        },
        "roles": {
            "data_curator": "curator",
            "independent_reviewer": "reviewer",
            "adjudicator": "adjudicator",
        },
        "quality": {
            "pilot_fraction": 0.1,
            "safety_double_label_fraction": 1.0,
            "other_double_label_fraction_min": 0.2,
            "categorical_kappa_min": 0.8,
            "structured_exact_agreement_min": 0.95,
            "pii_scan_passed": True,
        },
        "datasets": [
            {
                "id": "contact",
                "minimum_final": 20,
                "pilot_required": 2,
                "pilot_actual": 2,
                "schema": "datasets/schemas/contact.schema.json",
                "guide": "datasets/labeling-guides/contact.md",
                "categorical_gold_field": "classification",
                "safety_critical": False,
                "manifest": "datasets/manifests/contact-v1.manifest.json",
            }
        ],
        "release_qualification_allowed": False,
    }
    manifest = {
        "version": 1,
        "dataset_id": "contact",
        "dataset_version": "contact-pilot-v1",
        "schema": "datasets/schemas/contact.schema.json",
        "guide": "datasets/labeling-guides/contact.md",
        "source": {
            "source_manifest": "docs/source/contact-source.md",
            "source_manifest_sha256": _sha256(root / "docs/source/contact-source.md"),
            "consent_ref": "docs/source/contact-consent.md",
            "consent_sha256": _sha256(root / "docs/source/contact-consent.md"),
            "retention_ref": "docs/source/contact-retention.md",
            "retention_sha256": _sha256(root / "docs/source/contact-retention.md"),
        },
        "artifact": {
            "path": "datasets/artifacts/contact-v1.jsonl",
            "sha256": artifact_hash,
            "row_count": 3,
            "real_count": 2,
            "synthetic_count": 1,
        },
        "splits": {"development": 2, "validation": 0, "holdout": 1},
        "groups": {
            "total_count": 3,
            "split_counts": {"development": 2, "validation": 0, "holdout": 1},
        },
        "labels": {
            "implementer": "impl",
            "independent_reviewer": "reviewer",
            "adjudicator": "adjudicator",
            "review": {
                "path": "datasets/reviews/contact-v1.review.json",
                "sha256": _sha256(review_path),
            },
            "double_labeled_count": 2,
            "categorical_kappa": 1.0,
            "structured_exact_agreement": 1.0,
            "adjudication_complete": True,
        },
        "pii_scan": {
            "path": "datasets/scans/contact-v1.scan.json",
            "sha256": _sha256(scan_path),
        },
    }
    _write_json(root / "docs/evaluation/m1-gate.json", gate)
    _write_json(root / "datasets/manifests/d0-pilot-plan.json", pilot)
    _write_json(root / "datasets/manifests/contact-v1.manifest.json", manifest)
    return root, pilot, manifest, scan_report


def _run_full(root: Path) -> tuple[list[str], dict[str, Any]]:
    verifier = _load_verify_m1_module()
    frozen_datasets = {
        "contact": {
            "minimum_final": 20,
            "schema": "datasets/schemas/contact.schema.json",
            "guide": "datasets/labeling-guides/contact.md",
            "categorical_gold_field": "classification",
            "safety_critical": False,
        }
    }
    verifier.__dict__["FROZEN_D0_DATASETS"] = frozen_datasets
    return verifier.verify_full_pilot(root)


def _assert_fails(
    tmp_path: Path,
    mutate: Callable[[Path, dict[str, Any], dict[str, Any], dict[str, Any]], None],
    expected: str | tuple[str, ...],
) -> None:
    root, pilot, manifest, scan_report = _base_root(tmp_path)
    mutate(root, pilot, manifest, scan_report)
    _write_json(root / "datasets/manifests/d0-pilot-plan.json", pilot)
    _write_json(root / "datasets/manifests/contact-v1.manifest.json", manifest)
    scan_path = root / "datasets/scans/contact-v1.scan.json"
    _write_json(scan_path, scan_report)
    if manifest.get("pii_scan", {}).get("path") == "datasets/scans/contact-v1.scan.json":
        manifest["pii_scan"]["sha256"] = _sha256(scan_path)
        _write_json(root / "datasets/manifests/contact-v1.manifest.json", manifest)

    errors, _details = _run_full(root)

    expected_parts = (expected,) if isinstance(expected, str) else expected
    assert any(all(part in error for part in expected_parts) for error in errors), errors


def test_valid_schema_fixture_passes_full_pilot_verification(tmp_path: Path) -> None:
    root, _pilot, _manifest, _scan_report = _base_root(tmp_path)

    errors, details = _run_full(root)

    assert errors == []
    assert details["pilot_rows_actual"] == 2


def test_rejects_artifact_row_missing_dataset_required_field(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        row = _contact_row("c-1", "g-1", "development", synthetic=False)
        del row["source"]
        rows = [
            row,
            _contact_row("c-2", "g-2", "holdout", synthetic=False),
            _contact_row("c-3", "g-3", "development", synthetic=True),
        ]
        artifact_hash = _write_artifact(root, rows)
        manifest["artifact"]["sha256"] = artifact_hash
        scan["artifact_sha256"] = artifact_hash

    _assert_fails(tmp_path, mutate, "pilot contact row 1: missing required property source")


def test_rejects_artifact_row_unexpected_property(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        row = _contact_row("c-1", "g-1", "development", synthetic=False)
        row["rogue_row_property"] = True
        rows = [
            row,
            _contact_row("c-2", "g-2", "holdout", synthetic=False),
            _contact_row("c-3", "g-3", "development", synthetic=True),
        ]
        artifact_hash = _write_artifact(root, rows)
        manifest["artifact"]["sha256"] = artifact_hash
        scan["artifact_sha256"] = artifact_hash

    _assert_fails(tmp_path, mutate, "pilot contact row 1: 1 unexpected property")


def test_rejects_manifest_unexpected_property(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["rogue_manifest_property"] = True

    _assert_fails(tmp_path, mutate, "1 unexpected property")


def test_rejects_source_provenance_hash_mismatch(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["source"]["consent_sha256"] = "0" * 64

    _assert_fails(tmp_path, mutate, "source consent_ref hash mismatch")


def test_rejects_scan_report_missing_version(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], _manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        del scan["version"]

    _assert_fails(tmp_path, mutate, "pilot contact scan report: missing required property version")


def test_rejects_scan_report_unexpected_property(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], _manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        scan["rogue_scan_property"] = True

    _assert_fails(
        tmp_path,
        mutate,
        "pilot contact scan report: 1 unexpected property",
    )


def test_rejects_boolean_where_manifest_version_requires_integer(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["version"] = True

    _assert_fails(tmp_path, mutate, "pilot contact manifest.version: expected JSON type integer")


def test_rejects_non_finite_json_number_in_artifact(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        rows = [
            _contact_row("c-1", "g-1", "development", synthetic=False),
            _contact_row("c-2", "g-2", "holdout", synthetic=False),
            _contact_row("c-3", "g-3", "development", synthetic=True),
        ]
        rows[0]["candidate"]["address"] = float("nan")
        artifact_hash = _write_artifact(root, rows)
        manifest["artifact"]["sha256"] = artifact_hash
        scan["artifact_sha256"] = artifact_hash

    _assert_fails(tmp_path, mutate, "non-finite JSON number is forbidden: NaN")


def test_rejects_declared_agreement_that_differs_from_review_rows(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["labels"]["categorical_kappa"] = 0.99

    _assert_fails(tmp_path, mutate, "declared categorical_kappa does not match derived value")


def test_rejects_label_review_sample_set_mismatch(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        review_path = root / "datasets/reviews/contact-v1.review.json"
        review_rows = json.loads(review_path.read_text(encoding="utf-8"))
        _write_json(review_path, review_rows[:-1])
        manifest["labels"]["review"]["sha256"] = _sha256(review_path)

    _assert_fails(tmp_path, mutate, "label review sample set mismatch")


def test_rejects_primary_review_value_not_bound_to_artifact_gold(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        review_path = root / "datasets/reviews/contact-v1.review.json"
        review_rows = json.loads(review_path.read_text(encoding="utf-8"))
        review_rows[0]["primary"]["value"]["guessed"] = True
        review_rows[0]["secondary"]["value"]["guessed"] = True
        _write_json(review_path, review_rows)
        manifest["labels"]["review"]["sha256"] = _sha256(review_path)

    _assert_fails(tmp_path, mutate, "final review value does not match artifact gold")


def test_rejects_invalid_d0_schema_json(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        (root / "datasets/schemas/d0_scan_report.schema.json").write_text("{", encoding="utf-8")

    _assert_fails(tmp_path, mutate, "invalid JSON:")
