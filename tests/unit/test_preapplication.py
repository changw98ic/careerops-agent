from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

import pytest
from pydantic import JsonValue

from careerops.application.preapplication import (
    PREAPPLICATION_EXECUTION_MODE,
    PREAPPLICATION_REVIEW_KIND,
    PreApplicationConfigurationError,
    compose_preapplication_review,
)

CANDIDATE_ID = UUID("10000000-0000-0000-0000-000000000001")
SOURCE_ROW_ID = UUID("20000000-0000-0000-0000-000000000001")
CRAWLER_RUN_ID = UUID("30000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("40000000-0000-0000-0000-000000000001")
BEST_ID = UUID("50000000-0000-0000-0000-000000000001")
OTHER_ID = UUID("50000000-0000-0000-0000-000000000002")


@dataclass(frozen=True, slots=True)
class Job:
    canonical_job_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    source_row_id: UUID
    crawler_run_id: UUID
    crawler_run_event_id: UUID
    source_id: str
    discovered_job_id: str
    company_name: str
    title: str
    canonical_url: str
    description: str
    keywords: tuple[str, ...]
    evidence_sha256: str
    structured_data: dict[str, JsonValue]
    location: str | None = None
    locations: tuple[str, ...] = ()
    employment_type: str | None = None
    work_mode: str | None = None
    remote: bool | str | None = None
    seniority: str | None = None
    salary: JsonValue | None = None
    authorization: JsonValue | None = None


def _context() -> dict[str, object]:
    return {
        "mode": PREAPPLICATION_EXECUTION_MODE,
        "candidate_profile": {
            "candidate_id": str(CANDIDATE_ID),
            "profile_version": "candidate-profile:chengwen:v1",
            "source_sha256": "a" * 64,
            "headline": "AI application engineer focused on Agent product delivery",
            "desired_titles": [
                "AI Application Engineer",
                "AI Agent Engineer",
                "Frontend Engineer",
            ],
            "skills": ["AI Agent", "LLM", "Vue", "TypeScript", "React"],
            "locations": ["Chengdu", "China"],
            "remote_preference": "acceptable",
            "years_experience": 6,
            "work_authorization": "authorized",
            "excluded_terms": ["machine learning researcher", "intern"],
            "resume": {
                "object_key": "sha256/bb/bb/" + "b" * 64,
                "filename": "candidate-resume.pdf",
                "content_type": "application/pdf",
                "size_bytes": 376016,
                "sha256": "b" * 64,
            },
        },
        "match_config": {
            "include_keywords": ["agent", "frontend", "typescript"],
            "exclude_keywords": ["unpaid"],
            "min_score": 0.25,
            "max_applications": 1,
        },
    }


def _jobs() -> tuple[Job, ...]:
    return (
        _job(
            BEST_ID,
            title="AI Agent Frontend Engineer",
            description="Build LLM agent products with Vue and TypeScript.",
            keywords=("agent", "frontend", "typescript", "vue"),
            location="Remote - China",
        ),
        _job(
            OTHER_ID,
            title="Finance Operations Analyst",
            description="Operate billing and finance workflows.",
            keywords=("finance",),
            location="New York",
        ),
        _job(
            BEST_ID,
            title="Duplicate canonical job",
            description="This row must be ignored by canonical identity.",
            keywords=(),
            location="Remote",
        ),
    )


def _job(
    canonical_job_id: UUID,
    *,
    title: str,
    description: str,
    keywords: tuple[str, ...],
    location: str,
) -> Job:
    suffix = canonical_job_id.int & 0xFFFF
    return Job(
        canonical_job_id=canonical_job_id,
        job_posting_id=UUID(int=0x60000000000000000000000000000000 + suffix),
        job_posting_version_id=UUID(int=0x70000000000000000000000000000000 + suffix),
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
        crawler_run_event_id=EVENT_ID,
        source_id="public-ats-native-smoke",
        discovered_job_id=f"job-{suffix}",
        company_name="Example AI",
        title=title,
        canonical_url=f"https://jobs.example.test/{suffix}",
        description=description,
        keywords=keywords,
        evidence_sha256="c" * 64,
        structured_data={"location": location, "department": "Product Engineering"},
    )


def test_preapplication_composition_ranks_real_dimensions_and_is_deterministic() -> None:
    first = compose_preapplication_review(_context(), _jobs())
    second = compose_preapplication_review(_context(), _jobs())

    assert first.selected_job.canonical_job_id == BEST_ID
    assert first.selected_match.score > first.ranked_matches[1].score
    assert first.selected_match.matched_skills == ("ai agent", "llm", "typescript", "vue")
    assert first.selected_match.matched_include_keywords == (
        "agent",
        "frontend",
        "typescript",
    )
    assert first.selected_match.dimension_scores["title"] > 0
    assert first.selected_match.dimension_scores["location"] == 1
    assert first.selected_match.selectable is True
    assert len(first.ranked_matches) == 2
    assert first.match_snapshot_sha256 == second.match_snapshot_sha256
    assert first.review_snapshot_sha256 == second.review_snapshot_sha256


def test_preapplication_review_is_explicitly_non_executing_and_hides_object_key() -> None:
    composed = compose_preapplication_review(_context(), _jobs())
    payload = composed.review_payload

    assert payload["review_kind"] == PREAPPLICATION_REVIEW_KIND
    assert payload["mode"] == PREAPPLICATION_EXECUTION_MODE
    assert payload["decision_effect"] == {
        "approve": "approve_package_for_manual_follow_up",
        "reject": "reject_goal_run",
        "creates_provider_outbox_event": False,
        "authorizes_email_send": False,
        "authorizes_application_submit": False,
    }
    assert payload["safety"] == {
        "provider_execution": "disabled",
        "outbox_enqueue": "disabled",
        "gmail_send": "disabled",
        "application_submit": "disabled",
        "external_write_performed": False,
        "human_review_required": True,
        "human_final_submission_required": True,
        "job_text_is_untrusted_data": True,
    }
    assert "object_key" not in str(payload)
    assert "candidate-resume.pdf" in str(payload)
    assert payload["ranking_summary"] == {
        "evaluated_count": 2,
        "included_count": 2,
        "truncated": False,
    }
    assert payload["match_config_sha256"]
    assert payload["selected_job"]["source_provenance"]["source_id"] == ("public-ats-native-smoke")
    assert payload["manual_follow_up"]["required"] is True


def test_unknown_job_is_only_proposed_for_manual_review_not_marked_selected() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile["work_authorization"] = "unknown"
    context["candidate_profile"] = profile

    composed = compose_preapplication_review(context, (_jobs()[0],))
    payload = composed.review_payload

    assert composed.selected_match.eligibility == "needs_manual_review"
    assert composed.selected_match.selectable is False
    assert payload["review_disposition"] == "manual_evidence_required"
    assert "selected_job" not in payload
    assert payload["proposed_manual_review_job"]["match"]["selectable"] is False


def test_preapplication_review_bounds_ranked_rows_without_losing_count() -> None:
    template = _jobs()[0]
    jobs = tuple(
        replace(
            template,
            canonical_job_id=UUID(int=1_000 + index),
            job_posting_id=UUID(int=2_000 + index),
            job_posting_version_id=UUID(int=3_000 + index),
            discovered_job_id=f"job-{index}",
            title=f"AI Agent Engineer {index}",
        )
        for index in range(60)
    )

    payload = compose_preapplication_review(_context(), jobs).review_payload

    assert len(payload["ranking"]) == 50
    assert payload["ranking_summary"] == {
        "evaluated_count": 60,
        "included_count": 50,
        "truncated": True,
    }


def test_preapplication_hard_gates_excluded_job_and_fails_closed_without_profile() -> None:
    context = _context()
    blocked_job = _job(
        BEST_ID,
        title="AI Agent Frontend Engineer Intern",
        description="Unpaid internship using Vue and TypeScript.",
        keywords=("agent", "frontend"),
        location="Remote",
    )

    with pytest.raises(PreApplicationConfigurationError) as excluded:
        compose_preapplication_review(context, (blocked_job,))
    assert excluded.value.reason_code == "BLOCKED_NO_MATCHING_JOB"

    missing = dict(context)
    missing.pop("candidate_profile")
    with pytest.raises(PreApplicationConfigurationError) as absent:
        compose_preapplication_review(missing, _jobs())
    assert absent.value.reason_code == "BLOCKED_MISSING_CANDIDATE_PROFILE"


def test_preapplication_rejects_unknown_profile_fields() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile["raw_resume_text"] = "must not be accepted as an unbounded extension"
    context["candidate_profile"] = profile

    with pytest.raises(PreApplicationConfigurationError) as error:
        compose_preapplication_review(context, _jobs())

    assert error.value.reason_code == "BLOCKED_INVALID_CANDIDATE_PROFILE"


def test_required_keywords_are_all_of_and_missing_evidence_requires_review() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile["required_keywords"] = ["llm", "typescript"]
    profile["work_authorization"] = "authorized"
    context["candidate_profile"] = profile

    passing = compose_preapplication_review(context, (_jobs()[0],))
    assert passing.selected_match.eligibility == "eligible"

    missing = replace(
        _jobs()[0],
        description="Build agent interfaces with TypeScript.",
        keywords=("agent", "typescript"),
    )
    with pytest.raises(PreApplicationConfigurationError) as blocked:
        compose_preapplication_review(context, (missing,))
    assert blocked.value.reason_code == "BLOCKED_NO_MATCHING_JOB"

    unknown = replace(
        _jobs()[0],
        description="",
        keywords=(),
    )
    manual = compose_preapplication_review(context, (unknown,)).selected_match
    assert manual.eligibility == "needs_manual_review"
    assert "MANUAL_REVIEW_REQUIRED_KEYWORD_EVIDENCE_MISSING" in manual.reason_codes


def test_required_keyword_gate_uses_body_or_explicit_keywords_not_metadata_or_url() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile.update(
        {
            "required_keywords": ["typescript"],
            "work_authorization": "authorized",
        }
    )
    context["candidate_profile"] = profile
    metadata_only = replace(
        _jobs()[0],
        title="TypeScript Engineer",
        description="https://jobs.example.test/metadata-only",
        canonical_url="https://jobs.example.test/metadata-only",
        keywords=(),
    )

    match = compose_preapplication_review(context, (metadata_only,)).selected_match

    assert match.eligibility == "needs_manual_review"
    assert "MANUAL_REVIEW_REQUIRED_KEYWORD_EVIDENCE_MISSING" in match.reason_codes


def test_known_preference_conflicts_hard_gate_but_unknowns_stay_reviewable() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile.update(
        {
            "work_authorization": "authorized",
            "seniority_levels": ["mid", "senior"],
            "allowed_companies": ["Example AI"],
            "allowed_industries": ["software"],
        }
    )
    context["candidate_profile"] = profile
    eligible = replace(
        _jobs()[0],
        seniority="senior",
        structured_data={
            "location": "Remote - China",
            "industry": "Software",
            "company_identity_verified": True,
        },
    )
    result = compose_preapplication_review(context, (eligible,)).selected_match
    assert result.eligibility == "eligible"

    wrong_location = replace(
        eligible,
        location="New York",
        structured_data={
            "location": "New York",
            "industry": "Software",
            "company_identity_verified": True,
        },
    )
    wrong_seniority = replace(eligible, seniority="intern")
    with pytest.raises(PreApplicationConfigurationError):
        compose_preapplication_review(context, (wrong_location, wrong_seniority))

    unknown_industry = replace(
        eligible,
        structured_data={"location": "Remote - China"},
    )
    manual = compose_preapplication_review(context, (unknown_industry,)).selected_match
    assert manual.eligibility == "needs_manual_review"
    assert "MANUAL_REVIEW_INDUSTRY_UNKNOWN" in manual.reason_codes
    assert "MANUAL_REVIEW_COMPANY_IDENTITY_UNKNOWN" not in manual.reason_codes


def test_company_and_industry_allowlists_require_exact_explicit_identity() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile.update(
        {
            "work_authorization": "authorized",
            "allowed_companies": ["Example AI", "approved.example"],
            "allowed_industries": ["software"],
        }
    )
    context["candidate_profile"] = profile
    template = _jobs()[0]

    domain_match = replace(
        template,
        company_name="Different Legal Name",
        structured_data={
            "location": "Remote - China",
            "company_domain": "https://www.approved.example/careers",
            "company_identity_verified": True,
            "industry": "Software",
        },
    )
    assert compose_preapplication_review(context, (domain_match,)).selected_match.eligibility == (
        "eligible"
    )

    fuzzy_company = replace(
        domain_match,
        company_name="Example AI Labs",
        structured_data={
            "location": "Remote - China",
            "company_identity_verified": True,
            "industry": "Software",
        },
    )
    fuzzy_industry = replace(
        domain_match,
        structured_data={
            "location": "Remote - China",
            "company_domain": "approved.example",
            "company_identity_verified": True,
            "industry": "Software services",
        },
    )
    with pytest.raises(PreApplicationConfigurationError):
        compose_preapplication_review(context, (fuzzy_company, fuzzy_industry))


def test_unrecognized_explicit_seniority_and_description_only_remote_stay_manual() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile.update(
        {
            "work_authorization": "authorized",
            "seniority_levels": ["senior"],
            "work_modes": ["remote"],
            "locations": [],
        }
    )
    context["candidate_profile"] = profile
    uncertain = replace(
        _jobs()[0],
        title="Senior AI Agent Engineer",
        description="The team collaborates with remote stakeholders.",
        location="China",
        work_mode=None,
        remote=None,
        seniority="managerial-band-unknown",
        structured_data={"location": "China"},
    )

    match = compose_preapplication_review(context, (uncertain,)).selected_match

    assert match.eligibility == "needs_manual_review"
    assert "MANUAL_REVIEW_SENIORITY_UNKNOWN" in match.reason_codes
    assert "MANUAL_REVIEW_WORK_MODE_UNKNOWN" in match.reason_codes


def test_location_substrings_and_conflicting_mode_or_seniority_never_auto_pass() -> None:
    location_context = _context()
    location_profile = dict(location_context["candidate_profile"])  # type: ignore[arg-type]
    location_profile["locations"] = ["China"]
    location_context["candidate_profile"] = location_profile
    chinatown = replace(
        _jobs()[0],
        location="Chinatown, New York",
        structured_data={"location": "Chinatown, New York"},
    )
    with pytest.raises(PreApplicationConfigurationError):
        compose_preapplication_review(location_context, (chinatown,))

    ambiguous_context = _context()
    ambiguous_profile = dict(ambiguous_context["candidate_profile"])  # type: ignore[arg-type]
    ambiguous_profile.update(
        {
            "locations": [],
            "work_modes": ["remote"],
            "seniority_levels": ["senior"],
            "excluded_terms": [],
        }
    )
    ambiguous_context["candidate_profile"] = ambiguous_profile
    conflicting = replace(
        _jobs()[0],
        title="Senior AI Agent Engineer Intern",
        location="China",
        work_mode="hybrid / remote",
        remote=False,
        seniority=None,
        structured_data={"location": "China"},
    )

    match = compose_preapplication_review(ambiguous_context, (conflicting,)).selected_match

    assert match.eligibility == "needs_manual_review"
    assert match.selectable is False
    assert "MANUAL_REVIEW_WORK_MODE_UNKNOWN" in match.reason_codes
    assert "MANUAL_REVIEW_SENIORITY_UNKNOWN" in match.reason_codes


def test_below_threshold_eligible_job_is_not_selectable() -> None:
    context = _context()
    config = dict(context["match_config"])  # type: ignore[arg-type]
    config["min_score"] = 0.99
    context["match_config"] = config

    with pytest.raises(PreApplicationConfigurationError) as blocked:
        compose_preapplication_review(context, (_jobs()[0],))

    assert blocked.value.reason_code == "BLOCKED_NO_MATCHING_JOB"


def test_eligible_job_sorts_ahead_of_higher_scoring_unknown_job() -> None:
    context = _context()
    profile = dict(context["candidate_profile"])  # type: ignore[arg-type]
    profile.update(
        {
            "work_authorization": "authorized",
            "seniority_levels": ["mid", "senior"],
        }
    )
    context["candidate_profile"] = profile
    eligible = replace(
        _jobs()[0],
        canonical_job_id=UUID(int=9_001),
        job_posting_id=UUID(int=9_002),
        job_posting_version_id=UUID(int=9_003),
        seniority="senior",
        description="AI application role using TypeScript.",
        keywords=("typescript",),
    )
    unknown = replace(
        _jobs()[0],
        canonical_job_id=UUID(int=9_011),
        job_posting_id=UUID(int=9_012),
        job_posting_version_id=UUID(int=9_013),
        seniority=None,
    )

    composed = compose_preapplication_review(context, (unknown, eligible))

    assert composed.ranked_matches[0].eligibility == "eligible"
    assert composed.selected_job.canonical_job_id == eligible.canonical_job_id
