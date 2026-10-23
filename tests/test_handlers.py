import json
import os
from datetime import UTC, datetime, timedelta
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
            "containers": [{"name": "job", "exitCode": exit_code}],
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


def test_completion_before_dispatcher_records_the_task_arn(aws):
    aws.Table("jobs").put_item(Item={"job_id": "job-1", "run_region": "eu-north-1", "task_token": "token-1"})
    from handlers import complete_job

    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(stopped_event(), None)
    sfn.send_task_success.assert_called_once()
    assert aws.Table("jobs").get_item(Key={"job_id": "job-1"})["Item"]["task_arn"] == TASK_ARN


def test_early_completion_from_another_region_is_ignored(aws):
    aws.Table("jobs").put_item(Item={"job_id": "job-1", "run_region": "us-east-1", "task_token": "token-1"})
    from handlers import complete_job

    with mock.patch.object(complete_job, "_sfn") as sfn:
        complete_job.handler(stopped_event(), None)  # task ARN is in eu-north-1
    sfn.send_task_success.assert_not_called()


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


def stored_job(aws, **extra):
    from gridshift.models import JobSpec

    spec = JobSpec(
        job_id="job-1",
        name="render",
        command=("true",),
        vcpu=1.0,
        memory_gb=2.0,
        est_runtime_minutes=60,
        deadline=datetime(2099, 1, 1, tzinfo=UTC),
        data_region="us-east-1",
    )
    decision = {"region": "eu-north-1", "intensity": 25, "baseline_intensity": 380}
    aws.Table("jobs").put_item(
        Item={"job_id": "job-1", "spec": spec.to_json(), "decision": decision, "run_region": "eu-north-1", **extra}
    )


def test_finalize_success_records_measured_savings(aws):
    stored_job(aws)
    from handlers import finalize_job

    run = {"runtime_seconds": 3600, "region": "eu-north-1", "exit_code": 0}
    with mock.patch.object(finalize_job, "_sns") as sns:
        finalize_job.handler({"job_id": "job-1", "outcome": "SUCCEEDED", "run": run}, None)

    item = aws.Table("jobs").get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "SUCCEEDED"
    result = item["result"]
    assert result["region"] == "eu-north-1"
    assert float(result["carbon_saved_g"]) == pytest.approx(
        float(result["baseline_carbon_g"]) * (1 - 25 / 380), rel=1e-3
    )
    assert "succeeded in eu-north-1" in sns.publish.call_args.kwargs["Message"]


def test_finalize_fallback_run_uses_that_regions_static_intensity(aws):
    stored_job(aws)
    from handlers import finalize_job

    run = {"runtime_seconds": 3600, "region": "us-east-1"}
    with mock.patch.object(finalize_job, "_sns"):
        finalize_job.handler({"job_id": "job-1", "outcome": "SUCCEEDED", "run": run}, None)
    result = aws.Table("jobs").get_item(Key={"job_id": "job-1"})["Item"]["result"]
    assert float(result["carbon_saved_g"]) == pytest.approx(0, abs=1e-6)  # ran at home: nothing saved


def test_finalize_failure_records_the_error(aws):
    stored_job(aws)
    from handlers import finalize_job

    error = {"Error": "SpotInterruption", "Cause": "exit code None"}
    with mock.patch.object(finalize_job, "_sns") as sns:
        finalize_job.handler({"job_id": "job-1", "outcome": "FAILED", "error": error}, None)
    item = aws.Table("jobs").get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "FAILED"
    assert item["result"]["error"] == "SpotInterruption"
    assert "failed: SpotInterruption" in sns.publish.call_args.kwargs["Message"]


def test_ingest_stores_forecasts_and_survives_a_failing_region(aws):
    from gridshift.carbon import ForecastPoint, ProviderError
    from handlers import ingest_forecasts

    class FakeProvider:
        name = "fake"

        def forecast(self, zone):
            if zone == "CA-QC":
                raise ProviderError("HTTP 503")
            start = datetime(2026, 10, 24, 12, tzinfo=UTC)
            return [ForecastPoint(start + timedelta(hours=h), 100.0 + h) for h in range(3)]

    with mock.patch.object(ingest_forecasts, "_provider", return_value=FakeProvider()):
        result = ingest_forecasts.handler({}, None)

    assert result == {"stored": 6, "failed_regions": 1}  # us-east-1 + eu-north-1, 3 points each
    items = aws.Table("forecasts").scan()["Items"]
    assert {i["region"] for i in items} == {"us-east-1", "eu-north-1"}
    assert all(int(i["expires_at"]) > 0 and i["provider"] == "fake" for i in items)
