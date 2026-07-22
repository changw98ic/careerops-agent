from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from careerops.application.candidate_profile import (
    CandidateJobPreferencesV1,
    CandidateMaterialBundleDraft,
    CandidateMaterialKind,
    CandidateMaterialRefV1,
    CandidateProfileDecision,
    CandidateProfileDecisionCommand,
    CandidateProfileDocumentV1,
    CandidateProfileGetQuery,
    CandidateProfileImportCommand,
    CandidateProfileListPage,
    CandidateProfileListQuery,
    CandidateProfileRepositoryError,
    CandidateProfileSnapshotDraft,
    CandidateProfileSnapshotRecord,
    CandidateSeniority,
    EmploymentType,
    WorkAuthorizationStatus,
    WorkMode,
)
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StorageError,
)
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.candidate_profile import (
    PostgresCandidateProfileRepository,
)
from careerops.infrastructure.database.content_catalog import (
    ContentCatalogError,
    PostgresContentCatalogRepository,
)
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.storage.local import LocalContentAddressedStorage

_ID_NAMESPACE = UUID("2648fcf9-112f-4c6d-8bdc-60e9dbacb860")
_MAX_DOCUMENT_BYTES = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".rtf": "application/rtf",
}


class CandidateProfileOperator(Protocol):
    def import_profile(
        self,
        *,
        actor_id: UUID,
        candidate_id: UUID,
        display_name: str,
        profile: CandidateProfileDocumentV1,
        preferences: CandidateJobPreferencesV1,
        files: tuple[tuple[CandidateMaterialKind, Path], ...],
        retention_until: datetime,
        idempotency_key: str,
        trace_id: str,
    ) -> CandidateProfileSnapshotRecord: ...

    def decide(
        self,
        command: CandidateProfileDecisionCommand,
    ) -> CandidateProfileSnapshotRecord: ...

    def get(self, query: CandidateProfileGetQuery) -> CandidateProfileSnapshotRecord: ...

    def list(self, query: CandidateProfileListQuery) -> CandidateProfileListPage: ...


class CandidateProfileOperatorUnavailable(RuntimeError):
    pass


