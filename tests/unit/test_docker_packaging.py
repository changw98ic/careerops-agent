from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.yml"
CI_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _gmail_oauth_serve_runtime_dir_command(runtime_dir: Path, object_root: Path) -> str:
    return _gmail_oauth_serve_recipe_lines(runtime_dir, object_root)[0]


def _gmail_oauth_serve_broker_command(runtime_dir: Path, object_root: Path) -> str:
    return " ".join(
        line.strip().removesuffix("\\").strip()
        for line in _gmail_oauth_serve_recipe_lines(runtime_dir, object_root)[1:]
    )


def _gmail_oauth_serve_recipe_lines(runtime_dir: Path, object_root: Path) -> list[str]:
    result = subprocess.run(
        [
            "make",
            "-n",
            "gmail-oauth-serve",
            f"CAREEROPS_LOCAL_GMAIL_RUNTIME_DIR={runtime_dir}",
            f"CAREEROPS_LOCAL_GMAIL_OBJECT_ROOT={object_root}",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def _make_recipe(target: str, *overrides: str) -> str:
    result = subprocess.run(
        ["make", "-n", target, *overrides],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return " ".join(line.strip().removesuffix("\\").strip() for line in result.stdout.splitlines())


def _runtime_env_segment(command: str) -> str:
    return command.split('" && env -i ', maxsplit=1)[1].split(" uv run ", maxsplit=1)[0]


def test_package_exposes_host_gmail_broker_without_tracking_oauth_downloads() -> None:
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert 'careerops-gmail-broker = "careerops.cli.gmail_broker:main"' in pyproject
    assert "client_secret_*.json" in gitignore
    assert "*.apps.googleusercontent.com.json" in gitignore


def test_makefile_creates_gmail_oauth_runtime_dir_under_private_umask(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "gmail-runtime"
    object_root = tmp_path / "objects"

    mkdir_command = _gmail_oauth_serve_runtime_dir_command(runtime_dir, object_root)

    assert mkdir_command == f'umask 077; mkdir -p "{runtime_dir}" "{object_root}"'


def test_makefile_gmail_oauth_runtime_dir_is_private_when_new(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "gmail-runtime"
    object_root = tmp_path / "objects"
    mkdir_command = _gmail_oauth_serve_runtime_dir_command(runtime_dir, object_root)

    subprocess.run(["/bin/sh", "-c", mkdir_command], check=True)

    assert runtime_dir.stat().st_mode & 0o777 == 0o700
    assert object_root.stat().st_mode & 0o777 == 0o700


def test_makefile_gmail_oauth_runtime_dir_does_not_chmod_existing_dir(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "gmail-runtime"
    object_root = tmp_path / "objects"
    runtime_dir.mkdir()
    runtime_dir.chmod(0o755)
    object_root.mkdir()
    object_root.chmod(0o755)
    mkdir_command = _gmail_oauth_serve_runtime_dir_command(runtime_dir, object_root)

    subprocess.run(["/bin/sh", "-c", mkdir_command], check=True)

    assert runtime_dir.stat().st_mode & 0o777 == 0o755
    assert object_root.stat().st_mode & 0o777 == 0o755


def test_makefile_gmail_oauth_serve_starts_broker_with_minimal_env(tmp_path: Path) -> None:
    broker_command = _gmail_oauth_serve_broker_command(
        tmp_path / "gmail-runtime",
        tmp_path / "objects",
    )

    assert broker_command.startswith("env -i ")
    assert ' PATH="' in broker_command
    assert ' UV_PROJECT_ENVIRONMENT="venv"' in broker_command
    assert " uv run careerops-gmail-broker --json serve " in broker_command


def test_makefile_gmail_oauth_serve_does_not_inherit_exported_secrets(tmp_path: Path) -> None:
    broker_command = _gmail_oauth_serve_broker_command(
        tmp_path / "gmail-runtime",
        tmp_path / "objects",
    )

    assert not broker_command.startswith("uv run ")
    assert "CAREEROPS_" not in broker_command
    assert "POSTGRES" not in broker_command
    assert "DATABASE_URL" not in broker_command
    assert ".env" not in broker_command


def test_makefile_gmail_readonly_targets_use_encoded_database_url_helper() -> None:
    for target in ("gmail-readonly-status", "gmail-readonly-sync", "gmail-readonly-worker"):
        command = _make_recipe(target)

        assert "uv run careerops-database-url" in command
        assert 'uv run careerops-database-url)" && env -i ' in command
        assert 'CAREEROPS_DB_URL_USER="${CAREEROPS_DB_MAILBOX_USER}"' in command
        assert 'CAREEROPS_DB_URL_PASSWORD="${CAREEROPS_DB_MAILBOX_PASSWORD}"' in command
        assert "postgresql+psycopg://" not in command
        assert "careerops-gmail-readonly" in command


def test_makefile_gmail_send_targets_use_encoded_database_url_helper() -> None:
    for target in (
        "gmail-send-status",
        "gmail-send-once",
        "gmail-send-reconcile",
        "gmail-send-worker",
    ):
        command = _make_recipe(target)

        assert "uv run careerops-database-url" in command
        assert 'uv run careerops-database-url)" && env -i ' in command
        assert 'CAREEROPS_DB_URL_USER="${CAREEROPS_DB_MAIL_SENDER_USER}"' in command
        assert 'CAREEROPS_DB_URL_PASSWORD="${CAREEROPS_DB_MAIL_SENDER_PASSWORD}"' in command
        assert "postgresql+psycopg://" not in command
        assert "careerops-gmail-send" in command


def test_makefile_gmail_targets_do_not_expand_raw_password_into_url_argv() -> None:
    readonly = _make_recipe(
        "gmail-readonly-status",
        "CAREEROPS_DB_MAILBOX_PASSWORD=raw:mailbox/password",
    )
    send = _make_recipe(
        "gmail-send-status",
        "CAREEROPS_DB_MAIL_SENDER_PASSWORD=raw:sender/password",
    )

    assert "raw:mailbox/password" not in readonly
    assert "raw:sender/password" not in send
    assert "postgresql+psycopg://" not in readonly
    assert "postgresql+psycopg://" not in send


def test_makefile_gmail_worker_targets_are_bounded_long_running_commands() -> None:
    readonly = _make_recipe("gmail-readonly-worker")
    send = _make_recipe("gmail-send-worker")

    assert "uv run careerops-gmail-readonly worker" in readonly
    assert '--poll-seconds "5"' in readonly
    assert '--limit "10"' in readonly
    assert '--max-results "100"' in readonly
    assert "uv run careerops-gmail-send worker" in send
    assert '--poll-seconds "5"' in send
    assert '--limit "10"' in send


def test_makefile_gmail_runtime_env_is_minimal_and_gates_default_disabled() -> None:
    readonly_env = _runtime_env_segment(_make_recipe("gmail-readonly-status"))
    send_env = _runtime_env_segment(_make_recipe("gmail-send-status"))

    for env_segment in (readonly_env, send_env):
        assert 'PATH="' in env_segment
        assert 'UV_PROJECT_ENVIRONMENT="venv"' in env_segment
        assert 'CAREEROPS_DATABASE_URL="${CAREEROPS_DATABASE_URL}"' in env_segment
        assert 'CAREEROPS_ENVIRONMENT="${CAREEROPS_ENVIRONMENT:-development}"' in env_segment
        assert (
            'CAREEROPS_CONSOLE_COOKIE_SECURE="${CAREEROPS_CONSOLE_COOKIE_SECURE:-false}"'
            in env_segment
        )
        assert (
            'CAREEROPS_GOOGLE_OAUTH_ENABLED="${CAREEROPS_GOOGLE_OAUTH_ENABLED:-false}"'
            in env_segment
        )
        assert (
            'CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED="'
            '${CAREEROPS_GOOGLE_OAUTH_IN_PRODUCTION_ATTESTED:-false}"' in env_segment
        )
        assert (
            'CAREEROPS_EXTERNAL_WRITES_ENABLED="${CAREEROPS_EXTERNAL_WRITES_ENABLED:-false}"'
            in env_segment
        )
        assert 'CAREEROPS_AUTO_SEND_ENABLED="${CAREEROPS_AUTO_SEND_ENABLED:-false}"' in env_segment
        assert "CAREEROPS_DB_URL_PASSWORD" not in env_segment
        assert "CAREEROPS_DB_MAILBOX_PASSWORD" not in env_segment
        assert "CAREEROPS_DB_MAIL_SENDER_PASSWORD" not in env_segment

    for gate in (
        "CAREEROPS_GMAIL_SEND_ENABLED",
        "CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED",
        "CAREEROPS_GMAIL_SEND_IN_PRODUCTION_ATTESTED",
    ):
        assert f'{gate}="${{{gate}:-false}}"' in send_env

    assert "CAREEROPS_MAILBOX_BROKER_SOCKET=" in readonly_env
    assert "CAREEROPS_MAILBOX_BROKER_SOCKET=" in send_env
    assert "CAREEROPS_GMAIL_SEND_BROKER_SOCKET=" in send_env
    assert "CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET=" in send_env


def test_compose_exposes_broker_dependencies_on_loopback_with_native_default_ports() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "127.0.0.1:${CAREEROPS_POSTGRES_PORT:-5432}:5432" in compose
    assert "127.0.0.1:${CAREEROPS_REDIS_PORT:-6379}:6379" in compose
    assert "127.0.0.1:${CAREEROPS_TEMPORAL_PORT:-7233}:7233" in compose
    assert "CAREEROPS_POSTGRES_PORT=55432" in env_example
    assert "CAREEROPS_REDIS_PORT=56379" in env_example
    assert "CAREEROPS_TEMPORAL_PORT=7233" in env_example


def test_dockerfile_copies_m1_verifier_script_into_runtime_source_tree() -> None:
    dockerfile = REPOSITORY_ROOT / "Dockerfile"
    dockerignore = REPOSITORY_ROOT / ".dockerignore"
    verifier_script = REPOSITORY_ROOT / "scripts" / "verify_m1.py"

    assert verifier_script.is_file()

    dockerfile_lines = dockerfile.read_text(encoding="utf-8").splitlines()
    assert "COPY scripts ./scripts" in dockerfile_lines

    ignored_entries = {
        line.strip()
        for line in dockerignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "scripts" not in ignored_entries
    assert "scripts/verify_m1.py" not in ignored_entries


def test_compose_runs_crawler_outbox_as_separate_outbox_login() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    crawler_outbox = compose.split("  crawler-outbox:\n", maxsplit=1)[1].split(
        "\n  submission-outbox:\n", maxsplit=1
    )[0]

    assert "  crawler-outbox:" in compose
    assert "      - careerops-crawler-outbox" in crawler_outbox
    assert "      - --poll-seconds" in crawler_outbox
    assert "CAREEROPS_DB_OUTBOX_USER" in crawler_outbox
    assert "CAREEROPS_DB_OUTBOX_PASSWORD" in crawler_outbox
    assert "CAREEROPS_DATABASE_ROLE: outbox" in crawler_outbox
    assert "CAREEROPS_CRAWLER_WORKSPACE_ROOT: /app" in crawler_outbox
    assert "crawler-datasets:/app/datasets" in crawler_outbox


def test_compose_runs_temporal_worker_as_separate_workflow_login() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    workflow_worker = compose.split("  workflow-worker:\n", maxsplit=1)[1].split(
        "\n  crawler-outbox:\n", maxsplit=1
    )[0]

    assert "  workflow-worker:" in compose
    assert "CAREEROPS_DB_WORKFLOW_USER" in workflow_worker
    assert "CAREEROPS_DB_WORKFLOW_PASSWORD" in workflow_worker
    assert "CAREEROPS_DATABASE_ROLE: workflow" in workflow_worker
    assert "CAREEROPS_CRAWLER_WORKSPACE_ROOT: /app" in workflow_worker
    assert "crawler-datasets:/app/datasets" in workflow_worker
    assert "CAREEROPS_DB_WORKFLOW_USER=careerops_workflow_runtime" in env_example
    assert "CAREEROPS_DB_WORKFLOW_PASSWORD=" in env_example


def test_compose_console_allowlists_track_the_host_api_port() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "CAREEROPS_CONSOLE_ALLOWED_HOSTS: >-" in compose
    assert (
        '["127.0.0.1:${CAREEROPS_API_PORT:-8000}","localhost:${CAREEROPS_API_PORT:-8000}"]'
        in compose
    )
    assert "CAREEROPS_CONSOLE_ALLOWED_ORIGINS: >-" in compose
    assert (
        '["http://127.0.0.1:${CAREEROPS_API_PORT:-8000}",'
        '"http://localhost:${CAREEROPS_API_PORT:-8000}"]'
    ) in compose


def test_ci_postgres_job_uses_the_declared_database_consistently() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")
    postgres_job = ci.split("  postgres:\n", maxsplit=1)[1].split("\n  compose:\n", maxsplit=1)[0]

    assert "POSTGRES_DB: careerops_test_ci" in postgres_job
    assert '--health-cmd "pg_isready -U postgres -d careerops_test_ci"' in postgres_job
    assert "psql -h 127.0.0.1 -U postgres -d careerops_test_ci " in postgres_job
    assert "127.0.0.1:5432/careerops_test_ci" in postgres_job


def test_ci_compose_job_supplies_every_required_database_login() -> None:
    ci = CI_PATH.read_text(encoding="utf-8")
    compose_job = ci.split("  compose:\n", maxsplit=1)[1]

    for variable in (
        "CAREEROPS_DB_OWNER_USER",
        "CAREEROPS_DB_OWNER_PASSWORD",
        "CAREEROPS_DB_RUNTIME_USER",
        "CAREEROPS_DB_RUNTIME_PASSWORD",
        "CAREEROPS_DB_WORKFLOW_USER",
        "CAREEROPS_DB_WORKFLOW_PASSWORD",
        "CAREEROPS_DB_OUTBOX_USER",
        "CAREEROPS_DB_OUTBOX_PASSWORD",
        "CAREEROPS_DB_MAILBOX_USER",
        "CAREEROPS_DB_MAILBOX_PASSWORD",
        "CAREEROPS_DB_MAIL_SENDER_USER",
        "CAREEROPS_DB_MAIL_SENDER_PASSWORD",
        "CAREEROPS_DB_GREENHOUSE_SENDER_USER",
        "CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD",
    ):
        assert f"      {variable}:" in compose_job


def test_compose_profiles_continuous_synthetic_submission_worker_safely() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    submission_outbox = compose.split("  submission-outbox:\n", maxsplit=1)[1].split(
        "\n  gmail-readonly:\n", maxsplit=1
    )[0]

    assert "  submission-outbox:" in compose
    assert "      - synthetic-submission-outbox" in submission_outbox
    assert "      - careerops-submission-outbox" in submission_outbox
    assert "${CAREEROPS_SUBMISSION_OUTBOX_POLL_SECONDS:-5}" in submission_outbox
    assert "CAREEROPS_DATABASE_ROLE: outbox" in submission_outbox
    assert 'CAREEROPS_EXTERNAL_WRITES_ENABLED: "false"' in submission_outbox
    assert 'CAREEROPS_AUTO_SEND_ENABLED: "false"' in submission_outbox
    assert "CAREEROPS_SUBMISSION_OUTBOX_POLL_SECONDS=5" in env_example


def test_compose_profiles_readonly_gmail_worker_safely() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    gmail_readonly = compose.split("  gmail-readonly:\n", maxsplit=1)[1].split(
        "\n  gmail-send:\n", maxsplit=1
    )[0]

    assert "  gmail-readonly:" in compose
    assert "      - gmail-readonly" in gmail_readonly
    assert "      - careerops-gmail-readonly" in gmail_readonly
    assert "      - worker" in gmail_readonly
    assert "CAREEROPS_DB_MAILBOX_USER" in gmail_readonly
    assert "CAREEROPS_DB_MAILBOX_PASSWORD" in gmail_readonly
    assert "CAREEROPS_DATABASE_ROLE: mailbox" in gmail_readonly
    assert 'CAREEROPS_GOOGLE_OAUTH_ENABLED: "false"' in gmail_readonly
    assert 'CAREEROPS_EXTERNAL_WRITES_ENABLED: "false"' in gmail_readonly
    assert 'CAREEROPS_AUTO_SEND_ENABLED: "false"' in gmail_readonly
    assert "gmail-broker-socket:/run/careerops-gmail" in gmail_readonly
    assert "CAREEROPS_DB_MAILBOX_USER=careerops_mailbox_runtime" in env_example
    assert "CAREEROPS_MAILBOX_BROKER_SOCKET=/run/careerops-gmail/broker.sock" in env_example


def test_compose_profiles_reviewed_gmail_send_worker_safely() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    api = compose.split("  api:\n", maxsplit=1)[1].split("\n  workflow-worker:\n", maxsplit=1)[0]
    gmail_send = compose.split("  gmail-send:\n", maxsplit=1)[1].split("\nnetworks:\n", maxsplit=1)[
        0
    ]

    assert "  gmail-send:" in compose
    assert "    restart: unless-stopped" in gmail_send
    assert "      - gmail-send" in gmail_send
    assert "      - careerops-gmail-send" in gmail_send
    assert "      - worker" in gmail_send
    assert "${CAREEROPS_GMAIL_SEND_POLL_SECONDS:-5}" in gmail_send
    assert "${CAREEROPS_GMAIL_SEND_LIMIT:-10}" in gmail_send
    assert "CAREEROPS_DB_MAIL_SENDER_USER" in gmail_send
    assert "CAREEROPS_DB_MAIL_SENDER_PASSWORD" in gmail_send
    assert "CAREEROPS_DATABASE_ROLE: mail_sender" in gmail_send
    for capability in (
        "GOOGLE_OAUTH_ENABLED",
        "EXTERNAL_WRITES_ENABLED",
        "AUTO_SEND_ENABLED",
        "GMAIL_SEND_ENABLED",
        "GMAIL_SEND_RELEASE_ATTESTED",
        "GMAIL_SEND_IN_PRODUCTION_ATTESTED",
    ):
        assert f'CAREEROPS_{capability}: "${{CAREEROPS_{capability}:-false}}"' in gmail_send
        assert f'CAREEROPS_{capability}: "${{CAREEROPS_{capability}:-false}}"' in api
    assert "CAREEROPS_MAILBOX_BROKER_SOCKET:" in gmail_send
    assert "CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET:" in gmail_send
    assert "gmail-broker-socket:/run/careerops-gmail" in gmail_send
    assert "gmail-send-broker-socket:/run/careerops-gmail-send" in gmail_send
    assert "CAREEROPS_DB_MAIL_SENDER_USER=careerops_mail_sender_runtime" in env_example
    assert "CAREEROPS_GMAIL_SEND_POLL_SECONDS=5" in env_example
    assert "CAREEROPS_GMAIL_SEND_LIMIT=10" in env_example
    assert "CAREEROPS_GMAIL_SEND_BROKER_SOCKET=/run/careerops-gmail-send/broker.sock" in env_example
    assert (
        "CAREEROPS_GMAIL_SEND_ATTACHMENT_BROKER_SOCKET=/run/careerops-gmail-send/attachments.sock"
        in env_example
    )


def test_compose_profiles_reviewed_greenhouse_submit_worker_safely() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    api = compose.split("  api:\n", maxsplit=1)[1].split("\n  workflow-worker:\n", maxsplit=1)[0]
    greenhouse_submit = compose.split("  greenhouse-submit:\n", maxsplit=1)[1].split(
        "\nnetworks:\n", maxsplit=1
    )[0]

    assert "  greenhouse-submit:" in compose
    assert "    restart: unless-stopped" in greenhouse_submit
    assert "      - greenhouse-submit" in greenhouse_submit
    assert "      - python" in greenhouse_submit
    assert "      - -m" in greenhouse_submit
    assert "      - careerops.cli.greenhouse_submit" in greenhouse_submit
    assert "      - worker" in greenhouse_submit
    assert "${CAREEROPS_GREENHOUSE_SUBMIT_POLL_SECONDS:-5}" in greenhouse_submit
    assert "${CAREEROPS_GREENHOUSE_SUBMIT_LIMIT:-10}" in greenhouse_submit
    assert "CAREEROPS_DB_GREENHOUSE_SENDER_USER" in greenhouse_submit
    assert "CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD" in greenhouse_submit
    assert "CAREEROPS_DATABASE_ROLE: greenhouse_sender" in greenhouse_submit
    for capability in (
        "EXTERNAL_WRITES_ENABLED",
        "AUTO_SUBMIT_ENABLED",
        "GREENHOUSE_SUBMIT_ENABLED",
        "GREENHOUSE_SUBMIT_RELEASE_ATTESTED",
        "GREENHOUSE_SUBMIT_IN_PRODUCTION_ATTESTED",
    ):
        assert f'CAREEROPS_{capability}: "${{CAREEROPS_{capability}:-false}}"' in greenhouse_submit
        assert f'CAREEROPS_{capability}: "${{CAREEROPS_{capability}:-false}}"' in api
    assert "CAREEROPS_GREENHOUSE_SUBMIT_CREDENTIAL_BROKER_SOCKET:" in greenhouse_submit
    assert "CAREEROPS_GREENHOUSE_SUBMIT_ATTACHMENT_BROKER_SOCKET:" in greenhouse_submit
    assert "greenhouse-broker-socket:/run/careerops-greenhouse" in greenhouse_submit
    assert "object-data:" not in greenhouse_submit
    assert "crawler-datasets:" not in greenhouse_submit
    assert "API_KEY" not in greenhouse_submit
    assert "SECRET" not in greenhouse_submit
    assert "CAREEROPS_AUTO_SUBMIT_ENABLED=false" in env_example
    assert "CAREEROPS_DB_GREENHOUSE_SENDER_USER=careerops_greenhouse_sender_runtime" in env_example
    assert "CAREEROPS_GREENHOUSE_SUBMIT_POLL_SECONDS=5" in env_example
    assert "CAREEROPS_GREENHOUSE_SUBMIT_LIMIT=10" in env_example
    assert (
        "CAREEROPS_GREENHOUSE_SUBMIT_CREDENTIAL_BROKER_SOCKET=/run/careerops-greenhouse/broker.sock"
        in env_example
    )
    assert (
        "CAREEROPS_GREENHOUSE_SUBMIT_ATTACHMENT_BROKER_SOCKET=/run/careerops-greenhouse/attachments.sock"
        in env_example
    )
