"""Static per-region profile: grid zone, fallback carbon intensity and approximate prices.

Values are approximations (last reviewed 2026-10) used for planning only. Static
intensities are rough annual averages and are used only when no live forecast is available.
Prices are Fargate Spot (Linux/x86) per hour and inter-region data transfer out per GB.
Check them against the AWS pricing pages before relying on them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegionProfile:
    region: str
    grid_zone: str
    static_intensity: float
    spot_vcpu_hour: float
    spot_gb_hour: float
    transfer_out_per_gb: float


_PROFILES = [
    RegionProfile("us-east-1", "US-MIDA-PJM", 380.0, 0.01245, 0.00137, 0.02),
    RegionProfile("us-west-2", "US-NW-BPAT", 110.0, 0.01245, 0.00137, 0.02),
    RegionProfile("ca-central-1", "CA-QC", 35.0, 0.01370, 0.00150, 0.02),
    RegionProfile("eu-north-1", "SE-SE3", 25.0, 0.01310, 0.00144, 0.02),
    RegionProfile("eu-west-1", "IE", 330.0, 0.01390, 0.00152, 0.02),
    RegionProfile("eu-west-3", "FR", 55.0, 0.01470, 0.00160, 0.02),
    RegionProfile("eu-central-1", "DE", 380.0, 0.01520, 0.00167, 0.02),
    RegionProfile("ap-south-1", "IN-WE", 680.0, 0.01220, 0.00134, 0.086),
]

PROFILES: dict[str, RegionProfile] = {p.region: p for p in _PROFILES}


def get_profile(region: str) -> RegionProfile:
    try:
        return PROFILES[region]
    except KeyError:
        raise KeyError(f"No GridShift profile for region {region!r}; add it to gridshift/regions.py") from None
