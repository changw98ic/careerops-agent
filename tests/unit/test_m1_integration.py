"""Unit tests for M1 domain models, ingestion, dedup, adapters, and purge."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseAdapter,
    JsonLdAdapter,
    LeverAdapter,
    SitemapAdapter,
    StaticHtmlAdapter,
)
from careerops.application.crawl_policy import check_ssrf, evaluate_crawl_policy
from careerops.application.job_ingestion import (
    JobIngestionService,
    compute_content_hash,
    compute_fingerprint,
    normalize_title,
)
from careerops.application.raw_document_purge import (
    PurgeCandidate,
    RawDocumentPurgeService,
    build_evidence_snippet,
    compute_snippet_hash,
)
from careerops.domain.crawl import (
    CrawlDecision,
    CrawlPolicyInput,
    PurgeState,
)
from careerops.domain.jobs import (
    AggregateState,
    CanonicalJob,
    JobPosting,
    JobPostingVersion,
    PostingSourceState,
)
from careerops.evaluation.dedup_evaluator import (
    FROZEN_ASHBY_RESPONSE,
    FROZEN_CLOSE_REOPEN_DATASET,
    FROZEN_DEDUP_DATASET,
    FROZEN_GREENHOUSE_RESPONSE,
    FROZEN_JSON_LD_HTML,
    FROZEN_LEVER_RESPONSE,
    FROZEN_SITEMAP_XML,
    FROZEN_STATIC_HTML,
    CloseReopenCase,
    DedupPair,
    DedupPrediction,
    PostingState,
    evaluate_close_reopen,
    evaluate_dedup,
)

# --- Domain model tests ---


class TestNormalizeTitle:
    def test_basic_normalization(self) -> None:
        assert normalize_title("Senior Software Engineer") == "senior software engineer"

    def test_strips_punctuation(self) -> None:
        assert normalize_title("Sr. Engineer (Remote)") == "sr engineer remote"

    def test_collapses_whitespace(self) -> None:
        assert normalize_title("  Data   Scientist  ") == "data scientist"

    def test_unicode_normalization(self) -> None:
        result = normalize_title("Engineer\u00a0\u2003Role")
        assert "  " not in result

    def test_empty_string(self) -> None:
        assert normalize_title("") == ""


class TestComputeContentHash:
    def test_deterministic(self) -> None:
        data: dict[str, object] = {"title": "Engineer", "location": "Remote"}
        h1 = compute_content_hash(data)
        h2 = compute_content_hash(data)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_data_different_hash(self) -> None:
        h1 = compute_content_hash({"title": "A"})
        h2 = compute_content_hash({"title": "B"})
        assert h1 != h2

    def test_key_order_independent(self) -> None:
        h1 = compute_content_hash({"a": 1, "b": 2})
        h2 = compute_content_hash({"b": 2, "a": 1})
        assert h1 == h2


class TestComputeFingerprint:
    def test_deterministic(self) -> None:
        fp1 = compute_fingerprint("Engineer", "Remote", "Acme")
        fp2 = compute_fingerprint("Engineer", "Remote", "Acme")
        assert fp1 == fp2

    def test_case_insensitive(self) -> None:
        fp1 = compute_fingerprint("Engineer", "Remote", "Acme")
        fp2 = compute_fingerprint("engineer", "remote", "acme")
        assert fp1 == fp2

    def test_different_inputs(self) -> None:
        fp1 = compute_fingerprint("Engineer", "Remote", "Acme")
        fp2 = compute_fingerprint("Designer", "Remote", "Acme")
        assert fp1 != fp2


# --- Crawl policy tests ---


class TestCrawlPolicy:
    def test_blocked_terms_denied(self) -> None:
        result = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="https://example.com/jobs",
                terms_status="blocked",
                domain="example.com",
            )
        )
        assert result.decision == CrawlDecision.DENY_BLOCKED

    def test_rate_limited_denied(self) -> None:
        result = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="https://example.com/jobs",
                terms_status="allowed",
                domain="example.com",
                is_rate_limited=True,
            )
        )
        assert result.decision == CrawlDecision.DENY_RATE_LIMITED

    def test_unknown_terms_denied(self) -> None:
        result = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="https://example.com/jobs",
                terms_status="unknown",
                domain="example.com",
            )
        )
        assert result.decision == CrawlDecision.DENY_TERMS_UNKNOWN

    def test_allowed_terms_passes(self) -> None:
        result = evaluate_crawl_policy(
            CrawlPolicyInput(
                source_url="https://example.com/jobs",
                terms_status="allowed",
                domain="example.com",
            )
        )
        assert result.decision == CrawlDecision.ALLOW


class TestSSRFCheck:
    def test_private_ip_blocked(self) -> None:
        result = check_ssrf("http://192.168.1.1/jobs")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_loopback_blocked(self) -> None:
        result = check_ssrf("http://127.0.0.1/jobs")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_metadata_endpoint_blocked(self) -> None:
        result = check_ssrf("http://169.254.169.254/latest/meta-data")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_localhost_blocked(self) -> None:
        result = check_ssrf("http://localhost/jobs")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_public_url_allowed(self) -> None:
        result = check_ssrf("https://boards.greenhouse.io/techjobs")
        assert result is None

    def test_non_http_scheme_blocked(self) -> None:
        result = check_ssrf("ftp://example.com/file")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_link_local_blocked(self) -> None:
        result = check_ssrf("http://169.254.1.1/data")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF

    def test_ipv6_loopback_blocked(self) -> None:
        result = check_ssrf("http://[::1]/jobs")
        assert result is not None
        assert result.decision == CrawlDecision.DENY_SSRF


# --- Adapter tests with frozen fixtures ---


class TestGreenhouseAdapter:
    def test_detect(self) -> None:
        adapter = GreenhouseAdapter()
        assert adapter.detect("https://boards.greenhouse.io/techjobs")
        assert not adapter.detect("https://example.com/careers")

    def test_list_jobs_frozen(self) -> None:
        adapter = GreenhouseAdapter()
        result = adapter.list_jobs(FROZEN_GREENHOUSE_RESPONSE)
        assert len(result.jobs) == 2
        assert result.jobs[0].external_id == "12345"
        assert result.jobs[0].title == "Senior Software Engineer"
        assert result.jobs[0].location == "San Francisco, CA"
        assert result.jobs[1].title == "Product Manager"
        assert result.response_hash != ""
        assert result.parser_version == "greenhouse-v1"

    def test_invalid_data(self) -> None:
        adapter = GreenhouseAdapter()
        result = adapter.list_jobs("not a dict")
        assert len(result.jobs) == 0


class TestLeverAdapter:
    def test_detect(self) -> None:
        adapter = LeverAdapter()
        assert adapter.detect("https://jobs.lever.co/company")
        assert not adapter.detect("https://example.com")

    def test_list_jobs_frozen(self) -> None:
        adapter = LeverAdapter()
        result = adapter.list_jobs(FROZEN_LEVER_RESPONSE)
        assert len(result.jobs) == 2
        assert result.jobs[0].external_id == "lev-001"
        assert result.jobs[0].title == "Backend Engineer"
        assert result.jobs[0].location == "Berlin, Germany"
        assert result.jobs[1].location == "Remote"

    def test_invalid_data(self) -> None:
        adapter = LeverAdapter()
        result = adapter.list_jobs({"not": "a list"})
        assert len(result.jobs) == 0


class TestAshbyAdapter:
    def test_detect(self) -> None:
        adapter = AshbyAdapter()
        assert adapter.detect("https://jobs.ashbyhq.com/company")
        assert not adapter.detect("https://example.com")

    def test_list_jobs_frozen(self) -> None:
        adapter = AshbyAdapter()
        result = adapter.list_jobs(FROZEN_ASHBY_RESPONSE)
        assert len(result.jobs) == 1
        assert result.jobs[0].external_id == "ash-001"
        assert result.jobs[0].title == "Data Scientist"
        assert result.jobs[0].location == "London, UK"


class TestJsonLdAdapter:
    def test_list_jobs_frozen(self) -> None:
        adapter = JsonLdAdapter()
        result = adapter.list_jobs(FROZEN_JSON_LD_HTML)
        assert len(result.jobs) == 1
        assert result.jobs[0].title == "DevOps Engineer"
        assert result.jobs[0].location == "Chengdu"
        assert result.jobs[0].url == "https://testcorp.com/careers/devops"

    def test_no_json_ld(self) -> None:
        adapter = JsonLdAdapter()
        result = adapter.list_jobs("<html><body>No jobs</body></html>")
        assert len(result.jobs) == 0

    def test_non_jobposting_type_ignored(self) -> None:
        html = '<script type="application/ld+json">{"@type": "Organization"}</script>'
        adapter = JsonLdAdapter()
        result = adapter.list_jobs(html)
        assert len(result.jobs) == 0


class TestSitemapAdapter:
    def test_detect(self) -> None:
        adapter = SitemapAdapter()
        assert adapter.detect("https://example.com/sitemap.xml")
        assert not adapter.detect("https://example.com/jobs")

    def test_list_jobs_frozen(self) -> None:
        adapter = SitemapAdapter()
        result = adapter.list_jobs(FROZEN_SITEMAP_XML)
        assert len(result.jobs) == 2
        urls = [j.url for j in result.jobs]
        assert "https://company.com/jobs/engineer" in urls
        assert "https://company.com/jobs/designer" in urls
        assert "https://company.com/about" not in urls

    def test_xxe_rejected(self) -> None:
        adapter = SitemapAdapter()
        xxe = '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><urlset></urlset>'
        result = adapter.list_jobs(xxe)
        assert len(result.jobs) == 0

    def test_invalid_xml(self) -> None:
        adapter = SitemapAdapter()
        result = adapter.list_jobs("not xml at all")
        assert len(result.jobs) == 0


class TestStaticHtmlAdapter:
    def test_list_jobs_frozen(self) -> None:
        adapter = StaticHtmlAdapter()
        result = adapter.list_jobs(FROZEN_STATIC_HTML)
        assert len(result.jobs) == 1
        assert result.jobs[0].title == "QA Engineer"
        assert result.jobs[0].location == "Tokyo, Japan"

    def test_no_jobs(self) -> None:
        adapter = StaticHtmlAdapter()
        result = adapter.list_jobs("<html><body>No listings</body></html>")
        assert len(result.jobs) == 0


# --- Dedup evaluator tests ---


class TestDedupEvaluator:
    def test_perfect_predictions(self) -> None:
        pairs = [
            DedupPair(posting_a_id="a", posting_b_id="b", is_duplicate=True),
            DedupPair(posting_a_id="c", posting_b_id="d", is_duplicate=False),
        ]
        predictions = [
            DedupPrediction(posting_a_id="a", posting_b_id="b", score=1.0, rule="exact"),
        ]
        metrics = evaluate_dedup(pairs, predictions)
        assert metrics.precision == 1.0
        assert metrics.recall == 1.0
        assert metrics.f1 == 1.0
        assert metrics.true_positives == 1
        assert metrics.false_positives == 0
        assert metrics.false_negatives == 0

    def test_false_positive(self) -> None:
        pairs = [
            DedupPair(posting_a_id="a", posting_b_id="b", is_duplicate=False),
        ]
        predictions = [
            DedupPrediction(posting_a_id="a", posting_b_id="b", score=0.9, rule="fuzzy"),
        ]
        metrics = evaluate_dedup(pairs, predictions)
        assert metrics.precision == 0.0
        assert metrics.false_positives == 1

    def test_false_negative(self) -> None:
        pairs = [
            DedupPair(posting_a_id="a", posting_b_id="b", is_duplicate=True),
        ]
        predictions: list[DedupPrediction] = []
        metrics = evaluate_dedup(pairs, predictions)
        assert metrics.recall == 0.0
        assert metrics.false_negatives == 1

    def test_empty_dataset(self) -> None:
        metrics = evaluate_dedup([], [])
        assert metrics.precision == 0.0
        assert metrics.recall == 0.0
        assert metrics.f1 == 0.0

    def test_frozen_dataset_runs(self) -> None:
        predictions = [
            DedupPrediction(
                posting_a_id="gh-12345",
                posting_b_id="gh-12345-alias",
                score=1.0,
                rule="url_match",
            ),
        ]
        metrics = evaluate_dedup(FROZEN_DEDUP_DATASET, predictions)
        assert metrics.true_positives == 1
        assert metrics.false_positives == 0


class TestCloseReopenEvaluator:
    def test_frozen_dataset_all_pass(self) -> None:
        results = evaluate_close_reopen(FROZEN_CLOSE_REOPEN_DATASET)
        assert all(results.values())
        assert len(results) == 4

    def test_one_closed_one_active(self) -> None:
        cases = [
            CloseReopenCase(
                case_id="test",
                postings=(
                    PostingState(posting_id="p1", source_state="closed"),
                    PostingState(posting_id="p2", source_state="active"),
                ),
                expected_canonical_state="active",
            )
        ]
        results = evaluate_close_reopen(cases)
        assert results["test"] is True

    def test_all_closed_means_closed(self) -> None:
        cases = [
            CloseReopenCase(
                case_id="test",
                postings=(
                    PostingState(posting_id="p1", source_state="closed"),
                    PostingState(posting_id="p2", source_state="closed"),
                ),
                expected_canonical_state="closed",
            )
        ]
        results = evaluate_close_reopen(cases)
        assert results["test"] is True


# --- Raw document purge tests ---


class TestRawDocumentPurge:
    def _make_candidate(self) -> PurgeCandidate:
        return PurgeCandidate(
            content_object_id=uuid4(),
            blob_id=uuid4(),
            object_key="sha256/ab/cd/" + "a" * 64,
            sha256="a" * 64,
            source_url="https://example.com/jobs/1",
            fetched_at=datetime(2026, 1, 15, tzinfo=UTC),
            decision_snippet="Senior Engineer role at Example Corp",
        )

    def test_compute_snippet_hash(self) -> None:
        h = compute_snippet_hash("test snippet")
        assert len(h) == 64
        assert h == compute_snippet_hash("test snippet")

    def test_build_evidence_snippet(self) -> None:
        candidate = self._make_candidate()
        snippet = build_evidence_snippet(candidate)
        assert snippet.source_url == "https://example.com/jobs/1"
        assert snippet.content_hash == "a" * 64
        assert snippet.decision_snippet == "Senior Engineer role at Example Corp"
        assert len(snippet.snippet_hash) == 64

    def test_purge_with_no_dangling_refs(self) -> None:
        from careerops.domain.crawl import EvidenceSnippet

        class MockRepo:
            def persist_evidence_snippet(self, snippet: EvidenceSnippet) -> UUID:
                return uuid4()

            def count_version_references(self, blob_id: UUID) -> int:
                return 0

            def mark_content_purged(self, content_object_id: UUID, blob_id: UUID) -> None:
                pass

        service = RawDocumentPurgeService(MockRepo())
        candidate = self._make_candidate()
        result = service.purge_document(candidate)
        assert result.state == PurgeState.PURGED
        assert result.evidence_persisted is True
        assert result.dangling_references == 0

    def test_purge_blocked_by_dangling_refs(self) -> None:
        from careerops.domain.crawl import EvidenceSnippet

        class MockRepo:
            def persist_evidence_snippet(self, snippet: EvidenceSnippet) -> UUID:
                return uuid4()

            def count_version_references(self, blob_id: UUID) -> int:
                return 3

            def mark_content_purged(self, content_object_id: UUID, blob_id: UUID) -> None:
                pass

        service = RawDocumentPurgeService(MockRepo())
        candidate = self._make_candidate()
        result = service.purge_document(candidate)
        assert result.state == PurgeState.FAILED
        assert result.evidence_persisted is False
        assert result.dangling_references == 3

    def test_purge_batch(self) -> None:
        from careerops.domain.crawl import EvidenceSnippet

        class MockRepo:
            def persist_evidence_snippet(self, snippet: EvidenceSnippet) -> UUID:
                return uuid4()

            def count_version_references(self, blob_id: UUID) -> int:
                return 0

            def mark_content_purged(self, content_object_id: UUID, blob_id: UUID) -> None:
                pass

        service = RawDocumentPurgeService(MockRepo())
        candidates = [self._make_candidate() for _ in range(3)]
        results = service.purge_batch(candidates)
        assert len(results) == 3
        assert all(r.state == PurgeState.PURGED for r in results)


# --- Job ingestion service tests ---


class InMemoryJobRepository:
    """In-memory repository for testing job ingestion."""

    def __init__(self) -> None:
        self.postings: dict[object, JobPosting] = {}
        self.versions: dict[object, JobPostingVersion] = {}
        self.canonical_jobs: dict[object, CanonicalJob] = {}
        self._posting_by_source_ext: dict[tuple[object, str], JobPosting] = {}
        self._versions_by_posting: dict[object, list[JobPostingVersion]] = {}
        self._postings_by_canonical: dict[object, list[JobPosting]] = {}
        self._canonical_by_posting: dict[object, UUID] = {}
        self._fingerprint_by_canonical: dict[str, object] = {}

    def find_posting_by_source_and_external_id(
        self, source_id: object, external_id: str
    ) -> JobPosting | None:
        return self._posting_by_source_ext.get((source_id, external_id))

    def find_latest_version(self, posting_id: object) -> JobPostingVersion | None:
        versions = self._versions_by_posting.get(posting_id, [])
        return versions[-1] if versions else None

    def find_canonical_by_fingerprint(self, fingerprint: str) -> CanonicalJob | None:
        canonical_id = self._fingerprint_by_canonical.get(fingerprint)
        if canonical_id is None:
            return None
        return self.canonical_jobs.get(canonical_id)

    def find_canonical_by_id(self, canonical_job_id: object) -> CanonicalJob | None:
        return self.canonical_jobs.get(canonical_job_id)

    def find_canonical_for_posting(self, posting_id: object) -> UUID | None:
        return self._canonical_by_posting.get(posting_id)

    def find_postings_for_canonical(self, canonical_job_id: object) -> list[JobPosting]:
        return self._postings_by_canonical.get(canonical_job_id, [])

    def save_posting(self, posting: JobPosting) -> None:
        self.postings[posting.id] = posting
        self._posting_by_source_ext[(posting.source_id, posting.external_id)] = posting

    def save_version(self, version: JobPostingVersion) -> None:
        self.versions[version.id] = version
        self._versions_by_posting.setdefault(version.job_posting_id, []).append(version)

    def save_canonical_job(self, job: CanonicalJob) -> None:
        self.canonical_jobs[job.id] = job

    def save_merge_decision(self, decision: object) -> None:
        pass

    def save_posting_assignment(
        self, posting_id: object, canonical_job_id: object, decision_id: object
    ) -> None:
        self._canonical_by_posting[posting_id] = canonical_job_id  # pyright: ignore[reportArgumentType]
        posting = self.postings.get(posting_id)
        if posting is not None:
            self._postings_by_canonical.setdefault(canonical_job_id, []).append(posting)

    def update_posting_state(
        self, posting_id: object, state: PostingSourceState, now: datetime
    ) -> None:
        posting = self.postings.get(posting_id)
        if posting:
            updated = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=posting.last_seen_at,
                closed_at=now if state == PostingSourceState.CLOSED else None,
            )
            self.postings[posting_id] = updated
            canonical_id = self._canonical_by_posting.get(posting_id)
            if canonical_id is not None:
                postings = self._postings_by_canonical.get(canonical_id, [])
                self._postings_by_canonical[canonical_id] = [
                    updated if p.id == posting_id else p for p in postings
                ]

    def update_canonical_state(
        self, canonical_job_id: object, state: AggregateState, now: datetime
    ) -> None:
        job = self.canonical_jobs.get(canonical_job_id)
        if job:
            self.canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=state,
                primary_posting_id=job.primary_posting_id,
            )

    def update_posting_last_seen(self, posting_id: object, now: datetime) -> None:
        posting = self.postings.get(posting_id)
        if posting:
            self.postings[posting_id] = JobPosting(
                id=posting.id,
                source_id=posting.source_id,
                external_id=posting.external_id,
                canonical_url=posting.canonical_url,
                source_state=posting.source_state,
                first_seen_at=posting.first_seen_at,
                last_seen_at=now,
            )

    def update_canonical_primary_posting(
        self, canonical_job_id: object, posting_id: object
    ) -> None:
        job = self.canonical_jobs.get(canonical_job_id)
        if job:
            self.canonical_jobs[canonical_job_id] = CanonicalJob(
                id=job.id,
                company_id=job.company_id,
                canonical_title=job.canonical_title,
                normalized_title=job.normalized_title,
                aggregate_state=job.aggregate_state,
                primary_posting_id=UUID(str(posting_id)),
            )


class TestJobIngestionService:
    def _make_service(self) -> tuple[JobIngestionService, InMemoryJobRepository]:
        repo = InMemoryJobRepository()
        return JobIngestionService(repo), repo

    def test_new_posting_creates_canonical(self) -> None:
        service, _repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        result = service.ingest_posting(
            source_id=uuid4(),
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data={"title": "Engineer", "location": "Remote"},
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        assert result.is_new_posting is True
        assert result.is_new_version is True
        assert result.is_new_canonical is True

    def test_same_posting_no_duplicate(self) -> None:
        service, _repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        source_id = uuid4()
        data: dict[str, object] = {"title": "Engineer", "location": "Remote"}

        result1 = service.ingest_posting(
            source_id=source_id,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )

        result2 = service.ingest_posting(
            source_id=source_id,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )

        assert result2.is_new_posting is False
        assert result2.is_new_version is False
        assert result2.posting_id == result1.posting_id

    def test_content_change_creates_new_version(self) -> None:
        service, _repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        source_id = uuid4()

        data1: dict[str, object] = {"title": "Engineer", "location": "Remote"}
        result1 = service.ingest_posting(
            source_id=source_id,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data1,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )

        data2: dict[str, object] = {"title": "Senior Engineer", "location": "Remote"}
        result2 = service.ingest_posting(
            source_id=source_id,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data2,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )

        assert result2.is_new_posting is False
        assert result2.is_new_version is True
        assert result2.posting_id == result1.posting_id
        assert result2.version_id != result1.version_id

    def test_close_one_source_canonical_stays_active(self) -> None:
        """One source closed + another active = canonical job still active."""
        service, repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        source_id_1 = uuid4()
        source_id_2 = uuid4()

        data: dict[str, object] = {"title": "Engineer", "location": "Remote"}

        result1 = service.ingest_posting(
            source_id=source_id_1,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        canonical_id = result1.canonical_job_id

        result2 = service.ingest_posting(
            source_id=source_id_2,
            external_id="ext-2",
            canonical_url="https://example.com/jobs/2",
            structured_data=data,
            source_url="https://example.com/jobs/2",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        repo.save_posting_assignment(result2.posting_id, canonical_id, uuid4())

        service.close_posting(result1.posting_id, now=now)

        canonical = repo.canonical_jobs[canonical_id]
        assert canonical.aggregate_state == AggregateState.ACTIVE

    def test_all_sources_closed_canonical_closed(self) -> None:
        """All sources closed = canonical job closed."""
        service, repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        source_id_1 = uuid4()
        source_id_2 = uuid4()

        data: dict[str, object] = {"title": "Engineer", "location": "Remote"}

        result1 = service.ingest_posting(
            source_id=source_id_1,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        canonical_id = result1.canonical_job_id

        result2 = service.ingest_posting(
            source_id=source_id_2,
            external_id="ext-2",
            canonical_url="https://example.com/jobs/2",
            structured_data=data,
            source_url="https://example.com/jobs/2",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        repo.save_posting_assignment(result2.posting_id, canonical_id, uuid4())

        service.close_posting(result1.posting_id, now=now)
        service.close_posting(result2.posting_id, now=now)

        canonical = repo.canonical_jobs[canonical_id]
        assert canonical.aggregate_state == AggregateState.CLOSED

    def test_reopen_restores_canonical_active(self) -> None:
        """Reopening a closed posting restores canonical to active."""
        service, repo = self._make_service()
        now = datetime(2026, 1, 15, tzinfo=UTC)
        source_id = uuid4()

        data: dict[str, object] = {"title": "Engineer", "location": "Remote"}
        result = service.ingest_posting(
            source_id=source_id,
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            structured_data=data,
            source_url="https://example.com/jobs/1",
            parser_version="test-v1",
            company_name="Acme",
            now=now,
        )
        canonical_id = result.canonical_job_id

        service.close_posting(result.posting_id, now=now)
        canonical = repo.canonical_jobs[canonical_id]
        assert canonical.aggregate_state == AggregateState.CLOSED

        service.reopen_posting(result.posting_id, now=now)
        canonical = repo.canonical_jobs[canonical_id]
        assert canonical.aggregate_state == AggregateState.ACTIVE


# --- Workflow contracts tests ---


class TestWorkflowContracts:
    def test_m1_contracts_importable(self) -> None:
        from careerops.workflows.m1_contracts import (
            CompanyDiscoveryInput,
            CrawlJobSourceResult,
            RawDocumentPurgeInput,
        )

        inp = CompanyDiscoveryInput(company_id="c1", company_name="Acme")
        assert inp.company_id == "c1"

        result = CrawlJobSourceResult(source_id="s1", postings_crawled=5)
        assert result.postings_crawled == 5

        purge_input = RawDocumentPurgeInput(batch_size=10, dry_run=True)
        assert purge_input.dry_run is True

    def test_m1_workflows_importable(self) -> None:
        from careerops.workflows.m1_workflows import (
            CompanyDiscoveryWorkflow,
            CrawlJobSourceWorkflow,
            RawDocumentPurgeWorkflow,
        )

        assert CompanyDiscoveryWorkflow is not None
        assert CrawlJobSourceWorkflow is not None
        assert RawDocumentPurgeWorkflow is not None

    def test_crawl_job_source_result_defaults(self) -> None:
        from careerops.workflows.m1_contracts import CrawlJobSourceResult

        result = CrawlJobSourceResult(source_id="s1")
        assert result.postings_crawled == 0
        assert result.postings_ingested == 0
        assert result.new_postings == 0
        assert result.new_versions == 0
        assert result.errors == ()

    def test_raw_document_purge_result_defaults(self) -> None:
        from careerops.workflows.m1_contracts import RawDocumentPurgeResult

        result = RawDocumentPurgeResult()
        assert result.documents_purged == 0
        assert result.evidence_snippets_persisted == 0
        assert result.dangling_references_found == 0
        assert result.errors == ()
        assert result.dry_run is False

    def test_crawled_posting_record(self) -> None:
        from careerops.workflows.m1_contracts import CrawledPostingRecord

        record = CrawledPostingRecord(
            source_id="src-1",
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            source_url="https://example.com/jobs/1",
            structured_data={"title": "Engineer"},
            parser_version="test-v1",
        )
        assert record.external_id == "ext-1"
        assert record.structured_data == {"title": "Engineer"}

    def test_discovered_source_record(self) -> None:
        from careerops.workflows.m1_contracts import DiscoveredSourceRecord

        record = DiscoveredSourceRecord(
            source_type="greenhouse",
            source_identifier="techjobs",
            base_url="https://boards.greenhouse.io/techjobs",
        )
        assert record.source_type == "greenhouse"

    def test_company_discovery_result(self) -> None:
        from careerops.workflows.m1_contracts import CompanyDiscoveryResult

        result = CompanyDiscoveryResult(company_id="c1", sources_registered=2)
        assert result.sources_registered == 2
        assert result.discovered_sources == ()

    def test_purged_document_record(self) -> None:
        from careerops.workflows.m1_contracts import PurgedDocumentRecord

        record = PurgedDocumentRecord(
            content_object_id="co-1",
            blob_id="b-1",
            evidence_persisted=True,
            dangling_references=0,
        )
        assert record.evidence_persisted is True


# --- M1 Activities tests ---


class TestM1Activities:
    def test_activities_importable(self) -> None:
        from careerops.infrastructure.temporal.m1_activities import (
            M1CrawlActivities,
            M1DiscoveryActivities,
            M1PurgeActivities,
        )

        assert M1DiscoveryActivities is not None
        assert M1CrawlActivities is not None
        assert M1PurgeActivities is not None

    def test_noop_discovery_sink(self) -> None:
        import asyncio

        from careerops.infrastructure.temporal.m1_activities import NoOpDiscoverySink
        from careerops.workflows.m1_contracts import CompanyDiscoveryInput

        sink = NoOpDiscoverySink()
        request = CompanyDiscoveryInput(company_id="c1", company_name="Acme")
        result = asyncio.run(sink.discover_sources(request))
        assert result.company_id == "c1"
        assert result.discovered_sources == ()

    def test_noop_crawl_sink(self) -> None:
        import asyncio

        from careerops.infrastructure.temporal.m1_activities import NoOpCrawlSink
        from careerops.workflows.m1_contracts import CrawledPostingRecord, CrawlJobSourceInput

        sink = NoOpCrawlSink()
        request = CrawlJobSourceInput(
            source_id="s1",
            company_id="c1",
            company_name="Acme",
            source_type="greenhouse",
            base_url="https://boards.greenhouse.io/techjobs",
        )
        result = asyncio.run(sink.crawl_source(request))
        assert result == []

        record = CrawledPostingRecord(
            source_id="src-1",
            external_id="ext-1",
            canonical_url="https://example.com/jobs/1",
            source_url="https://example.com/jobs/1",
            structured_data={"title": "Engineer"},
            parser_version="test-v1",
        )
        ingest_result = asyncio.run(sink.ingest_posting(record))
        assert ingest_result["is_new_posting"] is True
        assert ingest_result["is_new_version"] is True

    def test_noop_purge_sink(self) -> None:
        import asyncio

        from careerops.infrastructure.temporal.m1_activities import NoOpPurgeSink
        from careerops.workflows.m1_contracts import RawDocumentPurgeInput

        sink = NoOpPurgeSink()
        request = RawDocumentPurgeInput(batch_size=10, dry_run=False)
        result = asyncio.run(sink.purge_documents(request))
        assert result.documents_purged == 0

    def test_m1_discovery_activities_with_sink(self) -> None:
        from careerops.infrastructure.temporal.m1_activities import M1DiscoveryActivities

        activities = M1DiscoveryActivities()
        assert activities._sink is not None

    def test_m1_crawl_activities_with_sink(self) -> None:
        from careerops.infrastructure.temporal.m1_activities import M1CrawlActivities

        activities = M1CrawlActivities()
        assert activities._sink is not None

    def test_m1_purge_activities_with_sink(self) -> None:
        from careerops.infrastructure.temporal.m1_activities import M1PurgeActivities

        activities = M1PurgeActivities()
        assert activities._sink is not None


# --- API routes tests ---


class TestJobsApiRoutes:
    def test_routes_importable(self) -> None:
        from careerops.api.routes.jobs import router

        assert router is not None
        assert router.prefix == "/api/v1"

    def test_response_models(self) -> None:
        from careerops.api.routes.jobs import (
            CanonicalJobResponse,
            CompanyResponse,
        )

        company = CompanyResponse(id="c1", name="Acme", normalized_name="acme")
        assert company.terms_status == "unknown"

        job = CanonicalJobResponse(id="j1", company_id="c1", canonical_title="Engineer")
        assert job.aggregate_state == "active"

    def test_list_companies_no_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/api/v1/companies")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_jobs_no_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_job_detail_no_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        response = client.get("/api/v1/jobs/nonexistent")
        assert response.status_code == 503
        data = response.json()
        assert data["error"] == "not_available"

    def test_list_companies_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        class MockRepo:
            def list_companies(self, cursor: str | None, limit: int) -> list[dict[str, object]]:
                return [
                    {
                        "id": "c1",
                        "name": "Acme",
                        "normalized_name": "acme",
                        "official_domains": ["acme.com"],
                        "terms_status": "allowed",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ]

        app = FastAPI()
        app.include_router(router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/api/v1/companies")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Acme"

    def test_list_jobs_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        class MockRepo:
            def list_canonical_jobs(
                self, cursor: str | None, limit: int, state: str | None
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": "j1",
                        "company_id": "c1",
                        "canonical_title": "Engineer",
                        "aggregate_state": "active",
                    }
                ]

        app = FastAPI()
        app.include_router(router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["canonical_title"] == "Engineer"

    def test_get_job_detail_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        class MockRepo:
            def get_job_detail(self, job_id: str) -> dict[str, object] | None:
                if job_id == "j1":
                    return {
                        "id": "j1",
                        "company_id": "c1",
                        "canonical_title": "Engineer",
                        "aggregate_state": "active",
                        "versions": [{"content_hash": "abc123"}],
                        "merge_decisions": [],
                    }
                return None

        app = FastAPI()
        app.include_router(router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)

        response = client.get("/api/v1/jobs/j1")
        assert response.status_code == 200
        data = response.json()
        assert data["canonical_job"]["canonical_title"] == "Engineer"
        assert len(data["versions"]) == 1

        response = client.get("/api/v1/jobs/nonexistent")
        assert response.status_code == 404

    def test_list_jobs_with_state_filter(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.api.routes.jobs import router

        class MockRepo:
            def list_canonical_jobs(
                self, cursor: str | None, limit: int, state: str | None
            ) -> list[dict[str, object]]:
                if state == "closed":
                    return []
                return [
                    {
                        "id": "j1",
                        "company_id": "c1",
                        "canonical_title": "Engineer",
                        "aggregate_state": "active",
                    }
                ]

        app = FastAPI()
        app.include_router(router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/api/v1/jobs?state=closed")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0


# --- Web UI tests ---


class TestJobsUi:
    def test_ui_importable(self) -> None:
        from careerops.web.jobs_ui import web_router

        assert web_router is not None

    def test_companies_page_no_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        app = FastAPI()
        app.include_router(web_router)
        client = TestClient(app)
        response = client.get("/companies")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_jobs_inbox_page_no_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        app = FastAPI()
        app.include_router(web_router)
        client = TestClient(app)
        response = client.get("/jobs")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_job_detail_page_not_found(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        app = FastAPI()
        app.include_router(web_router)
        client = TestClient(app)
        response = client.get("/jobs/nonexistent")
        assert response.status_code == 404

    def test_companies_page_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        class MockRepo:
            def list_companies(self, cursor: str | None, limit: int) -> list[dict[str, object]]:
                return [{"id": "c1", "name": "Acme", "normalized_name": "acme"}]

        app = FastAPI()
        app.include_router(web_router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/companies")
        assert response.status_code == 200
        assert "Acme" in response.text

    def test_jobs_inbox_page_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        class MockRepo:
            def list_canonical_jobs(
                self, cursor: str | None, limit: int, state: str | None
            ) -> list[dict[str, object]]:
                return [
                    {
                        "id": "j1",
                        "company_id": "c1",
                        "canonical_title": "Engineer",
                        "aggregate_state": "active",
                    }
                ]

        app = FastAPI()
        app.include_router(web_router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/jobs")
        assert response.status_code == 200
        assert "Engineer" in response.text

    def test_job_detail_page_with_repo(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from careerops.web.jobs_ui import web_router

        class MockRepo:
            def get_job_detail(self, job_id: str) -> dict[str, object] | None:
                if job_id == "j1":
                    return {
                        "id": "j1",
                        "company_id": "c1",
                        "canonical_title": "Engineer",
                        "aggregate_state": "active",
                        "versions": [],
                        "merge_decisions": [],
                    }
                return None

        app = FastAPI()
        app.include_router(web_router)
        app.state.job_read_repository = MockRepo()
        client = TestClient(app)
        response = client.get("/jobs/j1")
        assert response.status_code == 200
        assert "Engineer" in response.text
