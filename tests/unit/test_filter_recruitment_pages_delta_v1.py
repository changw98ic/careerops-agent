from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def _load_delta_filter_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "filter_recruitment_pages_delta_v1.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("filter_recruitment_pages_delta_v1", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bootstrap_uses_prior_summary_line_count_when_endpoint_has_no_signal(
    tmp_path: Path,
) -> None:
    delta_filter = _load_delta_filter_module()
    raw_path = tmp_path / "raw.jsonl"
    first_line = json.dumps({"status": "no_signal"}) + "\n"
    second_line = json.dumps({"status": "appended"}) + "\n"
    raw_path.write_text(first_line + second_line, encoding="utf-8")
    signals_path = tmp_path / "signals.jsonl"
    signals_path.write_text("", encoding="utf-8")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps({"total_raw_records_scanned": 1}),
        encoding="utf-8",
    )

    bootstrap = delta_filter._bootstrap_from_live_filter(raw_path, signals_path, summary_path)

    assert bootstrap == {
        "bootstrap_mode": "summary_line_count",
        "bootstrap_source": str(summary_path),
        "next_byte_offset": len(first_line.encode("utf-8")),
        "next_line_number": 2,
    }


def test_delta_filter_finishes_when_a_source_has_no_positive_signals(tmp_path: Path) -> None:
    delta_filter = _load_delta_filter_module()
    raw_path = tmp_path / "raw.jsonl"
    raw_path.write_text("{}\n", encoding="utf-8")
    output_dir = tmp_path / "output"

    summary = delta_filter.run_delta_filter(
        raw_path=raw_path,
        output_dir=output_dir,
        bootstrap_signals=output_dir / "recruitment_page_signals.jsonl",
        bootstrap_summary=output_dir / "summary.json",
        start_at_beginning=True,
    )

    assert summary["total_raw_records_scanned"] == 1
    assert summary["total_signal_records_after_run"] == 0
    assert summary["company_count"] == 0
    assert not (output_dir / "recruitment_page_signals.jsonl").exists()
    assert (output_dir / "summary.json").is_file()
