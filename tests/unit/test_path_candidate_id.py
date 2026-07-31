"""Unit tests for the ``path_candidate_id`` FastAPI dependency.

``path_candidate_id`` is the path-param replacement for the session-based
``require_candidate_id`` in the post-auth console. Identity comes from the URL
(``/api/v1/candidates/{candidate_id}/...``) and existence is verified through
``app.state.candidate_service``.

These tests mount a tiny FastAPI app whose only route depends on
``path_candidate_id`` and assert the three contract branches:
* existing candidate  -> 200, dependency returns the id;
* missing candidate   -> 404 (NotFoundError);
* service not wired    -> 503 (DependencyNotReadyError).
"""

from __future__ import annotations

from typing import Annotated
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from careerops.api.auth_dependency import path_candidate_id


def _make_app(*, candidate_service: object | None) -> FastAPI:
    """Build a minimal app with one route exercising the dependency."""
    app = FastAPI()
    if candidate_service is not None:
        app.state.candidate_service = candidate_service

    @app.get("/api/v1/candidates/{candidate_id}/probe")
    async def probe(
        candidate_id: Annotated[UUID, Depends(path_candidate_id)],
    ) -> dict[str, str]:
        return {"candidate_id": str(candidate_id)}

    return app


def test_path_candidate_id_returns_id_when_candidate_exists() -> None:
    """An existing candidate resolves to 200 and echoes the path id."""
    cid = uuid4()
    svc = MagicMock()
    svc.get.return_value = MagicMock(id=cid)  # truthy => candidate exists
    client = TestClient(_make_app(candidate_service=svc))

    response = client.get(f"/api/v1/candidates/{cid}/probe")

    assert response.status_code == 200
    assert response.json() == {"candidate_id": str(cid)}
    svc.get.assert_called_once_with(cid)


def test_path_candidate_id_returns_404_when_candidate_missing() -> None:
    """A candidate id the service does not know yields 404."""
    cid = uuid4()
    svc = MagicMock()
    svc.get.return_value = None  # candidate not found
    client = TestClient(_make_app(candidate_service=svc))

    response = client.get(f"/api/v1/candidates/{cid}/probe")

    assert response.status_code == 404
    svc.get.assert_called_once_with(cid)


def test_path_candidate_id_returns_503_when_service_absent() -> None:
    """A missing ``candidate_service`` on app.state yields 503."""
    cid = uuid4()
    client = TestClient(_make_app(candidate_service=None))

    response = client.get(f"/api/v1/candidates/{cid}/probe")

    assert response.status_code == 503
