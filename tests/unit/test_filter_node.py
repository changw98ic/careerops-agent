"""Pure job-filter tests (plan v0.4 §2.2, §2.6 Stage 2).

``filter_jobs`` is deterministic and pure: identical input + criteria produce
identical output, which ``review_gate`` resume relies on. It is NOT a match
decision (ADR 0006) — it only drops jobs that fail coverage / remote /
direction / region / salary gates.
"""

from __future__ import annotations

import pytest

from careerops.orchestration.filter_node import (
    FilterCriteria,
    FilterResult,
    RejectedJob,
    filter_jobs,
)
from careerops.orchestration.nodes import filter_node
from careerops.orchestration.state import RawJobDTO


def _job(
    *,
    external_id: str,
    title: str = "Eng",
    location: str = "SF",
    description: str = "A role",
    raw_data: dict[str, object] | None = None,
) -> RawJobDTO:
    dto: RawJobDTO = RawJobDTO(
        external_id=external_id,
        title=title,
        location=location,
        description=description,
    )
    if raw_data is not None:
        dto["raw_data"] = raw_data
    return dto


# ---------------------------------------------------------------------------
# description coverage (always enforced)
# ---------------------------------------------------------------------------


class TestDescriptionCoverage:
    def test_empty_description_rejected_even_with_no_other_criteria(self) -> None:
        jobs = (_job(external_id="1", description=""),)
        result = filter_jobs(jobs)
        assert result.kept == ()
        assert result.rejected[0].reason == "empty_description"
        assert result.count_by_reason() == {"empty_description": 1}

    def test_whitespace_only_description_rejected(self) -> None:
        jobs = (_job(external_id="1", description="   \t  "),)
        result = filter_jobs(jobs)
        assert result.kept == ()
        assert result.rejected[0].reason == "empty_description"

    def test_non_empty_description_kept_by_default(self) -> None:
        jobs = (_job(external_id="1", description="real JD"),)
        result = filter_jobs(jobs)
        assert len(result.kept) == 1
        assert result.rejected == ()


# ---------------------------------------------------------------------------
# remote
# ---------------------------------------------------------------------------


class TestRemoteFilter:
    def test_remote_only_keeps_remote_jobs(self) -> None:
        jobs = (
            _job(external_id="1", description="Fully remote role anywhere"),
            _job(external_id="2", description="Office-only in NYC"),
        )
        result = filter_jobs(jobs, criteria=FilterCriteria(remote_only=True))
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert result.rejected[0].reason == "not_remote"

    def test_remote_keyword_in_location_matches(self) -> None:
        jobs = (_job(external_id="1", description="role", location="Remote, US"),)
        result = filter_jobs(jobs, criteria=FilterCriteria(remote_only=True))
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# direction
# ---------------------------------------------------------------------------


class TestDirectionFilter:
    def test_direction_match_keeps_job(self) -> None:
        jobs = (
            _job(external_id="1", title="Backend Engineer", description="build apis"),
            _job(external_id="2", title="Designer", description="ui work"),
        )
        result = filter_jobs(jobs, criteria=FilterCriteria(directions=("backend",)))
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert result.rejected[0].reason == "direction_mismatch"

    def test_direction_match_is_case_insensitive(self) -> None:
        jobs = (_job(external_id="1", title="BACKEND developer", description="apis"),)
        result = filter_jobs(jobs, criteria=FilterCriteria(directions=("Backend", " Fullstack ")))
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# region
# ---------------------------------------------------------------------------


class TestRegionFilter:
    def test_region_match_keeps_job(self) -> None:
        jobs = (
            _job(external_id="1", description="role", location="San Francisco, CA"),
            _job(external_id="2", description="role", location="Berlin, DE"),
        )
        result = filter_jobs(jobs, criteria=FilterCriteria(regions=("san francisco", "bay area")))
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert result.rejected[0].reason == "region_mismatch"


# ---------------------------------------------------------------------------
# salary
# ---------------------------------------------------------------------------


class TestSalaryFilter:
    def test_explicit_subfloor_salary_rejected(self) -> None:
        jobs = (
            _job(
                external_id="1",
                description="role",
                raw_data={"salary_min": 80000},
            ),
            _job(
                external_id="2",
                description="role",
                raw_data={"min_salary": "150000"},
            ),
        )
        result = filter_jobs(jobs, criteria=FilterCriteria(min_salary=100000))
        assert [j.get("external_id") for j in result.kept] == ["2"]
        assert result.rejected[0].reason == "salary_below_minimum"

    def test_missing_salary_data_is_kept(self) -> None:
        # Absence of salary is not a rejection signal.
        jobs = (_job(external_id="1", description="role"),)
        result = filter_jobs(jobs, criteria=FilterCriteria(min_salary=100000))
        assert len(result.kept) == 1
        assert result.rejected == ()

    def test_salary_floor_string_parsed(self) -> None:
        jobs = (
            _job(
                external_id="1",
                description="role",
                raw_data={"salaryMin": "$120,000"},
            ),
        )
        result = filter_jobs(jobs, criteria=FilterCriteria(min_salary=100000))
        assert len(result.kept) == 1


