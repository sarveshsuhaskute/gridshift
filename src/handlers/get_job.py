"""GET /jobs/{job_id}: return job status and the scheduling decision."""

from __future__ import annotations

import json

from handlers.common import http_response, load_job


def handler(event, _context):
    job_id = (event.get("pathParameters") or {}).get("job_id", "")
    try:
        job = load_job(job_id)
    except KeyError:
        return http_response(404, {"error": "job not found"})
    job.pop("task_token", None)
    job["spec"] = json.loads(job["spec"])
    return http_response(200, job)
