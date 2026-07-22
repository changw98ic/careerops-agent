from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.api.greenhouse_submit import (
    GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND,
    CreateGreenhouseSubmitDraftResponse,
    DisabledGreenhouseSubmitOperatorProvider,
    GreenhouseSubmitAccountStatus,
    GreenhouseSubmitAccountStatusResponse,
    GreenhouseSubmitAccountSummary,
    GreenhouseSubmitConflict,
    GreenhouseSubmitNotFound,
    GreenhouseSubmitOperatorProvider,
    GreenhouseSubmitReconciliationCaseStatusResponse,
    GreenhouseSubmitReconciliationCaseSummary,
    GreenhouseSubmitUnavailable,
    ListGreenhouseSubmitAccountsResponse,
    ListGreenhouseSubmitReconciliationCasesResponse,
    RegisterGreenhouseSubmitAccountResponse,
    ReserveGreenhouseSubmitIntentResponse,
    ReviewGreenhouseReconciliationEvidenceResponse,
    ReviewGreenhouseSubmitDraftResponse,
    SubmitGreenhouseReconciliationEvidenceResponse,
    create_greenhouse_submit_operator_router,
)
from careerops.auth.contracts import AuthenticatedPrincipal, AuthRequestContext, SessionSecrets
from careerops.web.security import ConsoleWebSettings

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(hours=1)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000002201")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000002202")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000002203")
INTENT_ID = UUID("00000000-0000-0000-0000-000000002204")
PAYLOAD_VERSION_ID = UUID("00000000-0000-0000-0000-000000002205")
POLICY_DECISION_ID = UUID("00000000-0000-0000-0000-000000002206")
APPROVAL_REQUEST_ID = UUID("00000000-0000-0000-0000-000000002207")
CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000002208")
GRANT_VERSION_ID = UUID("00000000-0000-0000-0000-000000002209")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-00000000220a")
RESERVATION_ID = UUID("00000000-0000-0000-0000-00000000220b")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-00000000220c")
COMMAND_ID = UUID("00000000-0000-0000-0000-00000000220d")
RELEASE_QUALIFICATION_ID = UUID("00000000-0000-0000-0000-00000000220e")
RECONCILIATION_CASE_ID = UUID("00000000-0000-0000-0000-00000000220f")
EVIDENCE_REVIEW_ID = UUID("00000000-0000-0000-0000-000000002210")
SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64
SHA256_D = "d" * 64
SHA256_E = "e" * 64
SHA256_F = "f" * 64
MUTATION_HEADERS = {"Origin": "http://testserver", "X-CSRF-Token": "authenticated-csrf"}


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
            session_id=UUID("00000000-0000-0000-0000-000000002211"),
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
            session_id=UUID("00000000-0000-0000-0000-000000002211"),
            token="authenticated-session",
            csrf_token="authenticated-csrf",
            absolute_expires_at=NOW + timedelta(days=7),
        )


class RecordingGreenhouseSubmitProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        employer_id: str,
        board_token: str,
        account_subject: str,
        opaque_credential_handle: str,
        credential_profile_id: str,
        credential_profile_version: int,
        credential_fingerprint_sha256: str,
        credential_profile_status: Literal["active", "disabled", "expired", "revoked"],
        credential_profile_expires_at: datetime,
        employer_authorization_evidence_sha256: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: GreenhouseSubmitAccountStatus,
        daily_submit_limit: int,
        now: datetime,
    ) -> RegisterGreenhouseSubmitAccountResponse:
        self.calls.append(("register", locals() | {"self": None}))
        return RegisterGreenhouseSubmitAccountResponse(
            account=_account(
                candidate_id=candidate_id,
                employer_id=employer_id,
                board_token_sha256=hashlib.sha256(board_token.encode("utf-8")).hexdigest(),
                account_subject=account_subject,
                status=requested_status,
                credential_profile_id=credential_profile_id,
                credential_profile_version=credential_profile_version,
                credential_fingerprint_sha256=credential_fingerprint_sha256,
                credential_profile_status=credential_profile_status,
                credential_profile_expires_at=credential_profile_expires_at,
                employer_authorization_evidence_sha256=employer_authorization_evidence_sha256,
                daily_submit_limit=daily_submit_limit,
            ),
            receipt_state="created",
        )

    async def list_accounts(
        self, *, actor_id: UUID, limit: int
    ) -> ListGreenhouseSubmitAccountsResponse:
        self.calls.append(("list_accounts", {"actor_id": actor_id, "limit": limit}))
        return ListGreenhouseSubmitAccountsResponse(accounts=(_account(),))

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GreenhouseSubmitAccountStatusResponse:
        self.calls.append(("account_status", {"actor_id": actor_id, "account_id": account_id}))
        return GreenhouseSubmitAccountStatusResponse(account=_account())

    async def create_draft(self, **kwargs: object) -> CreateGreenhouseSubmitDraftResponse:
        self.calls.append(("create_draft", kwargs))
        return CreateGreenhouseSubmitDraftResponse(
            action_intent_id=INTENT_ID,
            payload_version_id=PAYLOAD_VERSION_ID,
            payload_hash=SHA256_D,
            material_hash=SHA256_E,
            submission_identity_sha256=SHA256_F,
            review_snapshot_sha256=SHA256_C,
            policy_decision_id=POLICY_DECISION_ID,
            approval_request_id=APPROVAL_REQUEST_ID,
            receipt_state="created",
        )

    async def review_draft(self, **kwargs: object) -> ReviewGreenhouseSubmitDraftResponse:
        self.calls.append(("review_draft", kwargs))
        return ReviewGreenhouseSubmitDraftResponse(
            approval_request_id=APPROVAL_REQUEST_ID,
            policy_decision_id=POLICY_DECISION_ID,
            authorization_id=AUTHORIZATION_ID,
            decision=cast(Literal["approved", "rejected"], kwargs["decision"]),
            receipt_state="created",
        )

    async def reserve_intent(self, **kwargs: object) -> ReserveGreenhouseSubmitIntentResponse:
        self.calls.append(("reserve_intent", kwargs))
        return ReserveGreenhouseSubmitIntentResponse(
            account_id=ACCOUNT_ID,
            reservation_id=RESERVATION_ID,
            outbox_event_id=OUTBOX_EVENT_ID,
            event_key="greenhouse-submit:reservation-1",
            receipt_state="created",
        )

    async def list_reconciliation_cases(
        self, *, actor_id: UUID, limit: int
    ) -> ListGreenhouseSubmitReconciliationCasesResponse:
        self.calls.append(("list_reconciliation_cases", {"actor_id": actor_id, "limit": limit}))
        return ListGreenhouseSubmitReconciliationCasesResponse(cases=(_case(),))

    async def reconciliation_case_status(
        self, *, actor_id: UUID, reconciliation_case_id: UUID
    ) -> GreenhouseSubmitReconciliationCaseStatusResponse:
        self.calls.append(
            (
                "reconciliation_case_status",
                {"actor_id": actor_id, "reconciliation_case_id": reconciliation_case_id},
            )
        )
        return GreenhouseSubmitReconciliationCaseStatusResponse(case=_case())

    async def submit_reconciliation_evidence(
        self, **kwargs: object
    ) -> SubmitGreenhouseReconciliationEvidenceResponse:
        self.calls.append(("submit_reconciliation_evidence", kwargs))
        return SubmitGreenhouseReconciliationEvidenceResponse(
            reconciliation_case_id=cast(UUID, kwargs["reconciliation_case_id"]),
            evidence_review_id=EVIDENCE_REVIEW_ID,
            reconciliation_status=cast(
                Literal[
                    "accepted_unverified",
                    "ambiguous",
                    "provider_rejected",
                    "reconciliation_required",
                ],
                kwargs["observed_status"],
            ),
            evidence_sha256=cast(str, kwargs["evidence_sha256"]),
            evidence_source=cast(
                Literal["employer_admin", "recruiting_webhook", "manual_employer_system"],
                kwargs["evidence_source"],
            ),
            receipt_state="created",
        )

    async def review_reconciliation_evidence(
        self, **kwargs: object
    ) -> ReviewGreenhouseReconciliationEvidenceResponse:
        self.calls.append(("review_reconciliation_evidence", kwargs))
        return ReviewGreenhouseReconciliationEvidenceResponse(
            reconciliation_case_id=cast(UUID, kwargs["reconciliation_case_id"]),
            evidence_review_id=cast(UUID, kwargs["evidence_review_id"]),
            reconciliation_status=cast(
                Literal["confirmed", "resolved_absent", "blocked"], kwargs["decision"]
            ),
            reviewed_evidence_source=cast(
                Literal["employer_admin", "recruiting_webhook", "manual_employer_system"],
                kwargs["reviewed_evidence_source"],
            ),
            reviewed_observed_status=cast(
                Literal[
                    "accepted_unverified",
                    "ambiguous",
                    "provider_rejected",
                    "reconciliation_required",
                ],
                kwargs["reviewed_observed_status"],
            ),
            reviewed_employer_authorization_evidence_sha256=cast(
                str,
                kwargs["reviewed_employer_authorization_evidence_sha256"],
            ),
            receipt_state="created",
        )


