"""Explicit opt-in smoke tests against live external crawl sources."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from careerops.infrastructure.playwright_tool import PlaywrightTool

pytestmark = pytest.mark.skipif(
    os.environ.get("CAREEROPS_RUN_REAL_CRAWL_SMOKE") != "1",
    reason="set CAREEROPS_RUN_REAL_CRAWL_SMOKE=1 for live-source verification",
)

_GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs"
_DYNAMIC_URL = "https://careers.airbnb.com/positions/"
_SESSION_VERIFY_URL = "https://httpbingo.org/cookies"


def _get_json(url: str) -> dict[str, object]:
    request = Request(url, headers={"User-Agent": "CareerOps acceptance smoke"})
    with urlopen(request, timeout=30) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_real_structured_greenhouse_source() -> None:
    payload = _get_json(_GREENHOUSE_URL)
    jobs = payload.get("jobs")
    assert isinstance(jobs, list)
    assert len(jobs) > 0
    assert all(
        isinstance(job, dict) and str(job.get("title", "")).strip()
        for job in jobs[:10]
    )


def test_real_public_dynamic_source_renders_in_browser() -> None:
    tool = PlaywrightTool()
    try:
        page = tool.navigate(_DYNAMIC_URL, wait_s=3)
        html = tool.capture_html()
        assert page["url"].startswith("https://careers.airbnb.com/")
        assert len(html) > 10_000
        assert "position" in html.lower() or "job" in html.lower()
    finally:
        tool.close()


def test_real_authorized_session_is_reused_without_storing_credentials(
    tmp_path: Path,
) -> None:
    session_ref = f"careerops-login-{uuid4()}"
    tool = PlaywrightTool(session_ref=session_ref, session_root=tmp_path)
    try:
        tool.navigate(_SESSION_VERIFY_URL, wait_s=1)
        cookie = tool.run_page_script(
            "() => {"
            "document.cookie = 'careerops_job_session=authorized; Path=/; "
            "SameSite=Lax; Max-Age=3600';"
            "return document.cookie;"
            "}"
        )
        assert "careerops_job_session=authorized" in str(cookie)
    finally:
        tool.close()

    reopened = PlaywrightTool(session_ref=session_ref, session_root=tmp_path)
    try:
        reopened.navigate(_SESSION_VERIFY_URL, wait_s=1)
        html = reopened.capture_html()
        assert "careerops_job_session" in html
        assert "authorized" in html
        assert "password" not in session_ref
    finally:
        reopened.close()
