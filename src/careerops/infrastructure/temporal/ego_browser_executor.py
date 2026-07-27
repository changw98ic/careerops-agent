"""Bounded, read-only bridge from crawl activities to ego-browser.

The browser is an explicit source executor, not an implicit HTTP fallback.
It returns the same minimized :class:`FetchedResponse` contract as the HTTP
fetcher and never writes page data, cookies, screenshots, or session material
to disk. A missing browser binary is a dependency error that the crawl layer
records as a failed source.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # nosec B404 -- fixed argv, no shell execution
from datetime import UTC, datetime
from typing import Final, cast
from urllib.parse import urlsplit

from careerops.adapters.http_fetcher import FetchedResponse, SSRFError, validate_url

__all__ = [
    "BrowserDependencyError",
    "BrowserPolicyError",
    "EgoBrowserExecutor",
]

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_MAX_BODY_BYTES: Final[int] = 1_000_000
_RESULT_MARKER: Final[str] = "CAREEROPS_EGO_RESULT:"


class BrowserDependencyError(RuntimeError):
    """The explicitly requested browser executor cannot run."""


class BrowserPolicyError(RuntimeError):
    """The requested browser URL fails the crawl SSRF policy."""


def _bounded_url(url: str) -> str:
    try:
        validate_url(url)
    except SSRFError as exc:
        raise BrowserPolicyError("browser URL rejected by SSRF policy") from exc
    parts = urlsplit(url)
    if parts.username or parts.password or parts.fragment:
        raise BrowserPolicyError("browser URL contains disallowed credentials or fragment")
    return url


class EgoBrowserExecutor:
    """Execute one read-only page capture in an isolated ego task space."""

    def __init__(
        self,
        *,
        binary: str = "ego-browser",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_body_bytes: int = _MAX_BODY_BYTES,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("browser timeout must be between 0 and 120 seconds")
        if max_body_bytes < 1 or max_body_bytes > 10_000_000:
            raise ValueError("browser response cap is outside the safe range")
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._max_body_bytes = max_body_bytes

    @property
    def is_ready(self) -> bool:
        return shutil.which(self._binary) is not None

    def fetch(self, url: str) -> FetchedResponse:
        target = _bounded_url(url)
        if not self.is_ready:
            raise BrowserDependencyError("ego-browser dependency is unavailable")

        # JSON-encoding the URL keeps page content out of the generated JS
        # source and prevents a URL from becoming executable browser code.
        encoded_url = json.dumps(target, ensure_ascii=True)
        encoded_cap = json.dumps(self._max_body_bytes)
        script = f"""
const target = {encoded_url};
const cap = {encoded_cap};
const task = await useOrCreateTaskSpace('careerops-crawl');
await openOrReuseTab(target, {{ wait: true, timeout: {int(self._timeout_seconds)} }});
await wait(1);
const info = await pageInfo();
const finalUrl = typeof info.url === 'string' ? info.url : target;
const html = await js(String.raw`(() => document.documentElement.outerHTML)()`);
if (typeof html !== 'string') throw new Error('browser page body was not text');
if (html.length > cap) throw new Error('browser page body exceeds response cap');
cliLog('{_RESULT_MARKER}' + JSON.stringify({{ finalUrl, html }}));
"""
        try:
            completed = subprocess.run(  # nosec B603 -- fixed argv and shell disabled
                [self._binary, "nodejs", "-e", script],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserDependencyError("ego-browser timed out") from exc
        except OSError as exc:
            raise BrowserDependencyError("ego-browser could not be started") from exc

        if completed.returncode != 0:
            raise BrowserDependencyError("ego-browser returned a non-zero status")
        payload = _extract_payload(completed.stdout)
        final_url = payload.get("finalUrl")
        body = payload.get("html")
        if not isinstance(final_url, str) or not isinstance(body, str):
            raise BrowserDependencyError("ego-browser returned an invalid capture")
        # A redirect is checked after rendering as a second policy boundary;
        # it is never silently treated as an HTTP request.
        _bounded_url(final_url)
        if len(body.encode("utf-8")) > self._max_body_bytes:
            raise BrowserDependencyError("ego-browser response exceeds response cap")
        return FetchedResponse(
            status_code=200,
            final_url=final_url,
            fetched_at=datetime.now(UTC),
            response_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            body=body,
        )


def _extract_payload(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(_RESULT_MARKER):
            continue
        try:
            value: object = json.loads(line[len(_RESULT_MARKER) :])
        except json.JSONDecodeError as exc:
            raise BrowserDependencyError("ego-browser returned malformed capture JSON") from exc
        if isinstance(value, dict):
            typed = cast(dict[object, object], value)
            return {str(key): item for key, item in typed.items()}
    raise BrowserDependencyError("ego-browser returned no capture")
