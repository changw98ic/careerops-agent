# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from careerops.application.greenhouse_submit import (
    GREENHOUSE_ADAPTER_ID,
    GREENHOUSE_BOARD_API_HOST,
    GREENHOUSE_CHANNEL,
    GreenhouseTarget,
)
from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef as DomainGreenhouseAttachmentRef,
)
from careerops.application.greenhouse_submit import (
    GreenhouseSubmissionPayload as DomainGreenhouseSubmissionPayload,
)
from careerops.auth.contracts import AuthenticatedPrincipal, AuthError
from careerops.web.routes import ConsoleAuthServicePort
from careerops.web.security import ConsoleWebSettings, OriginHostValidator, RequestOriginRejected

GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND = "greenhouse_submit_exact_payload"
GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX = "greenhouse-submit:"
GREENHOUSE_SUBMIT_FIXTURE_ID = "greenhouse-submit.v1"

BoundedKey = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:@/-]+$")]
BoardToken = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
]
EmailAddress = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReceiptState = Literal["created", "replayed"]
GreenhouseSubmitAccountStatus = Literal["disabled", "active"]
GreenhouseSubmitCredentialProfileStatus = Literal["active", "disabled", "expired", "revoked"]
GreenhouseSubmitReviewDecision = Literal["approved", "rejected"]
GreenhouseSubmitReconciliationStatus = Literal[
    "accepted_unverified",
    "ambiguous",
    "provider_rejected",
    "reconciliation_required",
    "confirmed",
    "resolved_absent",
    "blocked",
]
GreenhouseSubmitEvidenceReviewDecision = Literal["confirmed", "resolved_absent", "blocked"]
GreenhouseSubmitEvidenceSource = Literal[
    "employer_admin",
    "recruiting_webhook",
    "manual_employer_system",
]


class GreenhouseSubmitContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterGreenhouseSubmitAccountRequest(GreenhouseSubmitContract):
    command_id: UUID
    candidate_id: UUID
    employer_id: BoundedKey
    board_token: BoardToken
    account_subject: EmailAddress
    opaque_credential_handle: BoundedKey
    credential_profile_id: BoundedKey
    credential_profile_version: Annotated[int, Field(ge=1)]
    credential_fingerprint_sha256: Sha256Hex
    credential_profile_status: GreenhouseSubmitCredentialProfileStatus
    credential_profile_expires_at: datetime
    employer_authorization_evidence_sha256: Sha256Hex
    credential_store_evidence_sha256: Sha256Hex
    release_evidence_sha256: Sha256Hex
    status: GreenhouseSubmitAccountStatus = "disabled"
    daily_submit_limit: int = Field(default=25, ge=1, le=500)


class GreenhouseSubmitAttachmentRef(GreenhouseSubmitContract):
    field_name: Literal["resume", "cover_letter"]
    object_key: BoundedKey
    filename: Annotated[str, Field(min_length=1, max_length=180)]
    content_type: Annotated[str, Field(min_length=3, max_length=160)]
    size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    sha256: Sha256Hex


class CreateGreenhouseSubmitDraftRequest(GreenhouseSubmitContract):
    command_id: UUID
    candidate_id: UUID
    resource_id: UUID
    board_token: BoardToken
    job_id: int = Field(gt=0, le=9_999_999_999_999_999)
    first_name: Annotated[str, Field(min_length=1, max_length=255)]
    last_name: Annotated[str, Field(min_length=1, max_length=255)]
    email: EmailAddress
    phone: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    attachment_refs: tuple[GreenhouseSubmitAttachmentRef, ...] = ()
    source_draft_payload_hash: Sha256Hex
    approved_material_hashes: tuple[Sha256Hex, ...] = Field(min_length=1)
    ruleset_version: Annotated[str, Field(min_length=1, max_length=120)]
    expires_at: datetime
    requested_for: Literal["greenhouse_submit_exact_payload"] = (
        GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND
    )


class ReviewGreenhouseSubmitDraftRequest(GreenhouseSubmitContract):
    command_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    campaign_id: UUID
    grant_version_id: UUID
    payload_hash: Sha256Hex
    material_hash: Sha256Hex
    submission_identity_sha256: Sha256Hex
    decision: GreenhouseSubmitReviewDecision
    authorization_id: UUID
    review_snapshot_sha256: Sha256Hex
    authorization_expires_at: datetime
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    requested_for: Literal["greenhouse_submit_exact_payload"]


