"""Shared helpers for Lambda handlers."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

METRICS_NAMESPACE = "GridShift"
STARTED_BY_PREFIX = "gridshift-"


def utcnow() -> datetime:
    return datetime.now(UTC)


def home_region() -> str:
    return os.environ["HOME_REGION"]


def worker_regions() -> list[str]:
    return [r for r in os.environ["WORKER_REGIONS"].split(",") if r]


@cache
def _dynamodb():
    return boto3.resource("dynamodb")


def jobs_table():
    return _dynamodb().Table(os.environ["JOBS_TABLE"])


def forecasts_table():
    return _dynamodb().Table(os.environ["FORECASTS_TABLE"])


def to_dynamo(value):
    """Convert floats (which DynamoDB rejects) to Decimal, recursively."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def load_job(job_id: str) -> dict:
    item = jobs_table().get_item(Key={"job_id": job_id}).get("Item")
    if item is None:
        raise KeyError(f"job {job_id} not found")
    return from_dynamo(item)


def http_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def emit_metrics(dimensions: dict[str, str], **metrics: float) -> None:
    """Write CloudWatch metrics using the Embedded Metric Format (no API calls needed)."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRICS_NAMESPACE,
                            "Dimensions": [list(dimensions)],
                            "Metrics": [{"Name": name} for name in metrics],
                        }
                    ],
                },
                **dimensions,
                **metrics,
            }
        )
    )
