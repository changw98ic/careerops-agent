from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import careerops.cli.d0 as d0_cli
from careerops.cli.d0 import INCOMPLETE_MARKER, MAX_ARTIFACT_ROWS, MAX_JSON_BYTES, main

ROOT = Path(__file__).parents[2]
SENSITIVE_SENTINEL = "CONFIDENTIAL_INPUT_MUST_NOT_BE_ECHOED_7F3A91"


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = ["--root", str(repo), *args]
    stdout = StringIO()
    stderr = StringIO()
    original_argv = sys.argv
    sys.argv = ["careerops-d0", *argv]
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                returncode = main(argv)
            except SystemExit as exc:
                returncode = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = original_argv
    return subprocess.CompletedProcess(
        args=["careerops-d0", *argv],
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def _json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stderr == "" or "Traceback" not in result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def _json_stderr(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    parsed = json.loads(result.stderr)
    assert isinstance(parsed, dict)
    return parsed


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_schema(repo: Path, name: str) -> None:
    source = ROOT / "datasets" / "schemas" / name
    target = repo / "datasets" / "schemas" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _copy_verifier(repo: Path) -> None:
    target = repo / "scripts" / "verify_m1.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((ROOT / "scripts" / "verify_m1.py").read_text(encoding="utf-8"), "utf-8")


def _dataset_row(sample_id: str, *, split: str, synthetic: bool) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "group_id": sample_id,
        "split": split,
        "synthetic": synthetic,
        "source": {
            "kind": "job_page",
            "url_or_thread_ref": f"https://example.invalid/jobs/{sample_id}",
            "captured_at": "2026-07-18T00:00:00Z",
            "evidence_text": "public recruiting evidence",
            "content_sha256": hashlib.sha256(sample_id.encode()).hexdigest(),
        },
        "candidate": {
            "address": f"contact-{sample_id}",
            "display_name": "Recruiting",
            "domain_matches_company": True,
        },
        "gold": {
            "classification": "recruiting_contact",
            "publicly_listed": True,
            "guessed": False,
            "allowed_actions": ["display"],
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", "utf-8")
    return path


def _repo_with_claims_and_two_real_files(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for schema_name in (
        "contact.schema.json",
        "d0_dataset_manifest.schema.json",
        "d0_label_review.schema.json",
        "d0_scan_report.schema.json",
    ):
        _copy_schema(repo, schema_name)

    guide = repo / "datasets" / "labeling-guides" / "contact.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("contact guide\n", encoding="utf-8")

    source_manifest = repo / "docs" / "source" / "contact-source.md"
    consent = repo / "docs" / "source" / "contact-consent.md"
    retention = repo / "docs" / "source" / "contact-retention.md"
    for path, text in (
        (source_manifest, "source"),
        (consent, "consent"),
        (retention, "retention"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    rows = [
        _dataset_row("contact-1", split="development", synthetic=False),
        _dataset_row("contact-2", split="development", synthetic=True),
    ]
    artifact = _write_jsonl(repo / "datasets" / "artifacts" / "contact.jsonl", rows)
    review = _write_json(
        repo / "datasets" / "reviews" / "contact.review.json",
        [
            {
                "sample_id": "contact-1",
                "primary": {
                    "annotator_id": "reviewer-primary",
                    "label": "recruiting_contact",
                    "value": rows[0]["gold"],
                },
                "adjudication_status": "pending",
            }
        ],
    )
    scan = _write_json(
        repo / "datasets" / "scans" / "contact.scan.json",
        {
            "version": 2,
            "scan_scope": "technical_patterns_only",
            "dataset_id": "contact",
            "dataset_version": "contact-v1",
            "artifact_sha256": _sha256(artifact),
            "tool": "careerops-d0-scan",
            "tool_version": "2.1.0",
            "rule_set_version": "d0-technical-scan-rules-v3",
            "rule_set_sha256": "a" * 64,
            "scanned_at": "2026-07-18T00:00:00Z",
            "unsuppressed_findings_count": 0,
            "unsuppressed_findings": [],
            "reviewed_suppressions": [],
        },
    )
    manifest = _write_json(
        repo / "datasets" / "manifests" / "contact.manifest.json",
        {
            "version": 1,
            "dataset_id": "contact",
            "dataset_version": "contact-v1",
            "schema": "datasets/schemas/contact.schema.json",
            "guide": "datasets/labeling-guides/contact.md",
            "source": {
                "source_manifest": "docs/source/contact-source.md",
                "source_manifest_sha256": _sha256(source_manifest),
                "consent_ref": "docs/source/contact-consent.md",
                "consent_sha256": _sha256(consent),
                "retention_ref": "docs/source/contact-retention.md",
                "retention_sha256": _sha256(retention),
            },
            "artifact": {
                "path": "datasets/artifacts/contact.jsonl",
                "sha256": _sha256(artifact),
                "row_count": 999,
                "real_count": 999,
                "synthetic_count": 0,
            },
            "splits": {"development": 999, "validation": 0, "holdout": 0},
            "groups": {
                "total_count": 999,
                "split_counts": {"development": 999, "validation": 0, "holdout": 0},
            },
            "labels": {
                "implementer": "reviewer-primary",
                "independent_reviewer": "reviewer-secondary",
                "adjudicator": "reviewer-adjudicator",
                "review": {
                    "path": "datasets/reviews/contact.review.json",
                    "sha256": _sha256(review),
                },
                "double_labeled_count": 999,
                "categorical_kappa": 1.0,
                "structured_exact_agreement": 1.0,
                "adjudication_complete": True,
            },
            "pii_scan": {"path": "datasets/scans/contact.scan.json", "sha256": _sha256(scan)},
        },
    )
    _write_json(
        repo / "datasets" / "manifests" / "d0-pilot-plan.json",
        {
            "version": 1,
            "status": "completed",
            "roles": {
                "data_curator": "curator",
                "independent_reviewer": "reviewer-secondary",
                "adjudicator": "reviewer-adjudicator",
            },
            "quality": {
                "pii_scan_passed": True,
                "categorical_kappa_min": 0.8,
                "structured_exact_agreement_min": 0.95,
            },
            "datasets": [
                {
                    "id": "contact",
                    "pilot_required": 999,
                    "pilot_actual": 999,
                    "manifest": str(manifest.relative_to(repo)),
                }
            ],
            "release_qualification_allowed": True,
        },
    )
    return repo


def _repo_with_one_planned_dataset(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write_json(
        repo / "datasets" / "manifests" / "d0-pilot-plan.json",
        {
            "version": 1,
            "status": "planned",
            "datasets": [
                {
                    "id": "contact",
                    "schema": "datasets/schemas/contact.schema.json",
                    "guide": "datasets/labeling-guides/contact.md",
                    "pilot_required": 1,
                    "pilot_actual": 0,
                    "manifest": None,
                }
            ],
            "release_qualification_allowed": False,
        },
    )
    return repo


def test_invalid_arguments_are_reported_as_json_without_usage_or_traceback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_cli(repo, "scaffold", "--json")

    assert result.returncode == 2
    assert result.stdout == ""
    payload = _json_stderr(result)
    assert payload == {
        "errors": ["invalid arguments"],
        "passed": False,
        "scope": "d0_intake",
    }
    assert "usage:" not in result.stderr
    assert "Traceback" not in result.stderr


def test_status_fails_closed_when_plan_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = _run_cli(repo, "status", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert "pilot plan is missing" in payload["errors"]
    assert payload["counts"]["dataset_count"] == 0


def test_status_rejects_corrupt_plan_without_traceback_or_input_echo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    plan = repo / "datasets/manifests/d0-pilot-plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f'{{"datasets":[],"sensitive_input":"{SENSITIVE_SENTINEL}"',
        encoding="utf-8",
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert any("pilot plan is invalid JSON" in error for error in payload["errors"])
    rendered = result.stdout + result.stderr
    assert SENSITIVE_SENTINEL not in rendered
    assert "Traceback" not in rendered


def test_status_derives_counts_from_artifacts_instead_of_declared_booleans(
    tmp_path: Path,
) -> None:
    repo = _repo_with_claims_and_two_real_files(tmp_path)

    result = _run_cli(repo, "status", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert payload["counts"]["dataset_count"] == 1
    assert payload["counts"]["pilot_rows_declared_actual"] == 999
    assert payload["counts"]["pilot_rows_derived_actual"] == 1
    assert payload["counts"]["datasets_satisfied_by_derived_rows"] == 0
    assert payload["counts"]["scan_reports_present"] == 1
    assert payload["datasets"][0]["id"] == "contact"
    assert payload["datasets"][0]["pilot_actual_declared"] == 999
    assert payload["datasets"][0]["pilot_actual_derived"] == 1


def test_status_reports_missing_evidence_when_plan_claims_release_ready(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _write_json(
        repo / "datasets" / "manifests" / "d0-pilot-plan.json",
        {
            "version": 1,
            "status": "completed",
            "datasets": [{"id": "contact", "pilot_required": 1, "pilot_actual": 1}],
            "release_qualification_allowed": True,
        },
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert payload["counts"]["pilot_rows_derived_actual"] == 0
    assert payload["counts"]["manifest_references"] == 0
    assert payload["datasets"][0]["pilot_actual_declared"] == 1
    assert payload["datasets"][0]["pilot_actual_derived"] == 0


def test_status_rejects_oversize_plan_without_parsing_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    plan = repo / "datasets/manifests/d0-pilot-plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_bytes(b'{"padding":"' + (b"x" * MAX_JSON_BYTES) + b'"}')

    result = _run_cli(repo, "status", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert "pilot plan exceeds size limit" in payload["errors"]
    assert payload["counts"]["dataset_count"] == 0


def test_status_rejects_artifact_over_row_limit_without_counting_rows(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    artifact = repo / "datasets/artifacts/contact.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n" * (MAX_ARTIFACT_ROWS + 1), encoding="utf-8")
    _write_json(
        repo / "datasets/manifests/contact.manifest.json",
        {"artifact": {"path": "datasets/artifacts/contact.jsonl"}},
    )
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [
                {
                    "id": "contact",
                    "pilot_required": 1,
                    "pilot_actual": MAX_ARTIFACT_ROWS + 1,
                    "manifest": "datasets/manifests/contact.manifest.json",
                }
            ],
            "release_qualification_allowed": True,
        },
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert payload["counts"]["pilot_rows_derived_actual"] == 0
    assert payload["datasets"][0]["pilot_actual_derived"] == 0
    assert payload["datasets"][0]["error"] == "contact: artifact exceeds row limit"


def test_scaffold_writes_incomplete_skeleton_without_fake_acceptance_data(
    tmp_path: Path,
) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)

    result = _run_cli(
        repo,
        "scaffold",
        "--dataset",
        "contact",
        "--json",
    )

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["created"] == sorted(payload["created"])
    manifest = repo / "datasets/d0-intake/contact/manifest.incomplete.json"
    artifact = repo / "datasets/d0-intake/contact/artifact.incomplete.json"
    review = repo / "datasets/d0-intake/contact/label-review.incomplete.json"
    scan = repo / "datasets/d0-intake/contact/scan-report.incomplete.json"
    assert manifest.is_file()
    assert artifact.is_file()
    assert review.is_file()
    assert scan.is_file()
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in (manifest, review, scan))
    assert "INCOMPLETE SKELETON" in rendered
    assert "release_qualification_allowed" not in rendered
    assert "pilot_actual" not in rendered
    assert "example.com" not in rendered
    assert "pass" not in rendered.lower()
    assert "alice" not in rendered.lower()
    assert "manifest.incomplete.json" in "\n".join(payload["created"])


def test_scaffold_refuses_to_overwrite_existing_files(tmp_path: Path) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    existing = repo / "datasets/d0-intake/contact/manifest.incomplete.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("do not replace\n", encoding="utf-8")

    result = _run_cli(
        repo,
        "scaffold",
        "--dataset",
        "contact",
        "--json",
    )

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert str(existing.relative_to(repo)) in payload["skipped_existing"]
    assert existing.read_text(encoding="utf-8") == "do not replace\n"


def test_scaffold_rejects_malicious_dataset_id_without_writing_outside_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    malicious_id = "../../outside"
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [{"id": malicious_id, "pilot_required": 1, "pilot_actual": 0}],
        },
    )

    result = _run_cli(repo, "scaffold", "--dataset", malicious_id, "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["created"] == []
    assert payload["errors"]
    assert not (tmp_path / "outside").exists()


def test_status_rejects_manifest_parent_directory_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    _write_json(outside / "manifest.json", {"dataset_id": "contact"})
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [
                {
                    "id": "contact",
                    "pilot_required": 1,
                    "pilot_actual": 1,
                    "manifest": "../outside/manifest.json",
                }
            ],
            "release_qualification_allowed": True,
        },
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert payload["datasets"][0]["error"] == "manifest path escapes repository"
    assert payload["datasets"][0]["pilot_actual_derived"] == 0


def test_status_never_echoes_hostile_plan_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    hostile_entry = f"{SENSITIVE_SENTINEL}_ENTRY"
    hostile_id = f"{SENSITIVE_SENTINEL}_DATASET"
    unsafe_manifest = f"../{SENSITIVE_SENTINEL}_MANIFEST.json"
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [
                hostile_entry,
                {
                    "id": hostile_id,
                    "pilot_required": 1,
                    "pilot_actual": 1,
                    "manifest": unsafe_manifest,
                },
            ],
            "release_qualification_allowed": True,
        },
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    rendered = result.stdout + result.stderr
    assert hostile_entry not in rendered
    assert hostile_id not in rendered
    assert unsafe_manifest not in rendered
    assert "Traceback" not in rendered


def test_status_rejects_manifest_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    _write_json(outside / "manifest.json", {"dataset_id": "contact"})
    link = repo / "datasets/manifests/contact.manifest.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside / "manifest.json")
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [
                {
                    "id": "contact",
                    "pilot_required": 1,
                    "pilot_actual": 1,
                    "manifest": "datasets/manifests/contact.manifest.json",
                }
            ],
            "release_qualification_allowed": True,
        },
    )

    result = _run_cli(repo, "status", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["release_qualification_authorized"] is False
    assert payload["datasets"][0]["error"] == "manifest path escapes repository"
    assert payload["datasets"][0]["pilot_actual_derived"] == 0


def test_scaffold_with_symlinked_root_writes_under_resolved_repo(tmp_path: Path) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    root_link = tmp_path / "repo-link"
    root_link.symlink_to(repo, target_is_directory=True)

    result = _run_cli(root_link, "scaffold", "--dataset", "contact", "--json")

    assert result.returncode == 0
    payload = _json_stdout(result)
    assert payload["errors"] == []
    assert (repo / "datasets/d0-intake/contact/manifest.incomplete.json").is_file()


def test_scaffold_rejects_symlinked_dataset_directory_that_escapes_repo(
    tmp_path: Path,
) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    intake = repo / "datasets/d0-intake"
    intake.mkdir(parents=True)
    (intake / "contact").symlink_to(outside, target_is_directory=True)

    result = _run_cli(repo, "scaffold", "--dataset", "contact", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["created"] == []
    assert payload["errors"]
    assert all("unsafe scaffold path" in error for error in payload["errors"])
    assert list(outside.iterdir()) == []


def test_scaffold_parent_directory_swap_never_writes_through_outside_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canonical = repo / "datasets/d0-intake/contact"
    renamed = repo / "datasets/d0-intake/contact-opened"
    original_open = d0_cli._open_scaffold_directory
    interleaved = False

    def open_then_swap(root: Path, dataset_id: str) -> int | None:
        nonlocal interleaved
        directory_fd = original_open(root, dataset_id)
        assert directory_fd is not None
        assert interleaved is False
        canonical.rename(renamed)
        canonical.symlink_to(outside, target_is_directory=True)
        interleaved = True
        return directory_fd

    monkeypatch.setattr(d0_cli, "_open_scaffold_directory", open_then_swap)

    result = _run_cli(repo, "scaffold", "--dataset", "contact", "--json")

    payload = _json_stdout(result)
    assert interleaved is True
    assert payload["scope"] == "d0_intake"
    assert canonical.is_symlink()
    assert canonical.resolve() == outside.resolve()
    assert list(outside.iterdir()) == []
    assert "Traceback" not in result.stderr


def test_scaffold_force_refuses_to_replace_non_skeleton_file(tmp_path: Path) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    existing = repo / "datasets/d0-intake/contact/manifest.incomplete.json"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('{"dataset_id":"contact"}\n', encoding="utf-8")

    result = _run_cli(repo, "scaffold", "--dataset", "contact", "--force", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["errors"] == [
        "refusing to overwrite non-skeleton file: "
        "datasets/d0-intake/contact/manifest.incomplete.json"
    ]
    assert existing.read_text(encoding="utf-8") == '{"dataset_id":"contact"}\n'


def test_scaffold_force_rejects_exact_marker_with_forged_skeleton_content(
    tmp_path: Path,
) -> None:
    repo = _repo_with_one_planned_dataset(tmp_path)
    initial = _run_cli(repo, "scaffold", "--dataset", "contact", "--json")
    assert initial.returncode == 0

    manifest = repo / "datasets/d0-intake/contact/manifest.incomplete.json"
    review = repo / "datasets/d0-intake/contact/label-review.incomplete.json"
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    assert manifest_payload["marker"] == INCOMPLETE_MARKER
    assert review_payload["marker"] == INCOMPLETE_MARKER
    manifest_payload["release_qualification_allowed"] = True
    review_payload["rows"] = [{"accepted": True}]
    _write_json(manifest, manifest_payload)
    _write_json(review, review_payload)
    forged_manifest = manifest.read_text(encoding="utf-8")
    forged_review = review.read_text(encoding="utf-8")

    result = _run_cli(repo, "scaffold", "--dataset", "contact", "--force", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert sorted(payload["errors"]) == [
        "refusing to overwrite non-skeleton file: "
        "datasets/d0-intake/contact/label-review.incomplete.json",
        "refusing to overwrite non-skeleton file: "
        "datasets/d0-intake/contact/manifest.incomplete.json",
    ]
    assert manifest.read_text(encoding="utf-8") == forged_manifest
    assert review.read_text(encoding="utf-8") == forged_review


def test_validate_returns_deterministic_json_and_nonzero_for_invalid_manifest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _copy_verifier(repo)
    _write_json(
        repo / "docs/evaluation/m1-gate.json",
        {"contracts": {}, "required_metric_ids": [], "release_qualification_allowed": True},
    )

    first = _run_cli(repo, "validate", "--contracts-only", "--json")
    second = _run_cli(repo, "validate", "--contracts-only", "--json")

    assert first.returncode != 0
    assert second.returncode != 0
    assert first.stdout == second.stdout
    payload = _json_stdout(first)
    assert payload["passed"] is False
    assert payload["release_qualification_authorized"] is False
    assert payload["errors"]
    rendered = first.stdout + first.stderr
    assert SENSITIVE_SENTINEL not in rendered
    assert "Traceback" not in rendered


def test_validate_never_echoes_hostile_plan_entries_or_manifest_names(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    hostile_entry = f"{SENSITIVE_SENTINEL}_ENTRY"
    unsafe_manifest = f"../{SENSITIVE_SENTINEL}_MANIFEST.json"
    _write_json(
        repo / "docs/evaluation/m1-gate.json",
        {"contracts": {}, "required_metric_ids": []},
    )
    _write_json(
        repo / "datasets/manifests/d0-pilot-plan.json",
        {
            "version": 1,
            "datasets": [
                {
                    "id": "discovery_parser",
                    "minimum_final": 350,
                    "schema": "datasets/schemas/discovery_parser.schema.json",
                    "guide": "datasets/labeling-guides/discovery_parser.md",
                    "categorical_gold_field": "adapter",
                    "safety_critical": False,
                    "pilot_required": 35,
                    "pilot_actual": 0,
                    "manifest": unsafe_manifest,
                },
                *([hostile_entry] * 8),
            ],
        },
    )

    result = _run_cli(repo, "validate", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["passed"] is False
    assert payload["release_qualification_authorized"] is False
    rendered = result.stdout + result.stderr
    assert hostile_entry not in rendered
    assert unsafe_manifest not in rendered
    assert "Traceback" not in rendered


def test_validate_rejects_malformed_json_without_traceback_or_input_echo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _copy_verifier(repo)
    gate = repo / "docs/evaluation/m1-gate.json"
    gate.parent.mkdir(parents=True)
    gate.write_text(f'{{"sensitive_input":"{SENSITIVE_SENTINEL}",', encoding="utf-8")

    result = _run_cli(repo, "validate", "--contracts-only", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["passed"] is False
    assert any("invalid JSON" in error for error in payload["errors"])
    rendered = result.stdout + result.stderr
    assert SENSITIVE_SENTINEL not in rendered
    assert "Traceback" not in rendered


def test_validate_does_not_execute_repo_local_verifier_code(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    marker = tmp_path / "repo_verifier_executed"
    verifier = repo / "scripts" / "verify_m1.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')",
                "def verify_contracts(root):",
                "    return [], {'source': 'malicious'}",
                "def verify_full_pilot(root):",
                "    return [], {'source': 'malicious'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _write_json(
        repo / "docs/evaluation/m1-gate.json",
        {"contracts": {}, "required_metric_ids": []},
    )

    result = _run_cli(repo, "validate", "--contracts-only", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["passed"] is False
    assert marker.exists() is False
    assert "malicious" not in result.stdout
    assert "Traceback" not in result.stderr


def test_validate_rejects_oversize_json_without_traceback_or_input_echo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _copy_verifier(repo)
    gate = repo / "docs/evaluation/m1-gate.json"
    gate.parent.mkdir(parents=True)
    gate.write_text('{"payload":"' + ("x" * (9 * 1024 * 1024)) + '"}', encoding="utf-8")

    result = _run_cli(repo, "validate", "--contracts-only", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["passed"] is False
    assert "JSON file exceeds size limit" in payload["errors"]
    assert "Traceback" not in result.stderr


def test_validate_rejects_non_utf8_input_without_traceback_or_input_echo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _copy_verifier(repo)
    gate = repo / "docs/evaluation/m1-gate.json"
    gate.parent.mkdir(parents=True)
    gate.write_bytes(
        b'{"sensitive_input":"' + SENSITIVE_SENTINEL.encode("ascii") + b'", "\xff": true}'
    )

    result = _run_cli(repo, "validate", "--contracts-only", "--json")

    assert result.returncode != 0
    payload = _json_stdout(result)
    assert payload["passed"] is False
    assert "invalid JSON: input is not UTF-8" in payload["errors"]
    rendered = result.stdout + result.stderr
    assert SENSITIVE_SENTINEL not in rendered
    assert "Traceback" not in rendered
