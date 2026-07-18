from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response, status

from careerops import __version__
from careerops.api.contracts import CheckStatus, HealthResponse, ReadinessResponse
from careerops.application.ports.readiness import ReadinessProbe, ReadinessState
from careerops.config import Settings

router = APIRouter(prefix="/api/v1/health", tags=["health"])


def _settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)


def _readiness_probe(request: Request) -> ReadinessProbe:
    return cast("ReadinessProbe", request.app.state.readiness_probe)


@router.get("/live", response_model=HealthResponse, summary="Process liveness")
async def liveness(response: Response) -> HealthResponse:
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(version=__version__)


@router.get("/ready", response_model=ReadinessResponse, summary="Enabled dependency readiness")
async def readiness(request: Request, response: Response) -> ReadinessResponse:
    settings = _settings(request)
    response.headers["Cache-Control"] = "no-store"
    dependency_report = await _readiness_probe(request).check()
    checks = {
        "config": CheckStatus.OK,
        **{
            component: (CheckStatus.OK if state is ReadinessState.OK else CheckStatus.NOT_READY)
            for component, state in dependency_report.checks.items()
        },
        "model_provider": CheckStatus.DISABLED,
        "google_oauth": CheckStatus.DISABLED,
        "external_writes": CheckStatus.DISABLED,
    }
    if not dependency_report.ready or (
        settings.model_provider != "disabled"
        or settings.google_oauth_enabled
        or settings.external_writes_enabled
    ):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", checks=checks)
    return ReadinessResponse(status="ready", checks=checks)
