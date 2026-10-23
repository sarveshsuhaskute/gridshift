#!/usr/bin/env python3
"""Run the GridShift planner on your machine, no AWS account needed.

python scripts/plan_locally.py --runtime 90 --deadline-hours 12 --input-gb 5
ELECTRICITYMAPS_API_KEY=... python scripts/plan_locally.py   # live forecasts
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gridshift.carbon import ProviderError, build_provider  # noqa: E402
from gridshift.models import JobSpec  # noqa: E402
from gridshift.regions import PROFILES  # noqa: E402
from gridshift.scheduler import plan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--regions", default="us-east-1,ca-central-1,eu-north-1,eu-west-1,ap-south-1")
    parser.add_argument("--home", default="us-east-1", help="region where the input data lives")
    parser.add_argument("--vcpu", type=float, default=1)
    parser.add_argument("--memory-gb", type=float, default=2)
    parser.add_argument("--runtime", type=int, default=60, help="estimated runtime in minutes")
    parser.add_argument("--deadline-hours", type=float, default=12)
    parser.add_argument("--input-gb", type=float, default=0)
    parser.add_argument("--max-cost-overhead", type=float, default=25)
    parser.add_argument("--allowed", help="comma-separated data-residency allow list")
    args = parser.parse_args()

    regions = args.regions.split(",")
    profiles = {r: PROFILES[r] for r in regions}
    api_key = os.environ.get("ELECTRICITYMAPS_API_KEY")
    provider = build_provider("electricitymaps" if api_key else "static", api_key)

    forecasts = {}
    for region in regions:
        try:
            forecasts[region] = provider.forecast(profiles[region].grid_zone)
        except ProviderError as exc:
            print(f"warning: {region}: {exc}; using static average", file=sys.stderr)

    now = datetime.now(UTC)
    job = JobSpec(
        job_id="local",
        name="local-demo",
        command=("true",),
        vcpu=args.vcpu,
        memory_gb=args.memory_gb,
        est_runtime_minutes=args.runtime,
        deadline=now + timedelta(hours=args.deadline_hours),
        data_region=args.home,
        input_data_gb=args.input_gb,
        allowed_regions=tuple(args.allowed.split(",")) if args.allowed else None,
        max_cost_overhead_pct=args.max_cost_overhead,
    )
    decision = plan(job, now, profiles, forecasts)

    print(f"provider: {provider.name}\n")
    print(f"{'region':<14}{'zone':<14}{'now gCO2e/kWh':>15}")
    for region in regions:
        series = forecasts.get(region) or []
        current = next((p.intensity for p in reversed(series) if p.start <= now), None)
        shown = f"{current:.0f}" if current is not None else f"{profiles[region].static_intensity:.0f} (static)"
        print(f"{region:<14}{profiles[region].grid_zone:<14}{shown:>15}")
    print()
    for key, value in decision.to_dict().items():
        print(f"{key:>22}: {value}")


if __name__ == "__main__":
    main()
