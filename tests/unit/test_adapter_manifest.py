"""Manifest integrity + description-coverage acceptance for ATS fixtures.

Backs v0.4 section 5 ("数据验收", reproducible data acceptance): the manifest at
``tests/fixtures/adapter_manifest.json`` is the frozen, hash-anchored record
of the synthetic fixture corpus under ``tests/fixtures/adapter_responses/``.

Two contracts are asserted here:

1. **Directory hash** -- the SHA-256 recorded in the manifest is recomputed
   from the fixture files (sorted by name, ``name\\0bytes`` fed to the hasher)
   and must match. Any drift in a fixture file (or a file added/removed)
   flips the hash and fails the suite, forcing a conscious manifest update.
2. **Description coverage** -- for every source the manifest's
   ``description_covered`` count is recomputed directly from the fixture
   payload and must agree. ``description_covered`` counts samples whose raw
   response carries a non-empty JD body; it is a property of the *data*
   (not of whether the v1 list adapter happens to surface it), so the
   acceptance number stays stable as adapters evolve.

No dependency on ``data/`` or the network -- fixtures are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "adapter_responses"
MANIFEST_PATH = FIXTURES_DIR.parent / "adapter_manifest.json"

_JSON_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _compute_dir_hash(directory: Path) -> str:
    """Reproduce the manifest's directory hash over sorted fixture files."""
    hasher = hashlib.sha256()
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _greenhouse_list_coverage(data: Any) -> int:
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return sum(1 for j in jobs if isinstance(j, dict) and str(j.get("content", "")).strip())


def _greenhouse_detail_coverage(data: Any) -> int:
    return 1 if isinstance(data, dict) and str(data.get("content", "")).strip() else 0


def _ashby_coverage(data: Any) -> int:
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return sum(1 for j in jobs if isinstance(j, dict) and str(j.get("description", "")).strip())


def _ashby_detail_coverage(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    return (
        1
        if (
            str(data.get("descriptionPlain", "")).strip()
            or str(data.get("description", "")).strip()
        )
        else 0
    )


def _lever_coverage(data: Any) -> int:
    if not isinstance(data, list):
        return 0
    return sum(
        1
        for j in data
        if isinstance(j, dict)
        and (str(j.get("description", "")).strip() or str(j.get("descriptionPlain", "")).strip())
    )


def _json_ld_coverage(html: str) -> int:
    count = 0
    for match in _JSON_LD_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("@type") == "JobPosting"
            and str(parsed.get("description", "")).strip()
        ):
            count += 1
    return count


def _json_ld_sample_count(html: str) -> int:
    count = 0
    for match in _JSON_LD_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("@type") == "JobPosting":
            count += 1
    return count


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class TestManifestIntegrity:
    def test_manifest_file_exists_and_parses(self) -> None:
        assert MANIFEST_PATH.is_file()
        data = _manifest()
        assert isinstance(data, dict)
        assert data["version"] == "1"

    def test_fixtures_directory_hash_matches_manifest(self) -> None:
        manifest = _manifest()
        expected = manifest["fixtures_sha256"]
        actual = _compute_dir_hash(FIXTURES_DIR)

        assert actual == expected, (
            "fixtures directory hash drifted from manifest; regenerate "
            "tests/fixtures/adapter_manifest.json after changing any fixture"
        )

    def test_manifest_records_expected_fixture_dir(self) -> None:
        manifest = _manifest()

        assert manifest["fixtures_dir"] == "tests/fixtures/adapter_responses"
        assert FIXTURES_DIR.is_dir()

    def test_no_hardcoded_legacy_totals(self) -> None:
        """The manifest must not parrot the historical 7191/8057 baselines."""
        text = MANIFEST_PATH.read_text(encoding="utf-8")

        assert "7191" not in text
        assert "8057" not in text

    def test_totals_match_per_source_sum(self) -> None:
        manifest = _manifest()
        sources = manifest["sources"]

        assert manifest["total_sample_count"] == sum(s["sample_count"] for s in sources.values())
        assert manifest["total_description_covered"] == sum(
            s["description_covered"] for s in sources.values()
        )


class TestDescriptionCoverage:
    """Each source's description_covered is recomputed from its fixture payload."""

    def test_greenhouse_list_has_no_description_in_list_endpoint(self) -> None:
        manifest = _manifest()
        data = _load_json("greenhouse_list.json")
        covered = _greenhouse_list_coverage(data)

        assert manifest["sources"]["greenhouse"]["description_covered"] == covered == 0

    def test_greenhouse_detail_carries_content_body(self) -> None:
        manifest = _manifest()
        data = _load_json("greenhouse_detail.json")

        assert (
            manifest["sources"]["greenhouse_detail"]["description_covered"]
            == _greenhouse_detail_coverage(data)
            == 1
        )

    def test_json_ld_jobpostings_carry_description(self) -> None:
        manifest = _manifest()
        html = (FIXTURES_DIR / "jsonld_page.html").read_text(encoding="utf-8")

        assert manifest["sources"]["json_ld"]["sample_count"] == _json_ld_sample_count(html)
        assert manifest["sources"]["json_ld"]["description_covered"] == _json_ld_coverage(html)
        assert manifest["sources"]["json_ld"]["description_covered"] >= 1

    def test_lever_list_carries_description(self) -> None:
        manifest = _manifest()
        data = _load_json("lever_list.json")

        assert manifest["sources"]["lever"]["description_covered"] == _lever_coverage(data)
        assert manifest["sources"]["lever"]["description_covered"] >= 1

    def test_ashby_list_omits_description_body(self) -> None:
        manifest = _manifest()
        data = _load_json("ashby_list.json")

        assert manifest["sources"]["ashby"]["description_covered"] == _ashby_coverage(data)
        assert manifest["sources"]["ashby"]["description_covered"] == 0

    def test_ashby_detail_carries_description_body(self) -> None:
        manifest = _manifest()
        data = _load_json("ashby_detail.json")

        assert (
            manifest["sources"]["ashby_detail"]["description_covered"]
            == _ashby_detail_coverage(data)
            == 1
        )
