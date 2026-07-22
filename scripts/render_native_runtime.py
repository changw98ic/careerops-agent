#!/usr/bin/env python3
"""Render secret-free launchd jobs and the hashed Redis ACL for native macOS runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativeService:
    name: str
    label: str
    autoload: bool = True


SERVICES: tuple[NativeService, ...] = (
    NativeService("postgres", "io.careerops.postgresql17"),
    NativeService("redis", "io.careerops.redis"),
    NativeService("temporal", "io.careerops.temporal"),
    NativeService("gmail-broker", "io.careerops.gmail-broker"),
    NativeService("api", "io.careerops.api"),
    NativeService("workflow-worker", "io.careerops.workflow-worker"),
    NativeService("crawler-outbox", "io.careerops.crawler-outbox"),
    NativeService("gmail-readonly", "io.careerops.gmail-readonly", autoload=False),
)


def build_launchd_plist(
    *, runtime_home: Path, service: NativeService, home: Path
) -> dict[str, object]:
    runtime_home = runtime_home.resolve()
    log_directory = runtime_home / "logs"
    return {
        "Label": service.label,
        "ProgramArguments": [str(runtime_home / "bin" / "native_service.sh"), service.name],
        "WorkingDirectory": str(runtime_home / "workspace"),
        "EnvironmentVariables": {
            "HOME": str(home.resolve()),
            "CAREEROPS_NATIVE_HOME": str(runtime_home),
            "CAREEROPS_NATIVE_ENV_FILE": str(runtime_home / "config" / "runtime.env"),
            "CAREEROPS_NATIVE_VENV_BIN": str(runtime_home / "venv" / "bin"),
            "CAREEROPS_NATIVE_WORKSPACE": str(runtime_home / "workspace"),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": (
                f"{runtime_home / 'venv' / 'bin'}:{home / '.local' / 'bin'}:"
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            ),
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": service.autoload,
        "KeepAlive": True if service.autoload else {"SuccessfulExit": False},
        "ProcessType": "Standard",
        "ThrottleInterval": 5,
        "StandardOutPath": str(log_directory / f"{service.name}.log"),
        "StandardErrorPath": str(log_directory / f"{service.name}.error.log"),
    }


def render_launchd(*, runtime_home: Path, output_directory: Path, home: Path) -> tuple[Path, ...]:
    runtime_home = runtime_home.resolve()
    launcher = runtime_home / "bin" / "native_service.sh"
    if not launcher.is_file():
        raise ValueError(f"native launcher is missing: {launcher}")
    output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (runtime_home / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    rendered: list[Path] = []
    for service in SERVICES:
        path = output_directory / f"{service.label}.plist"
        payload = plistlib.dumps(
            build_launchd_plist(runtime_home=runtime_home, service=service, home=home),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
        _atomic_write(path, payload, mode=0o600)
        rendered.append(path)
    return tuple(rendered)


def render_redis(*, runtime_directory: Path, password: str, port: int) -> tuple[Path, Path]:
    if not password or "\x00" in password or "\n" in password or "\r" in password:
        raise ValueError("Redis password must be non-empty and single-line")
    if not 1 <= port <= 65535:
        raise ValueError("Redis port must be between 1 and 65535")
    runtime_directory = runtime_directory.resolve()
    data_directory = runtime_directory / "redis" / "data"
    config_directory = runtime_directory / "redis"
    data_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    acl_path = config_directory / "users.acl"
    config_path = config_directory / "redis.conf"
    password_digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    acl = f"user default on #{password_digest} ~* &* +@all\n".encode()
    config = (
        "bind 127.0.0.1 ::1\n"
        "protected-mode yes\n"
        f"port {port}\n"
        "daemonize no\n"
        "supervised no\n"
        "appendonly yes\n"
        "appendfsync everysec\n"
        f'dir "{_redis_quote(str(data_directory))}"\n'
        f'aclfile "{_redis_quote(str(acl_path))}"\n'
        'logfile ""\n'
    ).encode()
    _atomic_write(acl_path, acl, mode=0o600)
    _atomic_write(config_path, config, mode=0o600)
    return config_path, acl_path


def _redis_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        path.chmod(mode)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    launchd = subcommands.add_parser("launchd")
    launchd.add_argument("--runtime-home", type=Path, required=True)
    launchd.add_argument("--output-directory", type=Path, required=True)
    launchd.add_argument("--home", type=Path, required=True)
    redis = subcommands.add_parser("redis")
    redis.add_argument("--runtime-directory", type=Path, required=True)
    redis.add_argument("--password-env", default="CAREEROPS_REDIS_PASSWORD")
    redis.add_argument("--port", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "launchd":
        for path in render_launchd(
            runtime_home=args.runtime_home,
            output_directory=args.output_directory,
            home=args.home,
        ):
            print(path)
        return 0
    password = os.environ.get(args.password_env)
    if password is None:
        raise SystemExit(f"{args.password_env} is required")
    config_path, acl_path = render_redis(
        runtime_directory=args.runtime_directory,
        password=password,
        port=args.port,
    )
    if stat.S_IMODE(config_path.stat().st_mode) != 0o600:
        raise RuntimeError("Redis config permissions are unsafe")
    print(config_path)
    print(acl_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
