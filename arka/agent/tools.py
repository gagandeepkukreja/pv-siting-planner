"""Deterministic functions exposed to the model as tool declarations.

Every tool returns numbers computed by `pvlib`, the dispatch model or the
benchmark CSV. The model chooses which to call and with what arguments; it does
not get to supply a result. Argument values that are themselves quantities
(tilt, tariff, load) come from the user, and the UI shows them for confirmation.
"""

from __future__ import annotations

from typing import Any, Callable

from .. import benchmarks, dispatch, finance, geometry, modules, resource
from ..scenario import (
    ArraySpec,
    ArrayType,
    BatterySpec,
    FinanceInputs,
    Market,
    Mounting,
    Orientation,
    Site,
)


def polygon_metrics(boundary: list[list[float]], exclusions: list[list[list[float]]] | None = None,
                    edge_setback_m: float = 0.5) -> dict[str, Any]:
    """Area, usable area and implied azimuth for a drawn polygon."""
    site = Site(boundary=[(float(p[0]), float(p[1])) for p in boundary])
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    holes = [
        geometry.ring_to_polygon([(float(p[0]), float(p[1])) for p in ring], frame)
        for ring in (exclusions or [])
    ]
    usable = geometry.usable_area(poly, holes, edge_setback_m=edge_setback_m)
    return {
        "gross_area_m2": geometry.area_m2(poly),
        "usable_area_m2": geometry.area_m2(usable),
        "implied_azimuth_deg": geometry.azimuth_from_longest_edge(poly, frame.origin_lat),
        "centre_lon": frame.origin_lon,
        "centre_lat": frame.origin_lat,
    }


def pack_array(
    boundary: list[list[float]],
    module_name: str,
    tilt_deg: float,
    mounting: str = "BALLASTED",
    orientation: str = "PORTRAIT",
    azimuth_deg: float | None = None,
    edge_setback_m: float = 0.5,
    exclusions: list[list[list[float]]] | None = None,
    array_type: str = "ROOFTOP",
) -> dict[str, Any]:
    """Pack modules into a polygon and return module count and capacity."""
    site = Site(boundary=[(float(p[0]), float(p[1])) for p in boundary])
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    holes = [
        geometry.ring_to_polygon([(float(p[0]), float(p[1])) for p in ring], frame)
        for ring in (exclusions or [])
    ]
    spec = ArraySpec(
        array_type=ArrayType(array_type),
        mounting=Mounting(mounting),
        module=modules.get(module_name),
        orientation=Orientation(orientation),
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        edge_setback_m=edge_setback_m,
    )
    layout = geometry.pack(poly, spec.array_type, spec, latitude=frame.origin_lat, exclusions=holes)
    return {
        "module_count": layout.module_count,
        "kwp": layout.kwp,
        "rows": layout.rows,
        "usable_area_m2": layout.usable_area_m2,
        "row_pitch_m": layout.row_pitch_m,
        "azimuth_deg": layout.azimuth_deg,
        "ground_coverage_ratio": layout.ground_coverage_ratio,
        "area_per_kwp_m2": layout.area_per_kwp_m2,
    }


def annual_yield(lat: float, lon: float, tilt_deg: float, azimuth_deg: float,
                 kwp: float, mounting: str = "building") -> dict[str, Any]:
    """Annual and monthly generation from PVGIS for a sited array."""
    result = resource.hourly_yield(
        lat=lat, lon=lon, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg,
        kwp=kwp, mounting=mounting,
    )
    return {
        "annual_kwh": result.annual_kwh,
        "monthly_kwh": result.monthly_kwh,
        "specific_yield_kwh_per_kwp": result.specific_yield_kwh_per_kwp,
        "source": result.source,
    }


def battery_sweep(
    lat: float, lon: float, tilt_deg: float, azimuth_deg: float, kwp: float,
    annual_load_kwh: float, load_shape: str = "office",
    sizes_kwh: list[float] | None = None,
) -> dict[str, Any]:
    """Self-consumption against battery size, using a synthetic load shape."""
    generation = resource.hourly_yield(
        lat=lat, lon=lon, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg, kwp=kwp,
    )
    load = dispatch.synthetic_load_profile(annual_load_kwh, shape=load_shape)
    points = dispatch.sweep(
        generation.hourly_kwh, load.hourly_kwh, sizes_kwh or [0.0, 5.0, 10.0, 20.0, 50.0]
    )
    return {
        "load_profile": load.label,
        "points": [
            {
                "usable_kwh": p.usable_kwh,
                "self_consumption_fraction": p.self_consumption_fraction,
                "imported_kwh": p.result.imported_kwh,
                "exported_kwh": p.result.exported_kwh,
                "cycles": p.result.cycles,
            }
            for p in points
        ],
    }


