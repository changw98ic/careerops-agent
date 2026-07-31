import pytest
from pydantic import ValidationError

from careerops.config import (
    DatabaseCapabilityRole,
    DeploymentMode,
    RuntimeEnvironment,
    Settings,
)


def test_safe_defaults_are_loopback_and_capability_off() -> None:
    settings = Settings.model_validate({})

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


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"bind_host": "0.0.0.0"}, "bounded compose-loopback"),
        ({"database_url": "sqlite:///careerops.db"}, "PostgreSQL psycopg"),
        ({"redis_url": "http://redis:6379"}, "Redis URL"),
        ({"temporal_address": "https://temporal:7233"}, "host:port"),
        ({"model_provider": "external"}, "requires"),
        ({"storage_max_object_bytes": 0}, "greater than or equal to 1"),
        ({"storage_root": "."}, "dedicated directory"),
    ],
)
def test_unreleased_capabilities_fail_startup(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(override)


@pytest.mark.parametrize(
    "flag",
    ["google_oauth_enabled", "external_writes_enabled", "auto_send_enabled"],
)
def test_capability_flags_are_settable(flag: str) -> None:
    """The M4/M5A/M7 release gates are torn down for the single-user
    autonomous loop (real-autonomous-career-loop): these flags are honored at
    startup, not rejected. Defaults remain off (see test_safe_defaults...)."""
    settings = Settings.model_validate({flag: True})
    assert getattr(settings, flag) is True


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
