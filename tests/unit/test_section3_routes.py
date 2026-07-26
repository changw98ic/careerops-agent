"""Route tests for the Section-3 additive API (tasks 3.2 / 3.3 / 3.6).

Builds a minimal FastAPI app with ONLY the Section-3 routers and overrides the
``require_candidate_id`` / ``require_api_auth`` dependencies so the route logic
is exercised without a live auth service. Pins:

- missing service on app.state -> 503 (Iron Rule 2, no silent fallback)
- candidate ownership scoping (server-resolved id wins; client body never)
- profile create/activate/get/version-history flows (3.2)
- resume register (multipart) -> parse + evidence; dedupe; confirm (3.3)
- evidence confirm/reject idempotency + actor-from-session (3.6)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.auth_dependency import require_api_auth, require_candidate_id
from careerops.api.errors import install_error_handlers
from careerops.api.routes.evidence import router as evidence_router
from careerops.api.routes.profile import router as profile_router
from careerops.api.routes.resumes import router as resumes_router
from careerops.application.evidence_service import EvidenceService, ListEvidenceAuditSink
from careerops.application.profile_service import ProfileService
from careerops.application.resume_service import ResumeService
from careerops.auth.contracts import AuthenticatedPrincipal
from careerops.infrastructure.memory_repos import (
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryProfileRepository,
)

CANDIDATE = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# In-memory content store stub (mirror of the service-test stub)
# ---------------------------------------------------------------------------


class _MemStore:
    def __init__(self, *, max_bytes: int = 5_000_000) -> None:
        self._blobs: dict[str, bytes] = {}
        self._max = max_bytes

    def put(
        self, stream, *, media_type, classification, owner, retention_until, expected_sha256=None
    ):
        import hashlib

        content = stream.read()
        if len(content) > self._max:
            from careerops.application.ports.storage import SizeLimitExceeded

            raise SizeLimitExceeded("too big")
        digest = hashlib.sha256(content).hexdigest()
        self._blobs.setdefault(digest, content)
        from careerops.application.ports.storage import StoredContent

        return StoredContent(
            object_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
            sha256=digest,
            byte_size=len(content),
            media_type=media_type,
            classification=classification,
            owner=owner,
            retention_until=retention_until,
        )

    def read_bytes(self, stored, *, now=None):
        return self._blobs[stored.sha256]

    def delete_bytes(self, stored):
        self._blobs.pop(stored.sha256, None)
        from careerops.application.ports.storage import StorageDeleteResult

        return StorageDeleteResult.DELETED


def _principal() -> AuthenticatedPrincipal:
    from datetime import UTC, datetime

    return AuthenticatedPrincipal(
        user_id=USER_ID,
        username="owner",
        session_id=uuid4(),
        csrf_token_hash="hash",
        absolute_expires_at=datetime.now(tz=UTC),
        candidate_id=CANDIDATE,
    )


def _build_app(
    *,
    wire_services: bool = True,
    candidate_id: UUID | None = CANDIDATE,
) -> tuple[FastAPI, TestClient, dict[str, object]]:
    """Build a test app with the Section-3 routers and optional service wiring.

    Returns ``(app, client, services)``. When ``wire_services`` is False the
    services are NOT placed on app.state so routes surface 503.
    """
    app = FastAPI()
    app.include_router(profile_router)
    app.include_router(resumes_router)
    app.include_router(evidence_router)
    install_error_handlers(app)

    services: dict[str, object] = {}
    if wire_services:
        profile_repo = InMemoryProfileRepository()
        resume_repo = InMemoryApplicationRepository()
        evidence_repo = InMemoryEvidenceRepository()
        audit_sink = ListEvidenceAuditSink()
        services["profile_service"] = ProfileService(profile_repo)
        services["resume_service"] = ResumeService(
            resume_repo, evidence_repo, _MemStore(), max_bytes=5_000_000
        )
        services["evidence_service"] = EvidenceService(evidence_repo, audit_sink=audit_sink)
        services["_evidence_repo"] = evidence_repo
        services["_audit_sink"] = audit_sink
        for key, value in services.items():
            if not key.startswith("_"):
                setattr(app.state, key, value)

    # Override auth: server-resolved candidate id always wins. When the
    # resolved id is None, mirror the real dependency's 403 behavior rather
    # than handing the route a None owner.
    from careerops.api.errors import CandidateProfileRequiredError

    app.dependency_overrides[require_api_auth] = lambda: _principal()

    def _resolve_candidate() -> UUID:
        if candidate_id is None:
            raise CandidateProfileRequiredError()
        return candidate_id

    app.dependency_overrides[require_candidate_id] = _resolve_candidate

    client = TestClient(app)
    return app, client, services


# ===========================================================================
# 503 wiring (Iron Rule 2)
# ===========================================================================


class TestRouteWiring:
    def test_missing_service_is_503(self) -> None:
        _app, client, _services = _build_app(wire_services=False)
        # No profile_service on app.state -> 503, never a silent empty 200.
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_NOT_READY"

    def test_missing_principal_is_403(self) -> None:
        # candidate_id None -> CandidateProfileRequiredError (403).
        _app, client, _services = _build_app(candidate_id=None)
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "CANDIDATE_PROFILE_REQUIRED"


# ===========================================================================
# Profile routes (3.2)
# ===========================================================================


class TestProfileRoutes:
    def test_get_active_returns_404_when_absent(self) -> None:
        _app, client, _services = _build_app()
        resp = client.get("/api/v1/profile")
        assert resp.status_code == 404

    def test_create_then_get_active(self) -> None:
        _app, client, _services = _build_app()
        body = {
            "target_roles": [{"title": "Backend Engineer", "seniority": "senior"}],
            "locations": [{"name": "Chengdu", "kind": "required"}],
            "remote_rules": {"remote_allowed": True},
            "compensation": {"currency": "CNY", "amount_min": 100, "amount_max": 200},
        }
        resp = client.post("/api/v1/profile", json=body)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["version"] == 1
        assert created["is_active"] is True

        active = client.get("/api/v1/profile").json()
        assert active["id"] == created["id"]
        assert active["target_roles"][0]["title"] == "Backend Engineer"

    def test_invalid_preferences_return_409(self) -> None:
        _app, client, _services = _build_app()
        body = {
            "target_roles": [{"title": "X"}],
            "locations": [
                {"name": "NYC", "kind": "required"},
                {"name": "NYC", "kind": "excluded"},
            ],
        }
        resp = client.post("/api/v1/profile", json=body)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_STATE"

    def client_candidate_substitution_in_body_is_ignored(self) -> None:
        # A client-supplied candidate_id in the body MUST NOT be honored; the
        # server-resolved id always owns the new version. The body schema
        # doesn't even declare candidate_id, so any stray field is dropped by
        # Pydantic. Pin that behavior.
        _app, client, _services = _build_app()
        body = {
            "target_roles": [{"title": "X"}],
            "locations": [{"name": "Chengdu", "kind": "required"}],
            "candidate_id": str(OTHER),  # stray field — must be ignored.
        }
        resp = client.post("/api/v1/profile", json=body)
        assert resp.status_code == 201
        # The created version belongs to the server-resolved candidate.
        active = client.get("/api/v1/profile").json()
        assert active["candidate_id"] == str(CANDIDATE)

    def test_version_history_and_activate(self) -> None:
        _app, client, _services = _build_app()
        body = {
            "target_roles": [{"title": "A"}],
            "locations": [{"name": "Chengdu", "kind": "required"}],
        }
        first = client.post("/api/v1/profile", json=body).json()
        body2 = {
            "target_roles": [{"title": "B"}],
            "locations": [{"name": "Chengdu", "kind": "required"}],
        }
        client.post("/api/v1/profile", json=body2)

        history = client.get("/api/v1/profile/versions").json()
        assert history["total"] == 2
        assert [v["version"] for v in history["items"]] == [2, 1]

        # Re-activate the first version.
        resp = client.post(f"/api/v1/profile/versions/{first['id']}/activate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True
        active = client.get("/api/v1/profile").json()
        assert active["id"] == first["id"]

    def test_get_other_candidate_version_is_404(self) -> None:
        _app, client, _services = _build_app()
        body = {
            "target_roles": [{"title": "A"}],
            "locations": [{"name": "Chengdu", "kind": "required"}],
        }
        created = client.post("/api/v1/profile", json=body).json()
        # Swap the resolved candidate; the version id is not owned by OTHER.
        _app.dependency_overrides[require_candidate_id] = lambda: OTHER
        resp = client.get(f"/api/v1/profile/versions/{created['id']}")
        assert resp.status_code == 404


# ===========================================================================
# Resume routes (3.3)
# ===========================================================================


_RESUME_TEXT = (
    b"Jane Doe\nBackend Engineer with 5+ years of Python, Go, and PostgreSQL.\n"
    b"Built distributed systems with Kubernetes and AWS.\n"
)


class TestResumeRoutes:
    def test_register_multipart_parses_and_extracts(self) -> None:
        _app, client, _services = _build_app()
        resp = client.post(
            "/api/v1/resumes",
            files={"file": ("resume.txt", _RESUME_TEXT, "text/plain")},
            data={"target_type": "general", "source_reference": "upload"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["parse_status"] == "parsed"
        assert body["deduplicated"] is False
        assert body["extracted_evidence_count"] > 0
        assert body["resume"]["confirmation_status"] == "unconfirmed"

        # Evidence linked to the resume.
        ev = client.get(f"/api/v1/resumes/{body['resume']['id']}/evidence").json()
        assert ev["total"] == body["extracted_evidence_count"]

    def test_register_dedupe_returns_existing(self) -> None:
        _app, client, _services = _build_app()
        first = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _RESUME_TEXT, "text/plain")},
        ).json()
        second = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _RESUME_TEXT, "text/plain")},
        ).json()
        assert second["deduplicated"] is True
        assert second["resume"]["id"] == first["resume"]["id"]

    def test_register_rejects_unsupported_media(self) -> None:
        _app, client, _services = _build_app()
        resp = client.post(
            "/api/v1/resumes",
            files={"file": ("r.bin", b"x", "application/octet-stream")},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_STATE"

    def test_confirm_then_eligible(self) -> None:
        _app, client, _services = _build_app()
        rid = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _RESUME_TEXT, "text/plain")},
        ).json()["resume"]["id"]
        # Not eligible before confirm.
        assert client.get("/api/v1/resumes/eligible").json()["total"] == 0
        resp = client.post(f"/api/v1/resumes/{rid}/confirm")
        assert resp.status_code == 200
        assert resp.json()["confirmation_status"] == "confirmed"
        assert client.get("/api/v1/resumes/eligible").json()["total"] == 1

    def test_get_other_candidate_resume_is_404(self) -> None:
        _app, client, _services = _build_app()
        rid = client.post(
            "/api/v1/resumes",
            files={"file": ("r.txt", _RESUME_TEXT, "text/plain")},
        ).json()["resume"]["id"]
        _app.dependency_overrides[require_candidate_id] = lambda: OTHER
        assert client.get(f"/api/v1/resumes/{rid}").status_code == 404


# ===========================================================================
# Evidence routes (3.6)
# ===========================================================================


def _seed_evidence(service: EvidenceService) -> str:
    from careerops.domain.candidates import EvidenceItem

    item = EvidenceItem(
        id=uuid4(),
        candidate_id=CANDIDATE,
        kind=__import__(
            "careerops.domain.candidates", fromlist=["EvidenceKind"]
        ).EvidenceKind.SKILL,
        name="Python",
        confirmation_status=__import__(
            "careerops.domain.applications", fromlist=["ConfirmationStatus"]
        ).ConfirmationStatus.UNCONFIRMED,
        evidence_hash="a" * 64,
        content_hash="a" * 64,
    )
    # Store through the underlying repo (service holds the repo).
    stored = service._repo.store(item)
    return str(stored.id)


class TestEvidenceRoutes:
    def test_confirm_is_idempotent_and_records_actor(self) -> None:
        _app, client, services = _build_app()
        evidence_id = _seed_evidence(services["evidence_service"])  # type: ignore[arg-type]

        first = client.post(
            f"/api/v1/evidence/{evidence_id}/confirm", json={"source_reference": "r1"}
        )
        second = client.post(
            f"/api/v1/evidence/{evidence_id}/confirm", json={"source_reference": "r2"}
        )

        assert first.status_code == 200
        assert first.json()["was_change"] is True
        assert second.json()["was_change"] is False
        # Audit recorded the real actor (the session user id), once.
        sink: ListEvidenceAuditSink = services["_audit_sink"]  # type: ignore[assignment]
        assert len(sink.events) == 1
        assert sink.events[0].actor_id == str(USER_ID)
        assert sink.events[0].source_reference == "r1"

    def test_confirm_then_list_confirmed(self) -> None:
        _app, client, services = _build_app()
        evidence_id = _seed_evidence(services["evidence_service"])  # type: ignore[arg-type]
        client.post(f"/api/v1/evidence/{evidence_id}/confirm")
        confirmed = client.get("/api/v1/evidence?status=confirmed").json()
        assert confirmed["total"] == 1
        assert confirmed["items"][0]["id"] == evidence_id

    def test_confirm_other_candidate_is_404(self) -> None:
        _app, client, services = _build_app()
        evidence_id = _seed_evidence(services["evidence_service"])  # type: ignore[arg-type]
        _app.dependency_overrides[require_candidate_id] = lambda: OTHER
        resp = client.post(f"/api/v1/evidence/{evidence_id}/confirm")
        assert resp.status_code == 404

    def test_unknown_status_filter_is_409(self) -> None:
        _app, client, _services = _build_app()
        resp = client.get("/api/v1/evidence?status=bogus")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVALID_STATE"
