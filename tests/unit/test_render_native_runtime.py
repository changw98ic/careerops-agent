from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from scripts.render_native_runtime import SERVICES, render_launchd, render_redis


def test_launchd_render_contains_no_secret_values(tmp_path: Path) -> None:
    runtime_home = tmp_path / "Application Support" / "CareerOps"
    (runtime_home / "bin").mkdir(parents=True)
    (runtime_home / "bin" / "native_service.sh").write_text("#!/bin/sh\n")
    output = tmp_path / "LaunchAgents"

    paths = render_launchd(
        runtime_home=runtime_home,
        output_directory=output,
        home=tmp_path,
    )

    assert len(paths) == len(SERVICES)
    api = plistlib.loads((output / "io.careerops.api.plist").read_bytes())
    assert api["Label"] == "io.careerops.api"
    assert api["ProgramArguments"] == [
        str(runtime_home / "bin" / "native_service.sh"),
        "api",
    ]
    assert api["WorkingDirectory"] == str(runtime_home / "workspace")
    assert api["EnvironmentVariables"]["CAREEROPS_NATIVE_HOME"] == str(runtime_home)
    assert "PASSWORD" not in (output / "io.careerops.api.plist").read_text()
    assert (output / "io.careerops.api.plist").stat().st_mode & 0o777 == 0o600
    gmail = plistlib.loads((output / "io.careerops.gmail-readonly.plist").read_bytes())
    assert gmail["RunAtLoad"] is False
    assert gmail["KeepAlive"] == {"SuccessfulExit": False}


def test_redis_render_hashes_password_and_binds_loopback(tmp_path: Path) -> None:
    password = 'not in config " or acl'

    config_path, acl_path = render_redis(
        runtime_directory=tmp_path,
        password=password,
        port=56379,
    )

    config = config_path.read_text()
    acl = acl_path.read_text()
    assert password not in config
    assert password not in acl
    assert "bind 127.0.0.1 ::1" in config
    assert "port 56379" in config
    assert "appendonly yes" in config
    assert "user default on #" in acl
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert acl_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("password", ["", "line\nbreak", "line\rbreak", "nul\x00byte"])
def test_redis_render_rejects_unsafe_password(password: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="single-line"):
        render_redis(runtime_directory=tmp_path, password=password, port=56379)
