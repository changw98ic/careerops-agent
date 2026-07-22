from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from careerops.cli import crawl_sources

ROOT = Path(__file__).parents[2]


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for script_name in (
        "discover_recruitment_sitemaps.py",
        "discover_recruitment_commoncrawl.py",
        "discover_public_ats_jobs.py",
        "collect_recruitment_pages.py",
    ):
        (scripts / script_name).write_text("# registered test crawler\n", encoding="utf-8")
    triage = root / "datasets" / "private" / "triage.jsonl"
    triage.parent.mkdir(parents=True)
    triage.write_text('{"registrable_domain":"example.com"}\n', encoding="utf-8")
    return root, triage


def _manifest() -> dict[str, Any]:
    return {
        "version": 1,
        "sources": [
            {
                "source_id": "sitemaps",
                "adapter": "recruitment.sitemap_discovery",
                "input_artifact": "datasets/private/triage.jsonl",
                "output_dir": "datasets/raw/sitemaps",
                "limits": {"concurrency": 2, "retry_rounds": 1, "timeout_seconds": 5.0},
            },
            {
                "source_id": "commoncrawl",
                "adapter": "recruitment.commoncrawl_discovery",
                "input_artifact": "datasets/private/triage.jsonl",
                "output_dir": "datasets/raw/commoncrawl",
                "limits": {"concurrency": 2, "retry_rounds": 1, "timeout_seconds": 5.0},
            },
            {
                "source_id": "public-ats",
                "adapter": "recruitment.public_ats_feed",
                "input_artifact": "datasets/private/triage.jsonl",
                "output_dir": "datasets/raw/ats",
                "limits": {"concurrency": 2, "retry_rounds": 1, "timeout_seconds": 5.0},
            },
            {
                "source_id": "pages",
                "adapter": "recruitment.page_collection",
                "input_artifact": "datasets/private/triage.jsonl",
                "output_dir": "datasets/raw/pages",
                "additional_seed_sources": ["sitemaps", "commoncrawl"],
                "limits": {
                    "concurrency": 2,
                    "retry_rounds": 1,
                    "timeout_seconds": 5.0,
                    "max_stored_bytes": 1000,
                    "max_response_bytes": 100,
                    "max_concurrency_per_host": 2,
                    "depth": 1,
                },
            },
        ],
    }


