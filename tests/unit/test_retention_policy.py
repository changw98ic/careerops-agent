from datetime import UTC, datetime, timedelta

import pytest

from careerops.application.ports.storage import ContentClassification
from careerops.application.retention_policy import PersistenceProhibited, RetentionPolicy

CAPTURED_AT = datetime(2026, 7, 17, tzinfo=UTC)


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        (ContentClassification.RAW_WEBPAGE, timedelta(days=30)),
        (ContentClassification.RAW_HEADERS, timedelta(days=14)),
        (ContentClassification.RECRUITING_EMAIL, timedelta(days=365)),
        (ContentClassification.QUARANTINED_ATTACHMENT, timedelta(hours=24)),
        (ContentClassification.DECISION_EVIDENCE, timedelta(days=730)),
    ],
)
def test_default_retention_matches_adr(
    classification: ContentClassification,
    expected: timedelta,
) -> None:
    assert (
        RetentionPolicy().deadline(classification=classification, captured_at=CAPTURED_AT)
        == CAPTURED_AT + expected
    )


def test_requested_retention_can_only_shorten_a_default() -> None:
    policy = RetentionPolicy()
    shorter = CAPTURED_AT + timedelta(days=2)
    longer = CAPTURED_AT + timedelta(days=90)

    assert (
        policy.deadline(
            classification=ContentClassification.RAW_WEBPAGE,
            captured_at=CAPTURED_AT,
            requested_until=shorter,
        )
        == shorter
    )
    assert policy.deadline(
        classification=ContentClassification.RAW_WEBPAGE,
        captured_at=CAPTURED_AT,
        requested_until=longer,
    ) == CAPTURED_AT + timedelta(days=30)


def test_model_debug_persistence_is_unavailable_in_m0() -> None:
    with pytest.raises(PersistenceProhibited, match="disabled"):
        RetentionPolicy().deadline(
            classification=ContentClassification.MODEL_DEBUG,
            captured_at=CAPTURED_AT,
        )


def test_accepted_attachment_must_inherit_owner_retention() -> None:
    with pytest.raises(ValueError, match="follow its owner"):
        RetentionPolicy().deadline(
            classification=ContentClassification.ACCEPTED_ATTACHMENT,
            captured_at=CAPTURED_AT,
        )

    owner_deadline = CAPTURED_AT + timedelta(days=7)
    assert (
        RetentionPolicy().deadline(
            classification=ContentClassification.ACCEPTED_ATTACHMENT,
            captured_at=CAPTURED_AT,
            requested_until=owner_deadline,
        )
        == owner_deadline
    )


def test_naive_or_nonpositive_retention_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RetentionPolicy().deadline(
            classification=ContentClassification.RAW_WEBPAGE,
            captured_at=datetime(2026, 7, 17),
        )
    with pytest.raises(ValueError, match="after capture"):
        RetentionPolicy().deadline(
            classification=ContentClassification.RAW_WEBPAGE,
            captured_at=CAPTURED_AT,
            requested_until=CAPTURED_AT,
        )
