from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.base import Executable

from careerops.application.release_evidence import (
    IntentEvidenceTrace,
    JsonObject,
    JsonValue,
    ReleaseEvidenceArtifact,
    ReleaseQualificationBinding,
    ReleaseQualificationDecision,
    ReleaseQualificationDrilldown,
)
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    approval_requests,
    audit_events,
    autopilot_cap_reservations,
    autopilot_intent_authorizations,
    autopilot_kill_switch_events,
    autopilot_review_items,
    outbox_events,
    policy_decisions,
    provider_receipts,
    release_qualification_decisions,
    release_qualification_evidence,
    release_qualifications,
    side_effect_attempts,
)


class PostgresReleaseEvidenceReader:
    """Read-only evidence drill-down for release gates and intent execution chains."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def show(self, qualification_id: UUID) -> ReleaseQualificationDrilldown | None:
        qualification = (
            self._connection.execute(select_release_qualification_statement(qualification_id))
            .mappings()
            .one_or_none()
        )
        if qualification is None:
            return None
        evidence = tuple(
            _row_to_evidence(row)
            for row in self._connection.execute(
                select_release_qualification_evidence_statement(qualification_id)
            ).mappings()
        )
        decisions = tuple(
            _row_to_decision(row)
            for row in self._connection.execute(
                select_release_qualification_decisions_statement(qualification_id)
            ).mappings()
        )
        return ReleaseQualificationDrilldown(
            qualification=_row_to_qualification(qualification),
            evidence=evidence,
            decisions=decisions,
        )

    def trace_intent(self, intent_id: UUID) -> IntentEvidenceTrace | None:
        intent = (
            self._connection.execute(select_action_intent_statement(intent_id))
            .mappings()
            .one_or_none()
        )
        if intent is None:
            return None
        return IntentEvidenceTrace(
            action_intent=_json_row(intent),
            payload_versions=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_payload_versions_statement(intent_id)
                ).mappings()
            ),
            policy_decisions=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_policy_decisions_statement(intent_id)
                ).mappings()
            ),
            approval_requests=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_approval_requests_statement(intent_id)
                ).mappings()
            ),
            autopilot_authorizations=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_autopilot_authorizations_statement(intent_id)
                ).mappings()
            ),
            review_items=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_review_items_statement(intent_id)
                ).mappings()
            ),
            cap_reservations=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_cap_reservations_statement(intent_id)
                ).mappings()
            ),
            outbox_events=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_outbox_events_statement(intent_id)
                ).mappings()
            ),
            side_effect_attempts=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_side_effect_attempts_statement(intent_id)
                ).mappings()
            ),
            provider_receipts=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_provider_receipts_statement(intent_id)
                ).mappings()
            ),
            audit_events=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_audit_events_statement(intent_id)
                ).mappings()
            ),
            kill_switch_events=tuple(
                _json_row(row)
                for row in self._connection.execute(
                    select_kill_switch_events_statement(intent_id)
                ).mappings()
            ),
        )


def select_release_qualification_statement(
    qualification_id: UUID,
) -> sa.Select[tuple[object, ...]]:
    return sa.select(release_qualifications).where(release_qualifications.c.id == qualification_id)


def select_release_qualification_evidence_statement(
    qualification_id: UUID,
) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(release_qualification_evidence)
        .where(release_qualification_evidence.c.qualification_id == qualification_id)
        .order_by(
            release_qualification_evidence.c.created_at,
            release_qualification_evidence.c.id,
        )
    )


def select_release_qualification_decisions_statement(
    qualification_id: UUID,
) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(release_qualification_decisions)
        .where(release_qualification_decisions.c.qualification_id == qualification_id)
        .order_by(
            release_qualification_decisions.c.sequence,
        )
    )


def select_action_intent_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return sa.select(action_intents).where(action_intents.c.id == intent_id)


def select_payload_versions_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(action_payload_versions)
        .where(action_payload_versions.c.action_intent_id == intent_id)
        .order_by(action_payload_versions.c.version, action_payload_versions.c.created_at)
    )


def select_policy_decisions_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(policy_decisions)
        .where(policy_decisions.c.action_intent_id == intent_id)
        .order_by(policy_decisions.c.created_at, policy_decisions.c.id)
    )


def select_approval_requests_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(approval_requests)
        .where(approval_requests.c.action_intent_id == intent_id)
        .order_by(approval_requests.c.created_at, approval_requests.c.id)
    )


def select_autopilot_authorizations_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(autopilot_intent_authorizations)
        .where(autopilot_intent_authorizations.c.action_intent_id == intent_id)
        .order_by(
            autopilot_intent_authorizations.c.created_at,
            autopilot_intent_authorizations.c.id,
        )
    )


def select_review_items_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(autopilot_review_items)
        .join(
            autopilot_intent_authorizations,
            autopilot_review_items.c.authorization_id == autopilot_intent_authorizations.c.id,
        )
        .where(autopilot_intent_authorizations.c.action_intent_id == intent_id)
        .order_by(autopilot_review_items.c.created_at, autopilot_review_items.c.id)
    )


def select_cap_reservations_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(autopilot_cap_reservations)
        .where(autopilot_cap_reservations.c.action_intent_id == intent_id)
        .order_by(autopilot_cap_reservations.c.reserved_at, autopilot_cap_reservations.c.id)
    )


def select_outbox_events_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(outbox_events)
        .where(outbox_events.c.action_intent_id == intent_id)
        .order_by(outbox_events.c.created_at, outbox_events.c.id)
    )


def select_side_effect_attempts_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(side_effect_attempts)
        .where(side_effect_attempts.c.action_intent_id == intent_id)
        .order_by(side_effect_attempts.c.ordinal, side_effect_attempts.c.started_at)
    )


def select_provider_receipts_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(provider_receipts)
        .join(
            side_effect_attempts,
            provider_receipts.c.side_effect_attempt_id == side_effect_attempts.c.id,
        )
        .where(side_effect_attempts.c.action_intent_id == intent_id)
        .order_by(provider_receipts.c.received_at, provider_receipts.c.id)
    )


def select_audit_events_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(audit_events)
        .where(
            sa.or_(
                audit_events.c.resource_id == intent_id,
                audit_events.c.event_data["action_intent_id"].astext == str(intent_id),
            )
        )
        .order_by(audit_events.c.sequence)
    )


def select_kill_switch_events_statement(intent_id: UUID) -> sa.Select[tuple[object, ...]]:
    campaign_ids = (
        sa.select(autopilot_intent_authorizations.c.campaign_id)
        .where(autopilot_intent_authorizations.c.action_intent_id == intent_id)
        .union(
            sa.select(autopilot_cap_reservations.c.campaign_id).where(
                autopilot_cap_reservations.c.action_intent_id == intent_id
            )
        )
    )
    providers = (
        sa.select(
            sa.func.split_part(autopilot_cap_reservations.c.channel, ":", 1).label("provider")
        )
        .where(autopilot_cap_reservations.c.action_intent_id == intent_id)
        .distinct()
    )
    decision_anchor = sa.func.coalesce(
        sa.select(sa.func.min(autopilot_cap_reservations.c.reserved_at))
        .where(autopilot_cap_reservations.c.action_intent_id == intent_id)
        .scalar_subquery(),
        sa.select(sa.func.max(autopilot_intent_authorizations.c.created_at))
        .where(autopilot_intent_authorizations.c.action_intent_id == intent_id)
        .scalar_subquery(),
        sa.select(sa.func.max(policy_decisions.c.created_at))
        .where(policy_decisions.c.action_intent_id == intent_id)
        .scalar_subquery(),
        sa.select(action_intents.c.created_at)
        .where(action_intents.c.id == intent_id)
        .scalar_subquery(),
    )
    ranked = (
        sa.select(
            *autopilot_kill_switch_events.c,
            decision_anchor.label("decision_anchor_at"),
            sa.func.row_number()
            .over(
                partition_by=(
                    autopilot_kill_switch_events.c.scope_type,
                    autopilot_kill_switch_events.c.campaign_id,
                    autopilot_kill_switch_events.c.provider,
                ),
                order_by=(
                    autopilot_kill_switch_events.c.sequence.desc(),
                    autopilot_kill_switch_events.c.created_at.desc(),
                ),
            )
            .label("state_rank"),
        )
        .where(
            autopilot_kill_switch_events.c.created_at <= decision_anchor,
            sa.or_(
                autopilot_kill_switch_events.c.scope_type == "global",
                autopilot_kill_switch_events.c.campaign_id.in_(campaign_ids),
                autopilot_kill_switch_events.c.provider.in_(providers),
            ),
        )
        .subquery("ranked_kill_switch_state")
    )
    return (
        sa.select(
            *(ranked.c[column.name] for column in autopilot_kill_switch_events.c),
            ranked.c.decision_anchor_at,
        )
        .where(ranked.c.state_rank == 1)
        .order_by(
            ranked.c.sequence,
            ranked.c.created_at,
        )
    )


def _row_to_qualification(row: RowMapping) -> ReleaseQualificationBinding:
    return ReleaseQualificationBinding(
        id=cast("UUID", row["id"]),
        capability=cast("str", row["capability"]),
        action_name=cast("str", row["action_name"]),
        rollout_mode=cast("str", row["rollout_mode"]),
        provider=cast("str", row["provider"]),
        adapter_id=cast("str", row["adapter_id"]),
        adapter_version=cast("str", row["adapter_version"]),
        implementation_hash=cast("str", row["implementation_hash"]),
        config_hash=cast("str", row["config_hash"]),
        policy_hash=cast("str", row["policy_hash"]),
        dataset_hash=cast("str", row["dataset_hash"]),
        git_commit=cast("str", row["git_commit"]),
        image_digest=cast("str | None", row["image_digest"]),
        migration_revision=cast("str", row["migration_revision"]),
        oauth_scope_hash=cast("str", row["oauth_scope_hash"]),
        credential_ref_hash=cast("str", row["credential_ref_hash"]),
        network_policy_hash=cast("str", row["network_policy_hash"]),
        reconcile_policy_hash=cast("str", row["reconcile_policy_hash"]),
        hard_stop_hash=cast("str", row["hard_stop_hash"]),
        sensitive_field_hash=cast("str", row["sensitive_field_hash"]),
        kill_switch_hash=cast("str", row["kill_switch_hash"]),
        fixture_manifest_sha256=cast("str", row["fixture_manifest_sha256"]),
        fault_manifest_sha256=cast("str", row["fault_manifest_sha256"]),
        holdout_manifest_sha256=cast("str | None", row["holdout_manifest_sha256"]),
        live_sample_manifest_sha256=cast("str | None", row["live_sample_manifest_sha256"]),
        status=cast("str", row["status"]),
        requested_by_user_id=cast("UUID", row["requested_by_user_id"]),
        created_by=cast("str", row["created_by"]),
        expires_at=cast("datetime", row["expires_at"]),
        created_at=cast("datetime", row["created_at"]),
    )


def _row_to_evidence(row: RowMapping) -> ReleaseEvidenceArtifact:
    return ReleaseEvidenceArtifact(
        id=cast("UUID", row["id"]),
        evidence_kind=cast("str", row["evidence_kind"]),
        artifact_uri=cast("str", row["artifact_uri"]),
        artifact_sha256=cast("str", row["artifact_sha256"]),
        run_id=cast("str", row["run_id"]),
        runner_user_id=cast("UUID", row["runner_user_id"]),
        runner_actor=cast("str", row["runner_actor"]),
        metrics=cast("JsonObject", row["metrics"]),
        sample_manifest_sha256=cast("str", row["sample_manifest_sha256"]),
        created_at=cast("datetime", row["created_at"]),
    )


def _row_to_decision(row: RowMapping) -> ReleaseQualificationDecision:
    return ReleaseQualificationDecision(
        sequence=cast("int", row["sequence"]),
        id=cast("UUID", row["id"]),
        from_status=cast("str", row["from_status"]),
        to_status=cast("str", row["to_status"]),
        decision_role=cast("str", row["decision_role"]),
        actor_id=cast("str", row["actor_id"]),
        decided_by_user_id=cast("UUID | None", row["decided_by_user_id"]),
        reason=cast("str", row["reason"]),
        evidence_sha256=cast("str", row["evidence_sha256"]),
        evidence_ids=tuple(cast("Sequence[UUID]", row["evidence_ids"])),
        created_at=cast("datetime", row["created_at"]),
    )


def _json_row(row: RowMapping) -> JsonObject:
    output: dict[str, JsonValue] = {}
    for key, value in row.items():
        output[str(key)] = _json_value(value)
    return output


def _json_value(value: object) -> JsonValue:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        typed_mapping = cast("Mapping[object, object]", value)
        return {str(key): _json_value(nested) for key, nested in typed_mapping.items()}
    if isinstance(value, list | tuple):
        typed_sequence = cast("Sequence[object]", value)
        return [_json_value(item) for item in typed_sequence]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def compile_query_for_test(statement: sa.ClauseElement | Executable) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "PostgresReleaseEvidenceReader",
    "compile_query_for_test",
    "select_action_intent_statement",
    "select_audit_events_statement",
    "select_autopilot_authorizations_statement",
    "select_cap_reservations_statement",
    "select_kill_switch_events_statement",
    "select_outbox_events_statement",
    "select_payload_versions_statement",
    "select_policy_decisions_statement",
    "select_provider_receipts_statement",
    "select_release_qualification_decisions_statement",
    "select_release_qualification_evidence_statement",
    "select_release_qualification_statement",
    "select_review_items_statement",
    "select_side_effect_attempts_statement",
]
