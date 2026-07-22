from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import careerops.api.app as app_module
from careerops.api.app import create_app
from careerops.api.gmail_readonly import (
    DisabledGmailReadonlyOperatorProvider,
    GmailAccountStatusResponse,
    GmailAccountSummary,
    GmailProposalSummary,
    GmailReadonlyConflict,
    GmailReadonlyNotFound,
    GmailReadonlyOperatorProvider,
    GmailReadonlyUnavailable,
    GmailSyncRunSummary,
    ListGmailAccountsResponse,
    ListGmailProposalsResponse,
    ListGmailSyncRunsResponse,
    RegisterGmailAccountResponse,
    RequestGmailSyncResponse,
    ResetGmailHistoryResponse,
    ReviewGmailProposalResponse,
    RevokeGmailAccountResponse,
)
from careerops.application.dashboard import DashboardSnapshot, DashboardSystemStatus
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.auth.contracts import AuthenticatedPrincipal, AuthRequestContext, SessionSecrets
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000001101")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000001102")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000001103")
RUN_ID = UUID("00000000-0000-0000-0000-000000001104")
SIGNAL_ID = UUID("00000000-0000-0000-0000-000000001105")
PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000001106")
CANONICAL_JOB_ID = UUID("00000000-0000-0000-0000-000000001107")
JOB_POSTING_ID = UUID("00000000-0000-0000-0000-000000001108")
REGISTER_COMMAND_ID = UUID("00000000-0000-0000-0000-000000001109")
SYNC_COMMAND_ID = UUID("00000000-0000-0000-0000-00000000110a")
REVIEW_COMMAND_ID = UUID("00000000-0000-0000-0000-00000000110b")
RESET_COMMAND_ID = UUID("00000000-0000-0000-0000-00000000110c")
REVOKE_COMMAND_ID = UUID("00000000-0000-0000-0000-00000000110d")
SNAPSHOT_SHA256 = "a" * 64
EVIDENCE_SHA256 = "b" * 64
MUTATION_HEADERS = {"Origin": "http://testserver", "X-CSRF-Token": "authenticated-csrf"}


class FixedReadinessProbe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            checks={
                "database": ReadinessState.OK,
                "redis": ReadinessState.OK,
                "temporal": ReadinessState.OK,
                "storage": ReadinessState.OK,
            }
        )

    async def close(self) -> None:
        return None


class FixedRuntimeResources(FixedReadinessProbe):
    def __init__(self) -> None:
        super().__init__()
        self.database = SimpleNamespace(begin=object())
        self.redis_sync = object()


class FixedConsoleAuthService:
    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        assert now and context.trace_id
        return self._authenticated_secrets()

    def complete_bootstrap(self, **_kwargs: object) -> SessionSecrets:
        return self._authenticated_secrets()

    def login(self, **_kwargs: object) -> SessionSecrets:
        return self._authenticated_secrets()

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        assert now.tzinfo is not None
        if session_token != "authenticated-session":
            raise ValueError("invalid session")
        return AuthenticatedPrincipal(
            user_id=ACTOR_ID,
            username="owner",
            session_id=UUID("00000000-0000-0000-0000-00000000110e"),
            csrf_token_hash="not-exposed",
            absolute_expires_at=NOW + timedelta(days=7),
        )

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None:
        assert principal.user_id == ACTOR_ID
        if csrf_token != "authenticated-csrf":
            raise ValueError("bad csrf")

    def logout(self, **_kwargs: object) -> None:
        return None

    @staticmethod
    def _authenticated_secrets() -> SessionSecrets:
        return SessionSecrets(
            session_id=UUID("00000000-0000-0000-0000-00000000110e"),
            token="authenticated-session",
            csrf_token="authenticated-csrf",
            absolute_expires_at=NOW + timedelta(days=7),
        )


class FixedDashboardProvider:
    async def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            system_status=DashboardSystemStatus.READY,
            dependency_checks=(("database", "ok"),),
            integration_checks=(("gmail_writes", "disabled"),),
            pending_approvals=0,
            pending_outbox_events=0,
        )


class RecordingGmailReadonlyProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        credential_handle: str,
        account_subject: str,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        publishing_status: Literal["testing", "in_production"],
        credential_store_evidence_sha256: str,
        now: datetime,
    ) -> RegisterGmailAccountResponse:
        self.calls.append(
            (
                "register",
                (
                    actor_id,
                    command_id,
                    candidate_id,
                    credential_handle,
                    account_subject,
                    dedicated,
                    oauth_client_mode,
                    publishing_status,
                    credential_store_evidence_sha256,
                    now,
                ),
            )
        )
        return RegisterGmailAccountResponse(
            account=_account(
                actor_id=actor_id, candidate_id=candidate_id, account_subject=account_subject
            )
        )

    async def list_accounts(self, *, actor_id: UUID, limit: int) -> ListGmailAccountsResponse:
        self.calls.append(("list_accounts", (actor_id, limit)))
        return ListGmailAccountsResponse(accounts=(_account(actor_id=actor_id),))

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GmailAccountStatusResponse:
        self.calls.append(("account_status", (actor_id, account_id)))
        return GmailAccountStatusResponse(
            account=_account(actor_id=actor_id, account_id=account_id)
        )

    async def request_sync(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: Literal["manual", "history_expired", "scheduled", "recovery"],
        now: datetime,
    ) -> RequestGmailSyncResponse:
        self.calls.append(("request_sync", (actor_id, account_id, command_id, reason, now)))
        return RequestGmailSyncResponse(
            run=_run(actor_id=actor_id, account_id=account_id, reason=reason)
        )

    async def list_sync_runs(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailSyncRunsResponse:
        self.calls.append(("list_sync_runs", (actor_id, account_id, limit)))
        return ListGmailSyncRunsResponse(runs=(_run(actor_id=actor_id, account_id=account_id),))

    async def list_proposals(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailProposalsResponse:
        self.calls.append(("list_proposals", (actor_id, account_id, limit)))
        return ListGmailProposalsResponse(
            proposals=(_proposal(actor_id=actor_id, account_id=account_id),)
        )

    async def review_proposal(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        proposal_id: UUID,
        command_id: UUID,
        decision: Literal["approve", "reject"],
        snapshot_sha256: str,
        canonical_job_id: UUID | None,
        job_posting_id: UUID | None,
        reason: str | None,
        now: datetime,
    ) -> ReviewGmailProposalResponse:
        self.calls.append(
            (
                "review_proposal",
                (
                    actor_id,
                    account_id,
                    proposal_id,
                    command_id,
                    decision,
                    snapshot_sha256,
                    canonical_job_id,
                    job_posting_id,
                    reason,
                    now,
                ),
            )
        )
        return ReviewGmailProposalResponse(
            proposal=_proposal(
                actor_id=actor_id,
                account_id=account_id,
                proposal_id=proposal_id,
                canonical_job_id=canonical_job_id,
                job_posting_id=job_posting_id,
                proposal_status="approved" if decision == "approve" else "rejected",
            )
        )

    async def reset_history(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        reason: str | None,
        now: datetime,
    ) -> ResetGmailHistoryResponse:
        self.calls.append(
            ("reset_history", (actor_id, account_id, command_id, snapshot_sha256, reason, now))
        )
        return ResetGmailHistoryResponse(
            account=_account(actor_id=actor_id, account_id=account_id, status="sync_required")
        )

    async def revoke_account(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> RevokeGmailAccountResponse:
        self.calls.append(("revoke_account", (actor_id, account_id, command_id, reason, now)))
        return RevokeGmailAccountResponse(
            account=_account(actor_id=actor_id, account_id=account_id, status="revoked")
        )


class ErroringGmailReadonlyProvider(RecordingGmailReadonlyProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GmailAccountStatusResponse:
        self.calls.append(("account_status", (actor_id, account_id)))
        raise self._error


def test_gmail_readonly_routes_are_injected_without_write_mail_routes() -> None:
    client, _provider = _client(RecordingGmailReadonlyProvider())

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/internal/gmail-readonly/accounts" in paths
    assert "/api/v1/internal/gmail-readonly/accounts/{account_id}/syncs" in paths
    assert "/api/v1/internal/gmail-readonly/accounts/{account_id}/sync-runs" in paths
    assert "/api/v1/internal/gmail-readonly/accounts/{account_id}/proposals" in paths
    assert (
        "/api/v1/internal/gmail-readonly/accounts/{account_id}/proposals/{proposal_id}/review"
        in paths
    )
    assert "/api/v1/internal/gmail-readonly/accounts/{account_id}/reset-history" in paths
    assert "/api/v1/internal/gmail-readonly/accounts/{account_id}/revoke" in paths
    readonly_paths = [path for path in paths if path.startswith("/api/v1/internal/gmail-readonly")]
    assert not any("send" in path or "draft" in path or "modify" in path for path in readonly_paths)


def test_gmail_readonly_requires_authenticated_session() -> None:
    provider = RecordingGmailReadonlyProvider()
    app = _app(provider)

    response = TestClient(app).get("/api/v1/internal/gmail-readonly/accounts")

    assert response.status_code == 401
    assert provider.calls == []


def test_gmail_readonly_rejects_read_with_bad_host() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    response = client.get(
        "/api/v1/internal/gmail-readonly/accounts",
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 403
    assert provider.calls == []


def test_gmail_readonly_registers_dedicated_byo_account_without_scope_or_secret_inputs() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    response = client.post(
        "/api/v1/internal/gmail-readonly/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REGISTER_COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail/owner@example.com",
            "account_subject": "owner@example.com",
            "dedicated": True,
            "oauth_client_mode": "byo",
            "publishing_status": "testing",
            "credential_store_evidence_sha256": EVIDENCE_SHA256,
        },
    )
    rejected = client.post(
        "/api/v1/internal/gmail-readonly/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REGISTER_COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail/owner@example.com",
            "account_subject": "owner@example.com",
            "scope": "https://www.googleapis.com/auth/gmail.send",
            "token": "plaintext-token",
            "client_secret": "plaintext-secret",
            "refresh_token": "plaintext-refresh",
            "label_ids": ["INBOX"],
            "query": "subject:job",
            "watch_topic_name": "projects/x/topics/y",
        },
    )
    legacy_production = client.post(
        "/api/v1/internal/gmail-readonly/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REGISTER_COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail/owner@example.com",
            "account_subject": "owner@example.com",
            "dedicated": True,
            "oauth_client_mode": "byo",
            "publishing_status": "production",
            "credential_store_evidence_sha256": EVIDENCE_SHA256,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["account"]["readonly_scope"] == "https://www.googleapis.com/auth/gmail.readonly"
    assert body["account"]["sync_mode"] == "polling"
    assert body["account"]["dedicated"] is True
    assert body["account"]["oauth_client_mode"] == "byo"
    assert body["account"]["publishing_status"] == "testing"
    assert body["account"]["snapshot_sha256"] == SNAPSHOT_SHA256
    assert _contains_no_secret_fields(body)
    assert rejected.status_code == 422
    assert legacy_production.status_code == 422
    register = cast(tuple[object, ...], provider.calls[0][1])
    assert register[0:9] == (
        ACTOR_ID,
        REGISTER_COMMAND_ID,
        CANDIDATE_ID,
        "vault:gmail/owner@example.com",
        "owner@example.com",
        True,
        "byo",
        "testing",
        EVIDENCE_SHA256,
    )
    assert len(provider.calls) == 1


def test_gmail_readonly_limits_match_database_function_bounds() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    response = client.get("/api/v1/internal/gmail-readonly/accounts?limit=101")

    assert response.status_code == 400
    assert provider.calls == []


def test_gmail_readonly_forwards_status_sync_proposal_reset_and_revoke() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    list_accounts = client.get("/api/v1/internal/gmail-readonly/accounts?limit=5")
    status_response = client.get(f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}")
    sync_response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/syncs",
        headers=MUTATION_HEADERS,
        json={"command_id": str(SYNC_COMMAND_ID), "reason": "manual"},
    )
    runs_response = client.get(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/sync-runs?limit=7"
    )
    proposals_response = client.get(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/proposals?limit=9"
    )
    review_response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/proposals/{PROPOSAL_ID}/review",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REVIEW_COMMAND_ID),
            "decision": "approve",
            "snapshot_sha256": SNAPSHOT_SHA256,
            "canonical_job_id": str(CANONICAL_JOB_ID),
            "job_posting_id": str(JOB_POSTING_ID),
            "reason": "matches application row",
        },
    )
    reset_response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/reset-history",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(RESET_COMMAND_ID),
            "snapshot_sha256": SNAPSHOT_SHA256,
            "reason": "gmail history expired; request full sync",
        },
    )
    bad_reset = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/reset-history",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(RESET_COMMAND_ID),
            "snapshot_sha256": SNAPSHOT_SHA256,
            "provider_history_id": "999999",
        },
    )
    revoke_response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/revoke",
        headers=MUTATION_HEADERS,
        json={"command_id": str(REVOKE_COMMAND_ID), "reason": "operator disconnect"},
    )

    assert list_accounts.status_code == 200
    assert status_response.status_code == 200
    assert sync_response.status_code == 202
    assert runs_response.status_code == 200
    assert proposals_response.status_code == 200
    assert review_response.status_code == 200
    assert reset_response.status_code == 200
    assert bad_reset.status_code == 422
    assert revoke_response.status_code == 200
    assert _contains_no_secret_fields(review_response.json())
    assert [call[0] for call in provider.calls] == [
        "list_accounts",
        "account_status",
        "request_sync",
        "list_sync_runs",
        "list_proposals",
        "review_proposal",
        "reset_history",
        "revoke_account",
    ]
    assert cast(tuple[object, ...], provider.calls[0][1]) == (ACTOR_ID, 5)
    assert cast(tuple[object, ...], provider.calls[1][1]) == (ACTOR_ID, ACCOUNT_ID)
    assert cast(tuple[object, ...], provider.calls[2][1])[0:4] == (
        ACTOR_ID,
        ACCOUNT_ID,
        SYNC_COMMAND_ID,
        "manual",
    )
    assert cast(tuple[object, ...], provider.calls[3][1]) == (ACTOR_ID, ACCOUNT_ID, 7)
    assert cast(tuple[object, ...], provider.calls[4][1]) == (ACTOR_ID, ACCOUNT_ID, 9)
    review = cast(tuple[object, ...], provider.calls[5][1])
    assert review[0:8] == (
        ACTOR_ID,
        ACCOUNT_ID,
        PROPOSAL_ID,
        REVIEW_COMMAND_ID,
        "approve",
        SNAPSHOT_SHA256,
        CANONICAL_JOB_ID,
        JOB_POSTING_ID,
    )
    assert review[8] == "matches application row"
    assert cast(tuple[object, ...], provider.calls[6][1])[0:5] == (
        ACTOR_ID,
        ACCOUNT_ID,
        RESET_COMMAND_ID,
        SNAPSHOT_SHA256,
        "gmail history expired; request full sync",
    )
    assert cast(tuple[object, ...], provider.calls[7][1])[0:4] == (
        ACTOR_ID,
        ACCOUNT_ID,
        REVOKE_COMMAND_ID,
        "operator disconnect",
    )