def lookup_benchmark(parameter: str, market: str, band: str = "central") -> dict[str, Any]:
    """One cited benchmark from the CSV, with its source tier.

    A `gap`-tiered row returns `value: null` and the reason. The model must ask
    the user rather than filling it in.
    """
    table = benchmarks.load()
    row = table.find(parameter, market)
    try:
        value: float | None = row.value(band)
        reason = None
    except benchmarks.GapError as exc:
        value, reason = None, str(exc)
    return {
        "parameter": row.parameter,
        "market": row.market,
        "value": value,
        "unit": row.unit,
        "currency": row.currency,
        "source_tier": row.source_tier.value,
        "source": row.citation(),
        "source_url": row.source_url,
        "remarks": row.remarks,
        "gap_reason": reason,
    }


def list_gaps(market: str) -> dict[str, Any]:
    """Every benchmark the user must supply before a result can be produced."""
    table = benchmarks.load().for_market(market)
    return {
        "market": market,
        "gaps": [
            {"parameter": r.parameter, "unit": r.unit, "guidance": r.remarks}
            for r in table.gaps()
        ],
    }


def appraise(
    kwp: float, annual_generation_kwh: float, self_consumed_kwh: float, exported_kwh: float,
    capex_per_kwp: float, import_tariff_per_kwh: float, discount_rate_pct: float,
    tariff_escalation_pct: float, project_life_years: int, degradation_pct_per_year: float,
    export_tariff_per_kwh: float = 0.0, opex_per_year: float = 0.0,
    incentives_year_one: float = 0.0, currency: str = "GBP",
) -> dict[str, Any]:
    """NPV, IRR, payback and LCOE from an energy balance and the user's inputs."""
    balance = finance.EnergyBalance(
        kwp=kwp,
        generation_kwh=annual_generation_kwh,
        self_consumed_kwh=self_consumed_kwh,
        exported_kwh=exported_kwh,
    )
    inputs = FinanceInputs(
        discount_rate=discount_rate_pct / 100.0,
        tariff_escalation=tariff_escalation_pct,
        project_life_years=project_life_years,
        degradation_pct_per_year=degradation_pct_per_year,
        import_tariff_per_kwh=import_tariff_per_kwh,
        export_tariff_per_kwh=export_tariff_per_kwh,
        capex_per_kwp=capex_per_kwp,
        opex_per_year=opex_per_year,
        incentives_year_one=incentives_year_one,
        currency=currency,
    )
    result = finance.evaluate(balance, inputs)
    return {
        "npv": result.npv,
        "irr": result.irr,
        "simple_payback_years": result.simple_payback_years,
        "discounted_payback_years": result.discounted_payback_years,
        "lcoe_per_kwh": result.lcoe_per_kwh,
        "total_capex": result.total_capex,
        "currency": result.currency,
    }


def find_module(query: str, technology: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search the CEC module database."""
    names = modules.search(query, technology=technology, limit=limit)
    return {
        "matches": [
            {
                "name": n,
                "stc_watts": modules.get(n).stc_watts,
                "width_m": modules.get(n).width_m,
                "height_m": modules.get(n).height_m,
            }
            for n in names
        ]
    }


#: Name -> callable. `client.py` builds tool declarations from this registry,
#: so adding a deterministic function here is all it takes to expose it.
REGISTRY: dict[str, Callable[..., Any]] = {
    "polygon_metrics": polygon_metrics,
    "pack_array": pack_array,
    "annual_yield": annual_yield,
    "battery_sweep": battery_sweep,
    "lookup_benchmark": lookup_benchmark,
    "list_gaps": list_gaps,
    "appraise": appraise,
    "find_module": find_module,
}


def call(name: str, arguments: dict[str, Any]) -> Any:
    """Dispatch a model-requested tool call by name."""
    if name not in REGISTRY:
        raise KeyError(f"unknown tool {name!r}; available: {sorted(REGISTRY)}")
    return REGISTRY[name](**arguments)
