"""API login/logout endpoints for the Vue SPA."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool
    error: str = ""


@router.post("/api/v1/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service is None:
        response.status_code = 503
        return LoginResponse(ok=False, error="auth not configured")

    from careerops.auth.contracts import AuthError, AuthRequestContext

    ctx = AuthRequestContext(
        trace_id="api-login", client_key=request.client.host if request.client else "unknown"
    )
    now = datetime.now(UTC)

    try:
        preauth = auth_service.begin_preauth(now=now, context=ctx)
        secrets = auth_service.login(
            preauth_token=preauth.token,
            csrf_token=preauth.csrf_token,
            username=body.username,
            password=body.password,
            now=now,
            context=ctx,
        )
    except AuthError as e:
        response.status_code = 401
        return LoginResponse(ok=False, error=str(e))
    except Exception:
        response.status_code = 401
        return LoginResponse(ok=False, error="invalid credentials")

    # Set session cookies
    web_settings = getattr(request.app.state, "web_settings", None)
    secure = web_settings.cookie_secure if web_settings else False
    response.set_cookie(
        key="careerops_session",
        value=secrets.token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=604800,
        path="/",
    )
    response.set_cookie(
        key="careerops_csrf",
        value=secrets.csrf_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=604800,
        path="/",
    )
    return LoginResponse(ok=True)


@router.post("/api/v1/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("careerops_session", path="/")
    response.delete_cookie("careerops_csrf", path="/")
    return {"ok": True}
