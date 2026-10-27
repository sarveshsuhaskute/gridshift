import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gridshift.models import JobSpec
from gridshift.report import summarize, to_markdown

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def finished(region="eu-north-1", planned="eu-north-1", carbon=10.0, baseline=100.0, cost=1.1, delay_min=30):
    return {
        "status": "SUCCEEDED",
        "submitted_at": "2026-10-27T12:00:00Z",
        "decision": {
            "region": planned,
            "start_at": f"2026-10-27T{12 + delay_min // 60:02d}:{delay_min % 60:02d}:00Z",
            "est_cost_usd": cost,
            "baseline_cost_usd": 1.0,
            "data_source": "forecast",
        },
        "result": {"region": region, "carbon_g": carbon, "baseline_carbon_g": baseline},
    }


def test_summary_aggregates_finished_jobs_only():
    jobs = [
        finished(),
        finished(region="us-east-1", carbon=100.0, cost=1.0, delay_min=0),  # Spot fallback, ran at home
        {"status": "FAILED"},
        {"status": "SCHEDULED"},
    ]
    summary = summarize(jobs)
    assert (summary.total, summary.succeeded, summary.failed, summary.pending) == (4, 2, 1, 1)
    assert summary.carbon_saved_pct == pytest.approx(45.0)  # (200 - 110) / 200
    assert summary.cost_overhead_pct == pytest.approx(5.0)  # 2.1 vs 2.0
    assert summary.fallbacks == 1
    assert summary.median_delay_minutes == pytest.approx(15.0)
    assert summary.regions == {"eu-north-1": 1, "us-east-1": 1}


def test_empty_run_reports_zero_without_dividing_by_zero():
    summary = summarize([{"status": "SCHEDULED"}])
    assert summary.carbon_saved_pct == 0 and summary.cost_overhead_pct == 0 and summary.median_delay_minutes == 0


def test_markdown_contains_the_headline_numbers():
    text = to_markdown(summarize([finished()]), "2026-10-27 12:00 UTC", "us-east-1")
    assert "**90.0%**" in text
    assert "| eu-north-1 | 1 |" in text
    assert "home region (us-east-1)" in text


def test_benchmark_workload_is_valid_for_the_api():
    import benchmark

    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    for i, template in enumerate(benchmark.WORKLOAD):
        body = benchmark.build_job(template, i, now)
        spec = JobSpec.from_request(json.loads(json.dumps(body)), "us-east-1", now)
        assert spec.est_runtime_minutes > template["minutes"]  # estimate leaves headroom
