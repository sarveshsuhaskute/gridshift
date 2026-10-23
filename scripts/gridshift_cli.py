#!/usr/bin/env python3
"""Call the deployed GridShift API, signing requests with your AWS credentials (SigV4).

python scripts/gridshift_cli.py submit examples/job.json
python scripts/gridshift_cli.py status <job_id>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def api_url(stack: str, region: str) -> str:
    outputs = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=stack)["Stacks"][0][
        "Outputs"
    ]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "ApiUrl")


def call(method: str, url: str, region: str, body: str | None = None) -> dict:
    request = AWSRequest(method=method, url=url, data=body, headers={"content-type": "application/json"})
    SigV4Auth(boto3.Session().get_credentials(), "execute-api", region).add_auth(request)
    prepared = urllib.request.Request(url, data=body.encode() if body else None, method=method)  # noqa: S310
    for key, value in request.headers.items():
        prepared.add_header(key, value)
    try:
        with urllib.request.urlopen(prepared) as response:  # noqa: S310 (URL comes from our own stack output)
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1", help="home region of the control plane")
    parser.add_argument("--stack", default="GridShift-ControlPlane")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("submit").add_argument("job_file", type=Path)
    sub.add_parser("status").add_argument("job_id")
    args = parser.parse_args()

    base = api_url(args.stack, args.region)
    if args.command == "submit":
        result = call("POST", f"{base}/jobs", args.region, args.job_file.read_text())
    else:
        result = call("GET", f"{base}/jobs/{args.job_id}", args.region)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
