"""Ego-as-tool: programmatic ego-browser control for the crawl agent.

The CareerOps agent drives a real browser through this tool instead of a human
or a one-off script doing it manually. It wraps ``ego-browser nodejs -e`` so the
agent can: navigate, run page JavaScript, capture rendered HTML, capture the
page's network/API calls (packet capture), and replay/call an API from inside
the browser session (cookies included).

Design:
- Every operation runs as an ego nodejs script in a persistent task space; the
  browser tab persists across calls.
- Variable parts (URLs, page scripts, request params) are injected via
  :func:`json.dumps` so no manual quote-escaping is needed.
- Each script returns its result through a ``cliLog('<marker>' + json)`` line
  that this tool parses off stdout (handles large payloads, e.g. full-page HTML).

This is the canonical Tier 2 browser tool used by ``CrawlAgent`` (the multi-step
ego path). The single-shot ``EgoBrowserExecutor`` remains for structured Tier 1
captures; both satisfy the :class:`BrowserTool` protocol so the agent can also be
driven by a fake in tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- fixed argv, no shell
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

_MARKER = "EGORESULT:"
_DEFAULT_TIMEOUT = 120.0
# Large fetch response bodies go through a file (not cliLog) so they are not
# truncated/mangled on the marker line.
_FETCH_BODY_FILE = "/tmp/ego_fetch_body.txt"
_RESTORE_SCRIPT = (
    "(() => { try { "
    "if (window.__egoOrigFetch) window.fetch = window.__egoOrigFetch; "
    "if (window.__egoOrigXhrOpen) "
    "XMLHttpRequest.prototype.open = window.__egoOrigXhrOpen; "
    "if (window.__egoOrigXhrSend) "
    "XMLHttpRequest.prototype.send = window.__egoOrigXhrSend; "
    "} catch (e) {} return 'restored'; })()"
)


@dataclass(frozen=True, slots=True)
class CapturedApiCall:
    """One network call captured from the page (packet capture)."""

    url: str
    method: str
    body: str | None
    response: str


@dataclass(frozen=True, slots=True)
class PageFetchResult:
    """Result of an in-browser fetch (replay/API call)."""

    status: int
    body: str


class BrowserTool(Protocol):
    """Protocol that both EgoBrowserTool and PlaywrightTool satisfy.

    Lets CrawlAgent use either tool interchangeably (and a fake in tests).
    """

    @property
    def is_ready(self) -> bool: ...

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]: ...

    def capture_html(self) -> str: ...

    def run_page_script(self, script: str) -> Any: ...

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]: ...

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult: ...


class EgoBrowserTool:
    """Drive ego-browser from Python so the agent can crawl autonomously."""

    def __init__(
        self,
        *,
        binary: str = "ego-browser",
        task_space: str = "careerops-crawl",
        timeout_s: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if timeout_s <= 0 or timeout_s > 600:
            raise ValueError("timeout_s must be between 0 and 600 seconds")
        self._binary = binary
        self._task_space = task_space
        self._timeout_s = timeout_s

    @property
    def is_ready(self) -> bool:
        return shutil.which(self._binary) is not None

    # -- internals --------------------------------------------------------

    def _run(self, script_body: str) -> str:
        if not self.is_ready:
            raise RuntimeError("ego-browser binary is not available")
        wrapped = f"await useOrCreateTaskSpace({json.dumps(self._task_space)});\n" + script_body
        proc = subprocess.run(  # nosec B603 -- fixed argv, shell disabled
            [self._binary, "nodejs", "-e", wrapped],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=self._timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("ego-browser nodejs failed: " + (proc.stdout or "")[-400:])
        return proc.stdout

    def _run_result(self, script_body: str) -> Any:
        stdout = self._run(script_body)
        for line in reversed(stdout.splitlines()):
            if line.startswith(_MARKER):
                payload = line[len(_MARKER) :]
                return json.loads(payload)
        raise RuntimeError("no result marker in ego output: " + stdout[-300:])

    @staticmethod
    def _emit(value_expr: str) -> str:
        """Emit a result: ``value_expr`` is a JS expression evaluated in node."""
        return f"cliLog('{_MARKER}' + JSON.stringify({value_expr}));\n"

    # -- operations -------------------------------------------------------

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        """Open/reuse a tab at ``url`` and wait for it to render."""
        script = (
            f"await openOrReuseTab({json.dumps(url)});\n"
            f"await wait({max(1, int(wait_s))});\n"
            "const _i = await pageInfo();\n"
            + self._emit("{ url: String((_i&&_i.url)||''), title: String((_i&&_i.title)||'') }")
        )
        return cast("dict[str, str]", self._run_result(script))

    def capture_html(self) -> str:
        """Return the rendered ``outerHTML`` of the current page."""
        script = (
            "const _h = await js('(() => document.documentElement.outerHTML)()');\n"
            + self._emit("_h")
        )
        return cast(str, self._run_result(script))

    def run_page_script(self, script: str) -> Any:
        """Run arbitrary JavaScript in the page and return its value.

        The script must evaluate to a JSON-serializable value (wrap in an IIFE
        that returns). This is the flexible primitive the LLM agent uses to
        explore/interact with a page.
        """
        node_script = f"const _r = await js({json.dumps(script)});\n" + self._emit("_r")
        return self._run_result(node_script)

    def capture_network(
        self,
        *,
        url_contains: str,
        trigger_script: str,
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        """Install a fetch/XHR hook, run ``trigger_script``, return captured calls.

        ``url_contains`` is a plain substring matched against request URLs (e.g.
        ``"position/search"``). ``trigger_script`` is page JS that fires the
        request(s) to capture (e.g. filling + submitting a search box).
        """
        hook = self._hook_script(url_contains)
        script = (
            f"await js({json.dumps(hook)});\n"
            f"await js({json.dumps(trigger_script)});\n"
            f"await wait({max(1, int(wait_s))});\n"
            "const _c = await js('(() => JSON.stringify(window.__caps || []))()');\n"
            # Restore the original fetch/XHR so later fetch_in_page calls are
            # not intercepted (the hook would otherwise truncate their result).
            f"await js({json.dumps(_RESTORE_SCRIPT)});\n"
            "cliLog('" + _MARKER + "' + String(_c));\n"
        )
        # _run_result already parses the marker payload (the captured array as
        # JSON) into a list[object]; do not json.loads it a second time.
        items = cast("list[object]", self._run_result(script))
        calls: list[CapturedApiCall] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            it = cast("dict[str, object]", raw_item)
            body_val = it.get("body")
            calls.append(
                CapturedApiCall(
                    url=str(it.get("url", "")),
                    method=str(it.get("method", "GET")),
                    body=(str(body_val) if body_val is not None else None),
                    response=str(it.get("response", "")),
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
        """Call ``url`` from inside the browser (session cookies included).

        Used to replay a captured job-list API across pages for bulk extraction.
        """
        fetch_js = self._fetch_script(url, method=method, body=body, headers=headers)
        # Write the body to a file via node fs; return only the (small) status
        # through cliLog. This keeps large/awkward response bodies intact.
        script = (
            f"const _f = await js({json.dumps(fetch_js)});\n"
            "const _fs = await import('node:fs');\n"
            f"_fs.writeFileSync({json.dumps(_FETCH_BODY_FILE)}, String(_f.body));\n"
            + self._emit("_f.status")
        )
        status_val = self._run_result(script)
        status = int(status_val) if isinstance(status_val, (int, float)) else 0
        try:
            body_text = Path(_FETCH_BODY_FILE).read_text(encoding="utf-8")
        except OSError:
            body_text = ""
        return PageFetchResult(status=status, body=body_text)

    # -- script builders --------------------------------------------------

    @staticmethod
    def _hook_script(url_contains: str) -> str:
        sub = json.dumps(url_contains)
        return (
            "window.__caps = [];\n"
            "(function () {\n"
            "  var SUB = " + sub + ";\n"
            "  function rec(u, method, reqBody, respText) {\n"
            "    try { window.__caps.push({ url: String(u).slice(0, 500),\n"
            "      method: String(method || 'GET'),\n"
            "      body: reqBody != null ? String(reqBody).slice(0, 1500) : null,\n"
            "      response: String(respText || '').slice(0, 6000) }); } catch (e) {}\n"
            "  }\n"
            "  window.__egoOrigFetch = window.fetch;\n"
            "  window.__egoOrigXhrOpen = XMLHttpRequest.prototype.open;\n"
            "  window.__egoOrigXhrSend = XMLHttpRequest.prototype.send;\n"
            "  var of = window.fetch;\n"
            "  window.fetch = async function () {\n"
            "    var a = arguments; var r = await of.apply(this, a);\n"
            "    try { var u = (a[0] && (a[0].url || a[0])) || '';\n"
            "      if (String(u).indexOf(SUB) !== -1) { var c = r.clone();\n"
            "        rec(u, (a[1] && a[1].method) || 'GET', a[1] && a[1].body, "
            "await c.text()); } }\n"
            "      catch (e) {}\n"
            "    return r;\n"
            "  };\n"
            "  var oo = XMLHttpRequest.prototype.open;\n"
            "  XMLHttpRequest.prototype.open = function (m, u) {\n"
            "    this.__m = m; this.__u = u; return oo.apply(this, arguments);\n"
            "  };\n"
            "  var os = XMLHttpRequest.prototype.send;\n"
            "  XMLHttpRequest.prototype.send = function (b) {\n"
            "    var self = this;\n"
            "    this.addEventListener('load', function () {\n"
            "      try { var u = String(self.__u || '');\n"
            "        if (u.indexOf(SUB) !== -1) { rec(u, self.__m, b, self.responseText); } }\n"
            "        catch (e) {}\n"
            "    });\n"
            "    return os.apply(this, [b]);\n"
            "  };\n"
            "})();\n"
            "'installed';"
        )

    @staticmethod
    def _fetch_script(
        url: str,
        *,
        method: str,
        body: str | None,
        headers: dict[str, str] | None,
    ) -> str:
        url_js = json.dumps(url)
        method_js = json.dumps(method.upper())
        body_js = "null" if body is None else json.dumps(body)
        headers_js = json.dumps(headers or {})
        return (
            "(async function () {\n"
            "  try {\n"
            "    var r = await fetch(" + url_js + ", {\n"
            "      method: " + method_js + ",\n"
            "      headers: " + headers_js + ",\n"
            "      body: " + body_js + ",\n"
            "      credentials: 'include'\n"
            "    });\n"
            "    var t = await r.text();\n"
            "    return { status: r.status, body: t.slice(0, 200000) };\n"
            "  } catch (e) {\n"
            "    return { status: 0, body: 'ERR:' + (e && e.message ? e.message : String(e)) };\n"
            "  }\n"
            "})();"
        )
