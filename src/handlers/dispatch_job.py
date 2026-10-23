"""Step Functions task (waitForTaskToken): start the job's container in the chosen region."""

from __future__ import annotations

import json
from functools import cache

import boto3

from gridshift.models import JobSpec, format_time
from handlers.common import STARTED_BY_PREFIX, jobs_table, load_job, logger, utcnow

NETWORK_PARAMETER = "/gridshift/worker/network"


class DispatchError(RuntimeError):
    pass


@cache
def _worker_config(region: str) -> dict:
    """Worker stacks publish their cluster, subnets and security group to SSM."""
    ssm = boto3.client("ssm", region_name=region)
    return json.loads(ssm.get_parameter(Name=NETWORK_PARAMETER)["Parameter"]["Value"])


def handler(event, _context):
    job_id = event["job_id"]
    region = event["region"]
    capacity = event["capacity_provider"]
    spec = JobSpec.from_json(load_job(job_id)["spec"])
    worker = _worker_config(region)

    # Store the token before starting the task so the completion event can always find it.
    jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, task_token = :t, run_region = :r, capacity = :c REMOVE task_arn",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "DISPATCHING", ":t": event["task_token"], ":r": region, ":c": capacity},
    )

    ecs = boto3.client("ecs", region_name=region)
    response = ecs.run_task(
        cluster=worker["cluster"],
        taskDefinition=worker["task_definition_family"],
        capacityProviderStrategy=[{"capacityProvider": capacity, "weight": 1}],
        count=1,
        startedBy=f"{STARTED_BY_PREFIX}{job_id}",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": worker["subnets"],
                "securityGroups": worker["security_groups"],
                "assignPublicIp": worker.get("assign_public_ip", "ENABLED"),
            }
        },
        overrides={
            "cpu": str(int(spec.vcpu * 1024)),
            "memory": str(int(spec.memory_gb * 1024)),
            "containerOverrides": [
                {
                    "name": "job",
                    "command": list(spec.command),
                    "environment": [
                        {"name": "GRIDSHIFT_JOB_ID", "value": job_id},
                        {"name": "GRIDSHIFT_OUTPUT_URI", "value": f"s3://{worker['bucket']}/jobs/{job_id}/"},
                    ],
                }
            ],
        },
        tags=[{"key": "gridshift:job-id", "value": job_id}],
    )
    if response.get("failures") or not response.get("tasks"):
        raise DispatchError(f"RunTask failed in {region}: {response.get('failures')}")

    task_arn = response["tasks"][0]["taskArn"]
    jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, task_arn = :a, dispatched_at = :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "RUNNING", ":a": task_arn, ":d": format_time(utcnow())},
    )
    logger.info("job %s dispatched to %s on %s: %s", job_id, region, capacity, task_arn)
    return {"task_arn": task_arn}