def test_plan_uses_registered_crawlers_and_chains_discovery_artifacts(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    config_path = _write_json(root / "crawler-sources.json", _manifest())

    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    assert [plan.source_id for plan in plans] == ["sitemaps", "commoncrawl", "public-ats", "pages"]
    assert plans[0].command[1] == "-Es"
    assert plans[0].command[2].endswith("discover_recruitment_sitemaps.py")
    assert plans[1].command[2].endswith("discover_recruitment_commoncrawl.py")
    assert plans[2].command[2].endswith("discover_public_ats_jobs.py")
    page_plan = plans[-1]
    assert page_plan.command[2].endswith("collect_recruitment_pages.py")
    assert page_plan.dependency_artifacts == (
        root / "datasets" / "raw" / "sitemaps" / "discovered_recruitment_sitemap_urls.jsonl",
        root / "datasets" / "raw" / "commoncrawl" / "discovered_recruitment_commoncrawl_urls.jsonl",
    )
    assert "--allow-sandbox-egress-alias" not in page_plan.command
    assert "--max-concurrency-per-host" in page_plan.command
    assert "2" in page_plan.command


def test_schedule_caps_tighten_the_actual_registered_adapter_command(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    source = manifest["sources"][2]
    source["schedule"] = {
        "cadence_seconds": 3_600,
        "retry_rounds": 0,
        "budget": {"max_feed_bytes": 4_096, "timeout_seconds": 2.0},
        "rate_limits": {"concurrency": 1},
    }
    config_path = _write_json(root / "bounded-source.json", manifest)

    plan = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)[2]

    command = list(plan.command)
    assert command[command.index("--max-feed-bytes") + 1] == "4096"
    assert command[command.index("--timeout-seconds") + 1] == "2.0"
    assert command[command.index("--concurrency") + 1] == "1"
    assert command[command.index("--retry-rounds") + 1] == "0"
    assert plan.schedule["budget"] == {
        "max_feed_bytes": 4_096,
        "retry_rounds": 0,
        "timeout_seconds": 2.0,
    }
    assert plan.schedule["rate_limits"] == {"concurrency": 1}


def test_schedule_defaults_to_dedupe_only_for_every_adapter(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    config_path = _write_json(root / "default-ingestion-policy.json", _manifest())

    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    assert {plan.source_id: plan.schedule["canonical_ingestion_policy"] for plan in plans} == {
        "sitemaps": "dedupe_only",
        "commoncrawl": "dedupe_only",
        "public-ats": "dedupe_only",
        "pages": "dedupe_only",
    }


def test_only_public_ats_can_declare_canonical_job_ingestion(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][2]["schedule"] = {"canonical_ingestion_policy": "canonical_job_ingestion"}
    config_path = _write_json(root / "public-ats-canonical.json", manifest)

    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    assert plans[2].schedule["canonical_ingestion_policy"] == "canonical_job_ingestion"

    manifest = _manifest()
    manifest["sources"][0]["schedule"] = {"canonical_ingestion_policy": "canonical_job_ingestion"}
    config_path = _write_json(root / "sitemap-canonical.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="only supported by the public ATS"):
        crawl_sources.load_manifest(config_path)


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"budget": {"shell_timeout": 1}}, "invalid manifest fields"),
        ({"budget": {"max_indexes": 1}}, "unsupported by the selected adapter"),
        ({"rate_limits": {"concurrency": 8}}, "cannot weaken the adapter limit"),
    ],
)
def test_schedule_rejects_fake_or_weakening_controls(
    tmp_path: Path,
    schedule: dict[str, object],
    message: str,
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][2]["schedule"] = schedule
    config_path = _write_json(root / "unsafe-schedule.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match=message):
        crawl_sources.load_manifest(config_path)


def test_manifest_rejects_untrusted_url_and_unsafe_artifact_path(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][0]["url"] = "https://internal.example/metadata"
    config_path = _write_json(root / "bad.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="invalid manifest fields"):
        crawl_sources.load_manifest(config_path)

    manifest = _manifest()
    manifest["sources"][0]["input_artifact"] = "../outside.jsonl"
    config_path = _write_json(root / "unsafe-path.json", manifest)
    loaded = crawl_sources.load_manifest(config_path)

    with pytest.raises(crawl_sources.CrawlSourceError, match="inside datasets"):
        crawl_sources.plan_manifest(loaded, root=root)

    manifest = _manifest()
    manifest["sources"][0]["limits"]["allow_sandbox_egress_alias"] = True
    config_path = _write_json(root / "unsafe-egress.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="invalid manifest fields"):
        crawl_sources.load_manifest(config_path)


def test_resume_is_only_accepted_for_adapters_that_expose_no_resume(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][0]["resume"] = False
    config_path = _write_json(root / "no-resume-sitemap.json", manifest)

    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    assert "--no-resume" in plans[0].command

    manifest = _manifest()
    manifest["sources"][2]["resume"] = False
    config_path = _write_json(root / "no-resume-public-ats.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="invalid manifest fields"):
        crawl_sources.load_manifest(config_path)


def test_run_strips_proxy_environment_and_never_records_process_output(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][2]]
    config_path = _write_json(root / "one-source.json", manifest)
    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_runner(
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == root
        calls.append((args, env))
        return subprocess.CompletedProcess(args, 0, "DO_NOT_PERSIST", "ALSO_DO_NOT_PERSIST")

    results = crawl_sources.run_plans(
        plans,
        root=root,
        environment={
            "PATH": "/bin",
            "HTTPS_PROXY": "http://proxy.invalid",
            "PYTHONPATH": "/unreviewed/code",
        },
        runner=fake_runner,
    )

    assert [result.status for result in results] == ["completed"]
    assert calls[0][1] == {"PATH": "/bin"}
    receipt_text = results[0].receipt_path.read_text(encoding="utf-8")
    assert "DO_NOT_PERSIST" not in receipt_text
    assert "ALSO_DO_NOT_PERSIST" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["status"] == "completed"
    assert receipt["returncode"] == 0


def test_disabled_sources_are_validated_but_cannot_be_selected_or_run(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][2]["enabled"] = False
    config_path = _write_json(root / "disabled.json", manifest)
    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    assert "public-ats" not in [plan.source_id for plan in crawl_sources._select_plans(plans, [])]
    with pytest.raises(crawl_sources.CrawlSourceError, match="disabled"):
        crawl_sources._select_plans(plans, ["public-ats"])
    with pytest.raises(crawl_sources.CrawlSourceError, match="disabled"):
        crawl_sources.run_plans(plans, root=root)


def test_public_review_selector_exposes_only_enabled_manifest_sources(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"][2]["enabled"] = False
    config_path = _write_json(root / "disabled.json", manifest)
    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    selected = crawl_sources.select_plans_for_review(plans, ["pages"])

    assert [plan.source_id for plan in selected] == ["pages"]
    with pytest.raises(crawl_sources.CrawlSourceError, match="disabled"):
        crawl_sources.select_plans_for_review(plans, ["public-ats"])


def test_main_emits_a_machine_readable_plan_without_running_a_crawler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    config_path = _write_json(root / "crawler-sources.json", _manifest())

    returncode = crawl_sources.main(
        [
            "--root",
            str(root),
            "plan",
            "--config",
            str(config_path),
            "--source",
            "public-ats",
            "--json",
        ]
    )

    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["source_count"] == 1
    assert payload["sources"][0]["source_id"] == "public-ats"


def test_main_registers_a_durable_source_registry_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _triage = _repo(tmp_path)
    config_path = _write_json(root / "crawler-sources.json", _manifest())

    captured: dict[str, object] = {}

    class _FakeConnection:
        def __init__(self) -> None:
            self._in_transaction = True

        def in_transaction(self) -> bool:
            return self._in_transaction

    class _FakeBegin:
        def __enter__(self) -> _FakeConnection:
            return _FakeConnection()

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class _FakeEngine:
        def begin(self) -> _FakeBegin:
            return _FakeBegin()

        def dispose(self) -> None:
            return None

    class _FakeRegistryRepository:
        def __init__(self, connection: object) -> None:
            captured["connection"] = connection

        def register_registry(
            self,
            manifest: object,
            plans: object,
            registry_document: object,
            *,
            root: object,
            created_by: str,
            now: datetime,
        ) -> UUID:
            captured["manifest"] = manifest
            captured["plans"] = plans
            captured["registry_document"] = registry_document
            captured["root"] = root
            captured["created_by"] = created_by
            captured["now"] = now
            return uuid4()

    monkeypatch.setattr(crawl_sources, "create_database_engine", lambda settings: _FakeEngine())
    monkeypatch.setattr(
        crawl_sources,
        "PostgresCrawlerSourceRegistryRepository",
        _FakeRegistryRepository,
    )

    returncode = crawl_sources.main(
        [
            "--root",
            str(root),
            "register",
            "--config",
            str(config_path),
            "--json",
        ]
    )

    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    manifest = crawl_sources.load_manifest(config_path)
    expected_manifest_sha256 = crawl_sources._manifest_sha256(manifest)
    registry_path = (
        root
        / "datasets"
        / "private"
        / "recruitment"
        / "source-registry"
        / (f"{expected_manifest_sha256}.json")
    )
    assert payload["status"] == "registered"
    assert payload["manifest_sha256"] == expected_manifest_sha256
    assert payload["registry"] == str(registry_path.relative_to(root))
    assert payload["source_count"] == 4
    assert payload["database_registry_id"]
    assert registry_path.is_file()
    registry_document = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry_document["kind"] == "configured_crawl_source_registry"
    assert registry_document["manifest_sha256"] == expected_manifest_sha256
    assert [plan["source_id"] for plan in registry_document["resolved_plans"]] == [
        "sitemaps",
        "commoncrawl",
        "public-ats",
        "pages",
    ]
    assert captured["created_by"] == "careerops-crawl-sources"
    captured_registry = cast(
        "crawl_sources.CrawlSourceRegistryDocument",
        captured["registry_document"],
    )
    assert captured_registry.manifest_sha256 == expected_manifest_sha256


def test_example_manifest_matches_the_runtime_contract() -> None:
    manifest = crawl_sources.load_manifest(
        ROOT / "datasets" / "manifests" / "recruitment-crawler-sources.example.json"
    )

    assert [source.source_id for source in manifest.sources] == [
        "company-sitemaps",
        "company-commoncrawl",
        "public-ats-feeds",
        "public-career-pages",
    ]


def test_execution_approval_is_bound_to_the_exact_manifest_plan_and_expiry(
    tmp_path: Path,
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = crawl_sources.load_manifest(_write_json(root / "crawler-sources.json", _manifest()))
    plans = crawl_sources._select_plans(
        crawl_sources.plan_manifest(manifest, root=root),
        ["public-ats"],
    )
    now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    request = crawl_sources.create_execution_request(
        manifest,
        plans,
        root=root,
        reason="daily public ATS refresh",
        now=now,
        expires_in=timedelta(hours=24),
    )
    approval = crawl_sources.approve_execution_request(
        request,
        approved_by="operator@example",
        note="reviewed bounded plan",
        now=now + timedelta(minutes=5),
    )

    crawl_sources.verify_execution_approval(
        request,
        approval,
        now=now + timedelta(minutes=10),
    )

    with pytest.raises(crawl_sources.CrawlSourceError, match="expired"):
        crawl_sources.approve_execution_request(
            request,
            approved_by="operator@example",
            note="too late",
            now=now + timedelta(hours=24),
        )


def test_main_executes_only_a_matching_reviewed_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    config_path = _write_json(root / "crawler-sources.json", _manifest())
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/request.execution.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--source",
                "public-ats",
                "--request-output",
                request_path,
                "--reason",
                "daily public ATS refresh",
                "--json",
            ]
        )
        == 0
    )
    request_response = json.loads(capsys.readouterr().out)
    assert request_response["status"] == "review_requested"
    request_document = json.loads((root / request_path).read_text(encoding="utf-8"))
    assert request_document["reviewed_plan"][0]["source_id"] == "public-ats"
    assert request_document["reviewed_plan"][0]["implementation_files"]

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
                "--note",
                "reviewed bounded plan",
                "--json",
            ]
        )
        == 0
    )
    approval_response = json.loads(capsys.readouterr().out)
    assert approval_response["request_id"] == request_response["request_id"]

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
                "--json",
            ]
        )
        == 0
    )
    execution_response = json.loads(capsys.readouterr().out)
    assert execution_response["status"] == "completed"
    assert execution_response["source_count"] == 1
    assert execution_response["execution_claim"].startswith(
        "datasets/private/crawler-execution-claims/"
    )
    assert execution_response["execution_snapshot"].startswith(
        "datasets/private/crawler-execution-snapshots/"
    )
    assert (root / approval_path).is_file()
    assert (root / execution_response["execution_claim"]).is_file()
    receipt_path = root / "datasets/raw/ats/configured_crawl_receipt.json"
    assert receipt_path.is_file()
    assert (
        json.loads(receipt_path.read_text(encoding="utf-8"))["execution_request_id"]
        == request_response["request_id"]
    )

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
                "--json",
            ]
        )
        == 2
    )
    assert "already been claimed" in capsys.readouterr().err


