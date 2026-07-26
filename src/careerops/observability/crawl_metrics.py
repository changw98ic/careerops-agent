"""Crawl execution metrics (Section 5, task 5.9).

Prometheus metrics for crawl run observability. Follows the pattern in
:mod:`careerops.observability.metrics` with bounded, non-sensitive label
vocabulary.

Metrics:
- ``careerops_crawl_active_runs`` — Gauge: currently-running crawl executions.
- ``careerops_crawl_run_duration_seconds`` — Histogram: wall-clock duration
  per completed run, labelled by terminal state.
- ``careerops_crawl_source_failures_total`` — Counter: per-source failures
  grouped by safe error category (``BackoffReason`` value or policy decision).
- ``careerops_crawl_rate_limit_denials_total`` — Counter: rate-limit denials
  by domain.
- ``careerops_crawl_postings_total`` — Counter: postings created, updated, or
  closed, labelled by outcome (``created`` / ``updated`` / ``closed``).
- ``careerops_crawl_policy_denials_total`` — Counter: policy denials labelled
  by ``source_type`` and ``decision`` (the ``CrawlDecision`` enum value).

All labels are drawn from a closed vocabulary (enum values, bounded strings).
No raw URLs, PII, or error payloads appear in metric labels.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

__all__ = ["CrawlMetrics"]


class CrawlMetrics:
    """Crawl execution metrics with bounded label vocabulary.

    Instantiate once at app bootstrap and pass to the execution service /
    Temporal activities. Thread-safe (Prometheus client handles concurrency).
    """

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        reg = registry or CollectorRegistry(auto_describe=True)

        self._active_runs = Gauge(
            "careerops_crawl_active_runs",
            "Number of crawl executions currently in RUNNING state.",
            registry=reg,
        )
        self._run_duration = Histogram(
            "careerops_crawl_run_duration_seconds",
            "Wall-clock duration of completed crawl runs by terminal state.",
            ("state",),
            buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0),
            registry=reg,
        )
        self._source_failures = Counter(
            "careerops_crawl_source_failures_total",
            "Per-source crawl failures grouped by safe error category.",
            ("error_category",),
            registry=reg,
        )
        self._rate_limit_denials = Counter(
            "careerops_crawl_rate_limit_denials_total",
            "Rate-limit (429) denials by domain.",
            ("domain",),
            registry=reg,
        )
        self._postings = Counter(
            "careerops_crawl_postings_total",
            "Postings created, updated, or closed by crawl runs.",
            ("outcome",),
            registry=reg,
        )
        # Pre-initialize the three outcome labels so the metric is always
        # scrapeable even before the first run.
        for outcome in ("created", "updated", "closed"):
            self._postings.labels(outcome=outcome)

        self._policy_denials = Counter(
            "careerops_crawl_policy_denials_total",
            "Crawl policy denials by source type and decision.",
            ("source_type", "decision"),
            registry=reg,
        )

        self.registry = reg

    # -- Gauges (track in real-time) -----------------------------------------

    def run_started(self) -> None:
        """Increment active runs gauge (called when a run transitions to RUNNING)."""
        self._active_runs.inc()

    def run_ended(self) -> None:
        """Decrement active runs gauge (called when a run reaches terminal state)."""
        self._active_runs.dec()

    # -- Histograms (observe on completion) -----------------------------------

    def observe_run_duration(self, *, state: str, duration_seconds: float) -> None:
        """Record the wall-clock duration of a completed run.

        ``state`` is the terminal ``CrawlRunState`` value (``succeeded``,
        ``failed``, ``cancelled``, ``timeout``).
        """
        if duration_seconds >= 0:
            self._run_duration.labels(state=state).observe(duration_seconds)

    # -- Counters (accumulate during execution) ------------------------------

    def record_source_failure(self, *, error_category: str) -> None:
        """Increment the source failure counter for a given error category.

        ``error_category`` is a safe, non-sensitive label such as a
        ``BackoffReason`` value (``blocked_403``, ``rate_limited_429``,
        ``captcha_or_login_wall``, etc.) or a ``CrawlDecision`` value
        (``deny_blocked``, ``deny_rate_limited``, ``deny_ssrf``, etc.).
        """
        self._source_failures.labels(error_category=error_category).inc()

    def record_rate_limit_denial(self, *, domain: str) -> None:
        """Increment rate-limit denial counter for a domain."""
        self._rate_limit_denials.labels(domain=domain).inc()

    def record_posting_created(self, count: int = 1) -> None:
        """Increment postings-created counter."""
        if count > 0:
            self._postings.labels(outcome="created").inc(count)

    def record_posting_updated(self, count: int = 1) -> None:
        """Increment postings-updated counter."""
        if count > 0:
            self._postings.labels(outcome="updated").inc(count)

    def record_posting_closed(self, count: int = 1) -> None:
        """Increment postings-closed counter."""
        if count > 0:
            self._postings.labels(outcome="closed").inc(count)

    def record_policy_denial(self, *, source_type: str, decision: str) -> None:
        """Increment policy denial counter with bounded labels.

        ``source_type`` is a ``CrawlSourceType`` value (``greenhouse``,
        ``lever``, ``ashby``, ``official``). ``decision`` is a
        ``CrawlDecision`` value (``deny_blocked``, ``deny_rate_limited``,
        ``deny_terms_unknown``, ``deny_ssrf``).
        """
        self._policy_denials.labels(source_type=source_type, decision=decision).inc()

    # -- Convenience: observe from CrawlRunCounters --------------------------

    def observe_run_result(
        self,
        *,
        state: str,
        duration_seconds: float,
        discovered: int = 0,
        updated: int = 0,
        closed: int = 0,
    ) -> None:
        """Record all metrics from a completed run in one call.

        Combines duration observation and posting counter updates.
        """
        self.observe_run_duration(state=state, duration_seconds=duration_seconds)
        self.record_posting_created(discovered)
        self.record_posting_updated(updated)
        self.record_posting_closed(closed)
