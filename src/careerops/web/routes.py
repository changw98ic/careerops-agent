from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from careerops.application.dashboard import DashboardSnapshotProvider
from careerops.auth.contracts import (
    AuthenticatedPrincipal,
    AuthError,
    AuthRateLimited,
    AuthRequestContext,
    InvalidBootstrapCredential,
    InvalidCredentials,
    InvalidSession,
    SessionSecrets,
)
from careerops.web.security import (
    ConsoleSecurityHeadersMiddleware,
    ConsoleWebSettings,
    OriginHostValidator,
    RequestOriginRejected,
)

_TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))


class ConsoleAuthServicePort(Protocol):
    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets: ...

    def complete_bootstrap(
        self,
        *,
        preauth_token: str,
        csrf_token: str,
        bootstrap_token: str,
        username: str,
        password: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets: ...

    def login(
        self,
        *,
        preauth_token: str,
        csrf_token: str,
        username: str,
        password: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets: ...

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal: ...

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None: ...

    def logout(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> None: ...


def install_console_web(
    app: FastAPI,
    service: ConsoleAuthServicePort,
    dashboard_provider: DashboardSnapshotProvider,
    settings: ConsoleWebSettings,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    """Install the anonymous auth forms and a session-protected web router.

    This intentionally does not modify API routes. Root composition should call this before
    the application starts; health routes already installed on the parent app stay anonymous.
    """

    clock = now_provider or (lambda: datetime.now(UTC))
    app.add_middleware(ConsoleSecurityHeadersMiddleware)
    install_console_login_redirect(app)
    public_router, protected_router = create_console_routers(
        service,
        dashboard_provider,
        settings,
        now_provider=clock,
    )
    app.include_router(public_router)
    app.include_router(protected_router)


def create_console_routers(
    service: ConsoleAuthServicePort,
    dashboard_provider: DashboardSnapshotProvider,
    settings: ConsoleWebSettings,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> tuple[APIRouter, APIRouter]:
    clock = now_provider or (lambda: datetime.now(UTC))
    validator = OriginHostValidator(settings)
    public = APIRouter(tags=["console-auth"])

    def request_context(request: Request) -> AuthRequestContext:
        trace_id = cast("str", getattr(request.state, "trace_id", uuid4().hex))
        client_host = request.client.host if request.client is not None else "unknown"
        return AuthRequestContext(
            trace_id=trace_id,
            client_key=client_host,
        )

    def require_principal(request: Request) -> AuthenticatedPrincipal:
        raw_session = request.cookies.get(settings.session_cookie_name, "")
        raw_csrf = request.cookies.get(settings.csrf_cookie_name, "")
        try:
            validator.validate_host(request)
            principal = service.authenticate(raw_session, now=clock())
            service.validate_csrf(principal, raw_csrf)
        except (AuthError, RequestOriginRejected, ValueError):
            raise ConsoleLoginRequired from None
        request.state.console_principal = principal
        request.state.console_csrf = raw_csrf
        return principal

    protected = APIRouter(
        tags=["console"],
        dependencies=[Depends(require_principal)],
    )

    @public.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_form(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            validator.validate_host(request)
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        raw_session = request.cookies.get(settings.session_cookie_name, "")
        raw_csrf = request.cookies.get(settings.csrf_cookie_name, "")
        if raw_session and raw_csrf:
            try:
                principal = service.authenticate(raw_session, now=clock())
                service.validate_csrf(principal, raw_csrf)
                return RedirectResponse("/", status_code=303)
            except AuthError:
                pass
        return _new_auth_form(
            request,
            service,
            settings,
            clock,
            template="login.html",
            context=request_context(request),
        )

    @public.get("/bootstrap", response_class=HTMLResponse, include_in_schema=False)
    def bootstrap_form(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            validator.validate_host(request)
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        return _new_auth_form(
            request,
            service,
            settings,
            clock,
            template="bootstrap.html",
            context=request_context(request),
        )

    @public.post("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_submit(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        username: Annotated[str, Form(min_length=1, max_length=64)],
        password: Annotated[str, Form(min_length=1, max_length=1024)],
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
    ) -> Response:
        try:
            validator.validate_mutation(request)
            secrets = service.login(
                preauth_token=request.cookies.get(settings.session_cookie_name, ""),
                csrf_token=csrf_token,
                username=username,
                password=password,
                now=clock(),
                context=request_context(request),
            )
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        except AuthRateLimited:
            return _error(request, status_code=429, message="Try again later")
        except InvalidCredentials:
            return _new_auth_form(
                request,
                service,
                settings,
                clock,
                template="login.html",
                context=request_context(request),
                error="Username or password is invalid",
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        _set_session_cookies(response, settings, secrets)
        return response

    @public.post("/bootstrap", response_class=HTMLResponse, include_in_schema=False)
    def bootstrap_submit(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        username: Annotated[str, Form(min_length=1, max_length=64)],
        password: Annotated[str, Form(min_length=1, max_length=1024)],
        bootstrap_token: Annotated[str, Form(min_length=1, max_length=512)],
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
    ) -> Response:
        try:
            validator.validate_mutation(request)
            secrets = service.complete_bootstrap(
                preauth_token=request.cookies.get(settings.session_cookie_name, ""),
                csrf_token=csrf_token,
                bootstrap_token=bootstrap_token,
                username=username,
                password=password,
                now=clock(),
                context=request_context(request),
            )
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        except AuthRateLimited:
            return _error(request, status_code=429, message="Try again later")
        except InvalidBootstrapCredential:
            return _new_auth_form(
                request,
                service,
                settings,
                clock,
                template="bootstrap.html",
                context=request_context(request),
                error="Bootstrap credential is invalid or expired",
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        _set_session_cookies(response, settings, secrets)
        return response

    @protected.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        snapshot = await dashboard_provider.snapshot()
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "username": principal.username,
                "csrf_token": cast("str", request.state.console_csrf),
                "dashboard": snapshot,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @protected.post("/logout", response_class=HTMLResponse, include_in_schema=False)
    def logout_submit(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
    ) -> Response:
        try:
            validator.validate_mutation(request)
            service.logout(
                session_token=request.cookies.get(settings.session_cookie_name, ""),
                csrf_token=csrf_token,
                now=clock(),
                context=request_context(request),
            )
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        except AuthRateLimited:
            return _error(request, status_code=429, message="Try again later")
        except (InvalidSession, AuthError):
            return _error(request, status_code=401, message="Session is no longer valid")
        response = RedirectResponse("/login", status_code=303)
        _clear_session_cookies(response, settings)
        return response

    return public, protected


class ConsoleLoginRequired(Exception):
    pass


def install_console_login_redirect(app: FastAPI) -> None:
    """Install separately when root composition wants HTML redirects for protected web routes."""

    @app.exception_handler(ConsoleLoginRequired)
    async def console_login_required(  # pyright: ignore[reportUnusedFunction]
        _request: Request,
        _error: ConsoleLoginRequired,
    ) -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response


def _new_auth_form(
    request: Request,
    service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    clock: Callable[[], datetime],
    *,
    template: str,
    context: AuthRequestContext,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    try:
        secrets = service.begin_preauth(now=clock(), context=context)
    except AuthRateLimited:
        return _error(request, status_code=429, message="Try again later")
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context={"csrf_token": secrets.csrf_token, "error": error},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    _set_session_cookies(response, settings, secrets, preauth=True)
    return response


def _set_session_cookies(
    response: Response,
    settings: ConsoleWebSettings,
    secrets: SessionSecrets,
    *,
    preauth: bool = False,
) -> None:
    max_age = 15 * 60 if preauth else 7 * 24 * 60 * 60
    response.set_cookie(
        settings.session_cookie_name,
        secrets.token,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        secrets.csrf_token,
        max_age=max_age,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookies(response: Response, settings: ConsoleWebSettings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def _error(request: Request, *, status_code: int, message: str) -> Response:
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name="error.html",
        context={"message": message},
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = [
    "ConsoleAuthServicePort",
    "ConsoleLoginRequired",
    "create_console_routers",
    "install_console_login_redirect",
    "install_console_web",
]
