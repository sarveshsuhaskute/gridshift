"""Step Functions task: record the outcome, measured emissions and savings; notify."""

from __future__ import annotations

import os

import boto3

from gridshift import energy
from gridshift.models import JobSpec, format_time
from gridshift.regions import get_profile
from handlers.common import emit_metrics, jobs_table, load_job, logger, to_dynamo, utcnow

_sns = boto3.client("sns")


def _measured(spec: JobSpec, decision: dict, run: dict) -> dict:
    hours = float(run.get("runtime_seconds") or 0) / 3600
    region = run.get("region") or decision["region"]
    # A fallback run happens outside the planned slot, so use that region's static average.
    intensity = decision["intensity"] if region == decision["region"] else get_profile(region).static_intensity
    kwh = energy.compute_kwh(spec.vcpu, spec.memory_gb, hours)
    if region != spec.data_region:
        kwh += energy.transfer_kwh(spec.input_data_gb)
    emitted = energy.grams_co2(kwh, intensity)
    baseline = energy.grams_co2(energy.compute_kwh(spec.vcpu, spec.memory_gb, hours), decision["baseline_intensity"])
    return {
        "region": region,
        "runtime_seconds": round(hours * 3600, 1),
        "carbon_g": round(emitted, 3),
        "baseline_carbon_g": round(baseline, 3),
        "carbon_saved_g": round(baseline - emitted, 3),
    }


def handler(event, _context):
    job_id = event["job_id"]
    outcome = event["outcome"]
    job = load_job(job_id)
    spec = JobSpec.from_json(job["spec"])
    decision = job.get("decision") or {}

    values = {":s": outcome, ":f": format_time(utcnow())}
    update = "SET #s = :s, finished_at = :f"
    if outcome == "SUCCEEDED" and decision:
        result = _measured(spec, decision, event.get("run") or {})
        values[":r"] = to_dynamo(result)
        update += ", #result = :r"
        emit_metrics(
            {"Region": result["region"]},
            JobsSucceeded=1,
            CarbonEmittedGrams=result["carbon_g"],
            CarbonSavedGrams=result["carbon_saved_g"],
        )
        message = (
            f"Job {spec.name} ({job_id}) succeeded in {result['region']} after {result['runtime_seconds']:.0f}s. "
            f"Emitted {result['carbon_g']:.1f} gCO2e, saved {result['carbon_saved_g']:.1f} gCO2e "
            f"versus running now in {spec.data_region}."
        )
    else:
        error = event.get("error") or {}
        values[":e"] = to_dynamo({"error": error.get("Error", "Unknown"), "cause": error.get("Cause", "")[:1000]})
        update += ", #result = :e"
        emit_metrics({"Region": job.get("run_region", "none")}, JobsFailed=1)
        message = f"Job {spec.name} ({job_id}) failed: {error.get('Error', 'Unknown')} - {error.get('Cause', '')}"

    jobs_table().update_item(
        Key={"job_id": job_id},
        UpdateExpression=update,
        ExpressionAttributeNames={"#s": "status", "#result": "result"},
        ExpressionAttributeValues=values,
    )
    _sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject=f"GridShift job {outcome.lower()}", Message=message)
    logger.info(message)
    return {"job_id": job_id, "status": outcome}
