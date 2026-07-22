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
    WORKFLOW = "workflow"
    MAILBOX = "mailbox"
    RETENTION = "retention"
    OUTBOX = "outbox"
    MAIL_SENDER = "mail_sender"
    GREENHOUSE_SENDER = "greenhouse_sender"
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
    # A deployment-owned repository/workspace root.  Console users cannot choose this path;
    # persisted crawler artifacts are still constrained beneath its datasets/ subtree.
    crawler_workspace_root: Path = Path(".")
    storage_root: Path = Path("data/objects")
    storage_max_object_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
    )
    model_provider: str = "disabled"
    google_oauth_enabled: bool = False
    google_oauth_in_production_attested: bool = False
    mailbox_broker_socket: Path | None = Path("/run/careerops-gmail/broker.sock")
    external_writes_enabled: bool = False
    auto_send_enabled: bool = False
    auto_submit_enabled: bool = False
    gmail_send_enabled: bool = False
    gmail_send_release_attested: bool = False
    gmail_send_in_production_attested: bool = False
    gmail_send_broker_socket: Path | None = Path("/run/careerops-gmail-send/broker.sock")
    gmail_send_attachment_broker_socket: Path | None = Path(
        "/run/careerops-gmail-send/attachments.sock"
    )
    greenhouse_submit_enabled: bool = False
    greenhouse_submit_release_attested: bool = False
    greenhouse_submit_in_production_attested: bool = False
    greenhouse_submit_credential_broker_socket: Path | None = Path(
        "/run/careerops-greenhouse/broker.sock"
    )
    greenhouse_submit_attachment_broker_socket: Path | None = Path(
        "/run/careerops-greenhouse/attachments.sock"
    )

    @property
    def gmail_send_runtime_enabled(self) -> bool:
        """Whether both the API enqueue path and send worker may be activated."""
        broker_sockets = (
            self.gmail_send_broker_socket,
            self.mailbox_broker_socket,
            self.gmail_send_attachment_broker_socket,
        )
        production_attested = self.environment is not RuntimeEnvironment.PRODUCTION or (
            self.google_oauth_in_production_attested and self.gmail_send_in_production_attested
        )
        return (
            self.external_writes_enabled
            and self.auto_send_enabled
            and self.gmail_send_enabled
            and self.gmail_send_release_attested
            and self.google_oauth_enabled
            and all(socket is not None and socket.is_absolute() for socket in broker_sockets)
            and production_attested
        )

    @property
    def greenhouse_submit_runtime_enabled(self) -> bool:
        """Whether the explicitly authorized Greenhouse Job Board API lane may run."""
        broker_sockets = (
            self.greenhouse_submit_credential_broker_socket,
            self.greenhouse_submit_attachment_broker_socket,
        )
        production_attested = self.environment is not RuntimeEnvironment.PRODUCTION or (
            self.greenhouse_submit_in_production_attested
        )
        return (
            self.external_writes_enabled
            and self.auto_submit_enabled
            and self.greenhouse_submit_enabled
            and self.greenhouse_submit_release_attested
            and all(socket is not None and socket.is_absolute() for socket in broker_sockets)
            and production_attested
        )

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
            raise ValueError("model providers require an M2 privacy qualification")
        if (
            self.google_oauth_enabled
            and self.environment is RuntimeEnvironment.PRODUCTION
            and not self.google_oauth_in_production_attested
        ):
            raise ValueError("production Google OAuth requires in-production attestation")
        if self.database_role is DatabaseCapabilityRole.MAILBOX:
            if self.mailbox_broker_socket is None:
                raise ValueError("mailbox role requires an absolute Unix broker socket")
            if not self.mailbox_broker_socket.is_absolute():
                raise ValueError("mailbox broker socket must be absolute")
        if self.database_role is DatabaseCapabilityRole.GREENHOUSE_SENDER:
            sender_sockets = (
                self.greenhouse_submit_credential_broker_socket,
                self.greenhouse_submit_attachment_broker_socket,
            )
            if not all(socket is not None and socket.is_absolute() for socket in sender_sockets):
                raise ValueError(
                    "Greenhouse sender requires absolute submit and attachment sockets"
                )
        if self.gmail_send_enabled:
            if self.gmail_send_broker_socket is None:
                raise ValueError("Gmail send requires an absolute Unix broker socket")
            if not self.gmail_send_broker_socket.is_absolute():
                raise ValueError("Gmail send broker socket must be absolute")
            if self.mailbox_broker_socket is None:
                raise ValueError("Gmail send requires an absolute readonly broker socket")
            if not self.mailbox_broker_socket.is_absolute():
                raise ValueError("Gmail send readonly broker socket must be absolute")
            if self.gmail_send_attachment_broker_socket is None:
                raise ValueError("Gmail send requires an absolute attachment broker socket")
            if not self.gmail_send_attachment_broker_socket.is_absolute():
                raise ValueError("Gmail send attachment broker socket must be absolute")
        requested_gmail_send_capability = (
            self.gmail_send_enabled
            or self.gmail_send_release_attested
            or self.gmail_send_in_production_attested
        )
        if (
            requested_gmail_send_capability
            and self.environment is RuntimeEnvironment.PRODUCTION
            and self.external_writes_enabled
            and self.auto_send_enabled
            and self.gmail_send_enabled
            and self.gmail_send_release_attested
            and self.google_oauth_enabled
            and not self.gmail_send_in_production_attested
        ):
            raise ValueError("production Gmail send requires in-production attestation")
        if requested_gmail_send_capability and not self.gmail_send_runtime_enabled:
            raise ValueError(
                "Gmail send requires external writes, auto-send, gmail_send_enabled, "
                "release attestation, Google OAuth, and configured send, readonly, and "
                "attachment broker sockets"
            )
        requested_greenhouse_submit_capability = (
            self.greenhouse_submit_enabled
            or self.greenhouse_submit_release_attested
            or self.greenhouse_submit_in_production_attested
        )
        if self.greenhouse_submit_enabled:
            if (
                self.greenhouse_submit_credential_broker_socket is None
                or not self.greenhouse_submit_credential_broker_socket.is_absolute()
            ):
                raise ValueError("Greenhouse credential broker socket must be absolute")
            if (
                self.greenhouse_submit_attachment_broker_socket is None
                or not self.greenhouse_submit_attachment_broker_socket.is_absolute()
            ):
                raise ValueError("Greenhouse attachment broker socket must be absolute")
        if (
            requested_greenhouse_submit_capability
            and self.environment is RuntimeEnvironment.PRODUCTION
            and self.external_writes_enabled
            and self.auto_submit_enabled
            and self.greenhouse_submit_enabled
            and self.greenhouse_submit_release_attested
            and not self.greenhouse_submit_in_production_attested
        ):
            raise ValueError("production Greenhouse submit requires in-production attestation")
        if requested_greenhouse_submit_capability and not self.greenhouse_submit_runtime_enabled:
            raise ValueError(
                "Greenhouse submit requires external writes, auto-submit, "
                "greenhouse_submit_enabled, release attestation, and an absolute credential "
                "and attachment broker socket"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
