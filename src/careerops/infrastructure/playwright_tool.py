"""Playwright-backed browser tool with CDP-level network capture.

Unlike EgoBrowserTool (which installs a JS hook AFTER page load, missing
initial-load APIs), PlaywrightTool registers a response listener BEFORE
navigation — it captures EVERY HTTP response the browser makes, including
the SPA's initial data-fetch calls.

This mirrors EgoBrowserTool's interface so CrawlAgent can swap seamlessly:
```
tool = PlaywrightTool()   # instead of EgoBrowserTool()
agent = CrawlAgent(tool, extractor)
```
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from playwright.sync_api import Browser, BrowserContext, Page, Response, sync_playwright

from careerops.infrastructure.ego_tool import CapturedApiCall, PageFetchResult

_API_KEYWORDS = ("job", "position", "search", "list", "api", "career", "posting")


class _PlaywrightDriver:
    """Drive Chromium via Playwright with full CDP network capture.

    The response listener is registered at init (before any navigation), so
    ``navigate()`` captures ALL responses the page makes — including the
    SPA's initial-load API calls that ego's JS hook misses.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        session_ref: str | None = None,
        session_root: Path | None = None,
    ) -> None:
        self._headless = headless
        configured_root = os.environ.get("CAREEROPS_BROWSER_SESSION_ROOT")
        self._session_root = session_root or Path(
            configured_root or ".careerops/browser-sessions"
        )
        self._session_ref: str | None = None
        self._pw = sync_playwright().start()
        self._browser: Browser | None = None
        self._context: BrowserContext
        self._page: Page
        self._responses: list[Response] = []
        self._open_context(session_ref)

    def _open_context(self, session_ref: str | None) -> None:
        if session_ref is not None:
            if not re.fullmatch(r"careerops-login-[0-9a-f-]{36}", session_ref):
                raise ValueError("invalid crawl session reference")
            self._session_root.mkdir(parents=True, exist_ok=True)
            profile = self._session_root / session_ref
            self._context = self._pw.chromium.launch_persistent_context(
                user_data_dir=profile,
                headless=self._headless,
            )
            self._session_ref = session_ref
        else:
            self._browser = self._pw.chromium.launch(headless=self._headless)
            self._context = self._browser.new_context()
            self._session_ref = None
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.on("response", self._on_response)
        # Anti-bot: hide automation flags before ANY page JS runs.
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        )

    def bind_session(self, session_ref: str | None) -> None:
        """Switch to an isolated source session, or a clean public context."""
        if session_ref == self._session_ref:
            return
        self._context.close()
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._responses.clear()
        self._open_context(session_ref)

    @property
    def is_ready(self) -> bool:
        return True

    def _on_response(self, resp: Response) -> None:
        self._responses.append(resp)

    # -- operations (mirror EgoBrowserTool) -------------------------------

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        """Navigate to ``url``. ALL responses (including initial-load) captured."""
        self._responses.clear()
        with suppress(Exception):
            self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(max(wait_s, 5) * 1000),
            )
        self._page.wait_for_timeout(int(wait_s * 1000))
        return {
            "url": self._page.url,
            "title": self._page.title(),
        }

    def capture_html(self) -> str:
        return self._page.content()

    def run_page_script(self, script: str) -> Any:
        return self._page.evaluate(script)

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        """Return captured API calls — INCLUDING initial-load (unlike ego).

        ``trigger_script`` is optional: initial-load APIs are already captured
        during ``navigate()``. The trigger fires additional interaction APIs.
        """
        if trigger_script:
            with suppress(Exception):
                self._page.evaluate(trigger_script)
        self._page.wait_for_timeout(int(wait_s * 1000))

        calls: list[CapturedApiCall] = []
        for resp in self._responses:
            url = resp.url
            if url_contains and url_contains not in url:
                continue
            ct = resp.headers.get("content-type", "")
            if (
                "json" not in ct
                and not any(k in url.lower() for k in _API_KEYWORDS)
            ):
                continue
            try:
                body = resp.text()
            except Exception:
                continue
            req = resp.request
            calls.append(
                CapturedApiCall(
                    url=url,
                    method=req.method,
                    body=req.post_data,
                    response=body[:20000],
                )
            )
        return calls

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        """Call ``url`` from inside the browser (session cookies included)."""
        js = (
            "(async () => {"
            f"  const r = await fetch({json.dumps(url)}, {{"
            f"    method: {json.dumps(method.upper())},"
            f"    headers: {json.dumps(headers or {})},"
            f"    body: {json.dumps(body) if body else 'null'},"
            "    credentials: 'include'"
            "  });"
            "  const t = await r.text();"
            "  return { status: r.status, body: t.slice(0, 200000) };"
            "})()"
        )
        result = cast("dict[str, object]", self._page.evaluate(js))
        status_raw = result.get("status", 0)
        status = int(status_raw) if isinstance(status_raw, (int, float)) else 0
        return PageFetchResult(status=status, body=str(result.get("body", "")))

    def close(self) -> None:
        """Shut down the browser."""
        try:
            self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            self._pw.stop()


class PlaywrightTool:
    """Thread-affine proxy around Playwright's synchronous API.

    Playwright sync objects must stay on the thread that created them. API and
    Temporal callers can run inside asyncio loops or different worker threads,
    so every operation is dispatched to one private executor thread.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        session_ref: str | None = None,
        session_root: Path | None = None,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="careerops-playwright",
        )
        self._closed = False
        try:
            self._driver = self._executor.submit(
                _PlaywrightDriver,
                headless=headless,
                session_ref=session_ref,
                session_root=session_root,
            ).result()
        except Exception:
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise

    @property
    def is_ready(self) -> bool:
        return not self._closed

    def bind_session(self, session_ref: str | None) -> None:
        self._executor.submit(self._driver.bind_session, session_ref).result()

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        return self._executor.submit(
            self._driver.navigate,
            url,
            wait_s=wait_s,
        ).result()

    def capture_html(self) -> str:
        return self._executor.submit(self._driver.capture_html).result()

    def run_page_script(self, script: str) -> Any:
        return self._executor.submit(self._driver.run_page_script, script).result()

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        return self._executor.submit(
            self._driver.capture_network,
            url_contains=url_contains,
            trigger_script=trigger_script,
            wait_s=wait_s,
        ).result()

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        return self._executor.submit(
            self._driver.fetch_in_page,
            url,
            method=method,
            body=body,
            headers=headers,
        ).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.submit(self._driver.close).result()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)
