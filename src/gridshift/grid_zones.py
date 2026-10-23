"""Grid zone and static carbon intensity per AWS region.

Static intensities are rough annual grid averages (gCO2e/kWh), used only when no live forecast
is available. Zones are Electricity Maps zone identifiers.
"""

# region -> (Electricity Maps grid zone, static annual intensity)
STATIC: dict[str, tuple[str, float]] = {
    "us-east-1": ("US-MIDA-PJM", 380.0),
    "us-west-2": ("US-NW-BPAT", 110.0),
    "ca-central-1": ("CA-QC", 35.0),
    "eu-north-1": ("SE-SE3", 25.0),
    "eu-west-1": ("IE", 330.0),
    "eu-west-3": ("FR", 55.0),
    "eu-central-1": ("DE", 380.0),
    "ap-south-1": ("IN-WE", 680.0),
}
