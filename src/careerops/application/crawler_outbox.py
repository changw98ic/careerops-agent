from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
)
from careerops.cli.crawl_sources import (
    CrawlSourceError,
    load_execution_approval,
    load_execution_request,
)
from careerops.cli.crawl_sources import (
    main as crawl_sources_main,
)

CRAWLER_EXECUTION_EVENT_KEY_PREFIX = "crawler-execution:"


class CrawlerDispatchError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CrawlerExecutionDispatch:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    execution_key: str
    config_path: Path
    request_path: Path
    approval_path: Path
    request_sha256: str
    approval_sha256: str


class CrawlerExecutionDispatchReader(Protocol):
    def load_dispatch(self, event: ClaimedOutboxEvent) -> CrawlerExecutionDispatch: ...


class CrawlerExecutionRunner(Protocol):
    def execute(
        self,
        *,
        root: Path,
        config_path: Path,
        request_path: Path,
        approval_path: Path,
    ) -> int: ...


class CrawlSourcesCliRunner:
    """Run the existing reviewed crawler executor with structured argv, not a shell."""

    def execute(
        self,
        *,
        root: Path,
        config_path: Path,
        request_path: Path,
        approval_path: Path,
    ) -> int:
        return crawl_sources_main(
            (
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                str(request_path),
                "--approval",
                str(approval_path),
                "--json",
            )
        )


class CrawlerExecutionOutboxSink:
    def __init__(
        self,
        reader: CrawlerExecutionDispatchReader,
        runner: CrawlerExecutionRunner,
        *,
        root: Path,
    ) -> None:
        self._reader = reader
        self._runner = runner
        self._root = root.resolve()

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        try:
            self._deliver(event)
        except CrawlerDispatchError as error:
            raise InternalDeliveryError(error.error_code, retryable=error.retryable) from error
        except CrawlSourceError as error:
            raise InternalDeliveryError("CRAWLER_EXECUTION_REJECTED", retryable=False) from error
        except OSError as error:
            # This is before `crawl_sources execute` has claimed the one-shot request. A shared
            # volume/file may be temporarily unavailable, so retrying does not duplicate a crawl.
            raise InternalDeliveryError("CRAWLER_ARTIFACT_UNAVAILABLE", retryable=True) from error

    def _deliver(self, event: ClaimedOutboxEvent) -> None:
        if event.event_type != OutboxEventType.WORKFLOW_SIGNAL.value:
            raise CrawlerDispatchError(
                "CRAWLER_EVENT_TYPE_UNSUPPORTED",
                "crawler outbox sink only accepts workflow_signal events",
            )
        if not event.event_key.startswith(CRAWLER_EXECUTION_EVENT_KEY_PREFIX):
            raise CrawlerDispatchError(
                "CRAWLER_EVENT_KEY_UNSUPPORTED",
                "crawler outbox sink received a non-crawler event",
            )
        dispatch = self._reader.load_dispatch(event)
        _assert_dispatch_matches_event(dispatch, event)
        if _sha256_file(dispatch.request_path) != dispatch.request_sha256:
            raise CrawlerDispatchError(
                "CRAWLER_REQUEST_ARTIFACT_DRIFT",
                "crawler execution request changed after dispatch binding",
            )
        if _sha256_file(dispatch.approval_path) != dispatch.approval_sha256:
            raise CrawlerDispatchError(
                "CRAWLER_APPROVAL_ARTIFACT_DRIFT",
                "crawler execution approval changed after dispatch binding",
            )

        request = load_execution_request(dispatch.request_path)
        approval = load_execution_approval(dispatch.approval_path)
        expected_key = crawler_execution_event_key(dispatch.execution_key)
        if dispatch.event_key != expected_key:
            raise CrawlerDispatchError(
                "CRAWLER_EVENT_KEY_BINDING_MISMATCH",
                "crawler event key is not bound to the persisted execution key",
            )
        if approval.request_id != request.request_id:
            raise CrawlerDispatchError(
                "CRAWLER_APPROVAL_BINDING_MISMATCH",
                "crawler approval is not bound to the reviewed request",
            )
        if approval.request_sha256 != dispatch.request_sha256:
            raise CrawlerDispatchError(
                "CRAWLER_APPROVAL_REQUEST_HASH_MISMATCH",
                "crawler approval does not approve the dispatched request bytes",
            )
        if _execution_claim_exists(self._root, request.request_id):
            raise CrawlerDispatchError(
                "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED",
                "crawler execution request was already claimed and needs result reconciliation",
            )

        try:
            result = self._runner.execute(
                root=self._root,
                config_path=dispatch.config_path,
                request_path=dispatch.request_path,
                approval_path=dispatch.approval_path,
            )
        except Exception as error:
            raise CrawlerDispatchError(
                "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED",
                "crawler execution outcome is unknown after the outbox event was claimed",
            ) from error
        if result != 0:
            error_code = "CRAWLER_EXECUTION_FAILED"
            if result != 1 and _execution_claim_exists(self._root, request.request_id):
                error_code = "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED"
            raise CrawlerDispatchError(
                error_code,
                "crawler execution failed after the outbox event was claimed",
            )


def crawler_execution_event_key(execution_key: str) -> str:
    if execution_key.startswith(CRAWLER_EXECUTION_EVENT_KEY_PREFIX):
        return execution_key
    return f"{CRAWLER_EXECUTION_EVENT_KEY_PREFIX}{execution_key}"


def _execution_claim_exists(root: Path, request_id: str) -> bool:
    return (
        root / "datasets" / "private" / "crawler-execution-claims" / f"{request_id}.json"
    ).exists()


def _assert_dispatch_matches_event(
    dispatch: CrawlerExecutionDispatch,
    event: ClaimedOutboxEvent,
) -> None:
    if (
        dispatch.event_id != event.event_id
        or dispatch.event_key != event.event_key
        or dispatch.action_intent_id != event.action_intent_id
        or dispatch.payload_version_id != event.payload_version_id
    ):
        raise CrawlerDispatchError(
            "CRAWLER_DISPATCH_EVENT_MISMATCH",
            "crawler dispatch metadata does not match the claimed outbox event",
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
