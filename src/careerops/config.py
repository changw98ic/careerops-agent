from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DeploymentMode(StrEnum):
    LOOPBACK = "loopback"
    COMPOSE_LOOPBACK = "compose_loopback"


class DatabaseCapabilityRole(StrEnum):
    """M0 runtime capabilities that may be assumed by an application process."""

    API = "api"
    RETENTION = "retention"
    OUTBOX = "outbox"
    READONLY = "readonly"


class Settings(BaseSettings):
    """Process configuration with unsafe capabilities rejected at startup."""

    model_config = SettingsConfigDict(
        env_prefix="CAREEROPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: RuntimeEnvironment = RuntimeEnvironment.DEVELOPMENT
    deployment_mode: DeploymentMode = DeploymentMode.LOOPBACK
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8000, ge=1, le=65535)
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://careerops_runtime@127.0.0.1:5432/careerops"
    )
    database_role: DatabaseCapabilityRole = DatabaseCapabilityRole.API
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = Field(default="default", min_length=1, max_length=255)
    temporal_task_queue: str = Field(default="careerops-m0", min_length=1, max_length=255)
    readiness_timeout_seconds: float = Field(default=1.0, ge=0.05, le=10.0)
    console_allowed_hosts: tuple[str, ...] = (
        "127.0.0.1:8000",
        "localhost:8000",
    )
    console_allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    )
    console_cookie_secure: bool = False
    storage_root: Path = Path("data/objects")
    storage_max_object_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
    )

    model_provider: str = "disabled"
    # User-supplied LLM connection. When model_provider != "disabled", all three
    # fields are required. The provider is any Anthropic-compatible endpoint
    # (Xiaomi MiMo, Zhipu GLM, OpenAI, Anthropic, etc.).
    model_base_url: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_name: str = ""
    google_oauth_enabled: bool = False
    external_writes_enabled: bool = False
    auto_send_enabled: bool = False
    # Phase-0 capability gates (design Decision 11). These are NOT the three
    # prohibited external-effect flags above and are never rejected by
    # ``reject_unreleased_capabilities``; they gate the read/discovery path and
    # the review-only model path. ``SettingsCapabilityResolver.decide`` reads
    # them to return safe ``CapabilityDecision`` values. Defaults: crawl-plan
    # management released (still subject to source policy); model tailoring
    # disabled — model output is review-only regardless of this flag.
    crawl_plan_management_enabled: bool = True
    model_tailoring_enabled: bool = False
    # Smart form intake is a review-only convenience path. It is deliberately
    # disabled until the operator completes the rollout and privacy checks.
    smart_intake_enabled: bool = False

    @model_validator(mode="after")
    def reject_unreleased_capabilities(self) -> Self:
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("business state requires the PostgreSQL psycopg driver")
        if not self.redis_url.get_secret_value().startswith(("redis://", "rediss://")):
            raise ValueError("Redis URL must use redis:// or rediss://")
        if ":" not in self.temporal_address or "://" in self.temporal_address:
            raise ValueError("Temporal address must be a host:port authority")
        if not self.console_allowed_hosts or not self.console_allowed_origins:
            raise ValueError("console host and origin allowlists must be explicit")
        if self.environment is RuntimeEnvironment.PRODUCTION and not self.console_cookie_secure:
            raise ValueError("production console cookies must be Secure")
        if self.storage_root in {Path(""), Path(self.storage_root.anchor)}:
            raise ValueError("storage root must be a dedicated directory")
        loopback_hosts = {"127.0.0.1", "::1", "localhost"}
        # Container-wide bind is accepted only behind the explicit compose mode;
        # the checked-in topology publishes it on host loopback exclusively.
        compose_hosts = {"0.0.0.0", "::"}  # nosec B104
        if self.bind_host not in loopback_hosts and not (
            self.deployment_mode is DeploymentMode.COMPOSE_LOOPBACK
            and self.bind_host in compose_hosts
        ):
            raise ValueError("public bind requires the bounded compose-loopback deployment mode")
        if self.model_provider != "disabled":
            missing = [
                name
                for name, value in (
                    ("model_base_url", self.model_base_url),
                    ("model_api_key", self.model_api_key.get_secret_value()),
                    ("model_name", self.model_name),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"model_provider='{self.model_provider}' requires: {', '.join(missing)}"
                )
        if self.google_oauth_enabled:
            raise ValueError("Google OAuth is unavailable before the M4 integration gate")
        if self.external_writes_enabled:
            raise ValueError("external writes are unavailable before the M5A side-effect gate")
        if self.auto_send_enabled:
            raise ValueError("Auto-send is unavailable before M7 Release Qualification")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
