"""FastAPI dependency for API route authentication.

Two dependencies are provided:

* ``require_api_auth`` - reads the ``careerops_session`` cookie **and** the
  ``X-CSRF-Token`` header, validates both through
  ``ConsoleAuthService.authenticate`` and ``validate_csrf``, and injects the
  resulting ``AuthenticatedPrincipal``.  Use for state-changing API routers
  (POST / PUT / DELETE).

* ``require_web_auth`` - reads only the ``careerops_session`` cookie, validates
  it, and returns the principal.  No CSRF header check.  Use for UI routers
  that serve GET page loads where the browser does not send custom headers.

Routes that must remain public (health, metrics) simply do not include either
dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyCookie

from careerops.auth.contracts import AuthenticatedPrincipal, AuthError

_COOKIE_NAME = "careerops_session"
_csrf_header = "x-csrf-token"

_COOKIE_SCHEME = APIKeyCookie(name=_COOKIE_NAME, auto_error=False)


async def require_api_auth(
    request: Request,
    session_token: str | None = Depends(_COOKIE_SCHEME),
) -> AuthenticatedPrincipal | None:
    """Reject unauthenticated API requests with 401.

    Reads auth service and web settings from ``app.state`` (installed by
    ``create_app``).  When auth is not configured at all (``auth_service``
    and ``web_settings`` are both ``None``) the request is allowed through
    — this supports test / dev deployments that do not set up auth.  When
    auth IS configured, missing or invalid credentials are rejected.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    web_settings = getattr(request.app.state, "web_settings", None)

    # Auth not configured — allow through for backward-compatible deployments.
    # When auth IS configured, both pieces must be present.
    if auth_service is None or web_settings is None:
        if auth_service is None and web_settings is None:
            return None
        raise HTTPException(status_code=401, detail="authentication required")

    if not session_token:
        raise HTTPException(status_code=401, detail="authentication required")

    try:
        principal = auth_service.authenticate(session_token, now=_now())
    except (AuthError, ValueError):
        raise HTTPException(status_code=401, detail="authentication required") from None

    csrf_token = request.headers.get(_csrf_header, "")
    try:
        auth_service.validate_csrf(principal, csrf_token)
    except (AuthError, ValueError):
        raise HTTPException(status_code=403, detail="csrf token rejected") from None

    return principal


async def require_web_auth(
    request: Request,
    session_token: str | None = Depends(_COOKIE_SCHEME),
) -> AuthenticatedPrincipal | None:
    """Reject unauthenticated web requests with 401.

    Like ``require_api_auth`` but **without** CSRF header validation.
    Intended for UI routers that serve GET page loads — the browser sends the
    session cookie but does not attach custom headers on navigation.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    web_settings = getattr(request.app.state, "web_settings", None)

    # Auth not configured — allow through for backward-compatible deployments.
    if auth_service is None or web_settings is None:
        if auth_service is None and web_settings is None:
            return None
        raise HTTPException(status_code=401, detail="authentication required")

    if not session_token:
        raise HTTPException(status_code=401, detail="authentication required")

    try:
        return auth_service.authenticate(session_token, now=_now())
    except (AuthError, ValueError):
        raise HTTPException(status_code=401, detail="authentication required") from None


def _now() -> datetime:
    return datetime.now(tz=UTC)
