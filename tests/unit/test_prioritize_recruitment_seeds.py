from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_selector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "prioritize_recruitment_seeds.py"
    spec = importlib.util.spec_from_file_location("prioritize_recruitment_seeds", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_collector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "collect_recruitment_pages.py"
    spec = importlib.util.spec_from_file_location("collect_recruitment_pages", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sitemap_row(url: str, relationship: str = "first_party") -> dict[str, Any]:
    return {
        "canonical_url": url,
        "discovered_at": "2026-07-18T00:00:00Z",
        "discovery_method": "robots.txt and XML sitemap parsing; no browser, login, forms, or JS",
        "registrable_domain": "example.com",
        "root_url": "https://example.com",
        "source_assessment": "first_party_career_entry",
        "source_index": 1,
        "source_record_id": "agent-ecosystem-domain--example.com",
        "source_relationship": relationship,
        "source_sitemap_url": "https://example.com/sitemap.xml",
        "url": url,
    }


def _ats_row(url: str) -> dict[str, Any]:
    return {
        "canonical_url": url,
        "discovered_at": "2026-07-18T00:00:00Z",
        "discovery_method": "public ATS JSON feed; no browser, login, forms, or JS",
        "registrable_domain": "example.com",
        "source_feed_ats": "greenhouse",
        "source_feed_token": "example",
        "source_feed_url": "https://boards-api.greenhouse.io/v1/boards/example/jobs",
        "source_assessment": "external_ats_needs_verification",
        "source_job_id": "123",
        "source_job_location": "Remote",
        "source_job_title": "AI Engineer",
        "source_record_id": "agent-ecosystem-domain--example.com",
        "source_relationship": "known_ats",
        "url": url,
    }


def test_score_row_selects_career_and_job_pages_but_excludes_docs_and_vendor_pages(
    tmp_path: Path,
) -> None:
    selector = _load_selector_module()

    career = selector.score_row(
        _sitemap_row("https://example.com/careers"),
        source_kind="sitemap",
        source_line_number=1,
        input_path=tmp_path / "sitemap.jsonl",
    )
    job_detail = selector.score_row(
        _sitemap_row("https://example.com/careers/software-engineer"),
        source_kind="sitemap",
        source_line_number=2,
        input_path=tmp_path / "sitemap.jsonl",
    )
    docs = selector.score_row(
        _sitemap_row("https://example.com/docs/api/jobs"),
        source_kind="sitemap",
        source_line_number=3,
        input_path=tmp_path / "sitemap.jsonl",
    )
    vendor = selector.score_row(
        _sitemap_row("https://example.com/solutions/recruitment-platform"),
        source_kind="sitemap",
        source_line_number=4,
        input_path=tmp_path / "sitemap.jsonl",
    )

    assert career is not None
    assert career.target_kind == "career_portal"
    assert job_detail is not None
    assert job_detail.target_kind == "likely_job_detail_page"
    assert docs is None
    assert vendor is None


def test_ats_feed_rows_rank_before_duplicate_sitemap_urls(tmp_path: Path) -> None:
    selector = _load_selector_module()
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    sitemap_path = tmp_path / "sitemap.jsonl"
    ats_path = tmp_path / "ats.jsonl"
    output_path = tmp_path / "prioritized.jsonl"
    sitemap_path.write_text(json.dumps(_sitemap_row(url, "known_ats")) + "\n", encoding="utf-8")
    ats_path.write_text(json.dumps(_ats_row(url)) + "\n", encoding="utf-8")

    summary = selector.run_selector(
        sitemap_inputs=[sitemap_path],
        ats_feed_inputs=[ats_path],
        output_path=output_path,
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert summary["row_count"] == 1
    assert rows[0]["source_kind"] == "ats_feed"
    assert rows[0]["score"] == 100
    assert rows[0]["source_assessment"] == "external_ats_needs_verification"
    assert rows[0]["source_index"] == 0
    assert rows[0]["url"] == url
    assert rows[0]["validation"]["selected_without_fetching"] is True
    assert rows[0]["validation"]["excluded_forbidden_access_claim"] is False
    assert rows[0]["source_provenance"]["input_line_number"] == 1


def test_output_rows_are_collector_compatible_additional_seeds(tmp_path: Path) -> None:
    selector = _load_selector_module()
    collector = _load_collector_module()
    sitemap_path = tmp_path / "sitemap.jsonl"
    ats_path = tmp_path / "ats.jsonl"
    output_path = tmp_path / "prioritized.jsonl"
    sitemap_path.write_text(
        json.dumps(_sitemap_row("https://example.com/careers/designer")) + "\n",
        encoding="utf-8",
    )
    ats_path.write_text(
        json.dumps(_ats_row("https://job-boards.greenhouse.io/example/jobs/123")) + "\n",
        encoding="utf-8",
    )

    selector.run_selector(
        sitemap_inputs=[sitemap_path],
        ats_feed_inputs=[ats_path],
        output_path=output_path,
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    selected = collector.select_additional_seed_urls(rows)

    assert len(selected) == 2
    assert [seed.canonical_url for seed in selected] == [
        "https://job-boards.greenhouse.io/example/jobs/123",
        "https://example.com/careers/designer",
    ]
    assert [seed.source_assessment for seed in selected] == [
        "external_ats_needs_verification",
        "first_party_career_entry",
    ]
    assert selected[1].discovered_from == "https://example.com/sitemap.xml"


def test_run_selector_is_deterministic_and_preserves_provenance(tmp_path: Path) -> None:
    selector = _load_selector_module()
    sitemap_path = tmp_path / "sitemap.jsonl"
    output_path = tmp_path / "prioritized.jsonl"
    sitemap_path.write_text(
        "\n".join(
            [
                json.dumps(_sitemap_row("https://example.com/blog/recruiting")),
                json.dumps(_sitemap_row("https://example.com/jobs")),
                json.dumps(_sitemap_row("https://example.com/careers/designer")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    selector.run_selector(
        sitemap_inputs=[sitemap_path],
        ats_feed_inputs=[],
        output_path=output_path,
    )
    first = output_path.read_text(encoding="utf-8")
    selector.run_selector(
        sitemap_inputs=[sitemap_path],
        ats_feed_inputs=[],
        output_path=output_path,
    )
    second = output_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in first.splitlines()]

    assert first == second
    assert [row["priority_rank"] for row in rows] == [1, 2]
    assert rows[0]["score"] >= rows[1]["score"]
    assert rows[0]["source_provenance"]["source_record_id"] == (
        "agent-ecosystem-domain--example.com"
    )


def test_sitemap_cap_is_per_company_and_does_not_cap_ats_rows(tmp_path: Path) -> None:
    selector = _load_selector_module()
    sitemap_path = tmp_path / "sitemap.jsonl"
    ats_path = tmp_path / "ats.jsonl"
    output_path = tmp_path / "prioritized.jsonl"
    sitemap_rows = [_sitemap_row(f"https://example.com/careers/role-{index}") for index in range(4)]
    ats_rows = [
        _ats_row(f"https://job-boards.greenhouse.io/example/jobs/{index}") for index in range(3)
    ]
    sitemap_path.write_text(
        "".join(json.dumps(row) + "\n" for row in sitemap_rows),
        encoding="utf-8",
    )
    ats_path.write_text(
        "".join(json.dumps(row) + "\n" for row in ats_rows),
        encoding="utf-8",
    )

    summary = selector.run_selector(
        sitemap_inputs=[sitemap_path],
        ats_feed_inputs=[ats_path],
        output_path=output_path,
        max_sitemap_per_company=2,
    )
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]

    assert summary["row_count"] == 5
    assert summary["source_kind_counts"] == {"ats_feed": 3, "sitemap": 2}
    assert [row["source_kind"] for row in rows[:3]] == ["ats_feed", "ats_feed", "ats_feed"]
