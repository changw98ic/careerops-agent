"""Unit tests for M2: evidence import, matching, scoring, remote eligibility, compensation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from careerops.application.matching import (
    EvidenceImportRequest,
    EvidenceImportService,
    MatchingEngine,
    MatchOrchestrator,
    assess_remote_eligibility,
    compute_input_hash,
    compute_match_score,
    compute_output_hash,
    determine_tier,
    extract_requirements,
    normalize_compensation,
)
from careerops.domain.candidates import (
    EvidenceItem,
    EvidenceKind,
    FxSnapshot,
    JobRequirement,
    MatchLevel,
    MatchTier,
    RemoteEligibilityVerdict,
    RequirementMatch,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
CANDIDATE_ID = uuid4()
JOB_ID = uuid4()


# ---------------------------------------------------------------------------
# Evidence Import Tests
# ---------------------------------------------------------------------------


class InMemoryEvidenceRepo:
    def __init__(self) -> None:
        self._items: list[EvidenceItem] = []

    def find_by_idempotency_key(
        self,
        candidate_id: UUID,
        repository: str,
        commit_sha: str,
        path: str,
        symbol: str,
        content_hash: str,
    ) -> EvidenceItem | None:
        for item in self._items:
            if (
                item.candidate_id == candidate_id
                and item.repository == repository
                and item.commit_sha == commit_sha
                and item.path == path
                and item.symbol == symbol
                and item.content_hash == content_hash
            ):
                return item
        return None

    def find_by_candidate(self, candidate_id: UUID) -> list[EvidenceItem]:
        return [i for i in self._items if i.candidate_id == candidate_id]

    def save(self, item: EvidenceItem) -> None:
        self._items.append(item)


class TestEvidenceImport:
    def test_import_creates_new_item(self) -> None:
        repo = InMemoryEvidenceRepo()
        service = EvidenceImportService(repo)
        request = EvidenceImportRequest(
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Python",
            repository="github.com/user/repo",
            commit_sha="abc123",
            path="src/main.py",
            symbol="def hello",
        )
        result = service.import_evidence(request)
        assert result.name == "Python"
        assert result.candidate_id == CANDIDATE_ID
        assert result.kind == EvidenceKind.SKILL
        assert result.content_hash != ""

    def test_import_is_idempotent(self) -> None:
        repo = InMemoryEvidenceRepo()
        service = EvidenceImportService(repo)
        request = EvidenceImportRequest(
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Python",
            repository="github.com/user/repo",
            commit_sha="abc123",
            path="src/main.py",
            symbol="def hello",
        )
        first = service.import_evidence(request)
        second = service.import_evidence(request)
        assert first.id == second.id
        assert len(repo.find_by_candidate(CANDIDATE_ID)) == 1

    def test_different_content_creates_new_item(self) -> None:
        repo = InMemoryEvidenceRepo()
        service = EvidenceImportService(repo)
        req1 = EvidenceImportRequest(
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Python",
            repository="github.com/user/repo",
            commit_sha="abc123",
            path="src/main.py",
            symbol="def hello",
        )
        req2 = EvidenceImportRequest(
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Go",
            repository="github.com/user/repo",
            commit_sha="abc123",
            path="src/main.go",
            symbol="func main",
        )
        service.import_evidence(req1)
        service.import_evidence(req2)
        assert len(repo.find_by_candidate(CANDIDATE_ID)) == 2


# ---------------------------------------------------------------------------
# Requirement Extraction Tests
# ---------------------------------------------------------------------------


class TestRequirementExtraction:
    def test_extracts_from_skills_field(self) -> None:
        data: dict[str, object] = {"skills": ["Python", "Docker", "Kubernetes"]}
        reqs = extract_requirements(data)
        names = [r.name for r in reqs]
        assert "Python" in names
        assert "Docker" in names
        assert "Kubernetes" in names

    def test_extracts_from_description(self) -> None:
        data: dict[str, object] = {
            "description_text": "We need someone with Python and React experience."
        }
        reqs = extract_requirements(data)
        names = [r.name for r in reqs]
        assert "Python" in names
        assert "React" in names

    def test_hard_requirement_detection(self) -> None:
        data: dict[str, object] = {
            "description_text": "Must have Python experience. Nice to have React."
        }
        reqs = extract_requirements(data)
        python_req = next(r for r in reqs if r.name == "Python")
        assert python_req.is_hard_requirement is True

    def test_deduplicates_requirements(self) -> None:
        data: dict[str, object] = {
            "skills": ["Python"],
            "description_text": "Looking for Python developer with Python skills.",
        }
        reqs = extract_requirements(data)
        python_reqs = [r for r in reqs if r.name.lower() == "python"]
        assert len(python_reqs) == 1

    def test_empty_data_returns_empty(self) -> None:
        reqs = extract_requirements({})
        assert reqs == []


# ---------------------------------------------------------------------------
# Remote Eligibility Tests
# ---------------------------------------------------------------------------


class TestRemoteEligibility:
    def test_us_only_is_not_eligible(self) -> None:
        data: dict[str, object] = {
            "location": "US only",
            "description_text": "Must be located in the US.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.NOT_ELIGIBLE
        assert result.confidence > 0.8

    def test_security_clearance_blocked(self) -> None:
        data: dict[str, object] = {
            "description_text": "Security clearance required for this position.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.NOT_ELIGIBLE

    def test_fully_remote_is_eligible(self) -> None:
        data: dict[str, object] = {
            "location": "Remote",
            "description_text": "Fully remote position, work from anywhere.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.ELIGIBLE

    def test_apac_is_review_required(self) -> None:
        data: dict[str, object] = {
            "description_text": "Remote within APAC region.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.REVIEW_REQUIRED

    def test_no_signals_is_unknown(self) -> None:
        data: dict[str, object] = {
            "description_text": "Software engineer position.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.UNKNOWN

    def test_structured_remote_false_is_not_eligible(self) -> None:
        data: dict[str, object] = {
            "remote_eligible": False,
            "description_text": "Onsite position.",
        }
        result = assess_remote_eligibility(data)
        assert result.verdict == RemoteEligibilityVerdict.NOT_ELIGIBLE


# ---------------------------------------------------------------------------
# Matching Engine Tests
# ---------------------------------------------------------------------------


class TestMatchingEngine:
    def _make_evidence(self, name: str, verified: bool = False) -> EvidenceItem:
        return EvidenceItem(
            id=uuid4(),
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name=name,
            verified=verified,
        )

    def test_strong_match_with_verified_evidence(self) -> None:
        engine = MatchingEngine()
        evidence = [self._make_evidence("Python", verified=True)]
        reqs = [JobRequirement(name="Python", is_hard_requirement=False)]
        results = engine.match_requirements(reqs, evidence)
        assert len(results) == 1
        assert results[0].level == MatchLevel.STRONG
        assert results[0].confidence > 0.9

    def test_partial_match_with_unverified_evidence(self) -> None:
        engine = MatchingEngine()
        evidence = [self._make_evidence("Python", verified=False)]
        reqs = [JobRequirement(name="Python", is_hard_requirement=False)]
        results = engine.match_requirements(reqs, evidence)
        assert results[0].level == MatchLevel.PARTIAL

    def test_no_evidence_no_strong_match(self) -> None:
        """Critical M2 invariant: no evidence = no strong match."""
        engine = MatchingEngine()
        reqs = [
            JobRequirement(name="Python", is_hard_requirement=False),
            JobRequirement(name="Go", is_hard_requirement=False),
        ]
        results = engine.match_requirements(reqs, [])
        for r in results:
            assert r.level != MatchLevel.STRONG
            assert r.level != MatchLevel.PARTIAL

    def test_hard_requirement_no_evidence_is_hard_fail(self) -> None:
        engine = MatchingEngine()
        reqs = [JobRequirement(name="Python", is_hard_requirement=True)]
        results = engine.match_requirements(reqs, [])
        assert results[0].level == MatchLevel.HARD_FAIL

    def test_case_insensitive_matching(self) -> None:
        engine = MatchingEngine()
        evidence = [self._make_evidence("python", verified=True)]
        reqs = [JobRequirement(name="Python")]
        results = engine.match_requirements(reqs, evidence)
        assert results[0].level == MatchLevel.STRONG


# ---------------------------------------------------------------------------
# Scoring Tests
# ---------------------------------------------------------------------------


class TestScoring:
    def test_all_strong_gives_high_score(self) -> None:
        matches = [
            RequirementMatch(requirement_name="A", level=MatchLevel.STRONG, confidence=0.95),
            RequirementMatch(requirement_name="B", level=MatchLevel.STRONG, confidence=0.95),
        ]
        score = compute_match_score(matches)
        assert score >= 0.85

    def test_no_evidence_gives_low_score(self) -> None:
        matches = [
            RequirementMatch(requirement_name="A", level=MatchLevel.UNSUPPORTED, confidence=0.3),
            RequirementMatch(requirement_name="B", level=MatchLevel.UNSUPPORTED, confidence=0.3),
        ]
        score = compute_match_score(matches)
        assert score < 0.3

    def test_hard_fail_gives_zero_weight(self) -> None:
        matches = [
            RequirementMatch(requirement_name="A", level=MatchLevel.HARD_FAIL, confidence=0.9),
        ]
        score = compute_match_score(matches)
        assert score == 0.0

    def test_empty_matches_gives_zero(self) -> None:
        assert compute_match_score([]) == 0.0

    def test_geographic_blocked_always_blocked_tier(self) -> None:
        """Critical M2 invariant: geographic hard gate blocks apply_now."""
        tier = determine_tier(0.95, geographic_blocked=True, has_hard_fail=False)
        assert tier == MatchTier.BLOCKED

    def test_hard_fail_gives_blocked_tier(self) -> None:
        tier = determine_tier(0.95, geographic_blocked=False, has_hard_fail=True)
        assert tier == MatchTier.BLOCKED

    def test_high_score_no_blocks_gives_apply_now(self) -> None:
        tier = determine_tier(0.90, geographic_blocked=False, has_hard_fail=False)
        assert tier == MatchTier.APPLY_NOW

    def test_low_score_gives_not_recommended(self) -> None:
        tier = determine_tier(0.20, geographic_blocked=False, has_hard_fail=False)
        assert tier == MatchTier.NOT_RECOMMENDED


# ---------------------------------------------------------------------------
# Match Orchestrator Integration Tests
# ---------------------------------------------------------------------------


class TestMatchOrchestrator:
    def test_full_pipeline_no_evidence(self) -> None:
        """No evidence should never produce a strong match or apply_now tier."""
        orchestrator = MatchOrchestrator()
        structured_data: dict[str, object] = {
            "skills": ["Python", "Go"],
            "description_text": "Remote friendly position.",
        }
        result = orchestrator.run_match(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            structured_data=structured_data,
            evidence_items=[],
            now=NOW,
        )
        assert result.tier != MatchTier.APPLY_NOW
        assert result.tier != MatchTier.STRONG_CANDIDATE
        for m in result.requirement_matches:
            assert m.level != MatchLevel.STRONG

    def test_geographic_block_prevents_apply_now(self) -> None:
        """Geographic hard gate must block apply_now regardless of score."""
        orchestrator = MatchOrchestrator()
        evidence = [
            EvidenceItem(
                id=uuid4(),
                candidate_id=CANDIDATE_ID,
                kind=EvidenceKind.SKILL,
                name="Python",
                verified=True,
            ),
        ]
        structured_data: dict[str, object] = {
            "skills": ["Python"],
            "description_text": "US only. Must be located in the US.",
        }
        result = orchestrator.run_match(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            structured_data=structured_data,
            evidence_items=evidence,
            now=NOW,
        )
        assert result.tier == MatchTier.BLOCKED
        assert result.geographic_blocked is True

    def test_frozen_inputs_produce_stable_results(self) -> None:
        """Same inputs must produce identical output hashes."""
        orchestrator = MatchOrchestrator()
        evidence = [
            EvidenceItem(
                id=UUID("11111111-1111-1111-1111-111111111111"),
                candidate_id=CANDIDATE_ID,
                kind=EvidenceKind.SKILL,
                name="Python",
                verified=True,
            ),
        ]
        structured_data: dict[str, object] = {
            "skills": ["Python"],
            "description_text": "Fully remote position.",
        }
        result1 = orchestrator.run_match(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            structured_data=structured_data,
            evidence_items=evidence,
            now=NOW,
        )
        result2 = orchestrator.run_match(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            structured_data=structured_data,
            evidence_items=evidence,
            now=NOW,
        )
        assert result1.input_hash == result2.input_hash
        assert result1.output_hash == result2.output_hash
        assert result1.tier == result2.tier
        assert result1.overall_score == result2.overall_score

    def test_with_evidence_produces_strong_match(self) -> None:
        orchestrator = MatchOrchestrator()
        evidence = [
            EvidenceItem(
                id=uuid4(),
                candidate_id=CANDIDATE_ID,
                kind=EvidenceKind.SKILL,
                name="Python",
                verified=True,
            ),
            EvidenceItem(
                id=uuid4(),
                candidate_id=CANDIDATE_ID,
                kind=EvidenceKind.SKILL,
                name="Docker",
                verified=True,
            ),
        ]
        structured_data: dict[str, object] = {
            "skills": ["Python", "Docker"],
            "description_text": "Fully remote, work from anywhere.",
        }
        result = orchestrator.run_match(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            structured_data=structured_data,
            evidence_items=evidence,
            now=NOW,
        )
        assert result.overall_score >= 0.85
        assert result.tier == MatchTier.APPLY_NOW
        assert result.geographic_blocked is False


# ---------------------------------------------------------------------------
# Compensation Tests
# ---------------------------------------------------------------------------


class TestCompensation:
    def test_no_fx_snapshot_gives_unknown(self) -> None:
        """Without FX snapshot, cross-currency salary must be unknown/neutral."""
        record = normalize_compensation(
            canonical_job_id=JOB_ID,
            currency="USD",
            amount_min=100000,
            amount_max=150000,
            period="year",
            source_text="$100k-$150k",
            fx_snapshots=[],
        )
        assert record.score == "unknown"
        assert record.normalized_amount_min is None
        assert record.normalized_amount_max is None

    def test_same_currency_normalizes_directly(self) -> None:
        record = normalize_compensation(
            canonical_job_id=JOB_ID,
            currency="CNY",
            amount_min=300000,
            amount_max=400000,
            period="year",
            source_text="30-40万",
            fx_snapshots=[],
        )
        assert record.score == "competitive"
        assert record.normalized_amount_min == 300000
        assert record.normalized_amount_max == 400000

    def test_with_fx_snapshot_normalizes(self) -> None:
        snapshots = [
            FxSnapshot(
                from_currency="USD", to_currency="CNY", rate=7.2, effective_date=date(2026, 7, 1)
            )
        ]
        record = normalize_compensation(
            canonical_job_id=JOB_ID,
            currency="USD",
            amount_min=100000,
            amount_max=150000,
            period="year",
            source_text="$100k-$150k",
            fx_snapshots=snapshots,
        )
        assert record.score != "unknown"
        assert record.normalized_amount_min == 720000.0
        assert record.normalized_amount_max == 1080000.0
        assert record.fx_rate == 7.2

    def test_reverse_fx_lookup(self) -> None:
        snapshots = [
            FxSnapshot(
                from_currency="CNY", to_currency="USD", rate=0.139, effective_date=date(2026, 7, 1)
            )
        ]
        record = normalize_compensation(
            canonical_job_id=JOB_ID,
            currency="USD",
            amount_min=100000,
            amount_max=None,
            period="year",
            source_text="$100k+",
            fx_snapshots=snapshots,
        )
        assert record.fx_rate is not None
        assert record.normalized_amount_min is not None

    def test_no_amounts_gives_unknown(self) -> None:
        record = normalize_compensation(
            canonical_job_id=JOB_ID,
            currency="USD",
            amount_min=None,
            amount_max=None,
            period="",
            source_text="Competitive salary",
            fx_snapshots=[],
        )
        assert record.score == "unknown"


# ---------------------------------------------------------------------------
# Hash Stability Tests
# ---------------------------------------------------------------------------


class TestHashStability:
    def test_input_hash_deterministic(self) -> None:
        h1 = compute_input_hash("a", "b", "c")
        h2 = compute_input_hash("a", "b", "c")
        assert h1 == h2

    def test_input_hash_order_independent(self) -> None:
        h1 = compute_input_hash("a", "b", "c")
        h2 = compute_input_hash("c", "a", "b")
        assert h1 == h2

    def test_output_hash_deterministic(self) -> None:
        from careerops.domain.candidates import MatchResult

        result = MatchResult(
            id=uuid4(),
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            tier=MatchTier.APPLY_NOW,
            overall_score=0.9,
            requirement_matches=(
                RequirementMatch(
                    requirement_name="Python",
                    level=MatchLevel.STRONG,
                    confidence=0.95,
                ),
            ),
            rules_version="m2-rules-v1",
            input_hash="abc",
        )
        h1 = compute_output_hash(result)
        h2 = compute_output_hash(result)
        assert h1 == h2
        assert len(h1) == 64


# ---------------------------------------------------------------------------
# MatchOrchestrator.run_match_for_request Tests
# ---------------------------------------------------------------------------


class InMemoryMatchDataRepo:
    """In-memory implementation of MatchDataRepository for testing."""

    def __init__(
        self,
        jobs: dict[UUID, dict[str, object]] | None = None,
        evidence: dict[UUID, list[EvidenceItem]] | None = None,
    ) -> None:
        self._jobs = jobs or {}
        self._evidence = evidence or {}

    def get_job_structured_data(self, canonical_job_id: UUID) -> dict[str, object] | None:
        return self._jobs.get(canonical_job_id)

    def get_candidate_evidence(self, candidate_id: UUID) -> list[EvidenceItem]:
        return self._evidence.get(candidate_id, [])


class TestRunMatchForRequest:
    def test_with_data_repository_produces_match(self) -> None:
        evidence_item = EvidenceItem(
            id=UUID("22222222-2222-2222-2222-222222222222"),
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Python",
            verified=True,
        )
        repo = InMemoryMatchDataRepo(
            jobs={JOB_ID: {"skills": ["Python"], "description_text": "Fully remote."}},
            evidence={CANDIDATE_ID: [evidence_item]},
        )
        orchestrator = MatchOrchestrator(data_repository=repo)
        result = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            now=NOW,
        )
        assert result.overall_score >= 0.85
        assert result.tier == MatchTier.APPLY_NOW
        assert result.geographic_blocked is False

    def test_without_data_repository_produces_empty_match(self) -> None:
        orchestrator = MatchOrchestrator(data_repository=None)
        result = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            now=NOW,
        )
        assert result.overall_score == 0.0
        assert result.requirement_matches == ()

    def test_no_evidence_in_repo_no_strong_match(self) -> None:
        """Critical invariant: no evidence in repository = no strong match."""
        repo = InMemoryMatchDataRepo(
            jobs={JOB_ID: {"skills": ["Python", "Go"], "description_text": "Remote."}},
            evidence={},
        )
        orchestrator = MatchOrchestrator(data_repository=repo)
        result = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            now=NOW,
        )
        for m in result.requirement_matches:
            assert m.level != MatchLevel.STRONG
        assert result.tier != MatchTier.APPLY_NOW

    def test_geographic_block_via_repository(self) -> None:
        """Geographic hard gate blocks apply_now even with perfect evidence."""
        evidence_item = EvidenceItem(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Python",
            verified=True,
        )
        repo = InMemoryMatchDataRepo(
            jobs={JOB_ID: {"skills": ["Python"], "description_text": "US only. Must be in US."}},
            evidence={CANDIDATE_ID: [evidence_item]},
        )
        orchestrator = MatchOrchestrator(data_repository=repo)
        result = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            now=NOW,
        )
        assert result.tier == MatchTier.BLOCKED
        assert result.geographic_blocked is True

    def test_frozen_inputs_stable_via_repository(self) -> None:
        """Same repository data produces identical hashes across calls."""
        evidence_item = EvidenceItem(
            id=UUID("44444444-4444-4444-4444-444444444444"),
            candidate_id=CANDIDATE_ID,
            kind=EvidenceKind.SKILL,
            name="Docker",
            verified=True,
        )
        repo = InMemoryMatchDataRepo(
            jobs={JOB_ID: {"skills": ["Docker"], "description_text": "Remote friendly."}},
            evidence={CANDIDATE_ID: [evidence_item]},
        )
        orchestrator = MatchOrchestrator(data_repository=repo)
        r1 = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID, now=NOW
        )
        r2 = orchestrator.run_match_for_request(
            candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID, now=NOW
        )
        assert r1.input_hash == r2.input_hash
        assert r1.output_hash == r2.output_hash
        assert r1.tier == r2.tier
        assert r1.overall_score == r2.overall_score
