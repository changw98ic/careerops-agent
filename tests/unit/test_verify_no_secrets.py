from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_scanner_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "verify_no_secrets.py"
    spec = importlib.util.spec_from_file_location("verify_no_secrets", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_scanner_catches_supported_credential_formats() -> None:
    scanner = _load_scanner_module()
    patterns = scanner.PATTERNS

    samples = {
        "private_key": b"-----BEGIN " + b"PRIVATE KEY-----",
        "aws_access_key": b"AK" + b"IA1234567890ABCDEF",
        "github_token": b"gh" + b"p_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_fine_grained_token": b"github" + b"_pat_0123456789_abcdefghijklmnopqrstuv",
        "google_api_key": b"AIza" + b"a" * 35,
        "slack_token": b"xox" + b"b-0123456789-abcdefghijk",
    }

    for name, value in samples.items():
        assert patterns[name].search(value) is not None
