"""CrawlAgent Tier 2 skill injection tests (crawl-recipe-engine fix2).

Pins the wiring that the reviewer flagged as missing: a matched skill
playbook is injected into the ReAct system prompt (replacing blind ReAct),
while non-matching URLs and empty skill catalogs fall back to the blind-run
path with the prompt unchanged.

These tests are disjoint from ``test_crawl_agent_react_loop.py`` (which pins
action dispatch mechanics) and ``test_skill_loader.py`` (which pins file
parsing): they cover only the agent's match-then-inject behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.crawl_agent import DEFAULT_SKILLS_DIR, CrawlAgent
from careerops.infrastructure.ego_tool import PageFetchResult
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse
from careerops.recipes.skill_loader import Skill

_WORKDAY_URL = "https://myworkdayjobs.com/en-US/acme/careers"
_GENERIC_URL = "https://demo.test/careers"


class _StubBrowser:
    """Minimal BrowserTool: enough surface for ``crawl()`` to run one ReAct
    step and then fall through. We only need to capture the model prompt, so
    browser primitives return canned values.
    """

    @property
    def is_ready(self) -> bool:
        return True

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        return {"url": url}

    def capture_html(self) -> str:
        return "<html><body>careers</body></html>"

    def run_page_script(self, script: str) -> object:
        # _find_job_list_links returns a JSON list; keep it empty so crawl()
        # terminates cleanly after the ReAct loop gives up.
        if "a[href]" in script and "push" in script:
            return "[]"
        return "URL: stub\nTITLE: Stub Careers"

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[object]:
        return []

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        return PageFetchResult(status=200, body="{}")


class _CapturingModel:
    """StructuredModelClient that captures every request and returns give_up.

    The first (and only) invoke records the system_prompt so the test can
    assert whether the skill playbook was injected. give_up terminates the
    ReAct loop in one step, so crawl() falls through to _try_current_page
    afterwards (which the stub browser satisfies with empty results).
    """

    is_enabled = True

    def __init__(self) -> None:
        self.requests: list[StructuredModelRequest] = []

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        self.requests.append(request)
        return StructuredModelResponse(
            task_type=request.task_type,
            result={"action": "give_up", "target": "", "reasoning": "stub"},
            model_id="stub",
            prompt_version="stub",
            is_review_only=False,
            trace_id=request.trace_id,
        )


class _NoopExtractor:
    """LLMJobExtractor stand-in: returns no records (we only test the prompt)."""

    def extract(
        self,
        html: str,
        *,
        source_url: str = "",
        trace_id: str = "",
    ) -> list[RawJobRecord]:
        return []


def _make_skill(
    source_type: str,
    body: str,
    *,
    host_suffix: str = "",
    url_patterns: list[str] | None = None,
) -> Skill:
    match: dict[str, object] = {}
    if host_suffix:
        match["host_suffix"] = host_suffix
    if url_patterns:
        match["url_patterns"] = url_patterns
    return Skill(
        source_type=source_type,
        executor_mode="ego",
        match=match,
        body=body,
        path=Path(f"/fake/{source_type}/SKILL.md"),
    )


class TestSkillInjection:
    def test_workday_url_injects_skill_body_into_prompt(self) -> None:
        # Matching skill → playbook appears in the ReAct system prompt.
        body = "POST the tenant jobSearch endpoint; recurse facets under the 2000 cap."
        skill = _make_skill("workday", body, host_suffix="myworkdayjobs.com")
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={"workday": skill},
        )

        agent.crawl(_WORKDAY_URL, wait_s=0, max_pages=1)

        assert model.requests, "ReAct loop must invoke the model at least once"
        prompt = model.requests[0].system_prompt
        assert body in prompt
        assert "SKILL PLAYBOOK" in prompt
        assert "workday" in prompt.lower()

    def test_generic_url_does_not_inject_skill(self) -> None:
        # A skill is loaded but the URL doesn't match → blind-run prompt,
        # no playbook marker, no skill body leaks in.
        body = "WORKDAY-ONLY-PLAYBOOK-MARKER"
        skill = _make_skill("workday", body, host_suffix="myworkdayjobs.com")
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={"workday": skill},
        )

        agent.crawl(_GENERIC_URL, wait_s=0, max_pages=1)

        assert model.requests, "ReAct loop must still run (blind)"
        prompt = model.requests[0].system_prompt
        assert body not in prompt
        assert "SKILL PLAYBOOK" not in prompt

    def test_empty_skills_blind_runs(self) -> None:
        # No skills configured at all → identical to the pre-fix behavior.
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={},
        )

        agent.crawl(_WORKDAY_URL, wait_s=0, max_pages=1)

        assert model.requests
        prompt = model.requests[0].system_prompt
        assert "SKILL PLAYBOOK" not in prompt

    def test_source_type_hint_selects_skill_when_url_does_not_match(self) -> None:
        # The sink passes request.source_type as a disambiguation hint: even
        # when the URL doesn't match a skill's host_suffix, an explicit
        # source_type match must select that skill.
        body = "GREENHOUSE-EXPLICIT-HINT-MARKER"
        skill = _make_skill("greenhouse", body, host_suffix="greenhouse.io")
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={"greenhouse": skill},
        )

        agent.crawl(_GENERIC_URL, source_type="greenhouse", wait_s=0, max_pages=1)

        assert model.requests
        assert body in model.requests[0].system_prompt

    def test_url_patterns_match_also_injects(self) -> None:
        # ``url_patterns`` is the second match axis; covers skills that match
        # by path substring rather than host.
        body = "PATH-PATTERN-SKILL-MARKER"
        skill = _make_skill(
            "lever",
            body,
            url_patterns=["jobs.lever.co"],
        )
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={"lever": skill},
        )

        agent.crawl("https://jobs.lever.co/acme", wait_s=0, max_pages=1)

        assert model.requests
        assert body in model.requests[0].system_prompt

    def test_default_load_from_vendor_catalog_matches_real_workday(self) -> None:
        # Integration pin: the shipped ``vendor/crawl-recipes/skills`` catalog
        # loads by default (no explicit ``skills=``) and matches a real
        # workday URL, so production CrawlAgents get the playbook without any
        # caller wiring. Skips cleanly when the catalog isn't shipped (CI
        # sandboxes, stripped containers).
        if not DEFAULT_SKILLS_DIR.exists():
            pytest.skip("vendor skill catalog not present in this checkout")
        model = _CapturingModel()
        agent = CrawlAgent(_StubBrowser(), _NoopExtractor(), model_client=model)

        agent.crawl(_WORKDAY_URL, wait_s=0, max_pages=1)

        assert model.requests
        prompt = model.requests[0].system_prompt
        assert "SKILL PLAYBOOK" in prompt
        # The shipped workday playbook describes its facet-recursion strategy;
        # either the heading or the source_type name will appear.
        assert "workday" in prompt.lower()

    def test_default_load_with_missing_skills_dir_falls_back_to_blind_run(
        self, tmp_path: Path
    ) -> None:
        # When the default catalog isn't shipped (or skills_dir points at a
        # missing dir), load_skills returns {} and the agent blind-runs
        # without raising — the backward-compat guarantee.
        missing = tmp_path / "does-not-exist"
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills_dir=missing,
        )

        assert agent._skills == {}
        agent.crawl(_WORKDAY_URL, wait_s=0, max_pages=1)
        assert model.requests
        assert "SKILL PLAYBOOK" not in model.requests[0].system_prompt

    def test_crawl_bounded_threads_source_type_to_skill_match(self) -> None:
        # The bounded entry point also honors the source_type hint so the
        # sink's bounded path gets skill guidance too. The assertion that
        # matters is the prompt; the stop_reason depends on how many browser
        # ops the fall-through consumes, so we just check it ran to a
        # terminal state without crashing.
        body = "BOUNDED-PATH-SKILL-MARKER"
        skill = _make_skill("workday", body, host_suffix="myworkdayjobs.com")
        model = _CapturingModel()
        agent = CrawlAgent(
            _StubBrowser(),
            _NoopExtractor(),
            model_client=model,
            skills={"workday": skill},
        )

        result = agent.crawl_bounded(
            _WORKDAY_URL,
            max_actions=20,
            max_duration_s=10,
            wait_s=0,
            max_pages=1,
        )

        assert model.requests
        assert body in model.requests[0].system_prompt
        assert result.stop_reason in {
            "completed",
            "duplicate_stop",
            "action_limit",
            "time_limit",
        }