def test_main_rejects_a_reviewed_request_when_the_config_changed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    config_path = _write_json(root / "crawler-sources.json", manifest)
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--source",
                "public-ats",
                "--request-output",
                request_path,
                "--reason",
                "daily public ATS refresh",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    manifest["sources"][2]["limits"]["max_feed_bytes"] = 1_000_001
    _write_json(config_path, manifest)

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
                "--json",
            ]
        )
        == 2
    )
    assert "no longer matches the crawler manifest" in capsys.readouterr().err
    assert not (root / "datasets/raw/ats/configured_crawl_receipt.json").exists()


def test_execution_plan_fingerprint_binds_input_and_registered_script(tmp_path: Path) -> None:
    root, triage = _repo(tmp_path)
    manifest = crawl_sources.load_manifest(_write_json(root / "crawler-sources.json", _manifest()))
    plans = crawl_sources._select_plans(
        crawl_sources.plan_manifest(manifest, root=root),
        ["public-ats"],
    )
    request = crawl_sources.create_execution_request(
        manifest,
        plans,
        root=root,
        reason="daily public ATS refresh",
        now=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
        expires_in=timedelta(hours=24),
    )

    triage.write_text('{"registrable_domain":"changed.example"}\n', encoding="utf-8")
    changed_input_plans = crawl_sources._select_plans(
        crawl_sources.plan_manifest(manifest, root=root),
        ["public-ats"],
    )
    assert request.plan_sha256 != crawl_sources._sha256_json(
        crawl_sources._plan_bindings(changed_input_plans, root=root)
    )

    (root / "scripts/discover_public_ats_jobs.py").write_text(
        "# changed implementation\n",
        encoding="utf-8",
    )
    changed_script_plans = crawl_sources._select_plans(
        crawl_sources.plan_manifest(manifest, root=root),
        ["public-ats"],
    )
    assert request.plan_sha256 != crawl_sources._sha256_json(
        crawl_sources._plan_bindings(changed_script_plans, root=root)
    )


