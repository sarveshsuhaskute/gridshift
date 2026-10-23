"""Decision engine: choose the (region, start time) with the lowest expected emissions.

Every candidate slot is scored for carbon and cost. A slot is rejected if it breaks the
deadline, residency or cost budget. The baseline is "run now in the data's home region",
which is what a carbon-unaware scheduler would do.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from gridshift import energy
from gridshift.carbon import ForecastPoint
from gridshift.models import JobSpec, format_time
from gridshift.regions import RegionProfile

DEFAULT_STEP_MINUTES = 30
# Extra time reserved before the deadline so a Spot interruption can still be retried.
DEADLINE_SAFETY_FACTOR = 0.2
FULL_COVERAGE = 0.999


class PlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    region: str
    start: datetime
    intensity: float
    carbon_g: float
    cost_usd: float
    forecast_coverage: float

    @property
    def data_source(self) -> str:
        if self.forecast_coverage >= FULL_COVERAGE:
            return "forecast"
        return "partial-forecast" if self.forecast_coverage > 0 else "static"


@dataclass(frozen=True)
class Decision:
    job_id: str
    region: str
    start_at: str
    fallback_region: str
    intensity: float
    est_carbon_g: float
    est_cost_usd: float
    data_source: str
    baseline_region: str
    baseline_intensity: float
    baseline_carbon_g: float
    baseline_cost_usd: float
    carbon_saved_pct: float
    cost_overhead_pct: float
    candidates_evaluated: int
    rejected_over_budget: int
    timeout_seconds: int
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def average_intensity(
    series: Sequence[ForecastPoint], start: datetime, end: datetime, fallback: float
) -> tuple[float, float]:
    """Time-weighted mean intensity over [start, end) and the fraction covered by forecast data.

    Each point holds until the next point begins; the last point holds for one hour.
    Uncovered time uses ``fallback``.
    """
    window = (end - start).total_seconds()
    if window <= 0:
        raise ValueError("window must have positive length")
    weighted = 0.0
    covered = 0.0
    for i, point in enumerate(series):
        point_end = series[i + 1].start if i + 1 < len(series) else point.start + timedelta(hours=1)
        lo, hi = max(start, point.start), min(end, point_end)
        if hi > lo:
            seconds = (hi - lo).total_seconds()
            weighted += point.intensity * seconds
            covered += seconds
    covered = min(covered, window)
    weighted += fallback * (window - covered)
    return weighted / window, covered / window


def _candidate_starts(now: datetime, latest: datetime, step_minutes: int) -> list[datetime]:
    starts = [now]
    step = timedelta(minutes=step_minutes)
    epoch_minutes = now.timestamp() / 60
    aligned = datetime.fromtimestamp(math.ceil(epoch_minutes / step_minutes) * step_minutes * 60, tz=now.tzinfo)
    t = aligned if aligned > now else aligned + step
    while t <= latest:
        starts.append(t)
        t += step
    return starts


def _score(
    job: JobSpec,
    region: RegionProfile,
    data_region: RegionProfile,
    start: datetime,
    series: Sequence[ForecastPoint],
) -> Candidate:
    hours = job.est_runtime_hours
    end = start + timedelta(hours=hours)
    intensity, coverage = average_intensity(series, start, end, region.static_intensity)

    compute_kwh = energy.compute_kwh(job.vcpu, job.memory_gb, hours)
    carbon = energy.grams_co2(compute_kwh, intensity)
    cost = hours * (job.vcpu * region.spot_vcpu_hour + job.memory_gb * region.spot_gb_hour)

    if region.region != data_region.region and job.input_data_gb > 0:
        # Network energy is split between both ends, so use the mean of their static averages.
        network_intensity = (region.static_intensity + data_region.static_intensity) / 2
        carbon += energy.grams_co2(energy.transfer_kwh(job.input_data_gb), network_intensity)
        cost += job.input_data_gb * data_region.transfer_cost_per_gb(region.region)

    return Candidate(region.region, start, intensity, carbon, cost, coverage)


def plan(
    job: JobSpec,
    now: datetime,
    profiles: Mapping[str, RegionProfile],
    forecasts: Mapping[str, Sequence[ForecastPoint]],
    step_minutes: int = DEFAULT_STEP_MINUTES,
) -> Decision:
    if job.data_region not in profiles:
        raise PlanningError(f"data_region {job.data_region} is not a configured worker region")

    allowed = [r for r in (job.allowed_regions or profiles.keys()) if r in profiles]
    if not allowed:
        raise PlanningError("none of the allowed_regions are configured worker regions")

    # average_intensity needs each series in time order; callers may pass them in any order.
    series_by_region = {region: sorted(forecasts.get(region, ())) for region in profiles}
    data_profile = profiles[job.data_region]
    baseline = _score(job, data_profile, data_profile, now, series_by_region[job.data_region])

    runtime = timedelta(minutes=job.est_runtime_minutes)
    latest_start = job.deadline - runtime * (1 + DEADLINE_SAFETY_FACTOR)
    starts = _candidate_starts(now, latest_start, step_minutes) if latest_start >= now else [now]

    evaluated = 0
    over_budget = 0
    best: Candidate | None = None
    for region in allowed:
        series = series_by_region[region]
        for start in starts:
            candidate = _score(job, profiles[region], data_profile, start, series)
            evaluated += 1
            if _overhead_pct(candidate.cost_usd, baseline.cost_usd) > job.max_cost_overhead_pct + 1e-9:
                over_budget += 1
                continue
            if best is None or (candidate.carbon_g, candidate.start, candidate.cost_usd) < (
                best.carbon_g,
                best.start,
                best.cost_usd,
            ):
                best = candidate

    if best is None:
        # Every slot broke the cost budget. Run in the cheapest allowed region now rather than fail.
        best = min(
            (_score(job, profiles[r], data_profile, now, series_by_region[r]) for r in allowed),
            key=lambda c: c.cost_usd,
        )

    fallback = _fallback_region(best.region, allowed, job.data_region, profiles)
    saved_pct = 100.0 * (baseline.carbon_g - best.carbon_g) / baseline.carbon_g if baseline.carbon_g else 0.0
    overhead_pct = _overhead_pct(best.cost_usd, baseline.cost_usd)
    delay = best.start - now

    explanation = (
        f"Run in {best.region} "
        + (f"in {_humanize(delay)} " if delay.total_seconds() >= 60 else "now ")
        + f"at ~{best.intensity:.0f} gCO2e/kWh ({best.data_source}): "
        f"{saved_pct:.0f}% less CO2e than running now in {job.data_region}, "
        f"cost {overhead_pct:+.1f}%."
    )

    return Decision(
        job_id=job.job_id,
        region=best.region,
        start_at=format_time(best.start),
        fallback_region=fallback,
        intensity=round(best.intensity, 2),
        est_carbon_g=round(best.carbon_g, 3),
        est_cost_usd=round(best.cost_usd, 6),
        data_source=best.data_source,
        baseline_region=job.data_region,
        baseline_intensity=round(baseline.intensity, 2),
        baseline_carbon_g=round(baseline.carbon_g, 3),
        baseline_cost_usd=round(baseline.cost_usd, 6),
        carbon_saved_pct=round(saved_pct, 2),
        cost_overhead_pct=round(overhead_pct, 2),
        candidates_evaluated=evaluated,
        rejected_over_budget=over_budget,
        # Covers Fargate provisioning, the run itself, and slack for slow image pulls.
        timeout_seconds=int(job.est_runtime_minutes * 60 * 2 + 600),
        explanation=explanation,
    )


def _overhead_pct(cost: float, baseline: float) -> float:
    return 100.0 * (cost - baseline) / baseline if baseline else 0.0


def _fallback_region(
    chosen: str, allowed: Sequence[str], data_region: str, profiles: Mapping[str, RegionProfile]
) -> str:
    """Where to retry on on-demand capacity if the chosen region fails."""
    if chosen != data_region and data_region in allowed:
        return data_region
    others = [r for r in allowed if r != chosen]
    if not others:
        return chosen
    return min(others, key=lambda r: profiles[r].static_intensity)


def _humanize(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"
