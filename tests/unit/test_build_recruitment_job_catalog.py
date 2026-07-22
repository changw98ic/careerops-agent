from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_catalog_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "build_recruitment_job_catalog.py"
    spec = importlib.util.spec_from_file_location("build_recruitment_job_catalog", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signal(
    *,
    classification: str,
    domain: str,
    url: str,
    score: int,
    raw_line_number: int,
) -> dict[str, Any]:
    return {
        "classification": classification,
        "evidence": [
            {
                "category": classification,
                "excerpt": "<html>raw page text must not be copied</html>",
                "matched_term": "Open Role",
                "rule": "open_roles",
            },
            {"category": "recruitment_mention", "rule": "career_word"},
        ],
        "provenance": {
            "body_truncated": False,
            "body_sha256": "a" * 64,
            "canonical_url": url,
            "captured_at": f"2026-07-19T00:00:{raw_line_number:02d}Z",
            "depth": raw_line_number,
            "http_status": 200,
            "raw_file": "/private/raw.jsonl",
            "raw_line_number": raw_line_number,
            "registrable_domain": domain,
            "requested_url": f"{url}?token=pk_live_{'A' * 24}#frag",
            "source_assessment": "first_party_career_entry",
            "source_record_id": f"agent-ecosystem-domain--{domain}",
            "source_relationship": "first_party",
        },
        "response_text": "secret body sk-proj-" + ("B" * 24),
        "signal_score": score,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_build_catalog_uses_only_compact_safe_fields_and_stats() -> None:
    catalog_module = _load_catalog_module()
    rows = [
        _signal(
            classification="career_portal",
            domain="beta.example",
            url="https://beta.example/careers",
            score=70,
            raw_line_number=2,
        ),
        _signal(
            classification="actual_job_posting",
            domain="alpha.example",
            url="https://alpha.example/jobs/agent-engineer",
            score=100,
            raw_line_number=1,
        ),
    ]

    catalog = catalog_module.build_catalog(rows, input_path=Path("input.jsonl"))

    assert catalog["metadata"]["schema"]["version"] == "recruitment_job_catalog_v1"
    assert catalog["stats"] == {
        "classification_counts": {"actual_job_posting": 1, "career_portal": 1},
        "exported_jobs": 2,
        "input_rows": 2,
        "skipped_unsafe": 0,
        "skipped_without_url": 0,
    }
    assert [job["classification"] for job in catalog["jobs"]] == [
        "actual_job_posting",
        "career_portal",
    ]
    first_job = catalog["jobs"][0]
    assert first_job["public_url"] == "https://alpha.example/jobs/agent-engineer"
    assert first_job["title"] == ""
    assert first_job["body_truncated"] is False
    assert first_job["company_key"] == "record:agent-ecosystem-domain--alpha.example"
    assert first_job["depth"] == 1
    assert first_job["http_status"] == 200
    assert first_job["source_assessment"] == "first_party_career_entry"
    assert first_job["source_relationship"] == "first_party"
    assert first_job["evidence"] == {
        "category_counts": {"actual_job_posting": 1, "recruitment_mention": 1},
        "rules": ["career_word", "open_roles"],
    }
    serialized = json.dumps(catalog, sort_keys=True)
    assert "input_path" not in serialized
    assert "output_path" not in serialized
    assert "response_text" not in serialized
    assert "raw_file" not in serialized
    assert "raw_line_number" not in serialized
    assert "raw_byte_offset" not in serialized
    assert "body_sha256" not in serialized
    assert "<html>" not in serialized
    assert "sk-proj-" not in serialized
    assert "pk_live_" not in serialized


def test_run_export_writes_deterministic_compact_json(tmp_path: Path) -> None:
    catalog_module = _load_catalog_module()
    input_path = tmp_path / "signals.jsonl"
    output_path = tmp_path / "catalog.json"
    rows = [
        _signal(
            classification="job_listing",
            domain="zeta.example",
            url="https://zeta.example/jobs",
            score=80,
            raw_line_number=2,
        ),
        _signal(
            classification="actual_job_posting",
            domain="alpha.example",
            url="https://alpha.example/jobs",
            score=100,
            raw_line_number=1,
        ),
    ]
    _write_jsonl(input_path, rows)

    first_summary = catalog_module.run_export(input_path, output_path)
    first_output = output_path.read_text(encoding="utf-8")
    second_summary = catalog_module.run_export(input_path, output_path)
    second_output = output_path.read_text(encoding="utf-8")

    assert first_summary["exported_jobs"] == 2
    assert "input_path" not in first_summary
    assert "output_path" not in first_summary
    assert first_summary["output_file"] == "catalog.json"
    assert second_summary["schema_version"] == "recruitment_job_catalog_v1"
    assert first_output == second_output
    assert "\n" not in first_output.strip()
    parsed = json.loads(first_output)
    assert [job["registrable_domain"] for job in parsed["jobs"]] == [
        "alpha.example",
        "zeta.example",
    ]


def test_run_export_streams_jsonl_rows_without_materializing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_module = _load_catalog_module()
    input_path = tmp_path / "signals.jsonl"
    output_path = tmp_path / "catalog.json"
    _write_jsonl(
        input_path,
        [
            _signal(
                classification="job_listing",
                domain="alpha.example",
                url="https://alpha.example/jobs",
                score=80,
                raw_line_number=1,
            )
        ],
    )
    observed: dict[str, str | int | None] = {
        "rows_type": None,
        "consumed_count": None,
    }

    def fake_build_catalog(rows: object, *, input_path: Path) -> dict[str, object]:
        observed["rows_type"] = type(rows).__name__
        assert not isinstance(rows, list)
        consumed_rows = list(rows)  # type: ignore[arg-type]
        observed["consumed_count"] = len(consumed_rows)
        return {
            "jobs": [],
            "metadata": {"schema": {"version": "test"}, "source_label": "test"},
            "stats": {"exported_jobs": 0},
        }

    monkeypatch.setattr(catalog_module, "build_catalog", fake_build_catalog)

    summary = catalog_module.run_export(input_path, output_path)

    assert observed == {"rows_type": "generator", "consumed_count": 1}
    assert summary["exported_jobs"] == 0


def test_catalog_skips_credential_like_item_value() -> None:
    catalog_module = _load_catalog_module()
    credential = "AIza" + ("A" * 35)

    catalog = catalog_module.build_catalog(
        [
            _signal(
                classification="job_listing",
                domain=credential,
                url="https://example.com/jobs",
                score=80,
                raw_line_number=1,
            )
        ],
        input_path=Path("input.jsonl"),
    )

    assert catalog["jobs"] == []
    assert catalog["stats"]["input_rows"] == 1
    assert catalog["stats"]["skipped_unsafe"] == 1
    assert json.dumps(catalog, sort_keys=True).find(credential) == -1


def test_catalog_skips_public_url_path_with_credential_like_token() -> None:
    catalog_module = _load_catalog_module()
    token = "sk-proj-" + ("D" * 24)
    rows = [
        _signal(
            classification="job_listing",
            domain="unsafe.example",
            url=f"https://unsafe.example/jobs/{token}",
            score=90,
            raw_line_number=1,
        ),
        _signal(
            classification="career_portal",
            domain="safe.example",
            url="https://safe.example/careers",
            score=70,
            raw_line_number=2,
        ),
    ]

    catalog = catalog_module.build_catalog(rows, input_path=Path("input.jsonl"))

    assert catalog["stats"]["input_rows"] == 2
    assert catalog["stats"]["exported_jobs"] == 1
    assert catalog["stats"]["skipped_unsafe"] == 1
    assert catalog["jobs"][0]["registrable_domain"] == "safe.example"
    serialized = json.dumps(catalog, sort_keys=True)
    assert token not in serialized
    assert "unsafe.example" not in serialized


def test_rows_without_safe_urls_are_skipped() -> None:
    catalog_module = _load_catalog_module()
    row = _signal(
        classification="job_listing",
        domain="alpha.example",
        url="mailto:jobs@alpha.example",
        score=80,
        raw_line_number=1,
    )

    catalog = catalog_module.build_catalog([row], input_path=Path("input.jsonl"))

    assert catalog["jobs"] == []
    assert catalog["stats"]["skipped_unsafe"] == 0
    assert catalog["stats"]["skipped_without_url"] == 1


def test_safe_url_drops_userinfo_and_query_but_keeps_host_port_path() -> None:
    catalog_module = _load_catalog_module()
    row = _signal(
        classification="job_listing",
        domain="jobs.example",
        url="https://user:password@Jobs.Example:8443/a/b",
        score=80,
        raw_line_number=1,
    )
    provenance = row["provenance"]
    assert isinstance(provenance, dict)
    provenance["canonical_url"] = "https://user:password@Jobs.Example:8443/a/b?token=secret#frag"

    catalog = catalog_module.build_catalog([row], input_path=Path("/private/signals.jsonl"))

    assert catalog["jobs"][0]["public_url"] == "https://jobs.example:8443/a/b"
    serialized = json.dumps(catalog, sort_keys=True)
    assert "user:password" not in serialized
    assert "token=secret" not in serialized
    assert "/private/signals.jsonl" not in serialized


def test_company_key_does_not_fall_back_to_signal_key() -> None:
    catalog_module = _load_catalog_module()
    row = _signal(
        classification="job_listing",
        domain="alpha.example",
        url="https://alpha.example/jobs",
        score=80,
        raw_line_number=1,
    )
    provenance = row["provenance"]
    assert isinstance(provenance, dict)
    provenance.pop("registrable_domain")
    provenance.pop("source_record_id")
    row["signal_key"] = "https://internal.example/private?token=sk-proj-" + ("C" * 24)

    catalog = catalog_module.build_catalog([row], input_path=Path("input.jsonl"))

    assert "company_key" not in catalog["jobs"][0]
    assert catalog["jobs"][0]["id"] == "unknown-source:https://alpha.example/jobs"
    serialized = json.dumps(catalog, sort_keys=True)
    assert "internal.example" not in serialized
    assert "sk-proj-" not in serialized
