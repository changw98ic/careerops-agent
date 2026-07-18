from __future__ import annotations

import re
from pathlib import Path

EXPECTED_CAPABILITY_ROLES = {
    "careerops_api",
    "careerops_outbox",
    "careerops_readonly",
    "careerops_retention",
    "careerops_side_effect",
}

BOOTSTRAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "careerops"
    / "infrastructure"
    / "database"
    / "bootstrap_roles.sql"
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
