import os

import pytest
from pydantic import ValidationError

from careerops.config import (
    DatabaseCapabilityRole,
    DeploymentMode,
    RuntimeEnvironment,
    Settings,
)


def test_safe_defaults_are_loopback_and_capability_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("CAREEROPS_"):
            monkeypatch.delenv(name)
    settings = Settings.model_validate({"_env_file": None})

    assert settings.environment is RuntimeEnvironment.DEVELOPMENT
    assert settings.deployment_mode is DeploymentMode.LOOPBACK
    assert settings.bind_host == "127.0.0.1"
    assert settings.database_url.get_secret_value().startswith(
        "postgresql+psycopg://careerops_runtime@"
    )
    assert settings.database_role is DatabaseCapabilityRole.API
    assert settings.redis_url.get_secret_value() == "redis://127.0.0.1:6379/0"
    assert settings.temporal_address == "127.0.0.1:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "careerops-m0"
    assert settings.readiness_timeout_seconds == 1.0
    assert settings.storage_root.as_posix() == "data/objects"
    assert settings.storage_max_object_bytes == 10 * 1024 * 1024
    assert settings.model_provider == "disabled"
    assert settings.google_oauth_enabled is False
    assert settings.external_writes_enabled is False
    assert settings.auto_send_enabled is False
    assert settings.auto_submit_enabled is False
    assert settings.gmail_send_enabled is False
    assert settings.gmail_send_release_attested is False
    assert settings.gmail_send_in_production_attested is False
    assert settings.gmail_send_runtime_enabled is False
    assert settings.greenhouse_submit_enabled is False
    assert settings.greenhouse_submit_release_attested is False
    assert settings.greenhouse_submit_in_production_attested is False
    assert settings.greenhouse_submit_runtime_enabled is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"bind_host": "0.0.0.0"}, "bounded compose-loopback"),
        ({"database_url": "sqlite:///careerops.db"}, "PostgreSQL psycopg"),
        ({"redis_url": "http://redis:6379"}, "Redis URL"),
        ({"temporal_address": "https://temporal:7233"}, "host:port"),
        ({"model_provider": "external"}, "privacy qualification"),
        ({"gmail_send_enabled": True}, "Gmail send requires"),
        ({"gmail_send_release_attested": True}, "Gmail send requires"),
        ({"greenhouse_submit_enabled": True}, "Greenhouse submit requires"),
        ({"greenhouse_submit_release_attested": True}, "Greenhouse submit requires"),
        ({"storage_max_object_bytes": 0}, "greater than or equal to 1"),
        ({"storage_root": "."}, "dedicated directory"),
    ],
)
def test_unreleased_capabilities_fail_startup(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(override)


def test_environment_values_are_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAREEROPS_ENVIRONMENT", "production")
    monkeypatch.setenv("CAREEROPS_CONSOLE_COOKIE_SECURE", "true")
    monkeypatch.setenv("CAREEROPS_CONSOLE_ALLOWED_HOSTS", '["careerops.example"]')
    monkeypatch.setenv("CAREEROPS_CONSOLE_ALLOWED_ORIGINS", '["https://careerops.example"]')
    settings = Settings()

    assert settings.environment is RuntimeEnvironment.PRODUCTION


def test_production_rejects_insecure_console_cookies() -> None:
    with pytest.raises(ValidationError, match="must be Secure"):
        Settings.model_validate({"environment": "production"})


def test_database_password_is_redacted_from_settings_repr() -> None:
    settings = Settings.model_validate(
        {"database_url": "postgresql+psycopg://careerops:top-secret@db/careerops"}
    )

    assert "top-secret" not in repr(settings)
    assert str(settings.database_url) == "**********"


def test_compose_mode_allows_container_bind_without_weakening_default() -> None:
    settings = Settings.model_validate(
        {"deployment_mode": "compose_loopback", "bind_host": "0.0.0.0"}
    )

    assert settings.deployment_mode is DeploymentMode.COMPOSE_LOOPBACK
    assert settings.bind_host == "0.0.0.0"


@pytest.mark.parametrize("role", ["side_effect", "api; RESET ROLE", "careerops_api"])
def test_database_role_rejects_unreleased_or_unbounded_values(role: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"database_role": role})


def test_google_oauth_readonly_gate_allows_dev_without_owning_write_gates() -> None:
    settings = Settings.model_validate({"google_oauth_enabled": True})

    assert settings.google_oauth_enabled is True
    assert settings.external_writes_enabled is False
    assert settings.auto_send_enabled is False

    write_settings = Settings.model_validate(
        {
            "google_oauth_enabled": True,
            "external_writes_enabled": True,
        }
    )
    assert write_settings.google_oauth_enabled is True
    assert write_settings.external_writes_enabled is True
    assert write_settings.gmail_send_enabled is False


def test_external_write_flags_do_not_force_gmail_send_release_gate() -> None:
    settings = Settings.model_validate(
        {
            "external_writes_enabled": True,
            "auto_send_enabled": True,
        }
    )

    assert settings.external_writes_enabled is True
    assert settings.auto_send_enabled is True
    assert settings.gmail_send_enabled is False
    assert settings.gmail_send_release_attested is False


