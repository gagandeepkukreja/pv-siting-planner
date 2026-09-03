"""PVGIS parsing, caching and the azimuth convention. No network calls."""

from __future__ import annotations

import json
import math

import pytest

from arka import resource


def synthetic_payload(years=(2020, 2021, 2022), peak_watts=800.0) -> dict:
    """A PVGIS-shaped response: hourly P in watts for a 1 kWp system."""
    records = []
    for year in years:
        # Nudge each year so one is clearly closest to the mean.
        scale = 1.0 + 0.05 * (year - years[0] - 1)
        for day in range(365):
            for hour in range(24):
                daylight = max(0.0, math.sin(math.pi * (hour - 6) / 12.0))
                seasonal = 0.6 + 0.4 * math.cos(2 * math.pi * (day - 172) / 365.0)
                records.append(
                    {
                        "time": f"{year}{_stamp(day)}:{hour:02d}11",
                        "P": peak_watts * daylight * seasonal * scale,
                    }
                )
    return {"inputs": {}, "outputs": {"hourly": records}, "meta": {}}


def _stamp(day_index: int) -> str:
    lengths = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    remaining = day_index
    for month, length in enumerate(lengths, start=1):
        if remaining < length:
            return f"{month:02d}{remaining + 1:02d}"
        remaining -= length
    raise AssertionError("day index out of range")


# -- azimuth convention -----------------------------------------------------


@pytest.mark.parametrize(
    "pvlib_azimuth, pvgis_aspect",
    [(180.0, 0.0), (90.0, -90.0), (270.0, 90.0), (135.0, -45.0), (225.0, 45.0)],
)
def test_azimuth_converts_to_the_pvgis_convention(pvlib_azimuth, pvgis_aspect):
    assert resource.to_pvgis_aspect(pvlib_azimuth) == pytest.approx(pvgis_aspect)


def test_the_aspect_conversion_round_trips():
    for azimuth in (0.0, 45.0, 90.0, 180.0, 270.0, 359.0):
        back = resource.from_pvgis_aspect(resource.to_pvgis_aspect(azimuth))
        assert back % 360.0 == pytest.approx(azimuth % 360.0)


def test_request_params_use_the_south_referenced_aspect():
    request = resource.PVGISRequest(lat=51.5, lon=-0.12, tilt_deg=10.0, azimuth_deg=180.0)
    params = request.params()
    assert params["aspect"] == pytest.approx(0.0)
    assert params["angle"] == pytest.approx(10.0)
    assert params["raddatabase"] == "PVGIS-SARAH3"
    # Always requested at 1 kWp so one cache entry serves every array size.
    assert params["peakpower"] == pytest.approx(1.0)


# -- cache keys -------------------------------------------------------------


def test_cache_key_is_stable_for_the_same_request():
    a = resource.PVGISRequest(51.5, -0.12, 10.0, 180.0)
    b = resource.PVGISRequest(51.5, -0.12, 10.0, 180.0)
    assert a.cache_key() == b.cache_key()


def test_cache_key_changes_with_orientation():
    a = resource.PVGISRequest(51.5, -0.12, 10.0, 180.0)
    b = resource.PVGISRequest(51.5, -0.12, 30.0, 180.0)
    c = resource.PVGISRequest(51.5, -0.12, 10.0, 150.0)
    assert len({a.cache_key(), b.cache_key(), c.cache_key()}) == 3


def test_cache_is_read_instead_of_the_network(tmp_path):
    request = resource.PVGISRequest(51.5, -0.12, 10.0, 180.0)
    cache_file = tmp_path / f"{request.cache_key()}.json"
    payload = {"outputs": {"hourly": []}}
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    # No session is passed; if this hit the network it would fail, not pass.
    assert resource.fetch_raw(request, cache_dir=tmp_path) == payload


# -- parsing ----------------------------------------------------------------


def test_parse_returns_a_full_year():
    result = resource.parse_hourly(synthetic_payload(), kwp=10.0)
    assert len(result.hourly_kwh) == resource.HOURS_PER_YEAR
    assert len(result.monthly_kwh) == 12


