from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from careerops.api.gmail_readonly import GMAIL_READONLY_SCOPE
from careerops.api.gmail_send import GMAIL_SEND_SCOPE
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.gmail.oauth_broker import KeychainGmailOAuthBrokerHost
from careerops.infrastructure.gmail_operator import RuntimeGmailReadonlyOperatorProvider
from careerops.infrastructure.gmail_send_operator import RuntimeGmailSendOperatorProvider

_COMMAND_NAMESPACE = UUID("4f258d1b-4528-4e34-91aa-11e1684145e4")
_CANDIDATE_NAMESPACE = UUID("f7f9c10c-c37c-4df4-9e8f-530d88e565a6")
_HASH_VERSION = "careerops.gmail-local-onboarding.v1"
_HEX64 = frozenset("0123456789abcdef")
_DEFAULT_DAILY_SEND_LIMIT = 25
_MAX_DAILY_SEND_LIMIT = 500


class OnboardingError(RuntimeError):
    """Safe user-facing onboarding failure."""


class GmailOAuthOnboardingHost(Protocol):
    def local_onboarding_status(self) -> Mapping[str, object]: ...


class GmailReadonlyRegistrationProvider(Protocol):
    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        credential_handle: str,
        account_subject: str,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        publishing_status: Literal["testing", "in_production"],
        credential_store_evidence_sha256: str,
        now: datetime,
    ) -> object: ...


class GmailSendRegistrationProvider(Protocol):
    async def register_account(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        candidate_id: UUID,
        reconciliation_gmail_account_id: UUID,
        credential_handle: str,
        account_subject: str,
        credential_store_evidence_sha256: str,
        release_evidence_sha256: str,
        requested_status: Literal["disabled", "active"],
        daily_send_limit: int,
        dedicated: Literal[True],
        oauth_client_mode: Literal["byo"],
        now: datetime,
    ) -> object: ...


class OnboardingRepository(Protocol):
    def resolve_owner(self, *, expected_owner_user_id: UUID | None) -> UUID: ...

    def ensure_candidate(
        self,
        *,
        candidate_id: UUID | None,
        display_name: str | None,
    ) -> UUID: ...


@dataclass(frozen=True)
class ValidatedCredential:
    profile: Literal["readonly", "send"]
    account_subject: str
    credential_handle: str = field(repr=False)
    handle_sha256: str
    qualification_evidence_sha256: str | None
    scope: str


@dataclass(frozen=True)
class OnboardingResult:
    owner_user_id: UUID
    candidate_id: UUID
    account_subject: str
    readonly_account_id: UUID
    send_account_id: UUID
    readonly_command_id: UUID
    send_command_id: UUID
    readonly_credential_store_evidence_sha256: str
    send_credential_store_evidence_sha256: str
    send_release_evidence_sha256: str
    daily_send_limit: int
    readonly_handle_sha256: str
    send_handle_sha256: str


type TransactionFactory = Callable[[], AbstractContextManager[Connection]]
type EngineFactory = Callable[[Settings], Engine]


