import pytest

from gridshift.grid_zones import STATIC
from gridshift.regions import DEFAULT_TRANSFER_PER_GB, PROFILES, SPOT_DISCOUNT, get_profile


def test_every_region_has_prices_and_transfer_routes():
    assert set(PROFILES) == set(STATIC)
    for region, profile in PROFILES.items():
        assert profile.vcpu_hour > 0 and profile.gb_hour > 0
        assert set(profile.transfer_out_per_gb) == set(STATIC) - {region}


def test_spot_price_is_discounted_on_demand():
    profile = get_profile("us-east-1")
    assert profile.spot_vcpu_hour == pytest.approx(profile.vcpu_hour * (1 - SPOT_DISCOUNT))


def test_unknown_route_uses_conservative_transfer_price():
    assert get_profile("us-east-1").transfer_cost_per_gb("mars-east-1") == DEFAULT_TRANSFER_PER_GB


def test_unknown_region_has_helpful_error():
    with pytest.raises(KeyError, match="regions.py"):
        get_profile("mars-east-1")
