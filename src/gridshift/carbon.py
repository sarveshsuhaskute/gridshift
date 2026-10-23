"""Carbon-intensity data providers.

A provider returns a list of forecast points (gCO2e/kWh) for a grid zone. The planner treats
each point as valid until the next one starts. Anything it doesn't cover is filled with the
region's static average, so a failing provider degrades precision, never availability.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from gridshift.models import parse_time


@dataclass(frozen=True, order=True)
class ForecastPoint:
    start: datetime
    intensity: float


class ProviderError(RuntimeError):
    pass


class CarbonProvider(Protocol):
    name: str

    def forecast(self, zone: str) -> list[ForecastPoint]: ...


class StaticProvider:
    """Returns no live data, so the planner uses static regional averages."""

    name = "static"

    def forecast(self, zone: str) -> list[ForecastPoint]:
        return []


class ElectricityMapsProvider:
    """Electricity Maps API v3 (https://api.electricitymap.org).

    Uses the forecast endpoint. If the API plan doesn't include forecasts, it falls back to
    the latest measured value.
    """

    name = "electricitymaps"
    base_url = "https://api.electricitymap.org/v3"

    def __init__(self, api_key: str, timeout: float = 10.0):
        if not api_key:
            raise ValueError("Electricity Maps API key is required")
        self._api_key = api_key
        self._timeout = timeout

    def _get(self, path: str, zone: str) -> dict:
        url = f"{self.base_url}/{path}?{urllib.parse.urlencode({'zone': zone})}"
        request = urllib.request.Request(url, headers={"auth-token": self._api_key})  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 (fixed https URL)
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"{path} for {zone}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{path} for {zone}: {exc}") from exc

    def forecast(self, zone: str) -> list[ForecastPoint]:
        try:
            body = self._get("carbon-intensity/forecast", zone)
            raw = body.get("forecast", [])
        except ProviderError:
            raw = [self._get("carbon-intensity/latest", zone)]
        points = [
            ForecastPoint(parse_time(item["datetime"]), float(item["carbonIntensity"]))
            for item in raw
            if item.get("carbonIntensity") is not None and item.get("datetime")
        ]
        return sorted(points)


def build_provider(name: str, api_key: str | None = None) -> CarbonProvider:
    if name == "electricitymaps":
        return ElectricityMapsProvider(api_key or "")
    if name == "static":
        return StaticProvider()
    raise ValueError(f"unknown carbon provider {name!r}")