def test_plan_rejects_symlinked_dataset_inputs_and_dependencies(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    outside = root / "outside"
    outside.mkdir()
    linked_input_target = outside / "triage.jsonl"
    linked_input_target.write_text('{"registrable_domain":"outside.example"}\n', encoding="utf-8")
    linked_input = root / "datasets/private/linked-triage.jsonl"
    linked_input.symlink_to(linked_input_target)
    manifest = _manifest()
    manifest["sources"][0]["input_artifact"] = "datasets/private/linked-triage.jsonl"
    config_path = _write_json(root / "linked-input.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="symbolic"):
        crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][3]]
    manifest["sources"][1]["additional_seed_sources"] = ["sitemaps"]
    dependency_dir = root / "datasets/raw/sitemaps"
    dependency_dir.mkdir(parents=True)
    dependency = dependency_dir / "discovered_recruitment_sitemap_urls.jsonl"
    dependency.symlink_to(linked_input_target)
    config_path = _write_json(root / "linked-dependency.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="symbolic"):
        crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    review_link = root / "datasets/private/review-link"
    review_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(crawl_sources.CrawlSourceError, match="symbolic"):
        crawl_sources._resolve_new_data_file(
            root,
            "datasets/private/review-link/request.json",
            field="execution request output",
        )


def test_review_artifacts_and_crawler_outputs_use_separate_namespaces(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)

    with pytest.raises(crawl_sources.CrawlSourceError, match="crawler-execution-reviews"):
        crawl_sources._resolve_new_review_artifact(
            root,
            "datasets/private/other/request.json",
            field="execution request output",
        )

    manifest = _manifest()
    manifest["sources"][2]["output_dir"] = "datasets/private/crawler-execution-reviews/ats"
    config_path = _write_json(root / "reserved-output.json", manifest)

    with pytest.raises(crawl_sources.CrawlSourceError, match="must not be inside"):
        crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)

    safe_manifest = _manifest()
    safe_manifest["sources"] = [safe_manifest["sources"][2]]
    safe_config_path = _write_json(root / "safe-crawler-sources.json", safe_manifest)
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(safe_config_path),
                "--request-output",
                "datasets/raw/ats/configured_crawl_receipt.json",
                "--reason",
                "invalid review location",
            ]
        )
        == 2
    )
    assert "crawler-execution-reviews" in capsys.readouterr().err


