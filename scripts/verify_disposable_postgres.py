#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import secrets
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

OPT_IN_ENV = "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES"
DESTRUCTIVE_ACK_ENV = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"
DATABASE_URL_ENV = "CAREEROPS_TEST_DATABASE_URL"
POSTGRES_IMAGE = (
    "postgres:17.5-alpine3.22@"
    "sha256:6567bca8d7bc8c82c5922425a0baee57be8402df92bae5eacad5f01ae9544daa"
)
CONTAINER_PREFIX = "careerops_test_pg_"
DATABASE_PREFIX = "careerops_test_"
POSTGRES_PORT = "5432/tcp"
READY_TIMEOUT_SECONDS = 60.0
READY_POLL_SECONDS = 1.0
COMMAND_TIMEOUT_SECONDS = 15.0
IMAGE_PULL_TIMEOUT_SECONDS = 120.0
VERIFY_DB_TIMEOUT_SECONDS = 300.0
FINAL_READY_LOG_EVENTS = 2

ROOT = Path(__file__).resolve().parents[1]

Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleep = Callable[[float], None]
TokenHex = Callable[[int], str]
TokenUrlsafe = Callable[[int], str]

_SAFE_GENERATED_NAME = re.compile(r"^careerops_test_[a-z0-9_]+$")
_SAFE_GENERATED_CONTAINER = re.compile(r"^careerops_test_pg_[a-z0-9_]+$")
_POSTGRES_READY_LOG_LINE = re.compile(r"database system is ready to accept connections")


@dataclass(frozen=True)
class VerificationResult:
    returncode: int
    output: str