class RuntimeCandidateProfileOperator:
    def __init__(
        self,
        *,
        engine: Engine,
        storage: LocalContentAddressedStorage,
    ) -> None:
        self._engine = engine
        self._storage = storage

    def import_profile(
        self,
        *,
        actor_id: UUID,
        candidate_id: UUID,
        display_name: str,
        profile: CandidateProfileDocumentV1,
        preferences: CandidateJobPreferencesV1,
        files: tuple[tuple[CandidateMaterialKind, Path], ...],
        retention_until: datetime,
        idempotency_key: str,
        trace_id: str,
    ) -> CandidateProfileSnapshotRecord:
        now = datetime.now(UTC)
        if retention_until <= now:
            raise ValueError("--retention-until must be in the future")
        ordered_files = _normalize_material_files(files)
        try:
            with self._engine.begin() as connection:
                catalog = PostgresContentCatalogRepository(connection)
                materials: list[CandidateMaterialRefV1] = []
                for ordinal, (kind, path) in enumerate(ordered_files, start=1):
                    media_type = _media_type(path)
                    object_id = _stable_uuid(
                        actor_id,
                        candidate_id,
                        idempotency_key,
                        f"content:{ordinal}:{kind.value}",
                    )
                    material_id = _stable_uuid(
                        actor_id,
                        candidate_id,
                        idempotency_key,
                        f"material:{ordinal}:{kind.value}",
                    )
                    with _open_regular_file(path) as stream:
                        stored = self._storage.put(
                            stream,
                            media_type=media_type,
                            classification=ContentClassification.ACCEPTED_ATTACHMENT,
                            owner=ContentOwner(resource_type="candidate", resource_id=candidate_id),
                            retention_until=retention_until,
                        )
                    catalog.register(stored, object_id)
                    materials.append(
                        CandidateMaterialRefV1(
                            material_id=material_id,
                            content_object_id=object_id,
                            kind=kind,
                            label=_material_label(kind),
                            filename=path.name,
                            media_type=media_type,
                            sha256=stored.sha256,
                            object_key=stored.object_key,
                            byte_size=stored.byte_size,
                        )
                    )
                bundle = CandidateMaterialBundleDraft(
                    owner_user_id=actor_id,
                    candidate_id=candidate_id,
                    bundle_id=_stable_uuid(
                        actor_id,
                        candidate_id,
                        idempotency_key,
                        "material-bundle",
                    ),
                    materials=tuple(materials),
                )
                snapshot = CandidateProfileSnapshotDraft(
                    owner_user_id=actor_id,
                    candidate_id=candidate_id,
                    profile=profile,
                    preferences=preferences,
                    material_bundle=bundle,
                )
                command = CandidateProfileImportCommand(
                    snapshot=snapshot,
                    profile_version_id=_stable_uuid(
                        actor_id,
                        candidate_id,
                        idempotency_key,
                        "profile-version",
                    ),
                    display_name=display_name,
                    idempotency_key=idempotency_key,
                    trace_id=trace_id,
                )
                return PostgresCandidateProfileRepository(connection).import_snapshot(command)
        except SQLAlchemyError:
            raise CandidateProfileOperatorUnavailable(
                "candidate profile database is unavailable"
            ) from None

    def decide(
        self,
        command: CandidateProfileDecisionCommand,
    ) -> CandidateProfileSnapshotRecord:
        try:
            with self._engine.begin() as connection:
                return PostgresCandidateProfileRepository(connection).decide(command)
        except SQLAlchemyError:
            raise CandidateProfileOperatorUnavailable(
                "candidate profile database is unavailable"
            ) from None

    def get(self, query: CandidateProfileGetQuery) -> CandidateProfileSnapshotRecord:
        try:
            with self._engine.begin() as connection:
                return PostgresCandidateProfileRepository(connection).get(query)
        except SQLAlchemyError:
            raise CandidateProfileOperatorUnavailable(
                "candidate profile database is unavailable"
            ) from None

    def list(self, query: CandidateProfileListQuery) -> CandidateProfileListPage:
        try:
            with self._engine.begin() as connection:
                return PostgresCandidateProfileRepository(connection).list(query)
        except SQLAlchemyError:
            raise CandidateProfileOperatorUnavailable(
                "candidate profile database is unavailable"
            ) from None


