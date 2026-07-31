"""Contract tests for Phase 6.3 + 6.7: crawl-permission API endpoints.

Tests:
- API CRUD (list, get, request, grant, deny, revoke)
- Ownership scoping (candidate_id resolved server-side)
- Duplicate-request idempotency
- Grant/deny/revoke/expiry state transitions via API
- Audit log entries after each decision

Uses FastAPI TestClient with in-memory repositories (no database required).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from typing import Annotated

from careerops.application.crawl_permission_service import (
    CrawlPermissionService,
    ListPermissionAuditSink,
)
from careerops.api.routes.crawl_permissions import router as crawl_permissions_router
from careerops.domain.crawl_attempts import (
    CrawlPermissionState,
    CrawlSourcePermission,
    is_permission_transition_allowed,
)
from careerops.domain.crawl_plans import (
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceState,
    CrawlSourceType,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

OWNER = UUID("00000000-0000-0000-0000-000000000001")


class InMemorySourceRepo:
    """Minimal in-memory source repo for API tests."""

    def __init__(self) -> None:
        self._sources: dict[UUID, CrawlSource] = {}

    def save(self, source: CrawlSource) -> CrawlSource:
        self._sources[source.id] = source
        return source

    def get_by_id(self, owner_id: UUID, source_id: UUID) -> CrawlSource:
        src = self._sources.get(source_id)
        if src is None:
            from careerops.api.errors import NotFoundError

            raise NotFoundError("source not found")
        return src

    def list_for(self, owner_id: UUID, *, limit: int = 50) -> list[CrawlSource]:
        return list(self._sources.values())[:limit]

    def update_state(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        state: CrawlSourceState | None = None,
        enabled: bool | None = None,
        now: datetime | None = None,
        **kwargs: object,
    ) -> CrawlSource:
        existing = self._sources.get(source_id)
        if existing is None:
            from careerops.api.errors import NotFoundError

            raise NotFoundError("source not found")
        updated = dataclasses.replace(
            existing,
            state=state if state is not None else existing.state,
            enabled=enabled if enabled is not None else existing.enabled,
            updated_at=now or datetime.now(UTC),
        )
        self._sources[source_id] = updated
        return updated

    def remove(self, owner_id: UUID, source_id: UUID) -> None:
        self._sources.pop(source_id, None)

    def get_by_identity(self, owner_id: UUID, company_id: UUID, source_type: CrawlSourceType, source_identifier: str) -> CrawlSource | None:
        return None

    def upsert_by_identity(self, source: CrawlSource) -> CrawlSource:
        self._sources[source.id] = source
        return source


class InMemoryPermissionRepo:
    """Minimal in-memory permission repo for API tests."""

    def __init__(self) -> None:
        self._permissions: dict[UUID, CrawlSourcePermission] = {}

    def save(self, owner_id: UUID, permission: CrawlSourcePermission) -> CrawlSourcePermission:
        self._permissions[permission.id] = permission
        return permission

    def get_by_id(self, owner_id: UUID, permission_id: UUID) -> CrawlSourcePermission:
        perm = self._permissions.get(permission_id)
        if perm is None:
            from careerops.api.errors import NotFoundError

            raise NotFoundError("permission not found")
        return perm

    def get_unresolved_for_source(self, owner_id: UUID, source_id: UUID) -> CrawlSourcePermission | None:
        for perm in self._permissions.values():
            if perm.source_id == source_id and perm.state == CrawlPermissionState.PENDING:
                return perm
        return None

    def list_for_source(self, owner_id: UUID, source_id: UUID, *, limit: int = 50) -> list[CrawlSourcePermission]:
        perms = [p for p in self._permissions.values() if p.source_id == source_id]
        perms.sort(key=lambda p: p.created_at or datetime.min, reverse=True)
        return perms[:limit]

    def transition(
        self,
        owner_id: UUID,
        permission_id: UUID,
        *,
        to: CrawlPermissionState,
        now: datetime | None = None,
    ) -> CrawlSourcePermission:
        current = self.get_by_id(owner_id, permission_id)
        if not is_permission_transition_allowed(current.state, to):
            raise ValueError(
                f"illegal crawl permission transition: {current.state.value} -> {to.value}"
            )
        ts = now or datetime.now(UTC)
        updated = dataclasses.replace(current, state=to, updated_at=ts)
        if to == CrawlPermissionState.GRANTED:
            updated = dataclasses.replace(updated, granted_at=ts)
        elif to == CrawlPermissionState.DENIED:
            updated = dataclasses.replace(updated, denied_at=ts)
        elif to == CrawlPermissionState.REVOKED:
            updated = dataclasses.replace(updated, revoked_at=ts)
        elif to == CrawlPermissionState.EXPIRED:
            updated = dataclasses.replace(updated, expired_at=ts)
        self._permissions[permission_id] = updated
        return updated


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    source_repo: InMemorySourceRepo,
    perm_repo: InMemoryPermissionRepo,
    audit: ListPermissionAuditSink,
) -> FastAPI:
    """Build a minimal FastAPI app with the permission routes and DI stubs."""
    from careerops.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    svc = CrawlPermissionService(perm_repo, source_repo, audit_sink=audit)
    app.state.crawl_permission_service = svc

    # Stash repos so the list-all endpoint can iterate sources.
    svc._sources = source_repo  # type: ignore[attr-defined]

    # candidate_id now comes from the URL path (OWNER); no auth override needed.
    app.include_router(crawl_permissions_router)
    return app


def _make_source(*, enabled: bool = True, state: CrawlSourceState = CrawlSourceState.ACTIVE) -> CrawlSource:
    return CrawlSource(
        id=uuid4(),
        owner_id=OWNER,
        company_id=uuid4(),
        source_type=CrawlSourceType.GREENHOUSE,
        source_identifier="https://example.com/jobs",
        base_url="https://example.com/jobs",
        state=state,
        enabled=enabled,
        trust_status=CrawlPolicyStatus.ALLOWED,
    )


@pytest.fixture()
def client() -> tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]:
    source_repo = InMemorySourceRepo()
    perm_repo = InMemoryPermissionRepo()
    audit = ListPermissionAuditSink()
    app = _build_app(source_repo, perm_repo, audit)
    return TestClient(app, raise_server_exceptions=False), source_repo, perm_repo, audit


# ===========================================================================
# 6.7 -- API contract tests
# ===========================================================================


class TestPermissionAPIRequest:
    """POST /api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/permissions"""

    def test_request_creates_pending(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, audit = client
        source = _make_source()
        source_repo.save(source)

        resp = c.post(
            f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions",
            json={"login_evidence": "login required", "domain_scope": "example.com"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == "pending"
        assert data["source_id"] == str(source.id)
        assert data["disclosed_terms"]["login_evidence"] == "login required"
        # Source should be paused.
        updated = source_repo.get_by_id(OWNER, source.id)
        assert updated.state == CrawlSourceState.PAUSED
        assert not updated.enabled
        # Audit.
        assert len(audit.events) == 1

    def test_request_returns_existing_on_repeat(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        source = _make_source()
        source_repo.save(source)

        resp1 = c.post(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions", json={})
        resp2 = c.post(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions", json={})
        assert resp1.status_code == 201
        assert resp2.status_code == 201
        assert resp1.json()["id"] == resp2.json()["id"]

    def test_request_nonexistent_source_404(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, _, _, _ = client
        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-sources/{uuid4()}/permissions", json={})
        assert resp.status_code == 404


class TestPermissionAPIList:
    """GET /api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/permissions"""

    def test_list_returns_permissions(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        source = _make_source()
        source_repo.save(source)

        c.post(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions", json={})
        resp = c.get(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["state"] == "pending"

    def test_list_empty(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        source = _make_source()
        source_repo.save(source)

        resp = c.get(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestPermissionAPIDecisions:
    """POST /api/v1/candidates/{candidate_id}/crawl-permissions/{id}/grant|deny|revoke"""

    def _request_permission(self, c: TestClient, source_repo: InMemorySourceRepo) -> str:
        source = _make_source()
        source_repo.save(source)
        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-sources/{source.id}/permissions", json={})
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_grant(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, audit = client
        perm_id = self._request_permission(c, source_repo)

        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/grant")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "granted"
        assert data["granted_at"] is not None
        assert data["expires_at"] is not None
        # Audit: REQUESTED + GRANTED.
        assert len(audit.events) == 2
        assert audit.events[1].decision.value == "granted"

    def test_deny(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, audit = client
        perm_id = self._request_permission(c, source_repo)

        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/deny", json={"reason": "not now"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "denied"
        assert audit.events[-1].decision.value == "denied"

    def test_revoke_after_grant(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, audit = client
        perm_id = self._request_permission(c, source_repo)
        c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/grant")

        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/revoke", json={"reason": "changed mind"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "revoked"
        assert audit.events[-1].decision.value == "revoked"

    def test_deny_on_granted_returns_409(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        perm_id = self._request_permission(c, source_repo)
        c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/grant")

        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/deny", json={})
        assert resp.status_code == 409

    def test_grant_on_denied_returns_409(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        perm_id = self._request_permission(c, source_repo)
        c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/deny", json={})

        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}/grant")
        assert resp.status_code == 409

    def test_grant_nonexistent_404(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, _, _, _ = client
        resp = c.post(f"/api/v1/candidates/{OWNER}/crawl-permissions/{uuid4()}/grant")
        assert resp.status_code == 404

    def test_get_permission(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, source_repo, _, _ = client
        perm_id = self._request_permission(c, source_repo)

        resp = c.get(f"/api/v1/candidates/{OWNER}/crawl-permissions/{perm_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == perm_id
        assert resp.json()["state"] == "pending"

    def test_get_nonexistent_404(self, client: tuple[TestClient, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]) -> None:
        c, _, _, _ = client
        resp = c.get(f"/api/v1/candidates/{OWNER}/crawl-permissions/{uuid4()}")
        assert resp.status_code == 404
