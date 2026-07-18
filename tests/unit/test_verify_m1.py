from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

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

    repository_root = Path(__file__).parents[2]
    for schema_name in (
        "d0_dataset_manifest.schema.json",
        "d0_label_review.schema.json",
        "d0_scan_report.schema.json",
    ):
        source = repository_root / "datasets/schemas" / schema_name
        target = root / "datasets/schemas" / schema_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(
        root / "datasets/schemas/contact.schema.json",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["sample_id", "group_id", "split", "synthetic", "gold"],
            "properties": {
                "sample_id": {"type": "string", "minLength": 1},
                "group_id": {"type": "string", "minLength": 1},
                "split": {"enum": ["development", "validation", "holdout"]},
                "synthetic": {"type": "boolean"},
                "gold": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "accepted"],
                    "properties": {
                        "category": {"enum": ["recruiting_contact", "review_required"]},
                        "accepted": {"type": "boolean"},
                    },
                },
            },
        },
    )

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
                "categorical_gold_field": "category",
                "safety_critical": False,
                "manifest": "datasets/manifests/contact-v1.manifest.json",
            }
        ],
        "release_qualification_allowed": False,
    }
    artifact_rows = [
        {
            "sample_id": "c-1",
            "group_id": "g-1",
            "split": "development",
            "synthetic": False,
            "gold": {"category": "recruiting_contact", "accepted": True},
        },
        {
            "sample_id": "c-2",
            "group_id": "g-2",
            "split": "holdout",
            "synthetic": False,
            "gold": {"category": "review_required", "accepted": False},
        },
        {
            "sample_id": "c-3",
            "group_id": "g-3",
            "split": "development",
            "synthetic": True,
            "gold": {"category": "review_required", "accepted": False},
        },
    ]
    artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in artifact_rows) + "\n",
        encoding="utf-8",
    )
    artifact_hash = _sha256(artifact_path)
    review_path = root / "datasets/reviews/contact-v1.review.json"
    _write_json(
        review_path,
        [
            {
                "sample_id": "c-1",
                "primary": {
                    "annotator_id": "impl",
                    "label": "recruiting_contact",
                    "value": {"category": "recruiting_contact", "accepted": True},
                },
                "secondary": {
                    "annotator_id": "reviewer",
                    "label": "recruiting_contact",
                    "value": {"category": "recruiting_contact", "accepted": True},
                },
                "adjudication_status": "not_required",
            },
            {
                "sample_id": "c-2",
                "primary": {
                    "annotator_id": "impl",
                    "label": "review_required",
                    "value": {"category": "review_required", "accepted": False},
                },
                "secondary": {
                    "annotator_id": "reviewer",
                    "label": "review_required",
                    "value": {"category": "review_required", "accepted": False},
                },
                "adjudication_status": "not_required",
            },
            {
                "sample_id": "c-3",
                "primary": {
                    "annotator_id": "impl",
                    "label": "review_required",
                    "value": {"category": "review_required", "accepted": False},
                },
                "adjudication_status": "pending",
            },
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
            "categorical_gold_field": "category",
            "safety_critical": False,
        }
    }
    verifier.__dict__["FROZEN_D0_DATASETS"] = frozen_datasets
    return verifier.verify_full_pilot(root)


