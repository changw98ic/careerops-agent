from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from careerops.application.autopilot_control import (
    AutopilotCommandResult,
    AutopilotCommandState,
    AutopilotControlCapability,
    AutopilotControlCapabilityState,
    CampaignGrantCard,
    CampaignGrantSnapshot,
    CampaignGrantStatus,
    ReviewQueueCount,
    ReviewQueueItem,
    ReviewQueueSnapshot,
    ReviewQueueTab,
    ReviewResolutionMode,
)
from careerops.infrastructure.database.schema import (
    autopilot_campaigns,
    autopilot_grant_revocations,
    autopilot_grant_versions,
    autopilot_intent_authorizations,
    autopilot_review_items,
)

_REVIEW_KIND_BY_TAB = {
    ReviewQueueTab.PENDING: ReviewQueueTab.PENDING.value,
    ReviewQueueTab.EXCEPTIONS: ReviewQueueTab.EXCEPTIONS.value,
    ReviewQueueTab.RECOVERY: ReviewQueueTab.RECOVERY.value,
}
_TAB_BY_REVIEW_KIND = {value: key for key, value in _REVIEW_KIND_BY_TAB.items()}


class RuntimeAutopilotControlPlane:
    """Read-only database projection for bounded-autopilot console state.

    Commands deliberately remain unavailable here. This adapter only shows actor-owned,
    unrevoked, unexpired control-plane state and never publishes outbox/provider/browser work.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    async def campaign_grants(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> CampaignGrantSnapshot:
        _require_aware(now, "autopilot control-plane now")
        try:
            grants = await asyncio.to_thread(self._campaign_grants, actor_id, now)
        except (KeyError, TypeError, ValueError, SQLAlchemyError):
            return CampaignGrantSnapshot(
                capability=_disabled_capability("AUTOPILOT_CONTROL_PLANE_DATABASE_UNAVAILABLE"),
                generated_at=now,
                grants=(),
            )
        return CampaignGrantSnapshot(
            capability=_read_only_capability(),
            generated_at=now,
            grants=grants,
        )

    async def review_queue(
        self,
        *,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> ReviewQueueSnapshot:
        _require_aware(now, "autopilot control-plane now")
        try:
            counts, items = await asyncio.to_thread(self._review_queue, actor_id, tab, now)
        except (KeyError, TypeError, ValueError, SQLAlchemyError):
            return ReviewQueueSnapshot(
                capability=_disabled_capability("AUTOPILOT_CONTROL_PLANE_DATABASE_UNAVAILABLE"),
                generated_at=now,
                active_tab=tab,
                counts=_empty_review_counts(),
                items=(),
            )
        return ReviewQueueSnapshot(
            capability=_read_only_capability(),
            generated_at=now,
            active_tab=tab,
            counts=counts,
            items=items,
        )

    async def request_grant_activation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _read_only_command()

    async def request_grant_revocation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _read_only_command()

    async def request_review_resolution(
        self,
        *,
        actor_id: UUID,
        review_item_id: UUID,
        mode: ReviewResolutionMode,
    ) -> AutopilotCommandResult:
        del actor_id, review_item_id, mode
        return _read_only_command()

    def _campaign_grants(self, actor_id: UUID, now: datetime) -> tuple[CampaignGrantCard, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                campaign_grants_statement(actor_id=actor_id, now=now)
            ).mappings()
            return tuple(_grant_card_from_row(row) for row in rows)

    def _review_queue(
        self,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> tuple[tuple[ReviewQueueCount, ...], tuple[ReviewQueueItem, ...]]:
        with self._engine.connect() as connection:
            count_row = connection.execute(
                review_queue_counts_statement(actor_id=actor_id, now=now)
            ).one()
            item_rows = connection.execute(
                review_queue_items_statement(actor_id=actor_id, tab=tab, now=now)
            ).mappings()
        counts = (
            ReviewQueueCount(tab=ReviewQueueTab.PENDING, count=cast("int", count_row[0])),
            ReviewQueueCount(tab=ReviewQueueTab.EXCEPTIONS, count=cast("int", count_row[1])),
            ReviewQueueCount(tab=ReviewQueueTab.RECOVERY, count=cast("int", count_row[2])),
        )
        return counts, tuple(_review_item_from_row(row, active_tab=tab) for row in item_rows)


def campaign_grants_statement(*, actor_id: UUID, now: datetime) -> sa.Select[tuple[Any, ...]]:
    used_authorizations = (
        sa.select(sa.func.count())
        .select_from(autopilot_intent_authorizations)
        .where(
            autopilot_intent_authorizations.c.grant_version_id == autopilot_grant_versions.c.id,
            autopilot_intent_authorizations.c.authorization_outcome == "allow_autopilot_submission",
            autopilot_intent_authorizations.c.expires_at > now,
        )
        .scalar_subquery()
    )
    return (
        sa.select(
            autopilot_grant_versions.c.id.label("grant_id"),
            autopilot_campaigns.c.id.label("campaign_id"),
            autopilot_campaigns.c.name.label("campaign_name"),
            autopilot_campaigns.c.objective.label("scope_summary"),
            autopilot_grant_versions.c.version,
            autopilot_grant_versions.c.allowed_action_kinds,
            autopilot_grant_versions.c.allowed_channels,
            autopilot_grant_versions.c.allowed_target_hosts,
            autopilot_grant_versions.c.material_hashes,
            autopilot_grant_versions.c.max_total_submissions,
            (
                autopilot_grant_versions.c.max_total_submissions - cast("Any", used_authorizations)
            ).label("remaining_submissions"),
            autopilot_grant_versions.c.expires_at,
            autopilot_grant_versions.c.created_at,
        )
        .select_from(
            autopilot_grant_versions.join(
                autopilot_campaigns,
                autopilot_campaigns.c.id == autopilot_grant_versions.c.campaign_id,
            ).outerjoin(
                autopilot_grant_revocations,
                autopilot_grant_revocations.c.grant_version_id == autopilot_grant_versions.c.id,
            )
        )
        .where(
            autopilot_campaigns.c.owner_user_id == actor_id,
            autopilot_grant_versions.c.expires_at > now,
            autopilot_grant_revocations.c.id.is_(None),
        )
        .order_by(autopilot_grant_versions.c.created_at.desc(), autopilot_grant_versions.c.id)
    )


def review_queue_counts_statement(
    *,
    actor_id: UUID,
    now: datetime,
) -> sa.Select[tuple[int, int, int]]:
    base = _owned_live_review_rows(actor_id=actor_id, now=now)
    pending = (
        sa.select(sa.func.count())
        .select_from(base)
        .where(
            base.c.review_kind == _REVIEW_KIND_BY_TAB[ReviewQueueTab.PENDING],
            base.c.resolution_mode == ReviewResolutionMode.AGENT_APPROVABLE.value,
        )
        .scalar_subquery()
    )
    exceptions = (
        sa.select(sa.func.count())
        .select_from(base)
        .where(
            sa.or_(
                base.c.review_kind == _REVIEW_KIND_BY_TAB[ReviewQueueTab.EXCEPTIONS],
                base.c.resolution_mode == ReviewResolutionMode.MANUAL_ONLY.value,
            )
        )
        .scalar_subquery()
    )
    recovery = (
        sa.select(sa.func.count())
        .select_from(base)
        .where(
            sa.or_(
                base.c.review_kind == _REVIEW_KIND_BY_TAB[ReviewQueueTab.RECOVERY],
                base.c.resolution_mode == ReviewResolutionMode.REMEDIATION_REQUIRED.value,
            )
        )
        .scalar_subquery()
    )
    return sa.select(pending, exceptions, recovery)


def review_queue_items_statement(
    *,
    actor_id: UUID,
    tab: ReviewQueueTab,
    now: datetime,
    limit: int = 100,
) -> sa.Select[tuple[Any, ...]]:
    base = _owned_live_review_rows(actor_id=actor_id, now=now)
    statement = sa.select(
        base.c.item_id,
        base.c.campaign_id,
        base.c.grant_id,
        base.c.action_intent_id,
        base.c.payload_hash,
        base.c.review_kind,
        base.c.resolution_mode,
        base.c.reason_codes,
        base.c.snapshot,
        base.c.created_at,
    ).select_from(base)
    if tab is ReviewQueueTab.PENDING:
        statement = statement.where(
            base.c.review_kind == _REVIEW_KIND_BY_TAB[tab],
            base.c.resolution_mode == ReviewResolutionMode.AGENT_APPROVABLE.value,
        )
    elif tab is ReviewQueueTab.EXCEPTIONS:
        statement = statement.where(
            sa.or_(
                base.c.review_kind == _REVIEW_KIND_BY_TAB[tab],
                base.c.resolution_mode == ReviewResolutionMode.MANUAL_ONLY.value,
            )
        )
    else:
        statement = statement.where(
            sa.or_(
                base.c.review_kind == _REVIEW_KIND_BY_TAB[tab],
                base.c.resolution_mode == ReviewResolutionMode.REMEDIATION_REQUIRED.value,
            )
        )
    return statement.order_by(base.c.created_at, base.c.item_id).limit(limit)


def _owned_live_review_rows(*, actor_id: UUID, now: datetime) -> sa.Subquery:
    return (
        sa.select(
            autopilot_review_items.c.id.label("item_id"),
            autopilot_campaigns.c.id.label("campaign_id"),
            autopilot_grant_versions.c.id.label("grant_id"),
            autopilot_intent_authorizations.c.action_intent_id,
            autopilot_intent_authorizations.c.payload_hash,
            autopilot_review_items.c.review_kind,
            autopilot_review_items.c.resolution_mode,
            autopilot_review_items.c.reason_codes,
            autopilot_review_items.c.snapshot,
            autopilot_review_items.c.created_at,
        )
        .select_from(
            autopilot_review_items.join(
                autopilot_intent_authorizations,
                autopilot_intent_authorizations.c.id == autopilot_review_items.c.authorization_id,
            )
            .join(
                autopilot_grant_versions,
                autopilot_grant_versions.c.id == autopilot_intent_authorizations.c.grant_version_id,
            )
            .join(
                autopilot_campaigns,
                autopilot_campaigns.c.id == autopilot_grant_versions.c.campaign_id,
            )
            .outerjoin(
                autopilot_grant_revocations,
                autopilot_grant_revocations.c.grant_version_id == autopilot_grant_versions.c.id,
            )
        )
        .where(
            autopilot_campaigns.c.owner_user_id == actor_id,
            autopilot_grant_versions.c.expires_at > now,
            autopilot_intent_authorizations.c.expires_at > now,
            autopilot_grant_revocations.c.id.is_(None),
        )
        .subquery()
    )


def _grant_card_from_row(row: RowMapping) -> CampaignGrantCard:
    max_submissions = cast("int", row["max_total_submissions"])
    remaining = max(0, cast("int", row["remaining_submissions"]))
    return CampaignGrantCard(
        grant_id=cast("UUID", row["grant_id"]),
        campaign_id=cast("UUID", row["campaign_id"]),
        version=cast("int", row["version"]),
        status=CampaignGrantStatus.ACTIVE,
        title_zh=cast("str", row["campaign_name"]),
        scope_summary_zh=cast("str", row["scope_summary"]),
        action_kinds=_json_string_tuple(row["allowed_action_kinds"]),
        channels=_json_string_tuple(row["allowed_channels"]),
        target_hosts=_json_string_tuple(row["allowed_target_hosts"]),
        approved_material_hashes=_json_string_tuple(row["material_hashes"]),
        max_submissions=max_submissions,
        remaining_submissions=remaining,
        expires_at=cast("datetime", row["expires_at"]),
        created_at=cast("datetime", row["created_at"]),
    )


def _review_item_from_row(row: RowMapping, *, active_tab: ReviewQueueTab) -> ReviewQueueItem:
    snapshot = _json_mapping(row["snapshot"])
    reason_codes = _json_string_tuple(row["reason_codes"])
    reason_code = reason_codes[0] if reason_codes else "AUTOPILOT_REVIEW_REQUIRED"
    return ReviewQueueItem(
        item_id=cast("UUID", row["item_id"]),
        tab=active_tab,
        resolution_mode=_resolution_mode(cast("str", row["resolution_mode"])),
        reason_code=reason_code,
        title_zh=_bounded_text(snapshot.get("title_zh"), fallback="需要人工审核"),
        summary_zh=_bounded_text(snapshot.get("summary_zh"), fallback="该事项需要在控制台复核。"),
        campaign_id=cast("UUID", row["campaign_id"]),
        grant_id=cast("UUID", row["grant_id"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        target_host=_optional_bounded_identifier(snapshot.get("target_host")),
        payload_hash=cast("str", row["payload_hash"]),
        created_at=cast("datetime", row["created_at"]),
    )


def _json_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = cast("list[object]", value)
    return tuple(item for item in values if isinstance(item, str))


def _json_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast("Mapping[str, object]", value)


def _resolution_mode(value: str) -> ReviewResolutionMode:
    if value == ReviewResolutionMode.MANUAL_ONLY.value:
        return ReviewResolutionMode.MANUAL_ONLY
    if value == ReviewResolutionMode.REMEDIATION_REQUIRED.value:
        return ReviewResolutionMode.REMEDIATION_REQUIRED
    return ReviewResolutionMode.AGENT_APPROVABLE


def _bounded_text(value: object, *, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value[:240]


def _optional_bounded_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:96]


def _empty_review_counts() -> tuple[ReviewQueueCount, ...]:
    return tuple(ReviewQueueCount(tab=tab, count=0) for tab in ReviewQueueTab)


def _read_only_capability() -> AutopilotControlCapability:
    return AutopilotControlCapability(
        state=AutopilotControlCapabilityState.READ_ONLY,
        title_zh="高自治控制面只读",
        description_zh="当前只展示授权和审核状态, 不会批准、投递或触发外部动作。",
        reason_code="AUTOPILOT_CONTROL_PLANE_READ_ONLY",
    )


def _disabled_capability(reason_code: str) -> AutopilotControlCapability:
    return AutopilotControlCapability(
        state=AutopilotControlCapabilityState.DISABLED,
        title_zh="高自治控制面不可用",
        description_zh="数据库投影不可用, 已按失败关闭处理。",
        reason_code=reason_code,
    )


def _read_only_command() -> AutopilotCommandResult:
    return AutopilotCommandResult(
        state=AutopilotCommandState.REJECTED,
        reason_code="AUTOPILOT_CONTROL_PLANE_READ_ONLY",
        title_zh="操作未启用",
        description_zh="当前控制面仅开放只读投影, 未执行任何授权、撤销或审核操作。",
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "RuntimeAutopilotControlPlane",
    "campaign_grants_statement",
    "review_queue_counts_statement",
    "review_queue_items_statement",
]
