from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import httpx2
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import RuntimeEnvironment, Settings


class FixedReadinessProbe:
    def __init__(self) -> None:
        self._checks: Mapping[str, ReadinessState] = {
            "database": ReadinessState.OK,
            "redis": ReadinessState.OK,
            "temporal": ReadinessState.OK,
            "storage": ReadinessState.OK,
        }

    async def check(self) -> ReadinessReport:
        return ReadinessReport(checks=self._checks)

    async def close(self) -> None:
        return None


def make_client() -> httpx2.Client:
    settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
    return cast(
        "httpx2.Client", TestClient(create_app(settings, readiness_probe=FixedReadinessProbe()))
    )


def test_backend_does_not_register_frontend_html_entries() -> None:
    client = make_client()

    for path in (
        "/",
        "/login",
        "/bootstrap",
        "/logout",
        "/jobs",
        "/companies",
        "/matches",
        "/assets/index.js",
    ):
        response = client.get(path)
        assert response.status_code == 404, path


def test_api_health_remains_available_without_frontend_mount() -> None:
    response = make_client().get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_vite_forwards_api_requests_in_dev_and_preview() -> None:
    config = Path("frontend/vite.config.js").read_text(encoding="utf-8")

    assert "VITE_API_PROXY_TARGET" in config
    assert "server:" in config
    assert "preview:" in config
    assert "proxy: { '/api': apiProxy }" in config


def test_frontend_has_a_separate_container_build() -> None:
    frontend_dockerfile = Path("frontend/Dockerfile").read_text(encoding="utf-8")
    backend_dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "npm run build" in frontend_dockerfile
    assert '"preview"' in frontend_dockerfile
    assert "frontend/dist" not in backend_dockerfile
    assert "CAREEROPS_SERVE_SPA" not in backend_dockerfile
