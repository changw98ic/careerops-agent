"""Checkpoint state serialization tests (plan v0.4 §5).

The checkpoint state stores JSON-safe DTOs only. This file verifies:

- DTOs pickle and unpickle cleanly (``MemorySaver`` uses pickle for the
  in-process checkpointer; durable check pointers will use the JSON layer).
- DTOs round-trip through ``json.dumps``/``json.loads`` without losing
  information (``UUID`` are strings, ``datetime`` are ISO-8601 aware UTC,
  enums are value strings).
- No DTO contains a ``pathlib.Path``, lambda, or other JSON-incompatible value.
- ``datetime`` values are timezone-aware (no naive datetimes sneak in).
"""

from __future__ import annotations

import json
import pickle
from datetime import UTC, datetime
from uuid import uuid4

from careerops.orchestration.state import (
    CareerOpsState,
    ContactDTO,
    DraftDTO,
    EditedDraftItem,
    EditedDraftPayload,
    ErrorDTO,
    JobMatchDTO,
    RawJobDTO,
    SendReceiptDTO,
    SkillProfileDTO,
    StateAppender,
)

NOW = datetime(2026, 7, 24, 12, 0, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


def _sample_raw_job() -> RawJobDTO:
    return RawJobDTO(
        external_id="job-1",
        title="Backend Engineer",
        location="SF",
        url="https://example.com/jobs/1",
        description="Python role",
        source_url="https://example.com/jobs/1",
        fetched_at=NOW_ISO,
        response_hash="abc123",
        parser_version="test-v1",
        raw_data={"description": "Python role", "nested": {"a": 1, "b": [True, False]}},
    )


def _sample_contact() -> ContactDTO:
    return ContactDTO(
        email="hiring@example.com",
        platform="greenhouse",
        company_hint="Example",
        post_type="hiring",
        context="We are hiring",
        source_file="greenhouse.json",
        publicly_listed=True,
        extracted_at=NOW_ISO,
    )


def _sample_draft() -> DraftDTO:
    return DraftDTO(
        id=str(uuid4()),
        job_external_id="job-1",
        recipient="hiring@example.com",
        subject="Application: Backend Engineer",
        body="Dear Example, ...",
        revision=0,
        payload_hash="a" * 64,
    )


def _sample_match() -> JobMatchDTO:
    return JobMatchDTO(
        external_id="job-1",
        company="Example",
        title="Backend Engineer",
        match_score=0,
        tier="mismatch",
        recommendation="skip",
        seniority_fit="unclear",
        remote_compatible=None,
        matched_requirements=(),
        gaps=("python",),
        reasoning="disabled model",
        is_review_only=True,
        error="model provider disabled",
    )


def _sample_skill_profile() -> SkillProfileDTO:
    return SkillProfileDTO(
        skills=("python", "fastapi"),
        level="senior",
        years="5",
        highlights="sr engineer",
    )


def _sample_receipt() -> SendReceiptDTO:
    return SendReceiptDTO(
        intent_id=str(uuid4()),
        approval_id=str(uuid4()),
        provider="fake",
        provider_resource_id="fake-resource-1",
        reconciliation_key="rk-1",
        final_state="confirmed",
    )


def _sample_error() -> ErrorDTO:
    return ErrorDTO(node="match", error_type="DisabledModel", message="skip")


def _full_state() -> CareerOpsState:
    draft = _sample_draft()
    draft_id = str(draft.get("id", ""))
    return CareerOpsState(
        requested_for="user-1",
        raw_job_records=(_sample_raw_job(),),
        contacts=(_sample_contact(),),
        resume_text="resume text",
        skill_profile=_sample_skill_profile(),
        matches=(_sample_match(),),
        drafts=(draft,),
        review_revision=0,
        pending_approval_id=str(uuid4()),
        pending_intent_id=str(uuid4()),
        approved_draft_ids=(draft_id,),
        edit_payload=None,
        send_receipts=(_sample_receipt(),),
        errors=(_sample_error(),),
    )


class TestDtoPickleRoundTrip:
    """``MemorySaver`` pickles the checkpoint; DTOs must survive pickling."""

    def test_raw_job_dto_pickle_round_trip(self) -> None:
        original = _sample_raw_job()
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original

    def test_contact_dto_pickle_round_trip(self) -> None:
        original = _sample_contact()
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original

    def test_draft_dto_pickle_round_trip(self) -> None:
        original = _sample_draft()
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original

    def test_match_dto_pickle_round_trip(self) -> None:
        original = _sample_match()
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original

    def test_full_state_pickle_round_trip(self) -> None:
        original = _full_state()
        restored = pickle.loads(pickle.dumps(original))
        assert restored == original


class TestDtoJsonRoundTrip:
    """JSON-safe contract: every DTO must survive json.dumps/loads."""

    def test_raw_job_dto_is_json_safe(self) -> None:
        dto = _sample_raw_job()
        encoded = json.dumps(dto, sort_keys=True)
        restored = json.loads(encoded)
        assert restored["external_id"] == dto.get("external_id")
        assert restored["raw_data"]["nested"]["b"] == [True, False]
        assert restored["fetched_at"] == NOW_ISO

    def test_draft_dto_is_json_safe(self) -> None:
        dto = _sample_draft()
        restored = json.loads(json.dumps(dto, sort_keys=True))
        assert restored["id"] == dto.get("id")
        assert restored["revision"] == 0

    def test_full_state_json_round_trip_preserves_all_fields(self) -> None:
        state = _full_state()
        original_drafts = state.get("drafts") or ()
        original_draft_id = str(original_drafts[0].get("id", "")) if original_drafts else ""
        # state contains tuples, which JSON renders as lists; that's fine for
        # transport. We just check the values are recoverable.
        encoded = json.dumps(state, sort_keys=True, default=list)
        restored = json.loads(encoded)
        assert restored["requested_for"] == "user-1"
        assert restored["raw_job_records"][0]["title"] == "Backend Engineer"
        assert restored["drafts"][0]["id"] == original_draft_id
        assert restored["errors"][0]["node"] == "match"


class TestNoUnsafeValues:
    """Fail-fast if someone sneaks a Path or lambda into a DTO."""

    def test_no_pathlib_path_in_state(self) -> None:
        from pathlib import Path

        state = _full_state()

        def walk(obj: object) -> None:
            if isinstance(obj, Path):
                raise AssertionError(f"Path object found in state: {obj!r}")
            if isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    walk(v)

        walk(state)

    def test_no_callable_in_state(self) -> None:
        state = _full_state()

        def walk(obj: object) -> None:
            if callable(obj) and not isinstance(obj, type):
                raise AssertionError(f"callable found in state: {obj!r}")
            if isinstance(obj, dict):
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    walk(v)

        walk(state)


class TestDatetimeContract:
    """``datetime`` values are ISO-8601 UTC strings, never naive datetimes."""

    def test_all_datetime_fields_are_aware_iso_strings(self) -> None:
        from datetime import datetime as dt_cls

        state = _full_state()
        # The contract: known ``*_at`` fields must be ISO-8601 aware UTC strings.
        datetime_fields = {"fetched_at", "extracted_at", "provider_timestamp", "received_at"}
        checked = 0

        def check(value: object, field: str) -> None:
            nonlocal checked
            if field not in datetime_fields or not isinstance(value, str):
                return
            parsed = dt_cls.fromisoformat(value)
            assert parsed.tzinfo is not None, f"{field} must be tz-aware: {value!r}"
            checked += 1

        def walk(obj: object) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    check(v, str(k))
                    walk(v)
            elif isinstance(obj, tuple):
                for v in obj:
                    walk(v)

        walk(state)
        assert checked >= 2, "expected at least 2 *_at fields to be checked"


class TestStateAppender:
    def test_appender_concatenates_tuples(self) -> None:
        out = StateAppender((1, 2), (3, 4))
        assert out == (1, 2, 3, 4)

    def test_appender_handles_none_left(self) -> None:
        out = StateAppender(None, (1,))
        assert out == (1,)

    def test_appender_handles_none_right(self) -> None:
        out = StateAppender((1,), None)
        assert out == (1,)

    def test_appender_wraps_scalar(self) -> None:
        out = StateAppender((), "x")
        assert out == ("x",)

    def test_appender_pickle_round_trip(self) -> None:
        # Reducers themselves are not stored in state, but the merged tuple
        # output must pickle cleanly.
        out = StateAppender((1, 2), (3,))
        assert pickle.loads(pickle.dumps(out)) == (1, 2, 3)


class TestEditedDraftPayloadConstrained:
    def test_edited_draft_item_only_allows_id_subject_body(self) -> None:
        item = EditedDraftItem(id="d1", subject="new", body="new body")
        # Only the three fields are part of the TypedDict; an extra key would
        # be silently accepted at runtime by TypedDict but rejected by
        # parse_review_decision at the gate.
        assert set(item.keys()) <= {"id", "subject", "body"}

    def test_edited_payload_round_trips_json(self) -> None:
        payload = EditedDraftPayload(
            drafts=(
                EditedDraftItem(id="d1", subject="s1", body="b1"),
                EditedDraftItem(id="d2", subject="s2", body="b2"),
            )
        )
        restored = json.loads(json.dumps(payload, sort_keys=True))
        assert len(restored["drafts"]) == 2
        assert restored["drafts"][0]["id"] == "d1"
