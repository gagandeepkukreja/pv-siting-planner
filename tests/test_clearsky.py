"""The clear-sky ceiling and the checks built on it. No network."""

from __future__ import annotations

import pytest

from arka import clearsky
from arka.scenario import YieldResult


@pytest.fixture(scope="module")
def london():
    return clearsky.ceiling(51.5, -0.12, 10.0, 180.0, kwp=100.0)


@pytest.fixture(scope="module")
def dubai():
    return clearsky.ceiling(25.20, 55.27, 10.0, 180.0, kwp=100.0)


def series(annual_kwh: float, bound: clearsky.ClearSkyCeiling, scale: float | None = None,
           hourly: bool = True) -> YieldResult:
    """A yield shaped like the ceiling, scaled to a chosen annual total."""
    factor = annual_kwh / bound.annual_kwh if scale is None else scale
    return YieldResult(
        annual_kwh=annual_kwh,
        monthly_kwh=[m * factor for m in bound.monthly_kwh],
        hourly_kwh=[h * factor for h in bound.hourly_kwh] if hourly else [],
        kwp=bound.kwp,
    )


# -- the ceiling itself ------------------------------------------------------


def test_a_full_year_of_hours(london):
    assert len(london.hourly_kwh) == 8760
    assert len(london.monthly_kwh) == 12
    assert sum(london.monthly_kwh) == pytest.approx(london.annual_kwh)


def test_nothing_is_generated_at_night(london):
    assert london.hourly_kwh[0] == pytest.approx(0.0)      # 1 Jan, midnight UTC
    assert min(london.hourly_kwh) >= 0.0


def test_the_ceiling_scales_linearly_with_capacity():
    small = clearsky.ceiling(51.5, -0.12, 10.0, 180.0, kwp=1.0)
    large = clearsky.ceiling(51.5, -0.12, 10.0, 180.0, kwp=250.0)
    assert large.annual_kwh == pytest.approx(small.annual_kwh * 250.0, rel=1e-6)


def test_sunnier_latitudes_have_higher_ceilings(london, dubai):
    assert dubai.specific_yield_kwh_per_kwp > london.specific_yield_kwh_per_kwp


def test_the_ceiling_is_above_any_plausible_real_yield(london, dubai):
    """Real UK sits near 900-1000 kWh/kWp; the Gulf near 1800-1900."""
    assert london.specific_yield_kwh_per_kwp > 1_200.0
    assert dubai.specific_yield_kwh_per_kwp > 2_000.0


def test_facing_the_equator_beats_facing_away():
    south = clearsky.ceiling(51.5, -0.12, 30.0, 180.0, kwp=10.0).annual_kwh
    north = clearsky.ceiling(51.5, -0.12, 30.0, 0.0, kwp=10.0).annual_kwh
    assert south > north * 1.5


def test_summer_beats_winter_in_the_north(london):
    assert london.monthly_kwh[5] > london.monthly_kwh[11] * 2.0   # June vs December


def test_zero_capacity_is_rejected():
    with pytest.raises(clearsky.ClearSkyError):
        clearsky.ceiling(51.5, -0.12, 10.0, 180.0, kwp=0.0)


def test_nonsense_coordinates_are_rejected():
    with pytest.raises(clearsky.ClearSkyError):
        clearsky.ceiling(95.0, 0.0, 10.0, 180.0, kwp=10.0)


# -- the checks --------------------------------------------------------------


def test_a_realistic_series_passes(london):
    real = series(london.annual_kwh * 0.55, london)     # a normal UK year
    verdict = clearsky.check(real, london)
    assert verdict.ok, verdict.problems
    assert 0.5 < verdict.ratio < 0.6


def test_a_series_scaled_by_a_thousand_is_caught(london):
    """The classic W-for-kW mistake."""
    broken = series(london.annual_kwh * 550.0, london)
    verdict = clearsky.check(broken, london)
    assert not verdict.ok
    assert "exceeds the clear-sky ceiling" in verdict.problems[0]


def test_a_doubled_series_is_caught(london):
    broken = series(london.annual_kwh * 1.1, london)
    assert not clearsky.check(broken, london).ok


def test_a_series_scaled_down_by_a_thousand_is_caught(london):
    tiny = series(london.annual_kwh * 0.00055, london)
    verdict = clearsky.check(tiny, london)
    assert not verdict.ok
    assert "clear-sky ceiling" in verdict.problems[0] and "%" in verdict.problems[0]


def test_a_time_shifted_series_is_caught(london):
    """Same annual total, hours rotated by twelve: generation at night, none at noon."""
    shifted = london.hourly_kwh[12:] + london.hourly_kwh[:12]
    real = YieldResult(
        annual_kwh=london.annual_kwh * 0.55,
        monthly_kwh=[m * 0.55 for m in london.monthly_kwh],
        hourly_kwh=[h * 0.55 for h in shifted],
        kwp=100.0,
    )
    verdict = clearsky.check(real, london)
    assert not verdict.ok
    assert "sun is down" in " ".join(verdict.problems)


