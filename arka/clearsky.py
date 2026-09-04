"""A physical ceiling on yield, computed offline with pvlib.

PVGIS is the source of truth for yield. This module exists to catch the case
where what came back from PVGIS has been parsed, scaled or timestamped wrongly,
and it does so without any network access: a clear-sky sky is the brightest sky
a site can have, so the output a PV array would give under one is a hard upper
bound on what any real year can deliver.

The bound is made deliberately loose in the safe direction. Cells are held at
25 degrees so there is no thermal derate, and no system losses are applied. That
means a correct PVGIS series will sit comfortably below it — in the UK at around
half, in the Gulf at around four-fifths — and one that has been scaled by a
thousand, doubled, or mis-parsed lands above it and is caught.

Nothing here is a yield figure for display. It is a validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pvlib

from .resource import HOURS_PER_YEAR, monthly_totals
from .scenario import YieldResult

#: pvlib's clear-sky models want a full year of hourly timestamps. Any
#: non-leap year gives 8760; the choice does not affect the ceiling.
REFERENCE_YEAR = 2023

#: Cell temperature held constant so no thermal derate applies. Combined with
#: zero losses, this keeps the ceiling above any real output.
CELL_TEMPERATURE_C = 25.0

#: PVWatts's default temperature coefficient of power, per degree C. Irrelevant
#: at the reference temperature, retained for anyone lowering the cell
#: temperature to tighten the bound.
TEMPERATURE_COEFFICIENT = -0.004

#: A real annual yield below this fraction of the clear-sky ceiling is not
#: physically impossible, but no site in the three target markets comes close,
#: so it almost certainly means a unit or scaling error in the other direction.
IMPLAUSIBLY_LOW_RATIO = 0.25

#: Hour-by-hour comparison is not used: at dawn and dusk the clear-sky value is
#: near zero and any timing offset — PVGIS stamps its hours at :11 past —
#: registers as exceedance. Daily energy is immune to alignment within the day.
#: Cloud enhancement can push a single hour past clear sky but cannot sustain it
#: across a day, so a day is allowed only this much over its ceiling.
DAILY_EXCEEDANCE_FACTOR = 1.05
#: ...and on no more than this share of days.
DAILY_EXCEEDANCE_TOLERANCE = 0.02

#: Energy delivered while the clear-sky model says the sun is down, as a share
#: of the annual total. The two things this must separate are far apart: a
#: timestamp-convention offset (PVGIS stamps :11 past the hour) leaks a sliver
#: at sunrise and sunset, well under one percent; a wrong time zone, the error
#: worth catching, is a full hour or more and puts several percent at night.
#: Two percent sits between them with margin both ways. A false refusal of
#: correct data would cost more trust than a missed sliver.
NIGHT_ENERGY_TOLERANCE = 0.02

#: Solar noon is fixed for a site, so the energy-weighted centre of the
#: generating day is too. This catches the one-hour shift the night test
#: cannot: a wrong time zone moves the centre by a whole hour (measured 1.00 h),
#: the :11 stamping convention by minutes, and even afternoons 30% cloudier
#: than mornings every day of the year by 0.37 h. 0.6 h sits between with
#: about 1.7x margin on each side.
SOLAR_NOON_TOLERANCE_H = 0.6


class ClearSkyError(RuntimeError):
    """The ceiling could not be computed."""


@dataclass(frozen=True)
class ClearSkyCeiling:
    """Upper-bound output for an array under a year of clear skies."""

    annual_kwh: float
    monthly_kwh: list[float]
    hourly_kwh: list[float] = field(default_factory=list)
    kwp: float = 0.0
    tilt_deg: float = 0.0
    azimuth_deg: float = 0.0

    @property
    def specific_yield_kwh_per_kwp(self) -> float:
        return self.annual_kwh / self.kwp if self.kwp > 0.0 else 0.0


def ceiling(
    lat: float,
    lon: float,
    tilt_deg: float,
    azimuth_deg: float,
    kwp: float,
    cell_temperature_c: float = CELL_TEMPERATURE_C,
) -> ClearSkyCeiling:
    """Clear-sky output for an array, hour by hour.

    Azimuth follows the pvlib convention used everywhere in this codebase:
    degrees clockwise from north, 180 = south.
    """
    if kwp <= 0.0:
        raise ClearSkyError("kwp must be positive")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ClearSkyError(f"({lat}, {lon}) is not a coordinate")

    location = pvlib.location.Location(lat, lon, tz="UTC")
    times = pd.date_range(
        f"{REFERENCE_YEAR}-01-01", f"{REFERENCE_YEAR}-12-31 23:00", freq="h", tz="UTC"
    )
    try:
        sky = location.get_clearsky(times)              # Ineichen, bundled turbidity
        position = location.get_solarposition(times)
    except Exception as exc:  # pragma: no cover - depends on pvlib's data files
        raise ClearSkyError(f"pvlib could not compute clear-sky irradiance: {exc}") from exc

    plane = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt_deg,
        surface_azimuth=azimuth_deg,
        solar_zenith=position["apparent_zenith"],
        solar_azimuth=position["azimuth"],
        dni=sky["dni"],
        ghi=sky["ghi"],
        dhi=sky["dhi"],
    )
    dc_watts = pvlib.pvsystem.pvwatts_dc(
        plane["poa_global"].fillna(0.0),
        cell_temperature_c,
        pdc0=kwp * 1000.0,
        gamma_pdc=TEMPERATURE_COEFFICIENT,
    )
    hourly = [max(0.0, float(w)) / 1000.0 for w in dc_watts]
    if len(hourly) != HOURS_PER_YEAR:
        raise ClearSkyError(f"expected {HOURS_PER_YEAR} hours, built {len(hourly)}")
    return ClearSkyCeiling(
        annual_kwh=float(sum(hourly)),
        monthly_kwh=monthly_totals(hourly),
        hourly_kwh=hourly,
        kwp=kwp,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
    )


@dataclass(frozen=True)
class Verdict:
    """What the ceiling says about a yield series."""

    ratio: float                   # real annual / clear-sky annual
    days_over_ceiling: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check(result: YieldResult, bound: ClearSkyCeiling) -> Verdict:
    """Compare a yield series against its physical ceiling.

    Five tests. The annual total may not exceed the ceiling at all, and may not
    be implausibly far below it either, since that points at a scaling error the
    other way. With hourly data, no day may deliver materially more than its
    clear-sky day; nothing meaningful may be delivered while the sun is down;
    and the energy-weighted centre of the generating day must sit at the site's
    solar noon. Nothing is compared hour against hour: that is fragile at dawn
    and dusk and would refuse correct data over a timestamp convention.
    """
    problems: list[str] = []
    if bound.annual_kwh <= 0.0:
        raise ClearSkyError("ceiling has no output; check latitude and orientation")

    ratio = result.annual_kwh / bound.annual_kwh
    if ratio > 1.0:
        problems.append(
            f"annual yield {result.annual_kwh:,.0f} kWh exceeds the clear-sky ceiling of "
            f"{bound.annual_kwh:,.0f} kWh ({ratio:.2f}x). No real year can do that — the "
            "PVGIS response has been mis-parsed or mis-scaled."
        )
    elif ratio < IMPLAUSIBLY_LOW_RATIO:
        problems.append(
            f"annual yield is only {ratio:.0%} of the clear-sky ceiling. Even a very cloudy "
            "site delivers far more than that; check units and the kWp scaling."
        )

    over = 0
    if result.hourly_kwh and bound.hourly_kwh:
        if len(result.hourly_kwh) != len(bound.hourly_kwh):
            problems.append(
                f"yield series has {len(result.hourly_kwh)} hours, ceiling has "
                f"{len(bound.hourly_kwh)}; the series is not a full year"
            )
        else:
            over = _days_over_ceiling(result.hourly_kwh, bound.hourly_kwh)
            days = len(result.hourly_kwh) // 24
            if over / days > DAILY_EXCEEDANCE_TOLERANCE:
                problems.append(
                    f"{over} days ({over / days:.1%}) deliver more energy than a clear-sky "
                    "day. No sky can do that across a whole day; the series is mis-scaled."
                )
            night = _night_energy(result.hourly_kwh, bound.hourly_kwh)
            if result.annual_kwh > 0.0 and night / result.annual_kwh > NIGHT_ENERGY_TOLERANCE:
                problems.append(
                    f"{night:,.0f} kWh ({night / result.annual_kwh:.1%}) is delivered while "
                    "the sun is down. The series is shifted in time, or its timestamps are "
                    "in the wrong zone."
                )
            drift = _solar_noon_drift_h(result.hourly_kwh, bound.hourly_kwh)
            if drift is not None and abs(drift) > SOLAR_NOON_TOLERANCE_H:
                problems.append(
                    f"the generating day is centred {abs(drift):.1f} h "
                    f"{'later' if drift > 0 else 'earlier'} than solar noon. That is a time "
                    "zone error — most likely local time where UTC was expected, or the "
                    "reverse."
                )
    return Verdict(ratio=ratio, days_over_ceiling=over, problems=problems)


def _days_over_ceiling(real: list[float], sky: list[float]) -> int:
    """Days whose real energy exceeds the clear-sky day by more than the allowance."""
    over = 0
    for start in range(0, len(real) - 23, 24):
        real_day = sum(real[start:start + 24])
        sky_day = sum(sky[start:start + 24])
        if real_day > sky_day * DAILY_EXCEEDANCE_FACTOR and real_day > 0.0:
            over += 1
    return over


def _solar_noon_drift_h(real: list[float], sky: list[float]) -> float | None:
    """Hours between the series' energy-weighted mean hour and the clear-sky one."""
    def centroid(series: list[float]) -> float | None:
        total = sum(series)
        if total <= 0.0:
            return None
        return sum(e * (i % 24) for i, e in enumerate(series)) / total
    real_c, sky_c = centroid(real), centroid(sky)
    if real_c is None or sky_c is None:
        return None
    return real_c - sky_c


def _night_energy(real: list[float], sky: list[float]) -> float:
    """Energy the series delivers in hours the clear-sky model puts at zero.

    An exact zero from the clear-sky model means the sun is below the horizon,
    not merely low, so the twilight margin is excluded by construction.
    """
    return sum(r for r, s in zip(real, sky) if s == 0.0 and r > 0.0)
