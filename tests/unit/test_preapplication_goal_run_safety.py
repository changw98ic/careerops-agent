from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from careerops.infrastructure.goal_run_operator import _summary_from_record
from careerops.workflows.goal_run_contracts import GoalRunInput, GoalRunMode

OWNER_ID = UUID("10000000-0000-0000-0000-000000000001")
GOAL_RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
REGISTRY_ID = UUID("30000000-0000-0000-0000-000000000001")
REVIEW_ID = UUID("40000000-0000-0000-0000-000000000001")
SNAPSHOT_SHA256 = "a" * 64
NOW = datetime(2026, 7, 22, tzinfo=UTC)


def test_legacy_temporal_input_without_mode_defaults_to_gmail_dispatch() -> None:
    request = GoalRunInput(
        goal_run_id=GOAL_RUN_ID,
        owner_user_id=OWNER_ID,
        registry_id=REGISTRY_ID,
        fencing_token=UUID("50000000-0000-0000-0000-000000000001"),
    )

    assert request.mode is GoalRunMode.GMAIL_DISPATCH


def test_completed_preapplication_run_has_no_actionable_pending_review() -> None:
    """A retained audit item must never be projected as an actionable review."""

    record = SimpleNamespace(
        goal_run_id=GOAL_RUN_ID,
        actor_id=OWNER_ID,
        owner_user_id=OWNER_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        status="completed",
        phase="completed",
        context={
            "mode": "pre_application_only",
            "registry_id": str(REGISTRY_ID),
            "source_id": "public-ats",
        },
        review_item_id=REVIEW_ID,
        review_snapshot_sha256=SNAPSHOT_SHA256,
        review_kind="goal_run_pre_application_review.v1",
        review_payload={"snapshot_sha256": SNAPSHOT_SHA256},
        temporal_workflow_id="goal-run:preapp:test",
        created_at=NOW,
        updated_at=NOW,
        last_error_code=None,
    )

    summary = _summary_from_record(record)

    assert summary.status == "completed"
    assert summary.phase == "completed"
    assert summary.pending_review_id is None
    assert summary.pending_review is None
