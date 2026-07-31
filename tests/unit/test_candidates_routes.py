"""Unit tests for the global candidate CRUD routes (auth-rm Task 2).

The routes read ``app.state.candidate_service`` via the ``_service`` dependency
and return 503 when it is absent. These tests build a minimal FastAPI app
mounting ONLY the candidates router so they exercise the route logic and the
``app.state`` contract directly, without dragging in the full ``create_app``
graph (which carries unrelated RuntimeResources wiring).

The end-to-end wiring (``create_app`` setting ``app.state.candidate_service``
and calling ``app.include_router(candidates.router)``) is verified by
inspection in ``src/careerops/api/app.py`` inside the ``RuntimeResources``
block; see ``task-2-report.md`` for why a full ``create_app`` integration
test is blocked on pre-existing branch breakage.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.routes.candidates import router as candidates_router
from careerops.application.candidate_service import CandidateService


class _InMemoryCandidateRepo:
    """In-memory stand-in for ``PostgresCandidateRepository``.

    Implements the ``CandidateRepository`` Protocol: ``list_all`` / ``create``
    (accepts a ``Candidate``) / ``get``.
    """

    def __init__(self) -> None:
        self.rows: list = []
        self._by_id: dict = {}

    def list_all(self, *, limit: int = 50) -> list:
        return list(self.rows)[:limit]

    def create(self, candidate) -> object:
        self.rows.append(candidate)
        self._by_id[candidate.id] = candidate
        return candidate

    def get(self, cid) -> object | None:
        return self._by_id.get(cid)


def _make_client_with_service() -> tuple[TestClient, _InMemoryCandidateRepo]:
    """Build a minimal app mounting only the candidates router + service."""
    repo = _InMemoryCandidateRepo()
    app = FastAPI()
    app.include_router(candidates_router)
    app.state.candidate_service = CandidateService(repo)
    return TestClient(app), repo


def test_list_initially_empty_then_create_get_round_trip():
    client, _ = _make_client_with_service()

    # Initially empty list.
    r = client.get("/api/v1/candidates")
    assert r.status_code == 200
    assert r.json() == {"items": []}

    # Create.
    r = client.post("/api/v1/candidates", json={"display_name": "Alice"})
    assert r.status_code == 201
    created = r.json()
    cid = created["id"]
    assert created["display_name"] == "Alice"
    # IDs are returned as strings on the wire (UUID → str).
    assert isinstance(cid, str)

    # List reflects the new row.
    r = client.get("/api/v1/candidates")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == cid
    assert items[0]["display_name"] == "Alice"

    # Detail lookup by id.
    r = client.get(f"/api/v1/candidates/{cid}")
    assert r.status_code == 200
    assert r.json()["id"] == cid
    assert r.json()["display_name"] == "Alice"


def test_get_unknown_candidate_returns_404():
    client, _ = _make_client_with_service()
    r = client.get(f"/api/v1/candidates/{uuid4()}")
    assert r.status_code == 404


def test_create_validation_errors_return_422():
    client, _ = _make_client_with_service()
    # Empty display_name violates the min_length=1 constraint.
    r = client.post("/api/v1/candidates", json={"display_name": ""})
    assert r.status_code == 422
    # Missing body entirely.
    r = client.post("/api/v1/candidates", json={})
    assert r.status_code == 422


def test_missing_service_returns_503():
    """When app.state.candidate_service is absent, every endpoint surfaces 503."""
    app = FastAPI()
    app.include_router(candidates_router)
    # Deliberately do NOT inject candidate_service.
    client = TestClient(app)
    assert client.get("/api/v1/candidates").status_code == 503
    assert (
        client.post("/api/v1/candidates", json={"display_name": "x"}).status_code
        == 503
    )
    assert client.get(f"/api/v1/candidates/{uuid4()}").status_code == 503


def test_candidates_router_wiring_in_create_app():
    """Static check: create_app wires candidate_service + registers the router.

    Reads ``app.py`` as text (rather than importing it, which is blocked by
    unrelated pre-existing branch breakage — see module docstring) and asserts
    that the Task 2 wiring is present.
    """
    from pathlib import Path

    app_py = Path(__file__).resolve().parents[2] / "src" / "careerops" / "api" / "app.py"
    source = app_py.read_text()

    assert "from careerops.api.routes.candidates import router as candidates_router" in source
    assert "app.state.candidate_service = CandidateService(" in source
    assert "PostgresCandidateRepository(probe.database)" in source
    assert "app.include_router(candidates_router)" in source


# NOTE: A full ``create_app(Settings(environment="test"), readiness_probe=MagicMock())``
# integration test (which would exercise the runtime wiring of
# ``app.state.candidate_service`` end-to-end) cannot run today because the
# module-level ``app = create_app()`` at the bottom of ``app.py`` fails to
# construct ``RuntimeResources``: commit 3304648 added several
# ``probe.<service>`` references (``match_orchestrator``, ``crawl_permission_service``,
# ``inbox_service``, ``source_queue_service``) whose attributes were never added to
# the ``RuntimeResources`` class. That breakage predates and is unrelated to
# Task 2; see ``task-2-report.md``.