class PostgresOnboardingRepository:
    def __init__(self, *, transaction_factory: TransactionFactory) -> None:
        self._transaction_factory = transaction_factory

    def resolve_owner(self, *, expected_owner_user_id: UUID | None) -> UUID:
        with self._transaction_factory() as connection:
            owner_ids = tuple(
                connection.execute(
                    sa.text(
                        """
                        SELECT id
                        FROM careerops.console_users
                        WHERE disabled_at IS NULL
                        ORDER BY created_at, id
                        """
                    )
                ).scalars()
            )
        if not owner_ids:
            raise OnboardingError("no active console owner exists; bootstrap console auth first")
        if len(owner_ids) != 1:
            raise OnboardingError("expected exactly one active console owner")
        owner_user_id = _coerce_uuid(owner_ids[0], "owner_user_id")
        if expected_owner_user_id is not None and owner_user_id != expected_owner_user_id:
            raise OnboardingError("console owner did not match --owner-user-id")
        return owner_user_id

    def ensure_candidate(
        self,
        *,
        candidate_id: UUID | None,
        display_name: str | None,
    ) -> UUID:
        if candidate_id is None and display_name is None:
            raise OnboardingError(
                "candidate is required; pass --candidate-id or --candidate-display-name"
            )
        resolved_candidate_id = candidate_id or candidate_id_from_display_name(display_name)
        with self._transaction_factory() as connection:
            existing = (
                connection.execute(
                    sa.text("SELECT id FROM careerops.candidates WHERE id = :candidate_id"),
                    {"candidate_id": resolved_candidate_id},
                )
                .scalars()
                .first()
            )
            if existing is not None:
                return _coerce_uuid(existing, "candidate_id")
            if display_name is None:
                raise OnboardingError(
                    "candidate does not exist; pass --candidate-display-name to create it"
                )
            now = datetime.now(UTC)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO careerops.candidates (id, display_name, created_at, updated_at)
                    VALUES (:candidate_id, :display_name, :now, :now)
                    """
                ),
                {
                    "candidate_id": resolved_candidate_id,
                    "display_name": display_name,
                    "now": now,
                },
            )
        return resolved_candidate_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register live-qualified local Gmail OAuth handles with CareerOps."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    register = subcommands.add_parser(
        "register-local",
        help="register existing local Gmail broker handles without enabling send gates",
    )
    register.add_argument("--account-subject", required=True)
    register.add_argument("--owner-user-id", type=UUID)
    register.add_argument("--candidate-id", type=UUID)
    register.add_argument("--candidate-display-name")
    register.add_argument(
        "--daily-send-limit",
        type=int,
        default=_DEFAULT_DAILY_SEND_LIMIT,
        help="disabled Gmail send account limit to persist for future reviewed sends",
    )
    register.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    host: GmailOAuthOnboardingHost | None = None,
    repository: OnboardingRepository | None = None,
    readonly_provider: GmailReadonlyRegistrationProvider | None = None,
    send_provider: GmailSendRegistrationProvider | None = None,
    settings: Settings | None = None,
    engine_factory: EngineFactory = create_database_engine,
) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    engine: Engine | None = None
    try:
        if args.command != "register-local":
            raise OnboardingError("unknown Gmail onboarding command")
        if args.daily_send_limit < 1 or args.daily_send_limit > _MAX_DAILY_SEND_LIMIT:
            raise OnboardingError("--daily-send-limit must be between 1 and 500")
        resolved_host = host or KeychainGmailOAuthBrokerHost()
        resolved_repository = repository
        resolved_readonly_provider = readonly_provider
        resolved_send_provider = send_provider
        if (
            resolved_repository is None
            or resolved_readonly_provider is None
            or resolved_send_provider is None
        ):
            resolved_settings = settings or Settings(database_role=DatabaseCapabilityRole.API)
            if resolved_settings.database_role is not DatabaseCapabilityRole.API:
                raise OnboardingError("Gmail onboarding requires the API database role")
            engine = engine_factory(resolved_settings)
            transaction_factory = engine.begin
            if resolved_repository is None:
                resolved_repository = PostgresOnboardingRepository(
                    transaction_factory=transaction_factory
                )
            if resolved_readonly_provider is None:
                resolved_readonly_provider = RuntimeGmailReadonlyOperatorProvider(
                    transaction_factory=transaction_factory
                )
            if resolved_send_provider is None:
                resolved_send_provider = RuntimeGmailSendOperatorProvider(
                    transaction_factory=transaction_factory
                )
        result = asyncio.run(
            register_local(
                account_subject=args.account_subject,
                owner_user_id=args.owner_user_id,
                candidate_id=args.candidate_id,
                candidate_display_name=args.candidate_display_name,
                daily_send_limit=args.daily_send_limit,
                host=resolved_host,
                repository=resolved_repository,
                readonly_provider=resolved_readonly_provider,
                send_provider=resolved_send_provider,
            )
        )
    except OnboardingError as exc:
        _emit_error(str(exc), json_output=json_output)
        return 1
    except (RuntimeError, SQLAlchemyError, ValueError) as exc:
        _emit_error(_safe_runtime_error(exc), json_output=json_output)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    _emit_result(result, json_output=json_output)
    return 0


async def register_local(
    *,
    account_subject: str,
    owner_user_id: UUID | None,
    candidate_id: UUID | None,
    candidate_display_name: str | None,
    daily_send_limit: int,
    host: GmailOAuthOnboardingHost,
    repository: OnboardingRepository,
    readonly_provider: GmailReadonlyRegistrationProvider,
    send_provider: GmailSendRegistrationProvider,
) -> OnboardingResult:
    broker_status = host.local_onboarding_status()
    readonly = _validated_credential(
        broker_status,
        profile="readonly",
        expected_account_subject=account_subject,
        expected_scope=GMAIL_READONLY_SCOPE,
        require_qualification_digest=False,
    )
    send = _validated_credential(
        broker_status,
        profile="send",
        expected_account_subject=account_subject,
        expected_scope=GMAIL_SEND_SCOPE,
        require_qualification_digest=True,
    )
    resolved_owner_id = repository.resolve_owner(expected_owner_user_id=owner_user_id)
    resolved_candidate_id = repository.ensure_candidate(
        candidate_id=candidate_id,
        display_name=candidate_display_name,
    )

    readonly_evidence = _credential_store_evidence_sha256(
        "readonly",
        broker_status=broker_status,
        credential=readonly,
    )
    send_evidence = _credential_store_evidence_sha256(
        "send",
        broker_status=broker_status,
        credential=send,
    )
    readonly_command_id = _command_id(
        "readonly-register",
        {
            "account_subject": account_subject,
            "candidate_id": str(resolved_candidate_id),
            "credential_store_evidence_sha256": readonly_evidence,
            "owner_user_id": str(resolved_owner_id),
        },
    )
    now = datetime.now(UTC)
    readonly_response = await readonly_provider.register_account(
        actor_id=resolved_owner_id,
        command_id=readonly_command_id,
        candidate_id=resolved_candidate_id,
        credential_handle=readonly.credential_handle,
        account_subject=account_subject,
        dedicated=True,
        oauth_client_mode="byo",
        publishing_status="testing",
        credential_store_evidence_sha256=readonly_evidence,
        now=now,
    )
    readonly_account_id = _response_account_id(readonly_response, "readonly")

    send_release_evidence = _send_release_evidence_sha256(
        account_subject=account_subject,
        readonly_account_id=readonly_account_id,
        send_qualification_evidence_sha256=cast("str", send.qualification_evidence_sha256),
    )
    send_command_id = _command_id(
        "send-register",
        {
            "account_subject": account_subject,
            "candidate_id": str(resolved_candidate_id),
            "credential_store_evidence_sha256": send_evidence,
            "daily_send_limit": daily_send_limit,
            "owner_user_id": str(resolved_owner_id),
            "readonly_account_id": str(readonly_account_id),
            "release_evidence_sha256": send_release_evidence,
            "requested_status": "disabled",
        },
    )
    send_response = await send_provider.register_account(
        actor_id=resolved_owner_id,
        command_id=send_command_id,
        candidate_id=resolved_candidate_id,
        reconciliation_gmail_account_id=readonly_account_id,
        credential_handle=send.credential_handle,
        account_subject=account_subject,
        credential_store_evidence_sha256=send_evidence,
        release_evidence_sha256=send_release_evidence,
        requested_status="disabled",
        daily_send_limit=daily_send_limit,
        dedicated=True,
        oauth_client_mode="byo",
        now=now,
    )
    send_account_id = _response_account_id(send_response, "send")
    return OnboardingResult(
        owner_user_id=resolved_owner_id,
        candidate_id=resolved_candidate_id,
        account_subject=account_subject,
        readonly_account_id=readonly_account_id,
        send_account_id=send_account_id,
        readonly_command_id=readonly_command_id,
        send_command_id=send_command_id,
        readonly_credential_store_evidence_sha256=readonly_evidence,
        send_credential_store_evidence_sha256=send_evidence,
        send_release_evidence_sha256=send_release_evidence,
        daily_send_limit=daily_send_limit,
        readonly_handle_sha256=readonly.handle_sha256,
        send_handle_sha256=send.handle_sha256,
    )


def _validated_credential(
    broker_status: Mapping[str, object],
    *,
    profile: Literal["readonly", "send"],
    expected_account_subject: str,
    expected_scope: str,
    require_qualification_digest: bool,
) -> ValidatedCredential:
    clients = _mapping(broker_status.get("clients"), "clients")
    if clients.get(profile) is not True:
        raise OnboardingError(f"Gmail {profile} OAuth client is not configured")
    credentials = _mapping(broker_status.get("credentials"), "credentials")
    credential = _mapping(credentials.get(profile), f"credentials.{profile}")
    if credential.get("active") is not True:
        raise OnboardingError(f"Gmail {profile} credential is not active")
    if credential.get("live_qualified") is not True:
        raise OnboardingError(f"Gmail {profile} credential is not live qualified")
    account_subject = credential.get("account_subject")
    if account_subject != expected_account_subject:
        raise OnboardingError(f"Gmail {profile} account did not match --account-subject")
    scopes = credential.get("scopes")
    if not isinstance(scopes, (list, tuple)):
        raise OnboardingError(f"Gmail {profile} credential scopes are not exact")
    normalized_scopes = tuple(cast("Sequence[object]", scopes))
    if normalized_scopes != (expected_scope,):
        raise OnboardingError(f"Gmail {profile} credential scopes are not exact")
    handle = credential.get("handle")
    if not isinstance(handle, str) or not handle:
        raise OnboardingError(f"Gmail {profile} credential handle is unavailable")
    qualification = credential.get("qualification_evidence_sha256")
    if qualification is not None and not isinstance(qualification, str):
        raise OnboardingError(f"Gmail {profile} qualification evidence is invalid")
    if require_qualification_digest and not _is_sha256(qualification):
        raise OnboardingError(f"Gmail {profile} live qualification evidence is missing")
    return ValidatedCredential(
        profile=profile,
        account_subject=expected_account_subject,
        credential_handle=handle,
        handle_sha256=_sha256_text(handle),
        qualification_evidence_sha256=qualification,
        scope=expected_scope,
    )


def _credential_store_evidence_sha256(
    profile: Literal["readonly", "send"],
    *,
    broker_status: Mapping[str, object],
    credential: ValidatedCredential,
) -> str:
    clients = _mapping(broker_status.get("clients"), "clients")
    payload = {
        "account_subject": credential.account_subject,
        "active": True,
        "client_configured": clients.get(profile) is True,
        "handle_sha256": credential.handle_sha256,
        "live_qualified": True,
        "profile": profile,
        "scopes": (credential.scope,),
        "version": f"{_HASH_VERSION}.credential-store",
    }
    return _sha256_json(payload)


def _send_release_evidence_sha256(
    *,
    account_subject: str,
    readonly_account_id: UUID,
    send_qualification_evidence_sha256: str,
) -> str:
    return _sha256_json(
        {
            "account_subject": account_subject,
            "readonly_account_id": str(readonly_account_id),
            "requested_status": "disabled",
            "send_qualification_evidence_sha256": send_qualification_evidence_sha256,
            "version": f"{_HASH_VERSION}.release",
        }
    )


def _command_id(domain: str, payload: Mapping[str, object]) -> UUID:
    return uuid5(
        _COMMAND_NAMESPACE,
        json.dumps(
            {"domain": domain, "payload": payload, "version": _HASH_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _emit_result(result: OnboardingResult, *, json_output: bool) -> None:
    payload = {
        "account_subject": result.account_subject,
        "candidate_id": str(result.candidate_id),
        "daily_send_limit": result.daily_send_limit,
        "owner_user_id": str(result.owner_user_id),
        "readonly": {
            "account_id": str(result.readonly_account_id),
            "command_id": str(result.readonly_command_id),
            "credential_handle_sha256": result.readonly_handle_sha256,
            "credential_store_evidence_sha256": result.readonly_credential_store_evidence_sha256,
            "publishing_status": "testing",
        },
        "send": {
            "account_id": str(result.send_account_id),
            "command_id": str(result.send_command_id),
            "credential_handle_sha256": result.send_handle_sha256,
            "credential_store_evidence_sha256": result.send_credential_store_evidence_sha256,
            "release_evidence_sha256": result.send_release_evidence_sha256,
            "requested_status": "disabled",
        },
        "status": "registered_local",
    }
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    print(
        "registered local Gmail handles: "
        f"readonly_account_id={result.readonly_account_id} "
        f"send_account_id={result.send_account_id} "
        "send_status=disabled"
    )


def _emit_error(message: str, *, json_output: bool) -> None:
    safe_message = _bounded_error_message(message)
    if json_output:
        print(
            json.dumps(
                {"errors": [safe_message], "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return
    print(f"error: {safe_message}", file=sys.stderr)


def _safe_runtime_error(exc: RuntimeError | SQLAlchemyError | ValueError) -> str:
    if isinstance(exc, SQLAlchemyError):
        return "Gmail onboarding database operation failed"
    return "Gmail onboarding runtime operation failed"


def _bounded_error_message(message: str) -> str:
    stripped = " ".join(message.split())
    if not stripped:
        return "Gmail onboarding failed"
    if any(part in stripped.lower() for part in ("handle", "token", "secret")):
        return "Gmail onboarding failed with sensitive detail redacted"
    return stripped[:512]


def _response_account_id(response: object, profile: Literal["readonly", "send"]) -> UUID:
    account = getattr(response, "account", None)
    account_id = getattr(account, "account_id", None)
    if isinstance(account_id, UUID):
        return account_id
    raise OnboardingError(f"Gmail {profile} registration did not return an account id")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OnboardingError(f"Gmail broker status missing {label}")
    return cast("Mapping[str, object]", value)


def _coerce_uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise OnboardingError(f"database returned invalid {label}")


def candidate_id_from_display_name(display_name: str | None) -> UUID:
    if display_name is None:
        raise OnboardingError(
            "candidate is required; pass --candidate-id or --candidate-display-name"
        )
    normalized = " ".join(display_name.casefold().split())
    if not normalized:
        raise OnboardingError("--candidate-display-name must not be blank")
    return uuid5(_CANDIDATE_NAMESPACE, normalized)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX64 for char in value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, object]) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
