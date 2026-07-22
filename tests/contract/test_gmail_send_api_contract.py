from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import careerops.api.app as app_module
from careerops.api.app import create_app
from careerops.api.gmail_send import (
    GMAIL_SEND_EXACT_APPROVAL_KIND,
    CreateGmailSendDraftResponse,
    DisabledGmailSendOperatorProvider,
    GmailSendAccountStatus,
    GmailSendAccountStatusResponse,
    GmailSendAccountSummary,
    GmailSendConflict,
    GmailSendNotFound,
    GmailSendOperatorProvider,
    GmailSendUnavailable,
    ListGmailSendAccountsResponse,
    RegisterGmailSendAccountResponse,
    ReserveGmailSendIntentResponse,
    ReviewGmailSendDraftResponse,
)
from careerops.application.dashboard import DashboardSnapshot, DashboardSystemStatus
from careerops.application.gmail_send import (
    GMAIL_SEND_ADAPTER_ID,
    GMAIL_SEND_CHANNEL,
    GMAIL_SEND_FIXTURE_ID,
    GMAIL_SEND_TARGET_HOST,
    GmailSendPayload,
)
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.auth.contracts import AuthenticatedPrincipal, AuthRequestContext, SessionSecrets
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(hours=1)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000001201")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000001202")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000001203")
INTENT_ID = UUID("00000000-0000-0000-0000-000000001204")
PAYLOAD_VERSION_ID = UUID("00000000-0000-0000-0000-000000001205")
POLICY_DECISION_ID = UUID("00000000-0000-0000-0000-000000001206")
APPROVAL_REQUEST_ID = UUID("00000000-0000-0000-0000-000000001207")
CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000001208")
GRANT_VERSION_ID = UUID("00000000-0000-0000-0000-000000001209")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-00000000120a")
RESERVATION_ID = UUID("00000000-0000-0000-0000-00000000120b")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-00000000120c")
COMMAND_ID = UUID("00000000-0000-0000-0000-00000000120d")
RELEASE_QUALIFICATION_ID = UUID("00000000-0000-0000-0000-00000000120e")
SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64
SHA256_D = "d" * 64
SHA256_E = "e" * 64
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
        self.database = SimpleNamespace(begin=object())
        self.redis_sync = object()


class FixedConsoleAuthService:
    def begin_preauth(self, *, now: datetime, context: AuthRequestContext) -> SessionSecrets:
        assert now and context.trace_id
        return self._secrets()

    def complete_bootstrap(self, **_kwargs: object) -> SessionSecrets:
        return self._secrets()

    def login(self, **_kwargs: object) -> SessionSecrets:
        return self._secrets()

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        assert now.tzinfo is not None
        if session_token != "authenticated-session":
            raise ValueError("invalid session")
        return AuthenticatedPrincipal(
            user_id=ACTOR_ID,
            username="owner",
            session_id=UUID("00000000-0000-0000-0000-00000000120e"),
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
    def _secrets() -> SessionSecrets:
        return SessionSecrets(
            session_id=UUID("00000000-0000-0000-0000-00000000120e"),
            token="authenticated-session",
            csrf_token="authenticated-csrf",
            absolute_expires_at=NOW + timedelta(days=7),
        )


class FixedDashboardProvider:
    async def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            system_status=DashboardSystemStatus.READY,
            dependency_checks=(("database", "ok"),),
            integration_checks=(("gmail_send", "disabled"),),
            pending_approvals=0,
            pending_outbox_events=0,
        )


class RecordingGmailSendProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        reconciliation_gmail_account_id: UUID | None,
        credential_handle: str,
        account_subject: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: GmailSendAccountStatus,
        daily_send_limit: int,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        now: datetime,
    ) -> RegisterGmailSendAccountResponse:
        self.calls.append(("register", locals() | {"self": None}))
        return RegisterGmailSendAccountResponse(
            account=_account(account_subject=account_subject, status=requested_status),
            receipt_state="created",
        )

    async def list_accounts(self, *, actor_id: UUID, limit: int) -> ListGmailSendAccountsResponse:
        self.calls.append(("list_accounts", {"actor_id": actor_id, "limit": limit}))
        return ListGmailSendAccountsResponse(accounts=(_account(),))

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GmailSendAccountStatusResponse:
        self.calls.append(("account_status", {"actor_id": actor_id, "account_id": account_id}))
        return GmailSendAccountStatusResponse(account=_account())

    async def create_draft(self, **kwargs: object) -> CreateGmailSendDraftResponse:
        self.calls.append(("create_draft", kwargs))
        return CreateGmailSendDraftResponse(
            action_intent_id=INTENT_ID,
            payload_version_id=PAYLOAD_VERSION_ID,
            payload_hash=SHA256_D,
            policy_decision_id=POLICY_DECISION_ID,
            approval_request_id=APPROVAL_REQUEST_ID,
            receipt_state="created",
        )

    async def review_draft(self, **kwargs: object) -> ReviewGmailSendDraftResponse:
        self.calls.append(("review_draft", kwargs))
        return ReviewGmailSendDraftResponse(
            approval_request_id=APPROVAL_REQUEST_ID,
            policy_decision_id=POLICY_DECISION_ID,
            authorization_id=AUTHORIZATION_ID,
            decision=cast(Literal["approved", "rejected"], kwargs["decision"]),
            receipt_state="created",
        )

    async def reserve_intent(self, **kwargs: object) -> ReserveGmailSendIntentResponse:
        self.calls.append(("reserve_intent", kwargs))
        return ReserveGmailSendIntentResponse(
            account_id=ACCOUNT_ID,
            reservation_id=RESERVATION_ID,
            outbox_event_id=OUTBOX_EVENT_ID,
            event_key="gmail-send:reservation-1",
            receipt_state="created",
        )


class ErroringGmailSendProvider(RecordingGmailSendProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GmailSendAccountStatusResponse:
        self.calls.append(("account_status", {"actor_id": actor_id, "account_id": account_id}))
        raise self._error


def test_gmail_send_routes_are_migration_backed_without_raw_send_route() -> None:
    client, _provider = _client(RecordingGmailSendProvider())

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/internal/gmail-send/accounts" in paths
    assert "/api/v1/internal/gmail-send/drafts" in paths
    assert "/api/v1/internal/gmail-send/drafts/{approval_request_id}/review" in paths
    assert "/api/v1/internal/gmail-send/accounts/{account_id}/reservations" in paths
    assert not any(path.endswith("/send") for path in paths)
    assert "/api/v1/internal/gmail-send/accounts/{account_id}/drafts" not in paths


def test_gmail_send_account_registration_rejects_secrets_and_does_not_echo_handle() -> None:
    client, provider = _client(RecordingGmailSendProvider())

    response = client.post(
        "/api/v1/internal/gmail-send/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "reconciliation_gmail_account_id": str(ACCOUNT_ID),
            "credential_handle": "vault:gmail-send/owner@example.com",
            "account_subject": "owner@example.com",
            "credential_store_evidence_sha256": SHA256_A,
            "release_evidence_sha256": SHA256_B,
            "status": "active",
            "daily_send_limit": 25,
            "dedicated": True,
            "oauth_client_mode": "byo",
        },
    )
    rejected = client.post(
        "/api/v1/internal/gmail-send/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail-send/owner@example.com",
            "account_subject": "owner@example.com",
            "token": "plaintext",
            "client_secret": "plaintext",
            "refresh_token": "plaintext",
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["account"]["send_scope"] == "https://www.googleapis.com/auth/gmail.send"
    assert body["account"]["send_mode"] == "reviewed_send_only"
    assert body["account"]["status"] == "active"
    assert body["receipt_state"] == "created"
    assert _contains_no_secret_fields(body)
    assert rejected.status_code == 422
    register = cast(dict[str, object], provider.calls[0][1])
    assert register["credential_handle"] == "vault:gmail-send/owner@example.com"
    assert register["reconciliation_gmail_account_id"] == ACCOUNT_ID
    assert register["release_evidence_sha256"] == SHA256_B
    assert len(provider.calls) == 1


def test_gmail_send_active_account_requires_readonly_reconciliation_binding() -> None:
    client, provider = _client(RecordingGmailSendProvider())

    response = client.post(
        "/api/v1/internal/gmail-send/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "credential_handle": "vault:gmail-send/owner@example.com",
            "account_subject": "owner@example.com",
            "credential_store_evidence_sha256": SHA256_A,
            "release_evidence_sha256": SHA256_B,
            "status": "active",
            "daily_send_limit": 25,
            "dedicated": True,
            "oauth_client_mode": "byo",
        },
    )

    assert response.status_code == 422
    assert provider.calls == []