def create_runtime_candidate_profile_operator(
    settings: Settings,
) -> RuntimeCandidateProfileOperator:
    if settings.database_role is not DatabaseCapabilityRole.API:
        raise CandidateProfileOperatorUnavailable(
            "candidate profile CLI requires the API database capability"
        )
    return RuntimeCandidateProfileOperator(
        engine=create_database_engine(settings),
        storage=LocalContentAddressedStorage(
            settings.storage_root,
            max_object_bytes=settings.storage_max_object_bytes,
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import, review, and inspect immutable candidate profile snapshots.",
    )
    parser.add_argument("--actor-id", type=UUID, required=True)
    parser.add_argument("--candidate-id", type=UUID, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)

    import_command = subcommands.add_parser("import")
    import_command.add_argument("--display-name", required=True)
    import_command.add_argument("--profile", type=Path, required=True)
    import_command.add_argument("--preferences", type=Path, required=True)
    import_command.add_argument("--resume", type=Path, required=True)
    import_command.add_argument("--cover-letter", type=Path)
    import_command.add_argument("--portfolio", type=Path, action="append", default=[])
    import_command.add_argument("--certificate", type=Path, action="append", default=[])
    import_command.add_argument("--retention-until", type=_parse_aware_datetime, required=True)
    import_command.add_argument("--idempotency-key", required=True)
    import_command.add_argument("--trace-id", required=True)
    import_command.add_argument("--json", action="store_true")

    for decision in ("approve", "reject"):
        decision_command = subcommands.add_parser(decision)
        decision_command.add_argument("--profile-version-id", type=UUID, required=True)
        decision_command.add_argument("--snapshot-sha256", required=True)
        decision_command.add_argument("--reason", required=True)
        decision_command.add_argument("--idempotency-key", required=True)
        decision_command.add_argument("--trace-id", required=True)
        decision_command.add_argument("--json", action="store_true")

    list_command = subcommands.add_parser("list")
    list_command.add_argument("--limit", type=int, default=20)
    list_command.add_argument("--json", action="store_true")

    show_command = subcommands.add_parser("show")
    show_command.add_argument("--profile-version-id", type=UUID, required=True)
    show_command.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: CandidateProfileOperator | None = None,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    try:
        resolved_provider = provider or create_runtime_candidate_profile_operator(Settings())
        result = _run(args, provider=resolved_provider)
    except (
        CandidateProfileOperatorUnavailable,
        CandidateProfileRepositoryError,
        ContentCatalogError,
        StorageError,
        OSError,
        ValueError,
    ) as error:
        _emit_error(str(error), json_output=json_output)
        return 1
    _emit(result, json_output=json_output)
    return 0


def _run(
    args: argparse.Namespace,
    *,
    provider: CandidateProfileOperator,
) -> CandidateProfileSnapshotRecord | CandidateProfileListPage:
    if args.command == "import":
        profile = _profile_from_mapping(_load_json_object(args.profile, "profile"))
        preferences = _preferences_from_mapping(
            _load_json_object(args.preferences, "preferences")
        )
        files: list[tuple[CandidateMaterialKind, Path]] = [
            (CandidateMaterialKind.RESUME, args.resume)
        ]
        if args.cover_letter is not None:
            files.append((CandidateMaterialKind.COVER_LETTER, args.cover_letter))
        files.extend((CandidateMaterialKind.PORTFOLIO, path) for path in args.portfolio)
        files.extend((CandidateMaterialKind.CERTIFICATE, path) for path in args.certificate)
        return provider.import_profile(
            actor_id=args.actor_id,
            candidate_id=args.candidate_id,
            display_name=args.display_name,
            profile=profile,
            preferences=preferences,
            files=tuple(files),
            retention_until=args.retention_until,
            idempotency_key=args.idempotency_key,
            trace_id=args.trace_id,
        )
    if args.command in {"approve", "reject"}:
        return provider.decide(
            CandidateProfileDecisionCommand(
                actor_id=args.actor_id,
                candidate_id=args.candidate_id,
                profile_version_id=args.profile_version_id,
                snapshot_sha256=args.snapshot_sha256,
                decision=CandidateProfileDecision(args.command),
                reason=args.reason,
                idempotency_key=args.idempotency_key,
                trace_id=args.trace_id,
            )
        )
    if args.command == "list":
        return provider.list(
            CandidateProfileListQuery(
                actor_id=args.actor_id,
                candidate_id=args.candidate_id,
                limit=args.limit,
            )
        )
    if args.command == "show":
        return provider.get(
            CandidateProfileGetQuery(
                actor_id=args.actor_id,
                candidate_id=args.candidate_id,
                profile_version_id=args.profile_version_id,
            )
        )
    raise ValueError("unknown candidate profile command")


def _load_json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        with _open_regular_file(path) as stream:
            payload = stream.read(_MAX_DOCUMENT_BYTES + 1)
    except OSError as error:
        raise ValueError(f"{label} document could not be read safely") from error
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise ValueError(f"{label} document exceeds {_MAX_DOCUMENT_BYTES} bytes")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} document is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} document must be a JSON object")
    return cast("Mapping[str, object]", value)


