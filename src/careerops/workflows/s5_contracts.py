"""Workflow contracts for Section 5 crawl execution (task 5.4).

Activity names, input/output dataclasses for the crawl-execution Temporal
wiring.  Follows the same pattern as ``m1_contracts.py`` and
``smoke_contracts.py``: frozen dataclasses, string constants for activity
names, and no runtime imports from infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Activity names
# ---------------------------------------------------------------------------

EXECUTE_CRAWL_RUN_ACTIVITY = "careerops.s5.execute_crawl_run"
CREATE_SCHEDULED_RUN_ACTIVITY = "careerops.s5.create_scheduled_run"


# ---------------------------------------------------------------------------
# Activity I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlRunExecuteInput:
    """Input for the execute_crawl_run activity.

    ``owner_id`` and ``run_id`` are UUID strings (Temporal JSON codec
    rejects bare ``UUID`` objects in some SDK versions).
    """

    owner_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class CrawlRunExecuteResult:
    """Output of the execute_crawl_run activity.

    Mirrors the terminal :class:`CrawlRun` fields the caller needs.
    ``state`` is the terminal state string (succeeded / failed / timeout).
    ``error_category`` is non-empty when the run failed.
    ``next_eligible_at`` is set when backoff was triggered.
    """

    run_id: str
    state: str
    counters: dict[str, int] = field(default_factory=dict[str, int])
    error_category: str = ""
    next_eligible_at: str | None = None


@dataclass(frozen=True, slots=True)
class CreateScheduledRunInput:
    """Input for the create_scheduled_run activity.

    The activity calls ``CrawlRunService.run_now`` to create (or reuse) a
    PENDING run for the active plan version.  If the plan is paused or has
    no eligible sources, ``run_created`` is False and ``error`` describes why.
    """

    owner_id: str


@dataclass(frozen=True, slots=True)
class CreateScheduledRunResult:
    """Output of the create_scheduled_run activity.

    ``run_created`` is True when a PENDING run exists (either freshly
    created or reused via idempotency).  ``run_id`` is the UUID string
    of the PENDING run; ``None`` when ``run_created`` is False.
    """

    run_created: bool
    run_id: str | None = None
    error: str = ""


# ---------------------------------------------------------------------------
# Workflow I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlRunWorkflowInput:
    """Input for the manual CrawlRunWorkflow.

    ``owner_id`` + ``run_id`` identify the PENDING run already created by
    the Section-4 ``CrawlRunService.run_now`` API endpoint.
    """

    owner_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ScheduledCrawlWorkflowInput:
    """Input for the scheduled CrawlScheduledWorkflow.

    ``owner_id`` is the server-resolved candidate.  The workflow creates
    a PENDING run (via the create_scheduled_run activity) and then
    executes it (via the execute_crawl_run activity).
    """

    owner_id: str


@dataclass(frozen=True, slots=True)
class CrawlRunWorkflowResult:
    """Result shared by both manual and scheduled workflows."""

    run_id: str
    state: str
    counters: dict[str, int] = field(default_factory=dict[str, int])
    error_category: str = ""
    next_eligible_at: str | None = None
    skipped: bool = False
    skip_reason: str = ""
