from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from careerops.application.autopilot_control import (
    AutopilotControlPlane,
    CampaignGrantCard,
    DisabledAutopilotControlPlane,
    ReviewQueueItem,
    ReviewQueueTab,
    ReviewResolutionMode,
)
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
from careerops.web.crawler_execution import (
    CrawlerExecutionConsoleCommandResult,
    CrawlerExecutionConsoleProvider,
    DisabledCrawlerExecutionConsoleProvider,
)
from careerops.web.security import (
    ConsoleSecurityHeadersMiddleware,
    ConsoleWebSettings,
    OriginHostValidator,
    RequestOriginRejected,
)

_TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))


_REVIEW_QUEUE_TABS: tuple[tuple[ReviewQueueTab, str], ...] = (
    (ReviewQueueTab.PENDING, "待确认"),
    (ReviewQueueTab.EXCEPTIONS, "异常"),
    (ReviewQueueTab.RECOVERY, "恢复"),
)


AutopilotControlPlaneProvider = AutopilotControlPlane
AutopilotGrantSummary = CampaignGrantCard
EmptyAutopilotControlPlaneProvider = DisabledAutopilotControlPlane


class GoalRunWebProvider(Protocol):
    async def list_goal_runs(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> object: ...

    async def submit_review(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        review_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        now: datetime,
    ) -> object: ...


class DisabledGoalRunWebProvider:
    async def list_goal_runs(self, **_kwargs: object) -> object:
        raise GoalRunWebUnavailable("goal run operator runtime is not configured")

    async def submit_review(self, **_kwargs: object) -> object:
        raise GoalRunWebUnavailable("goal run operator runtime is not configured")


class GoalRunWebUnavailable(RuntimeError):
    pass


class GoalRunWebConflict(RuntimeError):
    pass


class GoalRunWebNotFound(LookupError):
    pass


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
    control_plane_provider: AutopilotControlPlane | None = None,
    crawler_execution_provider: CrawlerExecutionConsoleProvider | None = None,
    goal_run_provider: GoalRunWebProvider | None = None,
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
        control_plane_provider=control_plane_provider,
        crawler_execution_provider=crawler_execution_provider,
        goal_run_provider=goal_run_provider,
        now_provider=clock,
    )
    app.include_router(public_router)
    app.include_router(protected_router)


