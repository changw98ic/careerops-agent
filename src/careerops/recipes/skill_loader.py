"""Tier 2 LLM skill loader.

A *skill* is a Markdown playbook (``SKILL.md``) that tells the CrawlAgent how
to approach a site whose structure is too dynamic for a declarative YAML
recipe (Tier 1). Each file is a YAML frontmatter block
(``source_type`` / ``executor_mode`` / ``match``) followed by a free-form
Markdown body describing the crawl steps. Skills are *data* (guidance the LLM
agent reads), not per-site Python code.

This module only loads and validates the format. ``CrawlAgent`` consumes the
result: at crawl time it matches the source URL against each skill's
``match`` hints (``host_suffix`` / ``url_patterns``) and, on a hit, prepends
the playbook to the ReAct system prompt so the model follows known-good steps
instead of blind ReAct. On a miss the agent falls back to the generic Tier 2
loop unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import cast

import yaml


@dataclasses.dataclass(frozen=True, slots=True)
class Skill:
    """A loaded Tier 2 skill playbook.

    Attributes:
        source_type: Catalog key, mirrors the Tier 1 ``source_type``
            (e.g. ``"workday"``). Used as the dict key by :func:`load_skills`.
        executor_mode: How the engine should run this skill. ``"ego"`` means a
            browser-driven LLM agent; defaults to ``"ego"`` when omitted.
        match: Site-matching hints parsed from the frontmatter
            (e.g. ``{"host_suffix": "myworkdayjobs.com"}``). Opaque to the
            loader — interpreted by the future matcher.
        body: The Markdown playbook, with frontmatter stripped and surrounding
            whitespace trimmed.
        path: Filesystem path the skill was loaded from, for diagnostics.
    """

    source_type: str
    executor_mode: str
    match: dict[str, object]
    body: str
    path: Path


# A leading YAML frontmatter block delimited by ``---`` lines. ``re.DOTALL``
# lets ``.*?`` span the (possibly multi-line) YAML header. The opening ``---``
# must be the very first bytes of the file; a missing block fails the match
# and surfaces as a ``ValueError`` in :func:`load_skill`.
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def load_skill(path: Path) -> Skill:
    """Parse a single ``SKILL.md`` at ``path`` into a :class:`Skill`.

    Raises:
        ValueError: The file has no frontmatter block, the frontmatter is not
            a YAML mapping, or the required ``source_type`` field is missing
            or not a non-empty string.
    """
    text = Path(path).read_text(encoding="utf-8")
    front = _FRONTMATTER.match(text)
    if not front:
        raise ValueError(
            f"skill {path} missing frontmatter (expected leading '---\\n...\\n---\\n')"
        )
    # ``yaml.safe_load`` returns ``Any``; narrow to a typed mapping via an
    # ``isinstance`` guard plus ``cast`` so pyright strict sees ``dict[str,
    # object]`` rather than propagating ``Unknown`` through every field.
    meta_raw: object = yaml.safe_load(front.group(1))
    if not isinstance(meta_raw, dict):
        raise ValueError(f"skill {path} frontmatter must be a YAML mapping")
    meta = cast("dict[str, object]", meta_raw)

    source_type = meta.get("source_type")
    if not isinstance(source_type, str) or not source_type:
        raise ValueError(f"skill {path} missing required field 'source_type'")

    executor = meta.get("executor_mode")
    executor_mode = executor if isinstance(executor, str) else "ego"

    match_raw = meta.get("match")
    match = cast("dict[str, object]", match_raw) if isinstance(match_raw, dict) else {}

    return Skill(
        source_type=source_type,
        executor_mode=executor_mode,
        match=match,
        body=front.group(2).strip(),
        path=Path(path),
    )


def _load_manifest_skills(skills_dir: Path, manifest_path: Path) -> dict[str, Skill] | None:
    """Load skills declared in ``manifest_path``'s ``skills`` index.

    The catalog manifest (``vendor/crawl-recipes/manifest.json``) is the single
    source of truth for what ships: a skill present on disk but absent from the
    manifest's ``skills`` array is NOT loaded (PR #7 review round 2). This
    keeps the manifest authoritative — CONTRIBUTING states every addition must
    be referenced from ``manifest.json`` so the loader can discover it.

    Path confinement mirrors the recipe loader: each manifest ``path`` MUST be
    relative and MUST NOT escape the catalog root (no ``..`` segments, no
    absolute paths), so a malformed manifest cannot read arbitrary files.

    Returns ``None`` when the manifest has no ``skills`` key (older catalog or
    a recipes-only manifest) so the caller can fall back to glob discovery.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("skills")
    if entries is None:
        return None

    catalog_root = manifest_path.parent
    out: dict[str, Skill] = {}
    for entry in entries:
        rel = str(entry.get("path") or "")
        if not rel:
            continue  # malformed entry; skip rather than crash the agent
        p = Path(rel)
        if p.is_absolute() or any(part == ".." for part in p.parts):
            # Path confinement: never let a manifest walk above the catalog.
            continue
        skill_path = catalog_root / p
        if not skill_path.exists():
            continue
        skill = load_skill(skill_path)
        out[skill.source_type] = skill
    return out


def load_skills(skills_dir: Path) -> dict[str, Skill]:
    """Load the deployable skills, keyed by ``source_type``.

    Discovery is manifest-driven (PR #7 review round 2): the catalog's
    ``manifest.json`` (at ``skills_dir.parent / "manifest.json"``) holds a
    ``skills`` index whose ``path`` entries point at each ``SKILL.md``. Only
    manifest-listed skills load — a file on disk but not in the manifest is an
    authoring artefact, not a deployable skill, and is ignored.

    Falls back to globbing ``*/SKILL.md`` (skipping ``_``-prefixed authoring
    dirs) only when there is no manifest or it predates the ``skills`` key.
    This keeps the loader usable against a bare ``skills/`` directory (tests,
    stripped layouts) while making the manifest the authoritative index
    wherever it exists.
    """
    manifest_path = Path(skills_dir).parent / "manifest.json"
    if manifest_path.exists():
        manifest_skills = _load_manifest_skills(Path(skills_dir), manifest_path)
        if manifest_skills is not None:
            return manifest_skills

    out: dict[str, Skill] = {}
    for skill_path in Path(skills_dir).glob("*/SKILL.md"):
        if skill_path.parent.name.startswith("_"):
            continue
        skill = load_skill(skill_path)
        out[skill.source_type] = skill
    return out
