from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from careerops.cli import gmail_onboarding
from careerops.config import DatabaseCapabilityRole, Settings

ACCOUNT = "candidate@example.test"
OWNER_ID = UUID("0d2b2f76-7744-47e0-88c4-3fe00f3fa2f5")
CANDIDATE_ID = UUID("3c3f5d99-2959-4da9-8a0f-8654b3bd7670")
READONLY_ACCOUNT_ID = UUID("d93cc69e-1282-4fc9-b177-7e3cb3109c36")
SEND_ACCOUNT_ID = UUID("05c47656-3398-4b1e-8a3d-41efe712c16c")
QUALIFICATION_DIGEST = "a" * 64
READONLY_HANDLE = "gmail:readonly:do-not-print"
SEND_HANDLE = "gmail:send:do-not-print"


class FakeHost:
    def __init__(self, status: dict[str, object] | None = None) -> None:
        self._status = status or broker_status()

    def local_onboarding_status(self) -> dict[str, object]:
        return self._status


class FakeRepository:
    def __init__(
        self,
        *,
        owners: tuple[UUID, ...] = (OWNER_ID,),
        existing_candidates: set[UUID] | None = None,
    ) -> None:
        self.owners = owners
        self.existing_candidates = existing_candidates or {CANDIDATE_ID}
        self.created_candidates: list[tuple[UUID, str]] = []

    def resolve_owner(self, *, expected_owner_user_id: UUID | None) -> UUID:
        if not self.owners:
            raise gmail_onboarding.OnboardingError("no active console owner exists")
        if len(self.owners) != 1:
            raise gmail_onboarding.OnboardingError("expected exactly one active console owner")
        owner = self.owners[0]
        if expected_owner_user_id is not None and expected_owner_user_id != owner:
            raise gmail_onboarding.OnboardingError("console owner did not match --owner-user-id")
        return owner

    def ensure_candidate(
        self,
        *,
        candidate_id: UUID | None,
        display_name: str | None,
    ) -> UUID:
        if candidate_id is None and display_name is None:
            raise gmail_onboarding.OnboardingError("candidate is required")
        resolved = candidate_id or gmail_onboarding.candidate_id_from_display_name(display_name)
        if resolved in self.existing_candidates:
            return resolved
        if display_name is None:
            raise gmail_onboarding.OnboardingError("candidate does not exist")
        self.created_candidates.append((resolved, display_name))
        self.existing_candidates.add(resolved)
        return resolved


@dataclass
class ProviderCall:
    command_id: UUID
    credential_handle: str
    kwargs: dict[str, object]


class FakeReadonlyProvider:
    def __init__(self) -> None:
        self.calls: list[ProviderCall] = []

    async def register_account(self, **kwargs: object) -> object:
        self.calls.append(
            ProviderCall(
                command_id=cast("UUID", kwargs["command_id"]),
                credential_handle=str(kwargs["credential_handle"]),
                kwargs=dict(kwargs),
            )
        )
        return SimpleNamespace(account=SimpleNamespace(account_id=READONLY_ACCOUNT_ID))


class FakeSendProvider:
    def __init__(self) -> None:
        self.calls: list[ProviderCall] = []

    async def register_account(self, **kwargs: object) -> object:
        self.calls.append(
            ProviderCall(
                command_id=cast("UUID", kwargs["command_id"]),
                credential_handle=str(kwargs["credential_handle"]),
                kwargs=dict(kwargs),
            )
        )
        return SimpleNamespace(account=SimpleNamespace(account_id=SEND_ACCOUNT_ID))


class FailingReadonlyProvider:
    async def register_account(self, **kwargs: object) -> object:
        del kwargs
        raise RuntimeError(f"provider leaked {READONLY_HANDLE}")


def broker_status() -> dict[str, object]:
    return {
        "clients": {"readonly": True, "send": True},
        "credentials": {
            "readonly": {
                "account_subject": ACCOUNT,
                "active": True,
                "handle": READONLY_HANDLE,
                "live_qualified": True,
                "qualification_evidence_sha256": None,
                "scopes": ("https://www.googleapis.com/auth/gmail.readonly",),
            },
            "send": {
                "account_subject": ACCOUNT,
                "active": True,
                "handle": SEND_HANDLE,
                "live_qualified": True,
                "qualification_evidence_sha256": QUALIFICATION_DIGEST,
                "scopes": ("https://www.googleapis.com/auth/gmail.send",),
            },
        },
        "ok": True,
    }


