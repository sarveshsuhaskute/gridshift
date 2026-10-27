#!/usr/bin/env python3
"""Run a benchmark workload against the deployed API and publish measured results.

    python scripts/benchmark.py submit --count 20   # submit a mixed workload
    python scripts/benchmark.py report              # write docs/RESULTS.md from finished jobs

Jobs may wait hours for a clean slot, so `report` can be re-run at any time; unfinished jobs are
counted as pending. Each job is a few minutes of 0.25-1 vCPU, so a 20-job run costs cents.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from gridshift_cli import api_url, call  # noqa: E402

from gridshift.report import summarize, to_markdown  # noqa: E402

RUNS = ROOT / ".benchmark"
RESULTS = ROOT / "docs" / "RESULTS.md"

SLEEP = "import time; time.sleep({seconds})"
BUSY = "import time\nend = time.time() + {seconds}\nwhile time.time() < end: pass"

# A mix that exercises every constraint the planner handles.
WORKLOAD = [
    {"name": "nightly-report", "vcpu": 0.25, "memory_gb": 0.5, "minutes": 2, "busy": False, "deadline_h": 12},
    {"name": "video-encode", "vcpu": 1, "memory_gb": 2, "minutes": 3, "busy": True, "deadline_h": 24},
    {"name": "etl-5gb-input", "vcpu": 0.5, "memory_gb": 1, "minutes": 2, "busy": True, "deadline_h": 6,
     "input_data_gb": 5},
    {"name": "eu-resident", "vcpu": 0.5, "memory_gb": 1, "minutes": 2, "busy": True, "deadline_h": 6,
     "allowed_regions": ["eu-north-1"]},
    {"name": "zero-budget", "vcpu": 0.5, "memory_gb": 1, "minutes": 2, "busy": True, "deadline_h": 6,
     "max_cost_overhead_pct": 0},
    {"name": "urgent", "vcpu": 0.25, "memory_gb": 0.5, "minutes": 2, "busy": False, "deadline_h": 1},
]  # fmt: skip


def build_job(template: dict, index: int, now: datetime) -> dict:
    seconds = template["minutes"] * 60
    code = (BUSY if template["busy"] else SLEEP).format(seconds=seconds)
    job = {
        "name": f"bench-{template['name']}-{index}",
        "command": ["python", "-c", code],
        "vcpu": template["vcpu"],
        "memory_gb": template["memory_gb"],
        "est_runtime_minutes": template["minutes"] + 1,
        "cpu_utilization": 1.0 if template["busy"] else 0.05,
        "deadline": (now + timedelta(hours=template["deadline_h"])).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key in ("input_data_gb", "allowed_regions", "max_cost_overhead_pct"):
        if key in template:
            job[key] = template[key]
    return job


def submit(args) -> None:
    base = api_url(args.stack, args.region)
    now = datetime.now(UTC)
    job_ids = []
    for i in range(args.count):
        job = build_job(WORKLOAD[i % len(WORKLOAD)], i, now)
        response = call("POST", f"{base}/jobs", args.region, json.dumps(job))
        job_ids.append(response["job_id"])
        print(f"submitted {job['name']}: {response['job_id']}")
    RUNS.mkdir(exist_ok=True)
    run_file = RUNS / f"run-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    run_file.write_text(json.dumps({"submitted_at": now.isoformat(), "job_ids": job_ids}, indent=2))
    print(f"\n{len(job_ids)} jobs submitted; ids saved to {run_file.relative_to(ROOT)}")
    print("Run `python scripts/benchmark.py report` once they finish (some wait hours for a clean slot).")


def report(args) -> None:
    runs = sorted(RUNS.glob("run-*.json"))
    run_file = Path(args.run) if args.run else (runs[-1] if runs else None)
    if run_file is None:
        sys.exit("no benchmark run found; run `benchmark.py submit` first")
    base = api_url(args.stack, args.region)
    job_ids = json.loads(run_file.read_text())["job_ids"]
    jobs = [call("GET", f"{base}/jobs/{job_id}", args.region) for job_id in job_ids]
    summary = summarize(jobs)
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    RESULTS.write_text(to_markdown(summary, generated, args.region))
    print(RESULTS.read_text())
    if summary.pending:
        print(f"{summary.pending} jobs still pending; re-run later for final numbers.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default="us-east-1", help="home region of the control plane")
    parser.add_argument("--stack", default="GridShift-ControlPlane")
    sub = parser.add_subparsers(dest="command", required=True)
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--count", type=int, default=20)
    submit_parser.set_defaults(func=submit)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--run", help="run file (default: the latest)")
    report_parser.set_defaults(func=report)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
