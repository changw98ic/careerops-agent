# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from careerops.application.gmail_send import (
    GMAIL_SEND_ADAPTER_ID,
    GMAIL_SEND_CHANNEL,
    GMAIL_SEND_FIXTURE_ID,
    GMAIL_SEND_TARGET_HOST,
    GmailSendPayload,
)
from careerops.application.gmail_send import (
    GmailSendAttachmentRef as DomainGmailSendAttachmentRef,
)
from careerops.auth.contracts import AuthenticatedPrincipal, AuthError
from careerops.web.routes import ConsoleAuthServicePort
from careerops.web.security import ConsoleWebSettings, OriginHostValidator, RequestOriginRejected

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_EXACT_APPROVAL_KIND = "gmail_send_exact_payload"
GMAIL_SEND_EVENT_KEY_PREFIX = "gmail-send:"

BoundedKey = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:@/-]+$")]
EmailAddress = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReceiptState = Literal["created", "replayed"]
GmailSendAccountStatus = Literal["disabled", "active"]
GmailSendReviewDecision = Literal["approved", "rejected"]


class GmailSendContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterGmailSendAccountRequest(GmailSendContract):
    command_id: UUID
    candidate_id: UUID
    reconciliation_gmail_account_id: UUID
    credential_handle: BoundedKey
    account_subject: EmailAddress
    credential_store_evidence_sha256: Sha256Hex
    release_evidence_sha256: Sha256Hex
    status: GmailSendAccountStatus = "disabled"
    daily_send_limit: int = Field(default=25, ge=1, le=500)
    dedicated: Literal[True] = True
    oauth_client_mode: Literal["byo"] = "byo"


class GmailSendAttachmentRef(GmailSendContract):
    object_key: BoundedKey
    filename: Annotated[str, Field(min_length=1, max_length=180)]
    content_type: Annotated[str, Field(min_length=3, max_length=160)]
    size_bytes: int = Field(ge=0, le=25 * 1024 * 1024)
    sha256: Sha256Hex


class CreateGmailSendDraftRequest(GmailSendContract):
    command_id: UUID
    candidate_id: UUID
    resource_id: UUID
    sender: EmailAddress
    recipient: EmailAddress
    subject: Annotated[str, Field(min_length=1, max_length=320)]
    text_body: Annotated[str, Field(min_length=1, max_length=50_000)]
    attachment_refs: tuple[GmailSendAttachmentRef, ...] = ()
    thread_id: BoundedKey | None = None
    in_reply_to_message_id: Annotated[str, Field(min_length=3, max_length=320)] | None = None
    payload_hash: Sha256Hex
    ruleset_version: Annotated[str, Field(min_length=1, max_length=120)]
    decision_rule_reference: Annotated[str, Field(min_length=1, max_length=500)]
    expires_at: datetime
    requested_for: Literal["gmail_send_exact_payload"] = GMAIL_SEND_EXACT_APPROVAL_KIND


class ReviewGmailSendDraftRequest(GmailSendContract):
    command_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    campaign_id: UUID
    grant_version_id: UUID
    payload_hash: Sha256Hex
    decision: GmailSendReviewDecision
    authorization_id: UUID
    review_snapshot_sha256: Sha256Hex
    authorization_expires_at: datetime
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    requested_for: Literal["gmail_send_exact_payload"]


class ReserveGmailSendIntentRequest(GmailSendContract):
    command_id: UUID
    campaign_id: UUID
    grant_version_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: Sha256Hex
    recipient_sha256: Sha256Hex
    approval_request_id: UUID
    review_evidence_sha256: Sha256Hex
    review_snapshot_sha256: Sha256Hex
    release_qualification_id: UUID
    reservation_key: BoundedKey
    reconciliation_key: BoundedKey
    subject_sha256: Sha256Hex
    body_sha256: Sha256Hex


