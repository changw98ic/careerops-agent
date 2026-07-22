from __future__ import annotations

from io import StringIO

from careerops.cli import database_url


def test_database_url_outputs_sqlalchemy_postgres_url_with_encoded_password() -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = database_url.main(
        [],
        environ={
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": "p@ss:/?#%word",
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432",
            "CAREEROPS_DB_URL_NAME": "career_ops",
        },
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert stdout.getvalue() == (
        "postgresql+psycopg://worker_user:p%40ss%3A%2F%3F%23%25word"
        "@postgres.internal:5432/career_ops\n"
    )


def test_missing_required_env_reports_field_names_without_secret_values() -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = database_url.main(
        [],
        environ={
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": "super-secret",
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432",
        },
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "CAREEROPS_DB_URL_NAME" in stderr.getvalue()
    assert "super-secret" not in stderr.getvalue()
    assert "worker_user" not in stderr.getvalue()
    assert "postgres.internal" not in stderr.getvalue()


def test_bad_port_reports_sanitized_validation_error() -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = database_url.main(
        [],
        environ={
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": "secret-with-@:/?#%",
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432:secret",
            "CAREEROPS_DB_URL_NAME": "career_ops",
        },
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "port must be a decimal integer" in stderr.getvalue()
    assert "5432:secret" not in stderr.getvalue()
    assert "secret-with-@:/?#%" not in stderr.getvalue()


def test_unexpected_argv_does_not_echo_argument_values() -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = database_url.main(
        ["--password", "secret-with-@:/?#%"],
        environ={
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": "env-secret",
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432",
            "CAREEROPS_DB_URL_NAME": "career_ops",
        },
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "unexpected arguments are not accepted" in stderr.getvalue()
    assert "secret-with-@:/?#%" not in stderr.getvalue()
    assert "env-secret" not in stderr.getvalue()


def test_password_is_hidden_from_parts_repr() -> None:
    parts = database_url._parts_from_env(
        {
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": "repr-secret",
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432",
            "CAREEROPS_DB_URL_NAME": "career_ops",
        }
    )

    assert "repr-secret" not in repr(parts)


def test_bounded_value_error_does_not_echo_value() -> None:
    stdout = StringIO()
    stderr = StringIO()
    long_secret = "x" * (database_url.MAX_FIELD_LENGTH + 1)

    exit_code = database_url.main(
        [],
        environ={
            "CAREEROPS_DB_URL_USER": "worker_user",
            "CAREEROPS_DB_URL_PASSWORD": long_secret,
            "CAREEROPS_DB_URL_HOST": "postgres.internal",
            "CAREEROPS_DB_URL_PORT": "5432",
            "CAREEROPS_DB_URL_NAME": "career_ops",
        },
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "password exceeds maximum length" in stderr.getvalue()
    assert long_secret not in stderr.getvalue()
