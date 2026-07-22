from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.application.crawler_execution import CrawlerExecutionRequestSummary


class CrawlerExecutionConsoleCapabilityState(StrEnum):
    DISABLED = "disabled"
    REVIEWABLE = "reviewable"


@dataclass(frozen=True, slots=True)
class CrawlerExecutionConsoleCapability:
    state: CrawlerExecutionConsoleCapabilityState
    title_zh: str
    description_zh: str
    reason_code: str

    @property
    def can_review(self) -> bool:
        return self.state is CrawlerExecutionConsoleCapabilityState.REVIEWABLE


@dataclass(frozen=True, slots=True)
class CrawlerExecutionReviewSnapshot:
    capability: CrawlerExecutionConsoleCapability
    generated_at: datetime
    requests: tuple[CrawlerExecutionRequestSummary, ...]


@dataclass(frozen=True, slots=True)
class CrawlerExecutionConsoleCommandResult:
    accepted: bool
    reason_code: str
    title_zh: str
    description_zh: str


class CrawlerExecutionConsoleProvider(Protocol):
    async def pending_requests(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        limit: int = 50,
    ) -> CrawlerExecutionReviewSnapshot: ...

    async def approve_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult: ...

    async def reject_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        reason: str,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult: ...


class DisabledCrawlerExecutionConsoleProvider:
    async def pending_requests(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        limit: int = 50,
    ) -> CrawlerExecutionReviewSnapshot:
        del actor_id, limit
        return CrawlerExecutionReviewSnapshot(
            capability=_disabled_capability(),
            generated_at=now,
            requests=(),
        )

    async def approve_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        del actor_id, request_id, now
        return _disabled_command()

    async def reject_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        reason: str,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        del actor_id, request_id, reason, now
        return _disabled_command()


def _disabled_capability() -> CrawlerExecutionConsoleCapability:
    return CrawlerExecutionConsoleCapability(
        state=CrawlerExecutionConsoleCapabilityState.DISABLED,
        title_zh="Crawler 执行审核未启用",
        description_zh="当前控制台只展示空队列, 不会批准、拒绝、派发或运行任何 crawler.",
        reason_code="CRAWLER_EXECUTION_CONSOLE_DISABLED",
    )


def _disabled_command() -> CrawlerExecutionConsoleCommandResult:
    return CrawlerExecutionConsoleCommandResult(
        accepted=False,
        reason_code="CRAWLER_EXECUTION_CONSOLE_DISABLED",
        title_zh="Crawler 执行审核未启用",
        description_zh="当前没有注入已认证的 crawler 执行控制面。",
    )


__all__ = [
    "CrawlerExecutionConsoleCapability",
    "CrawlerExecutionConsoleCapabilityState",
    "CrawlerExecutionConsoleCommandResult",
    "CrawlerExecutionConsoleProvider",
    "CrawlerExecutionReviewSnapshot",
    "DisabledCrawlerExecutionConsoleProvider",
]
