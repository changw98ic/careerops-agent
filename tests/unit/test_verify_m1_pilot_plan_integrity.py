from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

Fixture = tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]
FrozenDatasets = dict[str, dict[str, Any]]

CONTACT_CONTRACT: dict[str, Any] = {
    "minimum_final": 20,
    "schema": "datasets/schemas/contact.schema.json",
    "guide": "datasets/labeling-guides/contact.md",
    "categorical_gold_field": "classification",
    "safety_critical": False,
}


def _load_schema_fixture_module() -> ModuleType:
    fixture_path = Path(__file__).with_name("test_verify_m1_schema_enforcement.py")
    spec = importlib.util.spec_from_file_location("verify_m1_plan_integrity_fixture", fixture_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_root(tmp_path: Path) -> Fixture:
    fixture_module = _load_schema_fixture_module()
    fixture_builder = cast(Callable[[Path], Fixture], fixture_module._base_root)
    return fixture_builder(tmp_path)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _persist(fixture: Fixture) -> None:
    root, pilot, manifest, scan_report = fixture
    scan_path = root / "datasets/scans/contact-v1.scan.json"
    _write_json(scan_path, scan_report)
    pii_scan = manifest.get("pii_scan")
    if isinstance(pii_scan, dict) and pii_scan.get("path") == "datasets/scans/contact-v1.scan.json":
        pii_scan["sha256"] = _sha256(scan_path)
    _write_json(root / "datasets/manifests/d0-pilot-plan.json", pilot)
    _write_json(root / "datasets/manifests/contact-v1.manifest.json", manifest)


def _set_gate_dataset_ids(root: Path, dataset_ids: list[str]) -> None:
    gate_path = root / "docs/evaluation/m1-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["dataset_contracts"] = dataset_ids
    _write_json(gate_path, gate)


def _run_full(
    root: Path,
    *,
    frozen_datasets: FrozenDatasets | None = None,
) -> tuple[list[str], dict[str, Any]]:
    fixture_module = _load_schema_fixture_module()
    verifier_loader = cast(Callable[[], ModuleType], fixture_module._load_verify_m1_module)
    verifier = verifier_loader()
    verifier.__dict__["FROZEN_D0_DATASETS"] = copy.deepcopy(
        frozen_datasets or {"contact": CONTACT_CONTRACT}
    )
    runner = cast(
        Callable[[Path], tuple[list[str], dict[str, Any]]],
        verifier.verify_full_pilot,
    )
    return runner(root)


def _assert_error(errors: list[str], expected: str) -> None:
    assert any(expected in error for error in errors), errors


def test_integrity_fixture_passes_with_frozen_contact_contract(tmp_path: Path) -> None:
    root, _pilot, _manifest, _scan_report = _base_root(tmp_path)

    errors, details = _run_full(root)

    assert errors == []
    assert details["pilot_rows_actual"] == 2
    assert details["pilot_rows_required"] == 2


def test_full_gate_requirements_must_be_an_object(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, _pilot, _manifest, _scan_report = fixture
    gate_path = root / "docs/evaluation/m1-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["full_gate_requires"] = ["not-an-object"]
    _write_json(gate_path, gate)

    errors, _details = _run_full(root)

    _assert_error(errors, "full_gate_requires must be an object")


def test_boolean_quality_thresholds_are_not_numbers(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    pilot["quality"]["categorical_kappa_min"] = True
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, "pilot quality value missing: categorical_kappa_min")


def test_full_verifier_rejects_duplicate_dataset_ids_even_when_count_matches(
    tmp_path: Path,
) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    shadow_contract = copy.deepcopy(CONTACT_CONTRACT)
    frozen = {"contact": copy.deepcopy(CONTACT_CONTRACT), "contact_shadow": shadow_contract}
    _set_gate_dataset_ids(root, list(frozen))
    pilot["datasets"] = [pilot["datasets"][0], copy.deepcopy(pilot["datasets"][0])]
    _persist(fixture)

    errors, _details = _run_full(root, frozen_datasets=frozen)

    _assert_error(errors, "pilot dataset IDs do not match the frozen D0 mapping")


def test_full_verifier_rejects_missing_dataset_id(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    del pilot["datasets"][0]["id"]
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, "pilot dataset IDs do not match the frozen D0 mapping")


def test_full_verifier_rejects_replacement_dataset_id(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    pilot["datasets"][0]["id"] = "contact_replacement"
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, "pilot dataset IDs do not match the frozen D0 mapping")


def test_pilot_required_must_be_ceil_of_frozen_minimum_and_fraction(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    frozen = {"contact": {**CONTACT_CONTRACT, "minimum_final": 21}}
    pilot["datasets"][0]["minimum_final"] = 21
    pilot["datasets"][0]["pilot_required"] = 2
    _persist(fixture)

    errors, _details = _run_full(root, frozen_datasets=frozen)

    _assert_error(errors, "pilot contact: pilot_required does not match frozen pilot fraction")


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("schema", "datasets/schemas/contact-weakened.schema.json"),
        ("guide", "datasets/labeling-guides/contact-weakened.md"),
        ("categorical_gold_field", "publicly_listed"),
    ],
)
def test_full_verifier_rejects_weakened_dataset_contract_fields(
    tmp_path: Path,
    field: str,
    tampered_value: str,
) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, manifest, _scan_report = fixture
    pilot["datasets"][0][field] = tampered_value
    if field == "schema":
        _write_json(
            root / tampered_value,
            {
                "type": "object",
                "additionalProperties": True,
                "required": ["sample_id", "group_id", "split", "synthetic"],
            },
        )
        manifest["schema"] = tampered_value
    elif field == "guide":
        guide_path = root / tampered_value
        guide_path.parent.mkdir(parents=True, exist_ok=True)
        guide_path.write_text("weakened guide", encoding="utf-8")
        manifest["guide"] = tampered_value
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, f"pilot contact: {field} does not match frozen contract")


