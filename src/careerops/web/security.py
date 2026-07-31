from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CSP = (
    "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'"
)


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
            raise RequestOriginRejected("request Host is not allowed")

    def validate_mutation(self, request: Request) -> None:
        self.validate_host(request)
        origin = request.headers.get("origin", "").rstrip("/")
        if origin not in self._settings.allowed_origins:
            raise RequestOriginRejected("request Origin is missing or not allowed")


class ConsoleSecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
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
