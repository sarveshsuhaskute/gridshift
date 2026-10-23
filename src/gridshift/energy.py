"""Energy model for Fargate workloads.

Coefficients follow the Cloud Carbon Footprint methodology for AWS:
https://www.cloudcarbonfootprint.org/docs/methodology
"""

from __future__ import annotations

PUE = 1.135
MIN_WATTS_PER_VCPU = 0.74
MAX_WATTS_PER_VCPU = 3.5
KWH_PER_GB_MEMORY_HOUR = 0.000392
KWH_PER_GB_TRANSFERRED = 0.001
DEFAULT_UTILIZATION = 0.5


def compute_kwh(vcpu: float, memory_gb: float, hours: float, utilization: float = DEFAULT_UTILIZATION) -> float:
    """Energy drawn by a task of the given size, including data-centre overhead (PUE)."""
    if not 0.0 <= utilization <= 1.0:
        raise ValueError("utilization must be between 0 and 1")
    watts_per_vcpu = MIN_WATTS_PER_VCPU + utilization * (MAX_WATTS_PER_VCPU - MIN_WATTS_PER_VCPU)
    cpu_kwh = vcpu * watts_per_vcpu * hours / 1000.0
    memory_kwh = memory_gb * hours * KWH_PER_GB_MEMORY_HOUR
    return (cpu_kwh + memory_kwh) * PUE


def transfer_kwh(gigabytes: float) -> float:
    """Energy used to move data between regions."""
    return gigabytes * KWH_PER_GB_TRANSFERRED


def grams_co2(kwh: float, intensity_g_per_kwh: float) -> float:
    return kwh * intensity_g_per_kwh
