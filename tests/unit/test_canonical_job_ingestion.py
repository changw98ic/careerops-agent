from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import uuid4

import pytest

from careerops.application.canonical_job_ingestion import (
    CanonicalJobDedupePolicy,
    CanonicalJobIngestionRecord,
    CanonicalJobIngestionResult,
    CanonicalJobIngestionService,
    CanonicalJobIngestionStore,
    CrawlerRunProvenance,
    PublicAtsJobRow,
    normalize_url,
    public_ats_row_from_mapping,
)
from careerops.infrastructure.database.canonical_job_ingestion import (
    compile_query_for_test,
    upsert_public_ats_core_statements,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


class CapturingStore(CanonicalJobIngestionStore):
    def __init__(self) -> None:
        self.records: list[CanonicalJobIngestionRecord] = []

    def ingest_public_ats_job(
        self,
        record: CanonicalJobIngestionRecord,
    ) -> CanonicalJobIngestionResult:
        self.records.append(record)
        return CanonicalJobIngestionResult(
            company_id=uuid4(),
            source_id=uuid4(),
            posting_id=uuid4(),
            version_id=uuid4(),
            canonical_job_id=uuid4(),
            merge_decision_id=uuid4(),
            evidence_id=uuid4(),
            dedupe_key=record.dedupe_key,
            inserted_version=True,
        )


def make_provenance() -> CrawlerRunProvenance:
    return CrawlerRunProvenance(
        crawler_source_row_id=uuid4(),
        crawler_run_id=uuid4(),
        run_event_id=uuid4(),
        registry_id=uuid4(),
        source_id="public-ats",
        adapter="public_ats",
        output_artifact_sha256=DIGEST,
    )


def make_row() -> PublicAtsJobRow:
    return PublicAtsJobRow(
        company_name="Example AI",
        company_domain="example.ai",
        source_type="public_ats",
        source_identifier="greenhouse:example",
        base_url="https://boards.greenhouse.io/example",
        external_id="12345",
        canonical_url="https://boards.greenhouse.io/example/jobs/12345/",
        title="Senior Agent Engineer",
        location="San Francisco, CA",
        department="Engineering",
        captured_at=NOW,
        parser_version="public-ats-jsonl:v1",
        source_url="https://boards.greenhouse.io/example/jobs/12345/",
        structured_data=MappingProxyType({"employment_type": "full-time"}),
    )


def test_service_builds_deterministic_record_without_provider_or_network_actions() -> None:
    store = CapturingStore()
    service = CanonicalJobIngestionService(store)
    row = make_row()
    provenance = make_provenance()

    result = service.ingest_public_ats_job(
        row,
        provenance=provenance,
        dedupe_policy=CanonicalJobDedupePolicy.HYBRID,
    )

    assert result.dedupe_key == store.records[0].dedupe_key
    assert store.records[0].dedupe_rule == "deterministic:hybrid:v1"
    assert store.records[0].evidence.output_artifact_sha256 == DIGEST
    assert store.records[0].structured_payload["source_adapter"] == "public_ats"


def test_dedupe_policies_are_deterministic_and_semantic_free() -> None:
    row = make_row()
    provenance = make_provenance()
    hybrid = CanonicalJobIngestionRecord(
        provenance=provenance,
        row=row,
        dedupe_policy=CanonicalJobDedupePolicy.HYBRID,
    )
    url = CanonicalJobIngestionRecord(
        provenance=provenance,
        row=row,
        dedupe_policy=CanonicalJobDedupePolicy.URL,
    )
    exact = CanonicalJobIngestionRecord(
        provenance=provenance,
        row=row,
        dedupe_policy=CanonicalJobDedupePolicy.HASH,
    )

    assert hybrid.dedupe_key == hybrid.dedupe_key
    assert len({hybrid.dedupe_key, url.dedupe_key, exact.dedupe_key}) == 3
    assert "semantic" not in hybrid.dedupe_rule
    assert normalize_url("HTTPS://Boards.Greenhouse.IO/example/jobs/12345/") == (
        "https://boards.greenhouse.io/example/jobs/12345"
    )

    changed_row = replace(row, title="Principal Agent Engineer")
    changed_hybrid = CanonicalJobIngestionRecord(
        provenance=provenance,
        row=changed_row,
        dedupe_policy=CanonicalJobDedupePolicy.HYBRID,
    )
    assert changed_hybrid.dedupe_key == hybrid.dedupe_key
    assert changed_row.effective_content_hash != row.effective_content_hash


def test_public_ats_mapping_accepts_existing_jsonl_shape() -> None:
    row = public_ats_row_from_mapping(
        {
            "schema_version": 2,
            "canonical_url": "https://jobs.ashbyhq.com/acme/abc",
            "source_record_id": "ashby-acme-abc",
            "source_feed_ats": "ashby",
            "source_feed_token": "acme",
            "source_job_id": "abc",
            "source_job_title": "Agent Infrastructure Engineer",
            "source_job_location": "Remote",
            "source_job_locations": ["Remote", "New York, NY"],
            "source_job_department": "Platform",
            "source_job_description": "Build reliable Python agent infrastructure.",
            "source_job_apply_url": "https://jobs.ashbyhq.com/acme/abc/application",
            "source_job_keywords": ["Python", "Agents"],
            "source_job_industry": "Software",
            "source_job_industries": ["Software", "Artificial Intelligence"],
            "source_job_employment_type": "FullTime",
            "source_job_work_mode": "Remote",
            "source_job_remote": True,
            "source_job_seniority": "Senior",
            "source_job_salary": {
                "compensation": {"currencyCode": "USD", "minValue": 160000}
            },
            "source_job_authorization": {
                "workAuthorization": {"sponsorship": False}
            },
            "registrable_domain": "acme.com",
            "discovered_at": "2026-07-20T09:00:00Z",
        }
    )

    assert row.company_domain == "acme.com"
    assert row.source_type == "public_ats"
    assert row.source_identifier.startswith("ashby:")
    assert row.external_id == "abc"
    assert row.captured_at == NOW
    assert row.parser_version == "public-ats-jsonl:v2"
    assert row.structured_data["artifact_schema_version"] == 2
    assert row.structured_data["description"] == (
        "Build reliable Python agent infrastructure."
    )
    assert row.structured_data["apply_url"] == (
        "https://jobs.ashbyhq.com/acme/abc/application"
    )
    assert row.structured_data["keywords"] == ["Python", "Agents"]
    assert row.structured_data["industry"] == "Software"
    assert row.structured_data["industries"] == [
        "Software",
        "Artificial Intelligence",
    ]
    assert row.structured_data["locations"] == ["Remote", "New York, NY"]
    assert row.structured_data["employment_type"] == "FullTime"
    assert row.structured_data["work_mode"] == "Remote"
    assert row.structured_data["remote"] is True
    assert row.structured_data["seniority"] == "Senior"
    assert row.structured_data["salary"] == {
        "compensation": {"currencyCode": "USD", "minValue": 160000}
    }
    assert row.structured_data["authorization"] == {
        "workAuthorization": {"sponsorship": False}
    }

    other_provider = public_ats_row_from_mapping(
        {
            "canonical_url": "https://boards.greenhouse.io/acme/jobs/abc",
            "source_feed_ats": "greenhouse",
            "source_feed_token": "acme",
            "source_job_id": "abc",
            "source_job_title": "Agent Infrastructure Engineer",
            "registrable_domain": "acme.com",
            "discovered_at": "2026-07-20T09:00:00Z",
        }
    )
    other_host = public_ats_row_from_mapping(
        {
            "canonical_url": "https://jobs.eu.ashbyhq.com/acme/abc",
            "source_feed_ats": "ashby",
            "source_feed_token": "acme",
            "source_job_id": "abc",
            "source_job_title": "Agent Infrastructure Engineer",
            "registrable_domain": "acme.com",
            "discovered_at": "2026-07-20T09:00:00Z",
        }
    )
    assert other_provider.source_identifier != row.source_identifier
    assert other_host.source_identifier != row.source_identifier
    assert other_provider.parser_version == "public-ats-jsonl:v1"
    assert dict(other_provider.structured_data) == {
        "source_feed_ats": "greenhouse",
        "source_feed_token": "acme",
    }

    with pytest.raises(ValueError, match="schema_version"):
        public_ats_row_from_mapping(
            {
                "schema_version": 3,
                "canonical_url": "https://jobs.ashbyhq.com/acme/abc",
                "source_job_id": "abc",
                "source_job_title": "Agent Infrastructure Engineer",
                "registrable_domain": "acme.com",
                "discovered_at": "2026-07-20T09:00:00Z",
            }
        )

    with pytest.raises(ValueError, match="captured_at"):
        public_ats_row_from_mapping(
            {
                "canonical_url": "https://jobs.ashbyhq.com/acme/abc",
                "source_job_id": "abc",
                "source_job_title": "Agent Infrastructure Engineer",
                "registrable_domain": "acme.com",
            }
        )


def test_public_ats_mapping_rejects_credential_bearing_apply_url() -> None:
    with pytest.raises(ValueError, match="apply_url must not contain credentials"):
        public_ats_row_from_mapping(
            {
                "schema_version": 2,
                "canonical_url": "https://jobs.ashbyhq.com/acme/abc",
                "source_job_id": "abc",
                "source_job_title": "Agent Infrastructure Engineer",
                "source_job_apply_url": "https://user:secret@jobs.ashbyhq.com/acme/abc/apply",
                "registrable_domain": "acme.com",
                "discovered_at": "2026-07-20T09:00:00Z",
            }
        )


def test_row_validation_rejects_local_paths_naive_times_and_bad_hashes() -> None:
    kwargs = {
        "company_name": "Example AI",
        "company_domain": "example.ai",
        "source_type": "public_ats",
        "source_identifier": "greenhouse:example",
        "base_url": "file:///tmp/jobs.jsonl",
        "external_id": "12345",
        "canonical_url": "https://boards.greenhouse.io/example/jobs/12345/",
        "title": "Senior Agent Engineer",
        "location": None,
        "department": None,
        "captured_at": NOW,
        "parser_version": "public-ats-jsonl:v1",
        "source_url": "https://boards.greenhouse.io/example/jobs/12345/",
        "structured_data": MappingProxyType({}),
    }
    with pytest.raises(ValueError, match="base_url"):
        PublicAtsJobRow(**kwargs)
    with pytest.raises(ValueError, match="captured_at"):
        PublicAtsJobRow(
            **{
                **kwargs,
                "base_url": "https://example.ai",
                "captured_at": NOW.replace(tzinfo=None),
            }
        )
    with pytest.raises(ValueError, match="content_hash"):
        PublicAtsJobRow(**{**kwargs, "base_url": "https://example.ai", "content_hash": "bad"})


def test_core_sql_mentions_all_required_canonical_and_evidence_tables() -> None:
    record = CanonicalJobIngestionRecord(
        provenance=make_provenance(),
        row=make_row(),
        dedupe_policy=CanonicalJobDedupePolicy.URL,
    )
    sql = "\n".join(
        compile_query_for_test(statement, literal_binds=False)
        for statement in upsert_public_ats_core_statements(
            record,
            company_id=uuid4(),
            source_id=uuid4(),
            posting_id=uuid4(),
            version_id=uuid4(),
            canonical_job_id=uuid4(),
            merge_decision_id=uuid4(),
            evidence_id=uuid4(),
        )
    )

    assert "INSERT INTO careerops.companies" in sql
    assert "INSERT INTO careerops.job_sources" in sql
    assert "INSERT INTO careerops.job_postings" in sql
    assert "INSERT INTO careerops.job_posting_versions" in sql
    assert "INSERT INTO careerops.canonical_jobs" in sql
    assert "INSERT INTO careerops.crawler_job_deduplication_keys" in sql
    assert "INSERT INTO careerops.job_merge_decisions" in sql
    assert "INSERT INTO careerops.crawler_job_ingestion_evidence" in sql
    assert "rule" in sql
    assert record.dedupe_rule == "deterministic:url:v1"
    assert "semantic" not in sql
