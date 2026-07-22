#!/usr/bin/env python3
"""Run destructive PostgreSQL verification in a fresh, local-only cluster.

This fallback exists for environments where Docker is unavailable. It never accepts a caller's
database URL: it owns a fresh ``initdb`` data directory under the system temporary directory,
starts PostgreSQL on a randomly chosen loopback port, and removes only that directory on exit.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

OPT_IN_ENV = "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES"
DESTRUCTIVE_ACK_ENV = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"
DATABASE_URL_ENV = "CAREEROPS_TEST_DATABASE_URL"
DATABASE_PREFIX = "careerops_test_"
DATA_DIRECTORY_PREFIX = "careerops_test_pg_"
SUPERUSER = "careerops_test_admin"
COMMAND_TIMEOUT_SECONDS = 120.0
ROOT = Path(__file__).resolve().parents[1]

_SAFE_DATA_DIRECTORY = re.compile(r"^careerops_test_pg_[a-z0-9_]+$")
_SAFE_DATABASE = re.compile(r"^careerops_test_[a-z0-9_]+$")

Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]
TempDirectory = Callable[..., str]
PortAllocator = Callable[[], int]
RemoveTree = Callable[[Path], None]
TokenHex = Callable[[int], str]


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
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def _require_opt_in(env: Mapping[str, str]) -> None:
    if env.get(OPT_IN_ENV) != "1":
        raise VerificationError(f"{OPT_IN_ENV}=1 is required", returncode=2)
    if env.get(DATABASE_URL_ENV):
        raise VerificationError(
            f"{DATABASE_URL_ENV} must be unset; this verifier owns its disposable database",
            returncode=2,
        )


def _generated_suffix(token_hex: TokenHex) -> str:
    suffix = token_hex(8).lower()
    if not re.fullmatch(r"[a-f0-9]{16}", suffix):
        raise VerificationError("generated suffix is not safe")
    return suffix


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise VerificationError("could not allocate a safe loopback PostgreSQL port")
    return port


def _safe_data_directory(path: Path) -> None:
    resolved = path.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if resolved.parent != temporary_root or not _SAFE_DATA_DIRECTORY.fullmatch(resolved.name):
        raise VerificationError(f"refusing unsafe PostgreSQL data directory: {path!s}")


def _safe_database_name(name: str) -> None:
    if not _SAFE_DATABASE.fullmatch(name):
        raise VerificationError(f"refusing unsafe PostgreSQL database name: {name!r}")


def _find_executable(name: str, which: Which) -> str:
    value = which(name)
    if value is None:
        raise VerificationError(
            f"required PostgreSQL executable is unavailable: {name}",
            returncode=2,
        )
    return value


def _append_output(parts: list[str], label: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        parts.append(f"--- {label} stdout ---\n{result.stdout}")
    if result.stderr:
        parts.append(f"--- {label} stderr ---\n{result.stderr}")


def _safe_process_error(error: OSError | subprocess.TimeoutExpired) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return f"timed out after {COMMAND_TIMEOUT_SECONDS:.0f} seconds"
    return type(error).__name__


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
            f"{label} could not execute: {_safe_process_error(error)}"
        ) from error
    _append_output(output_parts, label, result)
    if result.returncode != 0:
        raise VerificationError(f"{label} failed with exit code {result.returncode}")
    return result


def _stop_postgres(
    runner: Runner,
    *,
    pg_ctl: str,
    data_directory: Path,
    output_parts: list[str],
) -> int:
    _safe_data_directory(data_directory)
    try:
        result = runner(
            [pg_ctl, "-D", str(data_directory), "-m", "immediate", "-w", "stop"],
            cwd=None,
            env=None,
            input=None,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        output_parts.append(
            f"ERROR: native PostgreSQL cleanup failed: {_safe_process_error(error)}\n"
        )
        return 1
    _append_output(output_parts, "native PostgreSQL cleanup", result)
    return result.returncode


def _remove_data_directory(
    data_directory: Path,
    *,
    remove_tree: RemoveTree,
    output_parts: list[str],
) -> int:
    _safe_data_directory(data_directory)
    if not data_directory.exists():
        return 0
    try:
        remove_tree(data_directory)
    except OSError as error:
        output_parts.append(
            f"ERROR: native PostgreSQL data cleanup failed: {type(error).__name__}\n"
        )
        return 1
    return 0


def run_verification(
    *,
    repo_root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    runner: Runner = _default_runner,
    which: Which = shutil.which,
    make_temp_directory: TempDirectory = tempfile.mkdtemp,
    allocate_loopback_port: PortAllocator = _allocate_loopback_port,
    remove_tree: RemoveTree = shutil.rmtree,
    token_hex: TokenHex = secrets.token_hex,
) -> VerificationResult:
    environment = dict(os.environ if env is None else env)
    output_parts: list[str] = []
    result = VerificationResult(1, "")
    data_directory: Path | None = None
    pg_ctl: str | None = None
    started = False
    try:
        _require_opt_in(environment)
        suffix = _generated_suffix(token_hex)
        database_name = f"{DATABASE_PREFIX}{suffix}"
        _safe_database_name(database_name)
        data_directory = Path(make_temp_directory(prefix=f"{DATA_DIRECTORY_PREFIX}{suffix}_"))
        _safe_data_directory(data_directory)
        initdb = _find_executable("initdb", which)
        pg_ctl = _find_executable("pg_ctl", which)
        createdb = _find_executable("createdb", which)
        psql = _find_executable("psql", which)
        port = allocate_loopback_port()

        _run_checked(
            runner,
            [
                initdb,
                "-D",
                str(data_directory),
                f"--username={SUPERUSER}",
                "--auth-local=trust",
                "--auth-host=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            label="native initdb",
            output_parts=output_parts,
        )
        _run_checked(
            runner,
            [
                pg_ctl,
                "-D",
                str(data_directory),
                "-w",
                "-t",
                "60",
                "-l",
                str(data_directory / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {port} -c timezone=UTC",
                "start",
            ],
            label="native PostgreSQL start",
            output_parts=output_parts,
        )
        started = True
        _run_checked(
            runner,
            [
                createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                SUPERUSER,
                database_name,
            ],
            label="native create database",
            output_parts=output_parts,
        )
        bootstrap_path = (
            repo_root / "src" / "careerops" / "infrastructure" / "database" / "bootstrap_roles.sql"
        )
        _run_checked(
            runner,
            [
                psql,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-U",
                SUPERUSER,
                "-d",
                database_name,
                "-f",
                str(bootstrap_path),
            ],
            label="native bootstrap roles",
            output_parts=output_parts,
        )
        make_env: MutableMapping[str, str] = dict(environment)
        make_env[DATABASE_URL_ENV] = (
            f"postgresql+psycopg://{SUPERUSER}@127.0.0.1:{port}/{database_name}"
        )
        make_env[DESTRUCTIVE_ACK_ENV] = "1"
        _run_checked(
            runner,
            ["make", "verify-db"],
            label="make verify-db",
            output_parts=output_parts,
            cwd=repo_root,
            env=make_env,
        )
        result = VerificationResult(0, "".join(output_parts))
    except VerificationError as error:
        output_parts.append(f"ERROR: {error}\n")
        result = VerificationResult(error.returncode, "".join(output_parts))
    finally:
        cleanup_failed = False
        if data_directory is not None and started and pg_ctl is not None:
            cleanup_failed = (
                _stop_postgres(
                    runner,
                    pg_ctl=pg_ctl,
                    data_directory=data_directory,
                    output_parts=output_parts,
                )
                != 0
            )
        if data_directory is not None:
            cleanup_failed = (
                _remove_data_directory(
                    data_directory,
                    remove_tree=remove_tree,
                    output_parts=output_parts,
                )
                != 0
                or cleanup_failed
            )
        if cleanup_failed and result.returncode == 0:
            output_parts.append("ERROR: native PostgreSQL cleanup failed\n")
            result = VerificationResult(1, "".join(output_parts))
        else:
            result = VerificationResult(result.returncode, "".join(output_parts))
    return result


def main() -> int:
    result = run_verification()
    sys.stdout.write(result.output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
