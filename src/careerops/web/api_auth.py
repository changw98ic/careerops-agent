"""REST API auth endpoints for the Vue SPA."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from careerops.auth.contracts import (
    AuthError,
    AuthRateLimited,
    AuthRequestContext,
    InvalidBootstrapCredential,
    InvalidCredentials,
    InvalidSession,
    SingleUserAlreadyExists,
)

router = APIRouter(tags=["auth-api"])


class SessionUser(BaseModel):
    username: str
    candidate_id: str = ""


class MeResponse(BaseModel):
    user_id: str
    username: str
    candidate_id: str = ""


class SessionResponse(BaseModel):
    authenticated: bool
    user: SessionUser | None = None
    csrf_token: str | None = None
    expires_at: str | None = None
    trace_id: str


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class LoginResponse(BaseModel):
    ok: bool
    authenticated: bool = False
    user: SessionUser | None = None
    csrf_token: str | None = None
    expires_at: str | None = None
    trace_id: str
    error: str | None = None
    code: str | None = None


class BootstrapRequest(BaseModel):
    bootstrap_token: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class BootstrapResponse(BaseModel):
    ok: bool
    authenticated: bool = False
    user: SessionUser | None = None
    csrf_token: str | None = None
    expires_at: str | None = None
    trace_id: str
    error: str | None = None
    code: str | None = None


class LogoutResponse(BaseModel):
    ok: bool
    trace_id: str


class PreauthResponse(BaseModel):
    ok: bool
    csrf_token: str = ""
    trace_id: str
    error: str | None = None


@router.post("/api/v1/auth/preauth", response_model=PreauthResponse)
async def preauth(request: Request) -> JSONResponse:
    """Issue a short-lived pre-authentication session.

    The SPA must call this before ``/login`` or ``/bootstrap`` so that the
    backend can set the ``careerops_session`` and ``careerops_csrf`` cookies
    that those endpoints expect.
    """
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or uuid4().hex

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        return JSONResponse(
            status_code=503,
            content=PreauthResponse(
                ok=False, trace_id=trace_id, error="auth not configured"
            ).model_dump(),
        )

    client_host = request.client.host if request.client is not None else "unknown"
    now = datetime.now(UTC)
    context = AuthRequestContext(trace_id=trace_id, client_key=client_host)

    try:
        secrets = auth_service.begin_preauth(now=now, context=context)
    except AuthRateLimited:
        return JSONResponse(
            status_code=429,
            content=PreauthResponse(ok=False, trace_id=trace_id, error="rate limited").model_dump(),
        )

    response = JSONResponse(
        content=PreauthResponse(
            ok=True, csrf_token=secrets.csrf_token, trace_id=trace_id
        ).model_dump(),
    )
    # Preauth cookies are short-lived (15 minutes) for the SPA auth flow.
    response.set_cookie(
        "careerops_session",
        secrets.token,
        max_age=15 * 60,
        path="/",
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        "careerops_csrf",
        secrets.csrf_token,
        max_age=15 * 60,
        path="/",
        httponly=False,
        samesite="strict",
    )
    return response


@router.get("/api/v1/auth/session", response_model=SessionResponse)
async def session_status(request: Request) -> SessionResponse:
    """Check whether the caller holds a valid authenticated session.

    Returns ``{ authenticated: false }`` when the cookie is missing, expired,
    or otherwise invalid.  Returns user details plus CSRF token and absolute
    expiry when the session is alive.
    """
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or uuid4().hex

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        return SessionResponse(authenticated=False, trace_id=trace_id)

    session_token = request.cookies.get("careerops_session", "")
    if not session_token:
        return SessionResponse(authenticated=False, trace_id=trace_id)

    now = datetime.now(UTC)
    try:
        principal = auth_service.authenticate(session_token, now=now)
    except (AuthError, ValueError):
        return SessionResponse(authenticated=False, trace_id=trace_id)

    # Retrieve raw CSRF token from the cookie so the SPA can echo it back
    # in X-CSRF-Token headers for state-changing requests.
    csrf_token = request.cookies.get("careerops_csrf", "")

    return SessionResponse(
        authenticated=True,
        user=SessionUser(
            username=principal.username,
            candidate_id=str(principal.candidate_id) if principal.candidate_id else "",
        ),
        csrf_token=csrf_token,
        expires_at=principal.absolute_expires_at.isoformat(),
        trace_id=trace_id,
    )


@router.get("/api/v1/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    """Return the current user identity including candidate_id.

    Returns an empty identity when no valid session exists.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        return MeResponse(user_id="", username="")

    session_token = request.cookies.get("careerops_session", "")
    if not session_token:
        return MeResponse(user_id="", username="")

    now = datetime.now(UTC)
    try:
        principal = auth_service.authenticate(session_token, now=now)
    except (AuthError, ValueError):
        return MeResponse(user_id="", username="")

    return MeResponse(
        user_id=str(principal.user_id),
        username=principal.username,
        candidate_id=str(principal.candidate_id) if principal.candidate_id else "",
    )