def test_gmail_readonly_rejects_mutation_without_csrf_header() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/syncs",
        headers={"Origin": "http://testserver"},
        json={"command_id": str(SYNC_COMMAND_ID)},
    )

    assert response.status_code == 422
    assert provider.calls == []


def test_gmail_readonly_rejects_mutation_with_bad_origin() -> None:
    client, provider = _client(RecordingGmailReadonlyProvider())

    response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/syncs",
        headers={"Origin": "http://attacker.example", "X-CSRF-Token": "authenticated-csrf"},
        json={"command_id": str(SYNC_COMMAND_ID)},
    )

    assert response.status_code == 403
    assert provider.calls == []


def test_gmail_readonly_maps_not_found_conflict_and_unavailable_safely() -> None:
    for error, expected_status in (
        (GmailReadonlyNotFound("cross owner"), 404),
        (GmailReadonlyConflict("bad state"), 409),
        (GmailReadonlyUnavailable("gmail unavailable"), 503),
    ):
        client, provider = _client(ErroringGmailReadonlyProvider(error))

        response = client.get(f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}")

        assert response.status_code == expected_status
        assert provider.calls == [("account_status", (ACTOR_ID, ACCOUNT_ID))]
        assert "cross owner" not in response.text
        if expected_status == 409:
            assert "bad state" not in response.text
        if expected_status == 503:
            assert "gmail unavailable" not in response.text