class ReserveGreenhouseSubmitIntentRequest(GreenhouseSubmitContract):
    command_id: UUID
    campaign_id: UUID
    grant_version_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: Sha256Hex
    material_hash: Sha256Hex
    submission_identity_sha256: Sha256Hex
    board_token_sha256: Sha256Hex
    job_id_sha256: Sha256Hex
    schema_sha256: Sha256Hex
    approval_request_id: UUID
    review_evidence_sha256: Sha256Hex
    review_snapshot_sha256: Sha256Hex
    release_qualification_id: UUID
    reservation_key: BoundedKey
    reconciliation_key: BoundedKey


class SubmitGreenhouseReconciliationEvidenceRequest(GreenhouseSubmitContract):
    command_id: UUID
    evidence_source: GreenhouseSubmitEvidenceSource
    evidence_sha256: Sha256Hex
    observed_status: Literal[
        "accepted_unverified",
        "ambiguous",
        "provider_rejected",
        "reconciliation_required",
    ]
    observed_at: datetime
    reason_code: Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Z0-9_]+$")]


class ReviewGreenhouseReconciliationEvidenceRequest(GreenhouseSubmitContract):
    command_id: UUID
    evidence_review_id: UUID
    evidence_sha256: Sha256Hex
    reviewed_evidence_source: GreenhouseSubmitEvidenceSource
    reviewed_observed_status: Literal[
        "accepted_unverified",
        "ambiguous",
        "provider_rejected",
        "reconciliation_required",
    ]
    decision: GreenhouseSubmitEvidenceReviewDecision
    reviewed_employer_authorization_evidence_sha256: Sha256Hex
    review_snapshot_sha256: Sha256Hex
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class GreenhouseSubmitAccountSummary(GreenhouseSubmitContract):
    account_id: UUID
    candidate_id: UUID
    employer_id: str
    board_token_sha256: Sha256Hex
    account_subject: str
    status: GreenhouseSubmitAccountStatus
    credential_profile_id: str
    credential_profile_version: int = Field(ge=1)
    credential_fingerprint_sha256: Sha256Hex
    credential_profile_status: GreenhouseSubmitCredentialProfileStatus
    credential_profile_expires_at: datetime
    employer_authorization_evidence_sha256: Sha256Hex
    daily_submit_limit: int = Field(ge=1)
    submit_mode: Literal["reviewed_exact_greenhouse_payload_only"] = (
        "reviewed_exact_greenhouse_payload_only"
    )
    updated_at: datetime | None = None


class RegisterGreenhouseSubmitAccountResponse(GreenhouseSubmitContract):
    status: Literal["registered"] = "registered"
    account: GreenhouseSubmitAccountSummary
    receipt_state: ReceiptState


class ListGreenhouseSubmitAccountsResponse(GreenhouseSubmitContract):
    status: Literal["listed"] = "listed"
    accounts: tuple[GreenhouseSubmitAccountSummary, ...]


class GreenhouseSubmitAccountStatusResponse(GreenhouseSubmitContract):
    status: Literal["found"] = "found"
    account: GreenhouseSubmitAccountSummary


class CreateGreenhouseSubmitDraftResponse(GreenhouseSubmitContract):
    status: Literal["draft_created"] = "draft_created"
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: Sha256Hex
    material_hash: Sha256Hex
    submission_identity_sha256: Sha256Hex
    review_snapshot_sha256: Sha256Hex
    policy_decision_id: UUID
    approval_request_id: UUID
    requested_for: Literal["greenhouse_submit_exact_payload"] = (
        GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND
    )
    receipt_state: ReceiptState


class ReviewGreenhouseSubmitDraftResponse(GreenhouseSubmitContract):
    status: Literal["reviewed"] = "reviewed"
    approval_request_id: UUID
    policy_decision_id: UUID
    authorization_id: UUID | None = None
    decision: GreenhouseSubmitReviewDecision
    requested_for: Literal["greenhouse_submit_exact_payload"] = (
        GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND
    )
    receipt_state: ReceiptState