def test_register_local_registers_readonly_then_disabled_send_without_printing_handles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    readonly_provider = FakeReadonlyProvider()
    send_provider = FakeSendProvider()

    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(),
        repository=FakeRepository(),
        readonly_provider=readonly_provider,
        send_provider=send_provider,
    )

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert READONLY_HANDLE not in output
    assert SEND_HANDLE not in output
    assert payload["status"] == "registered_local"
    assert payload["readonly"]["account_id"] == str(READONLY_ACCOUNT_ID)
    assert payload["send"]["account_id"] == str(SEND_ACCOUNT_ID)
    assert payload["send"]["requested_status"] == "disabled"
    assert len(payload["readonly"]["credential_handle_sha256"]) == 64
    assert len(payload["send"]["credential_handle_sha256"]) == 64
    assert readonly_provider.calls[0].credential_handle == READONLY_HANDLE
    assert send_provider.calls[0].credential_handle == SEND_HANDLE
    assert send_provider.calls[0].kwargs["reconciliation_gmail_account_id"] == READONLY_ACCOUNT_ID
    assert send_provider.calls[0].kwargs["requested_status"] == "disabled"


def test_register_local_fails_without_owner(capsys: pytest.CaptureFixture[str]) -> None:
    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(),
        repository=FakeRepository(owners=()),
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "owner" in json.loads(captured.err)["errors"][0]


def test_register_local_requires_candidate_or_display_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = gmail_onboarding.main(
        ["register-local", "--account-subject", ACCOUNT, "--json"],
        host=FakeHost(),
        repository=FakeRepository(),
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "candidate" in json.loads(captured.err)["errors"][0]


def test_register_local_creates_candidate_only_with_explicit_display_name() -> None:
    repository = FakeRepository(existing_candidates=set())

    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-display-name",
            "Cheng Wen",
        ],
        host=FakeHost(),
        repository=repository,
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 0
    assert repository.created_candidates
    assert repository.created_candidates[0][1] == "Cheng Wen"


def test_display_name_candidate_id_is_deterministic_for_replay() -> None:
    first = gmail_onboarding.candidate_id_from_display_name("  Cheng   Wen ")
    second = gmail_onboarding.candidate_id_from_display_name("cheng wen")

    assert first == second


def test_validated_credential_repr_redacts_raw_credential_handle() -> None:
    raw_handle = "gmail:readonly:opaque-handle-client-secret-value"
    credential = gmail_onboarding.ValidatedCredential(
        profile="readonly",
        account_subject=ACCOUNT,
        credential_handle=raw_handle,
        handle_sha256="b" * 64,
        qualification_evidence_sha256=None,
        scope="https://www.googleapis.com/auth/gmail.readonly",
    )

    rendered = repr(credential)

    assert (
        "handle_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'"
        in rendered
    )
    assert raw_handle not in rendered
    assert "opaque-handle" not in rendered
    assert "client-secret-value" not in rendered


def test_register_local_rejects_scope_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    status = broker_status()
    credentials = status["credentials"]
    assert isinstance(credentials, dict)
    readonly = credentials["readonly"]
    assert isinstance(readonly, dict)
    readonly["scopes"] = ("https://www.googleapis.com/auth/gmail.modify",)

    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(status),
        repository=FakeRepository(),
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "scopes are not exact" in json.loads(captured.err)["errors"][0]


def test_register_local_rejects_account_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            "other@example.com",
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(),
        repository=FakeRepository(),
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "account did not match" in json.loads(captured.err)["errors"][0]


def test_register_local_rejects_unqualified_send(capsys: pytest.CaptureFixture[str]) -> None:
    status = broker_status()
    credentials = status["credentials"]
    assert isinstance(credentials, dict)
    send = credentials["send"]
    assert isinstance(send, dict)
    send["live_qualified"] = False

    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(status),
        repository=FakeRepository(),
        readonly_provider=FakeReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "send credential is not live qualified" in json.loads(captured.err)["errors"][0]