def test_gmail_readonly_disabled_provider_returns_unavailable() -> None:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    app = create_app(
        settings,
        readiness_probe=FixedReadinessProbe(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )
    client = TestClient(app)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")

    response = client.get("/api/v1/internal/gmail-readonly/accounts")

    assert response.status_code == 503


def test_runtime_provider_binds_control_plane_before_google_oauth_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    class RecordingRuntimeProvider(DisabledGmailReadonlyOperatorProvider):
        def __init__(self, *, transaction_factory: object) -> None:
            created.append(transaction_factory)

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(
        app_module,
        "RuntimeGmailReadonlyOperatorProvider",
        RecordingRuntimeProvider,
    )
    enabled_resources = FixedRuntimeResources()
    enabled = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "google_oauth_enabled": True,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )

    app_module.create_app(
        enabled,
        readiness_probe=enabled_resources,
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )

    disabled_resources = FixedRuntimeResources()
    disabled = enabled.model_copy(update={"google_oauth_enabled": False})
    app_module.create_app(
        disabled,
        readiness_probe=disabled_resources,
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )

    assert created == [enabled_resources.database.begin, disabled_resources.database.begin]


def test_readonly_account_registration_stays_available_before_google_oauth_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[RecordingGmailReadonlyProvider] = []

    class RecordingRuntimeProvider(RecordingGmailReadonlyProvider):
        def __init__(self, *, transaction_factory: object) -> None:
            super().__init__()
            self.transaction_factory = transaction_factory
            providers.append(self)

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(
        app_module,
        "RuntimeGmailReadonlyOperatorProvider",
        RecordingRuntimeProvider,
    )
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "google_oauth_enabled": False,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    app = app_module.create_app(
        settings,
        readiness_probe=FixedRuntimeResources(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")

    register_response = client.post(
        "/api/v1/internal/gmail-readonly/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REGISTER_COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail-readonly/owner@example.com",
            "account_subject": "owner@example.com",
            "dedicated": True,
            "oauth_client_mode": "byo",
            "publishing_status": "testing",
            "credential_store_evidence_sha256": EVIDENCE_SHA256,
        },
    )
    sync_response = client.post(
        f"/api/v1/internal/gmail-readonly/accounts/{ACCOUNT_ID}/syncs",
        headers=MUTATION_HEADERS,
        json={"command_id": str(SYNC_COMMAND_ID), "reason": "manual"},
    )

    assert register_response.status_code == 202
    assert register_response.json()["account"]["account_subject"] == "owner@example.com"
    assert sync_response.status_code == 503
    assert len(providers[0].calls) == 1
    call_name, call = providers[0].calls[0]
    assert call_name == "register"
    register_call = cast(tuple[object, ...], call)
    assert register_call[0:5] == (
        ACTOR_ID,
        REGISTER_COMMAND_ID,
        CANDIDATE_ID,
        "vault:gmail-readonly/owner@example.com",
        "owner@example.com",
    )


def _account(
    *,
    actor_id: UUID,
    candidate_id: UUID = CANDIDATE_ID,
    account_id: UUID = ACCOUNT_ID,
    account_subject: str = "owner@example.com",
    status: Literal["active", "paused", "sync_required", "revoked", "blocked"] = "active",
) -> GmailAccountSummary:
    return GmailAccountSummary(
        account_id=account_id,
        owner_user_id=actor_id,
        candidate_id=candidate_id,
        account_subject=account_subject,
        status=status,
        publishing_status="testing",
        snapshot_sha256=SNAPSHOT_SHA256,
        last_history_id="42",
        last_synced_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _run(
    *,
    actor_id: UUID,
    account_id: UUID,
    reason: Literal["manual", "history_expired", "scheduled", "recovery"] = "manual",
) -> GmailSyncRunSummary:
    return GmailSyncRunSummary(
        run_id=RUN_ID,
        account_id=account_id,
        owner_user_id=actor_id,
        reason=reason,
        status="queued",
        requested_at=NOW,
        message_count=0,
    )


def _proposal(
    *,
    actor_id: UUID,
    account_id: UUID,
    proposal_id: UUID = PROPOSAL_ID,
    canonical_job_id: UUID | None = None,
    job_posting_id: UUID | None = None,
    proposal_status: Literal["pending_review", "approved", "rejected"] = "pending_review",
) -> GmailProposalSummary:
    return GmailProposalSummary(
        proposal_id=proposal_id,
        account_id=account_id,
        owner_user_id=actor_id,
        signal_id=SIGNAL_ID,
        proposal_status=proposal_status,
        proposal_kind="application_status_update",
        review_priority="high",
        classification="interview_invitation",
        relevance="relevant",
        confidence=0.95,
        signal_sha256=EVIDENCE_SHA256,
        payload_sha256=SNAPSHOT_SHA256,
        redacted_excerpt="Recruiter requested interview availability.",
        payload_snapshot={
            "version": "gmail-readonly-proposal.v1",
            "classification": "interview_invitation",
        },
        canonical_job_id=canonical_job_id,
        job_posting_id=job_posting_id,
        created_at=NOW,
    )


def _app(provider: GmailReadonlyOperatorProvider):
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    return create_app(
        settings,
        readiness_probe=FixedReadinessProbe(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
        gmail_readonly_provider=provider,
    )


def _client(
    provider: GmailReadonlyOperatorProvider,
) -> tuple[TestClient, RecordingGmailReadonlyProvider]:
    app = _app(provider)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")
    return client, cast(RecordingGmailReadonlyProvider, provider)


def _contains_no_secret_fields(value: object) -> bool:
    forbidden = {
        "token",
        "client_secret",
        "refresh_token",
        "secret_handle",
        "credential_handle",
        "credential_reference_id",
        "oauth_credential_reference_id",
        "idempotency_key",
        "trace_id",
        "fencing_token",
        "lease_token",
    }
    if isinstance(value, dict):
        return all(
            key not in forbidden and _contains_no_secret_fields(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_contains_no_secret_fields(item) for item in value)
    return True
