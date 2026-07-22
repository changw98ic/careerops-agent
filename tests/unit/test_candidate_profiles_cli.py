from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from careerops.application.candidate_profile import (
    CandidateProfileDecision,
    CandidateProfileDecisionCommand,
    CandidateProfileDocumentV1,
    CandidateProfileGetQuery,
    CandidateProfileListPage,
    CandidateProfileListQuery,
    CandidateProfileSnapshotRecord,
)
from careerops.cli.candidate_profiles import main

ACTOR_ID = UUID(int=1)
CANDIDATE_ID = UUID(int=2)
PROFILE_VERSION_ID = UUID(int=3)
BUNDLE_ID = UUID(int=4)
SNAPSHOT = "a" * 64


def record(*, decision: CandidateProfileDecision | None = None) -> CandidateProfileSnapshotRecord:
    return CandidateProfileSnapshotRecord(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        profile_version_id=PROFILE_VERSION_ID,
        profile_version=1,
        material_bundle_id=BUNDLE_ID,
        material_bundle_version=1,
        profile={"schema_version": "candidate-profile.v1", "skills": ["python"]},
        preferences={
            "schema_version": "candidate-job-preferences.v1",
            "target_titles": ["engineer"],
        },
        materials=(
            {
                "kind": "resume",
                "filename": "resume.pdf",
                "sha256": "b" * 64,
            },
        ),
        material_bundle_sha256="c" * 64,
        snapshot_sha256=SNAPSHOT,
        decision=decision,
        newly_created=True,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.imported: dict[str, object] | None = None
        self.decision: CandidateProfileDecisionCommand | None = None
        self.get_query: CandidateProfileGetQuery | None = None
        self.list_query: CandidateProfileListQuery | None = None

    def import_profile(self, **kwargs: object) -> CandidateProfileSnapshotRecord:
        self.imported = kwargs
        return record()

    def decide(
        self,
        command: CandidateProfileDecisionCommand,
    ) -> CandidateProfileSnapshotRecord:
        self.decision = command
        return record(decision=command.decision)

    def get(self, query: CandidateProfileGetQuery) -> CandidateProfileSnapshotRecord:
        self.get_query = query
        return record()

    def list(self, query: CandidateProfileListQuery) -> CandidateProfileListPage:
        self.list_query = query
        return CandidateProfileListPage(items=(record(),), count=1)


def _base_args() -> list[str]:
    return ["--actor-id", str(ACTOR_ID), "--candidate-id", str(CANDIDATE_ID)]


def test_import_parses_strict_documents_and_forwards_local_materials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-profile.v1",
                "skills": ["Python"],
                "role_titles": ["AI Engineer"],
            }
        ),
        encoding="utf-8",
    )
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-job-preferences.v1",
                "target_titles": ["AI Engineer"],
            }
        ),
        encoding="utf-8",
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.7 test")
    provider = FakeProvider()

    assert (
        main(
            [
                *_base_args(),
                "import",
                "--display-name",
                "Candidate",
                "--profile",
                str(profile_path),
                "--preferences",
                str(preferences_path),
                "--resume",
                str(resume_path),
                "--retention-until",
                "2030-01-01T00:00:00Z",
                "--idempotency-key",
                "profile-import-1",
                "--trace-id",
                "trace-1",
                "--json",
            ],
            provider=provider,
        )
        == 0
    )

    assert provider.imported is not None
    assert provider.imported["display_name"] == "Candidate"
    imported_profile = provider.imported["profile"]
    assert isinstance(imported_profile, CandidateProfileDocumentV1)
    assert imported_profile.skills == ("python",)
    output = capsys.readouterr().out
    assert str(tmp_path) not in output
    assert json.loads(output)["snapshot_sha256"] == SNAPSHOT


def test_approve_binds_exact_profile_version_and_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider()

    result = main(
        [
            *_base_args(),
            "approve",
            "--profile-version-id",
            str(PROFILE_VERSION_ID),
            "--snapshot-sha256",
            SNAPSHOT,
            "--reason",
            "reviewed exact snapshot",
            "--idempotency-key",
            "profile-approve-1",
            "--trace-id",
            "trace-2",
            "--json",
        ],
        provider=provider,
    )

    assert result == 0
    assert provider.decision is not None
    assert provider.decision.profile_version_id == PROFILE_VERSION_ID
    assert provider.decision.snapshot_sha256 == SNAPSHOT
    assert provider.decision.decision is CandidateProfileDecision.APPROVE
    assert json.loads(capsys.readouterr().out)["decision"] == "approve"


def test_list_and_show_are_owner_and_candidate_scoped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = FakeProvider()

    assert main([*_base_args(), "list", "--limit", "5", "--json"], provider=provider) == 0
    assert provider.list_query == CandidateProfileListQuery(ACTOR_ID, CANDIDATE_ID, 5)
    assert json.loads(capsys.readouterr().out)["count"] == 1

    assert (
        main(
            [*_base_args(), "show", "--profile-version-id", str(PROFILE_VERSION_ID), "--json"],
            provider=provider,
        )
        == 0
    )
    assert provider.get_query == CandidateProfileGetQuery(
        ACTOR_ID,
        CANDIDATE_ID,
        PROFILE_VERSION_ID,
    )
    assert json.loads(capsys.readouterr().out)["profile_version_id"] == str(
        PROFILE_VERSION_ID
    )


def test_import_rejects_unknown_profile_fields_without_calling_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-profile.v1",
                "skills": ["python"],
                "role_titles": [],
                "secret": "must not be accepted",
            }
        ),
        encoding="utf-8",
    )
    preferences_path = tmp_path / "preferences.json"
    preferences_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-job-preferences.v1",
                "target_titles": ["engineer"],
            }
        ),
        encoding="utf-8",
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"test")
    provider = FakeProvider()

    result = main(
        [
            *_base_args(),
            "import",
            "--display-name",
            "Candidate",
            "--profile",
            str(profile_path),
            "--preferences",
            str(preferences_path),
            "--resume",
            str(resume_path),
            "--retention-until",
            "2030-01-01T00:00:00Z",
            "--idempotency-key",
            "profile-import-2",
            "--trace-id",
            "trace-3",
        ],
        provider=provider,
    )

    assert result == 1
    assert provider.imported is None
    assert "unsupported fields" in capsys.readouterr().err