def test_gmail_send_draft_review_and_reservation_are_exact_payload_gated() -> None:
    client, provider = _client(RecordingGmailSendProvider())

    create_response = client.post(
        "/api/v1/internal/gmail-send/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload(),
    )
    review_response = client.post(
        f"/api/v1/internal/gmail-send/drafts/{APPROVAL_REQUEST_ID}/review",
        headers=MUTATION_HEADERS,
        json=_review_payload(),
    )
    generic_approval = client.post(
        f"/api/v1/internal/gmail-send/drafts/{APPROVAL_REQUEST_ID}/review",
        headers=MUTATION_HEADERS,
        json=_review_payload() | {"requested_for": "generic_send"},
    )
    reserve_response = client.post(
        f"/api/v1/internal/gmail-send/accounts/{ACCOUNT_ID}/reservations",
        headers=MUTATION_HEADERS,
        json=_reserve_payload(),
    )

    assert create_response.status_code == 202
    assert review_response.status_code == 200
    assert generic_approval.status_code == 422
    assert reserve_response.status_code == 202
    assert _contains_no_secret_fields(create_response.json())
    assert _contains_no_secret_fields(reserve_response.json())
    assert [call[0] for call in provider.calls] == [
        "create_draft",
        "review_draft",
        "reserve_intent",
    ]
    create_call = cast(dict[str, object], provider.calls[0][1])
    review_call = cast(dict[str, object], provider.calls[1][1])
    reserve_call = cast(dict[str, object], provider.calls[2][1])
    assert create_call["requested_for"] == "gmail_send_exact_payload"
    assert review_call["requested_for"] == "gmail_send_exact_payload"
    assert reserve_call["reviewed_by_user_id"] == ACTOR_ID
    assert reserve_call["release_qualification_id"] == RELEASE_QUALIFICATION_ID
    assert "release_evidence_hash" not in reserve_call
    assert "release_evidence_expires_at" not in reserve_call
    assert reserve_call["reservation_key"] == "reservation-1"
    canonical = _draft_domain_payload()
    assert create_call["payload_hash"] is None
    assert create_call["target"] == dict(canonical.canonical_target()) | {
        "target_host": GMAIL_SEND_TARGET_HOST,
        "channel": GMAIL_SEND_CHANNEL,
        "adapter_id": GMAIL_SEND_ADAPTER_ID,
        "fixture_id": GMAIL_SEND_FIXTURE_ID,
    }
    assert create_call["payload"] == dict(canonical.canonical_without_hash()) | {
        "payload_hash": canonical.payload_hash,
        "message_id_header": canonical.message_id_header,
    }
    assert create_call["attachment_refs"] == ()


def test_gmail_send_requires_session_csrf_origin_limits_and_safe_errors() -> None:
    client, provider = _client(RecordingGmailSendProvider())
    unauthenticated = TestClient(_app(RecordingGmailSendProvider())).get(
        "/api/v1/internal/gmail-send/accounts"
    )
    bad_origin = client.post(
        f"/api/v1/internal/gmail-send/accounts/{ACCOUNT_ID}/reservations",
        headers={"Origin": "http://attacker.example", "X-CSRF-Token": "authenticated-csrf"},
        json={},
    )
    bad_limit = client.get("/api/v1/internal/gmail-send/accounts?limit=101")

    assert unauthenticated.status_code == 401
    assert bad_origin.status_code == 403
    assert bad_limit.status_code == 400
    assert provider.calls == []

    for error, expected_status in (
        (GmailSendNotFound("cross owner"), 404),
        (GmailSendConflict("bad state"), 409),
        (GmailSendUnavailable("send unavailable"), 503),
    ):
        error_client, error_provider = _client(ErroringGmailSendProvider(error))

        response = error_client.get(f"/api/v1/internal/gmail-send/accounts/{ACCOUNT_ID}")

        assert response.status_code == expected_status
        assert error_provider.calls == [
            ("account_status", {"actor_id": ACTOR_ID, "account_id": ACCOUNT_ID})
        ]
        assert "cross owner" not in response.text
        assert "bad state" not in response.text
        assert "send unavailable" not in response.text