def test_output_reservation_is_exclusive_for_reviewed_execution(tmp_path: Path) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][2]]
    loaded_manifest = crawl_sources.load_manifest(
        _write_json(root / "crawler-sources.json", manifest)
    )
    plans = crawl_sources.plan_manifest(loaded_manifest, root=root)
    bindings = crawl_sources._plan_bindings(plans, root=root)
    first = crawl_sources.stage_execution_snapshot(
        loaded_manifest,
        plans,
        bindings,
        root=root,
        request_id=str(uuid4()),
    )
    first.reserve_output_directories(plans, root=root, request_id=str(uuid4()))
    second = crawl_sources.stage_execution_snapshot(
        loaded_manifest,
        plans,
        bindings,
        root=root,
        request_id=str(uuid4()),
    )

    with pytest.raises(crawl_sources.CrawlSourceError, match="already reserved"):
        second.reserve_output_directories(plans, root=root, request_id=str(uuid4()))
    first.release_output_locks()
    second.reserve_output_directories(plans, root=root, request_id=str(uuid4()))
    second.release_output_locks()


def test_failed_selected_producer_blocks_dependent_collection_even_with_old_artifact(
    tmp_path: Path,
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][3]]
    manifest["sources"][1]["additional_seed_sources"] = ["sitemaps"]
    config_path = _write_json(root / "producer-dependency.json", manifest)
    plans = crawl_sources.plan_manifest(crawl_sources.load_manifest(config_path), root=root)
    old_dependency = root / "datasets/raw/sitemaps/discovered_recruitment_sitemap_urls.jsonl"
    old_dependency.parent.mkdir(parents=True)
    old_dependency.write_text('{"url":"https://old.example/jobs"}\n', encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def failed_runner(
        args: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == root
        assert env
        calls.append(args)
        return subprocess.CompletedProcess(args, 1)

    results = crawl_sources.run_plans(plans, root=root, runner=failed_runner)

    assert [result.status for result in results] == ["failed", "blocked_upstream_source"]
    assert len(calls) == 1


def test_execute_rejects_a_stale_selected_dependency_before_claim(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][3]]
    manifest["sources"][1]["additional_seed_sources"] = ["sitemaps"]
    config_path = _write_json(root / "crawler-sources.json", manifest)
    stale_dependency = root / "datasets/raw/sitemaps/discovered_recruitment_sitemap_urls.jsonl"
    stale_dependency.parent.mkdir(parents=True)
    stale_dependency.write_text('{"url":"https://stale.example/jobs"}\n', encoding="utf-8")
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                request_path,
                "--reason",
                "fresh seed chain",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
            ]
        )
        == 2
    )
    assert (
        "dependency artifact must be absent before a reviewed execution" in capsys.readouterr().err
    )
    assert not (root / "datasets/private/crawler-execution-claims").exists()


