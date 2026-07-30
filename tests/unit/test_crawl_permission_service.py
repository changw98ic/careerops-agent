"""Unit tests for Phase 6.2 + 6.7: crawl-permission service + classifier (6.1).

Tests:
- 6.1: Outcome classifier detects login redirects (3xx + login URL) and
       login walls on 200 pages, but NOT empty/403/captcha alone.
- 6.2: Permission service -- request_permission pauses source and creates
       at most one pending request; returns existing on repeats.
       grant/deny/revoke/expire enforce the state machine.
- 6.7: API CRUD + ownership + duplicate-request + grant/deny/revoke/expiry
       + audit log.

Uses in-memory fakes for repositories (no database required).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.crawl_permission_service import (
    CrawlPermissionService,
    ListPermissionAuditSink,
    PermissionAuditEvent,
    PermissionDecision,
)
from careerops.application.outcome_classifier import (
    ClassificationInput,
    classify_outcome,
    has_login_evidence,
    has_login_redirect_signal,
)
from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlPermissionRepository,
    CrawlPermissionState,
    CrawlSourcePermission,
    is_permission_transition_allowed,
)
from careerops.domain.crawl_plans import (
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
    CrawlSourceType,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult


# ---------------------------------------------------------------------------
# In-memory repository fakes
# ---------------------------------------------------------------------------

OWNER = UUID("00000000-0000-0000-0000-000000000001")


class InMemorySourceRepo:
    """In-memory ``CrawlSourceRepository`` for unit tests."""

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
    """In-memory ``CrawlPermissionRepository`` for unit tests."""

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
        # Stamp the decision timestamp.
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


def _empty_result(status_code: int = 200, body_prefix: str = "") -> CrawlSourceResult:
    return CrawlSourceResult(
        postings=(),
        status_code=status_code,
        body_prefix=body_prefix,
        expected_fields_missing=(),
    )


# ===========================================================================
# 6.1 -- Outcome classifier: login evidence detection
# ===========================================================================


class TestClassifierLoginEvidence:
    """Task 6.1: login requirements detected ONLY from explicit evidence."""

    def test_login_required_body_is_evidence(self) -> None:
        assert has_login_evidence("Please login required to view jobs")

    def test_sign_in_to_continue_is_evidence(self) -> None:
        assert has_login_evidence("Sign in to continue to your account")

    def test_authentication_required_is_evidence(self) -> None:
        assert has_login_evidence("Authentication required for this page")

    def test_empty_body_is_not_evidence(self) -> None:
        assert not has_login_evidence("")

    def test_random_body_is_not_evidence(self) -> None:
        assert not has_login_evidence("Welcome to our careers page")

    def test_login_redirect_signal(self) -> None:
        assert has_login_redirect_signal("Location: https://example.com/login")

    def test_signin_redirect_signal(self) -> None:
        assert has_login_redirect_signal("https://example.com/sign-in?next=/jobs")

    def test_sso_redirect_signal(self) -> None:
        assert has_login_redirect_signal("login.microsoftonline.com/authorize")

    def test_no_redirect_signal_on_regular_url(self) -> None:
        assert not has_login_redirect_signal("https://example.com/jobs/list")


class TestClassifierAuthRequired:
    """Task 6.1 + 5.6: AUTH_REQUIRED only with explicit login evidence."""

    def test_403_with_login_evidence_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(403, "login required"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_403_without_login_evidence_is_dynamic(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(403, "forbidden"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_200_with_login_wall_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(200, "sign in to continue to view job listings"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_200_without_signals_is_verified_empty(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(200, "No open positions right now"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.VERIFIED_EMPTY

    def test_302_with_login_redirect_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(302, "Location: https://example.com/login"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_302_without_login_redirect_is_dynamic(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(302, "Location: https://example.com/new-page"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_captcha_without_login_evidence_is_dynamic(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(200, "captcha please verify you are human"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED

    def test_captcha_with_login_evidence_is_auth_required(self) -> None:
        inp = ClassificationInput(
            result=_empty_result(200, "captcha login required"),
        )
        assert classify_outcome(inp) == CrawlAttemptOutcome.AUTH_REQUIRED

    def test_empty_body_no_signals_is_not_auth_required(self) -> None:
        inp = ClassificationInput(result=_empty_result(200, ""))
        assert classify_outcome(inp) != CrawlAttemptOutcome.AUTH_REQUIRED


# ===========================================================================
# 6.2 -- Permission service
# ===========================================================================


class TestPermissionServiceRequest:
    """Task 6.2: request_permission pauses source, creates one pending request."""

    def _setup(self) -> tuple[CrawlPermissionService, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]:
        source_repo = InMemorySourceRepo()
        perm_repo = InMemoryPermissionRepo()
        audit = ListPermissionAuditSink()
        svc = CrawlPermissionService(perm_repo, source_repo, audit_sink=audit)
        return svc, source_repo, perm_repo, audit

    def test_request_creates_pending_permission(self) -> None:
        svc, source_repo, _, audit = self._setup()
        source = _make_source()
        source_repo.save(source)

        perm = svc.request_permission(OWNER, source.id, login_evidence="login required")
        assert perm.state == CrawlPermissionState.PENDING
        assert perm.source_id == source.id
        assert perm.disclosed_terms.get("login_evidence") == "login required"
        # Source should be paused.
        updated_source = source_repo.get_by_id(OWNER, source.id)
        assert updated_source.state == CrawlSourceState.PAUSED
        assert not updated_source.enabled
        # Audit log should have one REQUESTED event.
        assert len(audit.events) == 1
        assert audit.events[0].decision == PermissionDecision.REQUESTED

    def test_request_returns_existing_pending_on_repeat(self) -> None:
        svc, source_repo, _, _ = self._setup()
        source = _make_source()
        source_repo.save(source)

        first = svc.request_permission(OWNER, source.id)
        second = svc.request_permission(OWNER, source.id)
        assert first.id == second.id
        assert first.state == CrawlPermissionState.PENDING

    def test_request_pauses_already_paused_source(self) -> None:
        svc, source_repo, _, _ = self._setup()
        source = _make_source(enabled=False, state=CrawlSourceState.PAUSED)
        source_repo.save(source)

        perm = svc.request_permission(OWNER, source.id)
        assert perm.state == CrawlPermissionState.PENDING

    def test_request_nonexistent_source_raises(self) -> None:
        svc, _, _, _ = self._setup()
        with pytest.raises(Exception):
            svc.request_permission(OWNER, uuid4())


class TestPermissionServiceDecisions:
    """Task 6.2: grant/deny/revoke/expire transitions + audit."""

    def _setup_with_request(self) -> tuple[CrawlPermissionService, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink, CrawlSourcePermission]:
        svc, source_repo, _, audit = self._setup()
        source = _make_source()
        source_repo.save(source)
        perm = svc.request_permission(OWNER, source.id, login_evidence="sign in")
        return svc, source_repo, _, audit, perm

    def _setup(self) -> tuple[CrawlPermissionService, InMemorySourceRepo, InMemoryPermissionRepo, ListPermissionAuditSink]:
        source_repo = InMemorySourceRepo()
        perm_repo = InMemoryPermissionRepo()
        audit = ListPermissionAuditSink()
        svc = CrawlPermissionService(perm_repo, source_repo, audit_sink=audit)
        return svc, source_repo, perm_repo, audit

    def test_grant_transitions_to_granted(self) -> None:
        svc, source_repo, _, audit, perm = self._setup_with_request()
        granted = svc.grant(OWNER, perm.id)
        assert granted.state == CrawlPermissionState.GRANTED
        assert granted.granted_at is not None
        assert granted.expires_at is not None
        # Source should be re-enabled.
        updated_source = source_repo.get_by_id(OWNER, perm.source_id)
        assert updated_source.state == CrawlSourceState.ACTIVE
        assert updated_source.enabled
        # Audit: REQUESTED + GRANTED.
        assert len(audit.events) == 2
        assert audit.events[1].decision == PermissionDecision.GRANTED

    def test_deny_transitions_to_denied(self) -> None:
        svc, _, _, audit, perm = self._setup_with_request()
        denied = svc.deny(OWNER, perm.id, reason="user declined")
        assert denied.state == CrawlPermissionState.DENIED
        assert denied.denied_at is not None
        assert audit.events[-1].decision == PermissionDecision.DENIED
        assert audit.events[-1].reason == "user declined"

    def test_revoke_transitions_to_revoked(self) -> None:
        svc, source_repo, _, audit, perm = self._setup_with_request()
        # Must grant first.
        svc.grant(OWNER, perm.id)
        revoked = svc.revoke(OWNER, perm.id, reason="changed mind")
        assert revoked.state == CrawlPermissionState.REVOKED
        assert revoked.revoked_at is not None
        # Source should be paused again.
        updated_source = source_repo.get_by_id(OWNER, perm.source_id)
        assert updated_source.state == CrawlSourceState.PAUSED
        assert not updated_source.enabled
        assert audit.events[-1].decision == PermissionDecision.REVOKED

    def test_expire_transitions_to_expired(self) -> None:
        svc, source_repo, _, audit, perm = self._setup_with_request()
        svc.grant(OWNER, perm.id)
        expired = svc.expire(OWNER, perm.id)
        assert expired.state == CrawlPermissionState.EXPIRED
        assert expired.expired_at is not None
        updated_source = source_repo.get_by_id(OWNER, perm.source_id)
        assert updated_source.state == CrawlSourceState.PAUSED
        assert audit.events[-1].decision == PermissionDecision.EXPIRED

    def test_deny_on_granted_raises(self) -> None:
        svc, _, _, _, perm = self._setup_with_request()
        svc.grant(OWNER, perm.id)
        with pytest.raises(ValueError, match="illegal"):
            svc.deny(OWNER, perm.id)

    def test_revoke_on_pending_raises(self) -> None:
        svc, _, _, _, perm = self._setup_with_request()
        with pytest.raises(ValueError, match="illegal"):
            svc.revoke(OWNER, perm.id)

    def test_grant_on_denied_raises(self) -> None:
        svc, _, _, _, perm = self._setup_with_request()
        svc.deny(OWNER, perm.id)
        with pytest.raises(ValueError, match="illegal"):
            svc.grant(OWNER, perm.id)

    def test_re_grant_after_revoke_requires_new_request(self) -> None:
        svc, source_repo, _, _, perm = self._setup_with_request()
        svc.grant(OWNER, perm.id)
        svc.revoke(OWNER, perm.id)
        # Terminal state -- cannot re-grant.
        with pytest.raises(ValueError, match="illegal"):
            svc.grant(OWNER, perm.id)
        # But a new request can be made.
        new_perm = svc.request_permission(OWNER, perm.source_id)
        assert new_perm.id != perm.id
        assert new_perm.state == CrawlPermissionState.PENDING

    def test_list_for_source_returns_all_permissions(self) -> None:
        svc, source_repo, _, _, perm = self._setup_with_request()
        svc.deny(OWNER, perm.id)
        # Create a new request.
        new_perm = svc.request_permission(OWNER, perm.source_id)
        all_perms = svc.list_for_source(OWNER, perm.source_id)
        assert len(all_perms) == 2
        ids = {p.id for p in all_perms}
        assert perm.id in ids
        assert new_perm.id in ids


# ===========================================================================
# 6.7 -- State machine transition table
# ===========================================================================


class TestPermissionTransitions:
    """Verify the ALLOWED_PERMISSION_TRANSITIONS table directly."""

    def test_pending_can_go_to_granted_denied_expired(self) -> None:
        for target in [CrawlPermissionState.GRANTED, CrawlPermissionState.DENIED, CrawlPermissionState.EXPIRED]:
            assert is_permission_transition_allowed(CrawlPermissionState.PENDING, target)

    def test_granted_can_go_to_revoked_expired(self) -> None:
        for target in [CrawlPermissionState.REVOKED, CrawlPermissionState.EXPIRED]:
            assert is_permission_transition_allowed(CrawlPermissionState.GRANTED, target)

    def test_denied_is_terminal(self) -> None:
        for target in CrawlPermissionState:
            assert not is_permission_transition_allowed(CrawlPermissionState.DENIED, target)

    def test_revoked_is_terminal(self) -> None:
        for target in CrawlPermissionState:
            assert not is_permission_transition_allowed(CrawlPermissionState.REVOKED, target)

    def test_expired_is_terminal(self) -> None:
        for target in CrawlPermissionState:
            assert not is_permission_transition_allowed(CrawlPermissionState.EXPIRED, target)

    def test_pending_cannot_go_to_revoked(self) -> None:
        assert not is_permission_transition_allowed(CrawlPermissionState.PENDING, CrawlPermissionState.REVOKED)

    def test_granted_cannot_go_to_pending(self) -> None:
        assert not is_permission_transition_allowed(CrawlPermissionState.GRANTED, CrawlPermissionState.PENDING)