class ReserveGreenhouseSubmitIntentResponse(GreenhouseSubmitContract):
    status: Literal["reserved"] = "reserved"
    account_id: UUID
    reservation_id: UUID
    outbox_event_id: UUID
    event_key: str
    dispatch_status: Literal["pending_dispatch"] = "pending_dispatch"
    receipt_state: ReceiptState


class GreenhouseSubmitReconciliationCaseSummary(GreenhouseSubmitContract):
    reconciliation_case_id: UUID
    account_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    reservation_key: str
    reconciliation_key: str
    status: GreenhouseSubmitReconciliationStatus
    evidence_sha256: Sha256Hex | None = None
    evidence_source: GreenhouseSubmitEvidenceSource | None = None
    updated_at: datetime | None = None


class ListGreenhouseSubmitReconciliationCasesResponse(GreenhouseSubmitContract):
    status: Literal["listed"] = "listed"
    cases: tuple[GreenhouseSubmitReconciliationCaseSummary, ...]


class GreenhouseSubmitReconciliationCaseStatusResponse(GreenhouseSubmitContract):
    status: Literal["found"] = "found"
    case: GreenhouseSubmitReconciliationCaseSummary


class SubmitGreenhouseReconciliationEvidenceResponse(GreenhouseSubmitContract):
    status: Literal["evidence_recorded"] = "evidence_recorded"
    reconciliation_case_id: UUID
    evidence_review_id: UUID
    reconciliation_status: Literal[
        "accepted_unverified",
        "ambiguous",
        "provider_rejected",
        "reconciliation_required",
    ]
    evidence_sha256: Sha256Hex
    evidence_source: GreenhouseSubmitEvidenceSource
    receipt_state: ReceiptState


class ReviewGreenhouseReconciliationEvidenceResponse(GreenhouseSubmitContract):
    status: Literal["evidence_reviewed"] = "evidence_reviewed"
    reconciliation_case_id: UUID
    evidence_review_id: UUID
    reconciliation_status: GreenhouseSubmitEvidenceReviewDecision
    reviewed_evidence_source: GreenhouseSubmitEvidenceSource
    reviewed_observed_status: Literal[
        "accepted_unverified",
        "ambiguous",
        "provider_rejected",
        "reconciliation_required",
    ]
    reviewed_employer_authorization_evidence_sha256: Sha256Hex
    receipt_state: ReceiptState


class GreenhouseSubmitNotFound(LookupError):
    """Greenhouse submit account, draft, reservation, or reconciliation case is missing."""


class GreenhouseSubmitConflict(RuntimeError):
    """Greenhouse submit command conflicts with reviewed payload or runtime state."""


class GreenhouseSubmitUnavailable(RuntimeError):
    """Greenhouse submit runtime is unavailable."""


