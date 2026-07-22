from __future__ import annotations

from typing import cast

import httpx2
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST

from careerops.api.app import create_app
from careerops.config import RuntimeEnvironment, Settings
from careerops.observability.metrics import (
    Metrics,
    ReleaseCapability,
    ReleaseGateOutcome,
    ReleaseMode,
    RolloutObservationResult,
)


def make_client() -> httpx2.Client:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "database_url": "postgresql+psycopg://careerops:do-not-export@localhost/careerops",
        }
    )
    return cast("httpx2.Client", TestClient(create_app(settings)))


def test_metrics_contract_is_uncached_and_not_in_openapi() -> None:
    client = make_client()
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "/metrics" not in client.get("/api/v1/openapi.json").json()["paths"]


def test_metrics_expose_build_and_disabled_capabilities_without_default_collectors() -> None:
    body = make_client().get("/metrics").text

    assert 'careerops_build_info{version="0.1.0"} 1.0' in body
    for capability in ("model_provider", "google_oauth", "external_writes", "auto_send"):
        assert f'careerops_capability_enabled{{capability="{capability}"}} 0.0' in body

    assert "careerops_operations_total" in body
    assert "python_gc_" not in body
    assert "process_" not in body
    assert "python_info" not in body


def test_http_metrics_use_route_templates_and_do_not_leak_request_data() -> None:
    client = make_client()
    raw_unknown_path = "/api/v1/private/super-secret-token"
    request_id = "request-id-must-not-be-exported"

    assert (
        client.get(
            "/api/v1/health/live",
            headers={"X-Request-ID": request_id},
        ).status_code
        == 200
    )
    assert client.get(raw_unknown_path).status_code == 404
    body = client.get("/metrics").text

    assert (
        'careerops_http_requests_total{method="GET",route="/api/v1/health/live",'
        'status_class="2xx"} 1.0'
    ) in body
    assert (
        'careerops_http_requests_total{method="GET",route="unmatched",status_class="4xx"} 1.0'
    ) in body
    assert raw_unknown_path not in body
    assert "super-secret-token" not in body
    assert request_id not in body
    assert "do-not-export" not in body


def test_release_rollout_metrics_use_only_closed_safe_labels() -> None:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "database_url": "postgresql+psycopg://careerops:do-not-export@localhost/careerops",
        }
    )
    metrics = Metrics(version="test-version", settings=settings)

    metrics.record_release_gate_decision(
        capability=ReleaseCapability.GMAIL_SEND,
        mode=ReleaseMode.REVIEW_REQUIRED,
        outcome=ReleaseGateOutcome.REVIEW_REQUIRED,
    )
    metrics.record_release_gate_decision(
        capability=ReleaseCapability.GREENHOUSE_SUBMIT,
        mode=ReleaseMode.LIMITED_AUTOPILOT,
        outcome=ReleaseGateOutcome.BLOCKED,
    )
    metrics.record_rollout_observation(
        capability=ReleaseCapability.GMAIL_SEND,
        mode=ReleaseMode.REVIEW_REQUIRED,
        result=RolloutObservationResult.HUMAN_APPROVED,
        count=3,
    )
    metrics.record_rollout_observation(
        capability=ReleaseCapability.BROWSER_SUBMIT,
        mode=ReleaseMode.SHADOW,
        result=RolloutObservationResult.FAIL_CLOSED,
    )
    metrics.set_release_qualified(
        capability=ReleaseCapability.GMAIL_SEND,
        mode=ReleaseMode.REVIEW_REQUIRED,
        qualified=True,
    )
    body = metrics.render().decode()

    assert (
        'careerops_release_gate_decisions_total{capability="gmail_send",'
        'mode="review_required",outcome="review_required"} 1.0'
    ) in body
    assert (
        'careerops_release_gate_decisions_total{capability="greenhouse_submit",'
        'mode="limited_autopilot",outcome="blocked"} 1.0'
    ) in body
    assert (
        'careerops_rollout_observations_total{capability="gmail_send",'
        'mode="review_required",result="human_approved"} 3.0'
    ) in body
    assert (
        'careerops_rollout_observations_total{capability="browser_submit",'
        'mode="shadow",result="fail_closed"} 1.0'
    ) in body
    assert 'careerops_release_qualified{capability="gmail_send",mode="review_required"} 1.0' in body
    assert (
        'careerops_release_qualified{capability="synthetic_sandbox",mode="synthetic_sandbox"} 0.0'
    ) in body
    assert (
        'careerops_release_qualified{capability="greenhouse_submit",mode="expanded_autopilot"} 0.0'
    ) in body
    assert 'mode="limited"' not in body
    assert 'mode="expanded"' not in body

    for leaked in (
        "applicant@example.com",
        "greenhouse.io",
        "job-123",
        "approval-token",
        "human free-form reason",
        "do-not-export",
    ):
        assert leaked not in body


def test_rollout_observation_count_must_be_positive() -> None:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "database_url": "postgresql+psycopg://careerops:do-not-export@localhost/careerops",
        }
    )
    metrics = Metrics(version="test-version", settings=settings)

    try:
        metrics.record_rollout_observation(
            capability=ReleaseCapability.GMAIL_SEND,
            mode=ReleaseMode.REVIEW_REQUIRED,
            result=RolloutObservationResult.HUMAN_APPROVED,
            count=0,
        )
    except ValueError as exc:
        assert str(exc) == "rollout observation count must be positive"
    else:  # pragma: no cover - explicit assertion readability
        raise AssertionError("expected ValueError for zero rollout observation count")
