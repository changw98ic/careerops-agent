from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_runner_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "process_recruitment_crawl_batches.py"
    spec = importlib.util.spec_from_file_location("process_recruitment_crawl_batches", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _raw_row(
    response_text: str,
    *,
    final_url: str,
    source_record_id: str = "agent-ecosystem-domain--example.com",
) -> dict[str, Any]:
    return {
        "body_bytes": len(response_text.encode()),
        "body_sha256": hashlib.sha256(response_text.encode()).hexdigest(),
        "body_truncated": False,
        "canonical_url": final_url,
        "captured_at": "2026-07-19T00:00:00Z",
        "content_type": "text/html",
        "depth": 1,
        "discovered_from": "https://example.com/careers",
        "final_url": final_url,
        "http_status": 200,
        "registrable_domain": "example.com",
        "requested_url": final_url,
        "source_assessment": "first_party_career_entry",
        "source_index": 1,
        "source_record_id": source_record_id,
        "source_relationship": "first_party",
        "response_text": response_text,
    }


def test_process_sources_incrementally_skips_partial_line_then_merges(tmp_path: Path) -> None:
    runner = _load_runner_module()
    crawl_dir = tmp_path / "crawl"
    crawl_dir.mkdir()
    raw_path = crawl_dir / runner.RAW_FILENAME
    first_row = _raw_row(
        "<main>We are hiring. View open roles.</main>",
        final_url="https://example.com/careers",
    )
    second_row = _raw_row(
        '<script type="application/ld+json">{"@type":"JobPosting"}</script>',
        final_url="https://example.com/jobs/engineer",
    )
    partial_second_row = json.dumps(second_row, sort_keys=True)
    raw_path.write_text(
        json.dumps(first_row, sort_keys=True) + "\n" + partial_second_row[:-1],
        encoding="utf-8",
    )

    sources, skipped = runner.discover_sources(
        crawl_outputs=[crawl_dir],
        sharded_batches=[],
        workspace_root=tmp_path,
    )
    output_dir = tmp_path / "derived"
    first_summary = runner.process_sources(
        sources=sources,
        output_dir=output_dir,
        skipped_sources=skipped,
    )

    assert first_summary["merge"]["merged_signal_rows"] == 1
    assert first_summary["company_rollup"]["company_count"] == 1
    assert (
        first_summary["processed_sources"][0]["filter_summary"]["stopped_at_partial_line"] is True
    )

    with raw_path.open("a", encoding="utf-8") as handle:
        handle.write(partial_second_row[-1] + "\n")

    second_summary = runner.process_sources(sources=sources, output_dir=output_dir)

    assert second_summary["merge"]["merged_signal_rows"] == 2
    assert second_summary["company_rollup"]["company_count"] == 1
    assert (
        second_summary["processed_sources"][0]["filter_summary"]["new_signal_records_appended"] == 1
    )
    merged_path = output_dir / "merged" / "recruitment_page_signals.jsonl"
    assert len(merged_path.read_text(encoding="utf-8").splitlines()) == 2


def test_discover_sources_reads_started_shards_and_skips_unstarted_ones(tmp_path: Path) -> None:
    runner = _load_runner_module()
    batch_dir = tmp_path / "batch"
    first_output = batch_dir / "outputs" / "shard-00"
    first_output.mkdir(parents=True)
    (first_output / runner.RAW_FILENAME).write_text("", encoding="utf-8")
    second_output = batch_dir / "outputs" / "shard-01"
    plan = {
        "shards": [
            {"output_dir": str(first_output.relative_to(tmp_path))},
            {"output_dir": str(second_output.relative_to(tmp_path))},
        ]
    }
    (batch_dir / runner.SHARDED_PLAN_FILENAME).write_text(
        json.dumps(plan),
        encoding="utf-8",
    )

    sources, skipped = runner.discover_sources(
        crawl_outputs=[],
        sharded_batches=[batch_dir],
        workspace_root=tmp_path,
    )

    assert [source.source_dir for source in sources] == [first_output.resolve()]
    assert skipped == [
        {
            "reason": "raw_output_not_created",
            "source_dir": str(second_output.resolve()),
        }
    ]


def test_sharded_plan_cannot_refer_to_a_raw_file_outside_its_batch(tmp_path: Path) -> None:
    runner = _load_runner_module()
    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    plan = {"shards": [{"output_dir": str((tmp_path / "other").resolve())}]}
    (batch_dir / runner.SHARDED_PLAN_FILENAME).write_text(
        json.dumps(plan),
        encoding="utf-8",
    )

    try:
        runner.discover_sources(
            crawl_outputs=[],
            sharded_batches=[batch_dir],
            workspace_root=tmp_path,
        )
    except ValueError as error:
        assert "escapes the batch directory" in str(error)
    else:  # pragma: no cover - explicit guard for the safety property
        raise AssertionError("expected an escaping sharded output directory to fail")


def test_processing_output_cannot_be_written_into_a_crawl_output(tmp_path: Path) -> None:
    runner = _load_runner_module()
    crawl_dir = tmp_path / "crawl"
    crawl_dir.mkdir()
    (crawl_dir / runner.RAW_FILENAME).write_text("", encoding="utf-8")
    sources, _skipped = runner.discover_sources(
        crawl_outputs=[crawl_dir],
        sharded_batches=[],
        workspace_root=tmp_path,
    )

    with pytest.raises(ValueError, match="must not overlap a crawl output"):
        runner.process_sources(sources=sources, output_dir=crawl_dir / "derived")


def test_sharded_batch_rejects_a_symlinked_raw_file(tmp_path: Path) -> None:
    runner = _load_runner_module()
    batch_dir = tmp_path / "batch"
    output_dir = batch_dir / "outputs" / "shard-00"
    output_dir.mkdir(parents=True)
    outside_raw = tmp_path / "outside.jsonl"
    outside_raw.write_text("{}\n", encoding="utf-8")
    (output_dir / runner.RAW_FILENAME).symlink_to(outside_raw)
    (batch_dir / runner.SHARDED_PLAN_FILENAME).write_text(
        json.dumps({"shards": [{"output_dir": str(output_dir.relative_to(tmp_path))}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not be a symlink"):
        runner.discover_sources(
            crawl_outputs=[],
            sharded_batches=[batch_dir],
            workspace_root=tmp_path,
        )


def test_processing_lock_rejects_another_owner(tmp_path: Path) -> None:
    runner = _load_runner_module()
    output_dir = tmp_path / "derived"

    with (
        runner._processing_lock(output_dir),
        pytest.raises(ValueError, match="already owns"),
        runner._processing_lock(output_dir),
    ):
        pass
