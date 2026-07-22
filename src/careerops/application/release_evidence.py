from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
JsonObject = Mapping[str, JsonValue]

_REDACTED = "[redacted]"
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "secret",
    "session",
    "token",
)


def redact_json(value: JsonValue) -> JsonValue:
    """Return a JSON-safe value with token/secret bodies removed.

    Release evidence is an operator drill-down surface.  It should show hashes,
    IDs, state transitions, and artifact bindings, but never raw credential or
    token material if an upstream audit payload accidentally contains it.
    """

    if isinstance(value, Mapping):
        redacted: dict[str, JsonValue] = {}
        for key, nested in value.items():
            lowered = key.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_json(nested)
        return redacted
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return [redact_json(item) for item in value]


def isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


@dataclass(frozen=True, slots=True)
class ReleaseQualificationBinding:
    id: UUID
    capability: str
    action_name: str
    rollout_mode: str
    provider: str
    adapter_id: str
    adapter_version: str
    implementation_hash: str
    config_hash: str
    policy_hash: str
    dataset_hash: str
    git_commit: str
    image_digest: str | None
    migration_revision: str
    oauth_scope_hash: str
    credential_ref_hash: str
    network_policy_hash: str
    reconcile_policy_hash: str
    hard_stop_hash: str
    sensitive_field_hash: str
    kill_switch_hash: str
    fixture_manifest_sha256: str
    fault_manifest_sha256: str
    holdout_manifest_sha256: str | None
    live_sample_manifest_sha256: str | None
    status: str
    requested_by_user_id: UUID
    created_by: str
    expires_at: datetime
    created_at: datetime

    def to_json(self) -> JsonObject:
        return {
            "id": str(self.id),
            "capability": self.capability,
            "action_name": self.action_name,
            "rollout_mode": self.rollout_mode,
            "provider": self.provider,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "implementation_hash": self.implementation_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "dataset_hash": self.dataset_hash,
            "git_commit": self.git_commit,
            "image_digest": self.image_digest,
            "migration_revision": self.migration_revision,
            "oauth_scope_hash": self.oauth_scope_hash,
            "credential_ref_hash": self.credential_ref_hash,
            "network_policy_hash": self.network_policy_hash,
            "reconcile_policy_hash": self.reconcile_policy_hash,
            "hard_stop_hash": self.hard_stop_hash,
            "sensitive_field_hash": self.sensitive_field_hash,
            "kill_switch_hash": self.kill_switch_hash,
            "fixture_manifest_sha256": self.fixture_manifest_sha256,
            "fault_manifest_sha256": self.fault_manifest_sha256,
            "holdout_manifest_sha256": self.holdout_manifest_sha256,
            "live_sample_manifest_sha256": self.live_sample_manifest_sha256,
            "status": self.status,
            "requested_by_user_id": str(self.requested_by_user_id),
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReleaseEvidenceArtifact:
    id: UUID
    evidence_kind: str
    artifact_uri: str
    artifact_sha256: str
    run_id: str
    runner_user_id: UUID
    runner_actor: str
    metrics: JsonObject
    sample_manifest_sha256: str
    created_at: datetime

    def to_json(self) -> JsonObject:
        return {
            "id": str(self.id),
            "evidence_kind": self.evidence_kind,
            "artifact_uri": self.artifact_uri,
            "artifact_sha256": self.artifact_sha256,
            "run_id": self.run_id,
            "runner_user_id": str(self.runner_user_id),
            "runner_actor": self.runner_actor,
            "metrics": redact_json(self.metrics),
            "sample_manifest_sha256": self.sample_manifest_sha256,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReleaseQualificationDecision:
    sequence: int
    id: UUID
    from_status: str
    to_status: str
    decision_role: str
    actor_id: str
    decided_by_user_id: UUID | None
    reason: str
    evidence_sha256: str
    evidence_ids: tuple[UUID, ...]
    created_at: datetime

    def to_json(self) -> JsonObject:
        return {
            "sequence": self.sequence,
            "id": str(self.id),
            "from_status": self.from_status,
            "to_status": self.to_status,
            "decision_role": self.decision_role,
            "actor_id": self.actor_id,
            "decided_by_user_id": (
                str(self.decided_by_user_id) if self.decided_by_user_id is not None else None
            ),
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
            "evidence_ids": [str(evidence_id) for evidence_id in self.evidence_ids],
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReleaseQualificationDrilldown:
    qualification: ReleaseQualificationBinding
    evidence: tuple[ReleaseEvidenceArtifact, ...]
    decisions: tuple[ReleaseQualificationDecision, ...]

    def effective_lifecycle_status(self, *, now: datetime) -> str:
        """Derive current lifecycle state from append-only decisions and expiry."""

        latest_status = (
            self.decisions[-1].to_status if self.decisions else self.qualification.status
        )
        if latest_status == "qualified" and now >= self.qualification.expires_at:
            return "expired"
        return latest_status

    def to_json(self, *, now: datetime | None = None) -> JsonObject:
        observed_at = now or datetime.now().astimezone()
        effective_status = self.effective_lifecycle_status(now=observed_at)
        return {
            "qualification": self.qualification.to_json(),
            "evidence": [item.to_json() for item in self.evidence],
            "decisions": [item.to_json() for item in self.decisions],
            "effective_state": {
                "lifecycle_status": effective_status,
                "external_write_authorized": False,
                "reason_code": "AUDIT_READ_MODEL_NEVER_AUTHORIZES_EXECUTION",
                "observed_at": observed_at.isoformat(),
            },
            "limitations": [
                "read_only_drilldown",
                "no_provider_write_authority",
                "no_outbox_publish_authority",
                "credential_and_token_bodies_redacted",
            ],
        }


@dataclass(frozen=True, slots=True)
class IntentEvidenceTrace:
    action_intent: JsonObject
    payload_versions: tuple[JsonObject, ...]
    policy_decisions: tuple[JsonObject, ...]
    approval_requests: tuple[JsonObject, ...]
    autopilot_authorizations: tuple[JsonObject, ...]
    review_items: tuple[JsonObject, ...]
    cap_reservations: tuple[JsonObject, ...]
    outbox_events: tuple[JsonObject, ...]
    side_effect_attempts: tuple[JsonObject, ...]
    provider_receipts: tuple[JsonObject, ...]
    audit_events: tuple[JsonObject, ...]
    kill_switch_events: tuple[JsonObject, ...]

    def to_json(self) -> JsonObject:
        kill_switch_state = [redact_json(row) for row in self.kill_switch_events]
        return {
            "action_intent": redact_json(self.action_intent),
            "payload_versions": [redact_json(row) for row in self.payload_versions],
            "policy_decisions": [redact_json(row) for row in self.policy_decisions],
            "approval_requests": [redact_json(row) for row in self.approval_requests],
            "autopilot_authorizations": [redact_json(row) for row in self.autopilot_authorizations],
            "review_items": [redact_json(row) for row in self.review_items],
            "cap_reservations": [redact_json(row) for row in self.cap_reservations],
            "outbox_events": [redact_json(row) for row in self.outbox_events],
            "side_effect_attempts": [redact_json(row) for row in self.side_effect_attempts],
            "provider_receipts": [redact_json(row) for row in self.provider_receipts],
            "audit_events": [redact_json(row) for row in self.audit_events],
            "kill_switch_events": kill_switch_state,
            "kill_switch_state_at_decision": kill_switch_state,
            "limitations": [
                "read_only_trace",
                "no_provider_write_authority",
                "no_outbox_publish_authority",
                "credential_and_token_bodies_redacted",
                "kill_switch_state_anchored_to_execution_decision_time",
            ],
        }


__all__ = [
    "IntentEvidenceTrace",
    "JsonObject",
    "JsonValue",
    "ReleaseEvidenceArtifact",
    "ReleaseQualificationBinding",
    "ReleaseQualificationDecision",
    "ReleaseQualificationDrilldown",
    "isoformat_or_none",
    "redact_json",
]
