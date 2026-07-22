#!/usr/bin/env python3
"""Redact derived recruitment raw snapshots without modifying source files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUDITED_DERIVED_SNAPSHOTS = (
    Path(
        "datasets/derived/recruitment_pages/2026-07-18/"
        "parallel_expansion_endpoint_sharded_v2_filter_v1/priority_sharded_v1_raw_snapshot.jsonl"
    ),
    Path(
        "datasets/derived/recruitment_pages/2026-07-18/"
        "priority_sharded_v1_filter_v1/priority_sharded_v1_raw_snapshot.jsonl"
    ),
    Path(
        "datasets/derived/recruitment_pages/2026-07-18/"
        "priority_sharded_v2_filter_v1/priority_sharded_v1_raw_snapshot.jsonl"
    ),
    Path(
        "datasets/derived/recruitment_pages/2026-07-18/"
        "priority_sharded_v3_filter_v1/priority_sharded_v1_raw_snapshot.jsonl"
    ),
)


@dataclass(frozen=True)
class CredentialPattern:
    name: str
    pattern: re.Pattern[str]
    prefix: Callable[[re.Match[str]], str]


CREDENTIAL_PATTERNS = (
    CredentialPattern(
        name="private_key_marker",
        pattern=re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        prefix=lambda _match: "private_key_marker",
    ),
    CredentialPattern(
        name="aws_access_key_id",
        pattern=re.compile(r"\b(?P<prefix>(?:AKIA|ASIA))[A-Z0-9]{16}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="github_token",
        pattern=re.compile(r"\b(?P<prefix>ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="github_fine_grained_token",
        pattern=re.compile(r"\b(?P<prefix>github_pat)_[A-Za-z0-9_]{22,}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="google_api_key",
        pattern=re.compile(r"\b(?P<prefix>AIza)[0-9A-Za-z_-]{35}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="openai_key",
        pattern=re.compile(r"\b(?P<prefix>sk)(?:-proj)?-[A-Za-z0-9_-]{20,}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="slack_token",
        pattern=re.compile(r"\b(?P<prefix>xox[baprs])-[A-Za-z0-9-]{10,}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
    CredentialPattern(
        name="stripe_live_key",
        pattern=re.compile(r"\b(?P<prefix>(?:pk|sk)_live)_[A-Za-z0-9]{16,}\b"),
        prefix=lambda match: match.group("prefix"),
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_line(row: Any) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _marker(pattern: str, prefix: str, length: int) -> str:
    return f"[REDACTED_CREDENTIAL:{pattern}:prefix={prefix}:length={length}]"


def redact_string(value: str, stats: dict[str, Any]) -> str:
    redacted = value
    for credential in CREDENTIAL_PATTERNS:
        prefix_counts = stats["credential_prefix_counts"].setdefault(credential.name, Counter())
        length_ranges = stats["credential_length_ranges"].setdefault(credential.name, [None, None])

        def replace(
            match: re.Match[str],
            *,
            credential: CredentialPattern = credential,
            length_ranges: list[int | None] = length_ranges,
            prefix_counts: Counter[str] = prefix_counts,
        ) -> str:
            prefix = credential.prefix(match)
            length = len(match.group(0))
            stats["credential_replacements"][credential.name] += 1
            prefix_counts[prefix] += 1
            if length_ranges[0] is None or length < length_ranges[0]:
                length_ranges[0] = length
            if length_ranges[1] is None or length > length_ranges[1]:
                length_ranges[1] = length
            return _marker(credential.name, prefix, length)

        redacted = credential.pattern.sub(replace, redacted)
    return redacted


def redact_json_value(value: Any, stats: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, child in value.items():
            if key == "response_text":
                stats["response_text_keys_removed"] += 1
                continue
            output[key] = redact_json_value(child, stats)
        return output
    if isinstance(value, list):
        return [redact_json_value(child, stats) for child in value]
    if isinstance(value, str):
        return redact_string(value, stats)
    return value


def _redaction_validation_errors(value: Any, path: str = "$") -> list[str]:
    """Return non-sensitive descriptions of residual supported secret patterns."""

    if isinstance(value, dict):
        errors: list[str] = []
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "response_text":
                errors.append(f"response_text key at {child_path}")
                continue
            errors.extend(_redaction_validation_errors(child, child_path))
        return errors
    if isinstance(value, list):
        return [
            error
            for index, child in enumerate(value)
            for error in _redaction_validation_errors(child, f"{path}[{index}]")
        ]
    if isinstance(value, str):
        return [
            f"{credential.name} pattern at {path}"
            for credential in CREDENTIAL_PATTERNS
            if credential.pattern.search(value) is not None
        ]
    return []


def _require_valid_redaction(value: Any) -> None:
    errors = _redaction_validation_errors(value)
    if errors:
        preview = "; ".join(errors[:5])
        suffix = "" if len(errors) <= 5 else f"; plus {len(errors) - 5} more"
        raise ValueError(f"redaction validation failed: {preview}{suffix}")


def _serializable_stats(stats: dict[str, Any]) -> dict[str, Any]:
    prefix_counts = {
        name: dict(counter)
        for name, counter in sorted(stats["credential_prefix_counts"].items())
        if counter
    }
    length_ranges = {
        name: {"min": value[0], "max": value[1]}
        for name, value in sorted(stats["credential_length_ranges"].items())
        if value[0] is not None
    }
    return {
        "credential_length_ranges": length_ranges,
        "credential_prefix_counts": prefix_counts,
        "credential_replacements": dict(stats["credential_replacements"]),
        "input_records": stats["input_records"],
        "response_text_keys_removed": stats["response_text_keys_removed"],
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "credential_length_ranges": {},
        "credential_prefix_counts": {},
        "credential_replacements": Counter(),
        "input_records": 0,
        "response_text_keys_removed": 0,
    }


def _reserve_new_path(path: Path) -> None:
    if path.exists():
        raise ValueError(f"output path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def _publish_without_overwrite(temp_path: Path, output_path: Path) -> None:
    try:
        os.link(temp_path, output_path)
    except FileExistsError as error:
        raise ValueError(f"output path already exists: {output_path}") from error
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_json_without_overwrite(path: Path, payload: dict[str, Any]) -> None:
    _reserve_new_path(path)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _publish_without_overwrite(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def redact_jsonl_file(
    *,
    input_path: Path,
    output_path: Path,
    summary_path: Path | None = None,
) -> dict[str, Any]:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if input_path == output_path:
        raise ValueError("input and output paths must differ")
    if not input_path.exists():
        raise ValueError(f"input path does not exist: {input_path}")
    if input_path.is_symlink():
        raise ValueError(f"input path must not be a symlink: {input_path}")
    _reserve_new_path(output_path)
    if summary_path is not None:
        summary_path = summary_path.resolve()
        if summary_path in {input_path, output_path}:
            raise ValueError("summary path must differ from input and output paths")
        _reserve_new_path(summary_path)

    stats = _empty_stats()
    temp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with (
            input_path.open("r", encoding="utf-8") as source,
            temp_path.open(
                "w",
                encoding="utf-8",
            ) as target,
        ):
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"invalid JSONL at line {line_number}: {error}") from error
                redacted_row = redact_json_value(row, stats)
                _require_valid_redaction(redacted_row)
                stats["input_records"] += 1
                target.write(_json_line(redacted_row) + "\n")
        _publish_without_overwrite(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    summary = {
        "created_at": _now(),
        "input_path": str(input_path),
        "input_size_bytes": input_path.stat().st_size,
        "output_path": str(output_path),
        "output_size_bytes": output_path.stat().st_size,
        "redaction_version": "recruitment_derived_snapshot_redaction_v1",
        **_serializable_stats(stats),
    }
    if summary_path is not None:
        _write_json_without_overwrite(summary_path, summary)
    return summary


def _default_redacted_path(source_path: Path, output_dir: Path) -> Path:
    return output_dir / source_path.parent.name / source_path.with_suffix(".redacted.jsonl").name


def redact_audited_snapshots(
    *,
    output_dir: Path,
    snapshot_paths: Sequence[Path] = AUDITED_DERIVED_SNAPSHOTS,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    summaries = []
    for source_path in snapshot_paths:
        output_path = _default_redacted_path(source_path, output_dir)
        summary_path = output_path.with_suffix(".summary.json")
        summaries.append(
            redact_jsonl_file(
                input_path=source_path,
                output_path=output_path,
                summary_path=summary_path,
            )
        )
    aggregate = {
        "created_at": _now(),
        "redaction_version": "recruitment_derived_snapshot_redaction_v1",
        "snapshot_count": len(summaries),
        "snapshots": summaries,
    }
    _write_json_without_overwrite(output_dir / "redaction_summary.json", aggregate)
    return aggregate


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="Input JSONL file to redact.")
    mode.add_argument(
        "--audited-snapshots",
        action="store_true",
        help="Redact the four audited derived raw snapshots.",
    )
    parser.add_argument("--output", type=Path, help="Output JSONL path for --input mode.")
    parser.add_argument("--summary", type=Path, help="Optional summary JSON path for --input mode.")
    parser.add_argument("--output-dir", type=Path, help="Output directory for --audited-snapshots.")
    args = parser.parse_args(argv)
    if args.input is not None and args.output is None:
        parser.error("--input requires --output")
    if args.audited_snapshots and args.output_dir is None:
        parser.error("--audited-snapshots requires --output-dir")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.input is not None:
            summary = redact_jsonl_file(
                input_path=args.input,
                output_path=args.output,
                summary_path=args.summary,
            )
        else:
            summary = redact_audited_snapshots(output_dir=args.output_dir)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
