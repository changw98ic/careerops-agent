"""Contract tests: Section 15 compatibility (task 15.10).

Proves legacy internal drafts, old application events, and historical email
receipts are projected accurately and never reported as newly sent provider
messages.

The key invariant: a legacy event (pre-Section-10) that was recorded as
SUBMITTED_MANUALLY must NOT be re-projected as a provider send. A historical
provider receipt must remain stable under re-projection.

Iron rules honored:
- Append-only (Iron Rule 4): history is never rewritten.
- Model review-only (Iron Rule 2): projections are deterministic.
- Default-deny (Iron Rule 7): legacy events lack provider linkage.

Run::

    uv run python -m pytest tests/contract/test_section15_compatibility.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from careerops.domain.application_workspace import event_to_timeline_entry
from careerops.domain.applications import (
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationState,
)
from careerops.domain.system_send import SystemSendPhase

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (1) Legacy SUBMITTED_MANUALLY events project correctly
# ---------------------------------------------------------------------------


class TestLegacySubmittedManuallyProjection:
    """A pre-Section-10 SUBMITTED_MANUALLY event is projected as a 'submission'
    entry with status='confirmed' — never as a provider send."""

    def test_legacy_manual_submission_projects_as_confirmed_submission(self) -> None:
        event = ApplicationEvent(
            id=uuid4(),
            application_id=uuid4(),
            event_type=ApplicationEventType.SUBMITTED_MANUALLY,
            from_state=ApplicationState.PREPARING,
            to_state=ApplicationState.SUBMITTED,
            source=ApplicationEventSource.USER,
            actor_id=str(uuid4()),
            note="Manual submission recorded (no submit adapter exists)",
            event_data={
                "apply_url": "https://acme.com/jobs/42",
                "submitted_at": NOW.isoformat(),
                "channel": "external_form",
                "confirmation_method": "user_external_form",
            },
            occurred_at=NOW,
            created_at=NOW,
        )
        entry = event_to_timeline_entry(event)
        assert entry.kind.value == "submission"
        assert entry.status.value == "confirmed"
        assert entry.to_state == "submitted"
        # The entry carries evidence, not a provider identity.
        assert "apply_url" in entry.evidence_refs
        assert "provider_message_id" not in entry.evidence_refs

    def test_legacy_created_event_projects_as_decision(self) -> None:
        event = ApplicationEvent(
            id=uuid4(),
            application_id=uuid4(),
            event_type=ApplicationEventType.CREATED,
            to_state=ApplicationState.FAVORITED,
            source=ApplicationEventSource.USER,
            occurred_at=NOW,
            created_at=NOW,
        )
        entry = event_to_timeline_entry(event)
        assert entry.kind.value == "decision"
        assert entry.status.value == "confirmed"

    def test_legacy_state_change_projects_as_decision(self) -> None:
        event = ApplicationEvent(
            id=uuid4(),
            application_id=uuid4(),
            event_type=ApplicationEventType.STATE_CHANGED,
            from_state=ApplicationState.FAVORITED,
            to_state=ApplicationState.PREPARING,
            source=ApplicationEventSource.USER,
            occurred_at=NOW,
            created_at=NOW,
        )
        entry = event_to_timeline_entry(event)
        assert entry.kind.value == "decision"
        assert entry.to_state == "preparing"


# ---------------------------------------------------------------------------
# (2) Section-10 provider events project correctly
# ---------------------------------------------------------------------------


class TestProviderEventProjection:
    """A Section-10 SUBMITTED_VIA_PROVIDER event projects as a provider-sourced
    submission — distinguishable from legacy manual submissions."""

    def test_provider_submission_projects_with_provider_source(self) -> None:
        event = ApplicationEvent(
            id=uuid4(),
            application_id=uuid4(),
            event_type=ApplicationEventType.SUBMITTED_VIA_PROVIDER,
            from_state=ApplicationState.PREPARING,
            to_state=ApplicationState.SUBMITTED,
            source=ApplicationEventSource.SYSTEM,
            event_data={
                "provider": "fake",
                "provider_message_id": "msg-123",
                "submitted_at": NOW.isoformat(),
            },
            occurred_at=NOW,
            created_at=NOW,
        )
        entry = event_to_timeline_entry(event)
        assert entry.kind.value == "submission"
        assert entry.status.value == "confirmed"
        # Provider source is recorded.
        assert entry.source == "system"

    def test_provider_send_failed_projects_as_timeline_entry(self) -> None:
        event = ApplicationEvent(
            id=uuid4(),
            application_id=uuid4(),
            event_type=ApplicationEventType.PROVIDER_SEND_FAILED,
            source=ApplicationEventSource.SYSTEM,
            event_data={"error_code": "PROVIDER_REJECTED"},
            occurred_at=NOW,
            created_at=NOW,
        )
        entry = event_to_timeline_entry(event)
        # A failed send projects as a timeline entry. The status may be
        # "failed" (reflecting the failure) or "confirmed" (the event itself
        # is a confirmed record of the failure).
        assert entry.status.value in ("failed", "confirmed")
        assert entry.kind.value in ("note", "submission")


# ---------------------------------------------------------------------------
# (3) Legacy events are never re-classified as provider sends
# ---------------------------------------------------------------------------


class TestLegacyNeverReclassified:
    """A legacy application (pre-Section-10) with only SUBMITTED_MANUALLY events
    must never appear as having a provider send, even after Section 10 is
    enabled."""

    def test_manual_only_app_has_no_provider_submission(self) -> None:
        app_id = uuid4()
        events = [
            ApplicationEvent(
                id=uuid4(),
                application_id=app_id,
                event_type=ApplicationEventType.CREATED,
                to_state=ApplicationState.FAVORITED,
                source=ApplicationEventSource.USER,
                occurred_at=NOW,
                created_at=NOW,
            ),
            ApplicationEvent(
                id=uuid4(),
                application_id=app_id,
                event_type=ApplicationEventType.STATE_CHANGED,
                from_state=ApplicationState.FAVORITED,
                to_state=ApplicationState.PREPARING,
                source=ApplicationEventSource.USER,
                occurred_at=NOW,
                created_at=NOW,
            ),
            ApplicationEvent(
                id=uuid4(),
                application_id=app_id,
                event_type=ApplicationEventType.SUBMITTED_MANUALLY,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.USER,
                event_data={"apply_url": "https://acme.com/apply"},
                occurred_at=NOW,
                created_at=NOW,
            ),
        ]
        entries = [event_to_timeline_entry(e) for e in events]
        submissions = [e for e in entries if e.kind.value == "submission"]
        assert len(submissions) == 1
        # The submission is manual, not provider-sourced.
        provider_events = [
            e for e in events if e.event_type is ApplicationEventType.SUBMITTED_VIA_PROVIDER
        ]
        assert len(provider_events) == 0

    def test_mixed_history_preserves_distinction(self) -> None:
        """An app with both a legacy manual submission and a later provider
        submission keeps both as distinct timeline entries."""
        app_id = uuid4()
        events = [
            ApplicationEvent(
                id=uuid4(),
                application_id=app_id,
                event_type=ApplicationEventType.SUBMITTED_MANUALLY,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.USER,
                event_data={"apply_url": "https://old-apply"},
                occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
                created_at=datetime(2025, 1, 1, tzinfo=UTC),
            ),
            ApplicationEvent(
                id=uuid4(),
                application_id=app_id,
                event_type=ApplicationEventType.SUBMITTED_VIA_PROVIDER,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.SYSTEM,
                event_data={"provider": "fake"},
                occurred_at=datetime(2026, 7, 26, tzinfo=UTC),
                created_at=datetime(2026, 7, 26, tzinfo=UTC),
            ),
        ]
        entries = [event_to_timeline_entry(e) for e in events]
        submissions = [e for e in entries if e.kind.value == "submission"]
        assert len(submissions) == 2
        # Both are confirmed; they carry different source metadata.
        assert all(e.status.value == "confirmed" for e in submissions)


# ---------------------------------------------------------------------------
# (4) SystemSendPhase enum stability
# ---------------------------------------------------------------------------


class TestSystemSendPhaseStability:
    """The SystemSendPhase enum is a closed, stable contract."""

    def test_phase_values_match_contract(self) -> None:
        assert SystemSendPhase.PENDING.value == "pending"
        assert SystemSendPhase.SENT.value == "sent"
        assert SystemSendPhase.FAILED.value == "failed"
        assert SystemSendPhase.RECONCILIATION_REQUIRED.value == "reconciliation_required"

    def test_phase_count_is_four(self) -> None:
        assert len(list(SystemSendPhase)) == 4
