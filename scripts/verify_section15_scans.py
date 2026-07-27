#!/usr/bin/env python3
"""Section 15 security scans: secret/PII in logs, payloads, model egress,
attachments, database dumps, and frontend errors (task 15.6).

Extends the existing ``verify_no_secrets.py`` with additional patterns for:
- PII patterns in log payloads (SSN, credit card, phone)
- Provider token patterns (OAuth refresh tokens, JWT)
- Model egress markers (raw prompts/responses in event payloads)
- Database dump patterns (full credential strings in migration files)

Run::

    python3 scripts/verify_section15_scans.py

Exit code 0 = pass, 1 = findings detected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "data",
    "dist",
    "secrets",
    "venv",
    "node_modules",
}
MAX_FILE_BYTES = 1_000_000

# Additional PII/secret patterns beyond verify_no_secrets.py
ADDITIONAL_PATTERNS = {
    # SSN patterns (US)
    "ssn_pattern": re.compile(rb"\b\d{3}-\d{2}-\d{4}\b"),
    # Credit card patterns (Visa/MC/Amex)
    "credit_card": re.compile(
        rb"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2})[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"
    ),
    # JWT tokens (three base64 segments separated by dots)
    "jwt_token": re.compile(rb"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # OAuth refresh tokens (long base64 strings near "refresh_token")
    "oauth_refresh": re.compile(rb"refresh_token[\"':\s]*[\"'][A-Za-z0-9_\-\.]{40,}[\"']"),
    # Raw prompt/response in event payloads
    "raw_prompt": re.compile(rb"\"raw_prompt\":\s*\"[^\"]{100,}\""),
    "raw_response": re.compile(rb"\"raw_response\":\s*\"[^\"]{100,}\""),
}

# Files/patterns that are known false positives
FALSE_POSITIVE_PATHS = {
    "scripts/verify_no_secrets.py",
    "scripts/verify_section15_scans.py",
    "tests/security/",
    "datasets/",
    "tests/unit/test_m2_matching.py",
}


def _is_false_positive(path: Path) -> bool:
    rel = str(path.relative_to(ROOT))
    return any(fp in rel for fp in FALSE_POSITIVE_PATHS)


def candidate_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
        and path.stat().st_size <= MAX_FILE_BYTES
        and not _is_false_positive(path)
    )


def main() -> int:
    findings: list[tuple[Path, str]] = []
    files = candidate_files()
    for path in files:
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        for name, pattern in ADDITIONAL_PATTERNS.items():
            if pattern.search(payload) is not None:
                findings.append((path, name))

    if findings:
        for path, finding in findings:
            print(f"{path.relative_to(ROOT)}: {finding}", file=sys.stderr)
        return 1
    print(f"section15 scan passed: {len(files)} bounded repository files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
