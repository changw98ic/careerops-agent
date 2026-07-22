from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from careerops.application import autopilot_control
from careerops.application.autopilot_control import (
    AutopilotCommandState,
    AutopilotControlCapabilityState,
    CampaignGrantCard,
    CampaignGrantSnapshot,
    CampaignGrantStatus,
    DisabledAutopilotControlPlane,
    ReviewQueueItem,
    ReviewQueueSnapshot,
    ReviewQueueTab,
    ReviewResolutionMode,
)

NOW = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)
HASH = "a" * 64


@pytest.mark.asyncio
async def test_disabled_control_plane_returns_disabled_empty_campaign_snapshot() -> None:
    actor_id = uuid4()
    control_plane = DisabledAutopilotControlPlane()

    snapshot = await control_plane.campaign_grants(actor_id=actor_id, now=NOW)

    assert snapshot.capability.state is AutopilotControlCapabilityState.DISABLED
    assert snapshot.capability.enabled is False
    assert snapshot.capability.title_zh == "高自治求职未启用"
    assert snapshot.generated_at == NOW
    assert snapshot.grants == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("tab", tuple(ReviewQueueTab))
async def test_disabled_control_plane_returns_empty_review_queue_for_every_tab(
    tab: ReviewQueueTab,
) -> None:
    control_plane = DisabledAutopilotControlPlane()

    snapshot = await control_plane.review_queue(actor_id=uuid4(), tab=tab, now=NOW)

    assert snapshot.capability.state is AutopilotControlCapabilityState.DISABLED
    assert snapshot.active_tab is tab
    assert snapshot.items == ()
    assert {count.tab: count.count for count in snapshot.counts} == {
        ReviewQueueTab.PENDING: 0,
        ReviewQueueTab.EXCEPTIONS: 0,
        ReviewQueueTab.RECOVERY: 0,
    }


@pytest.mark.asyncio
async def test_disabled_control_plane_rejects_every_command_without_side_effects() -> None:
    control_plane = DisabledAutopilotControlPlane()

    activation = await control_plane.request_grant_activation(
        actor_id=uuid4(),
        grant_id=uuid4(),
    )
    revocation = await control_plane.request_grant_revocation(
        actor_id=uuid4(),
        grant_id=uuid4(),
    )
    resolution = await control_plane.request_review_resolution(
        actor_id=uuid4(),
        review_item_id=uuid4(),
        mode=ReviewResolutionMode.AGENT_APPROVABLE,
    )

    assert activation.state is AutopilotCommandState.REJECTED
    assert revocation.state is AutopilotCommandState.REJECTED
    assert resolution.state is AutopilotCommandState.REJECTED
    assert {
        activation.reason_code,
        revocation.reason_code,
        resolution.reason_code,
    } == {"AUTOPILOT_CONTROL_PLANE_DISABLED"}


def test_campaign_grant_card_freezes_collections_and_validates_caps_and_time() -> None:
    card = CampaignGrantCard(
        grant_id=uuid4(),
        campaign_id=uuid4(),
        version=1,
        status=CampaignGrantStatus.ACTIVE,
        title_zh="后端岗位投递授权",
        scope_summary_zh="仅限批准材料和指定 ATS 域名。",
        action_kinds=("submit_application",),
        channels=("ats",),
        target_hosts=("greenhouse.io",),
        approved_material_hashes=[HASH],  # type: ignore[arg-type]
        max_submissions=10,
        remaining_submissions=3,
        expires_at=NOW,
        created_at=NOW,
    )

    assert card.approved_material_hashes == (HASH,)
    assert isinstance(card.approved_material_hashes, tuple)
    with pytest.raises(FrozenInstanceError):
        card.remaining_submissions = 2  # type: ignore[misc]


def test_campaign_grant_card_rejects_unbounded_or_ambiguous_inputs() -> None:
    with pytest.raises(ValueError, match="remaining_submissions exceeds"):
        CampaignGrantCard(
            grant_id=uuid4(),
            campaign_id=uuid4(),
            version=1,
            status=CampaignGrantStatus.ACTIVE,
            title_zh="授权",
            scope_summary_zh="范围",
            action_kinds=("submit_application",),
            channels=("ats",),
            target_hosts=("greenhouse.io",),
            approved_material_hashes=(HASH,),
            max_submissions=1,
            remaining_submissions=2,
            expires_at=NOW,
            created_at=NOW,
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        CampaignGrantSnapshot(
            capability=DisabledAutopilotControlPlane()._capability(),
            generated_at=datetime(2026, 7, 19, 9, 30),
            grants=(),
        )


def test_review_queue_manual_only_is_distinct_from_agent_approvable() -> None:
    manual_only = ReviewQueueItem(
        item_id=uuid4(),
        tab=ReviewQueueTab.EXCEPTIONS,
        resolution_mode=ReviewResolutionMode.MANUAL_ONLY,
        reason_code="SITE_AUTOMATION_PROHIBITED",
        title_zh="站点禁止自动化",
        summary_zh="该目标只能人工处理, 不能通过审核继续由 agent 投递。",
        created_at=NOW,
        target_host="workdayjobs.com",
        payload_hash=HASH,
    )
    approvable = ReviewQueueItem(
        item_id=uuid4(),
        tab=ReviewQueueTab.PENDING,
        resolution_mode=ReviewResolutionMode.AGENT_APPROVABLE,
        reason_code="EVIDENCE_INCOMPLETE",
        title_zh="证据待确认",
        summary_zh="审核通过后可回到 agent 处理。",
        created_at=NOW,
    )

    assert manual_only.can_agent_continue_after_approval is False
    assert approvable.can_agent_continue_after_approval is True

    with pytest.raises(ValueError, match="manual-only"):
        ReviewQueueItem(
            item_id=uuid4(),
            tab=ReviewQueueTab.PENDING,
            resolution_mode=ReviewResolutionMode.MANUAL_ONLY,
            reason_code="SITE_AUTOMATION_PROHIBITED",
            title_zh="站点禁止自动化",
            summary_zh="不能作为 agent 可批准事项。",
            created_at=NOW,
        )


def test_review_queue_snapshot_requires_items_to_match_active_tab() -> None:
    item = ReviewQueueItem(
        item_id=uuid4(),
        tab=ReviewQueueTab.RECOVERY,
        resolution_mode=ReviewResolutionMode.REMEDIATION_REQUIRED,
        reason_code="ADAPTER_RELEASE_REVOKED",
        title_zh="适配器资格撤销",
        summary_zh="需要修复后重新进入发布资格验证。",
        created_at=NOW,
    )

    with pytest.raises(ValueError, match="active_tab"):
        ReviewQueueSnapshot(
            capability=DisabledAutopilotControlPlane()._capability(),
            generated_at=NOW,
            active_tab=ReviewQueueTab.EXCEPTIONS,
            items=(item,),
        )


def test_autopilot_control_contract_has_no_execution_or_infrastructure_imports() -> None:
    source = inspect.getsource(autopilot_control)
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]

    forbidden_fragments = (
        "sqlalchemy",
        "outbox",
        "browser",
        "credential",
        "provider",
        "requests",
        "httpx",
        "playwright",
    )
    for fragment in forbidden_fragments:
        assert all(fragment not in line for line in import_lines)
