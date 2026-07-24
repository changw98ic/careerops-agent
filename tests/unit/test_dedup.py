"""Semantic deduplication tests (Task D).

``dedup_jobs`` is deterministic and pure: identical input + criteria produce
identical output. SimHash is locality-sensitive: near-duplicate descriptions
(within the Hamming distance threshold) are detected and the first occurrence
is always kept.
"""

from __future__ import annotations

import pytest

from careerops.orchestration.dedup import (
    DedupCriteria,
    DedupResult,
    DuplicateJob,
    dedup_jobs,
    hamming_distance,
    simhash,
    strip_html,
)
from careerops.orchestration.state import RawJobDTO

_SAMPLE_DESC = (
    "We are looking for a senior backend engineer with Python experience to join our team"
)
_SAMPLE_DESC_VARIANT = (
    "We are looking for a senior backend engineer with Python experience to join the team"
)


def _job(
    *,
    external_id: str,
    title: str = "Eng",
    description: str = "A role",
) -> RawJobDTO:
    return RawJobDTO(
        external_id=external_id,
        title=title,
        description=description,
    )


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------


class TestStripHtml:
    def test_strips_simple_tags(self) -> None:
        html = "<p>Hello <b>world</b></p>"
        assert strip_html(html) == "Hello world"

    def test_strips_nested_tags(self) -> None:
        html = "<div><span>Deep <em>text</em></span></div>"
        assert strip_html(html) == "Deep text"

    def test_normalizes_whitespace(self) -> None:
        html = "<p>  Lots   of\n\tspaced  text  </p>"
        assert strip_html(html) == "Lots of spaced text"

    def test_empty_string_returns_empty(self) -> None:
        assert strip_html("") == ""

    def test_plain_text_passes_through(self) -> None:
        assert strip_html("no html here") == "no html here"


# ---------------------------------------------------------------------------
# simhash
# ---------------------------------------------------------------------------


class TestSimhash:
    def test_simhash_deterministic(self) -> None:
        """Same text always produces the same hash."""
        text = "We are looking for a senior backend engineer with Python experience"
        h1 = simhash(text)
        h2 = simhash(text)
        assert h1 == h2

    def test_simhash_empty_string(self) -> None:
        assert simhash("") == 0

    def test_simhash_case_insensitive(self) -> None:
        assert simhash("Hello World") == simhash("hello world")

    def test_simhash_similar_texts_have_low_distance(self) -> None:
        """Nearly identical descriptions should have a small Hamming distance."""
        text1 = (
            "We are looking for a senior backend engineer with Python experience to join our team"
        )
        # Only whitespace/case difference -> same tokens after lower+split.
        text2 = (
            "we are looking for a senior backend engineer with python experience to join our team"
        )
        dist = hamming_distance(simhash(text1), simhash(text2))
        assert dist == 0

    def test_simhash_different_texts_have_high_distance(self) -> None:
        """Unrelated descriptions should have a large Hamming distance."""
        text1 = "We are looking for a senior backend engineer with Python experience"
        text2 = "Marketing coordinator needed for social media campaigns and event planning"
        dist = hamming_distance(simhash(text1), simhash(text2))
        assert dist > 3


# ---------------------------------------------------------------------------
# hamming_distance
# ---------------------------------------------------------------------------


class TestHammingDistance:
    def test_identical_values_distance_zero(self) -> None:
        assert hamming_distance(0, 0) == 0
        assert hamming_distance(0xFF, 0xFF) == 0

    def test_single_bit_diff(self) -> None:
        assert hamming_distance(0b0, 0b1) == 1
        assert hamming_distance(0b1010, 0b1000) == 1

    def test_max_distance_64bit(self) -> None:
        assert hamming_distance(0, (1 << 64) - 1) == 64


# ---------------------------------------------------------------------------
# dedup_jobs — keeps first occurrence
# ---------------------------------------------------------------------------


