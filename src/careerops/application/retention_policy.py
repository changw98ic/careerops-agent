from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from careerops.application.ports.storage import ContentClassification


class PersistenceProhibited(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """M0 retention defaults; callers may shorten but never widen them."""

    raw_webpage: timedelta = timedelta(days=30)
    raw_headers: timedelta = timedelta(days=14)
    recruiting_email: timedelta = timedelta(days=365)
    quarantined_attachment: timedelta = timedelta(hours=24)
    decision_evidence: timedelta = timedelta(days=730)

    def deadline(
        self,
        *,
        classification: ContentClassification,
        captured_at: datetime,
        requested_until: datetime | None = None,
    ) -> datetime:
        self._require_aware(captured_at, "captured_at")
        if requested_until is not None:
            self._require_aware(requested_until, "requested_until")
            if requested_until <= captured_at:
                raise ValueError("requested retention must end after capture")

        if classification is ContentClassification.MODEL_DEBUG:
            raise PersistenceProhibited("model debug capture is disabled before M2 qualification")
        if classification is ContentClassification.ACCEPTED_ATTACHMENT:
            if requested_until is None:
                raise ValueError("accepted attachment retention must follow its owner")
            return requested_until

        duration = {
            ContentClassification.RAW_WEBPAGE: self.raw_webpage,
            ContentClassification.RAW_HEADERS: self.raw_headers,
            ContentClassification.RECRUITING_EMAIL: self.recruiting_email,
            ContentClassification.QUARANTINED_ATTACHMENT: self.quarantined_attachment,
            ContentClassification.DECISION_EVIDENCE: self.decision_evidence,
        }[classification]
        policy_deadline = captured_at + duration
        return min(policy_deadline, requested_until) if requested_until else policy_deadline

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
