# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from careerops.auth.contracts import AuthenticatedPrincipal, AuthError
from careerops.web.routes import ConsoleAuthServicePort
from careerops.web.security import ConsoleWebSettings, OriginHostValidator, RequestOriginRejected

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

CredentialHandle = Annotated[
    str, Field(min_length=8, max_length=256, pattern=r"^[A-Za-z0-9._:@/-]+$")
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

GmailAccountStatus = Literal["active", "paused", "sync_required", "revoked", "blocked"]
GmailSyncReason = Literal["manual", "history_expired", "scheduled", "recovery"]
GmailSyncRunStatus = Literal["queued", "leased", "succeeded", "failed", "cancelled"]
GmailProposalStatus = Literal["pending_review", "approved", "rejected"]
GmailPublishingStatus = Literal["testing", "in_production"]
GmailProposalKind = Literal[
    "application_status_update",
    "follow_up_draft",
    "review_only",
    "reconciliation_update",
]


class GmailReadonlyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterGmailAccountRequest(GmailReadonlyContract):
    command_id: UUID
    candidate_id: UUID
    credential_handle: CredentialHandle
    account_subject: Annotated[str, Field(min_length=3, max_length=320)]
    dedicated: Literal[True] = True
    oauth_client_mode: Literal["byo"] = "byo"
    publishing_status: GmailPublishingStatus
    credential_store_evidence_sha256: Sha256Hex


class RequestGmailSyncRequest(GmailReadonlyContract):
    command_id: UUID
    reason: GmailSyncReason = "manual"


class ResetGmailHistoryRequest(GmailReadonlyContract):
    command_id: UUID
    snapshot_sha256: Sha256Hex
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class ReviewGmailProposalRequest(GmailReadonlyContract):
    command_id: UUID
    decision: Literal["approve", "reject"]
    snapshot_sha256: Sha256Hex
    canonical_job_id: UUID | None = None
    job_posting_id: UUID | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class RevokeGmailAccountRequest(GmailReadonlyContract):
    command_id: UUID
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class GmailAccountSummary(GmailReadonlyContract):
    account_id: UUID
    owner_user_id: UUID
    candidate_id: UUID
    provider: Literal["gmail"] = "gmail"
    account_subject: str
    status: GmailAccountStatus
    sync_mode: Literal["polling"] = "polling"
    readonly_scope: Literal["https://www.googleapis.com/auth/gmail.readonly"] = GMAIL_READONLY_SCOPE
    dedicated: Literal[True] = True
    oauth_client_mode: Literal["byo"] = "byo"
    publishing_status: GmailPublishingStatus
    snapshot_sha256: Sha256Hex
    last_history_id: str | None = None
    next_page_token_present: bool = False
    last_synced_at: datetime | None = None
    last_full_sync_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class RegisterGmailAccountResponse(GmailReadonlyContract):
    status: Literal["registered"] = "registered"
    account: GmailAccountSummary


class ListGmailAccountsResponse(GmailReadonlyContract):
    status: Literal["listed"] = "listed"
    accounts: tuple[GmailAccountSummary, ...]


class GmailAccountStatusResponse(GmailReadonlyContract):
    status: Literal["found"] = "found"
    account: GmailAccountSummary


class GmailSyncRunSummary(GmailReadonlyContract):
    run_id: UUID
    account_id: UUID
    owner_user_id: UUID
    reason: GmailSyncReason
    status: GmailSyncRunStatus
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    history_start_id: str | None = None
    history_end_id: str | None = None
    next_page_token_present: bool = False
    message_count: int = Field(ge=0)
    last_error_code: str | None = None


class RequestGmailSyncResponse(GmailReadonlyContract):
    status: Literal["queued"] = "queued"
    run: GmailSyncRunSummary


class ListGmailSyncRunsResponse(GmailReadonlyContract):
    status: Literal["listed"] = "listed"
    runs: tuple[GmailSyncRunSummary, ...]


class GmailProposalSummary(GmailReadonlyContract):
    proposal_id: UUID
    account_id: UUID
    owner_user_id: UUID
    signal_id: UUID
    proposal_status: GmailProposalStatus
    proposal_kind: GmailProposalKind
    review_priority: Literal["normal", "high"]
    classification: str
    relevance: Literal["relevant", "uncertain", "non_relevant"]
    confidence: float = Field(ge=0, le=1)
    signal_sha256: Sha256Hex
    payload_sha256: Sha256Hex
    redacted_excerpt: str
    payload_snapshot: Mapping[str, object]
    canonical_job_id: UUID | None = None
    job_posting_id: UUID | None = None
    created_at: datetime


class ListGmailProposalsResponse(GmailReadonlyContract):
    status: Literal["listed"] = "listed"
    proposals: tuple[GmailProposalSummary, ...]


class ReviewGmailProposalResponse(GmailReadonlyContract):
    status: Literal["submitted"] = "submitted"
    proposal: GmailProposalSummary


class ResetGmailHistoryResponse(GmailReadonlyContract):
    status: Literal["sync_required"] = "sync_required"
    account: GmailAccountSummary


class RevokeGmailAccountResponse(GmailReadonlyContract):
    status: Literal["revoked"] = "revoked"
    account: GmailAccountSummary


class GmailReadonlyNotFound(LookupError):
    """Gmail account, run, or proposal is missing for the authenticated owner."""


class GmailReadonlyConflict(RuntimeError):
    """Gmail read-only command conflicts with current account/proposal state."""


class GmailReadonlyUnavailable(RuntimeError):
    """Gmail read-only runtime is unavailable."""


class GmailReadonlyOperatorProvider(Protocol):
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
        publishing_status: GmailPublishingStatus,
        credential_store_evidence_sha256: str,
        now: datetime,
    ) -> RegisterGmailAccountResponse: ...

    async def list_accounts(self, *, actor_id: UUID, limit: int) -> ListGmailAccountsResponse: ...

    async def account_status(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
    ) -> GmailAccountStatusResponse: ...

    async def request_sync(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: GmailSyncReason,
        now: datetime,
    ) -> RequestGmailSyncResponse: ...

    async def list_sync_runs(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailSyncRunsResponse: ...

    async def list_proposals(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        limit: int,
    ) -> ListGmailProposalsResponse: ...

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
    ) -> ReviewGmailProposalResponse: ...

    async def reset_history(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        reason: str | None,
        now: datetime,
    ) -> ResetGmailHistoryResponse: ...

    async def revoke_account(
        self,
        *,
        actor_id: UUID,
        account_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> RevokeGmailAccountResponse: ...


class DisabledGmailReadonlyOperatorProvider:
    async def register_account(self, **_kwargs: object) -> RegisterGmailAccountResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def list_accounts(self, **_kwargs: object) -> ListGmailAccountsResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def account_status(self, **_kwargs: object) -> GmailAccountStatusResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def request_sync(self, **_kwargs: object) -> RequestGmailSyncResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def list_sync_runs(self, **_kwargs: object) -> ListGmailSyncRunsResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def list_proposals(self, **_kwargs: object) -> ListGmailProposalsResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def review_proposal(self, **_kwargs: object) -> ReviewGmailProposalResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def reset_history(self, **_kwargs: object) -> ResetGmailHistoryResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")

    async def revoke_account(self, **_kwargs: object) -> RevokeGmailAccountResponse:
        raise GmailReadonlyUnavailable("gmail read-only operator runtime is not configured")


def install_gmail_readonly_operator_api(
    app: FastAPI,
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GmailReadonlyOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    app.include_router(
        create_gmail_readonly_operator_router(
            auth_service=auth_service,
            settings=settings,
            provider=provider,
            now_provider=now_provider,
        )
    )


def create_gmail_readonly_operator_router(
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GmailReadonlyOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> APIRouter:
    clock = now_provider or (lambda: datetime.now(UTC))
    origin_validator = OriginHostValidator(settings)
    router = APIRouter(prefix="/api/v1/internal/gmail-readonly", tags=["gmail-readonly-operator"])

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
        response_model=RegisterGmailAccountResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def register_account(
        body: RegisterGmailAccountRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> RegisterGmailAccountResponse:
        return await _translate_gmail_readonly_errors(
            provider.register_account(
                actor_id=principal.user_id,
                command_id=body.command_id,
                candidate_id=body.candidate_id,
                credential_handle=body.credential_handle,
                account_subject=body.account_subject,
                dedicated=body.dedicated,
                oauth_client_mode=body.oauth_client_mode,
                publishing_status=body.publishing_status,
                credential_store_evidence_sha256=body.credential_store_evidence_sha256,
                now=clock(),
            )
        )

    @router.get("/accounts", response_model=ListGmailAccountsResponse)
    async def list_accounts(
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGmailAccountsResponse:
        _require_limit(limit)
        return await _translate_gmail_readonly_errors(
            provider.list_accounts(actor_id=principal.user_id, limit=limit)
        )

    @router.get("/accounts/{account_id}", response_model=GmailAccountStatusResponse)
    async def account_status(
        account_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
    ) -> GmailAccountStatusResponse:
        return await _translate_gmail_readonly_errors(
            provider.account_status(actor_id=principal.user_id, account_id=account_id)
        )

    @router.post(
        "/accounts/{account_id}/syncs",
        response_model=RequestGmailSyncResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def request_sync(
        account_id: UUID,
        body: RequestGmailSyncRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> RequestGmailSyncResponse:
        return await _translate_gmail_readonly_errors(
            provider.request_sync(
                actor_id=principal.user_id,
                account_id=account_id,
                command_id=body.command_id,
                reason=body.reason,
                now=clock(),
            )
        )

    @router.get("/accounts/{account_id}/sync-runs", response_model=ListGmailSyncRunsResponse)
    async def list_sync_runs(
        account_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGmailSyncRunsResponse:
        _require_limit(limit)
        return await _translate_gmail_readonly_errors(
            provider.list_sync_runs(actor_id=principal.user_id, account_id=account_id, limit=limit)
        )

    @router.get("/accounts/{account_id}/proposals", response_model=ListGmailProposalsResponse)
    async def list_proposals(
        account_id: UUID,
        principal: AuthenticatedPrincipal = principal_dependency,
        limit: int = 100,
    ) -> ListGmailProposalsResponse:
        _require_limit(limit)
        return await _translate_gmail_readonly_errors(
            provider.list_proposals(actor_id=principal.user_id, account_id=account_id, limit=limit)
        )

    @router.post(
        "/accounts/{account_id}/proposals/{proposal_id}/review",
        response_model=ReviewGmailProposalResponse,
    )
    async def review_proposal(
        account_id: UUID,
        proposal_id: UUID,
        body: ReviewGmailProposalRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ReviewGmailProposalResponse:
        return await _translate_gmail_readonly_errors(
            provider.review_proposal(
                actor_id=principal.user_id,
                account_id=account_id,
                proposal_id=proposal_id,
                command_id=body.command_id,
                decision=body.decision,
                snapshot_sha256=body.snapshot_sha256,
                canonical_job_id=body.canonical_job_id,
                job_posting_id=body.job_posting_id,
                reason=body.reason,
                now=clock(),
            )
        )

    @router.post("/accounts/{account_id}/reset-history", response_model=ResetGmailHistoryResponse)
    async def reset_history(
        account_id: UUID,
        body: ResetGmailHistoryRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> ResetGmailHistoryResponse:
        return await _translate_gmail_readonly_errors(
            provider.reset_history(
                actor_id=principal.user_id,
                account_id=account_id,
                command_id=body.command_id,
                snapshot_sha256=body.snapshot_sha256,
                reason=body.reason,
                now=clock(),
            )
        )

    @router.post("/accounts/{account_id}/revoke", response_model=RevokeGmailAccountResponse)
    async def revoke_account(
        account_id: UUID,
        body: RevokeGmailAccountRequest,
        principal: AuthenticatedPrincipal = mutation_dependency,
    ) -> RevokeGmailAccountResponse:
        return await _translate_gmail_readonly_errors(
            provider.revoke_account(
                actor_id=principal.user_id,
                account_id=account_id,
                command_id=body.command_id,
                reason=body.reason,
                now=clock(),
            )
        )

    return router


async def _translate_gmail_readonly_errors[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except GmailReadonlyUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail read-only runtime is unavailable",
        ) from None
    except GmailReadonlyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request could not be processed",
        ) from None
    except GmailReadonlyNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 100",
        )


__all__: Sequence[str] = (
    "GMAIL_READONLY_SCOPE",
    "DisabledGmailReadonlyOperatorProvider",
    "GmailAccountStatus",
    "GmailAccountStatusResponse",
    "GmailAccountSummary",
    "GmailProposalKind",
    "GmailProposalStatus",
    "GmailProposalSummary",
    "GmailPublishingStatus",
    "GmailReadonlyConflict",
    "GmailReadonlyContract",
    "GmailReadonlyNotFound",
    "GmailReadonlyOperatorProvider",
    "GmailReadonlyUnavailable",
    "GmailSyncReason",
    "GmailSyncRunStatus",
    "GmailSyncRunSummary",
    "ListGmailAccountsResponse",
    "ListGmailProposalsResponse",
    "ListGmailSyncRunsResponse",
    "RegisterGmailAccountRequest",
    "RegisterGmailAccountResponse",
    "RequestGmailSyncRequest",
    "RequestGmailSyncResponse",
    "ResetGmailHistoryRequest",
    "ResetGmailHistoryResponse",
    "ReviewGmailProposalRequest",
    "ReviewGmailProposalResponse",
    "RevokeGmailAccountRequest",
    "RevokeGmailAccountResponse",
    "create_gmail_readonly_operator_router",
    "install_gmail_readonly_operator_api",
)