class ErroringGreenhouseSubmitProvider(RecordingGreenhouseSubmitProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def account_status(
        self, *, actor_id: UUID, account_id: UUID
    ) -> GreenhouseSubmitAccountStatusResponse:
        self.calls.append(("account_status", {"actor_id": actor_id, "account_id": account_id}))
        raise self._error


def test_greenhouse_submit_routes_are_exact_and_have_no_raw_submit_route() -> None:
    client, _provider = _client(RecordingGreenhouseSubmitProvider())

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/internal/greenhouse-submit/accounts" in paths
    assert "/api/v1/internal/greenhouse-submit/drafts" in paths
    assert "/api/v1/internal/greenhouse-submit/drafts/{approval_request_id}/review" in paths
    assert "/api/v1/internal/greenhouse-submit/accounts/{account_id}/reservations" in paths
    assert "/api/v1/internal/greenhouse-submit/reconciliation-cases" in paths
    assert (
        "/api/v1/internal/greenhouse-submit/reconciliation-cases/{reconciliation_case_id}/evidence"
        in paths
    )
    assert (
        "/api/v1/internal/greenhouse-submit/reconciliation-cases/{reconciliation_case_id}/review"
        in paths
    )
    assert not any(path.endswith("/submit") or path.endswith("/raw-submit") for path in paths)


def test_greenhouse_submit_account_registration_binds_profile_and_does_not_echo_handle() -> None:
    client, provider = _client(RecordingGreenhouseSubmitProvider())

    response = client.post(
        "/api/v1/internal/greenhouse-submit/accounts",
        headers=MUTATION_HEADERS,
        json=_register_account_payload(),
    )
    rejected = client.post(
        "/api/v1/internal/greenhouse-submit/accounts",
        headers=MUTATION_HEADERS,
        json=_register_account_payload() | {"api_key": "plaintext", "authorization": "Bearer x"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["account"]["submit_mode"] == "reviewed_exact_greenhouse_payload_only"
    assert body["account"]["employer_id"] == "example-ai"
    assert body["account"]["board_token_sha256"] == hashlib.sha256(b"example").hexdigest()
    assert body["account"]["credential_profile_id"] == "profile-main"
    assert body["account"]["credential_profile_version"] == 7
    assert body["account"]["credential_fingerprint_sha256"] == SHA256_A
    assert body["account"]["employer_authorization_evidence_sha256"] == SHA256_B
    assert body["account"]["daily_submit_limit"] == 25
    assert _contains_no_secret_fields(body)
    assert rejected.status_code == 422
    register = cast(dict[str, object], provider.calls[0][1])
    assert register["actor_id"] == ACTOR_ID
    assert register["opaque_credential_handle"] == "vault:greenhouse/example/profile-main"
    assert register["credential_store_evidence_sha256"] == SHA256_C
    assert register["release_evidence_sha256"] == SHA256_D
    assert len(provider.calls) == 1


def test_greenhouse_submit_draft_accepts_fixed_target_core_materials_only() -> None:
    client, provider = _client(RecordingGreenhouseSubmitProvider())

    response = client.post(
        "/api/v1/internal/greenhouse-submit/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload(),
    )

    assert response.status_code == 202
    assert response.json()["review_snapshot_sha256"] == SHA256_C
    assert _contains_no_secret_fields(response.json())
    assert provider.calls[0][0] == "create_draft"
    call = cast(dict[str, object], provider.calls[0][1])
    assert call["target"] == {
        "host": "boards-api.greenhouse.io",
        "board_token": "example",
        "job_id": 123456,
        "target_host": "boards-api.greenhouse.io",
        "channel": "greenhouse:job-board",
        "adapter_id": "greenhouse-job-board",
        "fixture_id": "greenhouse-submit.v1",
    }
    assert call["core_fields"] == {
        "email": "ada@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "+1 555 0100",
    }
    assert call["attachment_refs"] == (
        {
            "field_name": "resume",
            "object_key": "materials/resume/a.pdf",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "size_bytes": 1024,
            "sha256": SHA256_A,
        },
    )
    assert call["approved_material_hashes"] == (SHA256_A, SHA256_B)
    assert call["requested_for"] == GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND
    assert "decision_rule_reference" not in call


def test_greenhouse_submit_draft_rejects_schema_url_auth_custom_question_and_location_inputs() -> (
    None
):
    client, provider = _client(RecordingGreenhouseSubmitProvider())

    for forbidden in (
        {"api_key": "plaintext"},
        {"authorization": "Bearer plaintext"},
        {"url": "https://boards-api.greenhouse.io/v1/boards/example/jobs/123456"},
        {"schema": {"questions": []}},
        {"question_123": "custom answer"},
        {"location": "Remote"},
        {"website": "https://example.com"},
    ):
        response = client.post(
            "/api/v1/internal/greenhouse-submit/drafts",
            headers=MUTATION_HEADERS,
            json=_create_draft_payload() | forbidden,
        )

        assert response.status_code == 422

    uppercase_hash = client.post(
        "/api/v1/internal/greenhouse-submit/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload() | {"approved_material_hashes": ("A" * 64,)},
    )
    generic_review = client.post(
        "/api/v1/internal/greenhouse-submit/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload() | {"requested_for": "generic_submit"},
    )
    bad_job = client.post(
        "/api/v1/internal/greenhouse-submit/drafts",
        headers=MUTATION_HEADERS,
        json=_create_draft_payload() | {"job_id": 0},
    )

    assert uppercase_hash.status_code == 422
    assert generic_review.status_code == 422
    assert bad_job.status_code == 422
    assert provider.calls == []


def test_greenhouse_submit_review_reserve_and_reconciliation_are_review_gated() -> None:
    client, provider = _client(RecordingGreenhouseSubmitProvider())

    review_response = client.post(
        f"/api/v1/internal/greenhouse-submit/drafts/{APPROVAL_REQUEST_ID}/review",
        headers=MUTATION_HEADERS,
        json=_review_payload(),
    )
    reserve_response = client.post(
        f"/api/v1/internal/greenhouse-submit/accounts/{ACCOUNT_ID}/reservations",
        headers=MUTATION_HEADERS,
        json=_reserve_payload(),
    )
    evidence_response = client.post(
        f"/api/v1/internal/greenhouse-submit/reconciliation-cases/{RECONCILIATION_CASE_ID}/evidence",
        headers=MUTATION_HEADERS,
        json=_evidence_payload(),
    )
    confirm_response = client.post(
        f"/api/v1/internal/greenhouse-submit/reconciliation-cases/{RECONCILIATION_CASE_ID}/review",
        headers=MUTATION_HEADERS,
        json=_evidence_review_payload(decision="confirmed"),
    )

    assert review_response.status_code == 200
    assert reserve_response.status_code == 202
    assert reserve_response.json()["dispatch_status"] == "pending_dispatch"
    assert "accepted_unverified" not in reserve_response.text
    assert evidence_response.status_code == 202
    assert evidence_response.json()["evidence_review_id"] == str(EVIDENCE_REVIEW_ID)
    assert evidence_response.json()["reconciliation_status"] == "accepted_unverified"
    assert "confirmed" not in evidence_response.text
    assert confirm_response.status_code == 200
    assert confirm_response.json()["evidence_review_id"] == str(EVIDENCE_REVIEW_ID)
    assert confirm_response.json()["reconciliation_status"] == "confirmed"
    assert confirm_response.json()["reviewed_evidence_source"] == "employer_admin"
    assert confirm_response.json()["reviewed_observed_status"] == "accepted_unverified"
    assert _contains_no_secret_fields(reserve_response.json())
    assert [call[0] for call in provider.calls] == [
        "review_draft",
        "reserve_intent",
        "submit_reconciliation_evidence",
        "review_reconciliation_evidence",
    ]
    review_call = cast(dict[str, object], provider.calls[0][1])
    reserve_call = cast(dict[str, object], provider.calls[1][1])
    evidence_review_call = cast(dict[str, object], provider.calls[3][1])
    assert review_call["requested_for"] == "greenhouse_submit_exact_payload"
    assert reserve_call["reviewed_by_user_id"] == ACTOR_ID
    assert reserve_call["review_evidence_sha256"] == SHA256_C
    assert reserve_call["material_hash"] == SHA256_E
    assert reserve_call["submission_identity_sha256"] == SHA256_F
    assert evidence_review_call["reviewed_evidence_source"] == "employer_admin"
    assert evidence_review_call["reviewed_observed_status"] == "accepted_unverified"
    assert evidence_review_call["reviewed_employer_authorization_evidence_sha256"] == SHA256_B


def test_greenhouse_submit_rejects_non_employer_authoritative_evidence_sources() -> None:
    client, provider = _client(RecordingGreenhouseSubmitProvider())

    provider_get = client.post(
        f"/api/v1/internal/greenhouse-submit/reconciliation-cases/{RECONCILIATION_CASE_ID}/evidence",
        headers=MUTATION_HEADERS,
        json=_evidence_payload() | {"evidence_source": "greenhouse_provider_get"},
    )
    gmail_clue = client.post(
        f"/api/v1/internal/greenhouse-submit/reconciliation-cases/{RECONCILIATION_CASE_ID}/review",
        headers=MUTATION_HEADERS,
        json=_evidence_review_payload(decision="confirmed")
        | {"reviewed_evidence_source": "gmail_clue"},
    )

    assert provider_get.status_code == 422
    assert gmail_clue.status_code == 422
    assert provider.calls == []


def test_greenhouse_submit_requires_session_csrf_origin_limits_and_safe_errors() -> None:
    client, provider = _client(RecordingGreenhouseSubmitProvider())
    unauthenticated = TestClient(_app(RecordingGreenhouseSubmitProvider())).get(
        "/api/v1/internal/greenhouse-submit/accounts"
    )
    bad_origin = client.post(
        f"/api/v1/internal/greenhouse-submit/accounts/{ACCOUNT_ID}/reservations",
        headers={"Origin": "http://attacker.example", "X-CSRF-Token": "authenticated-csrf"},
        json={},
    )
    missing_csrf = client.post(
        "/api/v1/internal/greenhouse-submit/drafts",
        headers={"Origin": "http://testserver"},
        json=_create_draft_payload(),
    )
    bad_limit = client.get("/api/v1/internal/greenhouse-submit/accounts?limit=101")

    assert unauthenticated.status_code == 401
    assert bad_origin.status_code == 403
    assert missing_csrf.status_code == 422
    assert bad_limit.status_code == 400
    assert provider.calls == []

    for error, expected_status in (
        (GreenhouseSubmitNotFound("cross owner"), 404),
        (GreenhouseSubmitConflict("bad state"), 409),
        (GreenhouseSubmitUnavailable("submit unavailable"), 503),
    ):
        error_client, error_provider = _client(ErroringGreenhouseSubmitProvider(error))

        response = error_client.get(f"/api/v1/internal/greenhouse-submit/accounts/{ACCOUNT_ID}")

        assert response.status_code == expected_status
        assert error_provider.calls == [
            ("account_status", {"actor_id": ACTOR_ID, "account_id": ACCOUNT_ID})
        ]
        assert "cross owner" not in response.text
        assert "bad state" not in response.text
        assert "submit unavailable" not in response.text


def test_greenhouse_submit_disabled_provider_returns_unavailable() -> None:
    client, _provider = _client(DisabledGreenhouseSubmitOperatorProvider())

    response = client.get("/api/v1/internal/greenhouse-submit/accounts")

    assert response.status_code == 503


def _account(
    *,
    candidate_id: UUID = CANDIDATE_ID,
    employer_id: str = "example-ai",
    board_token_sha256: str = SHA256_A,
    account_subject: str = "ops@example.com",
    status: GreenhouseSubmitAccountStatus = "active",
    credential_profile_id: str = "profile-main",
    credential_profile_version: int = 7,
    credential_fingerprint_sha256: str = SHA256_A,
    credential_profile_status: Literal["active", "disabled", "expired", "revoked"] = "active",
    credential_profile_expires_at: datetime = EXPIRES_AT,
    employer_authorization_evidence_sha256: str = SHA256_B,
    daily_submit_limit: int = 25,
) -> GreenhouseSubmitAccountSummary:
    return GreenhouseSubmitAccountSummary(
        account_id=ACCOUNT_ID,
        candidate_id=candidate_id,
        employer_id=employer_id,
        board_token_sha256=board_token_sha256,
        account_subject=account_subject,
        status=status,
        credential_profile_id=credential_profile_id,
        credential_profile_version=credential_profile_version,
        credential_fingerprint_sha256=credential_fingerprint_sha256,
        credential_profile_status=credential_profile_status,
        credential_profile_expires_at=credential_profile_expires_at,
        employer_authorization_evidence_sha256=employer_authorization_evidence_sha256,
        daily_submit_limit=daily_submit_limit,
        updated_at=NOW,
    )


def _case() -> GreenhouseSubmitReconciliationCaseSummary:
    return GreenhouseSubmitReconciliationCaseSummary(
        reconciliation_case_id=RECONCILIATION_CASE_ID,
        account_id=ACCOUNT_ID,
        action_intent_id=INTENT_ID,
        payload_version_id=PAYLOAD_VERSION_ID,
        reservation_key="reservation-1",
        reconciliation_key="reconciliation-1",
        status="accepted_unverified",
        evidence_sha256=SHA256_A,
        evidence_source="employer_admin",
        updated_at=NOW,
    )


def _register_account_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "candidate_id": str(CANDIDATE_ID),
        "employer_id": "example-ai",
        "board_token": "example",
        "account_subject": "ops@example.com",
        "opaque_credential_handle": "vault:greenhouse/example/profile-main",
        "credential_profile_id": "profile-main",
        "credential_profile_version": 7,
        "credential_fingerprint_sha256": SHA256_A,
        "credential_profile_status": "active",
        "credential_profile_expires_at": EXPIRES_AT.isoformat(),
        "employer_authorization_evidence_sha256": SHA256_B,
        "credential_store_evidence_sha256": SHA256_C,
        "release_evidence_sha256": SHA256_D,
        "status": "active",
        "daily_submit_limit": 25,
    }


def _create_draft_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "candidate_id": str(CANDIDATE_ID),
        "resource_id": str(CANDIDATE_ID),
        "board_token": "example",
        "job_id": 123456,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555 0100",
        "attachment_refs": [
            {
                "field_name": "resume",
                "object_key": "materials/resume/a.pdf",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1024,
                "sha256": SHA256_A,
            }
        ],
        "source_draft_payload_hash": SHA256_C,
        "approved_material_hashes": [SHA256_A, SHA256_B],
        "ruleset_version": "greenhouse-submit-rules.v1",
        "expires_at": EXPIRES_AT.isoformat(),
        "requested_for": GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND,
    }


def _review_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "action_intent_id": str(INTENT_ID),
        "payload_version_id": str(PAYLOAD_VERSION_ID),
        "campaign_id": str(CAMPAIGN_ID),
        "grant_version_id": str(GRANT_VERSION_ID),
        "payload_hash": SHA256_D,
        "material_hash": SHA256_E,
        "submission_identity_sha256": SHA256_F,
        "decision": "approved",
        "authorization_id": str(AUTHORIZATION_ID),
        "review_snapshot_sha256": SHA256_C,
        "authorization_expires_at": EXPIRES_AT.isoformat(),
        "reason": "exact Greenhouse payload approved",
        "requested_for": GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND,
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
        "material_hash": SHA256_E,
        "submission_identity_sha256": SHA256_F,
        "board_token_sha256": SHA256_A,
        "job_id_sha256": SHA256_B,
        "schema_sha256": SHA256_C,
        "approval_request_id": str(APPROVAL_REQUEST_ID),
        "review_evidence_sha256": SHA256_C,
        "review_snapshot_sha256": SHA256_D,
        "release_qualification_id": str(RELEASE_QUALIFICATION_ID),
        "reservation_key": "reservation-1",
        "reconciliation_key": "reconciliation-1",
    }


