#!/usr/bin/env python3
"""Refresh src/gridshift/prices.json from the public AWS Price List (no credentials needed).

    python scripts/refresh_prices.py

Fetches on-demand Fargate (Linux/x86) vCPU and memory prices and inter-region data-transfer
prices for every region in gridshift/regions.py. Fargate Spot prices are not published in the
Price List because they change over time, so the planner derives them with a discount factor.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gridshift.grid_zones import STATIC  # noqa: E402

BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
OUTPUT = ROOT / "src" / "gridshift" / "prices.json"


def fetch(offer: str, region: str) -> dict:
    url = f"{BASE}/{offer}/current/{region}/index.json"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (fixed https URL)
        return json.loads(response.read())


def on_demand_prices(offer: dict) -> dict[str, tuple[dict, float]]:
    """sku -> (product attributes, first-tier USD price)."""
    result = {}
    for sku, product in offer["products"].items():
        for term in offer["terms"].get("OnDemand", {}).get(sku, {}).values():
            dimension = min(term["priceDimensions"].values(), key=lambda d: float(d.get("beginRange", 0)))
            result[sku] = (product["attributes"], float(dimension["pricePerUnit"]["USD"]))
    return result


def fargate_prices(region: str) -> tuple[float, float]:
    vcpu = memory = None
    for attributes, price in on_demand_prices(fetch("AmazonECS", region)).values():
        usage = attributes.get("usagetype", "")
        if usage.endswith("-Fargate-vCPU-Hours:perCPU"):
            vcpu = price
        elif usage.endswith("-Fargate-GB-Hours"):
            memory = price
    if vcpu is None or memory is None:
        raise RuntimeError(f"Fargate prices not found for {region}")
    return vcpu, memory


def transfer_prices(region: str, destinations: list[str]) -> dict[str, float]:
    prices = {}
    for attributes, price in on_demand_prices(fetch("AWSDataTransfer", region)).values():
        if (
            attributes.get("transferType") == "InterRegion Outbound"
            and attributes.get("fromRegionCode") == region
            and attributes.get("toRegionCode") in destinations
        ):
            prices[attributes["toRegionCode"]] = price
    return dict(sorted(prices.items()))


def main() -> None:
    regions = sorted(STATIC)
    data = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "AWS Price List bulk API (on-demand, Linux/x86)",
        "regions": {},
    }
    for region in regions:
        vcpu, memory = fargate_prices(region)
        data["regions"][region] = {
            "fargate_vcpu_hour": vcpu,
            "fargate_gb_hour": memory,
            "transfer_out_per_gb": transfer_prices(region, [r for r in regions if r != region]),
        }
        print(f"{region:<14} vCPU ${vcpu:.5f}/h  GB ${memory:.6f}/h", file=sys.stderr)
    OUTPUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
