from __future__ import annotations

from uuid import uuid4

from careerops.infrastructure.database.release_evidence import (
    compile_query_for_test,
    select_audit_events_statement,
    select_cap_reservations_statement,
    select_kill_switch_events_statement,
    select_provider_receipts_statement,
    select_release_qualification_decisions_statement,
    select_release_qualification_evidence_statement,
    select_release_qualification_statement,
)


def test_release_qualification_drilldown_queries_are_read_only_and_ordered() -> None:
    qualification_id = uuid4()

    qualification_sql = compile_query_for_test(
        select_release_qualification_statement(qualification_id)
    )
    evidence_sql = compile_query_for_test(
        select_release_qualification_evidence_statement(qualification_id)
    )
    decisions_sql = compile_query_for_test(
        select_release_qualification_decisions_statement(qualification_id)
    )
    joined = "\n".join((qualification_sql, evidence_sql, decisions_sql)).upper()

    assert "FROM CAREEROPS.RELEASE_QUALIFICATIONS" in joined
    assert "FROM CAREEROPS.RELEASE_QUALIFICATION_EVIDENCE" in joined
    assert "FROM CAREEROPS.RELEASE_QUALIFICATION_DECISIONS" in joined
    assert "ORDER BY" in evidence_sql
    assert "ORDER BY" in decisions_sql
    assert "INSERT " not in joined
    assert "UPDATE " not in joined
    assert "DELETE " not in joined
    assert "FOR UPDATE" not in joined


def test_intent_trace_queries_cover_execution_audit_and_kill_switch_chain() -> None:
    intent_id = uuid4()

    reservation_sql = compile_query_for_test(select_cap_reservations_statement(intent_id))
    receipt_sql = compile_query_for_test(select_provider_receipts_statement(intent_id))
    audit_sql = compile_query_for_test(select_audit_events_statement(intent_id))
    kill_sql = compile_query_for_test(select_kill_switch_events_statement(intent_id))
    joined = "\n".join((reservation_sql, receipt_sql, audit_sql, kill_sql)).upper()

    assert "CAREEROPS.AUTOPILOT_CAP_RESERVATIONS" in joined
    assert "CAREEROPS.SIDE_EFFECT_ATTEMPTS" in joined
    assert "CAREEROPS.PROVIDER_RECEIPTS" in joined
    assert "CAREEROPS.AUDIT_EVENTS" in joined
    assert "CAREEROPS.AUTOPILOT_KILL_SWITCH_EVENTS" in joined
    assert "ACTION_INTENT_ID" in joined
    assert "PROVIDER IN (SELECT DISTINCT" in kill_sql.upper()
    assert "PROVIDER = (SELECT" not in kill_sql.upper()
    assert "ROW_NUMBER() OVER" in kill_sql.upper()
    assert "CREATED_AT <= COALESCE" in kill_sql.upper()
    assert "STATE_RANK = 1" in kill_sql.upper()
    assert "DECISION_ANCHOR_AT" in kill_sql.upper()
    assert "ORDER BY" in joined
    assert "INSERT " not in joined
    assert "UPDATE " not in joined
    assert "DELETE " not in joined
    assert "FOR UPDATE" not in joined
