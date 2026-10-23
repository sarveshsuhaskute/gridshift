import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.control_plane_stack import ControlPlaneStack
from infra.github_deploy_stack import GitHubDeployStack
from infra.worker_stack import WorkerRegionStack

ENV = {"account": "123456789012"}


def worker(region="eu-north-1", network_mode="public", image=None) -> Template:
    app = cdk.App(context={"job_image": image} if image else None)
    stack = WorkerRegionStack(
        app,
        "Worker",
        home_region="us-east-1",
        network_mode=network_mode,
        env=cdk.Environment(region=region, **ENV),
    )
    return Template.from_stack(stack)


def render(value) -> str:
    """Render a CloudFormation string (plain or Fn::Join) with references shown as <ref>."""
    if isinstance(value, str):
        return value
    return "".join(part if isinstance(part, str) else "<ref>" for part in value["Fn::Join"][1])


def worker_config(template: Template) -> str:
    (parameter,) = template.find_resources("AWS::SSM::Parameter").values()
    return render(parameter["Properties"]["Value"])


def policy_statements(template: Template) -> list[dict]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_public_mode_has_no_nat_and_no_endpoints():
    template = worker()
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 0)
    assert '"assign_public_ip":"ENABLED"' in worker_config(template)


def test_private_mode_is_isolated_and_uses_endpoints_and_ecr_cache():
    template = worker(network_mode="private")
    template.resource_count_is("AWS::EC2::NatGateway", 0)
    template.resource_count_is("AWS::EC2::InternetGateway", 0)
    template.resource_count_is("AWS::EC2::VPCEndpoint", 4)  # S3 gateway + ECR API, ECR Docker, Logs
    template.has_resource_properties(
        "AWS::ECR::PullThroughCacheRule", {"EcrRepositoryPrefix": "ecr-public", "UpstreamRegistryUrl": "public.ecr.aws"}
    )
    (task_definition,) = template.find_resources("AWS::ECS::TaskDefinition").values()
    image = render(task_definition["Properties"]["ContainerDefinitions"][0]["Image"])
    assert image == "123456789012.dkr.ecr.eu-north-1.<ref>/ecr-public/docker/library/python:3.12-slim"
    assert '"assign_public_ip":"DISABLED"' in worker_config(template)


def test_private_mode_rejects_images_it_cannot_cache():
    with pytest.raises(ValueError, match="public.ecr.aws"):
        worker(network_mode="private", image="docker.io/library/python:3.12")


def test_only_remote_workers_forward_events_to_the_home_region():
    worker(region="us-east-1").resource_count_is("AWS::Events::Rule", 0)
    worker(region="eu-north-1").has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": Match.object_like({"source": ["aws.ecs"]}), "Targets": [Match.object_like({})]},
    )


def test_control_plane_dispatcher_cannot_run_arbitrary_tasks():
    app = cdk.App()
    stack = ControlPlaneStack(
        app,
        "ControlPlane",
        worker_regions=["us-east-1", "eu-north-1"],
        carbon_provider="static",
        alert_email=None,
        monthly_budget_usd=10,
        env=cdk.Environment(region="us-east-1", **ENV),
    )
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    template.resource_count_is("AWS::Budgets::Budget", 0)
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"Targets": [Match.object_like({"DeadLetterConfig": Match.any_value(), "RetryPolicy": Match.any_value()})]},
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"DefaultRouteSettings": {"ThrottlingRateLimit": 10, "ThrottlingBurstLimit": 20}},
    )
    run_task = [st for st in policy_statements(template) if "ecs:RunTask" in json.dumps(st["Action"])]
    assert len(run_task) == 1
    resources = run_task[0]["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert all("task-definition/gridshift-runner:*" in json.dumps(r) for r in resources)
    assert "cluster/gridshift-workers" in json.dumps(run_task[0]["Condition"]["ArnEquals"])


@pytest.mark.parametrize(
    "existing_provider", [None, "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"]
)
def test_github_deploy_role_is_limited_to_the_production_environment(existing_provider):
    stack = GitHubDeployStack(
        cdk.App(),
        "Deploy",
        github_repo="sarveshsuhaskute/gridshift",
        existing_provider_arn=existing_provider,
        env=cdk.Environment(region="us-east-1", **ENV),
    )
    template = Template.from_stack(stack)
    template.resource_count_is("AWS::IAM::OIDCProvider", 0 if existing_provider else 1)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Action": "sts:AssumeRoleWithWebIdentity",
                            "Condition": {
                                "StringEquals": {
                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                    "token.actions.githubusercontent.com:sub": (
                                        "repo:sarveshsuhaskute/gridshift:environment:production"
                                    ),
                                }
                            },
                        }
                    )
                ]
            }
        },
    )
    (statement,) = policy_statements(template)
    assert statement["Action"] == "sts:AssumeRole"
    assert render(statement["Resource"]).endswith(":role/cdk-hnb659fds-*")
