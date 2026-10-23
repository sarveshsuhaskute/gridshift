"""Per-region profile: grid zone, fallback carbon intensity (see grid_zones.py) and prices.

Prices come from prices.json, which ``scripts/refresh_prices.py`` generates from the public AWS
Price List. Fargate Spot prices are not published there because they change over time, so they
are modelled as a discount on on-demand (AWS advertises up to 70%).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gridshift.grid_zones import STATIC

SPOT_DISCOUNT = 0.70
# Conservative rate used when the price file has no entry for a route.
DEFAULT_TRANSFER_PER_GB = 0.147


@dataclass(frozen=True)
class RegionProfile:
    region: str
    grid_zone: str
    static_intensity: float
    vcpu_hour: float
    gb_hour: float
    transfer_out_per_gb: Mapping[str, float]

    @property
    def spot_vcpu_hour(self) -> float:
        return self.vcpu_hour * (1 - SPOT_DISCOUNT)

    @property
    def spot_gb_hour(self) -> float:
        return self.gb_hour * (1 - SPOT_DISCOUNT)

    def transfer_cost_per_gb(self, destination: str) -> float:
        return self.transfer_out_per_gb.get(destination, DEFAULT_TRANSFER_PER_GB)


def _load() -> dict[str, RegionProfile]:
    prices = json.loads((Path(__file__).parent / "prices.json").read_text())["regions"]
    return {
        region: RegionProfile(
            region=region,
            grid_zone=zone,
            static_intensity=intensity,
            vcpu_hour=prices[region]["fargate_vcpu_hour"],
            gb_hour=prices[region]["fargate_gb_hour"],
            transfer_out_per_gb=prices[region]["transfer_out_per_gb"],
        )
        for region, (zone, intensity) in STATIC.items()
    }


PROFILES: dict[str, RegionProfile] = _load()


def get_profile(region: str) -> RegionProfile:
    try:
        return PROFILES[region]
    except KeyError:
        raise KeyError(f"No GridShift profile for region {region!r}; add it to gridshift/regions.py") from None