def test_output_scales_linearly_with_capacity():
    payload = synthetic_payload()
    small = resource.parse_hourly(payload, kwp=1.0)
    large = resource.parse_hourly(payload, kwp=250.0)
    assert large.annual_kwh == pytest.approx(small.annual_kwh * 250.0)


def test_monthly_totals_sum_to_the_annual_figure():
    result = resource.parse_hourly(synthetic_payload(), kwp=10.0)
    assert sum(result.monthly_kwh) == pytest.approx(result.annual_kwh)


def test_specific_yield_is_annual_over_capacity():
    result = resource.parse_hourly(synthetic_payload(), kwp=50.0)
    assert result.specific_yield_kwh_per_kwp == pytest.approx(result.annual_kwh / 50.0)


def test_a_representative_year_is_chosen_not_an_average():
    result = resource.parse_hourly(synthetic_payload(years=(2020, 2021, 2022)), kwp=1.0)
    assert result.year_range == (2020, 2022)
    assert "representative year" in result.source


def test_the_leap_day_is_dropped():
    payload = synthetic_payload(years=(2020,))
    payload["outputs"]["hourly"].extend(
        {"time": f"20200229:{hour:02d}11", "P": 100.0} for hour in range(24)
    )
    result = resource.parse_hourly(payload, kwp=1.0)
    assert len(result.hourly_kwh) == resource.HOURS_PER_YEAR


def test_an_empty_series_is_rejected():
    with pytest.raises(resource.PVGISError):
        resource.parse_hourly({"outputs": {"hourly": []}}, kwp=1.0)


def test_a_malformed_payload_is_rejected():
    with pytest.raises(resource.PVGISError):
        resource.parse_hourly({"nothing": "useful"}, kwp=1.0)


def test_an_incomplete_year_is_rejected():
    payload = synthetic_payload(years=(2020,))
    payload["outputs"]["hourly"] = payload["outputs"]["hourly"][:100]
    with pytest.raises(resource.PVGISError):
        resource.parse_hourly(payload, kwp=1.0)


def test_zero_capacity_is_rejected_before_any_request():
    with pytest.raises(ValueError):
        resource.hourly_yield(51.5, -0.12, 10.0, 180.0, kwp=0.0)


# -- reshaping --------------------------------------------------------------


def test_monthly_totals_split_a_flat_year_by_month_length():
    monthly = resource.monthly_totals([1.0] * 8760)
    assert monthly[0] == pytest.approx(31 * 24)
    assert monthly[1] == pytest.approx(28 * 24)
    assert sum(monthly) == pytest.approx(8760.0)


def test_heatmap_is_24_by_365():
    grid = resource.hourly_to_heatmap(list(range(8760)))
    assert len(grid) == 24
    assert all(len(row) == 365 for row in grid)
    assert grid[0][0] == 0
    assert grid[1][0] == 1


def test_reshaping_the_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        resource.hourly_to_heatmap([0.0] * 100)
    with pytest.raises(ValueError):
        resource.monthly_totals([0.0] * 100)


# -- sanity checks ----------------------------------------------------------


def test_specific_yield_inside_the_band_passes_quietly():
    result = resource.parse_hourly(synthetic_payload(), kwp=1.0)
    band = (result.specific_yield_kwh_per_kwp * 0.9, result.specific_yield_kwh_per_kwp * 1.1)
    assert resource.sanity_check_specific_yield(result, *band) is None


def test_specific_yield_below_the_band_is_flagged():
    result = resource.parse_hourly(synthetic_payload(), kwp=1.0)
    message = resource.sanity_check_specific_yield(result, 5000.0, 6000.0)
    assert message and "below" in message


def test_specific_yield_above_the_band_is_flagged():
    result = resource.parse_hourly(synthetic_payload(), kwp=1.0)
    message = resource.sanity_check_specific_yield(result, 10.0, 20.0)
    assert message and "above" in message
