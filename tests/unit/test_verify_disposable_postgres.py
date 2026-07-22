from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_disposable_postgres.py"
SPEC = importlib.util.spec_from_file_location("verify_disposable_postgres", SCRIPT_PATH)
assert SPEC is not None
verify_disposable_postgres = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = verify_disposable_postgres
SPEC.loader.exec_module(verify_disposable_postgres)


@dataclass(frozen=True)
class RecordedCall:
    args: tuple[str, ...]
    cwd: Path | None
    env: dict[str, str] | None
    input: str | None


class FakeRunner:
    def __init__(
        self,
        *,
        port_output: str = "127.0.0.1:55432\n",
        make_returncode: int = 0,
        cleanup_returncode: int = 0,
        image_present: bool = True,
        inspect_results: dict[str, int] | None = None,
        logs_outputs: list[str] | None = None,
    ) -> None:
        self.port_output = port_output
        self.make_returncode = make_returncode
        self.cleanup_returncode = cleanup_returncode
        self.image_present = image_present
        self.inspect_results = inspect_results or {}
        self.logs_outputs = logs_outputs or [
            (
                "database system is ready to accept connections\n"
                "PostgreSQL init process complete; ready for start up.\n"
                "database system is ready to accept connections\n"
            )
        ]
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
        if command[:3] == ("docker", "image", "inspect"):
            inspected_ref = command[3]
            if inspected_ref in self.inspect_results:
                return subprocess.CompletedProcess(
                    command,
                    self.inspect_results[inspected_ref],
                    "",
                    "image is not present" if self.inspect_results[inspected_ref] else "",
                )
            return subprocess.CompletedProcess(
                command,
                0 if self.image_present else 1,
                "",
                "image is not present" if not self.image_present else "",
            )
        if command[:2] == ("docker", "pull"):
            return subprocess.CompletedProcess(command, 0, "image pulled\n", "")
        if command[:2] == ("docker", "run"):
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ("docker", "logs"):
            output_index = min(
                sum(1 for call in self.calls if call.args[:2] == ("docker", "logs")) - 1,
                len(self.logs_outputs) - 1,
            )
            return subprocess.CompletedProcess(command, 0, self.logs_outputs[output_index], "")
        if command[:2] == ("docker", "exec") and "pg_isready" in command:
            return subprocess.CompletedProcess(command, 0, "accepting connections\n", "")
        if command[:2] == ("docker", "port"):
            return subprocess.CompletedProcess(command, 0, self.port_output, "")
        if command[:2] == ("docker", "exec") and "psql" in command:
            return subprocess.CompletedProcess(command, 0, "bootstrap ok\n", "")
        if command == ("make", "verify-db"):
            return subprocess.CompletedProcess(command, self.make_returncode, "verify output\n", "")
        if command[:3] == ("docker", "rm", "-f"):
            return subprocess.CompletedProcess(command, self.cleanup_returncode, "removed\n", "")
        return subprocess.CompletedProcess(command, 99, "", f"unexpected command: {command!r}")

    def commands(self) -> list[tuple[str, ...]]:
        return [call.args for call in self.calls]


def _repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    bootstrap = root / "src" / "careerops" / "infrastructure" / "database" / "bootstrap_roles.sql"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("SELECT 'roles';\n", encoding="utf-8")
    return root


def _run(
    tmp_path: Path,
    fake: FakeRunner,
    *,
    env: dict[str, str] | None = None,
) -> Any:
    return verify_disposable_postgres.run_verification(
        repo_root=_repo_root(tmp_path),
        env={verify_disposable_postgres.OPT_IN_ENV: "1"} if env is None else env,
        runner=fake,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        token_hex=lambda _n: "a1b2c3d4e5f60708",
        token_urlsafe=lambda _n: "p@ ss",
    )


def test_requires_explicit_ephemeral_postgres_opt_in(tmp_path: Path) -> None:
    fake = FakeRunner()

    result = _run(tmp_path, fake, env={})

    assert result.returncode == 2
    assert "CAREEROPS_ALLOW_EPHEMERAL_POSTGRES=1 is required" in result.output
    assert fake.calls == []


def test_rejects_non_loopback_docker_port_mapping_and_cleans_up(tmp_path: Path) -> None:
    fake = FakeRunner(port_output="0.0.0.0:55432\n")

    result = _run(tmp_path, fake)

    assert result.returncode == 1
    assert "non-loopback" in result.output
    assert ("make", "verify-db") not in fake.commands()
    assert fake.commands()[-1] == (
        "docker",
        "rm",
        "-f",
        "careerops_test_pg_a1b2c3d4e5f60708",
    )


def test_removes_own_container_when_verify_db_fails(tmp_path: Path) -> None:
    fake = FakeRunner(make_returncode=42)

    result = _run(tmp_path, fake)

    assert result.returncode == 1
    assert "make verify-db failed" in result.output
    assert fake.commands()[-1] == (
        "docker",
        "rm",
        "-f",
        "careerops_test_pg_a1b2c3d4e5f60708",
    )