def test_gmail_send_disabled_provider_returns_unavailable() -> None:
    client, _provider = _client(DisabledGmailSendOperatorProvider())

    response = client.get("/api/v1/internal/gmail-send/accounts")

    assert response.status_code == 503


def test_runtime_provider_binds_control_plane_before_send_execution_runtime(monkeypatch) -> None:
    created: list[object] = []

    class RecordingRuntimeProvider(DisabledGmailSendOperatorProvider):
        def __init__(self, *, transaction_factory: object) -> None:
            created.append(transaction_factory)

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(app_module, "RuntimeGmailSendOperatorProvider", RecordingRuntimeProvider)
    resources = FixedRuntimeResources()
    enabled = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "external_writes_enabled": True,
            "auto_send_enabled": True,
            "google_oauth_enabled": True,
            "gmail_send_enabled": True,
            "gmail_send_release_attested": True,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )

    enabled_resources = resources
    create_app(
        enabled,
        readiness_probe=enabled_resources,
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )
    disabled_resources = FixedRuntimeResources()
    disabled = enabled.model_copy(
        update={
            "external_writes_enabled": False,
            "auto_send_enabled": False,
            "gmail_send_enabled": False,
            "gmail_send_release_attested": False,
            "google_oauth_enabled": False,
        }
    )
    create_app(
        disabled,
        readiness_probe=disabled_resources,
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )
    prerequisite_resources: list[FixedRuntimeResources] = []
    for missing_prerequisite in (
        {"google_oauth_enabled": False},
        {"mailbox_broker_socket": None},
        {"gmail_send_attachment_broker_socket": None},
    ):
        prerequisite_resource = FixedRuntimeResources()
        prerequisite_resources.append(prerequisite_resource)
        create_app(
            enabled.model_copy(update=missing_prerequisite),
            readiness_probe=prerequisite_resource,
            console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
            dashboard_provider=FixedDashboardProvider(),
        )

    assert created == [
        enabled_resources.database.begin,
        disabled_resources.database.begin,
        *(resource.database.begin for resource in prerequisite_resources),
    ]


def test_disabled_send_account_registration_stays_available_before_execution_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers: list[RecordingGmailSendProvider] = []

    class RecordingRuntimeProvider(RecordingGmailSendProvider):
        def __init__(self, *, transaction_factory: object) -> None:
            super().__init__()
            self.transaction_factory = transaction_factory
            providers.append(self)

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(app_module, "RuntimeGmailSendOperatorProvider", RecordingRuntimeProvider)
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "external_writes_enabled": False,
            "auto_send_enabled": False,
            "google_oauth_enabled": False,
            "gmail_send_enabled": False,
            "gmail_send_release_attested": False,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    app = create_app(
        settings,
        readiness_probe=FixedRuntimeResources(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
    )
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")

    register_response = client.post(
        "/api/v1/internal/gmail-send/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "reconciliation_gmail_account_id": str(ACCOUNT_ID),
            "credential_handle": "vault:gmail-send/owner@example.com",
            "account_subject": "owner@example.com",
            "credential_store_evidence_sha256": SHA256_A,
            "release_evidence_sha256": SHA256_B,
            "status": "disabled",
            "daily_send_limit": 25,
            "dedicated": True,
            "oauth_client_mode": "byo",
        },
    )
    active_register_response = client.post(
        "/api/v1/internal/gmail-send/accounts",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(COMMAND_ID),
            "candidate_id": str(CANDIDATE_ID),
            "reconciliation_gmail_account_id": str(ACCOUNT_ID),
            "credential_handle": "vault:gmail-send/owner@example.com",
            "account_subject": "owner@example.com",
            "credential_store_evidence_sha256": SHA256_A,
            "release_evidence_sha256": SHA256_B,
            "status": "active",
            "daily_send_limit": 25,
            "dedicated": True,
            "oauth_client_mode": "byo",
        },
    )
    draft_response = client.post(
        "/api/v1/internal/gmail-send/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload(),
    )

    assert register_response.status_code == 202
    assert register_response.json()["account"]["status"] == "disabled"
    assert active_register_response.status_code == 503
    assert draft_response.status_code == 503
    assert len(providers[0].calls) == 1
    call_name, call = providers[0].calls[0]
    assert call_name == "register"
    register_call = cast(dict[str, object], call)
    assert register_call["actor_id"] == ACTOR_ID
    assert register_call["requested_status"] == "disabled"
    assert register_call["credential_handle"] == "vault:gmail-send/owner@example.com"


