from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from sqlalchemy.engine import URL

ENV_PREFIX = "CAREEROPS_DB_URL_"
MAX_FIELD_LENGTH = 2048
MAX_PORT = 65535
MIN_PORT = 1
REQUIRED_ENV_NAMES = (
    f"{ENV_PREFIX}USER",
    f"{ENV_PREFIX}PASSWORD",
    f"{ENV_PREFIX}HOST",
    f"{ENV_PREFIX}PORT",
    f"{ENV_PREFIX}NAME",
)


@dataclass(frozen=True, slots=True)
class DatabaseUrlParts:
    username: str
    password: str = field(repr=False)
    host: str
    port: int
    database: str


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    env = os.environ if environ is None else environ

    if args:
        print("careerops-database-url: unexpected arguments are not accepted", file=error_output)
        return 2

    try:
        parts = _parts_from_env(env)
    except ValueError as exc:
        print(f"careerops-database-url: {exc}", file=error_output)
        return 2

    print(_render_database_url(parts), file=output)
    return 0


def _parts_from_env(environ: Mapping[str, str]) -> DatabaseUrlParts:
    values = {name: environ.get(name, "") for name in REQUIRED_ENV_NAMES}
    missing = [name for name, value in values.items() if value == ""]
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")

    _validate_text_field("user", values[f"{ENV_PREFIX}USER"])
    _validate_text_field("password", values[f"{ENV_PREFIX}PASSWORD"])
    _validate_text_field("host", values[f"{ENV_PREFIX}HOST"])
    _validate_text_field("database name", values[f"{ENV_PREFIX}NAME"])

    return DatabaseUrlParts(
        username=values[f"{ENV_PREFIX}USER"],
        password=values[f"{ENV_PREFIX}PASSWORD"],
        host=values[f"{ENV_PREFIX}HOST"],
        port=_parse_port(values[f"{ENV_PREFIX}PORT"]),
        database=values[f"{ENV_PREFIX}NAME"],
    )


def _validate_text_field(label: str, value: str) -> None:
    if len(value) > MAX_FIELD_LENGTH:
        raise ValueError(f"{label} exceeds maximum length")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"{label} contains unsupported control characters")


def _parse_port(value: str) -> int:
    if not value.isdecimal():
        raise ValueError("port must be a decimal integer")
    port = int(value)
    if port < MIN_PORT or port > MAX_PORT:
        raise ValueError(f"port must be between {MIN_PORT} and {MAX_PORT}")
    return port


def _render_database_url(parts: DatabaseUrlParts) -> str:
    return URL.create(
        "postgresql+psycopg",
        username=parts.username,
        password=parts.password,
        host=parts.host,
        port=parts.port,
        database=parts.database,
    ).render_as_string(hide_password=False)


if __name__ == "__main__":
    raise SystemExit(main())
