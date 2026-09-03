"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

import math

import pytest

from arka.scenario import ArraySpec, ArrayType, ModuleSpec, Mounting, Orientation, Site

# A 550 W commercial module with real datasheet dimensions, so the packing
# tests do not depend on the CEC area-to-dimensions approximation.
MODULE_550 = ModuleSpec(
    name="Test 550 W bifacial",
    width_m=1.134,
    height_m=2.278,
    stc_watts=550.0,
    technology="Mono-c-Si",
    source="test fixture",
)


def square_site(side_m: float = 40.0, lat: float = 51.5, lon: float = -0.12) -> Site:
    """A square roof of a known metric size, expressed in lon/lat.

    The conversion is deliberately crude — the local frame in `geometry` does
    the accurate work, and this only has to land the polygon in roughly the
    right place at roughly the right size.
    """
    half = side_m / 2.0
    dlat = half / 111_320.0
    dlon = half / (111_320.0 * math.cos(math.radians(lat)))
    return Site(
        name="Test roof",
        boundary=[
            (lon - dlon, lat - dlat),
            (lon + dlon, lat - dlat),
            (lon + dlon, lat + dlat),
            (lon - dlon, lat + dlat),
        ],
    )


@pytest.fixture
def site() -> Site:
    return square_site()


@pytest.fixture
def rooftop_spec() -> ArraySpec:
    return ArraySpec(
        array_type=ArrayType.ROOFTOP,
        mounting=Mounting.BALLASTED,
        module=MODULE_550,
        orientation=Orientation.PORTRAIT,
        tilt_deg=10.0,
        azimuth_deg=180.0,
        edge_setback_m=0.5,
        min_solar_altitude_deg=20.0,
    )


@pytest.fixture
def flat_generation() -> list[float]:
    """A crude 8760: 8 kWh in each of hours 9-15, nothing overnight."""
    day = [0.0] * 24
    for hour in range(9, 16):
        day[hour] = 8.0
    return day * 365


@pytest.fixture
def flat_load() -> list[float]:
    """1 kWh every hour, all year — 8760 kWh."""
    return [1.0] * 8760