def create_console_routers(
    service: ConsoleAuthServicePort,
    dashboard_provider: DashboardSnapshotProvider,
    settings: ConsoleWebSettings,
    *,
    control_plane_provider: AutopilotControlPlane | None = None,
    crawler_execution_provider: CrawlerExecutionConsoleProvider | None = None,
    goal_run_provider: GoalRunWebProvider | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> tuple[APIRouter, APIRouter]:
    clock = now_provider or (lambda: datetime.now(UTC))
    control_plane = control_plane_provider or DisabledAutopilotControlPlane()
    crawler_execution = crawler_execution_provider or DisabledCrawlerExecutionConsoleProvider()
    goal_runs = goal_run_provider or DisabledGoalRunWebProvider()
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

    @protected.get("/review", response_class=HTMLResponse, include_in_schema=False)
    async def review_queue(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        tab: Annotated[str, Query(max_length=64)] = ReviewQueueTab.PENDING.value,
    ) -> Response:
        current_tab = _parse_review_queue_tab(tab)
        if current_tab is None:
            return _error(request, status_code=400, message="Invalid review queue tab")
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        snapshot = await control_plane.review_queue(
            actor_id=principal.user_id,
            tab=current_tab,
            now=clock(),
        )
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="review_queue.html",
            context={
                "username": principal.username,
                "items": snapshot.items,
                "counts": snapshot.counts,
                "counts_by_tab": {count.tab: count.count for count in snapshot.counts},
                "capability": snapshot.capability,
                "tabs": _REVIEW_QUEUE_TABS,
                "current_tab": current_tab,
                "current_tab_label": _review_queue_tab_label(current_tab),
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @protected.get("/grants/autopilot", response_class=HTMLResponse, include_in_schema=False)
    async def autopilot_grants(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        snapshot = await control_plane.campaign_grants(
            actor_id=principal.user_id,
            now=clock(),
        )
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="autopilot_grants.html",
            context={
                "username": principal.username,
                "grants": snapshot.grants,
                "capability": snapshot.capability,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @protected.get("/crawlers/review", response_class=HTMLResponse, include_in_schema=False)
    async def crawler_execution_review(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        snapshot = await crawler_execution.pending_requests(
            actor_id=principal.user_id,
            now=clock(),
        )
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="crawler_execution_review.html",
            context={
                "username": principal.username,
                "csrf_token": cast("str", request.state.console_csrf),
                "capability": snapshot.capability,
                "requests": snapshot.requests,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @protected.get("/goal-runs", response_class=HTMLResponse, include_in_schema=False)
    async def goal_runs_review(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        unavailable_message: str | None = None
        try:
            listed = await goal_runs.list_goal_runs(actor_id=principal.user_id, limit=50)
            raw_goal_runs = getattr(listed, "goal_runs", ())
        except Exception as exc:
            if not _is_goal_run_unavailable(exc):
                raise
            raw_goal_runs = ()
            unavailable_message = "GoalRun 审核运行时未启用。此页面不会触发任何外部写入。"
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="goal_runs.html",
            context={
                "username": principal.username,
                "csrf_token": cast("str", request.state.console_csrf),
                "goal_runs": tuple(_goal_run_view_model(item) for item in raw_goal_runs),
                "unavailable_message": unavailable_message,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @protected.post(
        "/goal-runs/{goal_run_id}/reviews/{review_id}/approve",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def approve_goal_run_review(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        goal_run_id: UUID,
        review_id: UUID,
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
        snapshot_sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
    ) -> Response:
        return await _goal_run_review_mutation(
            request,
            service=service,
            validator=validator,
            goal_runs=goal_runs,
            clock=clock,
            goal_run_id=goal_run_id,
            review_id=review_id,
            csrf_token=csrf_token,
            snapshot_sha256=snapshot_sha256,
            decision="approve",
        )

    @protected.post(
        "/goal-runs/{goal_run_id}/reviews/{review_id}/reject",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def reject_goal_run_review(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        goal_run_id: UUID,
        review_id: UUID,
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
        snapshot_sha256: Annotated[str, Form(pattern=r"^[0-9a-f]{64}$")],
    ) -> Response:
        return await _goal_run_review_mutation(
            request,
            service=service,
            validator=validator,
            goal_runs=goal_runs,
            clock=clock,
            goal_run_id=goal_run_id,
            review_id=review_id,
            csrf_token=csrf_token,
            snapshot_sha256=snapshot_sha256,
            decision="reject",
        )

    @protected.post(
        "/crawlers/review/{request_id}/approve",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def crawler_execution_approve(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        request_id: UUID,
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
    ) -> Response:
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        try:
            validator.validate_mutation(request)
            service.validate_csrf(principal, csrf_token)
            result = await crawler_execution.approve_request(
                actor_id=principal.user_id,
                request_id=request_id,
                now=clock(),
            )
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        except (AuthError, ValueError):
            return _error(request, status_code=400, message="Invalid CSRF token")
        return _crawler_execution_mutation_response(request, result, approved=True)

    @protected.post(
        "/crawlers/review/{request_id}/reject",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def crawler_execution_reject(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        request_id: UUID,
        csrf_token: Annotated[str, Form(min_length=1, max_length=512)],
        reason: Annotated[str, Form(min_length=1, max_length=1000)],
    ) -> Response:
        principal = cast("AuthenticatedPrincipal", request.state.console_principal)
        try:
            validator.validate_mutation(request)
            service.validate_csrf(principal, csrf_token)
            result = await crawler_execution.reject_request(
                actor_id=principal.user_id,
                request_id=request_id,
                reason=reason,
                now=clock(),
            )
        except RequestOriginRejected:
            return _error(request, status_code=400, message="Invalid request origin")
        except (AuthError, ValueError):
            return _error(request, status_code=400, message="Invalid CSRF token")
        return _crawler_execution_mutation_response(request, result, approved=False)

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


def _crawler_execution_mutation_response(
    request: Request,
    result: CrawlerExecutionConsoleCommandResult,
    *,
    approved: bool,
) -> Response:
    if result.accepted:
        status = "approved" if approved else "rejected"
        response = RedirectResponse(f"/crawlers/review?status={status}", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
    return _error(
        request,
        status_code=403,
        message=f"{result.title_zh}: {result.reason_code}",
    )


async def _goal_run_review_mutation(
    request: Request,
    *,
    service: ConsoleAuthServicePort,
    validator: OriginHostValidator,
    goal_runs: GoalRunWebProvider,
    clock: Callable[[], datetime],
    goal_run_id: UUID,
    review_id: UUID,
    csrf_token: str,
    snapshot_sha256: str,
    decision: Literal["approve", "reject"],
) -> Response:
    principal = cast("AuthenticatedPrincipal", request.state.console_principal)
    try:
        validator.validate_mutation(request)
        service.validate_csrf(principal, csrf_token)
        await goal_runs.submit_review(
            actor_id=principal.user_id,
            goal_run_id=goal_run_id,
            review_id=review_id,
            command_id=uuid4(),
            snapshot_sha256=snapshot_sha256,
            decision=decision,
            now=clock(),
        )
    except RequestOriginRejected:
        return _error(request, status_code=400, message="Invalid request origin")
    except (AuthError, ValueError):
        return _error(request, status_code=400, message="Invalid CSRF token")
    except Exception as exc:
        if _is_goal_run_not_found(exc):
            return _error(request, status_code=404, message="GoalRun review not found")
        if _is_goal_run_conflict(exc):
            return _error(request, status_code=409, message="GoalRun review snapshot changed")
        if _is_goal_run_unavailable(exc):
            return _error(request, status_code=503, message="GoalRun runtime unavailable")
        raise
    status_text = "approved" if decision == "approve" else "rejected"
    response = RedirectResponse(f"/goal-runs?status={status_text}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _goal_run_view_model(goal_run: object) -> dict[str, object]:
    pending_review = getattr(goal_run, "pending_review", None)
    return {
        "goal_run": goal_run,
        "pending_review": pending_review,
        "payload_json": _review_payload_json(
            getattr(pending_review, "payload", {}) if pending_review is not None else {}
        ),
    }


def _review_payload_json(payload: object) -> str:
    safe_payload = _sanitize_web_payload(payload)
    return json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _sanitize_web_payload(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_web_payload(item)
            for key, item in cast(dict[object, object], value).items()
            if not _is_sensitive_web_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_web_payload(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_sanitize_web_payload(item) for item in cast(tuple[object, ...], value)]
    return value


def _is_sensitive_web_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        marker in normalized
        for marker in (
            "credential",
            "secret",
            "token",
            "oauth",
            "password",
            "api_key",
            "private_key",
            "access_key",
            "handle",
        )
    )


def _is_goal_run_unavailable(exc: Exception) -> bool:
    return isinstance(exc, GoalRunWebUnavailable) or exc.__class__.__name__ in {
        "GoalRunUnavailable",
    }


def _is_goal_run_conflict(exc: Exception) -> bool:
    return isinstance(exc, GoalRunWebConflict) or exc.__class__.__name__ in {
        "GoalRunConflict",
    }


def _is_goal_run_not_found(exc: Exception) -> bool:
    return isinstance(exc, GoalRunWebNotFound) or exc.__class__.__name__ in {
        "GoalRunNotFound",
    }


def _review_queue_tab_label(tab: ReviewQueueTab) -> str:
    for value, label in _REVIEW_QUEUE_TABS:
        if value == tab:
            return label
    raise ValueError("review queue tab must be known")


def _parse_review_queue_tab(raw_tab: str) -> ReviewQueueTab | None:
    try:
        return ReviewQueueTab(raw_tab)
    except ValueError:
        return None


__all__ = [
    "AutopilotControlPlaneProvider",
    "AutopilotGrantSummary",
    "ConsoleAuthServicePort",
    "ConsoleLoginRequired",
    "CrawlerExecutionConsoleProvider",
    "EmptyAutopilotControlPlaneProvider",
    "GoalRunWebConflict",
    "GoalRunWebNotFound",
    "GoalRunWebProvider",
    "GoalRunWebUnavailable",
    "ReviewQueueItem",
    "ReviewQueueTab",
    "ReviewResolutionMode",
    "create_console_routers",
    "install_console_login_redirect",
    "install_console_web",
]