def test_bright_hours_within_a_day_are_tolerated(london):
    """Cloud enhancement pushes single hours past clear sky. Daily energy is
    what is bounded, so a bright hour in an otherwise cloudy day is fine."""
    hourly = [h * 0.55 for h in london.hourly_kwh]
    # One bright hour per day, at noon, on 200 days: 20% over the clear-sky
    # hour inside an otherwise ordinary day.
    for day in range(200):
        noon = day * 24 + 12
        hourly[noon] = london.hourly_kwh[noon] * 1.2
    real = YieldResult(annual_kwh=sum(hourly), monthly_kwh=[0.0] * 12,
                       hourly_kwh=hourly, kwp=100.0)
    verdict = clearsky.check(real, london)
    assert verdict.ok, verdict.problems


def test_a_one_hour_time_zone_error_is_caught(london):
    """The BST-for-GMT mistake. Too small for the night test (it leaks only
    0.5% of energy past sunset) but it moves solar noon by a full hour."""
    rotated = london.hourly_kwh[1:] + london.hourly_kwh[:1]
    real = YieldResult(annual_kwh=london.annual_kwh * 0.55, monthly_kwh=[0.0] * 12,
                       hourly_kwh=[h * 0.55 for h in rotated], kwp=100.0)
    verdict = clearsky.check(real, london)
    assert not verdict.ok
    assert "solar noon" in " ".join(verdict.problems)


def test_the_direction_of_a_shift_is_reported(london):
    later = london.hourly_kwh[-1:] + london.hourly_kwh[:-1]      # everything one hour later
    real = YieldResult(annual_kwh=london.annual_kwh * 0.55, monthly_kwh=[0.0] * 12,
                       hourly_kwh=[h * 0.55 for h in later], kwp=100.0)
    assert "later than solar noon" in " ".join(clearsky.check(real, london).problems)


def test_cloudier_afternoons_do_not_look_like_a_time_zone_error(london):
    """Real weather is asymmetric — convective afternoons are cloudier in many
    climates. Even 30% cloudier every afternoon of the year moves the centre by
    under 0.4 h, and must not be refused."""
    hourly = [h * 0.55 * (0.7 if (i % 24) > 12 else 1.0) for i, h in enumerate(london.hourly_kwh)]
    real = YieldResult(annual_kwh=sum(hourly), monthly_kwh=[0.0] * 12,
                       hourly_kwh=hourly, kwp=100.0)
    verdict = clearsky.check(real, london)
    assert verdict.ok, verdict.problems


def test_a_crude_but_plausible_series_is_accepted(london):
    """Regression: the parser's own synthetic fixture — a sine-of-hour shape with
    no latitude in it — was refused by the hour-by-hour check on twilight
    margins. Its daily totals and night hours are fine, so it must pass."""
    import json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, "tests")
    from test_resource import synthetic_payload
    from arka import resource
    cache = Path(tempfile.mkdtemp())
    request = resource.PVGISRequest(51.5, -0.12, 10.0, 180.0)
    (cache / f"{request.cache_key()}.json").write_text(json.dumps(synthetic_payload(peak_watts=520.0)))
    real = resource.hourly_yield(lat=51.5, lon=-0.12, tilt_deg=10.0, azimuth_deg=180.0,
                                 kwp=100.0, cache_dir=cache)
    verdict = clearsky.check(real, london)
    assert verdict.ok, verdict.problems


def test_a_day_delivering_more_than_clear_sky_is_caught(london):
    """A whole day over the ceiling cannot be cloud enhancement."""
    hourly = [h * 0.55 for h in london.hourly_kwh]
    days_broken = 0
    for start in range(0, 8760, 24):
        if days_broken >= 12 and sum(london.hourly_kwh[start:start + 24]) > 0.0:
            break
        if sum(london.hourly_kwh[start:start + 24]) > 0.0:
            for i in range(start, start + 24):
                hourly[i] = london.hourly_kwh[i] * 1.5
            days_broken += 1
    real = YieldResult(annual_kwh=sum(hourly), monthly_kwh=[0.0] * 12,
                       hourly_kwh=hourly, kwp=100.0)
    verdict = clearsky.check(real, london)
    assert not verdict.ok
    assert "clear-sky day" in " ".join(verdict.problems)
    assert verdict.days_over_ceiling == 12


def test_a_short_series_is_caught(london):
    real = YieldResult(annual_kwh=london.annual_kwh * 0.55, monthly_kwh=[0.0] * 12,
                       hourly_kwh=[1.0] * 100, kwp=100.0)
    assert "not a full year" in clearsky.check(real, london).problems[0]


def test_annual_only_series_is_still_checked(london):
    """No hourly data means no hourly test, but the annual bound still applies."""
    assert clearsky.check(series(london.annual_kwh * 0.5, london, hourly=False), london).ok
    assert not clearsky.check(series(london.annual_kwh * 2.0, london, hourly=False), london).ok


def test_the_verdict_reports_its_ratio(london):
    verdict = clearsky.check(series(london.annual_kwh * 0.5, london), london)
    assert verdict.ratio == pytest.approx(0.5)
