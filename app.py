#!/usr/bin/env python3
"""CDK entry point: one control plane in the home region plus one worker stack per worker region."""

import os
import sys
from pathlib import Path

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).parent / "src"))

from gridshift.regions import PROFILES  # noqa: E402
from infra.control_plane_stack import ControlPlaneStack  # noqa: E402
from infra.github_deploy_stack import GitHubDeployStack  # noqa: E402
from infra.worker_stack import WorkerRegionStack  # noqa: E402

app = cdk.App()

home_region = app.node.try_get_context("home_region")
worker_regions = list(app.node.try_get_context("worker_regions"))
network_mode = app.node.try_get_context("network_mode") or "public"
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
        network_mode=network_mode,
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

github_repo = app.node.try_get_context("github_repo")
if github_repo:
    deploy = GitHubDeployStack(
        app,
        "GridShift-GitHubDeploy",
        github_repo=github_repo,
        existing_provider_arn=app.node.try_get_context("github_oidc_provider_arn") or None,
        env=cdk.Environment(account=account, region=home_region),
        description="GridShift keyless GitHub Actions deploy role",
    )
    cdk.Tags.of(deploy).add("project", "gridshift")

app.synth()
