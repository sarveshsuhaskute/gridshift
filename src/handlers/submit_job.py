"""POST /jobs: validate a job, store it and start its workflow."""

from __future__ import annotations

import base64
import binascii
import json
import os

import boto3

from gridshift.models import JobSpec, ValidationError, format_time
from handlers.common import home_region, http_response, jobs_table, logger, utcnow, worker_regions

_sfn = boto3.client("stepfunctions")


def handler(event, _context):
    body = event.get("body") or "{}"
    try:
        # API Gateway base64-encodes bodies it doesn't recognise as text (e.g. no content-type).
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        body = json.loads(body)
    except (json.JSONDecodeError, binascii.Error, UnicodeDecodeError):
        return http_response(400, {"error": "body must be valid JSON"})

    now = utcnow()
    try:
        spec = JobSpec.from_request(body, home_region(), now)
    except ValidationError as exc:
        return http_response(400, {"error": str(exc)})

    workers = set(worker_regions())
    if spec.data_region not in workers:
        return http_response(400, {"error": f"data_region must be one of {sorted(workers)}"})
    if spec.allowed_regions and not set(spec.allowed_regions) & workers:
        return http_response(400, {"error": f"allowed_regions must include one of {sorted(workers)}"})

    jobs_table().put_item(
        Item={
            "job_id": spec.job_id,
            "name": spec.name,
            "status": "PLANNING",
            "submitted_at": format_time(now),
            "spec": spec.to_json(),
        }
    )
    execution = _sfn.start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=spec.job_id,
        input=json.dumps({"job_id": spec.job_id}),
    )
    logger.info("submitted job %s (%s)", spec.job_id, spec.name)
    return http_response(202, {"job_id": spec.job_id, "execution_arn": execution["executionArn"]})
