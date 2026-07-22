from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_runner_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "run_sharded_recruitment_crawl.py"
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("run_sharded_recruitment_crawl", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _triage_row(record_id: str, domain: str = "example.com") -> dict[str, Any]:
    return {
        "employer_hiring_assessment": "first_party_career_entry",
        "links": [{"href": f"https://{domain}/careers", "relationship": "first_party"}],
        "record_id": record_id,
        "registrable_domain": domain,
    }


def _additional_row(source_record_id: str, url: str) -> dict[str, Any]:
    return {
        "canonical_url": url,
        "registrable_domain": "example.com",
        "source_assessment": "first_party_career_entry",
        "source_record_id": source_record_id,
    }


def test_partition_keeps_triage_and_additional_rows_for_same_root_in_same_shard(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()
    raw_byte_caps = runner.allocate_raw_byte_caps(
        shard_count=4,
        max_total_bytes=100,
        existing_aggregate_bytes=8,
    )
    plans = runner.build_shard_plan(
        triage_rows=[_triage_row("alpha"), _triage_row("beta", "beta.example")],
        additional_seed_rows=[
            _additional_row("alpha", "https://example.com/jobs/engineering"),
            _additional_row("beta", "https://beta.example/jobs"),
        ],
        shard_count=4,
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        raw_byte_caps=raw_byte_caps,
        script_path=Path("scripts/collect_recruitment_pages.py"),
        per_shard_concurrency=3,
        retry_rounds=4,
        depth=2,
        max_response_bytes=128,
        timeout_seconds=9.0,
        user_agent="test-agent",
        max_concurrency_per_host=8,
        collector_supports_max_concurrency_per_host=False,
    )

    owner_by_record: dict[str, set[int]] = {"alpha": set(), "beta": set()}
    for plan in plans:
        for row in plan.triage_rows:
            owner_by_record[row["record_id"]].add(plan.shard_index)
        for row in plan.additional_seed_rows:
            owner_by_record[row["source_record_id"]].add(plan.shard_index)

    assert owner_by_record == {
        "alpha": {runner.stable_shard_index("record:alpha", 4)},
        "beta": {runner.stable_shard_index("record:beta", 4)},
    }
    assert sum(plan.raw_byte_cap for plan in plans) + 8 == 100
    assert all("--retry-rounds" in plan.command for plan in plans)
    assert all("--concurrency" in plan.command for plan in plans)
    assert all("3" in plan.command for plan in plans)
    assert all("--max-concurrency-per-host" not in plan.command for plan in plans)


def test_write_shard_inputs_and_manifest_are_isolated_by_shard(tmp_path: Path) -> None:
    runner = _load_runner_module()
    plans = runner.build_shard_plan(
        triage_rows=[_triage_row("alpha"), _triage_row("beta", "beta.example")],
        additional_seed_rows=[_additional_row("alpha", "https://example.com/jobs")],
        shard_count=2,
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        raw_byte_caps=(50, 50),
        script_path=Path("collector.py"),
        per_shard_concurrency=1,
        retry_rounds=4,
        depth=1,
        max_response_bytes=10,
        timeout_seconds=1.0,
        user_agent="ua",
        max_concurrency_per_host=8,
        collector_supports_max_concurrency_per_host=False,
        no_resume=True,
    )

    runner.write_shard_inputs(plans)
    plan_json = runner._plan_json(
        plans=plans,
        batch_dir=tmp_path,
        collector_supports_max_concurrency_per_host=False,
        max_total_bytes=110,
        existing_aggregate_bytes=10,
        run_requested=False,
        max_parallel_shards=2,
    )

    assert plan_json["total_preallocated_raw_bytes"] == 100
    assert plan_json["collector_supports_max_concurrency_per_host"] is False
    assert plan_json["remaining_aggregate_bytes"] == 100
    for plan in plans:
        triage_path = plan.shard_input_dir / runner.TRIAGE_SHARD_FILENAME
        assert triage_path.exists()
        assert plan.shard_output_dir == tmp_path / "outputs" / f"shard-{plan.shard_index:02d}"
        written_rows = [
            json.loads(line) for line in triage_path.read_text(encoding="utf-8").splitlines()
        ]
        assert written_rows == list(plan.triage_rows)
        if plan.additional_seed_rows:
            additional_path = plan.shard_input_dir / runner.ADDITIONAL_SHARD_FILENAME
            assert [
                json.loads(line)
                for line in additional_path.read_text(encoding="utf-8").splitlines()
            ] == list(plan.additional_seed_rows)
            assert str(additional_path) in plan.command
        assert "--no-resume" in plan.command


def test_main_dry_run_writes_plan_without_launching_collectors(tmp_path: Path) -> None:
    runner = _load_runner_module()
    triage_input = tmp_path / "triage.jsonl"
    triage_input.write_text(json.dumps(_triage_row("alpha")) + "\n", encoding="utf-8")
    batch_dir = tmp_path / "batch"

    result = runner.main(
        [
            "--triage-input",
            str(triage_input),
            "--batch-dir",
            str(batch_dir),
            "--shards",
            "2",
            "--existing-aggregate-bytes",
            "10",
            "--max-total-bytes",
            "20",
        ]
    )

    assert result == 0
    plan = json.loads((batch_dir / runner.PLAN_FILENAME).read_text(encoding="utf-8"))
    assert plan["run_requested"] is False
    assert plan["total_preallocated_raw_bytes"] == 10
    assert not (batch_dir / runner.RUN_SUMMARY_FILENAME).exists()


def test_build_shard_plan_passes_host_concurrency_when_collector_supports_it(
    tmp_path: Path,
) -> None:
    runner = _load_runner_module()

    plans = runner.build_shard_plan(
        triage_rows=[_triage_row("alpha")],
        additional_seed_rows=[],
        shard_count=1,
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        raw_byte_caps=(100,),
        script_path=Path("collector.py"),
        per_shard_concurrency=12,
        retry_rounds=4,
        depth=1,
        max_response_bytes=10,
        timeout_seconds=1.0,
        user_agent="ua",
        max_concurrency_per_host=8,
        collector_supports_max_concurrency_per_host=True,
    )

    command = plans[0].command
    host_flag_index = command.index("--max-concurrency-per-host")
    assert command[host_flag_index + 1] == "8"


def test_collector_script_support_detection_is_read_only_and_optional(tmp_path: Path) -> None:
    runner = _load_runner_module()
    unsupported = tmp_path / "collector.py"
    supported = tmp_path / "supported_collector.py"
    unsupported.write_text("parser.add_argument('--concurrency')\n", encoding="utf-8")
    supported.write_text("parser.add_argument('--max-concurrency-per-host')\n", encoding="utf-8")

    assert runner.collector_script_supports_max_concurrency_per_host(unsupported) is False
    assert runner.collector_script_supports_max_concurrency_per_host(supported) is True
    assert (
        runner.collector_script_supports_max_concurrency_per_host(tmp_path / "missing.py") is False
    )


def test_allocate_raw_byte_caps_rejects_overcommitted_existing_usage() -> None:
    runner = _load_runner_module()

    try:
        runner.allocate_raw_byte_caps(
            shard_count=4,
            max_total_bytes=10,
            existing_aggregate_bytes=8,
        )
    except ValueError as error:
        assert "at least one byte per shard" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