def test_runs_disposable_postgres_with_loopback_port_bootstrap_and_make_env(
    tmp_path: Path,
) -> None:
    fake = FakeRunner()

    result = _run(tmp_path, fake, env={verify_disposable_postgres.OPT_IN_ENV: "1", "KEEP": "1"})

    assert result.returncode == 0
    commands = fake.commands()
    docker_run = next(command for command in commands if command[:2] == ("docker", "run"))
    assert docker_run[:2] == ("docker", "run")
    assert "--name" in docker_run
    assert "careerops_test_pg_a1b2c3d4e5f60708" in docker_run
    assert "-p" in docker_run
    assert "127.0.0.1::5432" in docker_run
    assert verify_disposable_postgres.POSTGRES_IMAGE in docker_run

    bootstrap_call = next(call for call in fake.calls if "psql" in call.args)
    assert bootstrap_call.input == "SELECT 'roles';\n"

    make_call = next(call for call in fake.calls if call.args == ("make", "verify-db"))
    assert make_call.cwd == tmp_path / "repo"
    assert make_call.env is not None
    assert make_call.env["KEEP"] == "1"
    assert make_call.env[verify_disposable_postgres.DESTRUCTIVE_ACK_ENV] == "1"
    database_url = make_call.env[verify_disposable_postgres.DATABASE_URL_ENV]
    assert database_url.startswith("postgresql+psycopg://postgres:p%40%20ss@127.0.0.1:55432/")
    assert database_url.endswith("/careerops_test_a1b2c3d4e5f60708")
    assert commands[-1] == ("docker", "rm", "-f", "careerops_test_pg_a1b2c3d4e5f60708")


def test_waits_for_final_postgres_startup_before_bootstrap(tmp_path: Path) -> None:
    fake = FakeRunner(
        logs_outputs=[
            "database system is ready to accept connections\n",
            (
                "database system is ready to accept connections\n"
                "PostgreSQL init process complete; ready for start up.\n"
            ),
            (
                "database system is ready to accept connections\n"
                "PostgreSQL init process complete; ready for start up.\n"
                "database system is ready to accept connections\n"
            ),
        ],
    )

    result = _run(tmp_path, fake)

    assert result.returncode == 0
    commands = fake.commands()
    log_indexes = [
        index for index, command in enumerate(commands) if command[:2] == ("docker", "logs")
    ]
    pg_isready_index = next(
        index for index, command in enumerate(commands) if "pg_isready" in command
    )
    bootstrap_index = next(index for index, command in enumerate(commands) if "psql" in command)
    make_index = commands.index(("make", "verify-db"))
    assert len(log_indexes) == 3
    assert log_indexes[-1] < pg_isready_index < bootstrap_index < make_index
    assert "--- docker logs readiness stdout ---" in result.output


def test_pulls_pinned_image_when_docker_cache_is_cold(tmp_path: Path) -> None:
    fake = FakeRunner(image_present=False)

    result = _run(tmp_path, fake)

    assert result.returncode == 0
    commands = fake.commands()
    image_pull = ("docker", "pull", verify_disposable_postgres.POSTGRES_IMAGE)
    docker_run_index = next(
        index for index, command in enumerate(commands) if command[:2] == ("docker", "run")
    )
    assert image_pull in commands
    assert commands.index(image_pull) < docker_run_index
    assert "--- docker pull stdout ---\nimage pulled\n" in result.output


def test_uses_digest_alias_when_tagged_image_is_missing_but_digest_is_cached(
    tmp_path: Path,
) -> None:
    digest_ref = verify_disposable_postgres._postgres_image_refs()[1]
    fake = FakeRunner(
        inspect_results={
            verify_disposable_postgres.POSTGRES_IMAGE: 1,
            digest_ref: 0,
        }
    )

    result = _run(tmp_path, fake)

    assert result.returncode == 0
    commands = fake.commands()
    assert ("docker", "pull", verify_disposable_postgres.POSTGRES_IMAGE) not in commands
    assert ("docker", "image", "inspect", verify_disposable_postgres.POSTGRES_IMAGE) in commands
    assert ("docker", "image", "inspect", digest_ref) in commands


def test_docker_timeout_does_not_echo_generated_password(tmp_path: Path) -> None:
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
            if command[:2] == ("docker", "run"):
                raise subprocess.TimeoutExpired(command, 15)
            if command[:3] == ("docker", "rm", "-f"):
                return subprocess.CompletedProcess(command, 0, "removed\n", "")
            return super().__call__(args, **kwargs)

    result = _run(tmp_path, TimeoutRunner())

    assert result.returncode == 1
    assert "timed out after 15 seconds" in result.output
    assert "p@ ss" not in result.output
    assert "POSTGRES_PASSWORD" not in result.output


def test_full_database_verification_has_a_bounded_suite_timeout() -> None:
    assert verify_disposable_postgres._command_timeout_seconds(("docker", "run")) == 15
    assert verify_disposable_postgres._command_timeout_seconds(("make", "verify-db")) == 300