class VerificationError(RuntimeError):
    def __init__(self, message: str, *, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode


def _default_runner(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    # Arguments are fixed or verifier-generated, and shell remains disabled.
    return subprocess.run(  # nosec B603
        list(args),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        input=input,
        text=True,
        capture_output=True,
        check=False,
        timeout=_command_timeout_seconds(args),
    )


def _command_timeout_seconds(args: Sequence[str]) -> float:
    """Keep Docker controls short while allowing the complete PostgreSQL test suite."""

    if tuple(args[:2]) == ("docker", "pull"):
        return IMAGE_PULL_TIMEOUT_SECONDS
    if tuple(args[:2]) == ("make", "verify-db"):
        return VERIFY_DB_TIMEOUT_SECONDS
    return COMMAND_TIMEOUT_SECONDS


def _require_opt_in(env: Mapping[str, str]) -> None:
    if env.get(OPT_IN_ENV) != "1":
        raise VerificationError(f"{OPT_IN_ENV}=1 is required", returncode=2)


def _generated_suffix(token_hex: TokenHex) -> str:
    suffix = token_hex(8).lower()
    if not re.fullmatch(r"[a-f0-9]{16}", suffix):
        raise VerificationError("generated suffix is not safe")
    return suffix


def _ensure_safe_container_name(name: str) -> None:
    if not _SAFE_GENERATED_CONTAINER.fullmatch(name):
        raise VerificationError(f"refusing unsafe container name: {name!r}")


def _ensure_safe_database_name(name: str) -> None:
    if not _SAFE_GENERATED_NAME.fullmatch(name):
        raise VerificationError(f"refusing unsafe database name: {name!r}")


def _append_output(parts: list[str], label: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        parts.append(f"--- {label} stdout ---\n{result.stdout}")
    if result.stderr:
        parts.append(f"--- {label} stderr ---\n{result.stderr}")


def _run_checked(
    runner: Runner,
    args: Sequence[str],
    *,
    label: str,
    output_parts: list[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(args, cwd=cwd, env=env, input=input)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerificationError(
            f"{label} could not contact Docker: {_safe_process_error(error)}"
        ) from error
    _append_output(output_parts, label, result)
    if result.returncode != 0:
        raise VerificationError(f"{label} failed with exit code {result.returncode}")
    return result


def _ensure_postgres_image(
    runner: Runner,
    *,
    output_parts: list[str],
) -> None:
    """Pull the exact pinned image when a clean Docker cache does not have it yet."""

    for image_ref in _postgres_image_refs():
        try:
            inspect_result = runner(
                ["docker", "image", "inspect", image_ref],
                cwd=None,
                env=None,
                input=None,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError(
                f"docker image inspect could not contact Docker: {_safe_process_error(error)}"
            ) from error
        if inspect_result.returncode == 0:
            return
    _run_checked(
        runner,
        ["docker", "pull", POSTGRES_IMAGE],
        label="docker pull",
        output_parts=output_parts,
    )


def _postgres_image_refs() -> tuple[str, str]:
    image_name, _, digest = POSTGRES_IMAGE.partition("@")
    if not digest:
        return (POSTGRES_IMAGE, POSTGRES_IMAGE)
    return (POSTGRES_IMAGE, f"{image_name.rsplit(':', 1)[0]}@{digest}")


def _parse_loopback_port(port_output: str) -> int:
    line = port_output.strip().splitlines()[0] if port_output.strip() else ""
    host, separator, port_text = line.rpartition(":")
    if separator != ":" or host != "127.0.0.1":
        raise VerificationError(f"docker mapped PostgreSQL to non-loopback address: {line!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise VerificationError(f"docker returned invalid PostgreSQL port: {line!r}") from exc
    if not 1 <= port <= 65535:
        raise VerificationError(f"docker returned out-of-range PostgreSQL port: {line!r}")
    return port


def _ready_log_event_count(log_output: str) -> int:
    return sum(1 for line in log_output.splitlines() if _POSTGRES_READY_LOG_LINE.search(line))


def _wait_ready(
    runner: Runner,
    *,
    container_name: str,
    database_name: str,
    password: str,
    output_parts: list[str],
    sleep: Sleep,
    monotonic: Callable[[], float],
) -> None:
    deadline = monotonic() + READY_TIMEOUT_SECONDS
    last_logs_result: subprocess.CompletedProcess[str] | None = None
    last_result: subprocess.CompletedProcess[str] | None = None
    while monotonic() <= deadline:
        try:
            last_logs_result = runner(
                ["docker", "logs", container_name],
                cwd=None,
                env=None,
                input=None,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError(
                f"docker logs could not contact Docker: {_safe_process_error(error)}"
            ) from error
        if last_logs_result.returncode != 0:
            sleep(READY_POLL_SECONDS)
            continue
        if _ready_log_event_count(last_logs_result.stdout + last_logs_result.stderr) < (
            FINAL_READY_LOG_EVENTS
        ):
            sleep(READY_POLL_SECONDS)
            continue
        try:
            last_result = runner(
                [
                    "docker",
                    "exec",
                    "-e",
                    f"PGPASSWORD={password}",
                    container_name,
                    "pg_isready",
                    "-U",
                    "postgres",
                    "-d",
                    database_name,
                ],
                cwd=None,
                env=None,
                input=None,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError(
                f"docker pg_isready could not contact Docker: {_safe_process_error(error)}"
            ) from error
        if last_result.returncode == 0:
            _append_output(output_parts, "docker logs readiness", last_logs_result)
            _append_output(output_parts, "docker pg_isready", last_result)
            return
        sleep(READY_POLL_SECONDS)
    if last_logs_result is not None:
        _append_output(output_parts, "docker logs readiness", last_logs_result)
    if last_result is not None:
        _append_output(output_parts, "docker pg_isready", last_result)
    raise VerificationError("PostgreSQL container did not become ready before timeout")


def _bootstrap_roles(
    runner: Runner,
    *,
    repo_root: Path,
    container_name: str,
    database_name: str,
    password: str,
    output_parts: list[str],
) -> None:
    bootstrap_path = (
        repo_root / "src" / "careerops" / "infrastructure" / "database" / ("bootstrap_roles.sql")
    )
    bootstrap_sql = bootstrap_path.read_text(encoding="utf-8")
    _run_checked(
        runner,
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"PGPASSWORD={password}",
            container_name,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database_name,
        ],
        label="bootstrap roles",
        output_parts=output_parts,
        input=bootstrap_sql,
    )


def _remove_container(
    runner: Runner,
    *,
    container_name: str,
    output_parts: list[str],
) -> int:
    _ensure_safe_container_name(container_name)
    try:
        result = runner(
            ["docker", "rm", "-f", container_name],
            cwd=None,
            env=None,
            input=None,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        output_parts.append(
            f"ERROR: docker cleanup could not contact Docker: {_safe_process_error(error)}\n"
        )
        return 1
    _append_output(output_parts, "docker cleanup", result)
    return result.returncode


def _safe_process_error(error: OSError | subprocess.TimeoutExpired) -> str:
    """Describe a process failure without echoing command-line environment values."""

    if isinstance(error, subprocess.TimeoutExpired):
        timeout = error.timeout
        if isinstance(timeout, (int, float)):
            return f"timed out after {timeout:.0f} seconds"
        return f"timed out after {COMMAND_TIMEOUT_SECONDS:.0f} seconds"
    return type(error).__name__


def run_verification(
    *,
    repo_root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    runner: Runner = _default_runner,
    sleep: Sleep = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    token_hex: TokenHex = secrets.token_hex,
    token_urlsafe: TokenUrlsafe = secrets.token_urlsafe,
) -> VerificationResult:
    environment = dict(os.environ if env is None else env)
    output_parts: list[str] = []
    container_name: str | None = None
    result = VerificationResult(1, "")
    try:
        _require_opt_in(environment)
        suffix = _generated_suffix(token_hex)
        container_name = f"{CONTAINER_PREFIX}{suffix}"
        database_name = f"{DATABASE_PREFIX}{suffix}"
        _ensure_safe_container_name(container_name)
        _ensure_safe_database_name(database_name)
        password = token_urlsafe(24)

        _ensure_postgres_image(runner, output_parts=output_parts)

        _run_checked(
            runner,
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--label",
                "careerops.ephemeral-postgres=1",
                "-e",
                f"POSTGRES_DB={database_name}",
                "-e",
                "POSTGRES_USER=postgres",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-p",
                "127.0.0.1::5432",
                POSTGRES_IMAGE,
            ],
            label="docker run",
            output_parts=output_parts,
        )

        _wait_ready(
            runner,
            container_name=container_name,
            database_name=database_name,
            password=password,
            output_parts=output_parts,
            sleep=sleep,
            monotonic=monotonic,
        )

        port_result = _run_checked(
            runner,
            ["docker", "port", container_name, POSTGRES_PORT],
            label="docker port",
            output_parts=output_parts,
        )
        port = _parse_loopback_port(port_result.stdout)

        _bootstrap_roles(
            runner,
            repo_root=repo_root,
            container_name=container_name,
            database_name=database_name,
            password=password,
            output_parts=output_parts,
        )

        make_env: MutableMapping[str, str] = dict(environment)
        make_env[DATABASE_URL_ENV] = (
            f"postgresql+psycopg://postgres:{quote(password, safe='')}"
            f"@127.0.0.1:{port}/{database_name}"
        )
        make_env[DESTRUCTIVE_ACK_ENV] = "1"
        verify_result = runner(
            ["make", "verify-db"],
            cwd=repo_root,
            env=make_env,
            input=None,
        )
        _append_output(output_parts, "make verify-db", verify_result)
        if verify_result.returncode != 0:
            raise VerificationError(
                f"make verify-db failed with exit code {verify_result.returncode}"
            )
        result = VerificationResult(0, "".join(output_parts))
    except VerificationError as exc:
        output_parts.append(f"ERROR: {exc}\n")
        result = VerificationResult(exc.returncode, "".join(output_parts))
    finally:
        if container_name is not None:
            cleanup_rc = _remove_container(
                runner,
                container_name=container_name,
                output_parts=output_parts,
            )
            if cleanup_rc != 0 and "ERROR:" not in "".join(output_parts):
                output_parts.append(f"ERROR: docker cleanup failed with exit code {cleanup_rc}\n")
                result = VerificationResult(cleanup_rc, "".join(output_parts))
            else:
                result = VerificationResult(result.returncode, "".join(output_parts))
    return result


def main() -> int:
    result = run_verification()
    sys.stdout.write(result.output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
