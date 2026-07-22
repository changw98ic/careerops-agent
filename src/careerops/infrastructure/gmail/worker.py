from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import sleep
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.application.gmail_readonly import GmailMetadataSignal
from careerops.config import Settings
from careerops.infrastructure.gmail.client import (
    GmailApiError,
    GmailHistoryCursorExpired,
    GmailReadOnlyHttpClient,
)
from careerops.infrastructure.gmail.credentials import (
    GMAIL_READONLY_SCOPE,
    GmailAccessToken,
    GmailCredentialBrokerError,
    GmailCredentialHandle,
    UnixGmailCredentialBroker,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class GmailWorkerOutcome(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    SYNCED = "synced"
    DEFERRED = "deferred"
    FAILED = "failed"


class GmailCredentialError(RuntimeError):
    def __init__(self, error_code: str, *, status_code: int | None = None) -> None:
        super().__init__(error_code)
        self.error_code = _bounded_error_code(error_code)
        self.status_code = status_code


class GmailHistoryExpired(RuntimeError):
    """Gmail returned 404 for a history cursor; a full resync must be explicitly requested."""


@dataclass(frozen=True, slots=True)
class GmailMailboxSyncJob:
    mailbox_id: UUID
    owner_user_id: UUID
    candidate_id: UUID
    credential_handle: str
    account_subject: str
    last_history_id: str | None
    resync_required: bool
    sync_run_id: UUID
    lease_token: UUID
    anchor_history_id: str | None = None
    attempt_count: int = 0
    next_page_token: str | None = None


@dataclass(frozen=True, slots=True)
class GmailMessageBatch:
    messages: tuple[GmailMetadataSignal, ...]
    high_water_history_id: str
    next_page_token: str | None = None

    @property
    def complete(self) -> bool:
        return self.next_page_token is None


@dataclass(frozen=True, slots=True)
class GmailDomainProcessingResult:
    processed_messages: int
    review_proposals: int


@dataclass(frozen=True, slots=True)
class GmailMailboxSyncResult:
    mailbox_id: UUID
    outcome: GmailWorkerOutcome
    processed_messages: int = 0
    review_proposals: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class GmailWorkerRunResult:
    outcome: GmailWorkerOutcome
    claimed: int
    synced: int
    deferred: int
    failed: int
    processed_messages: int
    review_proposals: int
    disabled_reason: str | None = None
    mailbox_results: tuple[GmailMailboxSyncResult, ...] = ()


@dataclass(frozen=True, slots=True)
class GmailWorkerStatus:
    enabled: bool
    owner: str
    pending_mailboxes: int = 0
    disabled_reason: str | None = None


class GmailCredentialResolver(Protocol):
    def resolve(self, job: GmailMailboxSyncJob) -> GmailAccessToken: ...


class GmailReadOnlyProvider(Protocol):
    def fetch_batch(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch: ...


class GmailDomainProcessor(Protocol):
    def process(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        now: datetime,
    ) -> GmailDomainProcessingResult: ...


class GmailMailboxSyncRepository(Protocol):
    def claim_due(
        self, *, owner: str, now: datetime, limit: int
    ) -> tuple[GmailMailboxSyncJob, ...]: ...

    def count_due(self, *, now: datetime) -> int: ...

    def mark_succeeded(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        owner: str,
        now: datetime,
    ) -> None: ...

    def mark_incomplete_page(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        processing: GmailDomainProcessingResult,
        now: datetime,
    ) -> None: ...

    def mark_reauth_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None: ...

    def mark_resync_required(
        self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str
    ) -> None: ...

    def mark_retry(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None: ...

    def mark_failed(self, *, job: GmailMailboxSyncJob, now: datetime, error_code: str) -> None: ...


class DisabledGmailCredentialResolver:
    def resolve(self, job: GmailMailboxSyncJob) -> GmailAccessToken:
        del job
        raise GmailCredentialError("GOOGLE_OAUTH_DISABLED")


class DisabledGmailReadOnlyProvider:
    def fetch_batch(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch:
        del token, job, max_results
        raise GmailCredentialError("GOOGLE_OAUTH_DISABLED")


class DisabledGmailDomainProcessor:
    def process(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        now: datetime,
    ) -> GmailDomainProcessingResult:
        del job, batch, now
        raise RuntimeError("gmail domain processor is not configured")


class BrokerGmailCredentialResolver:
    def __init__(self, broker: UnixGmailCredentialBroker) -> None:
        self._broker = broker

    def resolve(self, job: GmailMailboxSyncJob) -> GmailAccessToken:
        try:
            return self._broker.resolve(
                GmailCredentialHandle(
                    opaque_handle=job.credential_handle,
                    account_subject=job.account_subject,
                    granted_scopes=(GMAIL_READONLY_SCOPE,),
                )
            )
        except GmailCredentialBrokerError as exc:
            raise GmailCredentialError("GMAIL_CREDENTIAL_BROKER_UNAVAILABLE") from exc


class HttpGmailReadOnlyProvider:
    def __init__(self, client: GmailReadOnlyHttpClient | None = None) -> None:
        self._client = client or GmailReadOnlyHttpClient()

    def fetch_batch(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch:
        try:
            profile = self._client.get_profile(token)
            if profile.email_address.casefold() != job.account_subject.casefold():
                raise GmailCredentialError(
                    "GMAIL_ACCOUNT_SUBJECT_MISMATCH",
                    status_code=403,
                )
            full_sync_anchor = job.anchor_history_id or profile.history_id
            return (
                self._fetch_history(token=token, job=job, max_results=max_results)
                if job.last_history_id
                else self._fetch_initial_page(
                    token=token,
                    job=job,
                    max_results=max_results,
                    profile_history_id=full_sync_anchor,
                )
            )
        except GmailHistoryCursorExpired as exc:
            raise GmailHistoryExpired() from exc
        except GmailApiError as exc:
            raise GmailCredentialError(exc.error_code, status_code=exc.status_code) from exc

    def _fetch_history(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
    ) -> GmailMessageBatch:
        if job.last_history_id is None:
            raise ValueError("history sync requires last_history_id")
        page = self._client.list_history(
            token,
            start_history_id=job.last_history_id,
            page_token=job.next_page_token,
            max_results=max_results,
        )
        message_ids = _message_ids_from_history(page.history)
        messages = tuple(
            self._metadata_signal(token=token, job=job, message_id=message_id)
            for message_id in message_ids
        )
        return GmailMessageBatch(
            messages=messages,
            high_water_history_id=page.checkpoint_history_id or job.last_history_id,
            next_page_token=page.next_page_token,
        )

    def _fetch_initial_page(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        max_results: int,
        profile_history_id: str,
    ) -> GmailMessageBatch:
        page = self._client.list_messages(
            token,
            page_token=job.next_page_token,
            max_results=max_results,
        )
        messages = tuple(
            self._metadata_signal(token=token, job=job, message_id=message.id)
            for message in page.messages
        )
        return GmailMessageBatch(
            messages=messages,
            high_water_history_id=profile_history_id,
            next_page_token=page.next_page_token,
        )

    def _metadata_signal(
        self,
        *,
        token: GmailAccessToken,
        job: GmailMailboxSyncJob,
        message_id: str,
    ) -> GmailMetadataSignal:
        message = self._client.get_message(
            token,
            message_id=message_id,
            metadata_headers=("Subject", "From", "Date"),
        )
        return GmailMetadataSignal(
            provider_message_id=message.id,
            provider_thread_id=message.thread_id,
            provider_history_id=message.history_id,
            account_subject=job.account_subject,
            headers=message.headers,
            label_ids=message.label_ids,
            snippet=message.snippet,
            received_at=message.internal_date,
        )


class MissingGmailDomainProcessor:
    def process(
        self,
        *,
        job: GmailMailboxSyncJob,
        batch: GmailMessageBatch,
        now: datetime,
    ) -> GmailDomainProcessingResult:
        del job, batch, now
        raise RuntimeError("gmail readonly domain repository is not configured")


class GmailReadOnlyWorker:
    def __init__(
        self,
        repository: GmailMailboxSyncRepository,
        credential_resolver: GmailCredentialResolver,
        provider: GmailReadOnlyProvider,
        domain_processor: GmailDomainProcessor,
        *,
        owner: str,
        enabled: bool,
        disabled_reason: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _validate_owner(owner)
        self._repository = repository
        self._credential_resolver = credential_resolver
        self._provider = provider
        self._domain_processor = domain_processor
        self._owner = owner
        self._enabled = enabled
        self._disabled_reason = disabled_reason
        self._clock = clock or (lambda: datetime.now(UTC))

    def status(self) -> GmailWorkerStatus:
        if not self._enabled:
            return GmailWorkerStatus(
                enabled=False,
                owner=self._owner,
                disabled_reason=self._disabled_reason or "GOOGLE_OAUTH_DISABLED",
            )
        return GmailWorkerStatus(
            enabled=True,
            owner=self._owner,
            pending_mailboxes=self._repository.count_due(now=self._clock()),
        )

    def run_once(self, *, limit: int = 10, max_results: int = 100) -> GmailWorkerRunResult:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if max_results < 1 or max_results > 500:
            raise ValueError("max_results must be between 1 and 500")
        if not self._enabled:
            return GmailWorkerRunResult(
                outcome=GmailWorkerOutcome.DISABLED,
                claimed=0,
                synced=0,
                deferred=0,
                failed=0,
                processed_messages=0,
                review_proposals=0,
                disabled_reason=self._disabled_reason or "GOOGLE_OAUTH_DISABLED",
            )
        jobs = self._repository.claim_due(owner=self._owner, now=self._clock(), limit=limit)
        if not jobs:
            return GmailWorkerRunResult(
                outcome=GmailWorkerOutcome.IDLE,
                claimed=0,
                synced=0,
                deferred=0,
                failed=0,
                processed_messages=0,
                review_proposals=0,
            )
        results = tuple(self._sync_job(job, max_results=max_results) for job in jobs)
        synced = sum(1 for result in results if result.outcome is GmailWorkerOutcome.SYNCED)
        failed = sum(1 for result in results if result.outcome is GmailWorkerOutcome.FAILED)
        deferred = sum(1 for result in results if result.outcome is GmailWorkerOutcome.DEFERRED)
        outcome = (
            GmailWorkerOutcome.FAILED
            if failed
            else GmailWorkerOutcome.DEFERRED
            if deferred
            else GmailWorkerOutcome.SYNCED
        )
        return GmailWorkerRunResult(
            outcome=outcome,
            claimed=len(jobs),
            synced=synced,
            deferred=deferred,
            failed=failed,
            processed_messages=sum(result.processed_messages for result in results),
            review_proposals=sum(result.review_proposals for result in results),
            mailbox_results=results,
        )

    def _sync_job(self, job: GmailMailboxSyncJob, *, max_results: int) -> GmailMailboxSyncResult:
        now = self._clock()
        try:
            token = self._credential_resolver.resolve(job)
            batch = self._provider.fetch_batch(token=token, job=job, max_results=max_results)
            processing = self._domain_processor.process(job=job, batch=batch, now=self._clock())
            if not batch.complete:
                self._repository.mark_incomplete_page(
                    job=job,
                    batch=batch,
                    processing=processing,
                    now=self._clock(),
                )
                return GmailMailboxSyncResult(
                    mailbox_id=job.mailbox_id,
                    outcome=GmailWorkerOutcome.DEFERRED,
                    processed_messages=processing.processed_messages,
                    review_proposals=processing.review_proposals,
                    error_code="GMAIL_PAGE_INCOMPLETE",
                )
            self._repository.mark_succeeded(
                job=job,
                batch=batch,
                processing=processing,
                owner=self._owner,
                now=self._clock(),
            )
            return GmailMailboxSyncResult(
                mailbox_id=job.mailbox_id,
                outcome=GmailWorkerOutcome.SYNCED,
                processed_messages=processing.processed_messages,
                review_proposals=processing.review_proposals,
            )
        except GmailCredentialError as error:
            if error.status_code in {401, 403}:
                self._repository.mark_reauth_required(
                    job=job,
                    now=now,
                    error_code="GMAIL_REAUTH_REQUIRED",
                )
                return GmailMailboxSyncResult(
                    mailbox_id=job.mailbox_id,
                    outcome=GmailWorkerOutcome.FAILED,
                    error_code="GMAIL_REAUTH_REQUIRED",
                )
            self._repository.mark_retry(job=job, now=now, error_code=error.error_code)
            return GmailMailboxSyncResult(
                mailbox_id=job.mailbox_id,
                outcome=GmailWorkerOutcome.DEFERRED,
                error_code=error.error_code,
            )
        except GmailHistoryExpired:
            self._repository.mark_resync_required(
                job=job,
                now=now,
                error_code="GMAIL_HISTORY_EXPIRED",
            )
            return GmailMailboxSyncResult(
                mailbox_id=job.mailbox_id,
                outcome=GmailWorkerOutcome.DEFERRED,
                error_code="GMAIL_HISTORY_EXPIRED",
            )
        except (AssertionError, TypeError, ValueError):
            self._repository.mark_failed(
                job=job,
                now=now,
                error_code="GMAIL_INTERNAL_CONTRACT_FAILED",
            )
            return GmailMailboxSyncResult(
                mailbox_id=job.mailbox_id,
                outcome=GmailWorkerOutcome.FAILED,
                error_code="GMAIL_INTERNAL_CONTRACT_FAILED",
            )


def create_runtime_gmail_worker(
    *,
    settings: Settings,
    engine: Engine,
    owner: str,
    credential_resolver: GmailCredentialResolver | None = None,
    provider: GmailReadOnlyProvider | None = None,
    domain_processor: GmailDomainProcessor | None = None,
) -> GmailReadOnlyWorker:
    from careerops.infrastructure.database.gmail_readonly import (
        PostgresGmailMailboxSyncRepository,
        PostgresGmailReadonlyDomainProcessor,
    )

    enabled = settings.google_oauth_enabled
    broker_socket = settings.mailbox_broker_socket or Path("/run/careerops-gmail/broker.sock")
    return GmailReadOnlyWorker(
        PostgresGmailMailboxSyncRepository(engine),
        credential_resolver
        or (
            BrokerGmailCredentialResolver(UnixGmailCredentialBroker(socket_path=broker_socket))
            if enabled
            else DisabledGmailCredentialResolver()
        ),
        provider or (HttpGmailReadOnlyProvider() if enabled else DisabledGmailReadOnlyProvider()),
        domain_processor
        or (
            PostgresGmailReadonlyDomainProcessor(engine)
            if enabled
            else DisabledGmailDomainProcessor()
        ),
        owner=owner,
        enabled=enabled,
        disabled_reason=None if enabled else "GOOGLE_OAUTH_DISABLED",
    )


def run_worker_forever(
    worker: GmailReadOnlyWorker,
    *,
    poll_seconds: float,
    limit: int,
    max_results: int,
) -> None:
    if poll_seconds <= 0 or poll_seconds > 60:
        raise ValueError("poll_seconds must be between 0 and 60")
    while True:
        worker.run_once(limit=limit, max_results=max_results)
        sleep(poll_seconds)


def _message_ids_from_history(history: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    message_ids: list[str] = []
    seen: set[str] = set()
    for event in history:
        for key in ("messagesAdded", "messages"):
            raw_items = event.get(key)
            if not isinstance(raw_items, list):
                continue
            items = cast("list[object]", raw_items)
            for item_object in items:
                raw_message: object = (
                    cast("Mapping[str, object]", item_object).get("message")
                    if isinstance(item_object, Mapping)
                    else item_object
                )
                if not isinstance(raw_message, Mapping):
                    continue
                message = cast("Mapping[str, object]", raw_message)
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
    return tuple(message_ids)


def _validate_owner(owner: str) -> None:
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("owner must be a bounded actor identifier")


def _bounded_error_code(error_code: str) -> str:
    if _ERROR_CODE.fullmatch(error_code) is None:
        raise ValueError("error_code must be a bounded machine code")
    return error_code


__all__: Sequence[str] = (
    "GMAIL_READONLY_SCOPE",
    "BrokerGmailCredentialResolver",
    "DisabledGmailCredentialResolver",
    "DisabledGmailDomainProcessor",
    "DisabledGmailReadOnlyProvider",
    "GmailCredentialError",
    "GmailCredentialResolver",
    "GmailDomainProcessingResult",
    "GmailDomainProcessor",
    "GmailHistoryExpired",
    "GmailMailboxSyncJob",
    "GmailMailboxSyncRepository",
    "GmailMailboxSyncResult",
    "GmailMessageBatch",
    "GmailReadOnlyProvider",
    "GmailReadOnlyWorker",
    "GmailWorkerOutcome",
    "GmailWorkerRunResult",
    "GmailWorkerStatus",
    "HttpGmailReadOnlyProvider",
    "MissingGmailDomainProcessor",
    "create_runtime_gmail_worker",
    "run_worker_forever",
)
