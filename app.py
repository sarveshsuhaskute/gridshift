#!/usr/bin/env python3
"""CDK entry point: one control plane in the home region plus one worker stack per worker region."""

import os

import aws_cdk as cdk

from infra.control_plane_stack import ControlPlaneStack
from infra.worker_stack import WorkerRegionStack
from src.gridshift.regions import PROFILES

app = cdk.App()

home_region = app.node.try_get_context("home_region")
worker_regions = list(app.node.try_get_context("worker_regions"))
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

unknown = [r for r in worker_regions if r not in PROFILES]
if unknown:
    raise ValueError(f"worker regions without a profile in src/gridshift/regions.py: {unknown}")
if home_region not in worker_regions:
    raise ValueError("home_region must also be a worker region (it is the baseline for every job)")

for region in worker_regions:
    stack = WorkerRegionStack(
        app,
        f"GridShift-Worker-{region}",
        home_region=home_region,
        env=cdk.Environment(account=account, region=region),
        description=f"GridShift worker region {region}",
    )
    cdk.Tags.of(stack).add("project", "gridshift")

control_plane = ControlPlaneStack(
    app,
    "GridShift-ControlPlane",
    worker_regions=worker_regions,
    carbon_provider=app.node.try_get_context("carbon_provider"),
    alert_email=app.node.try_get_context("alert_email") or None,
    monthly_budget_usd=float(app.node.try_get_context("monthly_budget_usd")),
    env=cdk.Environment(account=account, region=home_region),
    description="GridShift control plane: API, planner and workflow",
)
cdk.Tags.of(control_plane).add("project", "gridshift")

app.synth()