class GreenhouseSubmitOperatorProvider(Protocol):
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
        credential_profile_status: GreenhouseSubmitCredentialProfileStatus,
        credential_profile_expires_at: datetime,
        employer_authorization_evidence_sha256: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: GreenhouseSubmitAccountStatus,
        daily_submit_limit: int,
        now: datetime,
    ) -> RegisterGreenhouseSubmitAccountResponse: ...

    async def list_accounts(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGreenhouseSubmitAccountsResponse: ...

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GreenhouseSubmitAccountStatusResponse: ...

    async def create_draft(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        resource_id: UUID,
        target: Mapping[str, object],
        core_fields: Mapping[str, object],
        attachment_refs: tuple[Mapping[str, object], ...],
        source_draft_payload_hash: str,
        approved_material_hashes: tuple[str, ...],
        ruleset_version: str,
        expires_at: datetime,
        requested_for: Literal["greenhouse_submit_exact_payload"],
        now: datetime,
    ) -> CreateGreenhouseSubmitDraftResponse: ...

    async def review_draft(
        self,
        *,
        actor_id: UUID,
        approval_request_id: UUID,
        command_id: UUID,
        action_intent_id: UUID,
        payload_version_id: UUID,
        campaign_id: UUID,
        grant_version_id: UUID,
        payload_hash: str,
        material_hash: str,
        submission_identity_sha256: str,
        decision: GreenhouseSubmitReviewDecision,
        authorization_id: UUID,
        review_snapshot_sha256: str,
        authorization_expires_at: datetime,
        reason: str,
        requested_for: Literal["greenhouse_submit_exact_payload"],
        now: datetime,
    ) -> ReviewGreenhouseSubmitDraftResponse: ...

    async def reserve_intent(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        campaign_id: UUID,
        grant_version_id: UUID,
        authorization_id: UUID,
        action_intent_id: UUID,
        payload_version_id: UUID,
        payload_hash: str,
        material_hash: str,
        submission_identity_sha256: str,
        board_token_sha256: str,
        job_id_sha256: str,
        schema_sha256: str,
        approval_request_id: UUID,
        review_evidence_sha256: str,
        review_snapshot_sha256: str,
        reviewed_by_user_id: UUID,
        release_qualification_id: UUID,
        reservation_key: str,
        reconciliation_key: str,
        now: datetime,
    ) -> ReserveGreenhouseSubmitIntentResponse: ...

    async def list_reconciliation_cases(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGreenhouseSubmitReconciliationCasesResponse: ...

    async def reconciliation_case_status(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
    ) -> GreenhouseSubmitReconciliationCaseStatusResponse: ...

    async def submit_reconciliation_evidence(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
        command_id: UUID,
        evidence_source: GreenhouseSubmitEvidenceSource,
        evidence_sha256: str,
        observed_status: Literal[
            "accepted_unverified",
            "ambiguous",
            "provider_rejected",
            "reconciliation_required",
        ],
        observed_at: datetime,
        reason_code: str,
        now: datetime,
    ) -> SubmitGreenhouseReconciliationEvidenceResponse: ...

    async def review_reconciliation_evidence(
        self,
        *,
        actor_id: UUID,
        reconciliation_case_id: UUID,
        command_id: UUID,
        evidence_review_id: UUID,
        evidence_sha256: str,
        reviewed_evidence_source: GreenhouseSubmitEvidenceSource,
        reviewed_observed_status: Literal[
            "accepted_unverified",
            "ambiguous",
            "provider_rejected",
            "reconciliation_required",
        ],
        decision: GreenhouseSubmitEvidenceReviewDecision,
        reviewed_employer_authorization_evidence_sha256: str,
        review_snapshot_sha256: str,
        reason: str,
        now: datetime,
    ) -> ReviewGreenhouseReconciliationEvidenceResponse: ...


class DisabledGreenhouseSubmitOperatorProvider:
    async def register_account(self, **_kwargs: object) -> RegisterGreenhouseSubmitAccountResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def list_accounts(self, **_kwargs: object) -> ListGreenhouseSubmitAccountsResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def account_status(self, **_kwargs: object) -> GreenhouseSubmitAccountStatusResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def create_draft(self, **_kwargs: object) -> CreateGreenhouseSubmitDraftResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def review_draft(self, **_kwargs: object) -> ReviewGreenhouseSubmitDraftResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def reserve_intent(self, **_kwargs: object) -> ReserveGreenhouseSubmitIntentResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def list_reconciliation_cases(
        self, **_kwargs: object
    ) -> ListGreenhouseSubmitReconciliationCasesResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def reconciliation_case_status(
        self, **_kwargs: object
    ) -> GreenhouseSubmitReconciliationCaseStatusResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def submit_reconciliation_evidence(
        self, **_kwargs: object
    ) -> SubmitGreenhouseReconciliationEvidenceResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")

    async def review_reconciliation_evidence(
        self, **_kwargs: object
    ) -> ReviewGreenhouseReconciliationEvidenceResponse:
        raise GreenhouseSubmitUnavailable("greenhouse submit operator runtime is not configured")


def install_greenhouse_submit_operator_api(
    app: FastAPI,
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GreenhouseSubmitOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    app.include_router(
        create_greenhouse_submit_operator_router(
            auth_service=auth_service,
            settings=settings,
            provider=provider,
            now_provider=now_provider,
        )
    )


def create_greenhouse_submit_operator_router(
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GreenhouseSubmitOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> APIRouter:
    clock = now_provider or (lambda: datetime.now(UTC))
    origin_validator = OriginHostValidator(settings)
    router = APIRouter(prefix="/api/v1/internal/greenhouse-submit", tags=["greenhouse-submit"])

    def require_principal(request: Request) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_host(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        raw_session = request.cookies.get(settings.session_cookie_name, "")
        raw_csrf = request.cookies.get(settings.csrf_cookie_name, "")
        try:
            principal = auth_service.authenticate(raw_session, now=clock())
            auth_service.validate_csrf(principal, raw_csrf)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from None
        return principal

    principal_dependency = Depends(require_principal)

    def require_mutation_csrf(
        request: Request,
        x_csrf_token: Annotated[str, Header(min_length=1, max_length=512)],
        principal: AuthenticatedPrincipal = principal_dependency,
    ) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_mutation(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        try:
            auth_service.validate_csrf(principal, x_csrf_token)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        return principal

    mutation_dependency = Depends(require_mutation_csrf)

    @router.post(
        "/accounts",
        response_model=RegisterGreenhouseSubmitAccountResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def register_account(
        body: RegisterGreenhouseSubmitAccountRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> RegisterGreenhouseSubmitAccountResponse:
        return await _translate_greenhouse_submit_errors(
            provider.register_account(
                actor_id=principal.user_id,
                command_id=body.command_id,
                candidate_id=body.candidate_id,
                employer_id=body.employer_id,
                board_token=body.board_token,
                account_subject=body.account_subject,
                opaque_credential_handle=body.opaque_credential_handle,
                credential_profile_id=body.credential_profile_id,
                credential_profile_version=body.credential_profile_version,
                credential_fingerprint_sha256=body.credential_fingerprint_sha256,
                credential_profile_status=body.credential_profile_status,
                credential_profile_expires_at=body.credential_profile_expires_at,
                employer_authorization_evidence_sha256=(
                    body.employer_authorization_evidence_sha256
                ),
                credential_store_evidence_sha256=body.credential_store_evidence_sha256,
                release_evidence_sha256=body.release_evidence_sha256,
                requested_status=body.status,
                daily_submit_limit=body.daily_submit_limit,
                now=clock(),
            )
        )

    @router.get("/accounts", response_model=ListGreenhouseSubmitAccountsResponse)
    async def list_accounts(
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGreenhouseSubmitAccountsResponse:
        _require_limit(limit)
        return await _translate_greenhouse_submit_errors(
            provider.list_accounts(actor_id=principal.user_id, limit=limit)
        )

    @router.get("/accounts/{account_id}", response_model=GreenhouseSubmitAccountStatusResponse)
    async def account_status(
        account_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
    ) -> GreenhouseSubmitAccountStatusResponse:
        return await _translate_greenhouse_submit_errors(
            provider.account_status(actor_id=principal.user_id, account_id=account_id)
        )

    @router.post(
        "/drafts",
        response_model=CreateGreenhouseSubmitDraftResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_draft(
        body: CreateGreenhouseSubmitDraftRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> CreateGreenhouseSubmitDraftResponse:
        canonical = _canonical_greenhouse_submit_payload(body)
        return await _translate_greenhouse_submit_errors(
            provider.create_draft(
                actor_id=principal.user_id,
                command_id=body.command_id,
                candidate_id=body.candidate_id,
                resource_id=body.resource_id,
                target=canonical.target,
                core_fields=canonical.core_fields,
                attachment_refs=canonical.attachment_refs,
                source_draft_payload_hash=body.source_draft_payload_hash,
                approved_material_hashes=body.approved_material_hashes,
                ruleset_version=body.ruleset_version,
                expires_at=body.expires_at,
                requested_for=body.requested_for,
                now=clock(),
            )
        )

    @router.post(
        "/drafts/{approval_request_id}/review",
        response_model=ReviewGreenhouseSubmitDraftResponse,
    )
    async def review_draft(
        approval_request_id: UUID,
        body: ReviewGreenhouseSubmitDraftRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReviewGreenhouseSubmitDraftResponse:
        return await _translate_greenhouse_submit_errors(
            provider.review_draft(
                actor_id=principal.user_id,
                approval_request_id=approval_request_id,
                command_id=body.command_id,
                action_intent_id=body.action_intent_id,
                payload_version_id=body.payload_version_id,
                campaign_id=body.campaign_id,
                grant_version_id=body.grant_version_id,
                payload_hash=body.payload_hash,
                material_hash=body.material_hash,
                submission_identity_sha256=body.submission_identity_sha256,
                decision=body.decision,
                authorization_id=body.authorization_id,
                review_snapshot_sha256=body.review_snapshot_sha256,
                authorization_expires_at=body.authorization_expires_at,
                reason=body.reason,
                requested_for=body.requested_for,
                now=clock(),
            )
        )

    @router.post(
        "/accounts/{account_id}/reservations",
        response_model=ReserveGreenhouseSubmitIntentResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reserve_intent(
        account_id: UUID,
        body: ReserveGreenhouseSubmitIntentRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReserveGreenhouseSubmitIntentResponse:
        return await _translate_greenhouse_submit_errors(
            provider.reserve_intent(
                actor_id=principal.user_id,
                account_id=account_id,
                command_id=body.command_id,
                campaign_id=body.campaign_id,
                grant_version_id=body.grant_version_id,
                authorization_id=body.authorization_id,
                action_intent_id=body.action_intent_id,
                payload_version_id=body.payload_version_id,
                payload_hash=body.payload_hash,
                material_hash=body.material_hash,
                submission_identity_sha256=body.submission_identity_sha256,
                board_token_sha256=body.board_token_sha256,
                job_id_sha256=body.job_id_sha256,
                schema_sha256=body.schema_sha256,
                approval_request_id=body.approval_request_id,
                review_evidence_sha256=body.review_evidence_sha256,
                review_snapshot_sha256=body.review_snapshot_sha256,
                reviewed_by_user_id=principal.user_id,
                release_qualification_id=body.release_qualification_id,
                reservation_key=body.reservation_key,
                reconciliation_key=body.reconciliation_key,
                now=clock(),
            )
        )

    @router.get(
        "/reconciliation-cases",
        response_model=ListGreenhouseSubmitReconciliationCasesResponse,
    )
    async def list_reconciliation_cases(
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGreenhouseSubmitReconciliationCasesResponse:
        _require_limit(limit)
        return await _translate_greenhouse_submit_errors(
            provider.list_reconciliation_cases(actor_id=principal.user_id, limit=limit)
        )

    @router.get(
        "/reconciliation-cases/{reconciliation_case_id}",
        response_model=GreenhouseSubmitReconciliationCaseStatusResponse,
    )
    async def reconciliation_case_status(
        reconciliation_case_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
    ) -> GreenhouseSubmitReconciliationCaseStatusResponse:
        return await _translate_greenhouse_submit_errors(
            provider.reconciliation_case_status(
                actor_id=principal.user_id,
                reconciliation_case_id=reconciliation_case_id,
            )
        )

    @router.post(
        "/reconciliation-cases/{reconciliation_case_id}/evidence",
        response_model=SubmitGreenhouseReconciliationEvidenceResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_reconciliation_evidence(
        reconciliation_case_id: UUID,
        body: SubmitGreenhouseReconciliationEvidenceRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> SubmitGreenhouseReconciliationEvidenceResponse:
        return await _translate_greenhouse_submit_errors(
            provider.submit_reconciliation_evidence(
                actor_id=principal.user_id,
                reconciliation_case_id=reconciliation_case_id,
                command_id=body.command_id,
                evidence_source=body.evidence_source,
                evidence_sha256=body.evidence_sha256,
                observed_status=body.observed_status,
                observed_at=body.observed_at,
                reason_code=body.reason_code,
                now=clock(),
            )
        )

    @router.post(
        "/reconciliation-cases/{reconciliation_case_id}/review",
        response_model=ReviewGreenhouseReconciliationEvidenceResponse,
    )
    async def review_reconciliation_evidence(
        reconciliation_case_id: UUID,
        body: ReviewGreenhouseReconciliationEvidenceRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReviewGreenhouseReconciliationEvidenceResponse:
        return await _translate_greenhouse_submit_errors(
            provider.review_reconciliation_evidence(
                actor_id=principal.user_id,
                reconciliation_case_id=reconciliation_case_id,
                command_id=body.command_id,
                evidence_review_id=body.evidence_review_id,
                evidence_sha256=body.evidence_sha256,
                reviewed_evidence_source=body.reviewed_evidence_source,
                reviewed_observed_status=body.reviewed_observed_status,
                decision=body.decision,
                reviewed_employer_authorization_evidence_sha256=(
                    body.reviewed_employer_authorization_evidence_sha256
                ),
                review_snapshot_sha256=body.review_snapshot_sha256,
                reason=body.reason,
                now=clock(),
            )
        )

    return router


async def _translate_greenhouse_submit_errors[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except GreenhouseSubmitUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Greenhouse submit runtime is unavailable",
        ) from None
    except GreenhouseSubmitConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request could not be processed",
        ) from None
    except GreenhouseSubmitNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 100",
        )


class _CanonicalGreenhouseSubmitPayload(BaseModel):
    target: Mapping[str, object]
    core_fields: Mapping[str, object]
    attachment_refs: tuple[Mapping[str, object], ...]


def _canonical_greenhouse_submit_payload(
    body: CreateGreenhouseSubmitDraftRequest,
) -> _CanonicalGreenhouseSubmitPayload:
    try:
        target = GreenhouseTarget(board_token=body.board_token, job_id=body.job_id)
        fields: dict[str, object] = {
            "first_name": body.first_name,
            "last_name": body.last_name,
            "email": body.email,
        }
        if body.phone is not None:
            fields["phone"] = body.phone
        attachments = tuple(
            DomainGreenhouseAttachmentRef(
                field_name=ref.field_name,
                object_key=ref.object_key,
                filename=ref.filename,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
            )
            for ref in body.attachment_refs
        )
        payload = DomainGreenhouseSubmissionPayload(
            target=target,
            schema_hash="0" * 64,
            fields=fields,
            source_draft_payload_hash=body.source_draft_payload_hash,
            approved_material_hashes=body.approved_material_hashes,
            attachment_refs=attachments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
    return _CanonicalGreenhouseSubmitPayload(
        target=dict(target.canonical())
        | {
            "target_host": GREENHOUSE_BOARD_API_HOST,
            "channel": GREENHOUSE_CHANNEL,
            "adapter_id": GREENHOUSE_ADAPTER_ID,
            "fixture_id": GREENHOUSE_SUBMIT_FIXTURE_ID,
        },
        core_fields=dict(payload.fields),
        attachment_refs=tuple(item.canonical() for item in payload.attachment_refs),
    )


__all__: Sequence[str] = (
    "GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX",
    "GREENHOUSE_SUBMIT_EXACT_APPROVAL_KIND",
    "CreateGreenhouseSubmitDraftRequest",
    "CreateGreenhouseSubmitDraftResponse",
    "DisabledGreenhouseSubmitOperatorProvider",
    "GreenhouseSubmitAccountStatusResponse",
    "GreenhouseSubmitAccountSummary",
    "GreenhouseSubmitConflict",
    "GreenhouseSubmitContract",
    "GreenhouseSubmitNotFound",
    "GreenhouseSubmitOperatorProvider",
    "GreenhouseSubmitReconciliationCaseStatusResponse",
    "GreenhouseSubmitReconciliationCaseSummary",
    "GreenhouseSubmitUnavailable",
    "ListGreenhouseSubmitAccountsResponse",
    "ListGreenhouseSubmitReconciliationCasesResponse",
    "RegisterGreenhouseSubmitAccountRequest",
    "RegisterGreenhouseSubmitAccountResponse",
    "ReserveGreenhouseSubmitIntentRequest",
    "ReserveGreenhouseSubmitIntentResponse",
    "ReviewGreenhouseReconciliationEvidenceRequest",
    "ReviewGreenhouseReconciliationEvidenceResponse",
    "ReviewGreenhouseSubmitDraftRequest",
    "ReviewGreenhouseSubmitDraftResponse",
    "SubmitGreenhouseReconciliationEvidenceRequest",
    "SubmitGreenhouseReconciliationEvidenceResponse",
    "create_greenhouse_submit_operator_router",
    "install_greenhouse_submit_operator_api",
)
