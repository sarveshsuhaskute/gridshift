"""Job specification and validation."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from gridshift import energy

# Valid Fargate task sizes: vCPU -> allowed memory values in GB.
FARGATE_SIZES: dict[float, tuple[float, ...]] = {
    0.25: (0.5, 1, 2),
    0.5: tuple(range(1, 5)),
    1: tuple(range(2, 9)),
    2: tuple(range(4, 17)),
    4: tuple(range(8, 31)),
    8: tuple(range(16, 61, 4)),
    16: tuple(range(32, 121, 8)),
}

MAX_RUNTIME_MINUTES = 24 * 60


class ValidationError(ValueError):
    pass


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValidationError(f"timestamp {value!r} must include a timezone, e.g. 2026-10-24T06:00:00Z")
    return dt.astimezone(UTC)


def format_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    name: str
    command: tuple[str, ...]
    vcpu: float
    memory_gb: float
    est_runtime_minutes: int
    deadline: datetime
    data_region: str
    input_data_gb: float = 0.0
    allowed_regions: tuple[str, ...] | None = None
    max_cost_overhead_pct: float = 25.0
    # Average CPU load while the job runs (0-1). Drives the energy estimate: a mostly idle
    # I/O-bound job draws far less power than a CPU-bound render.
    cpu_utilization: float = energy.DEFAULT_UTILIZATION

    @property
    def est_runtime_hours(self) -> float:
        return self.est_runtime_minutes / 60.0

    def compute_kwh(self, hours: float) -> float:
        return energy.compute_kwh(self.vcpu, self.memory_gb, hours, self.cpu_utilization)

    @classmethod
    def from_request(cls, data: dict, home_region: str, now: datetime) -> JobSpec:
        """Build and validate a spec from an API request body."""
        if not isinstance(data, dict):
            raise ValidationError("request body must be a JSON object")

        name = str(data.get("name", "")).strip()
        if not name or len(name) > 128:
            raise ValidationError("name is required (max 128 characters)")

        command = data.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            raise ValidationError("command must be a non-empty list of strings")

        try:
            vcpu = float(data.get("vcpu", 0.25))
            memory_gb = float(data.get("memory_gb", 0.5))
            runtime = int(data["est_runtime_minutes"])
            input_gb = float(data.get("input_data_gb", 0.0))
            overhead = float(data.get("max_cost_overhead_pct", 25.0))
            utilization = float(data.get("cpu_utilization", energy.DEFAULT_UTILIZATION))
        except KeyError as exc:
            raise ValidationError(f"{exc.args[0]} is required") from None
        except (TypeError, ValueError):
            raise ValidationError("numeric fields must be numbers") from None

        if vcpu not in FARGATE_SIZES:
            raise ValidationError(f"vcpu must be one of {sorted(FARGATE_SIZES)}")
        if memory_gb not in FARGATE_SIZES[vcpu]:
            raise ValidationError(f"memory_gb {memory_gb:g} is not valid for {vcpu:g} vCPU on Fargate")
        if not 1 <= runtime <= MAX_RUNTIME_MINUTES:
            raise ValidationError(f"est_runtime_minutes must be between 1 and {MAX_RUNTIME_MINUTES}")
        if input_gb < 0:
            raise ValidationError("input_data_gb cannot be negative")
        if not 0 <= overhead <= 500:
            raise ValidationError("max_cost_overhead_pct must be between 0 and 500")
        if not 0.05 <= utilization <= 1.0:
            raise ValidationError("cpu_utilization must be between 0.05 and 1.0")

        if "deadline" not in data:
            raise ValidationError("deadline is required")
        try:
            deadline = parse_time(str(data["deadline"]))
        except ValueError as exc:
            raise ValidationError(f"invalid deadline: {exc}") from None
        if (deadline - now).total_seconds() < runtime * 60:
            raise ValidationError("deadline is too close to finish the job even if it starts now")

        allowed = data.get("allowed_regions")
        if allowed is not None:
            if not isinstance(allowed, list) or not allowed or not all(isinstance(r, str) for r in allowed):
                raise ValidationError("allowed_regions must be a non-empty list of region names")
            allowed = tuple(dict.fromkeys(allowed))

        return cls(
            job_id=uuid.uuid4().hex,
            name=name,
            command=tuple(command),
            vcpu=vcpu,
            memory_gb=memory_gb,
            est_runtime_minutes=runtime,
            deadline=deadline,
            data_region=str(data.get("data_region") or home_region),
            input_data_gb=input_gb,
            allowed_regions=allowed,
            max_cost_overhead_pct=overhead,
            cpu_utilization=utilization,
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["deadline"] = format_time(self.deadline)
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> JobSpec:
        data = json.loads(raw)
        data["deadline"] = parse_time(data["deadline"])
        data["command"] = tuple(data["command"])
        if data.get("allowed_regions") is not None:
            data["allowed_regions"] = tuple(data["allowed_regions"])
        return cls(**data)
