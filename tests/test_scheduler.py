from datetime import UTC, datetime, timedelta

import pytest

from gridshift.carbon import ForecastPoint
from gridshift.models import JobSpec
from gridshift.regions import PROFILES
from gridshift.scheduler import PlanningError, average_intensity, plan

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
WORKERS = {r: PROFILES[r] for r in ("us-east-1", "eu-north-1", "ca-central-1")}


def make_job(**overrides) -> JobSpec:
    fields = dict(
        job_id="job-1",
        name="render",
        command=("echo", "hi"),
        vcpu=1.0,
        memory_gb=2.0,
        est_runtime_minutes=60,
        deadline=NOW + timedelta(hours=12),
        data_region="us-east-1",
        input_data_gb=0.0,
        allowed_regions=None,
        max_cost_overhead_pct=25.0,
    )
    fields.update(overrides)
    return JobSpec(**fields)


def flat(intensity: float, hours: int = 24) -> list[ForecastPoint]:
    return [ForecastPoint(NOW + timedelta(hours=h), intensity) for h in range(hours)]


def test_average_intensity_weights_by_time_and_fills_gaps():
    series = [ForecastPoint(NOW, 100.0), ForecastPoint(NOW + timedelta(minutes=30), 300.0)]
    avg, coverage = average_intensity(series, NOW, NOW + timedelta(hours=1), fallback=999)
    assert avg == pytest.approx(200.0)
    assert coverage == pytest.approx(1.0)

    avg, coverage = average_intensity(series, NOW, NOW + timedelta(hours=2), fallback=500)
    # 30m@100 + 60m@300 (last point holds 1h) + 30m@fallback 500
    assert avg == pytest.approx((30 * 100 + 60 * 300 + 30 * 500) / 120)
    assert coverage == pytest.approx(0.75)


def test_moves_to_cleanest_region():
    forecasts = {"us-east-1": flat(400), "eu-north-1": flat(20), "ca-central-1": flat(40)}
    decision = plan(make_job(), NOW, WORKERS, forecasts)
    assert decision.region == "eu-north-1"
    assert decision.data_source == "forecast"
    assert decision.carbon_saved_pct == pytest.approx(95.0)
    assert decision.fallback_region == "us-east-1"


def test_shifts_in_time_within_a_region():
    # Home region gets much cleaner at 18:00 (e.g. overnight wind); only home is allowed.
    series = [ForecastPoint(NOW + timedelta(hours=h), 500.0 if h < 6 else 100.0) for h in range(24)]
    job = make_job(allowed_regions=("us-east-1",))
    decision = plan(job, NOW, WORKERS, {"us-east-1": series})
    assert decision.region == "us-east-1"
    assert decision.start_at == "2026-10-24T18:00:00Z"
    assert decision.carbon_saved_pct == pytest.approx(80.0)


def test_respects_deadline_with_safety_margin():
    series = [ForecastPoint(NOW + timedelta(hours=h), 500.0 if h < 6 else 100.0) for h in range(24)]
    # 3h deadline, 1h job: latest start = 3h - 1.2h = 1h48m, so the 18:00 dip is unreachable.
    job = make_job(allowed_regions=("us-east-1",), deadline=NOW + timedelta(hours=3))
    decision = plan(job, NOW, WORKERS, {"us-east-1": series})
    start = datetime.fromisoformat(decision.start_at)
    assert start + timedelta(minutes=72) <= job.deadline


def test_data_residency_is_never_violated():
    forecasts = {"us-east-1": flat(400), "eu-north-1": flat(20), "ca-central-1": flat(40)}
    job = make_job(allowed_regions=("us-east-1", "ca-central-1"))
    decision = plan(job, NOW, WORKERS, forecasts)
    assert decision.region == "ca-central-1"
    assert decision.fallback_region == "us-east-1"


def test_cost_budget_rejects_expensive_regions():
    forecasts = {"us-east-1": flat(400), "eu-north-1": flat(20), "ca-central-1": flat(40)}
    job = make_job(max_cost_overhead_pct=0.0)
    decision = plan(job, NOW, WORKERS, forecasts)
    assert decision.region == "us-east-1"
    assert decision.rejected_over_budget > 0


def test_data_gravity_keeps_heavy_jobs_home():
    # 500 GB input costs $10 to move, far more than the ~$0.015 compute saving allows.
    forecasts = {"us-east-1": flat(400), "eu-north-1": flat(20), "ca-central-1": flat(40)}
    job = make_job(input_data_gb=500.0)
    decision = plan(job, NOW, WORKERS, forecasts)
    assert decision.region == "us-east-1"


def test_missing_forecasts_fall_back_to_static_averages():
    decision = plan(make_job(), NOW, WORKERS, forecasts={})
    assert decision.region == "eu-north-1"
    assert decision.data_source == "static"


def test_unknown_data_region_is_rejected():
    with pytest.raises(PlanningError):
        plan(make_job(data_region="mars-east-1"), NOW, WORKERS, {})


def test_decision_is_serialisable():
    decision = plan(make_job(), NOW, WORKERS, {})
    data = decision.to_dict()
    assert data["job_id"] == "job-1"
    assert data["timeout_seconds"] == 60 * 60 * 2 + 600
    assert "less CO2e" in data["explanation"]