def _account(
    *,
    account_subject: str = "owner@example.com",
    status: GmailSendAccountStatus = "active",
) -> GmailSendAccountSummary:
    return GmailSendAccountSummary(
        account_id=ACCOUNT_ID,
        account_subject=account_subject,
        status=status,
        daily_send_limit=25,
        credential_status="active",
        updated_at=NOW,
    )


def _create_draft_payload() -> dict[str, object]:
    payload = _draft_domain_payload()
    return {
        "command_id": str(COMMAND_ID),
        "candidate_id": str(CANDIDATE_ID),
        "resource_id": str(CANDIDATE_ID),
        "sender": payload.sender,
        "recipient": payload.recipient,
        "subject": payload.subject,
        "text_body": payload.text_body,
        "attachment_refs": [],
        "payload_hash": payload.payload_hash,
        "ruleset_version": "gmail-send-rules.v1",
        "decision_rule_reference": "exact payload review required",
        "expires_at": EXPIRES_AT.isoformat(),
        "requested_for": GMAIL_SEND_EXACT_APPROVAL_KIND,
    }


def _draft_domain_payload() -> GmailSendPayload:
    return GmailSendPayload(
        sender="owner@example.com",
        recipient="recruiter@example.com",
        subject="Application",
        text_body="Reviewed body",
    )


def _review_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "action_intent_id": str(INTENT_ID),
        "payload_version_id": str(PAYLOAD_VERSION_ID),
        "campaign_id": str(CAMPAIGN_ID),
        "grant_version_id": str(GRANT_VERSION_ID),
        "payload_hash": SHA256_D,
        "decision": "approved",
        "authorization_id": str(AUTHORIZATION_ID),
        "review_snapshot_sha256": SHA256_E,
        "authorization_expires_at": EXPIRES_AT.isoformat(),
        "reason": "exact payload approved",
        "requested_for": "gmail_send_exact_payload",
    }


def _reserve_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "campaign_id": str(CAMPAIGN_ID),
        "grant_version_id": str(GRANT_VERSION_ID),
        "authorization_id": str(AUTHORIZATION_ID),
        "action_intent_id": str(INTENT_ID),
        "payload_version_id": str(PAYLOAD_VERSION_ID),
        "payload_hash": SHA256_D,
        "recipient_sha256": SHA256_A,
        "approval_request_id": str(APPROVAL_REQUEST_ID),
        "review_evidence_sha256": SHA256_B,
        "review_snapshot_sha256": SHA256_E,
        "release_qualification_id": str(RELEASE_QUALIFICATION_ID),
        "reservation_key": "reservation-1",
        "reconciliation_key": "reconciliation-1",
        "subject_sha256": SHA256_B,
        "body_sha256": SHA256_C,
    }


def _app(provider: GmailSendOperatorProvider):
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
        gmail_send_provider=provider,
    )


def _client(provider: GmailSendOperatorProvider) -> tuple[TestClient, RecordingGmailSendProvider]:
    client = TestClient(_app(provider), follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")
    return client, cast(RecordingGmailSendProvider, provider)


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
