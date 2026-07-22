from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, TextIO

from sqlalchemy.engine import Engine

from careerops.auth.contracts import BootstrapCredential
from careerops.config import Settings, get_settings
from careerops.infrastructure.auth import create_console_auth_service
from careerops.infrastructure.database.engine import create_database_engine


class BootstrapAuthService(Protocol):
    def issue_bootstrap_token(self, *, now: datetime) -> BootstrapCredential: ...


SettingsLoader = Callable[[], Settings]
EngineFactory = Callable[[Settings], Engine]
ServiceFactory = Callable[[Engine], BootstrapAuthService]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage one-time CareerOps console bootstrap credentials.",
    )
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("issue", help="issue and print a new one-time bootstrap token")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_loader: SettingsLoader = get_settings,
    engine_factory: EngineFactory = create_database_engine,
    service_factory: ServiceFactory = create_console_auth_service,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(file=stderr)
        return 2
    if args.command != "issue":
        raise SystemExit(f"unsupported command: {args.command}")
    settings = settings_loader()
    engine = engine_factory(settings)
    try:
        credential = service_factory(engine).issue_bootstrap_token(now=datetime.now(UTC))
    finally:
        engine.dispose()
    print(credential.token, file=stdout)
    print(f"expires_at={credential.expires_at.isoformat()}", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