def _profile_from_mapping(value: Mapping[str, object]) -> CandidateProfileDocumentV1:
    allowed = {
        "schema_version",
        "skills",
        "role_titles",
        "seniority",
        "years_experience",
        "industries",
        "languages",
        "work_authorization",
        "requires_sponsorship",
    }
    _require_exact_keys(value, allowed, "profile")
    if value.get("schema_version") != "candidate-profile.v1":
        raise ValueError("profile schema_version must be candidate-profile.v1")
    years_experience = value.get("years_experience")
    if years_experience is not None and (
        not isinstance(years_experience, int) or isinstance(years_experience, bool)
    ):
        raise ValueError("profile years_experience must be an integer or null")
    return CandidateProfileDocumentV1(
        skills=_string_tuple(value.get("skills"), "profile skills"),
        role_titles=_string_tuple(value.get("role_titles"), "profile role_titles"),
        seniority=CandidateSeniority(str(value.get("seniority", "unknown"))),
        years_experience=years_experience,
        industries=_string_tuple(value.get("industries", []), "profile industries"),
        languages=_string_tuple(value.get("languages", []), "profile languages"),
        work_authorization=WorkAuthorizationStatus(
            str(value.get("work_authorization", "unknown"))
        ),
        requires_sponsorship=_optional_bool(
            value.get("requires_sponsorship"),
            "profile requires_sponsorship",
        ),
    )


def _preferences_from_mapping(value: Mapping[str, object]) -> CandidateJobPreferencesV1:
    allowed = {
        "schema_version",
        "target_titles",
        "required_skills",
        "preferred_skills",
        "excluded_skills",
        "required_keywords",
        "preferred_keywords",
        "excluded_keywords",
        "allowed_locations",
        "excluded_locations",
        "work_modes",
        "employment_types",
        "seniority_levels",
        "minimum_salary",
        "salary_currency",
        "sponsorship_allowed",
        "allowed_companies",
        "excluded_companies",
        "allowed_industries",
        "excluded_industries",
        "minimum_match_score",
    }
    _require_exact_keys(value, allowed, "preferences")
    if value.get("schema_version") != "candidate-job-preferences.v1":
        raise ValueError(
            "preferences schema_version must be candidate-job-preferences.v1"
        )
    salary = value.get("minimum_salary")
    if salary is not None and (not isinstance(salary, int) or isinstance(salary, bool)):
        raise ValueError("preferences minimum_salary must be an integer or null")
    score = value.get("minimum_match_score", 0.5)
    if not isinstance(score, int | float) or isinstance(score, bool):
        raise ValueError("preferences minimum_match_score must be numeric")
    return CandidateJobPreferencesV1(
        target_titles=_string_tuple(value.get("target_titles"), "preferences target_titles"),
        required_skills=_string_tuple(value.get("required_skills", []), "required_skills"),
        preferred_skills=_string_tuple(value.get("preferred_skills", []), "preferred_skills"),
        excluded_skills=_string_tuple(value.get("excluded_skills", []), "excluded_skills"),
        required_keywords=_string_tuple(
            value.get("required_keywords", []), "required_keywords"
        ),
        preferred_keywords=_string_tuple(
            value.get("preferred_keywords", []), "preferred_keywords"
        ),
        excluded_keywords=_string_tuple(
            value.get("excluded_keywords", []), "excluded_keywords"
        ),
        allowed_locations=_string_tuple(
            value.get("allowed_locations", []), "allowed_locations"
        ),
        excluded_locations=_string_tuple(
            value.get("excluded_locations", []), "excluded_locations"
        ),
        work_modes=tuple(
            WorkMode(item)
            for item in _string_tuple(value.get("work_modes", []), "work_modes")
        ),
        employment_types=tuple(
            EmploymentType(item)
            for item in _string_tuple(value.get("employment_types", []), "employment_types")
        ),
        seniority_levels=tuple(
            CandidateSeniority(item)
            for item in _string_tuple(value.get("seniority_levels", []), "seniority_levels")
        ),
        minimum_salary=salary,
        salary_currency=_optional_string(value.get("salary_currency"), "salary_currency"),
        sponsorship_allowed=_optional_bool(
            value.get("sponsorship_allowed"), "sponsorship_allowed"
        ),
        allowed_companies=_string_tuple(value.get("allowed_companies", []), "allowed_companies"),
        excluded_companies=_string_tuple(
            value.get("excluded_companies", []), "excluded_companies"
        ),
        allowed_industries=_string_tuple(
            value.get("allowed_industries", []), "allowed_industries"
        ),
        excluded_industries=_string_tuple(
            value.get("excluded_industries", []), "excluded_industries"
        ),
        minimum_match_score=float(score),
    )


