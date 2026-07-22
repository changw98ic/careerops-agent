from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import MappingProxyType
from uuid import UUID

import pytest

from careerops.application.gmail_send import GMAIL_SEND_CHANNEL
from careerops.application.goal_run_composition import (
    GOAL_RUN_COMPOSITION_VERSION,
    GOAL_RUN_REVIEW_KIND,
    GoalRunApplicationComposer,
    canonical_json_hash,
    parse_goal_run_composition_context,
    rank_goal_run_applications,
)

CANDIDATE_ID = "00000000-0000-0000-0000-00000000c001"
RESUME_SHA = "a" * 64
COVER_SHA = "b" * 64
EVIDENCE_SHA = "c" * 64


def context() -> dict[str, object]:
    return {
        "composition": {
            "version": GOAL_RUN_COMPOSITION_VERSION,
            "candidate_id": CANDIDATE_ID,
            "sender_email": "candidate@example.com",
            "include_keywords": ["Python", "Agents", "distributed systems"],
            "exclude_keywords": ["intern", "unpaid"],
            "min_score": 0.67,
            "selected_application_id": "app-greenhouse-1",
            "applications": [
                {
                    "application_id": "app-ashby-2",
                    "channel": GMAIL_SEND_CHANNEL,
                    "job": {
                        "source_id": "crawler-config-1",
                        "discovered_job_id": "ashby-2",
                        "company_name": "Later AI",
                        "title": "Python Engineer",
                        "canonical_url": "https://jobs.example.com/later",
                        "description": "Python platform role.",
                        "keywords": ["python"],
                        "evidence_sha256": EVIDENCE_SHA,
                    },
                    "recipient_email": "recruiting-later@example.com",
                    "subject": "Application for Python Engineer",
                    "text_body": "Hello, I am interested in the Python Engineer role.",
                    "attachment_refs": [attachment("resume:lateral:v1", "resume.pdf", RESUME_SHA)],
                },
                {
                    "application_id": "app-greenhouse-1",
                    "channel": GMAIL_SEND_CHANNEL,
                    "job": {
                        "source_id": "crawler-config-1",
                        "discovered_job_id": "greenhouse-1",
                        "company_name": "Example AI",
                        "title": "Senior Agent Platform Engineer",
                        "canonical_url": "https://boards.greenhouse.io/example/jobs/123",
                        "description": (
                            "Build Python agents and distributed systems for production workflows."
                        ),
                        "keywords": ["agents", "python", "distributed systems"],
                        "evidence_sha256": EVIDENCE_SHA,
                    },
                    "recipient_email": "recruiting@example.com",
                    "subject": "Application for Senior Agent Platform Engineer",
                    "text_body": (
                        "Hello Example AI team,\n\n"
                        "I reviewed the Senior Agent Platform Engineer role and attached "
                        "my approved resume and cover letter.\n"
                    ),
                    "attachment_refs": [
                        attachment("resume:approved:v1", "resume.pdf", RESUME_SHA),
                        attachment("cover-letter:approved:v1", "cover-letter.pdf", COVER_SHA),
                    ],
                },
            ],
        }
    }


def attachment(object_key: str, filename: str, sha256: str) -> dict[str, object]:
    return {
        "object_key": object_key,
        "filename": filename,
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": sha256,
    }


