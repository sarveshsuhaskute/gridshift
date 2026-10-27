import os

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