def test_email_auto_send_does_not_activate_greenhouse_submit() -> None:
    settings = Settings.model_validate(
        {
            "external_writes_enabled": True,
            "auto_send_enabled": True,
        }
    )

    assert settings.auto_send_enabled is True
    assert settings.auto_submit_enabled is False
    assert settings.greenhouse_submit_runtime_enabled is False


def test_gmail_send_requires_full_release_gate_and_absolute_broker_socket() -> None:
    settings = Settings.model_validate(
        {
            "external_writes_enabled": True,
            "auto_send_enabled": True,
            "google_oauth_enabled": True,
            "gmail_send_enabled": True,
            "gmail_send_release_attested": True,
        }
    )

    assert settings.gmail_send_enabled is True
    assert settings.gmail_send_runtime_enabled is True
    assert settings.gmail_send_broker_socket is not None
    assert settings.gmail_send_broker_socket.is_absolute()
    assert settings.mailbox_broker_socket is not None
    assert settings.mailbox_broker_socket.is_absolute()
    assert settings.gmail_send_attachment_broker_socket is not None
    assert settings.gmail_send_attachment_broker_socket.is_absolute()

    with pytest.raises(ValidationError, match="broker socket must be absolute"):
        Settings.model_validate(
            {
                "external_writes_enabled": True,
                "auto_send_enabled": True,
                "google_oauth_enabled": True,
                "gmail_send_enabled": True,
                "gmail_send_release_attested": True,
                "gmail_send_broker_socket": "relative.sock",
            }
        )
    with pytest.raises(ValidationError, match="attachment broker socket must be absolute"):
        Settings.model_validate(
            {
                "external_writes_enabled": True,
                "auto_send_enabled": True,
                "google_oauth_enabled": True,
                "gmail_send_enabled": True,
                "gmail_send_release_attested": True,
                "gmail_send_attachment_broker_socket": "relative.sock",
            }
        )
    with pytest.raises(ValidationError, match="readonly broker socket must be absolute"):
        Settings.model_validate(
            {
                "external_writes_enabled": True,
                "auto_send_enabled": True,
                "google_oauth_enabled": True,
                "gmail_send_enabled": True,
                "gmail_send_release_attested": True,
                "mailbox_broker_socket": "relative.sock",
            }
        )


def test_production_gmail_send_requires_in_production_attestation() -> None:
    with pytest.raises(ValidationError, match="production Gmail send"):
        Settings.model_validate(
            {
                "environment": "production",
                "console_cookie_secure": True,
                "console_allowed_hosts": ("careerops.example",),
                "console_allowed_origins": ("https://careerops.example",),
                "external_writes_enabled": True,
                "auto_send_enabled": True,
                "google_oauth_enabled": True,
                "google_oauth_in_production_attested": True,
                "gmail_send_enabled": True,
                "gmail_send_release_attested": True,
            }
        )


def test_production_google_oauth_requires_in_production_attestation() -> None:
    with pytest.raises(ValidationError, match="in-production attestation"):
        Settings.model_validate(
            {
                "environment": "production",
                "console_cookie_secure": True,
                "console_allowed_hosts": ("careerops.example",),
                "console_allowed_origins": ("https://careerops.example",),
                "google_oauth_enabled": True,
            }
        )


def test_greenhouse_submit_requires_full_release_gate_and_absolute_broker_socket() -> None:
    settings = Settings.model_validate(
        {
            "external_writes_enabled": True,
            "auto_submit_enabled": True,
            "greenhouse_submit_enabled": True,
            "greenhouse_submit_release_attested": True,
        }
    )

    assert settings.greenhouse_submit_runtime_enabled is True
    assert settings.greenhouse_submit_credential_broker_socket is not None
    assert settings.greenhouse_submit_credential_broker_socket.is_absolute()
    assert settings.greenhouse_submit_attachment_broker_socket is not None
    assert settings.greenhouse_submit_attachment_broker_socket.is_absolute()

    with pytest.raises(ValidationError, match="credential broker socket must be absolute"):
        Settings.model_validate(
            {
                "external_writes_enabled": True,
                "auto_submit_enabled": True,
                "greenhouse_submit_enabled": True,
                "greenhouse_submit_release_attested": True,
                "greenhouse_submit_credential_broker_socket": "relative.sock",
            }
        )

    with pytest.raises(ValidationError, match="attachment broker socket must be absolute"):
        Settings.model_validate(
            {
                "external_writes_enabled": True,
                "auto_submit_enabled": True,
                "greenhouse_submit_enabled": True,
                "greenhouse_submit_release_attested": True,
                "greenhouse_submit_attachment_broker_socket": "relative.sock",
            }
        )


def test_production_greenhouse_submit_requires_in_production_attestation() -> None:
    with pytest.raises(ValidationError, match="production Greenhouse submit"):
        Settings.model_validate(
            {
                "environment": "production",
                "console_cookie_secure": True,
                "console_allowed_hosts": ("careerops.example",),
                "console_allowed_origins": ("https://careerops.example",),
                "external_writes_enabled": True,
                "auto_submit_enabled": True,
                "greenhouse_submit_enabled": True,
                "greenhouse_submit_release_attested": True,
            }
        )
