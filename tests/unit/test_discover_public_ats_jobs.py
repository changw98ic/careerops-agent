from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_module() -> ModuleType:
    scripts_dir = Path(__file__).parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script = scripts_dir / "discover_public_ats_jobs.py"
    spec = importlib.util.spec_from_file_location("discover_public_ats_jobs", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _triage_row(
    *,
    record_id: str = "company-one",
    assessment: str = "external_ats_needs_verification",
    registrable_domain: str = "company.test",
    links: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "registrable_domain": registrable_domain,
        "employer_hiring_assessment": assessment,
        "links": links,
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_feed_target_extraction_for_supported_ats_and_dedupes() -> None:
    module = _load_module()

    rows = [
        _triage_row(
            links=[
                {
                    "href": "https://boards.greenhouse.io/acme/jobs/123",
                    "relationship": "known_ats",
                },
                {
                    "href": "https://job-boards.greenhouse.io/acme",
                    "relationship": "known_ats",
                },
                {"href": "https://jobs.ashbyhq.com/omni", "relationship": "known_ats"},
                {"href": "https://jobs.lever.co/helix/456", "relationship": "known_ats"},
                {"href": "https://example.com/careers", "relationship": "first_party"},
                {"href": "https://workdayjobs.com/foo", "relationship": "known_ats"},
            ],
        ),
        _triage_row(
            record_id="ignored",
            assessment="no_recruitment_evidence",
            links=[{"href": "https://jobs.lever.co/ignored", "relationship": "known_ats"}],
        ),
    ]

    targets = module.select_feed_targets(rows)

    assert [(target.ats, target.token, target.canonical_url) for target in targets] == [
        (
            "greenhouse",
            "acme",
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
        ),
        ("ashby", "omni", "https://api.ashbyhq.com/posting-api/job-board/omni"),
        ("lever", "helix", "https://api.lever.co/v0/postings/helix?mode=json"),
    ]
    assert [target.source_record_id for target in targets] == [
        "company-one",
        "company-one",
        "company-one",
    ]


def test_feed_target_extraction_for_smartrecruiters_recruitee_and_workable() -> None:
    module = _load_module()

    rows = [
        _triage_row(
            links=[
                {
                    "href": "https://jobs.smartrecruiters.com/acme",
                    "relationship": "known_ats",
                },
                {
                    "href": "https://anywhereworks.recruitee.com/jobs",
                    "relationship": "known_ats",
                },
                {
                    "href": "https://apply.workable.com/careers/",
                    "relationship": "known_ats",
                },
            ],
        )
    ]

    targets = module.select_feed_targets(rows)

    assert [(target.ats, target.token, target.canonical_url) for target in targets] == [
        (
            "smartrecruiters",
            "acme",
            "https://api.smartrecruiters.com/v1/companies/acme/postings",
        ),
        ("recruitee", "anywhereworks", "https://anywhereworks.recruitee.com/api/offers"),
        ("workable", "careers", "https://www.workable.com/api/accounts/careers?details=true"),
    ]


def test_discovered_rows_parse_smartrecruiters_recruitee_and_workable_payloads() -> None:
    module = _load_module()
    smartrecruiters = module.feed_target_from_known_ats_url(
        "https://jobs.smartrecruiters.com/acme",
        source_record_id="sr",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    recruitee = module.feed_target_from_known_ats_url(
        "https://anywhereworks.recruitee.com/jobs",
        source_record_id="recruitee",
        source_index=1,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    workable = module.feed_target_from_known_ats_url(
        "https://apply.workable.com/careers/",
        source_record_id="workable",
        source_index=2,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert smartrecruiters is not None
    assert recruitee is not None
    assert workable is not None

    smartrecruiters_rows = module.discovered_rows_from_feed(
        module.FeedResult(
            target=smartrecruiters,
            status="fetched",
            payload={
                "content": [
                    {
                        "id": "sr-1",
                        "name": "Product Analyst",
                        "postingUrl": "https://jobs.smartrecruiters.com/acme/123-product-analyst",
                        "location": {"fullLocation": "Remote, United States"},
                        "department": {"label": "Data"},
                    },
                    {
                        "id": "sr-2",
                        "name": "Wrong board",
                        "postingUrl": "https://jobs.smartrecruiters.com/other/999-wrong-board",
                    },
                ]
            },
        )
    )
    recruitee_rows = module.discovered_rows_from_feed(
        module.FeedResult(
            target=recruitee,
            status="fetched",
            payload={
                "offers": [
                    {
                        "id": 99,
                        "title": "Designer",
                        "careers_url": "https://anywhereworks.recruitee.com/o/designer",
                        "locations": [{"name": "Paris"}],
                        "department": "Design",
                    },
                    {
                        "id": 100,
                        "title": "Wrong host",
                        "careers_url": "https://other.recruitee.com/o/wrong-host",
                    },
                ]
            },
        )
    )
    workable_rows = module.discovered_rows_from_feed(
        module.FeedResult(
            target=workable,
            status="fetched",
            payload={
                "jobs": [
                    {
                        "shortcode": "ABC123",
                        "title": "Engineer",
                        "url": "https://apply.workable.com/j/ABC123",
                        "location": {"location_str": "Boston, United States"},
                        "department": "Engineering",
                    },
                    {
                        "shortcode": "DEF456",
                        "title": "Ignored",
                        "url": "https://example.com/jobs/ignored",
                    },
                    {
                        "shortcode": "GHI789",
                        "full_title": "Fallback title",
                        "shortlink": "https://apply.workable.com/j/GHI789",
                        "location": "Remote",
                    },
                ]
            },
        )
    )

    assert [row["canonical_url"] for row in smartrecruiters_rows] == [
        "https://jobs.smartrecruiters.com/acme/123-product-analyst",
    ]
    assert smartrecruiters_rows[0]["source_job_id"] == "sr-1"
    assert smartrecruiters_rows[0]["source_job_department"] == "Data"

    assert [row["canonical_url"] for row in recruitee_rows] == [
        "https://anywhereworks.recruitee.com/o/designer",
    ]
    assert recruitee_rows[0]["source_job_id"] == "99"
    assert recruitee_rows[0]["source_job_location"] == "Paris"

    assert [row["canonical_url"] for row in workable_rows] == [
        "https://apply.workable.com/j/ABC123",
        "https://apply.workable.com/j/GHI789",
    ]
    assert workable_rows[0]["source_job_id"] == "ABC123"
    assert workable_rows[0]["source_job_location"] == "Boston, United States"
    assert workable_rows[1]["source_job_title"] == "Fallback title"
    assert workable_rows[1]["source_job_location"] == "Remote"


def test_discovered_rows_parse_feed_payloads_and_enforce_scope() -> None:
    module = _load_module()
    greenhouse = module.feed_target_from_known_ats_url(
        "https://boards.greenhouse.io/acme",
        source_record_id="gh",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert greenhouse is not None
    result = module.FeedResult(
        target=greenhouse,
        status="fetched",
        payload={
            "jobs": [
                {
                    "id": 1,
                    "title": "Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "application_url": "https://boards.greenhouse.io/acme/jobs/1/application",
                    "location": {"name": "Remote"},
                    "departments": [{"id": 4, "name": "Engineering"}],
                    "content": "<p>Build <strong>Python agents</strong>.</p>",
                    "skills": ["Python", {"name": "Agents"}],
                    "industries": ["Software", {"name": "Artificial Intelligence"}],
                    "employmentType": "Full-time",
                    "workplaceType": "Remote",
                    "seniorityLevel": "Senior",
                    "salaryRange": {"currency": "USD", "min": 150000, "max": 190000},
                    "visaSponsorship": "available",
                },
                {
                    "id": 2,
                    "title": "Wrong board",
                    "absolute_url": "https://boards.greenhouse.io/other/jobs/2",
                },
                {
                    "id": 3,
                    "title": "Duplicate",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1#fragment",
                },
            ]
        },
    )

    rows = module.discovered_rows_from_feed(result)

    assert len(rows) == 1
    assert rows[0]["canonical_url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert rows[0]["source_relationship"] == "known_ats"
    assert rows[0]["source_feed_ats"] == "greenhouse"
    assert rows[0]["source_feed_token"] == "acme"
    assert rows[0]["source_job_id"] == "1"
    assert rows[0]["source_job_title"] == "Engineer"
    assert rows[0]["source_job_location"] == "Remote"
    assert rows[0]["schema_version"] == 2
    assert rows[0]["source_job_locations"] == ["Remote"]
    assert rows[0]["source_job_department"] == "Engineering"
    assert rows[0]["source_job_description"] == "Build Python agents."
    assert rows[0]["source_job_apply_url"] == (
        "https://boards.greenhouse.io/acme/jobs/1/application"
    )
    assert rows[0]["source_job_keywords"] == ["Python", "Agents"]
    assert rows[0]["source_job_industry"] == "Software"
    assert rows[0]["source_job_industries"] == [
        "Software",
        "Artificial Intelligence",
    ]
    assert rows[0]["source_job_employment_type"] == "Full-time"
    assert rows[0]["source_job_work_mode"] == "Remote"
    assert rows[0]["source_job_seniority"] == "Senior"
    assert rows[0]["source_job_salary"] == {
        "salaryRange": {"currency": "USD", "min": 150000, "max": 190000}
    }
    assert rows[0]["source_job_authorization"] == {"visaSponsorship": "available"}


def test_discovered_rows_parse_ashby_and_lever_payloads() -> None:
    module = _load_module()
    ashby = module.feed_target_from_known_ats_url(
        "https://jobs.ashbyhq.com/omni",
        source_record_id="ashby",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    lever = module.feed_target_from_known_ats_url(
        "https://jobs.lever.co/helix",
        source_record_id="lever",
        source_index=1,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert ashby is not None
    assert lever is not None

    ashby_rows = module.discovered_rows_from_feed(
        module.FeedResult(
            target=ashby,
            status="fetched",
            payload={
                "jobs": [
                    {
                        "id": "ash-1",
                        "title": "Designer",
                        "jobUrl": "https://jobs.ashbyhq.com/omni/123",
                        "applyUrl": "https://jobs.ashbyhq.com/omni/123/application",
                        "location": "NYC",
                        "secondaryLocations": [{"name": "Remote - US"}],
                        "descriptionPlain": "Design agent workflows.",
                        "employmentType": "FullTime",
                        "isRemote": True,
                        "seniority": "Lead",
                        "compensation": {"currencyCode": "USD", "minValue": 175000},
                        "workAuthorization": {"sponsorship": False},
                        "keywords": ["Design Systems", "Agents"],
                    }
                ]
            },
        )
    )
    lever_rows = module.discovered_rows_from_feed(
        module.FeedResult(
            target=lever,
            status="fetched",
            payload=[
                {
                    "id": "lev-1",
                    "text": "Product",
                    "hostedUrl": "https://jobs.lever.co/helix/abc",
                    "categories": {"team": "PM", "location": "SF"},
                }
            ],
        )
    )

    assert ashby_rows[0]["canonical_url"] == "https://jobs.ashbyhq.com/omni/123"
    assert ashby_rows[0]["source_job_id"] == "ash-1"
    assert ashby_rows[0]["source_job_location"] == "NYC"
    assert ashby_rows[0]["source_job_locations"] == ["NYC", "Remote - US"]
    assert ashby_rows[0]["source_job_description"] == "Design agent workflows."
    assert ashby_rows[0]["source_job_apply_url"].endswith("/123/application")
    assert ashby_rows[0]["source_job_employment_type"] == "FullTime"
    assert ashby_rows[0]["source_job_work_mode"] == "remote"
    assert ashby_rows[0]["source_job_remote"] is True
    assert ashby_rows[0]["source_job_seniority"] == "Lead"
    assert ashby_rows[0]["source_job_salary"] == {
        "compensation": {"currencyCode": "USD", "minValue": 175000}
    }
    assert ashby_rows[0]["source_job_authorization"] == {
        "workAuthorization": {"sponsorship": False}
    }
    assert ashby_rows[0]["source_job_keywords"] == ["Design Systems", "Agents"]
    assert lever_rows[0]["canonical_url"] == "https://jobs.lever.co/helix/abc"
    assert lever_rows[0]["source_job_department"] == "PM"


def test_discover_public_ats_jobs_retries_then_writes_once(tmp_path: Path) -> None:
    module = _load_module()
    triage_input = tmp_path / "triage.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(
        triage_input,
        [_triage_row(links=[{"href": "https://jobs.lever.co/helix", "relationship": "known_ats"}])],
    )
    attempts = 0

    def fake_fetch(target: Any, config: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return module.FeedResult(target=target, status="network_error", error_kind="boom")
        return module.FeedResult(
            target=target,
            status="fetched",
            payload=[
                {
                    "id": "lev-1",
                    "text": "Engineer",
                    "hostedUrl": "https://jobs.lever.co/helix/abc",
                },
                {
                    "id": "lev-1-duplicate",
                    "text": "Engineer duplicate",
                    "hostedUrl": "https://jobs.lever.co/helix/abc#fragment",
                },
            ],
        )

    summary = module.discover_public_ats_jobs(
        triage_input=triage_input,
        output_dir=output_dir,
        fetcher=fake_fetch,
        concurrency=1,
        retry_rounds=4,
        config=module.FetchConfig(),
    )

    discovered_text = (output_dir / module.DISCOVERED_FILENAME).read_text(encoding="utf-8")
    discovered = [json.loads(line) for line in discovered_text.splitlines()]
    receipts = [
        json.loads(line)
        for line in (output_dir / module.RECEIPT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert attempts == 3
    assert summary["max_total_attempts_per_feed"] == 5
    assert summary["discovered_job_urls"] == 1
    assert discovered[0]["canonical_url"] == "https://jobs.lever.co/helix/abc"
    assert len(receipts) == 1
    assert receipts[0]["capture_status"] == "fetched"


def test_manifest_records_non_manifest_artifact_digests_and_sizes(tmp_path: Path) -> None:
    module = _load_module()
    triage_input = tmp_path / "triage.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(
        triage_input,
        [_triage_row(links=[{"href": "https://jobs.lever.co/helix", "relationship": "known_ats"}])],
    )

    def fake_fetch(target: Any, config: Any) -> Any:
        return module.FeedResult(
            target=target,
            status="fetched",
            payload=[
                {
                    "id": "lev-1",
                    "text": "Engineer",
                    "hostedUrl": "https://jobs.lever.co/helix/abc",
                }
            ],
        )

    module.discover_public_ats_jobs(
        triage_input=triage_input,
        output_dir=output_dir,
        fetcher=fake_fetch,
        concurrency=1,
        retry_rounds=4,
        config=module.FetchConfig(),
    )

    manifest = json.loads((output_dir / module.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["kind"] == "public_ats_job_feed_manifest"
    assert manifest["artifact_files"]["manifest"] == str(output_dir / module.MANIFEST_FILENAME)
    for key, filename in {
        "discovered_jobs": module.DISCOVERED_FILENAME,
        "receipts": module.RECEIPT_FILENAME,
        "summary": module.SUMMARY_FILENAME,
        "failure_streaks": module.FAILURE_STREAKS_FILENAME,
    }.items():
        path = output_dir / filename
        metadata = manifest["artifact_files"][key]
        assert metadata["path"] == str(path)
        assert metadata["bytes"] == path.stat().st_size
        assert metadata["artifact_sha256"] == _sha256_file(path)


def test_artifact_metadata_hashes_large_files_in_chunks(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_module()
    artifact_path = tmp_path / "large.jsonl"
    artifact_path.write_bytes(b"a" * 10 + b"b" * 11 + b"c" * 12)
    monkeypatch.setattr(module, "ARTIFACT_HASH_CHUNK_BYTES", 7)

    metadata = module._artifact_metadata(artifact_path)

    assert metadata == {
        "path": str(artifact_path),
        "bytes": artifact_path.stat().st_size,
        "artifact_sha256": _sha256_file(artifact_path),
    }


def test_manifest_discovered_jobs_digest_changes_after_output_mutation_on_resume(
    tmp_path: Path,
) -> None:
    module = _load_module()
    triage_input = tmp_path / "triage.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(
        triage_input,
        [_triage_row(links=[{"href": "https://jobs.lever.co/helix", "relationship": "known_ats"}])],
    )

    def fake_fetch(target: Any, config: Any) -> Any:
        return module.FeedResult(
            target=target,
            status="fetched",
            payload=[
                {
                    "id": "lev-1",
                    "text": "Engineer",
                    "hostedUrl": "https://jobs.lever.co/helix/abc",
                }
            ],
        )

    module.discover_public_ats_jobs(
        triage_input=triage_input,
        output_dir=output_dir,
        fetcher=fake_fetch,
        concurrency=1,
        retry_rounds=4,
        config=module.FetchConfig(),
    )
    first_manifest = json.loads((output_dir / module.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    discovered_path = output_dir / module.DISCOVERED_FILENAME
    discovered_path.write_text(
        discovered_path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "canonical_url": "https://jobs.lever.co/helix/manual-mutation",
                "source_job_title": "Manual mutation",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    module.discover_public_ats_jobs(
        triage_input=triage_input,
        output_dir=output_dir,
        fetcher=fake_fetch,
        concurrency=1,
        retry_rounds=4,
        config=module.FetchConfig(),
    )

    resumed_manifest = json.loads(
        (output_dir / module.MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert resumed_manifest["skipped_completed_feeds"] == 1
    assert resumed_manifest["artifact_files"]["discovered_jobs"]["artifact_sha256"] == (
        _sha256_file(discovered_path)
    )
    assert (
        resumed_manifest["artifact_files"]["discovered_jobs"]["artifact_sha256"]
        != (first_manifest["artifact_files"]["discovered_jobs"]["artifact_sha256"])
    )


def test_discover_public_ats_jobs_marks_feed_done_after_five_failures(tmp_path: Path) -> None:
    module = _load_module()
    triage_input = tmp_path / "triage.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(
        triage_input,
        [
            _triage_row(
                links=[{"href": "https://jobs.ashbyhq.com/omni", "relationship": "known_ats"}]
            )
        ],
    )
    attempts = 0

    def fake_fetch(target: Any, config: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return module.FeedResult(target=target, status="retryable_http_error", http_status=503)

    summary = module.discover_public_ats_jobs(
        triage_input=triage_input,
        output_dir=output_dir,
        fetcher=fake_fetch,
        concurrency=1,
        retry_rounds=4,
        config=module.FetchConfig(),
    )

    receipts = [
        json.loads(line)
        for line in (output_dir / module.RECEIPT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    failure_state = json.loads(
        (output_dir / module.FAILURE_STREAKS_FILENAME).read_text(encoding="utf-8")
    )
    assert attempts == 5
    assert summary["status_counts"] == {"retryable_http_error": 1}
    assert len(receipts) == 1
    assert (
        failure_state["failure_streaks"]["https://api.ashbyhq.com/posting-api/job-board/omni"] == 5
    )


def test_resume_skips_success_and_allows_unfinished_retry(tmp_path: Path) -> None:
    module = _load_module()
    discovered_path = tmp_path / module.DISCOVERED_FILENAME
    receipt_path = tmp_path / module.RECEIPT_FILENAME
    greenhouse = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    lever = "https://api.lever.co/v0/postings/helix?mode=json"
    _write_jsonl(
        discovered_path,
        [{"canonical_url": "https://jobs.lever.co/helix/abc"}],
    )
    _write_jsonl(
        receipt_path,
        [
            {"canonical_url": greenhouse, "capture_status": "fetched"},
            {"canonical_url": lever, "capture_status": "network_error"},
        ],
    )

    completed, discovered, failure_streaks = module._resume_state(
        receipt_path, discovered_path, retry_rounds=4
    )

    assert greenhouse in completed
    assert lever not in completed
    assert discovered == {"https://jobs.lever.co/helix/abc"}
    assert failure_streaks[lever] == 1


def test_fetch_feed_uses_public_url_guard(monkeypatch: Any) -> None:
    module = _load_module()
    target = module.feed_target_from_known_ats_url(
        "https://jobs.lever.co/helix",
        source_record_id="lever",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert target is not None

    def reject_public_url(*_args: object, **_kwargs: object) -> None:
        raise module.UnsafeTargetError("blocked")

    monkeypatch.setattr(module, "_ensure_public_http_url", reject_public_url)

    result = module.fetch_feed(target, module.FetchConfig())

    assert result.status == "invalid_url"
    assert result.error_kind == "UnsafeTargetError"


def test_scope_rejects_job_url_outside_board() -> None:
    module = _load_module()
    target = module.feed_target_from_known_ats_url(
        "https://jobs.lever.co/helix",
        source_record_id="lever",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert target is not None

    assert module._job_url_matches_scope("https://jobs.lever.co/helix/abc", target)
    assert not module._job_url_matches_scope("https://jobs.lever.co/other/abc", target)
    assert not module._job_url_matches_scope("https://example.com/jobs/abc", target)


def test_smartrecruiters_provider_limit_caps_parallel_fetches(tmp_path: Path) -> None:
    module = _load_module()
    triage_input = tmp_path / "triage.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(
        triage_input,
        [
            _triage_row(
                record_id=f"sr-{index}",
                links=[
                    {
                        "href": f"https://jobs.smartrecruiters.com/company{index}",
                        "relationship": "known_ats",
                    }
                ],
            )
            for index in range(10)
        ],
    )

    active = 0
    max_active = 0
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def fake_fetch(target: Any, config: Any) -> Any:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= module.SMARTRECRUITERS_MAX_CONCURRENCY:
                entered.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return module.FeedResult(
            target=target,
            status="fetched",
            payload={"content": []},
        )

    thread = threading.Thread(
        target=module.discover_public_ats_jobs,
        kwargs={
            "triage_input": triage_input,
            "output_dir": output_dir,
            "fetcher": fake_fetch,
            "concurrency": 12,
            "retry_rounds": 4,
            "config": module.FetchConfig(),
        },
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=5)
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert max_active <= module.SMARTRECRUITERS_MAX_CONCURRENCY


def test_smartrecruiters_fetch_feed_paginates_by_offset(monkeypatch: Any) -> None:
    module = _load_module()
    target = module.feed_target_from_known_ats_url(
        "https://jobs.smartrecruiters.com/acme",
        source_record_id="sr",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert target is not None

    monkeypatch.setattr(module, "SMARTRECRUITERS_PAGE_SIZE", 1)
    requested_urls: list[str] = []

    def fake_page(url: str, target_arg: Any, config: Any) -> Any:
        requested_urls.append(url)
        if "offset=0" in url:
            return module.FeedResult(
                target=target_arg,
                status="fetched",
                payload={
                    "content": [
                        {
                            "id": "1",
                            "name": "Analyst",
                            "postingUrl": "https://jobs.smartrecruiters.com/acme/analyst",
                        }
                    ],
                    "totalFound": 2,
                    "limit": 1,
                    "offset": 0,
                },
                final_url=url,
                http_status=200,
                content_type="application/json",
                body_bytes=10,
                body_sha256="page-1",
            )
        if "offset=1" in url:
            return module.FeedResult(
                target=target_arg,
                status="fetched",
                payload={
                    "content": [
                        {
                            "id": "2",
                            "name": "Designer",
                            "postingUrl": "https://jobs.smartrecruiters.com/acme/designer",
                        }
                    ],
                    "totalFound": 2,
                    "limit": 1,
                    "offset": 1,
                },
                final_url=url,
                http_status=200,
                content_type="application/json",
                body_bytes=11,
                body_sha256="page-2",
            )
        raise AssertionError(f"unexpected URL {url!r}")

    monkeypatch.setattr(module, "_fetch_json_page", fake_page)

    result = module.fetch_feed(target, module.FetchConfig())

    assert requested_urls == [
        "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1&offset=0",
        "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=1&offset=1",
    ]
    assert [job["id"] for job in result.payload["content"]] == ["1", "2"]
    assert result.body_bytes == 21
    assert result.http_status == 200


def test_workable_fetch_feed_follows_paging_next(monkeypatch: Any) -> None:
    module = _load_module()
    target = module.feed_target_from_known_ats_url(
        "https://apply.workable.com/careers/",
        source_record_id="wk",
        source_index=0,
        source_assessment="external_ats_needs_verification",
        registrable_domain="company.test",
    )
    assert target is not None

    requested_urls: list[str] = []

    def fake_page(url: str, target_arg: Any, config: Any) -> Any:
        requested_urls.append(url)
        if url == target.url:
            return module.FeedResult(
                target=target_arg,
                status="fetched",
                payload={
                    "jobs": [
                        {
                            "shortcode": "ABC123",
                            "title": "Engineer",
                            "url": "https://apply.workable.com/j/ABC123",
                        }
                    ],
                    "paging": {"next": "/api/accounts/careers?details=true&page=2"},
                },
                final_url=url,
                http_status=200,
                content_type="application/json",
                body_bytes=10,
                body_sha256="page-1",
            )
        if url == "https://www.workable.com/api/accounts/careers?details=true&page=2":
            return module.FeedResult(
                target=target_arg,
                status="fetched",
                payload={
                    "jobs": [
                        {
                            "shortcode": "DEF456",
                            "title": "Designer",
                            "url": "https://apply.workable.com/j/DEF456",
                        }
                    ]
                },
                final_url=url,
                http_status=200,
                content_type="application/json",
                body_bytes=11,
                body_sha256="page-2",
            )
        raise AssertionError(f"unexpected URL {url!r}")

    monkeypatch.setattr(module, "_fetch_json_page", fake_page)

    result = module.fetch_feed(target, module.FetchConfig())

    assert requested_urls == [
        "https://www.workable.com/api/accounts/careers?details=true",
        "https://www.workable.com/api/accounts/careers?details=true&page=2",
    ]
    assert [job["shortcode"] for job in result.payload["jobs"]] == ["ABC123", "DEF456"]
    assert result.body_bytes == 21
    assert result.http_status == 200