def _require_exact_keys(value: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} document contains unsupported fields")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of strings")
    items = cast("list[object]", value)
    if any(not isinstance(item, str) for item in items):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(cast("list[str]", items))


def _optional_bool(value: object, label: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be true, false, or null")


def _optional_string(value: object, label: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{label} must be a string or null")


def _normalize_material_files(
    files: tuple[tuple[CandidateMaterialKind, Path], ...],
) -> tuple[tuple[CandidateMaterialKind, Path], ...]:
    if sum(kind is CandidateMaterialKind.RESUME for kind, _ in files) != 1:
        raise ValueError("import requires exactly one resume")
    return tuple(sorted(files, key=lambda item: (item[0].value, item[1].name)))


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise ValueError("candidate material file type is unsupported")
    return media_type


def _material_label(kind: CandidateMaterialKind) -> str:
    return {
        CandidateMaterialKind.RESUME: "简历",
        CandidateMaterialKind.COVER_LETTER: "求职信",
        CandidateMaterialKind.PORTFOLIO: "作品集",
        CandidateMaterialKind.CERTIFICATE: "证书",
        CandidateMaterialKind.OTHER: "其他材料",
    }[kind]


def _stable_uuid(
    actor_id: UUID,
    candidate_id: UUID,
    idempotency_key: str,
    suffix: str,
) -> UUID:
    return uuid5(_ID_NAMESPACE, f"{actor_id}:{candidate_id}:{idempotency_key}:{suffix}")


@contextmanager
def _open_regular_file(path: Path) -> Generator[BinaryIO, None, None]:
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("input must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _record_payload(record: CandidateProfileSnapshotRecord) -> dict[str, JsonValue]:
    return {
        "owner_user_id": str(record.owner_user_id),
        "candidate_id": str(record.candidate_id),
        "profile_version_id": str(record.profile_version_id),
        "profile_version": record.profile_version,
        "material_bundle_id": str(record.material_bundle_id),
        "material_bundle_version": record.material_bundle_version,
        "profile": cast(JsonValue, dict(record.profile)),
        "preferences": cast(JsonValue, dict(record.preferences)),
        "materials": cast(JsonValue, [dict(item) for item in record.materials]),
        "material_bundle_sha256": record.material_bundle_sha256,
        "snapshot_sha256": record.snapshot_sha256,
        "decision": None if record.decision is None else record.decision.value,
        "newly_created": record.newly_created,
    }


def _emit(
    result: CandidateProfileSnapshotRecord | CandidateProfileListPage,
    *,
    json_output: bool,
) -> None:
    if isinstance(result, CandidateProfileListPage):
        payload: dict[str, JsonValue] = {
            "count": result.count,
            "items": [_record_payload(item) for item in result.items],
        }
        message = f"listed {result.count} candidate profile snapshot(s)"
    else:
        payload = _record_payload(result)
        message = (
            f"candidate profile {result.profile_version_id} "
            f"is {result.decision.value if result.decision is not None else 'pending_review'}"
        )
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(message)


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps({"errors": [message], "ok": False}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
    else:
        print(f"ERROR: {message}", file=sys.stderr)


__all__ = [
    "CandidateProfileOperator",
    "CandidateProfileOperatorUnavailable",
    "RuntimeCandidateProfileOperator",
    "create_runtime_candidate_profile_operator",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
