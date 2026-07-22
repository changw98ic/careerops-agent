from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionRejected,
    CrawlerExecutionRequestDraft,
    CrawlerExecutionRequestSummary,
    CrawlerExecutionService,
)
from careerops.cli.crawl_sources import (
    CrawlExecutionApprovalDocument,
    CrawlExecutionRequestDocument,
    CrawlSourceError,
    approve_execution_request,
    load_execution_approval,
    load_execution_request,
)
from careerops.infrastructure.database.crawler_execution import (
    CrawlerExecutionRepositoryError,
    PostgresCrawlerExecutionRepository,
)
from careerops.web.crawler_execution import (
    CrawlerExecutionConsoleCapability,
    CrawlerExecutionConsoleCapabilityState,
    CrawlerExecutionConsoleCommandResult,
    CrawlerExecutionReviewSnapshot,
)

_REVIEW_DIRECTORY = Path("datasets/private/crawler-execution-reviews")
_APPROVAL_NOTE = "Authenticated console approval; the database decision is execution authority."
_APPROVAL_REASON = "Approved through the authenticated crawler review console."


class CrawlerExecutionConsoleError(RuntimeError):
    """A fail-closed local-evidence error that is safe to show as a command refusal."""

    def __init__(self, reason_code: str, description_zh: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.description_zh = description_zh


class RuntimeCrawlerExecutionConsoleProvider:
    """Authenticated crawler-review adapter backed by the API database capability.

    The local request/approval JSON files remain byte-bound evidence for the existing crawler CLI.
    Only the append-only database approval row can create the dispatch and outbox event.
    """

    def __init__(self, engine: Engine, *, workspace_root: Path) -> None:
        self._engine = engine
        self._root = workspace_root.resolve()

    async def pending_requests(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        limit: int = 50,
    ) -> CrawlerExecutionReviewSnapshot:
        _require_aware(now, "crawler review now")
        capability = _workspace_capability(self._root)
        if not capability.can_review:
            return CrawlerExecutionReviewSnapshot(
                capability=capability,
                generated_at=now,
                requests=(),
            )
        requests = await asyncio.to_thread(self._pending_requests, actor_id, limit)
        return CrawlerExecutionReviewSnapshot(
            capability=capability,
            generated_at=now,
            requests=requests,
        )

    async def approve_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        _require_aware(now, "crawler approval now")
        capability = _workspace_capability(self._root)
        if not capability.can_review:
            return _unavailable_command(capability)
        try:
            await asyncio.to_thread(self._approve_request, actor_id, request_id, now)
        except (
            CrawlerExecutionConsoleError,
            CrawlerExecutionRejected,
            CrawlerExecutionRepositoryError,
        ) as error:
            return _rejected_command(error)
        except CrawlSourceError as error:
            return _rejected_command(
                CrawlerExecutionConsoleError(
                    "CRAWLER_LOCAL_EVIDENCE_INVALID",
                    f"本地 crawler 审核证据无效: {error}",
                )
            )
        return CrawlerExecutionConsoleCommandResult(
            accepted=True,
            reason_code="CRAWLER_EXECUTION_APPROVED_AND_DISPATCHED",
            title_zh="Crawler 执行已批准",
            description_zh="已记录认证审批并写入受限 crawler outbox 队列。",
        )

    async def reject_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        reason: str,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        _require_aware(now, "crawler rejection now")
        capability = _workspace_capability(self._root)
        if not capability.can_review:
            return _unavailable_command(capability)
        try:
            await asyncio.to_thread(self._reject_request, actor_id, request_id, reason, now)
        except (
            CrawlerExecutionConsoleError,
            CrawlerExecutionRejected,
            CrawlerExecutionRepositoryError,
        ) as error:
            return _rejected_command(error)
        return CrawlerExecutionConsoleCommandResult(
            accepted=True,
            reason_code="CRAWLER_EXECUTION_REJECTED",
            title_zh="Crawler 执行已拒绝",
            description_zh="已记录认证拒绝; 不会创建 crawler outbox 派发。",
        )

    def _pending_requests(
        self,
        actor_id: UUID,
        limit: int,
    ) -> tuple[CrawlerExecutionRequestSummary, ...]:
        with self._engine.begin() as connection:
            repository = PostgresCrawlerExecutionRepository(connection)
            return CrawlerExecutionService(repository, repository).list_pending_requests(
                owner_user_id=actor_id,
                limit=limit,
            )

    def _approve_request(self, actor_id: UUID, request_id: UUID, now: datetime) -> None:
        with self._engine.begin() as connection:
            repository = PostgresCrawlerExecutionRepository(connection)
            service = CrawlerExecutionService(repository, repository)
            request = _load_owned_request(service, request_id=request_id, actor_id=actor_id)
            approval_path, approval_sha256 = _local_approval_evidence(
                root=self._root,
                request=request,
                actor_id=actor_id,
                now=now,
            )
            service.approve_request(
                request_id,
                CrawlerExecutionApprovalDraft(
                    request_id=request_id,
                    outcome=CrawlerExecutionApprovalOutcome.APPROVE,
                    decided_by_user_id=actor_id,
                    decision_reason=_APPROVAL_REASON,
                    decided_at=now,
                    approval_artifact_path=str(approval_path.relative_to(self._root)),
                    approval_artifact_sha256=approval_sha256,
                    local_cli_approval_present=True,
                ),
                now=now,
            )

    def _reject_request(
        self,
        actor_id: UUID,
        request_id: UUID,
        reason: str,
        now: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            repository = PostgresCrawlerExecutionRepository(connection)
            service = CrawlerExecutionService(repository, repository)
            _load_owned_request(service, request_id=request_id, actor_id=actor_id)
            service.reject_request(
                request_id,
                CrawlerExecutionApprovalDraft(
                    request_id=request_id,
                    outcome=CrawlerExecutionApprovalOutcome.REJECT,
                    decided_by_user_id=actor_id,
                    decision_reason=reason,
                    decided_at=now,
                ),
                now=now,
            )


def _load_owned_request(
    service: CrawlerExecutionService,
    *,
    request_id: UUID,
    actor_id: UUID,
) -> CrawlerExecutionRequestDraft:
    request = service.get_request(request_id)
    if request is None:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_NOT_FOUND",
            "该 crawler 请求不存在或已不再可审核。",
        )
    if request.owner_user_id != actor_id:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_NOT_OWNED",
            "当前登录用户无权审核该 crawler 请求。",
        )
    return request


