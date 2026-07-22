from __future__ import annotations

import logging
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CSP = (
    "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; script-src 'none'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'"
)

_LOGGER = logging.getLogger(__name__)


def _origin_rejection_profile(origin: str, allowed_origins: frozenset[str]) -> str:
    """Return a bounded diagnostic category without retaining attacker-controlled input."""
    if origin == "null":
        return "opaque"
    try:
        parsed = urlsplit(origin)
        parsed_port = parsed.port
        allowed = tuple(urlsplit(item) for item in allowed_origins)
        allowed_keys = tuple(
            (item.scheme.lower(), item.hostname.lower(), item.port)
            for item in allowed
            if item.hostname is not None
        )
    except ValueError:
        return "malformed"
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return "non_http"
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return "unexpected_components"
    authority = (parsed.hostname.lower(), parsed_port)
    allowed_authorities = tuple(item[1:] for item in allowed_keys)
    origin_key = (parsed.scheme.lower(), *authority)
    if origin_key in allowed_keys:
        return "canonicalization_mismatch"
    if authority in allowed_authorities:
        return "scheme_mismatch"
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return "unconfigured_loopback"
    try:
        if ip_address(hostname).is_loopback:
            return "unconfigured_loopback"
    except ValueError:
        pass
    return "non_loopback"


class RequestOriginRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConsoleWebSettings:
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    session_cookie_name: str = "careerops_session"
    csrf_cookie_name: str = "careerops_csrf"
    cookie_secure: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_hosts or not self.allowed_origins:
            raise ValueError("console host and origin allowlists must be explicit")
        if any(not self._valid_host(host) for host in self.allowed_hosts):
            raise ValueError("console host allowlist contains an invalid host")
        if any(not self._valid_origin(origin) for origin in self.allowed_origins):
            raise ValueError("console origin allowlist contains an invalid origin")
        if self.cookie_secure and any(
            urlsplit(origin).scheme != "https" for origin in self.allowed_origins
        ):
            raise ValueError("Secure console cookies require HTTPS origins")
        for name in (self.session_cookie_name, self.csrf_cookie_name):
            if not name or any(character in name for character in " ;,\r\n\t"):
                raise ValueError("console cookie names must be bounded tokens")

    @staticmethod
    def _valid_host(host: str) -> bool:
        return bool(host) and host == host.lower() and "/" not in host and "@" not in host

    @staticmethod
    def _valid_origin(origin: str) -> bool:
        parsed = urlsplit(origin)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
            and origin == origin.rstrip("/")
        )


class OriginHostValidator:
    def __init__(self, settings: ConsoleWebSettings) -> None:
        self._settings = settings

    def validate_host(self, request: Request) -> None:
        host = request.headers.get("host", "").lower()
        if host not in self._settings.allowed_hosts:
            _LOGGER.warning("console request origin rejected reason=host_not_allowed")
            raise RequestOriginRejected("request Host is not allowed")

    def validate_mutation(self, request: Request) -> None:
        self.validate_host(request)
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in self._settings.allowed_origins:
            reason = "origin_missing" if not origin else "origin_not_allowed"
            profile = (
                "missing"
                if not origin
                else _origin_rejection_profile(origin, self._settings.allowed_origins)
            )
            _LOGGER.warning(
                "console request origin rejected reason=%s profile=%s",
                reason,
                profile,
            )
            raise RequestOriginRejected("request Origin is missing or not allowed")


class ConsoleSecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Fetch serializes the Origin of a non-CORS form POST as `null` under
        # `no-referrer`. `same-origin` preserves the exact same-origin value
        # required by the CSRF gate while still suppressing cross-origin Referer.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response


__all__ = [
    "CSP",
    "ConsoleSecurityHeadersMiddleware",
    "ConsoleWebSettings",
    "OriginHostValidator",
    "RequestOriginRejected",
]
