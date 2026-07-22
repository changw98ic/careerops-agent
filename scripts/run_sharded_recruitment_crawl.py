#!/usr/bin/env python3
"""Prepare and optionally run isolated shards for the next recruitment crawl batch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import collect_recruitment_pages as collector

DEFAULT_SHARD_COUNT = 4
DEFAULT_MAX_TOTAL_BYTES = 80 * 1024 * 1024 * 1024
DEFAULT_BATCH_NAME = "next"
TRIAGE_SHARD_FILENAME = "triage_input.jsonl"
ADDITIONAL_SHARD_FILENAME = "additional_seeds.jsonl"
PLAN_FILENAME = "sharded_recruitment_crawl_plan.json"
RUN_SUMMARY_FILENAME = "sharded_recruitment_crawl_run_summary.json"


@dataclass(frozen=True)
class ShardPlan:
    shard_index: int
    root_keys: tuple[str, ...]
    triage_rows: tuple[dict[str, Any], ...]
    additional_seed_rows: tuple[dict[str, Any], ...]
    raw_byte_cap: int
    shard_input_dir: Path
    shard_output_dir: Path
    command: tuple[str, ...]


def _captured_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {error.msg}") from error
            if not isinstance(decoded, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(decoded)
    return rows


def _json_line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(f"{_json_line(row)}\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def stable_shard_index(root_key: str, shard_count: int) -> int:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    digest = collector.hashlib.sha256(root_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def triage_root_key(row: dict[str, Any], source_index: int) -> str:
    record_id = row.get("record_id")
    if isinstance(record_id, str) and record_id.strip():
        return f"record:{record_id.strip()}"
    domain = row.get("registrable_domain")
    if isinstance(domain, str) and domain.strip():
        return f"domain:{domain.strip().lower()}"
    return f"triage-index:{source_index}"


def additional_seed_root_key(row: dict[str, Any], source_index: int) -> str:
    record_id = row.get("source_record_id")
    if isinstance(record_id, str) and record_id.strip():
        return f"record:{record_id.strip()}"
    root_url = row.get("root_url")
    if isinstance(root_url, str) and root_url.strip():
        canonical_root = collector.canonicalize_url(root_url)
        if canonical_root:
            return f"root-url:{canonical_root}"
    domain = row.get("registrable_domain")
    if isinstance(domain, str) and domain.strip():
        return f"domain:{domain.strip().lower()}"
    return f"additional-index:{source_index}"


def allocate_raw_byte_caps(
    *,
    shard_count: int,
    max_total_bytes: int,
    existing_aggregate_bytes: int,
) -> tuple[int, ...]:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if max_total_bytes < 1:
        raise ValueError("max_total_bytes must be at least 1")
    if existing_aggregate_bytes < 0:
        raise ValueError("existing_aggregate_bytes must be non-negative")
    remaining = max_total_bytes - existing_aggregate_bytes
    if remaining < shard_count:
        raise ValueError("remaining aggregate byte budget must leave at least one byte per shard")
    base = remaining // shard_count
    extra = remaining % shard_count
    return tuple(base + (1 if shard_index < extra else 0) for shard_index in range(shard_count))


def _collector_command(
    *,
    script_path: Path,
    triage_input: Path,
    additional_seeds: Path | None,
    output_dir: Path,
    raw_byte_cap: int,
    per_shard_concurrency: int,
    retry_rounds: int,
    depth: int,
    max_response_bytes: int,
    timeout_seconds: float,
    user_agent: str,
    max_concurrency_per_host: int,
    collector_supports_max_concurrency_per_host: bool,
    allow_sandbox_egress_alias: bool,
    no_resume: bool,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        str(script_path),
        "--triage-input",
        str(triage_input),
        "--output-dir",
        str(output_dir),
        "--max-stored-bytes",
        str(raw_byte_cap),
        "--max-response-bytes",
        str(max_response_bytes),
        "--concurrency",
        str(per_shard_concurrency),
        "--retry-rounds",
        str(retry_rounds),
        "--depth",
        str(depth),
        "--timeout-seconds",
        str(timeout_seconds),
        "--user-agent",
        user_agent,
    ]
    if collector_supports_max_concurrency_per_host:
        command.extend(["--max-concurrency-per-host", str(max_concurrency_per_host)])
    if additional_seeds is not None:
        command.extend(["--additional-seeds", str(additional_seeds)])
    if allow_sandbox_egress_alias:
        command.append("--allow-sandbox-egress-alias")
    if no_resume:
        command.append("--no-resume")
    return tuple(command)


def build_shard_plan(
    *,
    triage_rows: Sequence[dict[str, Any]],
    additional_seed_rows: Sequence[dict[str, Any]],
    shard_count: int,
    input_root: Path,
    output_root: Path,
    raw_byte_caps: Sequence[int],
    script_path: Path,
    per_shard_concurrency: int,
    retry_rounds: int,
    depth: int,
    max_response_bytes: int,
    timeout_seconds: float,
    user_agent: str,
    max_concurrency_per_host: int = 8,
    collector_supports_max_concurrency_per_host: bool = False,
    allow_sandbox_egress_alias: bool = False,
    no_resume: bool = False,
) -> list[ShardPlan]:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if len(raw_byte_caps) != shard_count:
        raise ValueError("raw_byte_caps length must match shard_count")
    if per_shard_concurrency < 1:
        raise ValueError("per_shard_concurrency must be at least 1")
    if max_concurrency_per_host < 1:
        raise ValueError("max_concurrency_per_host must be at least 1")

    triage_shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    additional_shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    root_keys_by_shard: list[set[str]] = [set() for _ in range(shard_count)]

    for index, row in enumerate(triage_rows):
        root_key = triage_root_key(row, index)
        shard_index = stable_shard_index(root_key, shard_count)
        triage_shards[shard_index].append(row)
        root_keys_by_shard[shard_index].add(root_key)

    for index, row in enumerate(additional_seed_rows):
        root_key = additional_seed_root_key(row, index)
        shard_index = stable_shard_index(root_key, shard_count)
        additional_shards[shard_index].append(row)
        root_keys_by_shard[shard_index].add(root_key)

    plans: list[ShardPlan] = []
    for shard_index in range(shard_count):
        shard_name = f"shard-{shard_index:02d}"
        shard_input_dir = input_root / shard_name
        shard_output_dir = output_root / shard_name
        triage_input = shard_input_dir / TRIAGE_SHARD_FILENAME
        additional_input = (
            shard_input_dir / ADDITIONAL_SHARD_FILENAME if additional_shards[shard_index] else None
        )
        command = _collector_command(
            script_path=script_path,
            triage_input=triage_input,
            additional_seeds=additional_input,
            output_dir=shard_output_dir,
            raw_byte_cap=raw_byte_caps[shard_index],
            per_shard_concurrency=per_shard_concurrency,
            retry_rounds=retry_rounds,
            depth=depth,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            max_concurrency_per_host=max_concurrency_per_host,
            collector_supports_max_concurrency_per_host=(
                collector_supports_max_concurrency_per_host
            ),
            allow_sandbox_egress_alias=allow_sandbox_egress_alias,
            no_resume=no_resume,
        )
        plans.append(
            ShardPlan(
                shard_index=shard_index,
                root_keys=tuple(sorted(root_keys_by_shard[shard_index])),
                triage_rows=tuple(triage_shards[shard_index]),
                additional_seed_rows=tuple(additional_shards[shard_index]),
                raw_byte_cap=raw_byte_caps[shard_index],
                shard_input_dir=shard_input_dir,
                shard_output_dir=shard_output_dir,
                command=command,
            )
        )
    return plans


def write_shard_inputs(plans: Sequence[ShardPlan]) -> None:
    for plan in plans:
        _write_jsonl_atomic(plan.shard_input_dir / TRIAGE_SHARD_FILENAME, plan.triage_rows)
        if plan.additional_seed_rows:
            _write_jsonl_atomic(
                plan.shard_input_dir / ADDITIONAL_SHARD_FILENAME,
                plan.additional_seed_rows,
            )


def _plan_json(
    *,
    plans: Sequence[ShardPlan],
    batch_dir: Path,
    collector_supports_max_concurrency_per_host: bool,
    max_total_bytes: int,
    existing_aggregate_bytes: int,
    run_requested: bool,
    max_parallel_shards: int,
) -> dict[str, Any]:
    return {
        "batch_dir": str(batch_dir),
        "collector_supports_max_concurrency_per_host": (
            collector_supports_max_concurrency_per_host
        ),
        "created_at": _captured_at(),
        "existing_aggregate_bytes": existing_aggregate_bytes,
        "max_parallel_shards": max_parallel_shards,
        "max_total_bytes": max_total_bytes,
        "remaining_aggregate_bytes": max_total_bytes - existing_aggregate_bytes,
        "run_requested": run_requested,
        "shard_count": len(plans),
        "shards": [
            {
                "additional_seed_count": len(plan.additional_seed_rows),
                "command": list(plan.command),
                "output_dir": str(plan.shard_output_dir),
                "raw_byte_cap": plan.raw_byte_cap,
                "root_keys": list(plan.root_keys),
                "shard_index": plan.shard_index,
                "triage_row_count": len(plan.triage_rows),
            }
            for plan in plans
        ],
        "total_preallocated_raw_bytes": sum(plan.raw_byte_cap for plan in plans),
    }


def run_shards(plans: Sequence[ShardPlan], *, max_parallel_shards: int) -> dict[str, Any]:
    if max_parallel_shards < 1:
        raise ValueError("max_parallel_shards must be at least 1")

    def run_one(plan: ShardPlan) -> dict[str, Any]:
        plan.shard_output_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            plan.command,
            check=False,
            capture_output=True,
            text=True,
        )
        return {
            "command": list(plan.command),
            "returncode": completed.returncode,
            "shard_index": plan.shard_index,
            "stderr": completed.stderr,
            "stdout": completed.stdout,
        }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel_shards) as executor:
        futures = [executor.submit(run_one, plan) for plan in plans]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda result: result["shard_index"])
    return {
        "completed_at": _captured_at(),
        "failed_shards": [result["shard_index"] for result in results if result["returncode"] != 0],
        "results": results,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage-input", type=Path, required=True)
    parser.add_argument("--additional-seeds", type=Path, action="append", default=[])
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--shards", type=int, default=DEFAULT_SHARD_COUNT)
    parser.add_argument("--per-shard-concurrency", type=int, default=8)
    parser.add_argument("--max-concurrency-per-host", type=int, default=8)
    parser.add_argument("--max-parallel-shards", type=int, default=None)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--existing-aggregate-bytes", type=int, default=0)
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=collector.DEFAULT_MAX_RESPONSE_BYTES,
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--retry-rounds", type=int, default=4)
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--user-agent", default=collector.DEFAULT_USER_AGENT)
    parser.add_argument(
        "--collector-script",
        type=Path,
        default=Path(__file__).with_name("collect_recruitment_pages.py"),
    )
    parser.add_argument("--allow-sandbox-egress-alias", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--run", action="store_true")
    return parser.parse_args(argv)


def collector_script_supports_max_concurrency_per_host(script_path: Path) -> bool:
    try:
        return "--max-concurrency-per-host" in script_path.read_text(encoding="utf-8")
    except OSError:
        return False


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        shard_count = args.shards
        max_parallel_shards = args.max_parallel_shards or shard_count
        raw_byte_caps = allocate_raw_byte_caps(
            shard_count=shard_count,
            max_total_bytes=args.max_total_bytes,
            existing_aggregate_bytes=args.existing_aggregate_bytes,
        )
        triage_rows = _load_jsonl(args.triage_input)
        additional_seed_rows: list[dict[str, Any]] = []
        for path in args.additional_seeds:
            additional_seed_rows.extend(_load_jsonl(path))

        batch_dir = args.batch_dir
        collector_supports_host_limit = collector_script_supports_max_concurrency_per_host(
            args.collector_script
        )
        plans = build_shard_plan(
            triage_rows=triage_rows,
            additional_seed_rows=additional_seed_rows,
            shard_count=shard_count,
            input_root=batch_dir / "inputs",
            output_root=batch_dir / "outputs",
            raw_byte_caps=raw_byte_caps,
            script_path=args.collector_script,
            per_shard_concurrency=args.per_shard_concurrency,
            retry_rounds=args.retry_rounds,
            depth=args.depth,
            max_response_bytes=args.max_response_bytes,
            timeout_seconds=args.timeout_seconds,
            user_agent=args.user_agent,
            max_concurrency_per_host=args.max_concurrency_per_host,
            collector_supports_max_concurrency_per_host=collector_supports_host_limit,
            allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
            no_resume=args.no_resume,
        )
        write_shard_inputs(plans)
        plan_json = _plan_json(
            plans=plans,
            batch_dir=batch_dir,
            collector_supports_max_concurrency_per_host=collector_supports_host_limit,
            max_total_bytes=args.max_total_bytes,
            existing_aggregate_bytes=args.existing_aggregate_bytes,
            run_requested=args.run,
            max_parallel_shards=max_parallel_shards,
        )
        _write_json_atomic(batch_dir / PLAN_FILENAME, plan_json)
        print(json.dumps(plan_json, ensure_ascii=False, sort_keys=True))
        if not args.run:
            return 0

        run_summary = run_shards(plans, max_parallel_shards=max_parallel_shards)
        _write_json_atomic(batch_dir / RUN_SUMMARY_FILENAME, run_summary)
        return 1 if run_summary["failed_shards"] else 0
    except (OSError, ValueError) as error:
        print(f"sharded recruitment crawl failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
