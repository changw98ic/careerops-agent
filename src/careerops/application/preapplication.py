from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import JsonValue

from careerops.application.goal_run_composition import canonical_json_hash

PREAPPLICATION_EXECUTION_MODE = "pre_application_only"
PREAPPLICATION_REVIEW_KIND = "goal_run_pre_application_review.v1"
PREAPPLICATION_ARTIFACT_VERSION = "goal_run_pre_application_artifact.v2"

_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9+#.]+|[\u3400-\u9fff]+")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 8_000
_MAX_REVIEW_RANKED_MATCHES = 50


class PreApplicationConfigurationError(ValueError):
    """A safe, stable configuration failure surfaced as a GoalRun blocker."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PreApplicationJob(Protocol):
    @property
    def canonical_job_id(self) -> UUID: ...

    @property
    def job_posting_id(self) -> UUID: ...

    @property
    def job_posting_version_id(self) -> UUID: ...

    @property
    def source_row_id(self) -> UUID: ...

    @property
    def crawler_run_id(self) -> UUID: ...

    @property
    def crawler_run_event_id(self) -> UUID: ...

    @property
    def source_id(self) -> str: ...

    @property
    def discovered_job_id(self) -> str: ...

    @property
    def company_name(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def canonical_url(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def keywords(self) -> tuple[str, ...]: ...

    @property
    def evidence_sha256(self) -> str: ...

    @property
    def structured_data(self) -> Mapping[str, JsonValue]: ...

    @property
    def location(self) -> str | None: ...

    @property
    def locations(self) -> tuple[str, ...]: ...

    @property
    def employment_type(self) -> str | None: ...

    @property
    def work_mode(self) -> str | None: ...

    @property
    def remote(self) -> bool | str | None: ...

    @property
    def seniority(self) -> str | None: ...

    @property
    def salary(self) -> JsonValue | None: ...

    @property
    def authorization(self) -> JsonValue | None: ...


@dataclass(frozen=True, slots=True)
class CandidateMaterialSnapshot:
    object_key: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str

    def review_safe(self) -> dict[str, JsonValue]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CandidateProfileSnapshot:
    candidate_id: UUID
    profile_version: str
    source_sha256: str
    headline: str
    desired_titles: tuple[str, ...]
    skills: tuple[str, ...]
    required_skills: tuple[str, ...]
    required_keywords: tuple[str, ...]
    locations: tuple[str, ...]
    excluded_locations: tuple[str, ...]
    allowed_companies: tuple[str, ...]
    allowed_industries: tuple[str, ...]
    remote_preference: str
    years_experience: float | None
    excluded_terms: tuple[str, ...]
    work_modes: tuple[str, ...]
    employment_types: tuple[str, ...]
    seniority_levels: tuple[str, ...]
    work_authorization: str
    requires_sponsorship: bool | None
    sponsorship_allowed: bool | None
    minimum_salary: int | None
    salary_currency: str | None
    resume: CandidateMaterialSnapshot | None

    def canonical(self) -> dict[str, JsonValue]:
        return {
            "candidate_id": str(self.candidate_id),
            "profile_version": self.profile_version,
            "source_sha256": self.source_sha256,
            "headline": self.headline,
            "desired_titles": list(self.desired_titles),
            "skills": list(self.skills),
            "required_skills": list(self.required_skills),
            "required_keywords": list(self.required_keywords),
            "locations": list(self.locations),
            "excluded_locations": list(self.excluded_locations),
            "allowed_companies": list(self.allowed_companies),
            "allowed_industries": list(self.allowed_industries),
            "remote_preference": self.remote_preference,
            "years_experience": self.years_experience,
            "excluded_terms": list(self.excluded_terms),
            "work_modes": list(self.work_modes),
            "employment_types": list(self.employment_types),
            "seniority_levels": list(self.seniority_levels),
            "work_authorization": self.work_authorization,
            "requires_sponsorship": self.requires_sponsorship,
            "sponsorship_allowed": self.sponsorship_allowed,
            "minimum_salary": self.minimum_salary,
            "salary_currency": self.salary_currency,
            "resume": self.resume.review_safe() if self.resume is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PreApplicationMatchConfig:
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    min_score: float
    max_applications: int

    def canonical(self) -> dict[str, JsonValue]:
        return {
            "include_keywords": list(self.include_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "min_score": self.min_score,
            "max_applications": self.max_applications,
        }


@dataclass(frozen=True, slots=True)
class PreApplicationRankedMatch:
    canonical_job_id: UUID
    job_posting_id: UUID
    job_posting_version_id: UUID
    score: float
    selectable: bool
    eligibility: Literal["eligible", "needs_manual_review", "ineligible"]
    matched_skills: tuple[str, ...]
    matched_include_keywords: tuple[str, ...]
    excluded_terms: tuple[str, ...]
    dimension_scores: Mapping[str, float]
    reason_codes: tuple[str, ...]

    def canonical(self) -> dict[str, JsonValue]:
        return {
            "canonical_job_id": str(self.canonical_job_id),
            "job_posting_id": str(self.job_posting_id),
            "job_posting_version_id": str(self.job_posting_version_id),
            "score": self.score,
            "selectable": self.selectable,
            "eligibility": self.eligibility,
            "matched_skills": list(self.matched_skills),
            "matched_include_keywords": list(self.matched_include_keywords),
            "excluded_terms": list(self.excluded_terms),
            "dimension_scores": dict(self.dimension_scores),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class PreApplicationComposition:
    profile: CandidateProfileSnapshot
    selected_job: PreApplicationJob
    selected_match: PreApplicationRankedMatch
    ranked_matches: tuple[PreApplicationRankedMatch, ...]
    profile_snapshot_sha256: str
    match_snapshot_sha256: str
    review_snapshot_sha256: str
    review_payload: Mapping[str, JsonValue]


def compose_preapplication_review(
    context: Mapping[str, object],
    candidates: Sequence[PreApplicationJob],
) -> PreApplicationComposition:
    profile = parse_candidate_profile(context.get("candidate_profile"))
    config = parse_match_config(context.get("match_config"))
    unique_jobs = _deduplicated_jobs(candidates)
    if not unique_jobs:
        raise PreApplicationConfigurationError("BLOCKED_NO_DISCOVERED_JOBS")

    ranked = tuple(
        sorted(
            (_score_job(profile, config, job) for job in unique_jobs),
            key=lambda item: (
                {"eligible": 0, "needs_manual_review": 1, "ineligible": 2}[item.eligibility],
                -item.score,
                str(item.canonical_job_id),
                str(item.job_posting_version_id),
            ),
        )
    )
    selected = next((item for item in ranked if item.selectable), None)
    if selected is None:
        selected = next(
            (
                item
                for item in ranked
                if item.eligibility == "needs_manual_review"
                and item.score >= config.min_score
            ),
            None,
        )
    if selected is None:
        raise PreApplicationConfigurationError("BLOCKED_NO_MATCHING_JOB")
    selected_job = next(
        job for job in unique_jobs if job.canonical_job_id == selected.canonical_job_id
    )

    profile_hash = canonical_json_hash(profile.canonical())
    match_config_hash = canonical_json_hash(config.canonical())
    match_snapshot: dict[str, JsonValue] = {
        "version": PREAPPLICATION_ARTIFACT_VERSION,
        "candidate_id": str(profile.candidate_id),
        "profile_snapshot_sha256": profile_hash,
        "match_config_sha256": match_config_hash,
        "match_config": config.canonical(),
        "evaluated_job_count": len(ranked),
        (
            "selected_canonical_job_id"
            if selected.selectable
            else "proposed_manual_review_canonical_job_id"
        ): str(selected.canonical_job_id),
        "ranked_matches": [item.canonical() for item in ranked],
    }
    match_hash = canonical_json_hash(match_snapshot)
    payload = _review_payload(
        profile=profile,
        profile_hash=profile_hash,
        selected_job=selected_job,
        selected=selected,
        ranked=ranked,
        jobs=unique_jobs,
        match_hash=match_hash,
        match_config_hash=match_config_hash,
    )
    review_hash = canonical_json_hash(payload)
    return PreApplicationComposition(
        profile=profile,
        selected_job=selected_job,
        selected_match=selected,
        ranked_matches=ranked,
        profile_snapshot_sha256=profile_hash,
        match_snapshot_sha256=match_hash,
        review_snapshot_sha256=review_hash,
        review_payload=MappingProxyType(payload),
    )


def parse_candidate_profile(value: object) -> CandidateProfileSnapshot:
    mapping = _mapping(value, "candidate_profile", "BLOCKED_MISSING_CANDIDATE_PROFILE")
    allowed = {
        "candidate_id",
        "profile_version",
        "source_sha256",
        "headline",
        "desired_titles",
        "skills",
        "required_skills",
        "required_keywords",
        "locations",
        "excluded_locations",
        "allowed_companies",
        "allowed_industries",
        "remote_preference",
        "years_experience",
        "excluded_terms",
        "work_modes",
        "employment_types",
        "seniority_levels",
        "work_authorization",
        "requires_sponsorship",
        "sponsorship_allowed",
        "minimum_salary",
        "salary_currency",
        "resume",
    }
    try:
        _reject_extra(mapping, allowed)
        candidate_id = UUID(_required_text(mapping, "candidate_id", max_length=64))
        source_sha256 = _required_text(mapping, "source_sha256", max_length=64)
        if _SHA256.fullmatch(source_sha256) is None:
            raise ValueError("source_sha256")
        remote_preference = _required_text(mapping, "remote_preference", max_length=32)
        if remote_preference not in {
            "required",
            "preferred",
            "acceptable",
            "onsite",
            "unspecified",
        }:
            raise ValueError("remote_preference")
        years = mapping.get("years_experience")
        if years is not None and (
            not isinstance(years, int | float) or isinstance(years, bool) or not 0 <= years <= 80
        ):
            raise ValueError("years_experience")
        resume_value = mapping.get("resume")
        resume = _parse_material(resume_value) if resume_value is not None else None
        work_authorization = _optional_enum_text(
            mapping.get("work_authorization", "unknown"),
            "work_authorization",
            {"unknown", "authorized", "restricted", "requires_sponsorship"},
        )
        requires_sponsorship = _optional_bool(
            mapping.get("requires_sponsorship"), "requires_sponsorship"
        )
        sponsorship_allowed = _optional_bool(
            mapping.get("sponsorship_allowed"), "sponsorship_allowed"
        )
        minimum_salary = mapping.get("minimum_salary")
        salary_currency = mapping.get("salary_currency")
        if minimum_salary is not None and (
            not isinstance(minimum_salary, int)
            or isinstance(minimum_salary, bool)
            or not 0 <= minimum_salary <= 100_000_000
        ):
            raise ValueError("minimum_salary")
        if (minimum_salary is None) != (salary_currency is None):
            raise ValueError("salary pair")
        if salary_currency is not None and (
            not isinstance(salary_currency, str)
            or re.fullmatch(r"[A-Z]{3}", salary_currency) is None
        ):
            raise ValueError("salary_currency")
        return CandidateProfileSnapshot(
            candidate_id=candidate_id,
            profile_version=_required_text(mapping, "profile_version", max_length=160),
            source_sha256=source_sha256,
            headline=_required_text(mapping, "headline", max_length=500),
            desired_titles=_string_tuple(mapping.get("desired_titles"), "desired_titles", 1, 30),
            skills=_string_tuple(mapping.get("skills"), "skills", 1, 100),
            required_skills=_string_tuple(
                mapping.get("required_skills", ()), "required_skills", 0, 50
            ),
            required_keywords=_string_tuple(
                mapping.get("required_keywords", ()), "required_keywords", 0, 50
            ),
            locations=_string_tuple(mapping.get("locations", ()), "locations", 0, 30),
            excluded_locations=_string_tuple(
                mapping.get("excluded_locations", ()), "excluded_locations", 0, 30
            ),
            allowed_companies=_string_tuple(
                mapping.get("allowed_companies", ()), "allowed_companies", 0, 50
            ),
            allowed_industries=_string_tuple(
                mapping.get("allowed_industries", ()), "allowed_industries", 0, 50
            ),
            remote_preference=remote_preference,
            years_experience=float(years) if years is not None else None,
            excluded_terms=_string_tuple(
                mapping.get("excluded_terms", ()), "excluded_terms", 0, 50
            ),
            work_modes=_enum_tuple(
                mapping.get("work_modes", ()),
                "work_modes",
                {"remote", "hybrid", "onsite"},
                3,
            ),
            employment_types=_enum_tuple(
                mapping.get("employment_types", ()),
                "employment_types",
                {"full_time", "part_time", "contract", "internship", "temporary"},
                5,
            ),
            seniority_levels=_enum_tuple(
                mapping.get("seniority_levels", ()),
                "seniority_levels",
                {
                    "unknown",
                    "intern",
                    "entry",
                    "mid",
                    "senior",
                    "staff",
                    "principal",
                    "executive",
                },
                8,
            ),
            work_authorization=work_authorization,
            requires_sponsorship=requires_sponsorship,
            sponsorship_allowed=sponsorship_allowed,
            minimum_salary=minimum_salary,
            salary_currency=salary_currency,
            resume=resume,
        )
    except (TypeError, ValueError):
        raise PreApplicationConfigurationError("BLOCKED_INVALID_CANDIDATE_PROFILE") from None


def parse_match_config(value: object) -> PreApplicationMatchConfig:
    mapping = _mapping(value, "match_config", "BLOCKED_MISSING_MATCH_CONFIGURATION")
    try:
        _reject_extra(
            mapping,
            {"include_keywords", "exclude_keywords", "min_score", "max_applications"},
        )
        score = mapping.get("min_score", 0.0)
        maximum = mapping.get("max_applications", 1)
        if not isinstance(score, int | float) or isinstance(score, bool) or not 0 <= score <= 1:
            raise ValueError("min_score")
        if maximum != 1:
            raise ValueError("max_applications")
        return PreApplicationMatchConfig(
            include_keywords=_string_tuple(
                mapping.get("include_keywords"), "include_keywords", 1, 50
            ),
            exclude_keywords=_string_tuple(
                mapping.get("exclude_keywords", ()), "exclude_keywords", 0, 50
            ),
            min_score=float(score),
            max_applications=1,
        )
    except (TypeError, ValueError):
        raise PreApplicationConfigurationError("BLOCKED_INVALID_MATCH_CONFIGURATION") from None


def _score_job(
    profile: CandidateProfileSnapshot,
    config: PreApplicationMatchConfig,
    job: PreApplicationJob,
) -> PreApplicationRankedMatch:
    structured = dict(job.structured_data)
    location = _normalize(
        " ".join(
            item
            for item in (
                job.location or "",
                " ".join(job.locations),
                _first_text(
                    structured,
                    "location",
                    "source_job_location",
                    "location_text",
                ),
            )
            if item
        )
    )
    department = _first_text(
        structured,
        "department",
        "source_job_department",
        "team",
    )
    industry_terms = _structured_terms(
        structured,
        "industry",
        "industries",
        "industry_name",
        "industry_names",
    )
    industry = " ".join(industry_terms)
    haystack = _normalize(
        " ".join(
            (
                job.company_name,
                job.title,
                job.description,
                location,
                department,
                industry,
                " ".join(job.keywords),
            )
        )
    )
    title_text = _normalize(job.title)
    title_score = max(
        (_term_coverage(term, title_text) for term in profile.desired_titles),
        default=0,
    )
    matched_skills = tuple(
        sorted(term for term in profile.skills if _term_coverage(term, haystack) >= 0.8)
    )
    skill_score = len(matched_skills) / len(profile.skills)
    missing_required_skills = tuple(
        sorted(term for term in profile.required_skills if _term_coverage(term, haystack) < 0.8)
    )
    keyword_evidence = _required_keyword_evidence(job)
    missing_required_keywords = tuple(
        sorted(
            term
            for term in profile.required_keywords
            if _term_coverage(term, keyword_evidence) < 0.8
        )
    )
    required_keyword_unknown = bool(missing_required_keywords) and not keyword_evidence
    required_keyword_gate = bool(missing_required_keywords) and not required_keyword_unknown
    matched_include = tuple(
        sorted(term for term in config.include_keywords if _term_coverage(term, haystack) >= 0.8)
    )
    include_score = len(matched_include) / len(config.include_keywords)
    normalized_job_work_mode = _normalized_work_mode(
        job.work_mode,
        job.remote,
        haystack,
    )
    location_score, remote_gate, location_gate, location_unknown = _location_fit(
        profile,
        location,
        normalized_job_work_mode,
    )
    excluded_locations = tuple(
        sorted(
            term
            for term in profile.excluded_locations
            if _location_term_matches(term, location)
        )
    )
    company_gate, company_unknown = _company_allowlist_fit(
        profile.allowed_companies,
        job.company_name,
        structured,
    )
    industry_gate, industry_unknown = _industry_allowlist_fit(
        profile.allowed_industries,
        industry_terms,
    )
    seniority_score, seniority_gate, seniority_unknown = _seniority_fit(
        profile,
        job.seniority,
        job.title,
    )
    employment_score, employment_gate, employment_unknown = _employment_fit(
        profile,
        job.employment_type,
    )
    work_mode_score, work_mode_gate, work_mode_unknown = _work_mode_fit(
        profile,
        job.work_mode,
        job.remote,
        location,
    )
    sponsorship_gate = profile.requires_sponsorship is True and profile.sponsorship_allowed is False
    salary_gate, salary_unknown = _salary_fit(profile, job.salary)
    excluded = tuple(
        sorted(
            term
            for term in {*profile.excluded_terms, *config.exclude_keywords}
            if _term_coverage(term, haystack) >= 0.8
        )
    )
    hard_gate = any(
        (
            excluded,
            missing_required_skills,
            required_keyword_gate,
            excluded_locations,
            remote_gate,
            location_gate,
            company_gate,
            industry_gate,
            seniority_gate,
            employment_gate,
            work_mode_gate,
            sponsorship_gate,
            salary_gate,
        )
    )
    manual_review = any(
        (
            required_keyword_unknown,
            location_unknown,
            company_unknown,
            industry_unknown,
            seniority_unknown,
            employment_unknown,
            work_mode_unknown,
            salary_unknown,
            profile.work_authorization == "unknown",
        )
    )
    eligibility: Literal["eligible", "needs_manual_review", "ineligible"] = (
        "ineligible" if hard_gate else "needs_manual_review" if manual_review else "eligible"
    )
    weighted = (
        title_score * 0.20
        + skill_score * 0.25
        + include_score * 0.20
        + location_score * 0.10
        + seniority_score * 0.10
        + employment_score * 0.075
        + work_mode_score * 0.075
    )
    score = round(weighted if eligibility != "ineligible" else 0.0, 6)
    selectable = eligibility == "eligible" and score >= config.min_score
    reasons: list[str] = []
    if excluded:
        reasons.append("HARD_GATE_EXCLUDED_TERM")
    if missing_required_skills:
        reasons.append("HARD_GATE_REQUIRED_SKILL_MISSING")
    if required_keyword_gate:
        reasons.append("HARD_GATE_REQUIRED_KEYWORD_MISSING")
    if excluded_locations:
        reasons.append("HARD_GATE_EXCLUDED_LOCATION")
    if remote_gate:
        reasons.append("HARD_GATE_REMOTE_REQUIRED")
    if location_gate:
        reasons.append("HARD_GATE_LOCATION_NOT_ALLOWED")
    if company_gate:
        reasons.append("HARD_GATE_COMPANY_NOT_ALLOWED")
    if industry_gate:
        reasons.append("HARD_GATE_INDUSTRY_NOT_ALLOWED")
    if seniority_gate:
        reasons.append("HARD_GATE_SENIORITY_NOT_ALLOWED")
    if employment_gate:
        reasons.append("HARD_GATE_EMPLOYMENT_TYPE")
    if work_mode_gate:
        reasons.append("HARD_GATE_WORK_MODE")
    if sponsorship_gate:
        reasons.append("HARD_GATE_SPONSORSHIP_NOT_ALLOWED")
    if salary_gate:
        reasons.append("HARD_GATE_SALARY_BELOW_MINIMUM")
    if employment_unknown:
        reasons.append("MANUAL_REVIEW_EMPLOYMENT_TYPE_UNKNOWN")
    if work_mode_unknown:
        reasons.append("MANUAL_REVIEW_WORK_MODE_UNKNOWN")
    if salary_unknown:
        reasons.append("MANUAL_REVIEW_SALARY_UNKNOWN")
    if required_keyword_unknown:
        reasons.append("MANUAL_REVIEW_REQUIRED_KEYWORD_EVIDENCE_MISSING")
    if location_unknown:
        reasons.append("MANUAL_REVIEW_LOCATION_UNKNOWN")
    if industry_unknown:
        reasons.append("MANUAL_REVIEW_INDUSTRY_UNKNOWN")
    if company_unknown:
        reasons.append("MANUAL_REVIEW_COMPANY_IDENTITY_UNKNOWN")
    if seniority_unknown:
        reasons.append("MANUAL_REVIEW_SENIORITY_UNKNOWN")
    if profile.work_authorization == "unknown":
        reasons.append("MANUAL_REVIEW_WORK_AUTHORIZATION_UNKNOWN")
    if title_score >= 0.5:
        reasons.append("TITLE_ALIGNMENT")
    if matched_skills:
        reasons.append("SKILL_ALIGNMENT")
    if matched_include:
        reasons.append("PREFERENCE_ALIGNMENT")
    reasons.append(
        "MEETS_MIN_SCORE"
        if eligibility != "ineligible" and score >= config.min_score
        else "BELOW_MIN_SCORE"
    )
    return PreApplicationRankedMatch(
        canonical_job_id=job.canonical_job_id,
        job_posting_id=job.job_posting_id,
        job_posting_version_id=job.job_posting_version_id,
        score=score,
        selectable=selectable,
        eligibility=eligibility,
        matched_skills=matched_skills,
        matched_include_keywords=matched_include,
        excluded_terms=excluded,
        dimension_scores=MappingProxyType(
            {
                "title": round(title_score, 6),
                "skills": round(skill_score, 6),
                "preferences": round(include_score, 6),
                "location": round(location_score, 6),
                "seniority": round(seniority_score, 6),
                "employment_type": round(employment_score, 6),
                "work_mode": round(work_mode_score, 6),
            }
        ),
        reason_codes=tuple(reasons),
    )


def _review_payload(
    *,
    profile: CandidateProfileSnapshot,
    profile_hash: str,
    selected_job: PreApplicationJob,
    selected: PreApplicationRankedMatch,
    ranked: Sequence[PreApplicationRankedMatch],
    jobs: Sequence[PreApplicationJob],
    match_hash: str,
    match_config_hash: str,
) -> dict[str, JsonValue]:
    by_id = {job.canonical_job_id: job for job in jobs}
    ranking: list[dict[str, JsonValue]] = []
    for index, item in enumerate(ranked[:_MAX_REVIEW_RANKED_MATCHES], start=1):
        job = by_id[item.canonical_job_id]
        ranking.append(
            {
                "rank": index,
                "company_name": job.company_name,
                "title": job.title,
                "canonical_url": job.canonical_url,
                **item.canonical(),
            }
        )
    reviewed_job = {
        "canonical_job_id": str(selected_job.canonical_job_id),
        "job_posting_id": str(selected_job.job_posting_id),
        "job_posting_version_id": str(selected_job.job_posting_version_id),
        "company_name": selected_job.company_name,
        "title": selected_job.title,
        "canonical_url": selected_job.canonical_url,
        "evidence_sha256": selected_job.evidence_sha256,
        "source_provenance": {
            "source_id": selected_job.source_id,
            "source_row_id": str(selected_job.source_row_id),
            "crawler_run_id": str(selected_job.crawler_run_id),
            "crawler_run_event_id": str(selected_job.crawler_run_event_id),
            "discovered_job_id": selected_job.discovered_job_id,
        },
        "match": selected.canonical(),
    }
    reviewed_job_key = (
        "selected_job" if selected.selectable else "proposed_manual_review_job"
    )
    return cast(
        "dict[str, JsonValue]",
        {
            "version": PREAPPLICATION_ARTIFACT_VERSION,
            "review_kind": PREAPPLICATION_REVIEW_KIND,
            "mode": PREAPPLICATION_EXECUTION_MODE,
            "summary_zh": (
                "请审核候选岗位、匹配证据和简历版本；"  # noqa: RUF001
                "批准只完成本地申请准备，不发送邮件或提交表单。"  # noqa: RUF001
            ),
            "candidate": {
                "candidate_id": str(profile.candidate_id),
                "profile_version": profile.profile_version,
                "headline": profile.headline,
                "profile_snapshot_sha256": profile_hash,
                "work_authorization": profile.work_authorization,
                "requires_sponsorship": profile.requires_sponsorship,
                "sponsorship_allowed": profile.sponsorship_allowed,
                "required_skills": list(profile.required_skills),
                "required_keywords": list(profile.required_keywords),
                "allowed_locations": list(profile.locations),
                "excluded_locations": list(profile.excluded_locations),
                "allowed_companies": list(profile.allowed_companies),
                "allowed_industries": list(profile.allowed_industries),
                "work_modes": list(profile.work_modes),
                "employment_types": list(profile.employment_types),
                "seniority_levels": list(profile.seniority_levels),
                "minimum_salary": profile.minimum_salary,
                "salary_currency": profile.salary_currency,
                "resume": profile.resume.review_safe() if profile.resume is not None else None,
            },
            "review_disposition": (
                "eligible_selection" if selected.selectable else "manual_evidence_required"
            ),
            reviewed_job_key: reviewed_job,
            "ranking": ranking,
            "ranking_summary": {
                "evaluated_count": len(ranked),
                "included_count": len(ranking),
                "truncated": len(ranked) > len(ranking),
            },
            "match_config_sha256": match_config_hash,
            "match_snapshot_sha256": match_hash,
            "prepared_materials": (
                [
                    {
                        "kind": "resume",
                        **profile.resume.review_safe(),
                    }
                ]
                if profile.resume is not None
                else []
            ),
            "manual_follow_up": {
                "application_url": selected_job.canonical_url,
                "required": True,
            },
            "decision_effect": {
                "approve": "approve_package_for_manual_follow_up",
                "reject": "reject_goal_run",
                "creates_provider_outbox_event": False,
                "authorizes_email_send": False,
                "authorizes_application_submit": False,
            },
            "safety": {
                "provider_execution": "disabled",
                "outbox_enqueue": "disabled",
                "gmail_send": "disabled",
                "application_submit": "disabled",
                "external_write_performed": False,
                "human_review_required": True,
                "human_final_submission_required": True,
                "job_text_is_untrusted_data": True,
            },
        },
    )


def _deduplicated_jobs(candidates: Sequence[PreApplicationJob]) -> tuple[PreApplicationJob, ...]:
    result: list[PreApplicationJob] = []
    seen: set[UUID] = set()
    for candidate in candidates:
        if candidate.canonical_job_id in seen:
            continue
        seen.add(candidate.canonical_job_id)
        result.append(candidate)
    return tuple(result)


def _parse_material(value: object) -> CandidateMaterialSnapshot:
    mapping = _mapping(value, "resume", "BLOCKED_INVALID_CANDIDATE_PROFILE")
    _reject_extra(
        mapping,
        {"object_key", "filename", "content_type", "size_bytes", "sha256"},
    )
    object_key = _required_text(mapping, "object_key", max_length=256)
    filename = _required_text(mapping, "filename", max_length=180)
    content_type = _required_text(mapping, "content_type", max_length=160)
    size = mapping.get("size_bytes")
    sha256 = _required_text(mapping, "sha256", max_length=64)
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 25 * 1024 * 1024:
        raise ValueError("size_bytes")
    if _SHA256.fullmatch(sha256) is None:
        raise ValueError("sha256")
    return CandidateMaterialSnapshot(
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size_bytes=size,
        sha256=sha256,
    )


def _mapping(value: object, label: str, missing_reason: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PreApplicationConfigurationError(missing_reason)
    mapping = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) for key in mapping):
        raise PreApplicationConfigurationError("BLOCKED_INVALID_PREAPPLICATION_CONFIGURATION")
    return cast("Mapping[str, object]", mapping)


def _reject_extra(mapping: Mapping[str, object], allowed: set[str]) -> None:
    if set(mapping) - allowed:
        raise ValueError("unsupported keys")


def _required_text(mapping: Mapping[str, object], key: str, *, max_length: int) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ValueError(key)
    normalized = _SPACE.sub(" ", value).strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(key)
    if any(ord(character) < 32 for character in normalized):
        raise ValueError(key)
    return normalized


def _string_tuple(
    value: object,
    label: str,
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(label)
    items = tuple(_normalize_item(item, label) for item in cast("Sequence[object]", value))
    result = tuple(sorted(set(items)))
    if not minimum <= len(result) <= maximum:
        raise ValueError(label)
    return result


def _enum_tuple(
    value: object,
    label: str,
    allowed: set[str],
    maximum: int,
) -> tuple[str, ...]:
    result = _string_tuple(value, label, 0, maximum)
    if any(item not in allowed for item in result):
        raise ValueError(label)
    return result


def _optional_enum_text(value: object, label: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise ValueError(label)
    normalized = value.strip().casefold()
    if normalized not in allowed:
        raise ValueError(label)
    return normalized


def _optional_bool(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(label)
    return value


def _normalize_item(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(label)
    normalized = _SPACE.sub(" ", value).strip().casefold()
    if not normalized or len(normalized) > 160:
        raise ValueError(label)
    return normalized


def _normalize(value: str) -> str:
    return _SPACE.sub(" ", value.casefold()).strip()[:_MAX_TEXT]


def _term_coverage(term: str, haystack: str) -> float:
    normalized = _normalize(term)
    if not normalized:
        return 0.0
    if normalized in haystack:
        return 1.0
    tokens = tuple(dict.fromkeys(_TOKEN.findall(normalized)))
    if not tokens:
        return 0.0
    return sum(1 for token in tokens if token in haystack) / len(tokens)


def _first_text(mapping: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize(value)
    return ""


def _structured_terms(mapping: Mapping[str, object], *keys: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            values.extend(
                item
                for item in cast("Sequence[object]", value)
                if isinstance(item, str) and item.strip()
            )
    return tuple(sorted({_normalize(value) for value in values if _normalize(value)}))


def _required_keyword_evidence(job: PreApplicationJob) -> str:
    description = job.description.strip()
    if description == job.canonical_url or re.fullmatch(r"https?://\S+", description):
        description = ""
    return _normalize(" ".join((description, " ".join(job.keywords))))


def _company_allowlist_fit(
    allowed_companies: Sequence[str],
    company_name: str,
    structured: Mapping[str, object],
) -> tuple[bool, bool]:
    if not allowed_companies:
        return False, False
    actual_name = _normalize_identity(company_name)
    actual_domain = _normalize_domain(
        _raw_first_text(structured, "company_domain", "employer_domain")
    )
    allowed_names = {_normalize_identity(value) for value in allowed_companies}
    allowed_domains = {
        domain for value in allowed_companies if (domain := _normalize_domain(value)) is not None
    }
    if actual_name in allowed_names or (
        actual_domain is not None and actual_domain in allowed_domains
    ):
        return False, False
    verified = (
        structured.get("company_identity_verified") is True
        or _first_text(
            structured,
            "company_identity_status",
        )
        == "verified"
    )
    return verified, not verified


def _industry_allowlist_fit(
    allowed: Sequence[str],
    actual: Sequence[str],
) -> tuple[bool, bool]:
    if not allowed:
        return False, False
    if not actual:
        return False, True
    allowed_identities = {_normalize_identity(item) for item in allowed}
    actual_identities = {_normalize_identity(item) for item in actual}
    if allowed_identities & actual_identities:
        return False, False
    return True, False


def _normalize_identity(value: str) -> str:
    return _SPACE.sub(" ", value.casefold()).strip()


def _location_term_matches(expected: str, actual: str) -> bool:
    normalized_expected = _normalize_identity(expected)
    normalized_actual = _normalize_identity(actual)
    if not normalized_expected or not normalized_actual:
        return False
    if re.search(r"[\u3400-\u9fff]", normalized_expected):
        return normalized_expected in normalized_actual
    pieces = tuple(
        re.escape(piece)
        for piece in re.split(r"[^a-z0-9]+", normalized_expected)
        if piece
    )
    if not pieces:
        return False
    pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]+".join(pieces) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_actual) is not None


def _normalize_domain(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    normalized = re.sub(r"^[a-z][a-z0-9+.-]*://", "", normalized)
    normalized = normalized.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    if normalized.startswith("www."):
        normalized = normalized[4:]
    if re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
        normalized,
    ) is None:
        return None
    return normalized


def _raw_first_text(mapping: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _location_fit(
    profile: CandidateProfileSnapshot,
    location: str,
    normalized_work_mode: str | None,
) -> tuple[float, bool, bool, bool]:
    remote = normalized_work_mode == "remote"
    preferred = any(_location_term_matches(item, location) for item in profile.locations)
    remote_gate = profile.remote_preference == "required" and not remote
    if profile.locations:
        if preferred:
            return 1.0, remote_gate, False, False
        scoped_location = re.sub(
            r"\b(remote|hybrid|onsite|on[ _-]?site|anywhere|worldwide)\b|远程|混合|现场",
            " ",
            location,
        )
        scoped_location = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", scoped_location).strip()
        if not scoped_location:
            return 0.5, remote_gate, False, True
        return 0.0, remote_gate, True, False
    if profile.remote_preference == "required":
        return (1.0 if remote else 0.0, remote_gate, False, False)
    if profile.remote_preference == "preferred":
        return (1.0 if remote else 0.35, False, False, False)
    if profile.remote_preference == "onsite":
        return (0.3 if remote else 0.5, False, False, False)
    if profile.remote_preference == "acceptable":
        return (1.0 if remote else 0.6, False, False, False)
    return (0.5, False, False, False)


def _employment_fit(
    profile: CandidateProfileSnapshot,
    employment_type: str | None,
) -> tuple[float, bool, bool]:
    if not profile.employment_types:
        return 0.5, False, False
    normalized = _normalized_employment_type(employment_type)
    if normalized is None:
        return 0.5, False, True
    matched = normalized in profile.employment_types
    return (1.0 if matched else 0.0, not matched, False)


def _normalized_employment_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^a-z]+", "_", value.casefold()).strip("_")
    aliases = {
        "fulltime": "full_time",
        "full_time": "full_time",
        "parttime": "part_time",
        "part_time": "part_time",
        "contractor": "contract",
        "contract": "contract",
        "intern": "internship",
        "internship": "internship",
        "temporary": "temporary",
        "temp": "temporary",
    }
    return aliases.get(normalized)


def _work_mode_fit(
    profile: CandidateProfileSnapshot,
    work_mode: str | None,
    remote: bool | str | None,
    haystack: str,
) -> tuple[float, bool, bool]:
    if not profile.work_modes:
        return 0.5, False, False
    normalized = _normalized_work_mode(work_mode, remote, haystack)
    if normalized is None:
        return 0.5, False, True
    matched = normalized in profile.work_modes
    return (1.0 if matched else 0.0, not matched, False)


def _normalized_work_mode(
    work_mode: str | None,
    remote: bool | str | None,
    location_evidence: str,
) -> str | None:
    explicit_values = " ".join(
        value
        for value in (
            work_mode or "",
            remote if isinstance(remote, str) else "",
        )
        if value
    ).casefold()
    modes: set[str] = set()
    if remote is True:
        modes.add("remote")
    elif remote is False:
        modes.add("onsite")
    if any(marker in explicit_values for marker in ("remote", "远程")):
        modes.add("remote")
    if any(marker in explicit_values for marker in ("hybrid", "混合")):
        modes.add("hybrid")
    if any(marker in explicit_values for marker in ("onsite", "on_site", "on site", "现场")):
        modes.add("onsite")
    if not explicit_values and remote is None:
        if any(marker in location_evidence for marker in ("remote", "远程")):
            modes.add("remote")
        if any(marker in location_evidence for marker in ("hybrid", "混合")):
            modes.add("hybrid")
        if any(marker in location_evidence for marker in ("onsite", "on site", "现场")):
            modes.add("onsite")
    return next(iter(modes)) if len(modes) == 1 else None


def _salary_fit(
    profile: CandidateProfileSnapshot,
    salary: JsonValue | None,
) -> tuple[bool, bool]:
    if profile.minimum_salary is None:
        return False, False
    if not isinstance(salary, Mapping):
        return False, True
    salary_map = cast("Mapping[object, object]", salary)
    currency = _mapping_text(salary_map, "currency", "currencyCode", "currency_code")
    period = _mapping_text(salary_map, "period", "interval", "unit", "pay_period")
    maximum = _mapping_number(
        salary_map,
        "max",
        "maximum",
        "maxValue",
        "salary_max",
        "to",
    )
    if (
        currency is None
        or currency.upper() != profile.salary_currency
        or period is None
        or period.casefold() not in {"year", "yearly", "annual", "annually", "per_year"}
        or maximum is None
    ):
        return False, True
    return maximum < profile.minimum_salary, False


def _mapping_text(mapping: Mapping[object, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _mapping_number(mapping: Mapping[object, object], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _seniority_fit(
    profile: CandidateProfileSnapshot,
    explicit_seniority: str | None,
    title: str,
) -> tuple[float, bool, bool]:
    if not profile.seniority_levels:
        return _seniority_score(title, profile.years_experience), False, False
    normalized = _normalized_seniority(explicit_seniority, title)
    if normalized is None:
        if "unknown" in profile.seniority_levels:
            return 0.5, False, False
        return 0.5, False, True
    matched = normalized in profile.seniority_levels
    return (1.0 if matched else 0.0, not matched, False)


def _normalized_seniority(explicit_seniority: str | None, title: str) -> str | None:
    explicit = _normalize(explicit_seniority or "")
    if explicit:
        return _seniority_value(explicit)
    return _seniority_value(_normalize(title))


def _seniority_value(value: str) -> str | None:
    levels: set[str] = set()
    if any(
        term in value
        for term in ("chief", "director", "head of", "vice president", "vp", "总监")
    ):
        levels.add("executive")
    if any(term in value for term in ("principal", "首席")):
        levels.add("principal")
    if any(term in value for term in ("staff", "tech lead", "team lead", "负责人")):
        levels.add("staff")
    if any(term in value for term in ("senior", "sr.", "sr ", "高级", "资深")):
        levels.add("senior")
    if any(term in value for term in ("mid level", "mid-level", "intermediate", "中级")):
        levels.add("mid")
    if any(term in value for term in ("entry", "junior", "new grad", "graduate", "初级")):
        levels.add("entry")
    if any(term in value for term in ("intern", "internship", "实习")):
        levels.add("intern")
    aliases = {
        "mid": "mid",
        "mid_level": "mid",
        "mid-level": "mid",
        "sr": "senior",
        "lead": "staff",
        "exec": "executive",
    }
    alias = aliases.get(value)
    if alias is not None:
        levels.add(alias)
    return next(iter(levels)) if len(levels) == 1 else None


def _seniority_score(title: str, years_experience: float | None) -> float:
    normalized = _normalize(title)
    if years_experience is None:
        return 0.5
    if any(term in normalized for term in ("intern", "实习", "graduate", "new grad")):
        return 0.2
    if any(term in normalized for term in ("director", "head of", "vp", "总监")):
        return 0.25 if years_experience < 10 else 0.7
    if any(term in normalized for term in ("principal", "staff", "lead", "负责人")):
        return 0.65 if years_experience >= 6 else 0.35
    if any(term in normalized for term in ("senior", "高级", "资深")):
        return 1.0 if years_experience >= 5 else 0.5
    if any(term in normalized for term in ("junior", "初级")):
        return 0.55
    return 0.9


__all__ = [
    "PREAPPLICATION_ARTIFACT_VERSION",
    "PREAPPLICATION_EXECUTION_MODE",
    "PREAPPLICATION_REVIEW_KIND",
    "CandidateMaterialSnapshot",
    "CandidateProfileSnapshot",
    "PreApplicationComposition",
    "PreApplicationConfigurationError",
    "PreApplicationJob",
    "PreApplicationMatchConfig",
    "PreApplicationRankedMatch",
    "compose_preapplication_review",
    "parse_candidate_profile",
    "parse_match_config",
]