def test_execute_claims_then_refuses_an_existing_output_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][3]]
    manifest["sources"][1]["additional_seed_sources"] = ["sitemaps"]
    config_path = _write_json(root / "crawler-sources.json", manifest)
    (root / "datasets/raw/sitemaps").mkdir(parents=True)
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                request_path,
                "--reason",
                "fresh output run",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
            ]
        )
        == 2
    )
    assert "source sitemaps output_dir must be absent" in capsys.readouterr().err
    claim_directory = root / "datasets/private/crawler-execution-claims"
    assert len(list(claim_directory.glob("*.json"))) == 1


def test_execute_allows_existing_output_for_an_independent_adapter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][2]]
    config_path = _write_json(root / "crawler-sources.json", manifest)
    (root / "datasets/raw/ats").mkdir(parents=True)
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                request_path,
                "--reason",
                "resumable independent run",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
            ]
        )
        == 0
    )
    assert (root / "datasets/raw/ats/configured_crawl_receipt.json").is_file()
    capsys.readouterr()

    second_request_path = "datasets/private/crawler-execution-reviews/request-2.json"
    second_approval_path = "datasets/private/crawler-execution-reviews/approval-2.json"
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                second_request_path,
                "--reason",
                "second resumable independent run",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                second_request_path,
                "--approval-output",
                second_approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                second_request_path,
                "--approval",
                second_approval_path,
            ]
        )
        == 0
    )


