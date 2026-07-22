from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
_DESTRUCTIVE_ACKNOWLEDGEMENT = "CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE"


def test_verify_db_requires_the_same_destructive_acknowledgement_as_migration_tests() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")

    verify_db_target = makefile.split("verify-db:\n", maxsplit=1)[1].split(
        "\nverify-temporal:", maxsplit=1
    )[0]
    assert "CAREEROPS_TEST_DATABASE_URL" in verify_db_target
    assert _DESTRUCTIVE_ACKNOWLEDGEMENT in verify_db_target
    assert '"$(CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE)" = "1"' in verify_db_target


def test_ci_postgres_job_explicitly_acknowledges_destructive_migration_tests() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    postgres_job = workflow.split("  postgres:\n", maxsplit=1)[1].split("\n  compose:", maxsplit=1)[
        0
    ]

    assert "CAREEROPS_TEST_DATABASE_URL" in postgres_job
    assert f'{_DESTRUCTIVE_ACKNOWLEDGEMENT}: "1"' in postgres_job
    assert "POSTGRES_DB: careerops_test_" in postgres_job
    assert "/careerops_test_" in postgres_job


def test_native_ephemeral_verifier_has_the_same_explicit_opt_in_guard() -> None:
    makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
    native_target = makefile.split("verify-db-native-ephemeral:\n", maxsplit=1)[1].split(
        "\nverify-temporal:", maxsplit=1
    )[0]

    assert '"$(CAREEROPS_ALLOW_EPHEMERAL_POSTGRES)" = "1"' in native_target
    assert "scripts/verify_native_ephemeral_postgres.py" in native_target
