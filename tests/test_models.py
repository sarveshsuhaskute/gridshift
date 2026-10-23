from datetime import UTC, datetime

import pytest

from gridshift.models import JobSpec, ValidationError

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def request(**overrides) -> dict:
    body = {
        "name": "nightly-report",
        "command": ["python", "-c", "print('hi')"],
        "vcpu": 1,
        "memory_gb": 2,
        "est_runtime_minutes": 30,
        "deadline": "2026-10-25T06:00:00Z",
    }
    body.update(overrides)
    return body


def test_valid_request_round_trips_through_json():
    spec = JobSpec.from_request(request(allowed_regions=["eu-north-1", "eu-north-1"]), "us-east-1", NOW)
    assert spec.data_region == "us-east-1"
    assert spec.allowed_regions == ("eu-north-1",)
    assert JobSpec.from_json(spec.to_json()) == spec


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"name": ""}, "name"),
        ({"command": "echo hi"}, "command"),
        ({"vcpu": 3}, "vcpu"),
        ({"vcpu": 1, "memory_gb": 16}, "memory_gb"),
        ({"est_runtime_minutes": 0}, "est_runtime_minutes"),
        ({"deadline": "2026-10-24T12:10:00Z"}, "deadline"),
        ({"deadline": "2026-10-25T06:00:00"}, "timezone"),
        ({"max_cost_overhead_pct": -1}, "max_cost_overhead_pct"),
        ({"allowed_regions": []}, "allowed_regions"),
        ({"vcpu": "lots"}, "numeric"),
    ],
)
def test_invalid_requests_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        JobSpec.from_request(request(**overrides), "us-east-1", NOW)


def test_missing_runtime_is_reported():
    body = request()
    del body["est_runtime_minutes"]
    with pytest.raises(ValidationError, match="est_runtime_minutes is required"):
        JobSpec.from_request(body, "us-east-1", NOW)
