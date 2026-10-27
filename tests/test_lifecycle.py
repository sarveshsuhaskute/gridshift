"""End-to-end job lifecycle, wired exactly as the deployed state machine wires it.

The state machine definition is synthesized from the CDK stack and every JSONPath it reads
from the plan is resolved against plan_job's real output. That way a renamed field in either
the infrastructure or a handler breaks this test instead of a production job.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.control_plane_stack import ControlPlaneStack

WORKER_CONFIG = {
    "cluster": "gridshift-workers",
    "task_definition_family": "gridshift-runner",
    "subnets": ["subnet-1", "subnet-2"],
    "security_groups": ["sg-1"],
    "assign_public_ip": "ENABLED",
    "bucket": "gridshift-outputs",
}


@pytest.fixture(scope="module")
def states() -> dict:
    stack = ControlPlaneStack(
        cdk.App(),
        "ControlPlane",
        worker_regions=["us-east-1", "eu-north-1", "ca-central-1"],
        carbon_provider="static",
        alert_email=None,
        monthly_budget_usd=10,
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    (machine,) = Template.from_stack(stack).find_resources("AWS::StepFunctions::StateMachine").values()
    parts = machine["Properties"]["DefinitionString"]["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "ref" for p in parts))["States"]


def resolve(path: str, document: dict):
    """Minimal JSONPath for the `$.a.b` paths the workflow uses."""
    value = document
    for key in path.removeprefix("$.").split("."):
        value = value[key]
    return value


def payload(state: dict, document: dict) -> dict:
    """Build a Lambda payload the way Step Functions does from a task's Parameters."""
    # Direct invocations pass Parameters as the payload; lambda:invoke wraps it in "Payload".
    parameters = state["Parameters"]
    result = {}
    for key, value in parameters.get("Payload", parameters).items():
        if key.endswith(".$"):
            result[key[:-2]] = "task-token" if value == "$$.Task.Token" else resolve(value, document)
        else:
            result[key] = value
    return result


class FakeEcs:
    def __init__(self, region: str, calls: list):
        self.region, self.calls = region, calls

    def run_task(self, **kwargs):
        self.calls.append((self.region, kwargs))
        arn = f"arn:aws:ecs:{self.region}:123456789012:task/gridshift-workers/{len(self.calls)}"
        return {"tasks": [{"taskArn": arn}], "failures": []}


class FakeSsm:
    def get_parameter(self, Name):  # noqa: N803 (boto3 argument name)
        assert Name == "/gridshift/worker/network"
        return {"Parameter": {"Value": json.dumps(WORKER_CONFIG)}}