def test_register_local_uses_deterministic_command_ids_for_replay() -> None:
    first_readonly = FakeReadonlyProvider()
    first_send = FakeSendProvider()
    second_readonly = FakeReadonlyProvider()
    second_send = FakeSendProvider()
    argv = [
        "register-local",
        "--account-subject",
        ACCOUNT,
        "--candidate-id",
        str(CANDIDATE_ID),
    ]

    assert (
        gmail_onboarding.main(
            argv,
            host=FakeHost(),
            repository=FakeRepository(),
            readonly_provider=first_readonly,
            send_provider=first_send,
        )
        == 0
    )
    assert (
        gmail_onboarding.main(
            argv,
            host=FakeHost(),
            repository=FakeRepository(),
            readonly_provider=second_readonly,
            send_provider=second_send,
        )
        == 0
    )

    assert first_readonly.calls[0].command_id == second_readonly.calls[0].command_id
    assert first_send.calls[0].command_id == second_send.calls[0].command_id


def test_send_command_id_is_bound_to_daily_send_limit() -> None:
    first_send = FakeSendProvider()
    second_send = FakeSendProvider()
    base = [
        "register-local",
        "--account-subject",
        ACCOUNT,
        "--candidate-id",
        str(CANDIDATE_ID),
    ]

    assert (
        gmail_onboarding.main(
            [*base, "--daily-send-limit", "25"],
            host=FakeHost(),
            repository=FakeRepository(),
            readonly_provider=FakeReadonlyProvider(),
            send_provider=first_send,
        )
        == 0
    )
    assert (
        gmail_onboarding.main(
            [*base, "--daily-send-limit", "26"],
            host=FakeHost(),
            repository=FakeRepository(),
            readonly_provider=FakeReadonlyProvider(),
            send_provider=second_send,
        )
        == 0
    )

    assert first_send.calls[0].command_id != second_send.calls[0].command_id


def test_credential_evidence_does_not_bind_send_qualification_digest() -> None:
    first_send = FakeSendProvider()
    second_send = FakeSendProvider()
    second_status = broker_status()
    credentials = second_status["credentials"]
    assert isinstance(credentials, dict)
    send = credentials["send"]
    assert isinstance(send, dict)
    send["qualification_evidence_sha256"] = "b" * 64
    argv = [
        "register-local",
        "--account-subject",
        ACCOUNT,
        "--candidate-id",
        str(CANDIDATE_ID),
    ]

    assert (
        gmail_onboarding.main(
            argv,
            host=FakeHost(),
            repository=FakeRepository(),
            readonly_provider=FakeReadonlyProvider(),
            send_provider=first_send,
        )
        == 0
    )
    assert (
        gmail_onboarding.main(
            argv,
            host=FakeHost(second_status),
            repository=FakeRepository(),
            readonly_provider=FakeReadonlyProvider(),
            send_provider=second_send,
        )
        == 0
    )

    assert (
        first_send.calls[0].kwargs["credential_store_evidence_sha256"]
        == second_send.calls[0].kwargs["credential_store_evidence_sha256"]
    )
    assert (
        first_send.calls[0].kwargs["release_evidence_sha256"]
        != second_send.calls[0].kwargs["release_evidence_sha256"]
    )


def test_register_local_rejects_non_api_settings_without_coercion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_engine_factory(settings: Settings):
        raise AssertionError(f"engine factory should not be called with {settings.database_role}")

    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(),
        settings=Settings(database_role=DatabaseCapabilityRole.MAILBOX),
        engine_factory=fail_engine_factory,
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "API database role" in json.loads(captured.err)["errors"][0]


def test_provider_runtime_error_is_bounded_and_does_not_leak_handle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = gmail_onboarding.main(
        [
            "register-local",
            "--account-subject",
            ACCOUNT,
            "--candidate-id",
            str(CANDIDATE_ID),
            "--json",
        ],
        host=FakeHost(),
        repository=FakeRepository(),
        readonly_provider=FailingReadonlyProvider(),
        send_provider=FakeSendProvider(),
    )

    captured = capsys.readouterr()
    output = captured.err
    assert code == 1
    assert captured.out == ""
    assert READONLY_HANDLE not in output
    assert json.loads(output)["errors"] == ["Gmail onboarding runtime operation failed"]


def test_help_does_not_touch_injected_host_or_providers(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        gmail_onboarding.main(["register-local", "--help"], host=FakeHost())

    assert exc.value.code == 0
    assert "--candidate-display-name" in capsys.readouterr().out
