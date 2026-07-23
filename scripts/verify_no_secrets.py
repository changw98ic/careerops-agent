#!/usr/bin/env python3
"""Fail on high-confidence plaintext credential formats in repository files."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".qoder",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "data",
    "dist",
    "secrets",
    "venv",
}
MAX_FILE_BYTES = 1_000_000
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github_token": re.compile(rb"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{36,255}\b"),
    "github_fine_grained_token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    "google_api_key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(rb"\bxox(?:a|b|p|r|s)-[0-9A-Za-z-]{20,}\b"),
}


def candidate_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
        and path.stat().st_size <= MAX_FILE_BYTES
    )


def main() -> int:
    findings: list[tuple[Path, str]] = []
    files = candidate_files()
    for path in files:
        try:
            payload = path.read_bytes()
        except OSError:
            findings.append((path, "unreadable_file"))
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(payload) is not None:
                findings.append((path, name))

    if findings:
        for path, finding in findings:
            print(f"{path.relative_to(ROOT)}: {finding}", file=sys.stderr)
        return 1
    print(f"secret scan passed: {len(files)} bounded repository files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