def test_job_runs_end_to_end_in_the_cleanest_region(aws, states):
    from handlers import complete_job, dispatch_job, finalize_job, get_job, plan_job, submit_job

    # 1. API: submit
    body = {
        "name": "nightly-report",
        "command": ["python", "-c", "print('hi')"],
        "vcpu": 1,
        "memory_gb": 2,
        "est_runtime_minutes": 30,
        "cpu_utilization": 0.8,
        "deadline": (datetime.now(UTC) + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with mock.patch.object(submit_job, "_sfn") as sfn:
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}
        response = submit_job.handler({"body": json.dumps(body)}, None)
    assert response["statusCode"] == 202
    execution = {"job_id": json.loads(response["body"])["job_id"]}
    assert json.loads(sfn.start_execution.call_args.kwargs["input"]) == execution

    # 2. Plan, stored at the state's ResultPath
    execution["plan"] = plan_job.handler(payload(states["Plan"], execution), None)
    assert states["Plan"]["ResultPath"] == "$.plan"
    assert execution["plan"]["region"] == "eu-north-1"  # static averages: Stockholm is cleanest

    # 3. Every plan field the workflow reads exists in plan_job's output
    resolve(states["WaitForCleanSlot"]["TimestampPath"], execution)
    for name in ("RunOnSpot", "RunFallbackOnDemand"):
        resolve(states[name]["TimeoutSecondsPath"], execution)
        payload(states[name], execution)

    # 4. Dispatch on Spot in the chosen region
    ecs_calls: list = []
    clients = {"ecs": lambda region: FakeEcs(region, ecs_calls), "ssm": lambda region: FakeSsm()}
    dispatch_job._worker_config.cache_clear()
    with mock.patch.object(dispatch_job.boto3, "client", lambda service, region_name: clients[service](region_name)):
        dispatch_job.handler(payload(states["RunOnSpot"], execution), None)
    ((region, run_task),) = ecs_calls
    assert region == "eu-north-1"
    assert run_task["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE_SPOT", "weight": 1}]
    assert run_task["overrides"]["cpu"] == "1024" and run_task["overrides"]["memory"] == "2048"
    assert run_task["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"

    # 5. Task stops in the worker region; the forwarded event resumes the workflow
    task_arn = "arn:aws:ecs:eu-north-1:123456789012:task/gridshift-workers/1"
    event = {
        "detail": {
            "taskArn": task_arn,
            "startedBy": run_task["startedBy"],
            "stopCode": "EssentialContainerExited",
            "containers": [{"name": "job", "exitCode": 0}],
            "startedAt": "2026-10-27T12:00:00Z",
            "stoppedAt": "2026-10-27T12:25:00Z",
        }
    }
    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(event, None)
    assert sfn.send_task_success.call_args.kwargs["taskToken"] == "task-token"
    execution["run"] = json.loads(sfn.send_task_success.call_args.kwargs["output"])

    # 6. Finalize with the payload the workflow builds
    with mock.patch.object(finalize_job, "_sns") as sns:
        finalize_job.handler(payload(states["FinalizeSucceeded"], execution), None)
    assert "succeeded in eu-north-1" in sns.publish.call_args.kwargs["Message"]

    # 7. API: status
    job = json.loads(get_job.handler({"pathParameters": {"job_id": execution["job_id"]}}, None)["body"])
    assert job["status"] == "SUCCEEDED"
    assert "task_token" not in job
    assert job["result"]["runtime_seconds"] == 1500
    assert job["result"]["carbon_saved_g"] > 0.9 * job["result"]["baseline_carbon_g"]


def test_spot_interruption_falls_back_to_on_demand_in_another_region(aws, states):
    from handlers import complete_job, dispatch_job, finalize_job, get_job, plan_job, submit_job

    body = {
        "name": "encode",
        "command": ["true"],
        "est_runtime_minutes": 10,
        "deadline": (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with mock.patch.object(submit_job, "_sfn") as sfn:
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}
        execution = {"job_id": json.loads(submit_job.handler({"body": json.dumps(body)}, None)["body"])["job_id"]}
    execution["plan"] = plan_job.handler(payload(states["Plan"], execution), None)

    ecs_calls: list = []
    clients = {"ecs": lambda region: FakeEcs(region, ecs_calls), "ssm": lambda region: FakeSsm()}
    dispatch_job._worker_config.cache_clear()

    def dispatch(state: str) -> None:
        with mock.patch.object(
            dispatch_job.boto3, "client", lambda service, region_name: clients[service](region_name)
        ):
            dispatch_job.handler(payload(states[state], execution), None)

    def stop(stop_code: str, exit_code):
        region, run_task = ecs_calls[-1]
        event = {
            "detail": {
                "taskArn": f"arn:aws:ecs:{region}:123456789012:task/gridshift-workers/{len(ecs_calls)}",
                "startedBy": run_task["startedBy"],
                "stopCode": stop_code,
                "containers": [{"name": "job", "exitCode": exit_code}],
                "startedAt": "2026-10-27T12:00:00Z",
                "stoppedAt": "2026-10-27T12:10:00Z",
            }
        }
        with mock.patch.object(complete_job, "_sfn") as sfn:
            complete_job.handler(event, None)
        return sfn

    # Spot attempt in the cleanest region is interrupted...
    dispatch("RunOnSpot")
    sfn = stop("SpotInterruption", None)
    assert sfn.send_task_failure.call_args.kwargs["error"] == "SpotInterruption"
    catch = states["RunOnSpot"]["Catch"]
    assert [c["Next"] for c in catch if "States.ALL" in c["ErrorEquals"]] == ["RunFallbackOnDemand"]

    # ...so the workflow re-dispatches on on-demand capacity in the fallback region.
    dispatch("RunFallbackOnDemand")
    region, run_task = ecs_calls[-1]
    assert region == execution["plan"]["fallback_region"] == "us-east-1"
    assert run_task["capacityProviderStrategy"] == [{"capacityProvider": "FARGATE", "weight": 1}]

    sfn = stop("EssentialContainerExited", 0)
    execution["run"] = json.loads(sfn.send_task_success.call_args.kwargs["output"])
    with mock.patch.object(finalize_job, "_sns"):
        finalize_job.handler(payload(states["FinalizeSucceeded"], execution), None)

    job = json.loads(get_job.handler({"pathParameters": {"job_id": execution["job_id"]}}, None)["body"])
    assert job["status"] == "SUCCEEDED"
    assert job["result"]["region"] == "us-east-1"
    assert job["result"]["carbon_saved_g"] == pytest.approx(0, abs=1e-6)  # ran at home after all


def test_a_job_that_crashes_is_not_retried(states):
    catch = states["RunOnSpot"]["Catch"]
    assert catch[0] == {"ErrorEquals": ["EssentialContainerExited"], "ResultPath": "$.error", "Next": "FinalizeFailed"}
