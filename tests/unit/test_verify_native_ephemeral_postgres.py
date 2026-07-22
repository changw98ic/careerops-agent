from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "verify_native_ephemeral_postgres.py"
)
SPEC = importlib.util.spec_from_file_location("verify_native_ephemeral_postgres", SCRIPT_PATH)
assert SPEC is not None
verify_native_ephemeral_postgres = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = verify_native_ephemeral_postgres
SPEC.loader.exec_module(verify_native_ephemeral_postgres)


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    input: str | None


class FakeRunner:
    def __init__(self, *, make_returncode: int = 0) -> None:
        self.make_returncode = make_returncode
        self.calls: list[RecordedCall] = []

    def __call__(
        self,
        args: Any,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(str(item) for item in args)
        self.calls.append(
            RecordedCall(
                args=command,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input,
            )
        )
        if command[0] == "/test/initdb":
            return subprocess.CompletedProcess(command, 0, "initialized\n", "")
        if command[0] == "/test/pg_ctl":
            return subprocess.CompletedProcess(command, 0, "postgres ok\n", "")
        if command[0] == "/test/createdb":
            return subprocess.CompletedProcess(command, 0, "database created\n", "")
        if command[0] == "/test/psql":
            return subprocess.CompletedProcess(command, 0, "roles bootstrapped\n", "")
        if command == ("make", "verify-db"):
            return subprocess.CompletedProcess(command, self.make_returncode, "verify output\n", "")
        return subprocess.CompletedProcess(command, 99, "", f"unexpected command: {command!r}")

    def commands(self) -> list[tuple[str, ...]]:
        return [call.args for call in self.calls]


def _make_temp_directory(*, prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)


def _run(
    fake: FakeRunner,
    *,
    env: dict[str, str] | None = None,
    remove_tree: Any = shutil.rmtree,
) -> Any:
    return verify_native_ephemeral_postgres.run_verification(
        repo_root=Path("/repo"),
        env={verify_native_ephemeral_postgres.OPT_IN_ENV: "1"} if env is None else env,
        runner=fake,
        which=lambda name: f"/test/{name}",
        make_temp_directory=_make_temp_directory,
        allocate_loopback_port=lambda: 55432,
        remove_tree=remove_tree,
        token_hex=lambda _n: "a1b2c3d4e5f60708",
    )


def test_requires_explicit_ephemeral_postgres_opt_in() -> None:
    fake = FakeRunner()

    result = _run(fake, env={})

    assert result.returncode == 2
    assert "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 is required" in result.output
    assert fake.calls == []


def test_refuses_a_caller_supplied_database_url() -> None:
    fake = FakeRunner()

    result = _run(
        fake,
        env={
            verify_native_ephemeral_postgres.OPT_IN_ENV: "1",
            verify_native_ephemeral_postgres.DATABASE_URL_ENV: "postgresql://shared.example/careerops",
        },
    )

    assert result.returncode == 2
    assert "must be unset" in result.output
    assert fake.calls == []


def test_runs_native_loopback_cluster_bootstraps_and_cleans_up() -> None:
    fake = FakeRunner()
    removed: list[Path] = []

    def remove_data_directory(path: Path) -> None:
        removed.append(path)
        shutil.rmtree(path)

    result = _run(fake, remove_tree=remove_data_directory)

    assert result.returncode == 0
    commands = fake.commands()
    initdb = commands[0]
    assert initdb[0] == "/test/initdb"
    assert "--auth-local=trust" in initdb
    assert "--auth-host=trust" in initdb
    assert "--username=careerops_test_admin" in initdb
    start = commands[1]
    assert start[0] == "/test/pg_ctl"
    assert start[start.index("-l") + 1].endswith("/postgres.log")
    assert "-h 127.0.0.1 -p 55432 -c timezone=UTC" in start
    create_database = commands[2]
    assert create_database[:2] == ("/test/createdb", "-h")
    assert create_database[-1] == "careerops_test_a1b2c3d4e5f60708"
    bootstrap = commands[3]
    assert bootstrap[0] == "/test/psql"
    assert bootstrap[-1] == "/repo/src/careerops/infrastructure/database/bootstrap_roles.sql"
    make_call = next(call for call in fake.calls if call.args == ("make", "verify-db"))
    assert make_call.cwd == Path("/repo")
    assert make_call.env is not None
    assert make_call.env[verify_native_ephemeral_postgres.DESTRUCTIVE_ACK_ENV] == "1"
    assert make_call.env[verify_native_ephemeral_postgres.DATABASE_URL_ENV] == (
        "postgresql+psycopg://careerops_test_admin@127.0.0.1:55432/careerops_test_a1b2c3d4e5f60708"
    )
    assert commands[-1][0] == "/test/pg_ctl"
    assert commands[-1][-1] == "stop"
    assert len(removed) == 1
    assert not removed[0].exists()


def test_stops_and_removes_only_its_temp_cluster_after_verify_failure() -> None:
    fake = FakeRunner(make_returncode=42)
    removed: list[Path] = []

    def remove_data_directory(path: Path) -> None:
        removed.append(path)
        shutil.rmtree(path)

    result = _run(fake, remove_tree=remove_data_directory)

    assert result.returncode == 1
    assert "make verify-db failed with exit code 42" in result.output
    assert fake.commands()[-1][-1] == "stop"
    assert len(removed) == 1
    assert removed[0].name.startswith("careerops_test_pg_a1b2c3d4e5f60708_")
    assert not removed[0].exists()


def test_timeout_does_not_echo_full_command() -> None:
    class TimeoutRunner(FakeRunner):
        def __call__(self, args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            command = tuple(str(item) for item in args)
            self.calls.append(
                RecordedCall(
                    args=command,
                    cwd=kwargs.get("cwd"),
                    env=kwargs.get("env"),
                    input=kwargs.get("input"),
                )
            )
            if command[0] == "/test/initdb":
                raise subprocess.TimeoutExpired(command, 120)
            return super().__call__(args, **kwargs)

    removed: list[Path] = []

    def remove_data_directory(path: Path) -> None:
        removed.append(path)
        shutil.rmtree(path)

    result = _run(TimeoutRunner(), remove_tree=remove_data_directory)

    assert result.returncode == 1
    assert "timed out after 120 seconds" in result.output
    assert "/test/initdb -D" not in result.output
    assert len(removed) == 1
    assert not removed[0].exists()
