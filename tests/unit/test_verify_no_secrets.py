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
        "google_oauth_client_secret": b"GOCSPX-" + b"a" * 28,
        "google_refresh_token": b"1//" + b"0" + b"a" * 44,
        "google_access_token": b"ya29." + b"a" * 80,
        "google_service_account_private_key": (
            b'{"type":"service_account","private_key":"-----BEGIN ' + b"PRIVATE KEY-----\\n"
        ),
        "slack_token": b"xox" + b"b-0123456789-abcdefghijk",
    }

    for name, value in samples.items():
        assert patterns[name].search(value) is not None


def test_secret_scanner_ignores_google_placeholder_references() -> None:
    scanner = _load_scanner_module()
    patterns = scanner.PATTERNS

    samples = [
        b"GOOGLE_CLIENT_SECRET=your-google-client-secret",
        b"GOOGLE_REFRESH_TOKEN=<google-refresh-token>",
        b"GOOGLE_ACCESS_TOKEN=ya29.placeholder-token",
        b'"private_key": "-----BEGIN ' + b"PRIVATE KEY-----\\nREDACTED\\n-----END PRIVATE KEY-----",
        b"docs mention GOCSPX-your-client-secret as a placeholder",
        b"docs mention 1//your-refresh-token as a placeholder",
    ]

    for value in samples:
        google_matches = [
            name
            for name, pattern in patterns.items()
            if name.startswith("google_") and pattern.search(value) is not None
        ]
        assert google_matches == []
