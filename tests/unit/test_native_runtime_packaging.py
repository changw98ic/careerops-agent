from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_brewfile_uses_isolated_bsd_redis_protocol_server() -> None:
    brewfile = (ROOT / "Brewfile").read_text()

    assert 'brew "postgresql@17"' in brewfile
    assert 'brew "valkey", link: false' in brewfile
    assert 'brew "temporal"' in brewfile
    assert 'brew "uv"' in brewfile
    assert 'brew "redis"' not in brewfile


def test_native_launcher_has_no_container_or_send_worker_path() -> None:
    launcher = (ROOT / "scripts" / "native_service.sh").read_text()

    assert "HOMEBREW_PREFIX=$(brew --prefix)" in launcher
    assert "$HOMEBREW_PREFIX/opt/valkey" in launcher
    assert "valkey-server" in launcher
    assert "-c timezone=UTC" in launcher
    assert "docker" not in launcher.casefold()
    assert "gmail-send)" not in launcher
    assert "CAREEROPS_EXTERNAL_WRITES_ENABLED=false" in launcher
    assert "CAREEROPS_AUTO_SEND_ENABLED=false" in launcher
    assert 'CAREEROPS_BIND_PORT="${CAREEROPS_API_PORT:-' in launcher
    assert 'CAREEROPS_TEMPORAL_ADDRESS="127.0.0.1:${CAREEROPS_TEMPORAL_PORT:-7233}"' in launcher
    assert "CAREEROPS_NATIVE_REDIS_PORT" in launcher
    assert launcher.count("exec /usr/bin/env -i") == 4
    assert "clear_runtime_secrets" in launcher
    assert "CAREEROPS_DB_MAIL_SENDER_PASSWORD" in launcher
    assert "CAREEROPS_GMAIL_SEND_RELEASE_ATTESTED=false" in launcher
    assert "CAREEROPS_GREENHOUSE_SUBMIT_RELEASE_ATTESTED=false" in launcher


def test_native_lifecycle_is_project_scoped_and_non_purging() -> None:
    lifecycle = (ROOT / "scripts" / "native_runtime.sh").read_text()

    assert 'NATIVE_HOME="$HOME/Library/Application Support/CareerOps"' in lifecycle
    assert "io.careerops.postgresql17" in lifecycle
    assert "io.careerops.redis" in lifecycle
    assert "io.careerops.temporal" in lifecycle
    assert "docker" not in lifecycle.casefold()
    assert "purge)" not in lifecycle
    assert "rm -rf" not in lifecycle
    assert "brew install --formula valkey --skip-link" in lifecycle
    assert "brew unlink redis" not in lifecycle
    assert "runtime_was_running" in lifecycle
    assert "UV_PREFIX=$(brew --prefix uv)" in lifecycle
    assert 'if [ -z "$pid" ]' in lifecycle
    assert "wait_workflow_worker" in lifecycle
    assert "CAREEROPS_TEMPORAL_WORKER_IDENTITY=careerops-native-worker" in lifecycle
    assert "client.shutdown(socket.SHUT_WR)" in lifecycle
    assert "start_or_reconcile_runtime" in lifecycle
    stop_case = lifecycle.split("    stop)", maxsplit=1)[1].split(";;", maxsplit=1)[0]
    assert "require_env" not in stop_case


def test_native_database_bootstrap_keeps_passwords_out_of_psql_argv() -> None:
    bootstrap = (ROOT / "scripts" / "native_postgres_bootstrap.sh").read_text()

    assert "--set" not in bootstrap
    assert "\\getenv owner_password CAREEROPS_DB_OWNER_PASSWORD" in bootstrap
    assert "\\getenv runtime_password CAREEROPS_DB_RUNTIME_PASSWORD" in bootstrap
    assert "\\getenv mail_sender_password CAREEROPS_DB_MAIL_SENDER_PASSWORD" in bootstrap


def test_homebrew_example_uses_isolated_ports() -> None:
    example = (ROOT / ".env.example").read_text()

    assert "CAREEROPS_POSTGRES_PORT=55432" in example
    assert "CAREEROPS_REDIS_PORT=56379" in example
    assert "CAREEROPS_API_PORT=8000" in example
    assert "CAREEROPS_TEMPORAL_UI_PORT=8233" in example
    assert "127.0.0.1:55432/careerops" in example
    assert "127.0.0.1:56379/0" in example