def _local_approval_evidence(
    *,
    root: Path,
    request: CrawlerExecutionRequestDraft,
    actor_id: UUID,
    now: datetime,
) -> tuple[Path, str]:
    request_path = _safe_existing_review_file(root, request.request_artifact_path)
    request_document = load_execution_request(request_path)
    _assert_request_document_matches_record(request_document, request)

    approval_path = _approval_path(root, request.request_id)
    approval_document = _load_or_create_approval(
        approval_path,
        request_document=request_document,
        actor_id=actor_id,
        now=now,
    )
    _assert_approval_document_matches_record(
        approval_document,
        request_document=request_document,
        actor_id=actor_id,
    )
    return approval_path, _sha256_file(approval_path)


def _assert_request_document_matches_record(
    document: CrawlExecutionRequestDocument,
    request: CrawlerExecutionRequestDraft,
) -> None:
    expires_at = _parse_document_timestamp(document.expires_at, "execution request expiry")
    if (
        document.request_id != str(request.request_id)
        or document.fingerprint != request.request_sha256
        or document.manifest_sha256 != request.manifest_sha256
        or document.plan_sha256 != request.reviewed_plan_sha256
        or tuple(document.source_ids) != request.source_ids
        or document.reason != request.reason
        or expires_at != request.expires_at.astimezone(UTC)
    ):
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_DRIFT",
            "本地 crawler 请求证据已变化, 不能批准或派发。",
        )


def _approval_path(root: Path, request_id: UUID) -> Path:
    review_root = _review_root(root)
    return review_root / f"{request_id}.console-approval.json"


def _load_or_create_approval(
    path: Path,
    *,
    request_document: CrawlExecutionRequestDocument,
    actor_id: UUID,
    now: datetime,
) -> CrawlExecutionApprovalDocument:
    if path.is_symlink():
        raise CrawlerExecutionConsoleError(
            "CRAWLER_APPROVAL_ARTIFACT_PATH_INVALID",
            "crawler 审批证据路径不能是符号链接。",
        )
    if path.exists():
        return load_execution_approval(path)
    document = approve_execution_request(
        request_document,
        approved_by=str(actor_id),
        note=_APPROVAL_NOTE,
        now=now,
    )
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    document.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
    except FileExistsError:
        pass
    except OSError as error:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_APPROVAL_EVIDENCE_UNAVAILABLE",
            "无法写入本地 crawler 审批证据。",
        ) from error
    return load_execution_approval(path)


