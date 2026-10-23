import json
import os
from unittest import mock

import boto3
import pytest
from moto import mock_aws

os.environ.update(
    AWS_DEFAULT_REGION="us-east-1",
    AWS_ACCESS_KEY_ID="testing",
    AWS_SECRET_ACCESS_KEY="testing",  # noqa: S105 (moto dummy credential)
    HOME_REGION="us-east-1",
    WORKER_REGIONS="us-east-1,eu-north-1,ca-central-1",
    JOBS_TABLE="jobs",
    FORECASTS_TABLE="forecasts",
    TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:gridshift",
    STATE_MACHINE_ARN="arn:aws:states:us-east-1:123456789012:stateMachine:gridshift",
)

TASK_ARN = "arn:aws:ecs:eu-north-1:123456789012:task/gridshift-workers/abc"


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(
            TableName="jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="forecasts",
            KeySchema=[{"AttributeName": "region", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "region", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        from handlers import common

        common._dynamodb.cache_clear()
        yield ddb


def submit(body: dict) -> dict:
    from handlers import submit_job

    with mock.patch.object(submit_job, "_sfn") as sfn:
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}
        return submit_job.handler({"body": json.dumps(body)}, None)


VALID = {
    "name": "nightly-report",
    "command": ["python", "-c", "print('hi')"],
    "vcpu": 1,
    "memory_gb": 2,
    "est_runtime_minutes": 30,
    "deadline": "2099-01-01T00:00:00Z",
}


def test_submit_then_plan_stores_a_decision(aws):
    response = submit(VALID)
    assert response["statusCode"] == 202
    job_id = json.loads(response["body"])["job_id"]

    aws.Table("forecasts").put_item(Item={"region": "ca-central-1", "ts": "2000-01-01T00:00:00Z", "intensity": 1})
    from handlers import plan_job

    result = plan_job.handler({"job_id": job_id}, None)
    assert result["region"] == "eu-north-1"  # static averages: Stockholm is cleanest
    item = aws.Table("jobs").get_item(Key={"job_id": job_id})["Item"]
    assert item["status"] == "SCHEDULED"
    assert "less CO2e" in item["decision"]["explanation"]


@pytest.mark.parametrize(
    "body, error",
    [
        ({**VALID, "vcpu": 3}, "vcpu"),
        ({**VALID, "data_region": "ap-south-1"}, "data_region"),
        ({**VALID, "allowed_regions": ["ap-south-1"]}, "allowed_regions"),
    ],
)
def test_submit_rejects_invalid_jobs(aws, body, error):
    response = submit(body)
    assert response["statusCode"] == 400
    assert error in json.loads(response["body"])["error"]


def stopped_event(task_arn=TASK_ARN, exit_code=0, stop_code="EssentialContainerExited"):
    return {
        "region": "eu-north-1",
        "detail": {
            "taskArn": task_arn,
            "startedBy": "gridshift-job-1",
            "stopCode": stop_code,
            "containers": [{"exitCode": exit_code}],
            "startedAt": "2026-10-24T12:00:00Z",
            "stoppedAt": "2026-10-24T12:30:00Z",
        },
    }


def test_completion_resumes_workflow_exactly_once(aws):
    aws.Table("jobs").put_item(Item={"job_id": "job-1", "task_arn": TASK_ARN, "task_token": "token-1"})
    from handlers import complete_job

    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(stopped_event(), None)
        complete_job.handler(stopped_event(), None)  # duplicate delivery

    sfn.send_task_success.assert_called_once()
    output = json.loads(sfn.send_task_success.call_args.kwargs["output"])
    assert output == {"exit_code": 0, "runtime_seconds": 1800.0, "region": "eu-north-1", "task_arn": TASK_ARN}


def test_completion_ignores_events_from_a_previous_attempt(aws):
    aws.Table("jobs").put_item(Item={"job_id": "job-1", "task_arn": "arn:new-task", "task_token": "token-2"})
    from handlers import complete_job

    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(stopped_event(task_arn=TASK_ARN), None)
    sfn.send_task_success.assert_not_called()
    sfn.send_task_failure.assert_not_called()


def test_spot_interruption_fails_the_attempt(aws):
    aws.Table("jobs").put_item(Item={"job_id": "job-1", "task_arn": TASK_ARN, "task_token": "token-1"})
    from handlers import complete_job

    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(stopped_event(exit_code=None, stop_code="SpotInterruption"), None)
    assert sfn.send_task_failure.call_args.kwargs["error"] == "SpotInterruption"