class GmailSendAccountSummary(GmailSendContract):
    account_id: UUID
    reconciliation_gmail_account_id: UUID | None = None
    account_subject: str
    status: GmailSendAccountStatus
    daily_send_limit: int = Field(ge=1)
    send_scope: Literal["https://www.googleapis.com/auth/gmail.send"] = GMAIL_SEND_SCOPE
    send_mode: Literal["reviewed_send_only"] = "reviewed_send_only"
    credential_status: str | None = None
    updated_at: datetime | None = None


class RegisterGmailSendAccountResponse(GmailSendContract):
    status: Literal["registered"] = "registered"
    account: GmailSendAccountSummary
    receipt_state: ReceiptState


class ListGmailSendAccountsResponse(GmailSendContract):
    status: Literal["listed"] = "listed"
    accounts: tuple[GmailSendAccountSummary, ...]


class GmailSendAccountStatusResponse(GmailSendContract):
    status: Literal["found"] = "found"
    account: GmailSendAccountSummary


class CreateGmailSendDraftResponse(GmailSendContract):
    status: Literal["draft_created"] = "draft_created"
    action_intent_id: UUID
    payload_version_id: UUID
    payload_hash: Sha256Hex
    policy_decision_id: UUID
    approval_request_id: UUID
    requested_for: Literal["gmail_send_exact_payload"] = GMAIL_SEND_EXACT_APPROVAL_KIND
    receipt_state: ReceiptState


class ReviewGmailSendDraftResponse(GmailSendContract):
    status: Literal["reviewed"] = "reviewed"
    approval_request_id: UUID
    policy_decision_id: UUID
    authorization_id: UUID | None = None
    decision: GmailSendReviewDecision
    requested_for: Literal["gmail_send_exact_payload"] = GMAIL_SEND_EXACT_APPROVAL_KIND
    receipt_state: ReceiptState


class ReserveGmailSendIntentResponse(GmailSendContract):
    status: Literal["reserved"] = "reserved"
    account_id: UUID
    reservation_id: UUID
    outbox_event_id: UUID
    event_key: str
    receipt_state: ReceiptState


class GmailSendNotFound(LookupError):
    """Gmail send account, draft, or reservation is missing for the authenticated owner."""


class GmailSendConflict(RuntimeError):
    """Gmail send command conflicts with reviewed payload or runtime state."""


class GmailSendUnavailable(RuntimeError):
    """Gmail send runtime is unavailable."""


class GmailSendOperatorProvider(Protocol):
    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        reconciliation_gmail_account_id: UUID,
        credential_handle: str,
        account_subject: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: GmailSendAccountStatus,
        daily_send_limit: int,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        now: datetime,
    ) -> RegisterGmailSendAccountResponse: ...

    async def list_accounts(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGmailSendAccountsResponse: ...

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GmailSendAccountStatusResponse: ...

    async def create_draft(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        resource_id: UUID,
        target: Mapping[str, object],
        payload: Mapping[str, object],
        attachment_refs: tuple[Mapping[str, object], ...],
        payload_hash: str | None,
        ruleset_version: str,
        decision_rule_reference: str,
        expires_at: datetime,
        requested_for: Literal["gmail_send_exact_payload"],
        now: datetime,
    ) -> CreateGmailSendDraftResponse: ...

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
        decision: GmailSendReviewDecision,
        authorization_id: UUID,
        review_snapshot_sha256: str,
        authorization_expires_at: datetime,
        reason: str,
        requested_for: Literal["gmail_send_exact_payload"],
        now: datetime,
    ) -> ReviewGmailSendDraftResponse: ...

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
        recipient_sha256: str,
        approval_request_id: UUID,
        review_evidence_sha256: str,
        review_snapshot_sha256: str,
        reviewed_by_user_id: UUID,
        release_qualification_id: UUID,
        reservation_key: str,
        reconciliation_key: str,
        subject_sha256: str,
        body_sha256: str,
        now: datetime,
    ) -> ReserveGmailSendIntentResponse: ...


