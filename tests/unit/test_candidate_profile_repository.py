from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.application.candidate_profile import (
    ApprovedCandidateProfileQuery,
    CandidateJobPreferencesV1,
    CandidateMaterialBundleDraft,
    CandidateMaterialKind,
    CandidateMaterialRefV1,
    CandidateProfileDecision,
    CandidateProfileDecisionCommand,
    CandidateProfileDocumentV1,
    CandidateProfileGetQuery,
    CandidateProfileImportCommand,
    CandidateProfileListQuery,
    CandidateProfileRepositoryError,
    CandidateProfileSnapshotDraft,
)
from careerops.infrastructure.database.candidate_profile import (
    PostgresCandidateProfileRepository,
    candidate_profile_decide_statement,
    candidate_profile_get_approved_statement,
    candidate_profile_get_statement,
    candidate_profile_import_statement,
    candidate_profile_list_statement,
    compile_candidate_profile_query_for_test,
)

ACTOR_ID = UUID(int=1)
CANDIDATE_ID = UUID(int=2)
PROFILE_VERSION_ID = UUID(int=3)
BUNDLE_ID = UUID(int=4)
SNAPSHOT = "b" * 64


def import_command() -> CandidateProfileImportCommand:
    digest = "a" * 64
    bundle = CandidateMaterialBundleDraft(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        bundle_id=BUNDLE_ID,
        materials=(
            CandidateMaterialRefV1(
                material_id=UUID(int=5),
                content_object_id=UUID(int=6),
                kind=CandidateMaterialKind.RESUME,
                label="简历",
                filename="resume.pdf",
                media_type="application/pdf",
                sha256=digest,
                object_key=f"sha256/aa/aa/{digest}",
                byte_size=1200,
            ),
        ),
    )
    return CandidateProfileImportCommand(
        snapshot=CandidateProfileSnapshotDraft(
            owner_user_id=ACTOR_ID,
            candidate_id=CANDIDATE_ID,
            profile=CandidateProfileDocumentV1(
                skills=("python",),
                role_titles=("engineer",),
            ),
            preferences=CandidateJobPreferencesV1(target_titles=("engineer",)),
            material_bundle=bundle,
        ),
        profile_version_id=PROFILE_VERSION_ID,
        display_name="Candidate",
        idempotency_key="profile-import-1",
        trace_id="trace-1",
    )


def decision_command() -> CandidateProfileDecisionCommand:
    command = import_command()
    return CandidateProfileDecisionCommand(
        actor_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        profile_version_id=PROFILE_VERSION_ID,
        snapshot_sha256=command.snapshot.snapshot_sha256,
        decision=CandidateProfileDecision.APPROVE,
        reason="approved exact snapshot",
        idempotency_key="profile-approve-1",
        trace_id="trace-2",
    )


def database_record(*, decision: str | None = None) -> dict[str, object]:
    command = import_command()
    return {
        "owner_user_id": str(ACTOR_ID),
        "candidate_id": str(CANDIDATE_ID),
        "profile_version_id": str(PROFILE_VERSION_ID),
        "profile_version": 1,
        "material_bundle_id": str(BUNDLE_ID),
        "material_bundle_version": 1,
        "profile": command.snapshot.profile.to_json(),
        "preferences": command.snapshot.preferences.to_json(),
        "materials": command.snapshot.material_bundle.manifest_json,
        "material_bundle_sha256": command.snapshot.material_bundle.bundle_sha256,
        "snapshot_sha256": command.snapshot.snapshot_sha256,
        "decision": decision,
        "newly_created": True,
    }


def test_repository_requires_explicit_transaction() -> None:
    class NoTransaction:
        def in_transaction(self) -> bool:
            return False

    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresCandidateProfileRepository(cast("Connection", NoTransaction()))


def test_statement_builders_only_call_candidate_profile_definer_functions() -> None:
    command = import_command()
    statements = (
        candidate_profile_import_statement(command),
        candidate_profile_decide_statement(decision_command()),
        candidate_profile_get_statement(
            CandidateProfileGetQuery(ACTOR_ID, CANDIDATE_ID, PROFILE_VERSION_ID)
        ),
        candidate_profile_list_statement(CandidateProfileListQuery(ACTOR_ID, CANDIDATE_ID)),
        candidate_profile_get_approved_statement(
            ApprovedCandidateProfileQuery(ACTOR_ID, CANDIDATE_ID)
        ),
    )

    rendered = "\n".join(
        compile_candidate_profile_query_for_test(item, literal_binds=False)
        for item in statements
    )

    for function_name in (
        "careerops.candidate_profile_import",
        "careerops.candidate_profile_decide",
        "careerops.candidate_profile_get",
        "careerops.candidate_profile_list",
        "careerops.candidate_profile_get_approved",
    ):
        assert function_name in rendered
    assert "p_material_bundle_canonical_json" in rendered
    assert "p_snapshot_canonical_json" in rendered
    assert "INSERT INTO careerops." not in rendered
    assert "UPDATE careerops." not in rendered


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class FakeConnection:
    def __init__(self, values: list[Mapping[str, object]]) -> None:
        self._values = iter(values)
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
        self.statements.append(
            compile_candidate_profile_query_for_test(
                cast("sa.ClauseElement", statement), literal_binds=False
            )
        )
        return FakeScalarResult(next(self._values))


def test_repository_maps_import_decision_get_list_and_approved_records() -> None:
    pending = database_record()
    approved = database_record(decision="approve")
    connection = FakeConnection(
        [pending, approved, approved, {"items": [pending, approved], "count": 2}, approved]
    )
    repository = PostgresCandidateProfileRepository(cast("Connection", connection))

    assert repository.import_snapshot(import_command()).decision is None
    assert repository.decide(decision_command()).decision is CandidateProfileDecision.APPROVE
    assert repository.get(
        CandidateProfileGetQuery(ACTOR_ID, CANDIDATE_ID, PROFILE_VERSION_ID)
    ).snapshot_sha256 == import_command().snapshot.snapshot_sha256
    assert repository.list(CandidateProfileListQuery(ACTOR_ID, CANDIDATE_ID)).count == 2
    assert repository.get_approved(
        ApprovedCandidateProfileQuery(ACTOR_ID, CANDIDATE_ID)
    ).decision is CandidateProfileDecision.APPROVE


@pytest.mark.parametrize(
    ("sqlstate", "reason_code"),
    [
        ("22023", "CANDIDATE_PROFILE_INPUT_INVALID"),
        ("23503", "CANDIDATE_PROFILE_NOT_FOUND"),
        ("23505", "CANDIDATE_PROFILE_IDEMPOTENCY_CONFLICT"),
        ("23514", "CANDIDATE_PROFILE_STATE_CONFLICT"),
    ],
)
def test_repository_sanitizes_database_errors(sqlstate: str, reason_code: str) -> None:
    class OriginError(Exception):
        def __init__(self, state: str) -> None:
            super().__init__("leaked database detail")
            self.sqlstate = state

    class FailingConnection:
        def in_transaction(self) -> bool:
            return True

        def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
            del statement
            raise DBAPIError("select leaked_secret", {"secret": "value"}, OriginError(sqlstate))

    with pytest.raises(CandidateProfileRepositoryError) as error:
        PostgresCandidateProfileRepository(cast("Connection", FailingConnection())).get(
            CandidateProfileGetQuery(ACTOR_ID, CANDIDATE_ID, PROFILE_VERSION_ID)
        )

    assert error.value.reason_code == reason_code
    assert error.value.sqlstate == sqlstate
    assert "secret" not in str(error.value)