def _evidence_payload() -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "evidence_source": "employer_admin",
        "evidence_sha256": SHA256_A,
        "observed_status": "accepted_unverified",
        "observed_at": NOW.isoformat(),
        "reason_code": "GREENHOUSE_POST_ACCEPTED_NEEDS_RECONCILIATION",
    }


def _evidence_review_payload(
    *,
    decision: Literal["confirmed", "resolved_absent", "blocked"],
) -> dict[str, object]:
    return {
        "command_id": str(COMMAND_ID),
        "evidence_review_id": str(EVIDENCE_REVIEW_ID),
        "evidence_sha256": SHA256_A,
        "reviewed_evidence_source": "employer_admin",
        "reviewed_observed_status": "accepted_unverified",
        "decision": decision,
        "reviewed_employer_authorization_evidence_sha256": SHA256_B,
        "review_snapshot_sha256": SHA256_C,
        "reason": "reviewed employer-authorized evidence only",
    }


def _app(provider: GreenhouseSubmitOperatorProvider) -> FastAPI:
    app = FastAPI(openapi_url="/api/v1/openapi.json")
    app.include_router(
        create_greenhouse_submit_operator_router(
            auth_service=cast(FixedConsoleAuthService, FixedConsoleAuthService()),
            settings=ConsoleWebSettings(
                allowed_hosts=frozenset({"testserver"}),
                allowed_origins=frozenset({"http://testserver"}),
            ),
            provider=provider,
            now_provider=lambda: NOW,
        )
    )
    return app


def _client(
    provider: GreenhouseSubmitOperatorProvider,
) -> tuple[TestClient, RecordingGreenhouseSubmitProvider]:
    client = TestClient(_app(provider), follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")
    return client, cast(RecordingGreenhouseSubmitProvider, provider)


def _contains_no_secret_fields(value: object) -> bool:
    forbidden = {
        "api_key",
        "authorization",
        "auth_header",
        "token",
        "secret",
        "opaque_credential_handle",
        "credential_handle",
        "opaque_broker_handle",
        "idempotency_key",
        "trace_id",
        "fencing_token",
        "lease_token",
    }
    if isinstance(value, dict):
        return all(
            key.lower() not in forbidden and _contains_no_secret_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_contains_no_secret_fields(item) for item in value)
    return True
