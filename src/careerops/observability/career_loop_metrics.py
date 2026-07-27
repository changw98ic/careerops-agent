"""Career-loop business metrics (Section 15, task 15.7).

Prometheus metrics for the end-to-end career application loop, complementing
the crawl metrics in :mod:`careerops.observability.crawl_metrics` and the
operational metrics in :mod:`careerops.observability.metrics`.

All label vocabularies are CLOSED: every label is drawn from a StrEnum
(documented below) or a bounded cardinality (a small fixed set of values). No
raw URLs, email addresses, free-form error payloads, PII, or user content
appear in metric labels — only safe, structural identifiers. This keeps the
metric cardinality bounded and prevents leakage of sensitive data through the
``/metrics`` scrape (ADR 0006; Iron Rule 2 / Iron Rule 7).

Metric groups:

- ``careerops_inbox_decisions_total`` — Counter: inbox favorite/ignore/snooze
  decisions, labelled by ``decision`` (a ``FilterVerdict``-aligned value).
- ``careerops_package_approvals_total`` — Counter: package approval outcomes
  labelled by ``outcome`` (``approved`` / ``rejected``).
- ``careerops_send_intents_total`` — Counter: system-managed send confirmations
  labelled by ``phase`` (a ``SystemSendPhase`` value).
- ``careerops_provider_receipts_total`` — Counter: confirmed provider receipts
  labelled by ``final_state`` (a ``ReceiptFinalState`` value).
- ``careerops_reconciliation_total`` — Counter: send reconciliation outcomes
  labelled by ``status`` (a ``ReconciliationStatus`` value).
- ``careerops_mail_proposals_total`` — Counter: mail-intelligence proposal
  review outcomes labelled by ``decision`` (``accepted`` / ``rejected``) and
  ``source`` (``rules`` / ``model``).
- ``careerops_review_latency_seconds`` — Histogram: human review latency for
  mail proposals / reply drafts / package approvals, labelled by ``review_kind``.
- ``careerops_follow_up_total`` — Counter: follow-up lifecycle events labelled
  by ``event`` (``created`` / ``snoozed`` / ``completed`` / ``cancelled``).

Gauges:

- ``careerops_pending_approvals`` — Gauge: currently-pending human approvals
  (packages / replies) by ``kind``.
- ``careerops_reconciliation_backlog`` — Gauge: send intents awaiting
  reconciliation.

The bounded label values are pre-seeded so every metric is scrapeable on the
first ``/metrics`` call before any business event fires.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

__all__ = ["CareerLoopMetrics"]


# Closed vocabularies. Mirroring enum values keeps labels stable even when a
# domain enum is later renamed — the metric name/label text is the contract.
_INBOX_DECISIONS = ("favorite", "ignore", "snooze", "restore")
_PACKAGE_OUTCOMES = ("approved", "rejected")
_SEND_PHASES = (
    "pending",
    "sent",
    "failed",
    "reconciliation_required",
)
_RECEIPT_FINAL_STATES = (
    "confirmed_sent",
    "confirmed_not_sent",
    "ambiguous",
    "escalated_manual",
)
_RECONCILIATION_STATUSES = (
    "pending",
    "confirmed_sent",
    "confirmed_not_sent",
    "ambiguous",
    "escalated_manual",
)
_MAIL_PROPOSAL_DECISIONS = ("accepted", "rejected")
_MAIL_PROPOSAL_SOURCES = ("rules", "model")
_REVIEW_KINDS = ("mail_proposal", "reply_draft", "package_approval")
_FOLLOW_UP_EVENTS = ("created", "snoozed", "completed", "cancelled")
_PENDING_KINDS = ("package", "reply")


class CareerLoopMetrics:
    """Business metrics for the career application loop (task 15.7).

    Instantiate once at app bootstrap and pass into the relevant services /
    Temporal activities. Thread-safe (Prometheus client handles concurrency).
    No method accepts a free-form string label — every label is taken from a
    closed vocabulary so metric cardinality stays bounded.
    """

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        reg = registry or CollectorRegistry(auto_describe=True)
        self.registry = reg

        self._inbox_decisions = Counter(
            "careerops_inbox_decisions_total",
            "Inbox decisions (favorite/ignore/snooze/restore) by outcome.",
            ("decision",),
            registry=reg,
        )
        for decision in _INBOX_DECISIONS:
            self._inbox_decisions.labels(decision=decision)

        self._package_approvals = Counter(
            "careerops_package_approvals_total",
            "Application package approval attempts by outcome.",
            ("outcome",),
            registry=reg,
        )
        for outcome in _PACKAGE_OUTCOMES:
            self._package_approvals.labels(outcome=outcome)

        self._send_intents = Counter(
            "careerops_send_intents_total",
            "System-managed send confirmations by phase.",
            ("phase",),
            registry=reg,
        )
        for phase in _SEND_PHASES:
            self._send_intents.labels(phase=phase)

        self._provider_receipts = Counter(
            "careerops_provider_receipts_total",
            "Provider receipts by final reconciliation state.",
            ("final_state",),
            registry=reg,
        )
        for state in _RECEIPT_FINAL_STATES:
            self._provider_receipts.labels(final_state=state)

        self._reconciliation = Counter(
            "careerops_reconciliation_total",
            "Send reconciliation outcomes by status.",
            ("status",),
            registry=reg,
        )
        for status in _RECONCILIATION_STATUSES:
            self._reconciliation.labels(status=status)

        self._mail_proposals = Counter(
            "careerops_mail_proposals_total",
            "Mail-intelligence proposal review outcomes by decision and source.",
            ("decision", "source"),
            registry=reg,
        )
        for decision in _MAIL_PROPOSAL_DECISIONS:
            for source in _MAIL_PROPOSAL_SOURCES:
                self._mail_proposals.labels(decision=decision, source=source)

        self._review_latency = Histogram(
            "careerops_review_latency_seconds",
            "Human review latency for proposals/drafts/approvals by kind.",
            ("review_kind",),
            buckets=(1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0, 21600.0, 86400.0),
            registry=reg,
        )
        for kind in _REVIEW_KINDS:
            self._review_latency.labels(review_kind=kind)

        self._follow_ups = Counter(
            "careerops_follow_up_total",
            "Follow-up lifecycle events by event.",
            ("event",),
            registry=reg,
        )
        for event in _FOLLOW_UP_EVENTS:
            self._follow_ups.labels(event=event)

        self._pending_approvals = Gauge(
            "careerops_pending_approvals",
            "Currently-pending human approvals by kind.",
            ("kind",),
            registry=reg,
        )
        for kind in _PENDING_KINDS:
            self._pending_approvals.labels(kind=kind)

        self._reconciliation_backlog = Gauge(
            "careerops_reconciliation_backlog",
            "Send intents currently awaiting reconciliation.",
            registry=reg,
        )

    # -- Inbox -------------------------------------------------------------

    def record_inbox_decision(self, *, decision: str) -> None:
        """Increment the inbox decision counter.

        ``decision`` MUST be one of: ``favorite``, ``ignore``, ``snooze``,
        ``restore`` (the closed inbox-action vocabulary).
        """
        if decision in _INBOX_DECISIONS:
            self._inbox_decisions.labels(decision=decision).inc()

    # -- Packages ----------------------------------------------------------

    def record_package_approval(self, *, outcome: str) -> None:
        """Increment the package approval counter.

        ``outcome`` MUST be ``approved`` or ``rejected``.
        """
        if outcome in _PACKAGE_OUTCOMES:
            self._package_approvals.labels(outcome=outcome).inc()

    # -- System-managed send ----------------------------------------------

    def record_send_intent(self, *, phase: str) -> None:
        """Increment the send-intent counter by ``SystemSendPhase`` value."""
        if phase in _SEND_PHASES:
            self._send_intents.labels(phase=phase).inc()

    def record_provider_receipt(self, *, final_state: str) -> None:
        """Increment the provider receipt counter by final reconciliation state."""
        if final_state in _RECEIPT_FINAL_STATES:
            self._provider_receipts.labels(final_state=final_state).inc()

    def record_reconciliation(self, *, status: str) -> None:
        """Increment the reconciliation outcome counter by status."""
        if status in _RECONCILIATION_STATUSES:
            self._reconciliation.labels(status=status).inc()

    # -- Mail intelligence -------------------------------------------------

    def record_mail_proposal(self, *, decision: str, source: str) -> None:
        """Increment the mail proposal review counter.

        ``decision`` is ``accepted`` / ``rejected``; ``source`` is
        ``rules`` / ``model`` (the ``MailExtractionSource`` vocabulary).
        """
        if decision in _MAIL_PROPOSAL_DECISIONS and source in _MAIL_PROPOSAL_SOURCES:
            self._mail_proposals.labels(decision=decision, source=source).inc()

    def observe_review_latency(self, *, review_kind: str, seconds: float) -> None:
        """Observe human review latency in seconds for the given review kind.

        ``review_kind`` is ``mail_proposal`` / ``reply_draft`` /
        ``package_approval``. Non-positive durations are dropped (no spurious
        series from a missing timestamp).
        """
        if review_kind in _REVIEW_KINDS and seconds >= 0:
            self._review_latency.labels(review_kind=review_kind).observe(seconds)

    # -- Follow-ups --------------------------------------------------------

    def record_follow_up(self, *, event: str) -> None:
        """Increment the follow-up lifecycle counter by event."""
        if event in _FOLLOW_UP_EVENTS:
            self._follow_ups.labels(event=event).inc()

    # -- Gauges ------------------------------------------------------------

    def set_pending_approvals(self, *, kind: str, value: float) -> None:
        """Set the pending-approvals gauge for the given kind."""
        if kind in _PENDING_KINDS:
            self._pending_approvals.labels(kind=kind).set(value)

    def inc_pending_approvals(self, *, kind: str) -> None:
        if kind in _PENDING_KINDS:
            self._pending_approvals.labels(kind=kind).inc()

    def dec_pending_approvals(self, *, kind: str) -> None:
        if kind in _PENDING_KINDS:
            self._pending_approvals.labels(kind=kind).dec()

    def set_reconciliation_backlog(self, value: float) -> None:
        self._reconciliation_backlog.set(value)

    def inc_reconciliation_backlog(self) -> None:
        self._reconciliation_backlog.inc()

    def dec_reconciliation_backlog(self) -> None:
        self._reconciliation_backlog.dec()