def _assert_approval_document_matches_record(
    document: CrawlExecutionApprovalDocument,
    *,
    request_document: CrawlExecutionRequestDocument,
    actor_id: UUID,
) -> None:
    if (
        document.request_id != request_document.request_id
        or document.request_sha256 != request_document.fingerprint
        or document.manifest_sha256 != request_document.manifest_sha256
        or document.plan_sha256 != request_document.plan_sha256
        or tuple(document.source_ids) != tuple(request_document.source_ids)
        or document.expires_at != request_document.expires_at
        or document.approved_by != str(actor_id)
    ):
        raise CrawlerExecutionConsoleError(
            "CRAWLER_APPROVAL_ARTIFACT_DRIFT",
            "本地 crawler 审批证据未绑定到当前认证用户和请求。",
        )


def _workspace_capability(root: Path) -> CrawlerExecutionConsoleCapability:
    try:
        _review_root(root)
    except CrawlerExecutionConsoleError as error:
        return CrawlerExecutionConsoleCapability(
            state=CrawlerExecutionConsoleCapabilityState.DISABLED,
            title_zh="Crawler 执行审核不可用",
            description_zh=error.description_zh,
            reason_code=error.reason_code,
        )
    return CrawlerExecutionConsoleCapability(
        state=CrawlerExecutionConsoleCapabilityState.REVIEWABLE,
        title_zh="Crawler 执行审核已连接",
        description_zh="认证审批会写入数据库记录, 并仅派发受限的 crawler outbox 事件。",
        reason_code="CRAWLER_EXECUTION_CONSOLE_READY",
    )


def _review_root(root: Path) -> Path:
    if not root.is_dir():
        raise CrawlerExecutionConsoleError(
            "CRAWLER_WORKSPACE_UNAVAILABLE",
            "crawler 工作目录不存在或不是目录。",
        )
    current = root
    for part in _REVIEW_DIRECTORY.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise CrawlerExecutionConsoleError(
                "CRAWLER_WORKSPACE_UNAVAILABLE",
                "crawler 审核目录缺失、不是目录或包含符号链接。",
            )
    return current


def _safe_existing_review_file(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID",
            "crawler 请求证据路径不安全。",
        )
    review_root = _review_root(root)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise CrawlerExecutionConsoleError(
                "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID",
                "crawler 请求证据路径不能包含符号链接。",
            )
    try:
        current.relative_to(review_root)
    except ValueError as error:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_PATH_INVALID",
            "crawler 请求证据必须位于受限审核目录。",
        ) from error
    try:
        mode = current.stat().st_mode
    except OSError as error:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_UNAVAILABLE",
            "crawler 请求证据不可读取。",
        ) from error
    if not stat.S_ISREG(mode):
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_UNAVAILABLE",
            "crawler 请求证据必须是普通文件。",
        )
    return current


def _parse_document_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_REQUEST_ARTIFACT_DRIFT",
            f"{label} 无效。",
        ) from error
    _require_aware(parsed, label)
    return parsed.astimezone(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise CrawlerExecutionConsoleError(
            "CRAWLER_APPROVAL_EVIDENCE_UNAVAILABLE",
            "crawler 审批证据不可读取。",
        ) from error
    return digest.hexdigest()


def _unavailable_command(
    capability: CrawlerExecutionConsoleCapability,
) -> CrawlerExecutionConsoleCommandResult:
    return CrawlerExecutionConsoleCommandResult(
        accepted=False,
        reason_code=capability.reason_code,
        title_zh=capability.title_zh,
        description_zh=capability.description_zh,
    )


def _rejected_command(
    error: CrawlerExecutionConsoleError
    | CrawlerExecutionRejected
    | CrawlerExecutionRepositoryError,
) -> CrawlerExecutionConsoleCommandResult:
    if isinstance(error, CrawlerExecutionConsoleError):
        return CrawlerExecutionConsoleCommandResult(
            accepted=False,
            reason_code=error.reason_code,
            title_zh="Crawler 执行未获批准",
            description_zh=error.description_zh,
        )
    if isinstance(error, CrawlerExecutionRejected):
        reason_code = error.reason_codes[0]
    else:
        reason_code = "CRAWLER_EXECUTION_REJECTED"
    return CrawlerExecutionConsoleCommandResult(
        accepted=False,
        reason_code=reason_code,
        title_zh="Crawler 执行未获批准",
        description_zh="请求不再符合认证审批或可派发条件。",
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "CrawlerExecutionConsoleError",
    "RuntimeCrawlerExecutionConsoleProvider",
]
