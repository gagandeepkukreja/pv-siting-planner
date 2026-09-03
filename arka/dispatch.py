"""Hourly battery dispatch and self-consumption.

Pure arithmetic over two 8760 series: generation and load. The strategy is
plain maximise-self-consumption — charge from surplus, discharge into deficit —
which is what an unmetered behind-the-meter battery does in all three target
markets. Tariff arbitrage would need a price series and is not modelled.

Round-trip efficiency is split evenly across charge and discharge, so a 90%
round trip is 94.87% each way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .scenario import BatterySpec, DispatchResult, LoadProfile

HOURS_PER_YEAR = 8760


class DispatchError(ValueError):
    """The inputs cannot be dispatched against each other."""


def simulate(
    generation_kwh: list[float],
    load_kwh: list[float],
    battery: BatterySpec,
    export_limit_kw: float | None = None,
    record_soc: bool = False,
) -> DispatchResult:
    """Run one year of dispatch. Both series must be the same length."""
    if len(generation_kwh) != len(load_kwh):
        raise DispatchError(
            f"generation has {len(generation_kwh)} steps but load has {len(load_kwh)}"
        )
    if not generation_kwh:
        raise DispatchError("nothing to dispatch: generation series is empty")
    if battery.round_trip_efficiency <= 0.0 or battery.round_trip_efficiency > 1.0:
        raise DispatchError("round-trip efficiency must be in (0, 1]")
    if not 0.0 <= battery.min_soc_fraction < 1.0:
        raise DispatchError("min_soc_fraction must be in [0, 1)")

    one_way = math.sqrt(battery.round_trip_efficiency)
    capacity = max(0.0, battery.usable_kwh)
    floor = capacity * battery.min_soc_fraction
    soc = min(max(capacity * battery.initial_soc_fraction, floor), capacity)
    step_power = battery.power_kw if battery.power_kw > 0.0 else capacity

    self_consumed = exported = imported = 0.0
    charged = discharged = curtailed = 0.0
    soc_trace: list[float] = []

    for gen, load in zip(generation_kwh, load_kwh):
        gen = max(0.0, gen)
        load = max(0.0, load)
        direct = min(gen, load)
        self_consumed += direct
        surplus = gen - direct
        deficit = load - direct

        if surplus > 0.0 and capacity > 0.0:
            headroom = (capacity - soc) / one_way
            take = min(surplus, step_power, max(0.0, headroom))
            soc += take * one_way
            charged += take
            surplus -= take
        elif deficit > 0.0 and capacity > 0.0:
            available = (soc - floor) * one_way
            give = min(deficit, step_power, max(0.0, available))
            soc -= give / one_way
            discharged += give
            deficit -= give

        if surplus > 0.0:
            if export_limit_kw is None:
                exported += surplus
            else:
                allowed = min(surplus, max(0.0, export_limit_kw))
                exported += allowed
                curtailed += surplus - allowed
        imported += deficit

        if record_soc:
            soc_trace.append(soc)

    return DispatchResult(
        self_consumed_kwh=self_consumed,
        exported_kwh=exported,
        imported_kwh=imported,
        charged_kwh=charged,
        discharged_kwh=discharged,
        curtailed_kwh=curtailed,
        cycles=(charged / capacity) if capacity > 0.0 else 0.0,
        soc_hourly_kwh=soc_trace,
    )


@dataclass(frozen=True)
class SweepPoint:
    """One point on the battery sizing curve."""

    usable_kwh: float
    result: DispatchResult

    @property
    def self_consumption_fraction(self) -> float:
        return self.result.self_consumption_fraction


def sweep(
    generation_kwh: list[float],
    load_kwh: list[float],
    sizes_kwh: list[float],
    power_ratio: float = 0.5,
    round_trip_efficiency: float = 0.90,
    min_soc_fraction: float = 0.0,
    export_limit_kw: float | None = None,
) -> list[SweepPoint]:
    """Dispatch at a range of battery sizes.

    `power_ratio` is inverter power as a fraction of usable energy — 0.5 means a
    2-hour battery, the common residential and small commercial shape.
    """
    points: list[SweepPoint] = []
    for size in sorted(sizes_kwh):
        battery = BatterySpec(
            usable_kwh=size,
            power_kw=size * power_ratio,
            round_trip_efficiency=round_trip_efficiency,
            min_soc_fraction=min_soc_fraction,
        )
        points.append(
            SweepPoint(
                usable_kwh=size,
                result=simulate(generation_kwh, load_kwh, battery, export_limit_kw=export_limit_kw),
            )
        )
    return points


def marginal_shift(points: list[SweepPoint]) -> list[tuple[float, float]]:
    """(size, extra kWh shifted off the grid vs the previous size).

    This is what feeds the additive battery tranches on the MACC — each bar is
    the increment over the size to its left, never a standalone scenario.
    """
    out: list[tuple[float, float]] = []
    previous = 0.0
    for point in points:
        shifted = point.result.discharged_kwh
        out.append((point.usable_kwh, shifted - previous))
        previous = shifted
    return out


# ---------------------------------------------------------------------------
# Placeholder load shapes
# ---------------------------------------------------------------------------
#
# These are synthetic normalised shapes, not measured data. They exist so the
# Storage screen has something to draw before the client's half-hourly data
# arrives, and every screen that uses one says so. Replace with real metering
# before any figure leaves the building.

_SHAPES: dict[str, tuple[float, ...]] = {
    # 24 relative weights, midnight to 23:00.
    "flat": tuple([1.0] * 24),
    "office": (
        0.30, 0.28, 0.27, 0.27, 0.28, 0.35, 0.55, 0.85,
        1.30, 1.55, 1.65, 1.70, 1.60, 1.65, 1.65, 1.55,
        1.30, 0.95, 0.70, 0.55, 0.45, 0.40, 0.35, 0.32,
    ),
    "residential": (
        0.55, 0.48, 0.45, 0.44, 0.45, 0.52, 0.75, 1.05,
        1.00, 0.85, 0.78, 0.75, 0.78, 0.75, 0.75, 0.85,
        1.15, 1.60, 1.85, 1.75, 1.50, 1.20, 0.90, 0.70,
    ),
    "industrial-two-shift": (
        0.75, 0.72, 0.70, 0.70, 0.75, 1.05, 1.45, 1.55,
        1.55, 1.55, 1.50, 1.35, 1.45, 1.55, 1.55, 1.50,
        1.30, 1.10, 0.95, 0.90, 0.85, 0.82, 0.80, 0.78,
    ),
}


def shapes() -> list[str]:
    return sorted(_SHAPES)


def synthetic_load_profile(
    annual_kwh: float,
    shape: str = "office",
    weekend_factor: float = 1.0,
) -> LoadProfile:
    """An 8760 load profile scaled to an annual total.

    Synthetic by construction. `weekend_factor` scales Saturday and Sunday;
    1 Jan of a non-leap reference year is treated as a Monday.
    """
    if annual_kwh <= 0.0:
        raise DispatchError("annual load must be positive")
    if shape not in _SHAPES:
        raise DispatchError(f"unknown load shape {shape!r}; available: {shapes()}")
    weights = _SHAPES[shape]
    raw: list[float] = []
    for day in range(365):
        day_factor = weekend_factor if day % 7 in (5, 6) else 1.0
        raw.extend(w * day_factor for w in weights)
    total = sum(raw)
    scale = annual_kwh / total
    return LoadProfile(
        hourly_kwh=[value * scale for value in raw],
        label=f"synthetic:{shape} ({annual_kwh:,.0f} kWh/yr, weekend x{weekend_factor:g})",
    )
