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

Server-side candidate ownership (end-to-end-career-application-loop Iron Rule
2): ``require_candidate_id`` resolves the active candidate from the
authenticated principal's ``candidate_id`` (populated from
``console_users.candidate_id`` via the session). Client-supplied
``candidate_id`` values in query or body parameters are NEVER honored for
ownership scoping — Section-3+ routes receive the server-resolved id through
this dependency and MUST ignore any client-provided substitute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie

from careerops.api.errors import (
    CandidateProfileRequiredError,
    CSRFRejectedError,
    UnauthorizedError,
)
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
        raise UnauthorizedError()

    if not session_token:
        raise UnauthorizedError()

    try:
        principal = auth_service.authenticate(session_token, now=_now())
    except (AuthError, ValueError):
        raise UnauthorizedError() from None

    # Per spec §4: GET/HEAD only check session cookie; mutating methods also check CSRF.
    if request.method not in ("GET", "HEAD"):
        csrf_token = request.headers.get(_csrf_header, "")
        try:
            auth_service.validate_csrf(principal, csrf_token)
        except (AuthError, ValueError):
            raise CSRFRejectedError() from None

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
        raise UnauthorizedError()

    if not session_token:
        raise UnauthorizedError()

    try:
        return auth_service.authenticate(session_token, now=_now())
    except (AuthError, ValueError):
        raise UnauthorizedError() from None


async def require_candidate_id(
    principal: Annotated[AuthenticatedPrincipal | None, Depends(require_api_auth)],
) -> UUID:
    """Resolve the active candidate_id SERVER-SIDE from the session (Iron Rule 2).

    The candidate is read from the authenticated principal, which carries the
    ``candidate_id`` linked to the console user at bootstrap/login time
    (``console_users.candidate_id``). A request with no authenticated session,
    or a session whose user has no linked candidate, raises
    :class:`CandidateProfileRequiredError` (HTTP 403).

    Section-3+ routes consume the returned id via ``Depends(require_candidate_id)``
    and MUST NOT read a ``candidate_id`` from the request body or query string
    for ownership scoping. Any client-supplied candidate value is by definition
    a substitution attempt and is rejected by simply not being read. Use
    :func:`reject_candidate_substitution` for routes that historically accepted
    a candidate_id field and need to assert it matches the server-resolved id.
    """
    if principal is None or principal.candidate_id is None:
        raise CandidateProfileRequiredError()
    return principal.candidate_id


def reject_candidate_substitution(*, provided: object, resolved: UUID) -> None:
    """Defense-in-depth guard for routes that still receive a client candidate_id.

    Routes that accept a candidate-shaped field in their body/query (e.g. legacy
    M3 endpoints kept for backward compatibility) MUST call this with the
    client value and the server-resolved id. A non-empty client value that
    differs from the server-resolved id raises
    :class:`CandidateProfileRequiredError` — the request is treated as an
    ownership-substitution attempt regardless of whether the target row exists.

    An empty/``None`` client value is allowed: "not supplied" is not a
    substitution. The server-resolved id always wins.
    """
    if provided is None:
        return
    if isinstance(provided, str):
        if not provided:
            return
        try:
            provided_id = UUID(provided)
        except ValueError:
            raise CandidateProfileRequiredError("candidate_id is not a valid uuid") from None
    elif isinstance(provided, UUID):
        provided_id = provided
    else:
        raise CandidateProfileRequiredError("candidate_id has unsupported type")
    if provided_id != resolved:
        raise CandidateProfileRequiredError(
            "client-supplied candidate_id does not match the authenticated user"
        )


def _now() -> datetime:
    return datetime.now(tz=UTC)
