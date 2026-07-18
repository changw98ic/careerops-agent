from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import JsonValue


class AuditActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    POLICY = "policy"
    WORKER = "worker"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    event_type: str
    actor_type: AuditActorType
    resource_type: str
    resource_id: UUID
    trace_id: str
    actor_id: str | None = None
    event_data: dict[str, JsonValue] = field(default_factory=lambda: {})
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("audit occurred_at must be timezone-aware")
        for value, label in (
            (self.event_type, "event_type"),
            (self.resource_type, "resource_type"),
            (self.trace_id, "trace_id"),
        ):
            if not value.strip():
                raise ValueError(f"audit {label} must not be blank")


@dataclass(frozen=True, slots=True)
class AppendedAuditEvent:
    sequence: int
    event_id: UUID
    previous_hash: str | None
    event_hash: str
