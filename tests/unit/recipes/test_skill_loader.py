"""Behaviour tests for the Tier 2 skill loader.

Covers:

* ``load_skill`` splits a ``SKILL.md`` into frontmatter meta + Markdown body,
  coerces the parsed types (``match`` inline mapping, ``source_type`` key),
  and defaults ``executor_mode`` to ``"ego"`` when omitted.
* ``load_skills`` walks a skills directory and keys results by
  ``source_type``.
* Malformed input surfaces as ``ValueError``: a file with no frontmatter
  block, and one whose frontmatter omits the required ``source_type``.
* Underscore-prefixed directories (e.g. ``_template``) are skipped — they are
  authoring aids, not catalog entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from careerops.recipes.skill_loader import Skill, load_skill, load_skills


def _write_skill(
    skill_dir: Path,
    source_type: str = "demo",
    *,
    body: str = "# demo skill\nstep 1\n",
    extra_front: str = "",
) -> Path:
    """Write a minimal valid ``SKILL.md`` under ``skill_dir`` and return its path."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nsource_type: {source_type}\n{extra_front}---\n{body}",
        encoding="utf-8",
    )
    return path


def test_load_skill_parses_frontmatter_and_body(tmp_path):
    path = _write_skill(
        tmp_path / "workday",
        source_type="workday",
        extra_front=('executor_mode: ego\nmatch: {host_suffix: "myworkdayjobs.com"}\n'),
        body="# Workday skill\nPOST + facet 细分\n",
    )

    skill = load_skill(path)

    assert isinstance(skill, Skill)
    assert skill.source_type == "workday"
    assert skill.executor_mode == "ego"
    assert skill.match == {"host_suffix": "myworkdayjobs.com"}
    assert skill.body == "# Workday skill\nPOST + facet 细分"
    assert skill.path == path


def test_load_skill_defaults_executor_mode_to_ego(tmp_path):
    path = _write_skill(tmp_path / "demo", source_type="demo")
    skill = load_skill(path)
    assert skill.executor_mode == "ego"
    # match defaults to an empty dict when the frontmatter omits it.
    assert skill.match == {}


def test_load_skills_scans_directory_keyed_by_source_type(tmp_path):
    _write_skill(tmp_path / "workday", source_type="workday")
    _write_skill(tmp_path / "lever", source_type="lever")

    skills = load_skills(tmp_path)

    assert set(skills) == {"workday", "lever"}
    assert all(isinstance(s, Skill) for s in skills.values())
    assert skills["workday"].source_type == "workday"


def test_load_skill_raises_on_missing_frontmatter(tmp_path):
    path = tmp_path / "bare.md"
    path.write_text("# just markdown\nno frontmatter here\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing frontmatter"):
        load_skill(path)


def test_load_skill_raises_on_missing_source_type(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nexecutor_mode: ego\n---\n# no source_type\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source_type"):
        load_skill(path)


def test_load_skills_skips_underscore_prefixed_dirs(tmp_path):
    # A real skill...
    _write_skill(tmp_path / "workday", source_type="workday")
    # ...alongside a template that must NOT enter the catalog.
    _write_skill(tmp_path / "_template", source_type="example")

    skills = load_skills(tmp_path)

    assert set(skills) == {"workday"}


def test_workday_skill_in_vendor_catalog_loads():
    """The committed workday skill under vendor/crawl-recipes loads cleanly.

    Guards against the playbook and the loader drifting apart (e.g. a
    frontmatter edit that breaks ``yaml.safe_load``).
    """
    repo_root = Path(__file__).resolve().parents[3]
    skills = load_skills(repo_root / "vendor" / "crawl-recipes" / "skills")

    assert "workday" in skills
    workday = skills["workday"]
    assert workday.executor_mode == "ego"
    assert workday.match == {"host_suffix": "myworkdayjobs.com"}
    assert "facet" in workday.body