def test_full_verifier_rejects_safety_critical_downgrade(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    frozen = {"contact": {**CONTACT_CONTRACT, "safety_critical": True}}
    pilot["datasets"][0]["safety_critical"] = False
    _persist(fixture)

    errors, _details = _run_full(root, frozen_datasets=frozen)

    _assert_error(errors, "pilot contact: safety_critical does not match frozen contract")


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("independent_reviewer", "dataset reviewer does not match pilot role"),
        ("adjudicator", "dataset adjudicator does not match pilot role"),
    ],
)
def test_dataset_label_roles_must_match_top_level_pilot_roles(
    tmp_path: Path,
    role: str,
    expected: str,
) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    pilot["roles"][role] = f"replacement-{role}"
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, expected)


def test_required_total_is_derived_even_when_declared_requirement_is_forged(
    tmp_path: Path,
) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, _manifest, _scan_report = fixture
    pilot["datasets"][0]["pilot_required"] = 0
    _persist(fixture)

    errors, details = _run_full(root)

    _assert_error(errors, "pilot contact: pilot_required does not match frozen pilot fraction")
    assert details["pilot_rows_required"] == 2


def test_editable_quality_boolean_cannot_replace_missing_scan_evidence(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, manifest, _scan_report = fixture
    assert pilot["quality"]["pii_scan_passed"] is True
    del manifest["pii_scan"]
    _persist(fixture)

    errors, _details = _run_full(root)

    assert any("pii_scan" in error or "scan" in error for error in errors), errors


def test_editable_quality_values_cannot_replace_derived_label_evidence(tmp_path: Path) -> None:
    fixture = _base_root(tmp_path)
    root, pilot, manifest, _scan_report = fixture
    review_path = root / "datasets/reviews/contact-v1.review.json"
    review_rows = json.loads(review_path.read_text(encoding="utf-8"))
    for row in review_rows:
        row.pop("secondary", None)
        row["adjudication_status"] = "pending"
    _write_json(review_path, review_rows)
    manifest["labels"]["review"]["sha256"] = _sha256(review_path)
    manifest["labels"]["double_labeled_count"] = 0
    assert pilot["quality"]["categorical_kappa_min"] == 0.8
    assert pilot["quality"]["structured_exact_agreement_min"] == 0.95
    _persist(fixture)

    errors, _details = _run_full(root)

    _assert_error(errors, "derived double label count 0 is below required 1")
    _assert_error(errors, "categorical kappa is undefined")
    _assert_error(errors, "structured exact agreement is undefined")
