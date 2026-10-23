"""EventBridge target: resume the workflow when a job's ECS task stops.

Events can arrive late or more than once, and a retried job may get an event from its
previous task. The conditional update guarantees each task token is used at most once,
and only by the task that currently owns it.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from gridshift.models import parse_time
from handlers.common import STARTED_BY_PREFIX, jobs_table, logger

_sfn = boto3.client("stepfunctions")


def _claim_token(job_id: str, task_arn: str) -> str | None:
    # A very fast task can stop before the dispatcher records its ARN. In that case accept the
    # event if it comes from the region the current attempt was sent to.
    region = task_arn.split(":")[3]
    try:
        old = jobs_table().update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET task_arn = :a REMOVE task_token",
            ConditionExpression=(
                "attribute_exists(task_token) AND "
                "(task_arn = :a OR (attribute_not_exists(task_arn) AND run_region = :r))"
            ),
            ExpressionAttributeValues={":a": task_arn, ":r": region},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return old["Attributes"]["task_token"]


def handler(event, _context):
    detail = event["detail"]
    started_by = detail.get("startedBy", "")
    if not started_by.startswith(STARTED_BY_PREFIX):
        return
    job_id = started_by.removeprefix(STARTED_BY_PREFIX)
    task_arn = detail["taskArn"]

    token = _claim_token(job_id, task_arn)
    if token is None:
        logger.info("ignoring stale or duplicate event for job %s task %s", job_id, task_arn)
        return

    # The task definition has a single essential container named "job".
    job_container = next((c for c in detail.get("containers", []) if c.get("name") == "job"), {})
    exit_code = job_container.get("exitCode")
    stop_code = detail.get("stopCode", "")
    runtime = 0.0
    if detail.get("startedAt") and detail.get("stoppedAt"):
        runtime = (parse_time(detail["stoppedAt"]) - parse_time(detail["startedAt"])).total_seconds()

    try:
        if exit_code == 0 and stop_code == "EssentialContainerExited":
            output = {
                "exit_code": exit_code,
                "runtime_seconds": runtime,
                # arn:aws:ecs:<region>:<account>:task/... identifies where it actually ran
                "region": task_arn.split(":")[3],
                "task_arn": task_arn,
            }
            _sfn.send_task_success(taskToken=token, output=json.dumps(output))
        else:
            reason = detail.get("stoppedReason", "")
            _sfn.send_task_failure(
                taskToken=token,
                error=stop_code or "TaskFailed",
                cause=f"exit code {exit_code}: {reason}"[:256],
            )
    except _sfn.exceptions.TaskTimedOut:
        logger.warning("task token for job %s already timed out", job_id)
    except _sfn.exceptions.InvalidToken:
        logger.warning("task token for job %s is no longer valid", job_id)
