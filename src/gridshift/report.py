"""Aggregate finished jobs into a results report (used by scripts/benchmark.py)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from gridshift.models import parse_time


@dataclass
class Summary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    carbon_g: float = 0.0
    baseline_carbon_g: float = 0.0
    est_cost_usd: float = 0.0
    baseline_cost_usd: float = 0.0
    delay_minutes: list[float] = field(default_factory=list)
    regions: Counter = field(default_factory=Counter)
    data_sources: Counter = field(default_factory=Counter)
    fallbacks: int = 0

    @property
    def carbon_saved_pct(self) -> float:
        if not self.baseline_carbon_g:
            return 0.0
        return 100.0 * (self.baseline_carbon_g - self.carbon_g) / self.baseline_carbon_g

    @property
    def cost_overhead_pct(self) -> float:
        if not self.baseline_cost_usd:
            return 0.0
        return 100.0 * (self.est_cost_usd - self.baseline_cost_usd) / self.baseline_cost_usd

    @property
    def median_delay_minutes(self) -> float:
        values = sorted(self.delay_minutes)
        if not values:
            return 0.0
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def summarize(jobs: list[dict]) -> Summary:
    """Jobs are GET /jobs/{id} responses."""
    summary = Summary(total=len(jobs))
    for job in jobs:
        status = job.get("status")
        if status == "FAILED":
            summary.failed += 1
            continue
        if status != "SUCCEEDED":
            summary.pending += 1
            continue
        summary.succeeded += 1
        decision, result = job["decision"], job["result"]
        summary.carbon_g += result["carbon_g"]
        summary.baseline_carbon_g += result["baseline_carbon_g"]
        summary.est_cost_usd += decision["est_cost_usd"]
        summary.baseline_cost_usd += decision["baseline_cost_usd"]
        summary.regions[result["region"]] += 1
        summary.data_sources[decision["data_source"]] += 1
        if result["region"] != decision["region"]:
            summary.fallbacks += 1
        delay = parse_time(decision["start_at"]) - parse_time(job["submitted_at"])
        summary.delay_minutes.append(max(delay.total_seconds(), 0) / 60)
    return summary


def to_markdown(summary: Summary, generated_at: str, home_region: str) -> str:
    lines = [
        "# GridShift: measured results",
        "",
        f"Generated {generated_at} by `scripts/benchmark.py` from jobs that ran on AWS.",
        f"The baseline for every job is running it immediately in its home region ({home_region}).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Jobs | {summary.total} ({summary.succeeded} succeeded, {summary.failed} failed, "
        f"{summary.pending} pending) |",
        f"| CO2e emitted | {summary.carbon_g:.1f} g |",
        f"| CO2e at baseline | {summary.baseline_carbon_g:.1f} g |",
        f"| **CO2e reduction** | **{summary.carbon_saved_pct:.1f}%** |",
        f"| Estimated compute cost vs. baseline | {summary.cost_overhead_pct:+.1f}% |",
        f"| Median delay before start | {summary.median_delay_minutes:.0f} min |",
        f"| Spot fallbacks to on-demand | {summary.fallbacks} |",
        "",
        "## Where jobs ran",
        "",
        "| Region | Jobs |",
        "|---|---|",
        *(f"| {region} | {count} |" for region, count in summary.regions.most_common()),
        "",
        "## Carbon data used for decisions",
        "",
        "| Source | Jobs |",
        "|---|---|",
        *(f"| {source} | {count} |" for source, count in summary.data_sources.most_common()),
        "",
        "Emissions use the Cloud Carbon Footprint energy model with each job's declared CPU utilisation",
        "and measured runtime. Costs are estimates from the AWS Price List, with Spot modelled as a",
        "70% discount on on-demand.",
        "",
    ]
    return "\n".join(lines)