def test_composer_builds_exact_gmail_payload_and_chinese_review_bundle() -> None:
    composed = GoalRunApplicationComposer().compose(context())

    assert composed.selected_application.application_id == "app-greenhouse-1"
    assert composed.gmail_payload.sender == "candidate@example.com"
    assert composed.gmail_payload.recipient == "recruiting@example.com"
    assert composed.gmail_payload.subject == "Application for Senior Agent Platform Engineer"
    assert composed.gmail_payload.attachment_refs[0].object_key == "resume:approved:v1"
    assert composed.selected_match.score == 1.0
    assert composed.selected_match.reason_codes == (
        "MEETS_MIN_SCORE",
        "ALL_INCLUDE_KEYWORDS_MATCHED",
    )
    assert [match.application_id for match in composed.ranked_matches] == [
        "app-greenhouse-1",
        "app-ashby-2",
    ]
    assert composed.review_payload["version"] == GOAL_RUN_REVIEW_KIND
    assert "请审核" in str(composed.review_payload["summary_zh"])
    assert composed.review_payload["safety"] == {
        "contains_credentials": False,
        "external_io_performed": False,
        "human_review_required": True,
        "model_output_is_not_execution_authority": True,
    }
    assert composed.review_payload["email"]["sender"] == "ca***@example.com"  # type: ignore[index]
    assert composed.review_payload["email"]["recipient"] == "recruiting@example.com"  # type: ignore[index]
    assert "object_key" not in str(composed.review_payload["attachments"])
    assert (
        composed.review_payload["exact_payload"]["payload_hash"]  # type: ignore[index]
        == composed.gmail_payload.payload_hash
    )
    assert composed.identity.review_snapshot_sha256 == canonical_json_hash(composed.review_payload)
    assert composed.identity.match_snapshot_sha256 == canonical_json_hash(composed.match_snapshot)
    assert composed.identity.reservation_key
    assert composed.identity.reconciliation_key
    assert isinstance(composed.review_payload, MappingProxyType)

    with pytest.raises(TypeError):
        composed.review_payload["version"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        composed.review_payload["safety"]["external_io_performed"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        composed.selected_match.score = 0.0  # type: ignore[misc]


def test_parse_rejects_missing_extra_or_unsupported_configuration() -> None:
    with pytest.raises(ValueError, match="requires composition"):
        parse_goal_run_composition_context({})

    unsafe = context()
    unsafe["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported keys"):
        parse_goal_run_composition_context(unsafe)

    unsupported_channel = context()
    applications = unsupported_channel["composition"]["applications"]  # type: ignore[index]
    applications[1]["channel"] = "browser:form"  # type: ignore[index]
    with pytest.raises(ValueError, match="unsupported application channel"):
        parse_goal_run_composition_context(unsupported_channel)

    extra_nested = context()
    extra_nested["composition"]["applications"][1]["credential"] = "secret"  # type: ignore[index]
    with pytest.raises(ValueError, match="unsupported keys"):
        parse_goal_run_composition_context(extra_nested)


def test_selected_application_must_be_unique_and_meet_matching_policy() -> None:
    missing = context()
    missing["composition"]["selected_application_id"] = "missing"  # type: ignore[index]
    with pytest.raises(ValueError, match="exactly one"):
        GoalRunApplicationComposer().compose(missing)

    below_min = context()
    below_min["composition"]["selected_application_id"] = "app-ashby-2"  # type: ignore[index]
    with pytest.raises(ValueError, match="min_score"):
        GoalRunApplicationComposer().compose(below_min)

    excluded = context()
    selected_job = excluded["composition"]["applications"][1]["job"]  # type: ignore[index]
    selected_job["description"] = "Python agents role, unpaid internship."  # type: ignore[index]
    with pytest.raises(ValueError, match="exclude_keywords"):
        GoalRunApplicationComposer().compose(excluded)


def test_ranking_is_deterministic_and_context_is_normalized() -> None:
    parsed = parse_goal_run_composition_context(context())
    ranked = rank_goal_run_applications(parsed)
    reparsed = parse_goal_run_composition_context(context())
    reranked = rank_goal_run_applications(reparsed)

    assert parsed.candidate_id == UUID(CANDIDATE_ID)
    assert parsed.include_keywords == ("agents", "distributed systems", "python")
    assert parsed.exclude_keywords == ("intern", "unpaid")
    assert [item.canonical() for item in ranked] == [item.canonical() for item in reranked]
    assert ranked[0].canonical() == {
        "application_id": "app-greenhouse-1",
        "score": 1.0,
        "matched_keywords": ("agents", "distributed systems", "python"),
        "excluded_keywords": (),
        "reason_codes": ("MEETS_MIN_SCORE", "ALL_INCLUDE_KEYWORDS_MATCHED"),
    }


def test_deterministic_identity_changes_only_when_exact_payload_changes() -> None:
    first = GoalRunApplicationComposer().compose(context())
    second = GoalRunApplicationComposer().compose(context())
    changed_context = context()
    changed_context["composition"]["applications"][1]["text_body"] = "Changed reviewed body."  # type: ignore[index]
    changed = GoalRunApplicationComposer().compose(changed_context)

    assert first.identity == second.identity
    assert first.gmail_payload.payload_hash == second.gmail_payload.payload_hash
    assert first.identity.review_item_id != changed.identity.review_item_id
    assert first.identity.action_intent_id != changed.identity.action_intent_id
    assert first.identity.reservation_key != changed.identity.reservation_key
