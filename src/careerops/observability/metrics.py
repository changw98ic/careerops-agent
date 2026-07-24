from __future__ import annotations

from enum import StrEnum
from time import perf_counter

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from careerops.config import Settings

_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_STATUS_CLASSES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"})


class OperationalComponent(StrEnum):
    CRAWL = "crawl"
    MATCHING = "matching"
    EMAIL = "email"
    CALENDAR = "calendar"
    STORAGE = "storage"
    OUTBOX = "outbox"


class OperationalOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    RETRY = "retry"


class Metrics:
    """Application-scoped metrics with a fixed, non-sensitive label vocabulary."""

    def __init__(self, *, version: str, settings: Settings) -> None:
        self.registry = CollectorRegistry(auto_describe=True)

        build_info = Gauge(
            "careerops_build_info",
            "Static build identity for the running CareerOps API.",
            ("version",),
            registry=self.registry,
        )
        build_info.labels(version=version).set(1)

        capability_enabled = Gauge(
            "careerops_capability_enabled",
            "Whether a release-gated capability is enabled (1) or disabled (0).",
            ("capability",),
            registry=self.registry,
        )
        capability_values = {
            "model_provider": settings.model_provider != "disabled",
            "google_oauth": settings.google_oauth_enabled,
            "external_writes": settings.external_writes_enabled,
            "auto_send": settings.auto_send_enabled,
        }
        for capability, enabled in capability_values.items():
            capability_enabled.labels(capability=capability).set(int(enabled))

        self._http_requests = Counter(
            "careerops_http_requests_total",
            "HTTP responses grouped only by bounded request attributes.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self._http_duration = Histogram(
            "careerops_http_request_duration_seconds",
            "HTTP request duration grouped only by method and route template.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=self.registry,
        )
        self._operations = Counter(
            "careerops_operations_total",
            "Operational outcomes using a closed component and outcome vocabulary.",
            ("component", "outcome"),
            registry=self.registry,
        )
        for component in OperationalComponent:
            for outcome in OperationalOutcome:
                self._operations.labels(component=component.value, outcome=outcome.value)

        # ADR 0006: aggregate token COUNTS only. Raw prompt/response content is
        # never recorded. ``component`` is bounded to the provider surface;
        # ``token_kind`` is bounded to input/output.
        self._llm_tokens = Counter(
            "careerops_llm_tokens_total",
            "Aggregate LLM token usage (ADR 0006: count only, never content).",
            ("component", "token_kind"),
            registry=self.registry,
        )
        for token_kind in ("input", "output"):
            self._llm_tokens.labels(component="provider", token_kind=token_kind)

        # Business instrumentation (plan v0.4 §3 Stage 3.5). v1 only accumulates
        # counts; conversion analysis lands once apply/interview data flows in.
        self._apply_submitted = Counter(
            "careerops_apply_submitted_total",
            "Applications submitted through the apply pipeline.",
            registry=self.registry,
        )
        self._interview_received = Counter(
            "careerops_interview_received_total",
            "Interview invitations received in response to applications.",
            registry=self.registry,
        )

    def observe_http(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        safe_method = method if method in _HTTP_METHODS else "OTHER"
        status_class = _status_class(status_code)
        self._http_requests.labels(
            method=safe_method,
            route=route,
            status_class=status_class,
        ).inc()
        self._http_duration.labels(method=safe_method, route=route).observe(duration_seconds)

    def record_operation(
        self,
        *,
        component: OperationalComponent,
        outcome: OperationalOutcome,
    ) -> None:
        self._operations.labels(component=component.value, outcome=outcome.value).inc()

    def record_llm_tokens(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        component: str = "provider",
    ) -> None:
        """Record aggregate LLM token usage (ADR 0006: counts only, never content).

        No-ops on non-positive counts so a missing/zero usage block cannot create
        spurious label series.
        """
        if input_tokens > 0:
            self._llm_tokens.labels(component=component, token_kind="input").inc(input_tokens)  # nosec B106
        if output_tokens > 0:
            self._llm_tokens.labels(component=component, token_kind="output").inc(output_tokens)  # nosec B106

    def record_apply_submitted(self, amount: int = 1) -> None:
        """Increment the apply-submitted counter (v1 instrumentation pipeline)."""
        if amount > 0:
            self._apply_submitted.inc(amount)

    def record_interview_received(self, amount: int = 1) -> None:
        """Increment the interview-received counter (v1 instrumentation pipeline)."""
        if amount > 0:
            self._interview_received.inc(amount)

    def render(self) -> bytes:
        return generate_latest(self.registry)


def request_started_at() -> float:
    return perf_counter()


def elapsed_since(started_at: float) -> float:
    return max(0.0, perf_counter() - started_at)


def _status_class(status_code: int) -> str:
    candidate = f"{status_code // 100}xx" if status_code >= 0 else "other"
    return candidate if candidate in _STATUS_CLASSES else "other"
