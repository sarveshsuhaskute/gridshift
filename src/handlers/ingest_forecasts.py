"""Scheduled task: fetch carbon-intensity forecasts for every worker region into DynamoDB."""

from __future__ import annotations

import os
from datetime import timedelta
from functools import cache

import boto3

from gridshift.carbon import ProviderError, build_provider
from gridshift.models import format_time
from gridshift.regions import get_profile
from handlers.common import emit_metrics, forecasts_table, logger, to_dynamo, worker_regions

RETENTION = timedelta(hours=48)


@cache
def _provider():
    name = os.environ.get("CARBON_PROVIDER", "static")
    api_key = None
    if name == "electricitymaps":
        ssm = boto3.client("ssm")
        api_key = ssm.get_parameter(Name=os.environ["API_KEY_PARAMETER"], WithDecryption=True)["Parameter"]["Value"]
    return build_provider(name, api_key)


def handler(_event, _context):
    provider = _provider()
    table = forecasts_table()
    stored = failed = 0
    for region in worker_regions():
        zone = get_profile(region).grid_zone
        try:
            points = provider.forecast(zone)
        except ProviderError as exc:
            # The planner falls back to static averages for this region.
            logger.warning("forecast unavailable for %s (%s): %s", region, zone, exc)
            failed += 1
            continue
        with table.batch_writer(overwrite_by_pkeys=["region", "ts"]) as batch:
            for point in points:
                batch.put_item(
                    Item=to_dynamo(
                        {
                            "region": region,
                            "ts": format_time(point.start),
                            "intensity": point.intensity,
                            "zone": zone,
                            "provider": provider.name,
                            "expires_at": int((point.start + RETENTION).timestamp()),
                        }
                    )
                )
        stored += len(points)
    emit_metrics({"Provider": provider.name}, ForecastPointsStored=stored, ForecastRegionsFailed=failed)
    return {"stored": stored, "failed_regions": failed}
