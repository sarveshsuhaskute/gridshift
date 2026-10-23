"""Step Functions task: choose region and start time for a job."""

from __future__ import annotations

from datetime import timedelta

from boto3.dynamodb.conditions import Key

from gridshift.carbon import ForecastPoint
from gridshift.models import JobSpec, format_time, parse_time
from gridshift.regions import get_profile
from gridshift.scheduler import plan
from handlers.common import forecasts_table, jobs_table, load_job, logger, to_dynamo, utcnow, worker_regions


def _load_forecasts(regions: list[str], start, end) -> dict[str, list[ForecastPoint]]:
    table = forecasts_table()
    # Include the point just before "now", which is still in effect.
    lower, upper = format_time(start - timedelta(hours=1)), format_time(end)
    forecasts = {}
    for region in regions:
        items = table.query(KeyConditionExpression=Key("region").eq(region) & Key("ts").between(lower, upper))["Items"]
        forecasts[region] = [ForecastPoint(parse_time(i["ts"]), float(i["intensity"])) for i in items]
    return forecasts


def handler(event, _context):
    job_id = event["job_id"]
    spec = JobSpec.from_json(load_job(job_id)["spec"])
    regions = worker_regions()
    profiles = {r: get_profile(r) for r in regions}
    now = utcnow()

    decision = plan(spec, now, profiles, _load_forecasts(regions, now, spec.deadline))
    logger.info("job %s: %s", job_id, decision.explanation)

    jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s, decision = :d",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "SCHEDULED", ":d": to_dynamo(decision.to_dict())},
    )
    return {
        "region": decision.region,
        "start_at": decision.start_at,
        "fallback_region": decision.fallback_region,
        "timeout_seconds": decision.timeout_seconds,
    }