# ---------------------------------------------------------------------------
# combined + determinism + defaults
# ---------------------------------------------------------------------------


class TestCombinedAndDeterminism:
    def test_multiple_criteria_all_apply(self) -> None:
        jobs = (
            _job(
                external_id="1",
                title="Backend",
                description="remote role",
                location="SF",
                raw_data={"salary_min": 150000},
            ),
            _job(external_id="2", title="Backend", description="onsite", location="SF"),
            _job(external_id="3", description="", location="SF"),
        )
        result = filter_jobs(
            jobs,
            criteria=FilterCriteria(remote_only=True, directions=("backend",), min_salary=100000),
        )
        assert [j.get("external_id") for j in result.kept] == ["1"]
        reasons = {r.external_id: r.reason for r in result.rejected}
        assert reasons == {"2": "not_remote", "3": "empty_description"}

    def test_is_deterministic_across_calls(self) -> None:
        jobs = (
            _job(external_id=str(i), description=f"job {i}", location="remote") for i in range(5)
        )
        jobs_tuple = tuple(jobs)
        first = filter_jobs(jobs_tuple, criteria=FilterCriteria(remote_only=True))
        second = filter_jobs(jobs_tuple, criteria=FilterCriteria(remote_only=True))
        assert first == second

    def test_default_criteria_normalizes_blank_entries(self) -> None:
        crit = FilterCriteria(directions=("backend", "", "  ", "Frontend"))
        assert crit.normalized_directions() == ("backend", "frontend")

    def test_empty_input_returns_empty_result(self) -> None:
        result = filter_jobs(())
        assert result.kept == ()
        assert result.rejected == ()
        assert result.count_by_reason() == {}


# ---------------------------------------------------------------------------
# filter_node wiring
# ---------------------------------------------------------------------------


class TestFilterNodeWiring:
    def test_node_overwrites_jobs_and_records_errors(self) -> None:
        state = {
            "raw_job_records": (
                _job(external_id="1", description="real"),
                _job(external_id="2", description=""),
            )
        }
        update = filter_node(state)  # type: ignore[arg-type]
        # Kept set overwrites raw_job_records.
        kept = update["raw_job_records"]
        assert [j.get("external_id") for j in kept] == ["1"]
        # Rejection reason surfaced as ErrorDTO entries.
        errors = update["errors"]
        assert errors[0].get("node") == "filter"
        assert errors[0].get("error_type") == "empty_description"
        assert errors[0].get("message") == "2:Eng"

    def test_node_accepts_criteria(self) -> None:
        state = {
            "raw_job_records": (
                _job(external_id="1", description="remote role"),
                _job(external_id="2", description="office only"),
            )
        }
        update = filter_node(state, criteria=FilterCriteria(remote_only=True))  # type: ignore[arg-type]
        kept = update["raw_job_records"]
        assert [j.get("external_id") for j in kept] == ["1"]
        assert update["errors"][0].get("error_type") == "not_remote"

    def test_node_no_jobs_returns_empty(self) -> None:
        update = filter_node({})  # type: ignore[arg-type]
        assert update["raw_job_records"] == ()
        assert update["errors"] == ()


# ---------------------------------------------------------------------------
# result dataclasses
# ---------------------------------------------------------------------------


class TestResultDataclasses:
    def test_filter_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = FilterResult()
        with pytest.raises(FrozenInstanceError):
            result.kept = ()  # type: ignore[misc]

    def test_rejected_job_carries_identity_and_reason(self) -> None:
        r = RejectedJob(external_id="x", title="t", reason="empty_description")
        assert r.external_id == "x"
        assert r.reason == "empty_description"

    def test_count_by_reason_aggregates(self) -> None:
        result = FilterResult(
            rejected=(
                RejectedJob(external_id="1", title="a", reason="empty_description"),
                RejectedJob(external_id="2", title="b", reason="empty_description"),
                RejectedJob(external_id="3", title="c", reason="not_remote"),
            )
        )
        assert result.count_by_reason() == {"empty_description": 2, "not_remote": 1}
