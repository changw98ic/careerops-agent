"""Thin Jinja2+HTMX web UI for Companies, Jobs Inbox, and Job Detail (M1.11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

_TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))

web_router = APIRouter(tags=["console-jobs"])


@web_router.get("/companies", response_class=HTMLResponse, include_in_schema=False)
async def companies_page(request: Request) -> Response:
    repo = _get_repo(request)
    companies: list[dict[str, Any]] = []
    if repo is not None:
        result = repo.list_companies(cursor=None, limit=100)
        companies = result["items"] if isinstance(result, dict) else result
    principal = _get_principal(request)
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name="companies.html",
        context={
            "username": principal,
            "companies": companies,
            "csrf_token": _get_csrf(request),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@web_router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_inbox_page(request: Request) -> Response:
    repo = _get_repo(request)
    jobs: list[dict[str, Any]] = []
    if repo is not None:
        result = repo.list_canonical_jobs(cursor=None, limit=100, state=None)
        jobs = result["items"] if isinstance(result, dict) else result
    principal = _get_principal(request)
    response = _TEMPLATES.TemplateResponse(
        request=request,
        name="jobs_inbox.html",
        context={
            "username": principal,
            "jobs": jobs,
            "csrf_token": _get_csrf(request),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@web_router.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def job_detail_page(job_id: str, request: Request) -> Response:
    repo = _get_repo(request)
    detail: dict[str, Any] | None = None
    if repo is not None:
        detail = repo.get_job_detail(job_id)
    principal = _get_principal(request)
    if detail is None:
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Job not found"},
            status_code=404,
        )
    else:
        response = _TEMPLATES.TemplateResponse(
            request=request,
            name="job_detail.html",
            context={
                "username": principal,
                "job": detail,
                "csrf_token": _get_csrf(request),
            },
        )
    response.headers["Cache-Control"] = "no-store"
    return response


def _get_repo(request: Request) -> Any:
    return getattr(request.app.state, "job_read_repository", None)


def _get_principal(request: Request) -> str:
    principal = getattr(request.state, "console_principal", None)
    if principal is not None:
        return cast("str", getattr(principal, "username", "user"))
    return "user"


def _get_csrf(request: Request) -> str:
    return cast("str", getattr(request.state, "console_csrf", ""))