def _assert_fails(
    tmp_path: Path,
    mutate: Callable[[Path, dict[str, Any], dict[str, Any], dict[str, Any]], None],
    expected: str,
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
    assert any(expected in error for error in errors), errors


def test_valid_d0_fixture_passes_full_pilot_layer(tmp_path: Path) -> None:
    root, _pilot, _manifest, _scan_report = _base_root(tmp_path)

    errors, details = _run_full(root)

    assert errors == []
    assert details["pilot_rows_actual"] == 2
    assert details["pilot_rows_required"] == 2
    assert details["release_qualification_authorized"] is False


def test_manifest_missing_fails(tmp_path: Path) -> None:
    def mutate(
        _root: Path, pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        pilot["datasets"][0]["manifest"] = None

    _assert_fails(tmp_path, mutate, "pilot manifest missing")


def test_empty_manifest_fails(tmp_path: Path) -> None:
    root, _pilot, _manifest, _scan_report = _base_root(tmp_path)
    (root / "datasets/manifests/contact-v1.manifest.json").write_text("", encoding="utf-8")

    errors, _details = _run_full(root)

    assert any("required file is empty" in error for error in errors), errors


def test_path_traversal_manifest_fails(tmp_path: Path) -> None:
    def mutate(
        _root: Path, pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        pilot["datasets"][0]["manifest"] = "../outside.json"

    _assert_fails(tmp_path, mutate, "path escapes repository")


def test_forged_release_boolean_fails_when_evidence_incomplete(tmp_path: Path) -> None:
    def mutate(
        _root: Path, pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        pilot["release_qualification_allowed"] = True
        pilot["roles"]["independent_reviewer"] = None

    _assert_fails(tmp_path, mutate, "cannot authorize Release Qualification")


@pytest.mark.parametrize("value", [True, None, 1, "false"])
def test_m1_never_accepts_release_authority_values(tmp_path: Path, value: Any) -> None:
    def mutate(
        _root: Path, pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        pilot["release_qualification_allowed"] = value

    _assert_fails(tmp_path, mutate, "cannot authorize Release Qualification")


def test_m1_requires_explicit_false_release_authority(tmp_path: Path) -> None:
    def mutate(
        _root: Path, pilot: dict[str, Any], _manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        del pilot["release_qualification_allowed"]

    _assert_fails(tmp_path, mutate, "cannot authorize Release Qualification")


def test_manifest_count_mismatch_fails(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["artifact"]["real_count"] = 99

    _assert_fails(tmp_path, mutate, "artifact real_count=99 does not match derived 2")


def test_artifact_hash_mismatch_fails(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["artifact"]["sha256"] = "0" * 64

    _assert_fails(tmp_path, mutate, "artifact hash mismatch")


def test_scan_missing_fails(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        _ = root
        manifest["pii_scan"]["path"] = "datasets/scans/missing.scan.json"
        manifest["pii_scan"]["sha256"] = "0" * 64

    _assert_fails(tmp_path, mutate, "missing required file")


def test_scan_findings_fail(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], _manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        scan["unsuppressed_findings_count"] = 1
        scan["unsuppressed_findings"] = [
            {
                "finding_id": "a" * 64,
                "rule_id": "d0.pii.email_like.v2",
                "target_ref": "row:0:locator:" + "b" * 64,
            }
        ]

    _assert_fails(tmp_path, mutate, "scan findings do not match reproduced scan")


def test_group_cross_split_fails(tmp_path: Path) -> None:
    def mutate(
        root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
        rows = [
            {"sample_id": "c-1", "group_id": "g-1", "split": "development", "synthetic": False},
            {"sample_id": "c-2", "group_id": "g-1", "split": "holdout", "synthetic": False},
        ]
        artifact_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
        artifact_hash = _sha256(artifact_path)
        manifest["artifact"] = {
            "path": "datasets/artifacts/contact-v1.jsonl",
            "sha256": artifact_hash,
            "row_count": 2,
            "real_count": 2,
            "synthetic_count": 0,
        }
        manifest["splits"] = {"development": 1, "validation": 0, "holdout": 1}
        manifest["groups"] = {
            "total_count": 1,
            "split_counts": {"development": 1, "validation": 0, "holdout": 1},
        }
        scan["artifact_sha256"] = artifact_hash

    _assert_fails(tmp_path, mutate, "appears in both")


def test_implementer_cannot_be_reviewer(tmp_path: Path) -> None:
    def mutate(
        _root: Path, _pilot: dict[str, Any], manifest: dict[str, Any], _scan: dict[str, Any]
    ) -> None:
        manifest["labels"]["independent_reviewer"] = copy.deepcopy(
            manifest["labels"]["implementer"]
        )

    _assert_fails(tmp_path, mutate, "implementer cannot be independent_reviewer")


def test_synthetic_only_cannot_satisfy_real_required(tmp_path: Path) -> None:
    def mutate(
        root: Path, pilot: dict[str, Any], manifest: dict[str, Any], scan: dict[str, Any]
    ) -> None:
        artifact_path = root / "datasets/artifacts/contact-v1.jsonl"
        rows = [
            {"sample_id": "c-1", "group_id": "g-1", "split": "development", "synthetic": True},
            {"sample_id": "c-2", "group_id": "g-2", "split": "holdout", "synthetic": True},
        ]
        artifact_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )
        artifact_hash = _sha256(artifact_path)
        pilot["datasets"][0]["pilot_actual"] = 0
        manifest["artifact"] = {
            "path": "datasets/artifacts/contact-v1.jsonl",
            "sha256": artifact_hash,
            "row_count": 2,
            "real_count": 0,
            "synthetic_count": 2,
        }
        manifest["splits"] = {"development": 1, "validation": 0, "holdout": 1}
        manifest["groups"] = {
            "total_count": 2,
            "split_counts": {"development": 1, "validation": 0, "holdout": 1},
        }
        scan["artifact_sha256"] = artifact_hash

    _assert_fails(tmp_path, mutate, "synthetic-only evidence cannot satisfy real pilot")
