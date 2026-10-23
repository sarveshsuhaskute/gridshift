import io
import json
import urllib.error
from unittest import mock

import pytest

from gridshift import energy
from gridshift.carbon import ElectricityMapsProvider, StaticProvider, build_provider


def test_compute_kwh_matches_ccf_coefficients():
    # 1 vCPU at 50% = 2.12 W, 2 GB memory; 1 hour; PUE 1.135
    expected = (2.12 / 1000 + 2 * 0.000392) * 1.135
    assert energy.compute_kwh(1, 2, 1) == pytest.approx(expected)


def test_compute_kwh_rejects_bad_utilization():
    with pytest.raises(ValueError):
        energy.compute_kwh(1, 2, 1, utilization=1.5)


def _response(payload: dict):
    return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(payload).encode()))


def test_electricity_maps_parses_forecast():
    payload = {
        "zone": "SE-SE3",
        "forecast": [
            {"carbonIntensity": 30, "datetime": "2026-10-24T13:00:00.000Z"},
            {"carbonIntensity": 25, "datetime": "2026-10-24T12:00:00.000Z"},
            {"carbonIntensity": None, "datetime": "2026-10-24T14:00:00.000Z"},
        ],
    }
    with mock.patch("urllib.request.urlopen", return_value=_response(payload)) as urlopen:
        points = ElectricityMapsProvider("key").forecast("SE-SE3")
    assert [p.intensity for p in points] == [25.0, 30.0]
    assert urlopen.call_args.args[0].get_header("Auth-token") == "key"


def test_electricity_maps_falls_back_to_latest():
    forbidden = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
    latest = {"zone": "FR", "carbonIntensity": 52, "datetime": "2026-10-24T12:00:00.000Z"}
    with mock.patch("urllib.request.urlopen", side_effect=[forbidden, _response(latest)]):
        points = ElectricityMapsProvider("key").forecast("FR")
    assert [p.intensity for p in points] == [52.0]


def test_build_provider():
    assert isinstance(build_provider("static"), StaticProvider)
    with pytest.raises(ValueError):
        build_provider("electricitymaps", api_key="")
    with pytest.raises(ValueError):
        build_provider("nope")