class TestDedupKeepsFirstOccurrence:
    def test_unique_jobs_all_kept(self) -> None:
        jobs = (
            _job(external_id="1", description="Backend engineer Python Django"),
            _job(external_id="2", description="Frontend developer React TypeScript"),
            _job(external_id="3", description="DevOps engineer Kubernetes AWS"),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1", "2", "3"]
        assert result.duplicates == ()

    def test_keeps_first_of_duplicate_pair(self) -> None:
        """When two jobs have near-identical descriptions, the first is kept."""
        desc = (
            "We are looking for a senior backend engineer with Python experience to join our team"
        )
        jobs = (
            _job(external_id="1", description=desc),
            _job(external_id="2", description=desc),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 1
        assert result.duplicates[0].external_id == "2"


# ---------------------------------------------------------------------------
# dedup_jobs — drops duplicates
# ---------------------------------------------------------------------------


class TestDedupDropsDuplicates:
    def test_duplicate_recorded_with_distance(self) -> None:
        desc = _SAMPLE_DESC
        jobs = (
            _job(external_id="1", description=desc),
            _job(external_id="2", description=desc),
        )
        result = dedup_jobs(jobs)
        dup = result.duplicates[0]
        assert dup.external_id == "2"
        assert dup.distance == 0  # identical text = distance 0

    def test_multiple_duplicates_dropped(self) -> None:
        desc = "Identical job description for testing deduplication"
        jobs = (
            _job(external_id="1", description=desc),
            _job(external_id="2", description=desc),
            _job(external_id="3", description=desc),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 2

    def test_empty_description_not_deduped(self) -> None:
        """Empty descriptions have hash 0; two empties are duplicates."""
        jobs = (
            _job(external_id="1", description=""),
            _job(external_id="2", description=""),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 1


# ---------------------------------------------------------------------------
# dedup_jobs — configurable threshold
# ---------------------------------------------------------------------------


class TestDedupConfigurableThreshold:
    def test_threshold_0_only_drops_exact(self) -> None:
        """With threshold=0, only identical hashes are duplicates."""
        desc = _SAMPLE_DESC
        similar = _SAMPLE_DESC_VARIANT
        jobs = (
            _job(external_id="1", description=desc),
            _job(external_id="2", description=similar),
        )
        result = dedup_jobs(jobs, criteria=DedupCriteria(threshold=0))
        # At threshold=0, only exact duplicates are caught; these are slightly
        # different so both should be kept (distance > 0).
        assert len(result.kept) == 2
        assert result.duplicates == ()

    def test_higher_threshold_catches_more(self) -> None:
        """A higher threshold catches more near-duplicates."""
        desc1 = _SAMPLE_DESC
        desc2 = _SAMPLE_DESC_VARIANT
        jobs = (
            _job(external_id="1", description=desc1),
            _job(external_id="2", description=desc2),
        )
        # With threshold=10, near-duplicates should be caught.
        result = dedup_jobs(jobs, criteria=DedupCriteria(threshold=10))
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 1

    def test_default_threshold_is_3(self) -> None:
        crit = DedupCriteria()
        assert crit.threshold == 3


# ---------------------------------------------------------------------------
# dedup_jobs — HTML descriptions
# ---------------------------------------------------------------------------


class TestDedupWithHtmlDescriptions:
    def test_html_stripped_before_hashing(self) -> None:
        """HTML-wrapped and plain-text versions of the same content are duplicates."""
        plain = _SAMPLE_DESC
        html = f"<p>{_SAMPLE_DESC}</p>"
        jobs = (
            _job(external_id="1", description=plain),
            _job(external_id="2", description=html),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 1

    def test_html_with_formatting_still_matches(self) -> None:
        """HTML formatting differences are stripped; content match triggers dedup."""
        html1 = "<div><h2>Backend Engineer</h2><p>Python <b>required</b></p></div>"
        html2 = "<div><h2>Backend Engineer</h2><p>Python <em>required</em></p></div>"
        jobs = (
            _job(external_id="1", description=html1),
            _job(external_id="2", description=html2),
        )
        result = dedup_jobs(jobs)
        assert [j.get("external_id") for j in result.kept] == ["1"]
        assert len(result.duplicates) == 1


# ---------------------------------------------------------------------------
# result dataclasses
# ---------------------------------------------------------------------------


class TestResultDataclasses:
    def test_dedup_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = DedupResult()
        with pytest.raises(FrozenInstanceError):
            result.kept = ()  # type: ignore[misc]

    def test_duplicate_job_carries_identity_and_distance(self) -> None:
        d = DuplicateJob(external_id="x", title="t", distance=2)
        assert d.external_id == "x"
        assert d.distance == 2

    def test_count_returns_number_of_duplicates(self) -> None:
        result = DedupResult(
            duplicates=(
                DuplicateJob(external_id="1", title="a", distance=0),
                DuplicateJob(external_id="2", title="b", distance=1),
            )
        )
        assert result.count() == 2


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_is_deterministic_across_calls(self) -> None:
        jobs = tuple(
            _job(external_id=str(i), description=f"job description number {i}") for i in range(5)
        )
        first = dedup_jobs(jobs)
        second = dedup_jobs(jobs)
        assert first == second

    def test_empty_input_returns_empty_result(self) -> None:
        result = dedup_jobs(())
        assert result.kept == ()
        assert result.duplicates == ()
        assert result.count() == 0