@router.post("/api/v1/auth/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    """Authenticate with username/password and receive session cookies.

    The caller must already hold a preauth session cookie (careerops_session)
    obtained from a prior ``begin_preauth`` call.  The CSRF token from the
    preauth cookie is forwarded automatically.
    """
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or uuid4().hex

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        return JSONResponse(
            status_code=503,
            content=LoginResponse(
                ok=False,
                trace_id=trace_id,
                error="Authentication service is not available",
                code="DEPENDENCY_NOT_READY",
            ).model_dump(),
        )

    preauth_token = request.cookies.get("careerops_session", "")
    csrf_token = request.cookies.get("careerops_csrf", "")

    if not preauth_token or not csrf_token:
        return JSONResponse(
            status_code=422,
            content=LoginResponse(
                ok=False,
                trace_id=trace_id,
                error="Missing preauth session; load the login page first",
                code="VALIDATION_ERROR",
            ).model_dump(),
        )

    client_host = request.client.host if request.client is not None else "unknown"
    now = datetime.now(UTC)
    context = AuthRequestContext(trace_id=trace_id, client_key=client_host)

    try:
        secrets = auth_service.login(
            preauth_token=preauth_token,
            csrf_token=csrf_token,
            username=body.username,
            password=body.password,
            now=now,
            context=context,
        )
    except AuthRateLimited:
        return JSONResponse(
            status_code=429,
            content=LoginResponse(
                ok=False,
                trace_id=trace_id,
                error="Too many attempts; try again later",
                code="RATE_LIMITED",
            ).model_dump(),
        )
    except InvalidCredentials:
        return JSONResponse(
            status_code=401,
            content=LoginResponse(
                ok=False,
                trace_id=trace_id,
                error="Username or password is invalid",
                code="INVALID_CREDENTIALS",
            ).model_dump(),
        )
    except (InvalidSession, AuthError):
        return JSONResponse(
            status_code=401,
            content=LoginResponse(
                ok=False,
                trace_id=trace_id,
                error="Session is invalid or expired",
                code="INVALID_CREDENTIALS",
            ).model_dump(),
        )

    response = JSONResponse(
        content=LoginResponse(
            ok=True,
            authenticated=True,
            user=SessionUser(username=body.username),
            csrf_token=secrets.csrf_token,
            expires_at=secrets.absolute_expires_at.isoformat(),
            trace_id=trace_id,
        ).model_dump(),
    )
    # Session cookie (HttpOnly)
    response.set_cookie(
        "careerops_session",
        secrets.token,
        max_age=7 * 24 * 60 * 60,
        path="/",
        httponly=True,
        samesite="strict",
    )
    # CSRF cookie (readable by JS for X-CSRF-Token header)
    response.set_cookie(
        "careerops_csrf",
        secrets.csrf_token,
        max_age=7 * 24 * 60 * 60,
        path="/",
        httponly=False,
        samesite="strict",
    )
    return response


@router.post("/api/v1/auth/bootstrap", response_model=BootstrapResponse)
async def bootstrap(request: Request, body: BootstrapRequest) -> JSONResponse:
    """Create the initial admin user via a one-time bootstrap token.

    The caller must hold a preauth session cookie (careerops_session) obtained
    from a prior ``begin_preauth`` call.  On success the preauth session is
    promoted to a full authenticated session and cookies are set.
    """
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or uuid4().hex

    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        return JSONResponse(
            status_code=503,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="Authentication service is not available",
                code="DEPENDENCY_NOT_READY",
            ).model_dump(),
        )

    preauth_token = request.cookies.get("careerops_session", "")
    csrf_token = request.cookies.get("careerops_csrf", "")

    if not preauth_token or not csrf_token:
        return JSONResponse(
            status_code=422,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="Missing preauth session; load the page first",
                code="VALIDATION_ERROR",
            ).model_dump(),
        )

    client_host = request.client.host if request.client is not None else "unknown"
    now = datetime.now(UTC)
    context = AuthRequestContext(trace_id=trace_id, client_key=client_host)

    try:
        secrets = auth_service.complete_bootstrap(
            preauth_token=preauth_token,
            csrf_token=csrf_token,
            bootstrap_token=body.bootstrap_token,
            username=body.username,
            password=body.password,
            now=now,
            context=context,
        )
    except SingleUserAlreadyExists:
        return JSONResponse(
            status_code=409,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="A user already exists; bootstrap is closed",
                code="BOOTSTRAP_CLOSED",
            ).model_dump(),
        )
    except AuthRateLimited:
        return JSONResponse(
            status_code=429,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="Too many attempts; try again later",
                code="RATE_LIMITED",
            ).model_dump(),
        )
    except InvalidBootstrapCredential:
        return JSONResponse(
            status_code=401,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="Bootstrap token is invalid or expired",
                code="INVALID_BOOTSTRAP_CREDENTIAL",
            ).model_dump(),
        )
    except (InvalidSession, AuthError):
        return JSONResponse(
            status_code=401,
            content=BootstrapResponse(
                ok=False,
                trace_id=trace_id,
                error="Session is invalid or expired",
                code="INVALID_BOOTSTRAP_CREDENTIAL",
            ).model_dump(),
        )

    response = JSONResponse(
        content=BootstrapResponse(
            ok=True,
            authenticated=True,
            user=SessionUser(username=body.username),
            csrf_token=secrets.csrf_token,
            expires_at=secrets.absolute_expires_at.isoformat(),
            trace_id=trace_id,
        ).model_dump(),
    )
    response.set_cookie(
        "careerops_session",
        secrets.token,
        max_age=7 * 24 * 60 * 60,
        path="/",
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        "careerops_csrf",
        secrets.csrf_token,
        max_age=7 * 24 * 60 * 60,
        path="/",
        httponly=False,
        samesite="strict",
    )
    return response


@router.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(request: Request) -> JSONResponse:
    """Revoke the current session and clear cookies.

    Requires a valid session cookie and matching X-CSRF-Token header.
    Returns ``{ ok: true }`` even when no session exists (already logged out).
    """
    trace_id = getattr(getattr(request, "state", None), "trace_id", None) or uuid4().hex

    auth_service = getattr(request.app.state, "auth_service", None)

    session_token = request.cookies.get("careerops_session", "")
    csrf_token = request.headers.get("x-csrf-token", "")

    # Graceful no-op when no session cookie is present.
    if not session_token:
        response = JSONResponse(content=LogoutResponse(ok=True, trace_id=trace_id).model_dump())
        response.delete_cookie("careerops_session", path="/")
        response.delete_cookie("careerops_csrf", path="/")
        return response

    # Missing CSRF header while session cookie exists -- reject.
    if not csrf_token:
        return JSONResponse(
            status_code=403,
            content=LogoutResponse(ok=False, trace_id=trace_id).model_dump(),
        )

    # Service unavailable -- still clear cookies so the SPA can recover.
    if auth_service is None:
        response = JSONResponse(content=LogoutResponse(ok=True, trace_id=trace_id).model_dump())
        response.delete_cookie("careerops_session", path="/")
        response.delete_cookie("careerops_csrf", path="/")
        return response

    client_host = request.client.host if request.client is not None else "unknown"
    now = datetime.now(UTC)
    context = AuthRequestContext(trace_id=trace_id, client_key=client_host)

    # Session already revoked or invalid -- treat as success so the
    # frontend can always clear its state.
    with contextlib.suppress(InvalidSession, AuthError):
        auth_service.logout(
            session_token=session_token,
            csrf_token=csrf_token,
            now=now,
            context=context,
        )

    response = JSONResponse(content=LogoutResponse(ok=True, trace_id=trace_id).model_dump())
    response.delete_cookie("careerops_session", path="/")
    response.delete_cookie("careerops_csrf", path="/")
    return response
