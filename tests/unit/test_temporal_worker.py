import pytest

from careerops.infrastructure.temporal.worker import TemporalWorkerSettings


def test_worker_settings_load_from_explicit_environment() -> None:
    settings = TemporalWorkerSettings.from_environment(
        {
            "CAREEROPS_TEMPORAL_ADDRESS": "temporal.internal:7233",
            "CAREEROPS_TEMPORAL_NAMESPACE": "careerops",
            "CAREEROPS_TEMPORAL_TASK_QUEUE": "careerops-test",
            "CAREEROPS_TEMPORAL_WORKER_IDENTITY": "worker-1",
        }
    )

    assert settings.target == "temporal.internal:7233"
    assert settings.namespace == "careerops"
    assert settings.task_queue == "careerops-test"
    assert settings.identity == "worker-1"


@pytest.mark.parametrize("field_name", ["target", "namespace", "task_queue"])
def test_worker_settings_reject_blank_routing_fields(field_name: str) -> None:
    values = {
        "target": "127.0.0.1:7233",
        "namespace": "default",
        "task_queue": "careerops-m0",
    }
    values[field_name] = "  "

    with pytest.raises(ValueError, match=f"{field_name} must not be blank"):
        TemporalWorkerSettings(**values)
