"""Bidirectional conversion tests (plan v0.4 §2.6, Stage 2).

Verifies the conversions module is the sole boundary between domain dataclasses
and JSON-safe checkpoint DTOs, and that required provenance
(``source_url`` / ``publicly_listed`` / ``company_id`` / envelope UUIDs) is
filled by the caller at the boundary — never fabricated from DTO state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.llm_matching import JobMatchResult, SkillProfile
from careerops.domain.contacts import (
    ContactAction,
    ContactConfidence,
    ContactSource,
    RecruitingContact,
)
from careerops.domain.email import DraftStatus, ReplyDraft
from careerops.orchestration.conversions import (
    JobProvenance,
    build_draft_dto,
    contact_dto_to_recruiting_contact,
    draft_dto_to_reply_draft,
    job_match_dto_to_result,
    job_match_result_to_dto,
    raw_job_dto_to_record,
    raw_job_record_to_dto,
    raw_job_records_to_dtos,
    recruiting_contact_to_dto,
    skill_profile_from_dto,
    skill_profile_to_dto,
)
from careerops.orchestration.state import (
    ContactDTO,
    DraftDTO,
    RawJobDTO,
    SkillProfileDTO,
)

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _record() -> RawJobRecord:
    return RawJobRecord(
        external_id="ext-1",
        title="Backend Engineer",
        location="SF",
        url="https://example.com/jobs/1",
        description="Python role",
        raw_data={"company": "Example", "description": "Python role", "n": 2},
    )


def _provenance() -> JobProvenance:
    return JobProvenance(
        source_url="https://example.com/api/jobs",
        fetched_at=NOW,
        response_hash="abc123",
        parser_version="greenhouse-v1",
    )


# ---------------------------------------------------------------------------
# RawJobRecord <-> RawJobDTO
# ---------------------------------------------------------------------------


class TestRawJobRecordToDto:
    def test_attaches_all_four_provenance_fields(self) -> None:
        dto = raw_job_record_to_dto(_record(), _provenance())
        assert dto.get("external_id") == "ext-1"
        assert dto.get("title") == "Backend Engineer"
        assert dto.get("description") == "Python role"
        assert dto.get("raw_data") == {
            "company": "Example",
            "description": "Python role",
            "n": 2,
        }
        assert dto.get("source_url") == "https://example.com/api/jobs"
        assert dto.get("fetched_at") == NOW.isoformat()
        assert dto.get("response_hash") == "abc123"
        assert dto.get("parser_version") == "greenhouse-v1"

    def test_fetched_at_serializes_as_aware_iso(self) -> None:
        dto = raw_job_record_to_dto(_record(), _provenance())
        parsed = datetime.fromisoformat(dto.get("fetched_at", ""))
        assert parsed.tzinfo is not None
        assert parsed == NOW

    def test_empty_provenance_omits_fields(self) -> None:
        dto = raw_job_record_to_dto(_record(), JobProvenance())
        assert "source_url" not in dto
        assert "fetched_at" not in dto
        assert "response_hash" not in dto
        assert "parser_version" not in dto
        # Core parser fields remain.
        assert dto.get("external_id") == "ext-1"
        assert dto.get("description") == "Python role"

    def test_raw_data_is_copied_not_aliased(self) -> None:
        record = _record()
        dto = raw_job_record_to_dto(record, _provenance())
        raw = dto.get("raw_data")
        assert isinstance(raw, dict)
        raw["injected"] = True
        assert "injected" not in record.raw_data


class TestRawJobDtoToRecord:
    def test_strips_provenance_keeps_parser_fields(self) -> None:
        dto = raw_job_record_to_dto(_record(), _provenance())
        assert raw_job_dto_to_record(dto) == _record()

    def test_record_dto_record_round_trip_is_lossless(self) -> None:
        original = _record()
        round_tripped = raw_job_dto_to_record(raw_job_record_to_dto(original, _provenance()))
        assert round_tripped == original

    def test_missing_raw_data_yields_empty_dict(self) -> None:
        dto: RawJobDTO = RawJobDTO(external_id="x", title="t")
        record = raw_job_dto_to_record(dto)
        assert record.raw_data == {}


class TestBatchRecordsToDtos:
    def test_shares_one_provenance_across_records(self) -> None:
        dtos = raw_job_records_to_dtos((_record(), _record()), _provenance())
        assert len(dtos) == 2
        assert all(d.get("parser_version") == "greenhouse-v1" for d in dtos)
        assert all(d.get("response_hash") == "abc123" for d in dtos)


# ---------------------------------------------------------------------------
# RecruitingContact <-> ContactDTO
# ---------------------------------------------------------------------------


def _contact() -> RecruitingContact:
    return RecruitingContact(
        id=UUID(int=1),
        company_id=UUID(int=2),
        email="hiring@example.com",
        name="Example",
        role="recruiter",
        source=ContactSource.JOB_PAGE,
        source_url="https://example.com/jobs/1",
        source_text="We are hiring",
        publicly_listed=True,
        confidence=ContactConfidence.HIGH,
        verified_at=NOW,
    )


class TestRecruitingContactToDto:
    def test_projects_core_fields_and_publicly_listed(self) -> None:
        dto = recruiting_contact_to_dto(_contact())
        assert dto.get("email") == "hiring@example.com"
        assert dto.get("publicly_listed") is True
        assert dto.get("platform") == "recruiter"
        assert dto.get("company_hint") == "Example"
        assert dto.get("post_type") == "job_page"
        assert dto.get("context") == "We are hiring"
        assert dto.get("extracted_at") == NOW.isoformat()

    def test_id_and_company_id_not_leaked_to_dto(self) -> None:
        dto = recruiting_contact_to_dto(_contact())
        # Provenance UUIDs belong to the domain layer; the DTO must not carry them.
        assert "id" not in dto
        assert "company_id" not in dto


class TestContactDtoToRecruitingContact:
    def _dto(self) -> ContactDTO:
        return ContactDTO(
            email="hiring@example.com",
            platform="greenhouse",
            company_hint="Example",
            context="We are hiring",
            publicly_listed=True,
            extracted_at=NOW.isoformat(),
        )

    def test_fills_required_provenance_from_caller(self) -> None:
        contact = contact_dto_to_recruiting_contact(
            self._dto(),
            contact_id=UUID(int=1),
            company_id=UUID(int=2),
            source_url="https://example.com/jobs/1",
        )
        assert contact.id == UUID(int=1)
        assert contact.company_id == UUID(int=2)
        assert contact.source_url == "https://example.com/jobs/1"
        assert contact.publicly_listed is True
        assert contact.email == "hiring@example.com"
        assert contact.verified_at == NOW

    def test_publicly_listed_false_is_rejected(self) -> None:
        dto = self._dto()
        dto["publicly_listed"] = False
        with pytest.raises(ValueError, match="publicly_listed"):
            contact_dto_to_recruiting_contact(
                dto,
                contact_id=UUID(int=1),
                company_id=UUID(int=2),
                source_url="https://example.com/jobs/1",
            )

    def test_domain_constructor_still_requires_source_url(self) -> None:
        # The domain invariant (fail-closed evidence) is enforced even though
        # the conversion accepts an empty string: passing empty raises.
        with pytest.raises(ValueError, match="source_url"):
            contact_dto_to_recruiting_contact(
                self._dto(),
                contact_id=UUID(int=1),
                company_id=UUID(int=2),
                source_url="",
            )

    def test_low_confidence_restricts_allowed_actions(self) -> None:
        contact = contact_dto_to_recruiting_contact(
            self._dto(),
            contact_id=UUID(int=1),
            company_id=UUID(int=2),
            source_url="https://example.com/jobs/1",
            confidence=ContactConfidence.LOW,
        )
        # Low confidence contacts may only be displayed/reviewed, never contacted.
        assert ContactAction.INITIATE_CONTACT not in contact.allowed_actions


# ---------------------------------------------------------------------------
# SkillProfile <-> SkillProfileDTO
# ---------------------------------------------------------------------------


class TestSkillProfileRoundTrip:
    def test_to_dto_preserves_tuple(self) -> None:
        profile = SkillProfile(
            skills=("python", "fastapi"),
            level="senior",
            years="5",
            highlights="sr engineer",
        )
        dto = skill_profile_to_dto(profile)
        assert dto.get("skills") == ("python", "fastapi")
        assert dto.get("level") == "senior"
        assert dto.get("years") == "5"
        assert dto.get("highlights") == "sr engineer"

    def test_round_trip_is_lossless(self) -> None:
        original = SkillProfile(
            skills=("python", "go"),
            level="staff",
            years="8",
            highlights="architect",
        )
        round_tripped = skill_profile_from_dto(skill_profile_to_dto(original))
        assert round_tripped == original

    def test_from_dto_handles_missing_skills(self) -> None:
        dto: SkillProfileDTO = SkillProfileDTO(level="junior")
        profile = skill_profile_from_dto(dto)
        assert profile.skills == ()
        assert profile.level == "junior"


# ---------------------------------------------------------------------------
# JobMatchResult <-> JobMatchDTO
# ---------------------------------------------------------------------------


class TestJobMatchRoundTrip:
    def _result(self) -> JobMatchResult:
        return JobMatchResult(
            company="Example",
            title="Backend Engineer",
            match_score=72,
            tier="partial",
            matched_requirements=("python", "fastapi"),
            gaps=("rust",),
            transferable_skills=("go",),
            seniority_fit="match",
            remote_compatible=True,
            reasoning="good fit",
            recommendation="consider",
            confidence=0.8,
            model_id="claude-test",
            is_review_only=True,
            error="",
        )

    def test_to_dto_drops_advisory_extras(self) -> None:
        dto = job_match_result_to_dto("ext-1", self._result())
        assert dto.get("external_id") == "ext-1"
        assert dto.get("match_score") == 72
        assert dto.get("matched_requirements") == ("python", "fastapi")
        assert dto.get("remote_compatible") is True
        assert dto.get("is_review_only") is True
        # Fields absent from the DTO contract.
        assert "transferable_skills" not in dto
        assert "confidence" not in dto
        assert "model_id" not in dto

    def test_dto_to_result_defaults_dropped_fields(self) -> None:
        dto = job_match_result_to_dto("ext-1", self._result())
        result = job_match_dto_to_result(dto)
        assert result.company == "Example"
        assert result.match_score == 72
        assert result.matched_requirements == ("python", "fastapi")
        # Dropped fields take their defaults.
        assert result.transferable_skills == ()
        assert result.confidence == 0.0
        assert result.model_id == ""
        assert result.is_review_only is True

    def test_disabled_model_error_round_trips(self) -> None:
        disabled = JobMatchResult(company="Example", title="Eng", error="model provider disabled")
        dto = job_match_result_to_dto("ext-1", disabled)
        assert dto.get("error") == "model provider disabled"
        assert dto.get("recommendation") == "skip"
        assert dto.get("is_review_only") is True


# ---------------------------------------------------------------------------
# (subject, body) -> DraftDTO  +  DraftDTO -> ReplyDraft envelope
# ---------------------------------------------------------------------------


class TestBuildDraftDto:
    def test_assembles_envelope(self) -> None:
        draft = build_draft_dto(
            job_external_id="ext-1",
            recipient="hiring@example.com",
            subject="Application: Backend",
            body="Dear Example, ...",
            draft_id="draft-1",
        )
        assert draft.get("id") == "draft-1"
        assert draft.get("job_external_id") == "ext-1"
        assert draft.get("recipient") == "hiring@example.com"
        assert draft.get("subject") == "Application: Backend"
        assert draft.get("revision") == 0
        assert "payload_hash" not in draft  # omitted when not supplied

    def test_payload_hash_included_when_supplied(self) -> None:
        draft = build_draft_dto(
            job_external_id="ext-1",
            recipient="hiring@example.com",
            subject="s",
            body="b",
            draft_id="draft-1",
            payload_hash="a" * 64,
        )
        assert draft.get("payload_hash") == "a" * 64


class TestDraftDtoToReplyDraft:
    def _draft(self) -> DraftDTO:
        return DraftDTO(
            id=str(uuid4()),
            job_external_id="ext-1",
            recipient="hiring@example.com",
            subject="Application: Backend",
            body="Dear Example, ...",
            revision=0,
            payload_hash="a" * 64,
        )

    def test_fills_envelope_placeholders(self) -> None:
        draft = self._draft()
        message_id = uuid4()
        thread_id = uuid4()
        account_id = uuid4()
        reply = draft_dto_to_reply_draft(
            draft,
            message_id=message_id,
            thread_id=thread_id,
            account_id=account_id,
        )
        assert isinstance(reply, ReplyDraft)
        assert reply.id == UUID(draft.get("id", ""))
        assert reply.message_id == message_id
        assert reply.thread_id == thread_id
        assert reply.account_id == account_id
        assert reply.to_address == "hiring@example.com"
        assert reply.subject == "Application: Backend"
        assert reply.body_text == "Dear Example, ..."
        assert reply.payload_hash == "a" * 64
        assert reply.status is DraftStatus.DRAFT

    def test_does_not_invent_envelope_uuids(self) -> None:
        # The caller must supply all three envelope UUIDs; the conversion never
        # mints them (v1 send boundary passes placeholders explicitly).
        draft = self._draft()
        with pytest.raises(TypeError):
            draft_dto_to_reply_draft(draft)  # type: ignore[call-arg]

    def test_raises_on_missing_draft_id(self) -> None:
        draft: DraftDTO = DraftDTO(recipient="x", subject="s", body="b")
        with pytest.raises(ValueError, match="UUID id"):
            draft_dto_to_reply_draft(
                draft, message_id=uuid4(), thread_id=uuid4(), account_id=uuid4()
            )
