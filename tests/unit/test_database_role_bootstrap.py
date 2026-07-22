from __future__ import annotations

import re
from pathlib import Path

EXPECTED_CAPABILITY_ROLES = {
    "careerops_api",
    "careerops_mailbox",
    "careerops_mail_sender",
    "careerops_greenhouse_sender",
    "careerops_outbox",
    "careerops_readonly",
    "careerops_retention",
    "careerops_side_effect",
    "careerops_workflow",
}

BOOTSTRAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "careerops"
    / "infrastructure"
    / "database"
    / "bootstrap_roles.sql"
)
COMPOSE_BOOTSTRAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "postgres"
    / "init"
    / "20-create-runtime-and-temporal.sh"
)

_CREATE_ROLE = re.compile(
    r"\bCREATE\s+ROLE\s+(?P<role>[a-z_][a-z0-9_]*)\s+(?P<attributes>[^;]+);",
    re.IGNORECASE,
)
_ALTER_ROLE = re.compile(
    r"\bALTER\s+ROLE\s+(?P<role>[a-z_][a-z0-9_]*)\s+"
    r"(?:WITH\s+)?(?P<attributes>[^;]+);",
    re.IGNORECASE,
)

REQUIRED_SAFE_ATTRIBUTES = {
    "NOBYPASSRLS",
    "NOCREATEDB",
    "NOCREATEROLE",
    "NOINHERIT",
    "NOLOGIN",
    "NOREPLICATION",
    "NOSUPERUSER",
}


def test_bootstrap_creates_only_expected_nologin_capability_roles() -> None:
    sql = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    matches = list(_CREATE_ROLE.finditer(sql))
    declarations = {
        match.group("role").lower(): match.group("attributes").upper().split() for match in matches
    }

    assert len(matches) == len(EXPECTED_CAPABILITY_ROLES)
    assert set(declarations) == EXPECTED_CAPABILITY_ROLES
    for attributes in declarations.values():
        assert "NOLOGIN" in attributes
        assert "LOGIN" not in attributes
        assert "PASSWORD" not in attributes


def test_bootstrap_never_enables_login_or_embeds_a_password() -> None:
    sql = BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert re.search(r"\bLOGIN\b", sql, flags=re.IGNORECASE) is None
    assert re.search(r"\bPASSWORD\b", sql, flags=re.IGNORECASE) is None


def test_bootstrap_repairs_every_capability_role_to_safe_attributes() -> None:
    sql = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    matches = list(_ALTER_ROLE.finditer(sql))
    declarations = {
        match.group("role").lower(): set(match.group("attributes").upper().split())
        for match in matches
    }

    assert len(matches) == len(EXPECTED_CAPABILITY_ROLES)
    assert set(declarations) == EXPECTED_CAPABILITY_ROLES
    for attributes in declarations.values():
        assert attributes == REQUIRED_SAFE_ATTRIBUTES


def test_bootstrap_removes_only_outbound_capability_role_memberships() -> None:
    sql = BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "FROM pg_auth_members AS membership" in sql
    assert "member_role.oid = membership.member" in sql
    assert "granted_role.oid = membership.roleid" in sql
    assert "WHERE member_role.rolname IN" in sql
    assert "'REVOKE %I FROM %I'" in sql
    assert re.search(
        r"'REVOKE %I FROM %I',\s*granted_role_name,\s*capability_role_name",
        sql,
    )


def test_compose_bootstrap_creates_distinct_outbox_login() -> None:
    script = COMPOSE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "CAREEROPS_DB_OUTBOX_USER:?CAREEROPS_DB_OUTBOX_USER is required" in script
    assert "CAREEROPS_DB_OUTBOX_PASSWORD:?CAREEROPS_DB_OUTBOX_PASSWORD is required" in script
    assert '--set outbox_user="$CAREEROPS_DB_OUTBOX_USER"' in script
    assert 'CREATE ROLE :"outbox_user"' in script
    assert "LOGIN PASSWORD :'outbox_password'" in script
    assert 'GRANT careerops_outbox TO :"outbox_user";' in script


def test_compose_bootstrap_creates_distinct_workflow_login() -> None:
    script = COMPOSE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "CAREEROPS_DB_WORKFLOW_USER:?CAREEROPS_DB_WORKFLOW_USER is required" in script
    assert "CAREEROPS_DB_WORKFLOW_PASSWORD:?CAREEROPS_DB_WORKFLOW_PASSWORD is required" in script
    assert '--set workflow_user="$CAREEROPS_DB_WORKFLOW_USER"' in script
    assert 'CREATE ROLE :"workflow_user"' in script
    assert "LOGIN PASSWORD :'workflow_password'" in script
    assert 'GRANT careerops_workflow TO :"workflow_user";' in script


def test_compose_bootstrap_creates_distinct_mailbox_login() -> None:
    script = COMPOSE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "CAREEROPS_DB_MAILBOX_USER:?CAREEROPS_DB_MAILBOX_USER is required" in script
    assert "CAREEROPS_DB_MAILBOX_PASSWORD:?CAREEROPS_DB_MAILBOX_PASSWORD is required" in script
    assert '--set mailbox_user="$CAREEROPS_DB_MAILBOX_USER"' in script
    assert 'CREATE ROLE :"mailbox_user"' in script
    assert "LOGIN PASSWORD :'mailbox_password'" in script
    assert 'GRANT careerops_mailbox TO :"mailbox_user";' in script


def test_compose_bootstrap_creates_distinct_mail_sender_login() -> None:
    script = COMPOSE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert "CAREEROPS_DB_MAIL_SENDER_USER:?CAREEROPS_DB_MAIL_SENDER_USER is required" in script
    assert (
        "CAREEROPS_DB_MAIL_SENDER_PASSWORD:?CAREEROPS_DB_MAIL_SENDER_PASSWORD is required" in script
    )
    assert '--set mail_sender_user="$CAREEROPS_DB_MAIL_SENDER_USER"' in script
    assert 'CREATE ROLE :"mail_sender_user"' in script
    assert "LOGIN PASSWORD :'mail_sender_password'" in script
    assert 'GRANT careerops_mail_sender TO :"mail_sender_user";' in script


def test_compose_bootstrap_creates_distinct_greenhouse_sender_login() -> None:
    script = COMPOSE_BOOTSTRAP_PATH.read_text(encoding="utf-8")

    assert (
        "CAREEROPS_DB_GREENHOUSE_SENDER_USER:?CAREEROPS_DB_GREENHOUSE_SENDER_USER is required"
        in script
    )
    assert (
        "CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD:"
        "?CAREEROPS_DB_GREENHOUSE_SENDER_PASSWORD is required" in script
    )
    assert '--set greenhouse_sender_user="$CAREEROPS_DB_GREENHOUSE_SENDER_USER"' in script
    assert 'CREATE ROLE :"greenhouse_sender_user"' in script
    assert "LOGIN PASSWORD :'greenhouse_sender_password'" in script
    assert 'GRANT careerops_greenhouse_sender TO :"greenhouse_sender_user";' in script