class DisabledGmailSendOperatorProvider:
    async def register_account(self, **_kwargs: object) -> RegisterGmailSendAccountResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")

    async def list_accounts(self, **_kwargs: object) -> ListGmailSendAccountsResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")

    async def account_status(self, **_kwargs: object) -> GmailSendAccountStatusResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")

    async def create_draft(self, **_kwargs: object) -> CreateGmailSendDraftResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")

    async def review_draft(self, **_kwargs: object) -> ReviewGmailSendDraftResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")

    async def reserve_intent(self, **_kwargs: object) -> ReserveGmailSendIntentResponse:
        raise GmailSendUnavailable("gmail send operator runtime is not configured")


def install_gmail_send_operator_api(
    app: FastAPI,
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GmailSendOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    app.include_router(
        create_gmail_send_operator_router(
            auth_service=auth_service,
            settings=settings,
            provider=provider,
            now_provider=now_provider,
        )
    )


def create_gmail_send_operator_router(
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GmailSendOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> APIRouter:
    clock = now_provider or (lambda: datetime.now(UTC))
    origin_validator = OriginHostValidator(settings)
    router = APIRouter(prefix="/api/v1/internal/gmail-send", tags=["gmail-send-operator"])

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
        response_model=RegisterGmailSendAccountResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def register_account(
        body: RegisterGmailSendAccountRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> RegisterGmailSendAccountResponse:
        return await _translate_gmail_send_errors(
            provider.register_account(
                actor_id=principal.user_id,
                command_id=body.command_id,
                candidate_id=body.candidate_id,
                reconciliation_gmail_account_id=body.reconciliation_gmail_account_id,
                credential_handle=body.credential_handle,
                account_subject=body.account_subject,
                credential_store_evidence_sha256=body.credential_store_evidence_sha256,
                release_evidence_sha256=body.release_evidence_sha256,
                requested_status=body.status,
                daily_send_limit=body.daily_send_limit,
                dedicated=body.dedicated,
                oauth_client_mode=body.oauth_client_mode,
                now=clock(),
            )
        )

    @router.get("/accounts", response_model=ListGmailSendAccountsResponse)
    async def list_accounts(
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGmailSendAccountsResponse:
        _require_limit(limit)
        return await _translate_gmail_send_errors(
            provider.list_accounts(actor_id=principal.user_id, limit=limit)
        )

    @router.get("/accounts/{account_id}", response_model=GmailSendAccountStatusResponse)
    async def account_status(
        account_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
    ) -> GmailSendAccountStatusResponse:
        return await _translate_gmail_send_errors(
            provider.account_status(actor_id=principal.user_id, account_id=account_id)
        )

    @router.post(
        "/drafts",
        response_model=CreateGmailSendDraftResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_draft(
        body: CreateGmailSendDraftRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> CreateGmailSendDraftResponse:
        canonical = _canonical_gmail_send_payload(body)
        return await _translate_gmail_send_errors(
            provider.create_draft(
                actor_id=principal.user_id,
                command_id=body.command_id,
                candidate_id=body.candidate_id,
                resource_id=body.resource_id,
                target=canonical.target,
                payload=canonical.payload,
                attachment_refs=canonical.attachment_refs,
                payload_hash=None,
                ruleset_version=body.ruleset_version,
                decision_rule_reference=body.decision_rule_reference,
                expires_at=body.expires_at,
                requested_for=body.requested_for,
                now=clock(),
            )
        )

    @router.post(
        "/drafts/{approval_request_id}/review",
        response_model=ReviewGmailSendDraftResponse,
    )
    async def review_draft(
        approval_request_id: UUID,
        body: ReviewGmailSendDraftRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReviewGmailSendDraftResponse:
        return await _translate_gmail_send_errors(
            provider.review_draft(
                actor_id=principal.user_id,
                approval_request_id=approval_request_id,
                command_id=body.command_id,
                action_intent_id=body.action_intent_id,
                payload_version_id=body.payload_version_id,
                campaign_id=body.campaign_id,
                grant_version_id=body.grant_version_id,
                payload_hash=body.payload_hash,
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
        response_model=ReserveGmailSendIntentResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def reserve_intent(
        account_id: UUID,
        body: ReserveGmailSendIntentRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReserveGmailSendIntentResponse:
        return await _translate_gmail_send_errors(
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
                recipient_sha256=body.recipient_sha256,
                approval_request_id=body.approval_request_id,
                review_evidence_sha256=body.review_evidence_sha256,
                review_snapshot_sha256=body.review_snapshot_sha256,
                reviewed_by_user_id=principal.user_id,
                release_qualification_id=body.release_qualification_id,
                reservation_key=body.reservation_key,
                reconciliation_key=body.reconciliation_key,
                subject_sha256=body.subject_sha256,
                body_sha256=body.body_sha256,
                now=clock(),
            )
        )

    return router


async def _translate_gmail_send_errors[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except GmailSendUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail send runtime is unavailable",
        ) from None
    except GmailSendConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request could not be processed",
        ) from None
    except GmailSendNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 100",
        )


class _CanonicalGmailSendPayload(BaseModel):
    target: Mapping[str, object]
    payload: Mapping[str, object]
    attachment_refs: tuple[Mapping[str, object], ...]


def _canonical_gmail_send_payload(body: CreateGmailSendDraftRequest) -> _CanonicalGmailSendPayload:
    try:
        attachments = tuple(
            DomainGmailSendAttachmentRef(
                object_key=ref.object_key,
                filename=ref.filename,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
                sha256=ref.sha256,
            )
            for ref in body.attachment_refs
        )
        payload = GmailSendPayload(
            sender=body.sender,
            recipient=body.recipient,
            subject=body.subject,
            text_body=body.text_body,
            attachment_refs=attachments,
            thread_id=body.thread_id,
            in_reply_to_message_id=body.in_reply_to_message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
    if body.payload_hash != payload.payload_hash:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    payload_json = dict(payload.canonical_without_hash()) | {
        "payload_hash": payload.payload_hash,
        "message_id_header": payload.message_id_header,
    }
    attachment_json = tuple(ref.canonical() for ref in payload.attachment_refs)
    target_json: Mapping[str, object] = dict(payload.canonical_target()) | {
        "target_host": GMAIL_SEND_TARGET_HOST,
        "channel": GMAIL_SEND_CHANNEL,
        "adapter_id": GMAIL_SEND_ADAPTER_ID,
        "fixture_id": GMAIL_SEND_FIXTURE_ID,
    }
    return _CanonicalGmailSendPayload(
        target=target_json,
        payload=payload_json,
        attachment_refs=attachment_json,
    )


__all__: Sequence[str] = (
    "GMAIL_SEND_EVENT_KEY_PREFIX",
    "GMAIL_SEND_EXACT_APPROVAL_KIND",
    "GMAIL_SEND_SCOPE",
    "CreateGmailSendDraftRequest",
    "CreateGmailSendDraftResponse",
    "DisabledGmailSendOperatorProvider",
    "GmailSendAccountStatusResponse",
    "GmailSendAccountSummary",
    "GmailSendConflict",
    "GmailSendContract",
    "GmailSendNotFound",
    "GmailSendOperatorProvider",
    "GmailSendUnavailable",
    "ListGmailSendAccountsResponse",
    "RegisterGmailSendAccountRequest",
    "RegisterGmailSendAccountResponse",
    "ReserveGmailSendIntentRequest",
    "ReserveGmailSendIntentResponse",
    "ReviewGmailSendDraftRequest",
    "ReviewGmailSendDraftResponse",
    "create_gmail_send_operator_router",
    "install_gmail_send_operator_api",
)
