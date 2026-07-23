"""Cross-source dedup dataset evaluator (M1.12)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DedupPair:
    """A labeled pair of postings for dedup evaluation."""

    posting_a_id: str
    posting_b_id: str
    is_duplicate: bool
    source_a: str = ""
    source_b: str = ""


@dataclass(frozen=True, slots=True)
class DedupPrediction:
    """A predicted duplicate pair."""

    posting_a_id: str
    posting_b_id: str
    score: float
    rule: str


@dataclass(frozen=True, slots=True)
class DedupMetrics:
    """Precision/recall/F1 for dedup evaluation."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    total_pairs: int


@dataclass(frozen=True, slots=True)
class CloseReopenCase:
    """A test case for close/reopen canonical state logic."""

    case_id: str
    postings: tuple[PostingState, ...] = ()
    expected_canonical_state: str = "active"


@dataclass(frozen=True, slots=True)
class PostingState:
    posting_id: str
    source_state: str


class DedupDataset(Protocol):
    def pairs(self) -> list[DedupPair]: ...

    def predictions(self) -> list[DedupPrediction]: ...


def evaluate_dedup(pairs: list[DedupPair], predictions: list[DedupPrediction]) -> DedupMetrics:
    """Evaluate dedup predictions against labeled pairs.

    Computes pairwise precision, recall, and F1.
    """
    labeled_positive = {(p.posting_a_id, p.posting_b_id) for p in pairs if p.is_duplicate}

    predicted_positive = {(p.posting_a_id, p.posting_b_id) for p in predictions}

    true_positives = len(predicted_positive & labeled_positive)
    false_positives = len(predicted_positive - labeled_positive)
    false_negatives = len(labeled_positive - predicted_positive)

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives) > 0
        else 0.0
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if (true_positives + false_negatives) > 0
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return DedupMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        total_pairs=len(pairs),
    )


def evaluate_close_reopen(cases: list[CloseReopenCase]) -> dict[str, bool]:
    """Evaluate close/reopen logic: canonical is active if any posting is active."""
    results: dict[str, bool] = {}
    for case in cases:
        has_active = any(p.source_state == "active" for p in case.postings)
        computed_state = "active" if has_active else "closed"
        results[case.case_id] = computed_state == case.expected_canonical_state
    return results


# --- Frozen adapter fixtures (M1.12) ---

FROZEN_GREENHOUSE_RESPONSE: dict[str, object] = {
    "jobs": [
        {
            "id": 12345,
            "title": "Senior Software Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/techjobs/jobs/12345",
        },
        {
            "id": 12346,
            "title": "Product Manager",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/techjobs/jobs/12346",
        },
    ]
}

FROZEN_LEVER_RESPONSE: list[dict[str, object]] = [
    {
        "id": "lev-001",
        "text": "Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/company/lev-001",
        "categories": {"location": "Berlin, Germany"},
    },
    {
        "id": "lev-002",
        "text": "Frontend Engineer",
        "hostedUrl": "https://jobs.lever.co/company/lev-002",
        "categories": {"location": "Remote"},
    },
]

FROZEN_ASHBY_RESPONSE: dict[str, object] = {
    "jobs": [
        {
            "id": "ash-001",
            "title": "Data Scientist",
            "location": "London, UK",
            "url": "https://jobs.ashbyhq.com/company/ash-001",
        }
    ]
}

FROZEN_JSON_LD_HTML: str = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "DevOps Engineer",
  "datePosted": "2026-01-15",
  "hiringOrganization": {"name": "TestCorp"},
  "jobLocation": {"address": {"addressLocality": "Chengdu"}},
  "url": "https://testcorp.com/careers/devops"
}
</script>
</head><body></body></html>
"""

FROZEN_SITEMAP_XML: str = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://company.com/jobs/engineer</loc></url>
  <url><loc>https://company.com/jobs/designer</loc></url>
  <url><loc>https://company.com/about</loc></url>
</urlset>
"""

FROZEN_STATIC_HTML: str = """
<html><body>
<div class="job-listing">
  <h2 class="job-title">QA Engineer</h2>
  <span class="job-location">Tokyo, Japan</span>
  <a class="apply-link" href="https://company.com/apply/qa">Apply</a>
</div>
</body></html>
"""

# Cross-source dedup dataset: same job posted on multiple sources
FROZEN_DEDUP_DATASET: list[DedupPair] = [
    DedupPair(
        posting_a_id="gh-12345",
        posting_b_id="lev-001",
        is_duplicate=False,
        source_a="greenhouse",
        source_b="lever",
    ),
    DedupPair(
        posting_a_id="gh-12345",
        posting_b_id="gh-12345-alias",
        is_duplicate=True,
        source_a="greenhouse",
        source_b="json_ld",
    ),
    DedupPair(
        posting_a_id="lev-002",
        posting_b_id="ash-001",
        is_duplicate=False,
        source_a="lever",
        source_b="ashby",
    ),
]

# Close/reopen dataset
FROZEN_CLOSE_REOPEN_DATASET: list[CloseReopenCase] = [
    CloseReopenCase(
        case_id="one_closed_one_active",
        postings=(
            PostingState(posting_id="p1", source_state="closed"),
            PostingState(posting_id="p2", source_state="active"),
        ),
        expected_canonical_state="active",
    ),
    CloseReopenCase(
        case_id="all_closed",
        postings=(
            PostingState(posting_id="p1", source_state="closed"),
            PostingState(posting_id="p2", source_state="closed"),
        ),
        expected_canonical_state="closed",
    ),
    CloseReopenCase(
        case_id="single_active",
        postings=(PostingState(posting_id="p1", source_state="active"),),
        expected_canonical_state="active",
    ),
    CloseReopenCase(
        case_id="reopened_after_close",
        postings=(
            PostingState(posting_id="p1", source_state="active"),
            PostingState(posting_id="p2", source_state="closed"),
        ),
        expected_canonical_state="active",
    ),
]
