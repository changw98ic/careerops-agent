"""M2 application services: evidence, matching, scoring, remote eligibility, compensation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.domain.candidates import (
    CompensationRecord,
    EvidenceItem,
    EvidenceKind,
    FxSnapshot,
    JobRequirement,
    MatchLevel,
    MatchResult,
    MatchTier,
    RemoteEligibility,
    RemoteEligibilityVerdict,
    RequirementMatch,
)

RULES_VERSION = "m2-rules-v1"


def compute_input_hash(*parts: str) -> str:
    """Compute a deterministic hash from input parts for reproducibility."""
    canonical = json.dumps(sorted(parts), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_output_hash(result: MatchResult) -> str:
    """Compute a deterministic hash of a match result for replay verification."""
    data = {
        "candidate_id": str(result.candidate_id),
        "canonical_job_id": str(result.canonical_job_id),
        "tier": result.tier.value,
        "overall_score": result.overall_score,
        "geographic_blocked": result.geographic_blocked,
        "remote_verdict": result.remote_verdict.value,
        "requirement_matches": [
            {
                "requirement_name": m.requirement_name,
                "level": m.level.value,
                "confidence": m.confidence,
            }
            for m in result.requirement_matches
        ],
    }
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Evidence Import
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceImportRequest:
    candidate_id: UUID
    kind: EvidenceKind
    name: str
    description: str = ""
    repository: str = ""
    commit_sha: str = ""
    path: str = ""
    symbol: str = ""
    source_url: str = ""


class EvidenceRepository(Protocol):
    def find_by_idempotency_key(
        self,
        candidate_id: UUID,
        repository: str,
        commit_sha: str,
        path: str,
        symbol: str,
        content_hash: str,
    ) -> EvidenceItem | None: ...

    def find_by_candidate(self, candidate_id: UUID) -> list[EvidenceItem]: ...

    def save(self, item: EvidenceItem) -> None: ...


class EvidenceImportService:
    """Handles idempotent import of candidate evidence."""

    def __init__(self, repository: EvidenceRepository) -> None:
        self._repository = repository

    def import_evidence(self, request: EvidenceImportRequest) -> EvidenceItem:
        content_hash = self._compute_content_hash(request)
        existing = self._repository.find_by_idempotency_key(
            candidate_id=request.candidate_id,
            repository=request.repository,
            commit_sha=request.commit_sha,
            path=request.path,
            symbol=request.symbol,
            content_hash=content_hash,
        )
        if existing is not None:
            return existing

        item = EvidenceItem(
            id=uuid4(),
            candidate_id=request.candidate_id,
            kind=request.kind,
            name=request.name,
            description=request.description,
            repository=request.repository,
            commit_sha=request.commit_sha,
            path=request.path,
            symbol=request.symbol,
            content_hash=content_hash,
            source_url=request.source_url,
            verified=False,
        )
        self._repository.save(item)
        return item

    def _compute_content_hash(self, request: EvidenceImportRequest) -> str:
        parts = [
            request.kind.value,
            request.name,
            request.description,
            request.repository,
            request.commit_sha,
            request.path,
            request.symbol,
        ]
        return compute_input_hash(*parts)


# ---------------------------------------------------------------------------
# Requirement Extraction (deterministic)
# ---------------------------------------------------------------------------

_HARD_REQUIREMENT_PATTERNS = re.compile(
    r"\b(must have|required|mandatory|essential|minimum)\b", re.IGNORECASE
)

_SKILL_PATTERNS = re.compile(
    r"\b(Python|Java|Go|Rust|TypeScript|JavaScript|React|Vue|Angular|"
    r"PostgreSQL|MySQL|Redis|Docker|Kubernetes|AWS|GCP|Azure|"
    r"Machine Learning|Deep Learning|NLP|LLM|"
    r"FastAPI|Django|Flask|Spring|Node\.js|"
    r"SQL|NoSQL|GraphQL|REST|gRPC)\b",
    re.IGNORECASE,
)


def extract_requirements(structured_data: dict[str, object]) -> list[JobRequirement]:
    """Extract job requirements deterministically from structured posting data."""
    requirements: list[JobRequirement] = []
    seen_names: set[str] = set()

    skills = structured_data.get("skills", [])
    if isinstance(skills, list):
        for skill in skills:  # pyright: ignore[reportUnknownVariableType]
            name = str(skill).strip()  # pyright: ignore[reportUnknownArgumentType]
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                requirements.append(
                    JobRequirement(name=name, is_hard_requirement=False, category="skill")
                )

    description = str(structured_data.get("description_text", ""))
    for match in _SKILL_PATTERNS.finditer(description):
        name = match.group(0).strip()
        if name.lower() not in seen_names:
            seen_names.add(name.lower())
            context_start = max(0, match.start() - 50)
            context_end = min(len(description), match.end() + 50)
            context = description[context_start:context_end]
            is_hard = bool(_HARD_REQUIREMENT_PATTERNS.search(context))
            requirements.append(
                JobRequirement(
                    name=name,
                    is_hard_requirement=is_hard,
                    category="skill",
                    source_text=context.strip(),
                )
            )

    return requirements


# ---------------------------------------------------------------------------
# Remote Eligibility (deterministic hard gate)
# ---------------------------------------------------------------------------

_CHINA_BLOCKED_PATTERNS = re.compile(
    r"\b(us only|usa only|united states only|us citizens|"
    r"must be (located|based) in (the )?(us|usa|united states)|"
    r"no (china|chinese)|"
    r"cannot (work|hire) (from|in) (china)|"
    r"requires? (us|usa) (work authorization|citizenship|clearance)|"
    r"security clearance required)\b",
    re.IGNORECASE,
)

_REMOTE_ELIGIBLE_PATTERNS = re.compile(
    r"\b(remote|work from anywhere|distributed team|global remote|"
    r"remote[- ]?(friendly|first|ok)|anywhere in the world|"
    r"fully remote|100% remote)\b",
    re.IGNORECASE,
)

_AMBIGUOUS_PATTERNS = re.compile(
    r"\b(apac|asia[- ]?pacific|emea|americas|"
    r"remote within (the )?(us|europe|region)|"
    r"hybrid|onsite|in[- ]?office)\b",
    re.IGNORECASE,
)


def assess_remote_eligibility(
    structured_data: dict[str, object],
    rules_version: str = RULES_VERSION,
) -> RemoteEligibility:
    """Deterministically assess remote eligibility for China/Chengdu."""
    canonical_job_id = UUID(str(structured_data.get("canonical_job_id", str(uuid4()))))
    location = str(structured_data.get("location", ""))
    description = str(structured_data.get("description_text", ""))
    remote_field = structured_data.get("remote_eligible")

    combined_text = f"{location} {description}"

    if _CHINA_BLOCKED_PATTERNS.search(combined_text):
        return RemoteEligibility(
            canonical_job_id=canonical_job_id,
            verdict=RemoteEligibilityVerdict.NOT_ELIGIBLE,
            confidence=0.95,
            evidence_spans=(_extract_matching_span(combined_text, _CHINA_BLOCKED_PATTERNS),),
            structured_fields={"location": location},
            reason="Explicit geographic restriction detected",
            rules_version=rules_version,
        )

    if remote_field is False:
        return RemoteEligibility(
            canonical_job_id=canonical_job_id,
            verdict=RemoteEligibilityVerdict.NOT_ELIGIBLE,
            confidence=0.90,
            structured_fields={"location": location, "remote_eligible": "false"},
            reason="Structured field indicates not remote",
            rules_version=rules_version,
        )

    if _AMBIGUOUS_PATTERNS.search(combined_text):
        return RemoteEligibility(
            canonical_job_id=canonical_job_id,
            verdict=RemoteEligibilityVerdict.REVIEW_REQUIRED,
            confidence=0.50,
            evidence_spans=(_extract_matching_span(combined_text, _AMBIGUOUS_PATTERNS),),
            structured_fields={"location": location},
            reason="Ambiguous geographic scope (APAC/region-specific)",
            rules_version=rules_version,
        )

    if _REMOTE_ELIGIBLE_PATTERNS.search(combined_text) and remote_field is not False:
        return RemoteEligibility(
            canonical_job_id=canonical_job_id,
            verdict=RemoteEligibilityVerdict.ELIGIBLE,
            confidence=0.85,
            evidence_spans=(_extract_matching_span(combined_text, _REMOTE_ELIGIBLE_PATTERNS),),
            structured_fields={"location": location},
            reason="Remote-friendly indicators found",
            rules_version=rules_version,
        )

    return RemoteEligibility(
        canonical_job_id=canonical_job_id,
        verdict=RemoteEligibilityVerdict.UNKNOWN,
        confidence=0.0,
        structured_fields={"location": location},
        reason="No remote eligibility signals found",
        rules_version=rules_version,
    )


def _extract_matching_span(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if match is None:
        return ""
    start = max(0, match.start() - 20)
    end = min(len(text), match.end() + 20)
    return text[start:end].strip()


# ---------------------------------------------------------------------------
# Matching Engine
# ---------------------------------------------------------------------------


def _normalize_skill_name(name: str) -> str:
    return re.sub(r"[^a-z0-9+#.]", "", name.lower().strip())


class MatchingEngine:
    """Deterministic evidence-constrained matching engine."""

    def match_requirements(
        self,
        requirements: list[JobRequirement],
        evidence_items: list[EvidenceItem],
    ) -> list[RequirementMatch]:
        evidence_by_name: dict[str, list[EvidenceItem]] = {}
        for e in evidence_items:
            key = _normalize_skill_name(e.name)
            evidence_by_name.setdefault(key, []).append(e)

        results: list[RequirementMatch] = []
        for req in requirements:
            req_norm = _normalize_skill_name(req.name)
            matching_evidence = evidence_by_name.get(req_norm, [])

            if matching_evidence:
                verified = [e for e in matching_evidence if e.verified]
                if verified:
                    level = MatchLevel.STRONG
                    confidence = 0.95
                    reason = "Verified evidence directly matches requirement"
                else:
                    level = MatchLevel.PARTIAL
                    confidence = 0.70
                    reason = "Unverified evidence matches requirement"
                evidence_ids = tuple(e.id for e in matching_evidence)
            elif req.is_hard_requirement:
                level = MatchLevel.HARD_FAIL
                confidence = 0.90
                evidence_ids = ()
                reason = "Hard requirement with no matching evidence"
            else:
                level = MatchLevel.UNSUPPORTED
                confidence = 0.30
                evidence_ids = ()
                reason = "No evidence found for this requirement"

            results.append(
                RequirementMatch(
                    requirement_name=req.name,
                    level=level,
                    evidence_ids=evidence_ids,
                    confidence=confidence,
                    reason=reason,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_LEVEL_WEIGHTS: dict[MatchLevel, float] = {
    MatchLevel.STRONG: 1.0,
    MatchLevel.PARTIAL: 0.6,
    MatchLevel.TRANSFERABLE: 0.4,
    MatchLevel.UNSUPPORTED: 0.1,
    MatchLevel.HARD_FAIL: 0.0,
}


def compute_match_score(requirement_matches: list[RequirementMatch]) -> float:
    """Compute overall match score from requirement matches."""
    if not requirement_matches:
        return 0.0
    total_weight = 0.0
    earned_weight = 0.0
    for m in requirement_matches:
        weight = 2.0 if m.level == MatchLevel.HARD_FAIL or _is_hard_req(m) else 1.0
        total_weight += weight
        earned_weight += weight * _LEVEL_WEIGHTS[m.level]
    if total_weight == 0:
        return 0.0
    return round(earned_weight / total_weight, 4)


def _is_hard_req(match: RequirementMatch) -> bool:
    return match.level == MatchLevel.HARD_FAIL


def determine_tier(
    score: float,
    geographic_blocked: bool,
    has_hard_fail: bool,
) -> MatchTier:
    """Determine match tier from score and constraints."""
    if geographic_blocked or has_hard_fail:
        return MatchTier.BLOCKED
    if score >= 0.85:
        return MatchTier.APPLY_NOW
    if score >= 0.70:
        return MatchTier.STRONG_CANDIDATE
    if score >= 0.50:
        return MatchTier.WORTH_EXPLORING
    if score >= 0.30:
        return MatchTier.STRETCH
    return MatchTier.NOT_RECOMMENDED


# ---------------------------------------------------------------------------
# Match Orchestration
# ---------------------------------------------------------------------------


class MatchDataRepository(Protocol):
    """Repository port for looking up job data and candidate evidence for matching."""

    def get_job_structured_data(self, canonical_job_id: UUID) -> dict[str, object] | None: ...

    def get_candidate_evidence(self, candidate_id: UUID) -> list[EvidenceItem]: ...


class MatchResultRepository(Protocol):
    """Persistence port for completed deterministic matching results."""

    def add_match(self, result: MatchResult) -> None: ...


class MatchOrchestrator:
    """Orchestrates the full matching pipeline: extract -> match -> score -> tier."""

    def __init__(
        self,
        engine: MatchingEngine | None = None,
        data_repository: MatchDataRepository | None = None,
        result_repository: MatchResultRepository | None = None,
    ) -> None:
        self._engine = engine or MatchingEngine()
        self._data_repository = data_repository
        self._result_repository = result_repository

    def run_match_for_request(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        now: datetime,
    ) -> MatchResult:
        """Run matching using repository data. Used by the API layer."""
        structured_data: dict[str, object] = {}
        evidence_items: list[EvidenceItem] = []
        if self._data_repository is not None:
            job_data = self._data_repository.get_job_structured_data(canonical_job_id)
            if job_data is not None:
                structured_data = job_data
            evidence_items = self._data_repository.get_candidate_evidence(candidate_id)
        result = self.run_match(
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            structured_data=structured_data,
            evidence_items=evidence_items,
            now=now,
        )
        if self._result_repository is not None:
            self._result_repository.add_match(result)
        return result

    def run_match(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        structured_data: dict[str, object],
        evidence_items: list[EvidenceItem],
        now: datetime,
    ) -> MatchResult:
        requirements = extract_requirements(structured_data)
        remote = assess_remote_eligibility(
            {**structured_data, "canonical_job_id": str(canonical_job_id)}
        )
        geographic_blocked = remote.verdict == RemoteEligibilityVerdict.NOT_ELIGIBLE

        requirement_matches = self._engine.match_requirements(requirements, evidence_items)
        score = compute_match_score(requirement_matches)
        has_hard_fail = any(m.level == MatchLevel.HARD_FAIL for m in requirement_matches)

        tier = determine_tier(score, geographic_blocked, has_hard_fail)

        if geographic_blocked and tier != MatchTier.BLOCKED:
            tier = MatchTier.BLOCKED

        input_hash = compute_input_hash(
            str(candidate_id),
            str(canonical_job_id),
            json.dumps(structured_data, sort_keys=True, ensure_ascii=True),
            json.dumps(
                [str(e.id) for e in sorted(evidence_items, key=lambda x: str(x.id))],
            ),
            RULES_VERSION,
        )

        result = MatchResult(
            id=uuid4(),
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            tier=tier,
            overall_score=score,
            requirement_matches=tuple(requirement_matches),
            geographic_blocked=geographic_blocked,
            remote_verdict=remote.verdict,
            rules_version=RULES_VERSION,
            input_hash=input_hash,
            created_at=now,
        )
        output_hash = compute_output_hash(result)
        return MatchResult(
            id=result.id,
            candidate_id=result.candidate_id,
            canonical_job_id=result.canonical_job_id,
            tier=result.tier,
            overall_score=result.overall_score,
            requirement_matches=result.requirement_matches,
            geographic_blocked=result.geographic_blocked,
            remote_verdict=result.remote_verdict,
            rules_version=result.rules_version,
            input_hash=result.input_hash,
            output_hash=output_hash,
            created_at=result.created_at,
        )


# ---------------------------------------------------------------------------
# Compensation Normalization
# ---------------------------------------------------------------------------


def normalize_compensation(
    canonical_job_id: UUID,
    currency: str,
    amount_min: float | None,
    amount_max: float | None,
    period: str,
    source_text: str,
    fx_snapshots: list[FxSnapshot],
    normalized_currency: str = "CNY",
) -> CompensationRecord:
    """Normalize compensation with static FX snapshots. No FX = unknown/neutral."""
    fx_rate: float | None = None
    fx_effective_date: date | None = None
    norm_min: float | None = None
    norm_max: float | None = None
    score = "unknown"

    if currency.upper() == normalized_currency.upper():
        fx_rate = 1.0
        norm_min = amount_min
        norm_max = amount_max
        score = _compute_salary_score(amount_min, amount_max)
    elif amount_min is not None or amount_max is not None:
        snapshot = _find_fx_snapshot(currency, normalized_currency, fx_snapshots)
        if snapshot is not None:
            fx_rate = snapshot.rate
            fx_effective_date = snapshot.effective_date
            norm_min = round(amount_min * fx_rate, 2) if amount_min is not None else None
            norm_max = round(amount_max * fx_rate, 2) if amount_max is not None else None
            score = _compute_salary_score(norm_min, norm_max)
        else:
            score = "unknown"

    return CompensationRecord(
        id=uuid4(),
        canonical_job_id=canonical_job_id,
        currency=currency.upper(),
        amount_min=amount_min,
        amount_max=amount_max,
        period=period,
        fx_rate=fx_rate,
        fx_effective_date=fx_effective_date,
        normalized_amount_min=norm_min,
        normalized_amount_max=norm_max,
        normalized_currency=normalized_currency,
        score=score,
        source_text=source_text,
    )


def _find_fx_snapshot(
    from_currency: str, to_currency: str, snapshots: list[FxSnapshot]
) -> FxSnapshot | None:
    from_upper = from_currency.upper()
    to_upper = to_currency.upper()
    for s in snapshots:
        if s.from_currency.upper() == from_upper and s.to_currency.upper() == to_upper:
            return s
    for s in snapshots:
        if s.from_currency.upper() == to_upper and s.to_currency.upper() == from_upper:
            return FxSnapshot(
                from_currency=from_upper,
                to_currency=to_upper,
                rate=1.0 / s.rate,
                effective_date=s.effective_date,
            )
    return None


def _compute_salary_score(amount_min: float | None, amount_max: float | None) -> str:
    if amount_min is None and amount_max is None:
        return "unknown"
    effective = amount_max if amount_max is not None else amount_min
    if effective is None:
        return "unknown"
    if effective >= 500000:
        return "high"
    if effective >= 250000:
        return "competitive"
    if effective >= 100000:
        return "moderate"
    return "below_market"