def test_execute_captures_a_fresh_selected_dependency_for_its_consumer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][3]]
    manifest["sources"][1]["additional_seed_sources"] = ["sitemaps"]
    config_path = _write_json(root / "crawler-sources.json", manifest)
    (root / "scripts/discover_recruitment_sitemaps.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "output_dir = Path(sys.argv[sys.argv.index('--output-dir') + 1])\n"
        "output_dir.mkdir(parents=True, exist_ok=True)\n"
        "(output_dir / 'discovered_recruitment_sitemap_urls.jsonl').write_text(\n"
        "    '{\"url\":\"https://fresh.example/jobs\"}\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    (root / "scripts/collect_recruitment_pages.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "seed_path = Path(sys.argv[sys.argv.index('--additional-seeds') + 1])\n"
        "output_dir = Path(sys.argv[sys.argv.index('--output-dir') + 1])\n"
        "output_dir.mkdir(parents=True, exist_ok=True)\n"
        "(output_dir / 'observed-seeds.jsonl').write_text(\n"
        "    seed_path.read_text(encoding='utf-8'), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                request_path,
                "--reason",
                "fresh seed chain",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
                "--json",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "completed"
    assert (root / "datasets/raw/pages/observed-seeds.jsonl").read_text(
        encoding="utf-8"
    ) == '{"url":"https://fresh.example/jobs"}\n'
    sitemap_receipt = json.loads(
        (root / "datasets/raw/sitemaps/configured_crawl_receipt.json").read_text(encoding="utf-8")
    )
    assert sitemap_receipt["produced_dependency_artifacts"]


def test_execute_uses_snapshotted_input_and_script_after_originals_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, triage = _repo(tmp_path)
    manifest = _manifest()
    manifest["sources"] = [manifest["sources"][0], manifest["sources"][2]]
    config_path = _write_json(root / "crawler-sources.json", manifest)
    public_ats_script = root / "scripts/discover_public_ats_jobs.py"
    approved_triage = triage.read_text(encoding="utf-8")
    public_ats_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "input_path = Path(sys.argv[sys.argv.index('--triage-input') + 1])\n"
        "output_dir = Path(sys.argv[sys.argv.index('--output-dir') + 1])\n"
        "output_dir.mkdir(parents=True, exist_ok=True)\n"
        "(output_dir / 'observed-triage.jsonl').write_text(\n"
        "    input_path.read_text(encoding='utf-8'), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    (root / "scripts/discover_recruitment_sitemaps.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(triage)!r}).write_text(\n"
        "    '{\"registrable_domain\":\"changed.example\"}\\n', encoding='utf-8'\n"
        ")\n"
        f"Path({str(public_ats_script)!r}).write_text(\n"
        "    'raise SystemExit(99)\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    request_path = "datasets/private/crawler-execution-reviews/request.json"
    approval_path = "datasets/private/crawler-execution-reviews/approval.json"

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "request",
                "--config",
                str(config_path),
                "--request-output",
                request_path,
                "--reason",
                "two-stage bounded run",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "approve",
                "--request",
                request_path,
                "--approval-output",
                approval_path,
                "--approved-by",
                "operator@example",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        crawl_sources.main(
            [
                "--root",
                str(root),
                "execute",
                "--config",
                str(config_path),
                "--request",
                request_path,
                "--approval",
                approval_path,
                "--json",
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "completed"
    assert triage.read_text(encoding="utf-8") != approved_triage
    assert public_ats_script.read_text(encoding="utf-8") == "raise SystemExit(99)\n"
    assert (root / "datasets/raw/ats/observed-triage.jsonl").read_text(
        encoding="utf-8"
    ) == approved_triage


def test_default_runner_discards_child_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    root, _triage = _repo(tmp_path)
    child = root / "scripts/child.py"
    child.write_text(
        "import sys\n"
        "print('child-standard-output')\n"
        "print('child-standard-error', file=sys.stderr)\n",
        encoding="utf-8",
    )

    completed = crawl_sources._default_runner(
        (sys.executable, str(child)),
        cwd=root,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert completed.returncode == 0
    captured = capfd.readouterr()
    assert "child-standard-output" not in captured.out
    assert "child-standard-error" not in captured.err
