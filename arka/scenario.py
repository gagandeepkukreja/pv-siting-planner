"""The single piece of application state.

`Scenario` is read and written by every screen. Nothing else holds state.
Every field is optional so a scenario can be built up screen by screen, and the
whole thing round-trips through JSON for save/load and for handing structured
context to the agent layer.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Sequence

# Longitude/latitude vertex, in that order, matching GeoJSON.
LonLat = tuple[float, float]
Ring = list[LonLat]

# Floats are compared with a tolerance, never with `==` (CLAUDE.md rule 5).
FLOAT_TOL = 1e-9


def isclose(a: float, b: float, tol: float = FLOAT_TOL) -> bool:
    """Tolerant float comparison. Use this instead of `==` on floats."""
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


class ArrayType(str, Enum):
    """The three siting geometries. Only ROOFTOP is implemented."""

    ROOFTOP = "ROOFTOP"
    GROUND_MOUNT = "GROUND_MOUNT"
    FLOATING = "FLOATING"


class Mounting(str, Enum):
    """How modules sit on the plane they are packed onto."""

    FLUSH = "FLUSH"          # parallel to the roof pitch, no self-shading rows
    BALLASTED = "BALLASTED"  # tilted rows on a flat plane, row pitch matters


class Market(str, Enum):
    """Markets with benchmark coverage in data/cost_benchmarks.csv."""

    UK = "UK"
    INDIA = "India"
    UAE = "UAE"


class Orientation(str, Enum):
    PORTRAIT = "PORTRAIT"
    LANDSCAPE = "LANDSCAPE"


@dataclass(frozen=True)
class Site:
    """Where the array goes. Geometry is stored in lon/lat (EPSG:4326)."""

    name: str = "Untitled site"
    market: Market = Market.UK
    boundary: Ring = field(default_factory=list)
    exclusions: list[Ring] = field(default_factory=list)

    @property
    def centre(self) -> LonLat | None:
        """Naive vertex mean — good enough to seed a map view or a PVGIS call."""
        if not self.boundary:
            return None
        lons = [v[0] for v in self.boundary]
        lats = [v[1] for v in self.boundary]
        return (sum(lons) / len(lons), sum(lats) / len(lats))


@dataclass(frozen=True)
class ModuleSpec:
    """A single PV module. Dimensions in metres, power at STC in watts."""

    name: str
    width_m: float
    height_m: float
    stc_watts: float
    technology: str = "Mono-c-Si"
    efficiency: float | None = None
    source: str = "pvlib CEC"

    @property
    def area_m2(self) -> float:
        return self.width_m * self.height_m

    def footprint(self, orientation: Orientation) -> tuple[float, float]:
        """(across-row width, along-slope length) for the given orientation."""
        if orientation is Orientation.PORTRAIT:
            return (self.width_m, self.height_m)
        return (self.height_m, self.width_m)


@dataclass(frozen=True)
class ArraySpec:
    """Everything the packing routine needs beyond the polygon itself."""

    array_type: ArrayType = ArrayType.ROOFTOP
    mounting: Mounting = Mounting.BALLASTED
    module: ModuleSpec | None = None
    orientation: Orientation = Orientation.PORTRAIT
    tilt_deg: float = 10.0
    azimuth_deg: float | None = None   # None -> derive from the polygon
    edge_setback_m: float = 0.5
    module_gap_m: float = 0.02
    row_gap_m: float | None = None     # None -> derive from tilt and shading limit
    min_solar_altitude_deg: float = 20.0
    exclusion_buffer_m: float = 0.0


@dataclass(frozen=True)
class ModulePlacement:
    """One packed module, in the site-local metric frame (metres from centroid)."""

    x_m: float
    y_m: float
    row: int
    col: int


@dataclass(frozen=True)
class Layout:
    """Output of `geometry.pack`. Areas in m2, capacity in kWp."""

    module_count: int
    kwp: float
    rows: int
    gross_area_m2: float
    usable_area_m2: float
    module_area_m2: float
    row_pitch_m: float
    azimuth_deg: float
    tilt_deg: float
    placements: list[ModulePlacement] = field(default_factory=list)
    polygon_m: list[tuple[float, float]] = field(default_factory=list)
    holes_m: list[list[tuple[float, float]]] = field(default_factory=list)

    @property
    def ground_coverage_ratio(self) -> float:
        if self.usable_area_m2 <= 0.0:
            return 0.0
        return self.module_area_m2 / self.usable_area_m2

    @property
    def area_per_kwp_m2(self) -> float:
        if self.kwp <= 0.0:
            return 0.0
        return self.usable_area_m2 / self.kwp


@dataclass(frozen=True)
class YieldResult:
    """PVGIS output, normalised. `hourly_kwh` is 8760 values starting 1 Jan 00:00."""

    annual_kwh: float
    monthly_kwh: list[float]
    hourly_kwh: list[float] = field(default_factory=list)
    kwp: float = 0.0
    source: str = "PVGIS 5.3 (PVGIS-SARAH3)"
    database: str = "PVGIS-SARAH3"
    year_range: tuple[int, int] | None = None
    system_loss_pct: float = 14.0

    @property
    def specific_yield_kwh_per_kwp(self) -> float:
        if self.kwp <= 0.0:
            return 0.0
        return self.annual_kwh / self.kwp


@dataclass(frozen=True)
class LoadProfile:
    """Site demand. 8760 hourly kWh values, or empty if no load is modelled."""

    hourly_kwh: list[float] = field(default_factory=list)
    label: str = "none"

    @property
    def annual_kwh(self) -> float:
        return float(sum(self.hourly_kwh))


@dataclass(frozen=True)
class BatterySpec:
    """Usable energy, not nameplate — the benchmark CSV prices usable kWh."""

    usable_kwh: float = 0.0
    power_kw: float = 0.0
    round_trip_efficiency: float = 0.90
    min_soc_fraction: float = 0.0
    initial_soc_fraction: float = 0.0
    #: Share of the pack held back for energy that would otherwise be clipped by
    #: an export limit. Only meaningful when an export limit applies.
    curtailment_reserve_fraction: float = 0.0


@dataclass(frozen=True)
class DispatchResult:
    """Energy balance over the modelled year. All figures kWh unless stated."""

    self_consumed_kwh: float
    exported_kwh: float
    imported_kwh: float
    charged_kwh: float
    discharged_kwh: float
    curtailed_kwh: float = 0.0
    cycles: float = 0.0
    soc_hourly_kwh: list[float] = field(default_factory=list)

    @property
    def generation_kwh(self) -> float:
        return self.self_consumed_kwh + self.exported_kwh + self.charged_kwh + self.curtailed_kwh

    @property
    def self_consumption_fraction(self) -> float:
        gen = self.generation_kwh
        if gen <= 0.0:
            return 0.0
        return (self.self_consumed_kwh + self.discharged_kwh) / gen


@dataclass(frozen=True)
class FinanceInputs:
    """Values the user must supply. The `gap`-tiered rows of the CSV land here.

    `discount_rate` and `tariff_escalation` are deliberately `None` by default:
    CLAUDE.md rule 3 forbids silently defaulting a `gap` row.
    """

    discount_rate: float | None = None
    tariff_escalation: float | None = None
    project_life_years: int | None = None
    degradation_pct_per_year: float | None = None
    import_tariff_per_kwh: float | None = None
    export_tariff_per_kwh: float | None = None
    grid_emission_factor_t_per_mwh: float | None = None
    capex_per_kwp: float | None = None
    battery_capex_per_kwh: float | None = None
    battery_usable_kwh: float = 0.0
    capex_lump_sums: dict[str, float] = field(default_factory=dict)
    opex_per_year: float = 0.0
    #: Calendar year (1-based) -> one-off cost, for mid-life replacements. The
    #: UAE benchmark rows call for an inverter replacement around year 12.
    year_costs: dict[int, float] = field(default_factory=dict)
    incentives_year_one: float = 0.0
    currency: str = "GBP"

    def missing(self) -> list[str]:
        """Names of required inputs still unset — the UI blocks on this list."""
        required = (
            "discount_rate",
            "tariff_escalation",
            "project_life_years",
            "degradation_pct_per_year",
            "import_tariff_per_kwh",
            "capex_per_kwp",
        )
        return [name for name in required if getattr(self, name) is None]


@dataclass(frozen=True)
class FinanceResult:
    npv: float
    irr: float | None
    simple_payback_years: float | None
    discounted_payback_years: float | None
    lcoe_per_kwh: float
    total_capex: float
    cashflows: list[float] = field(default_factory=list)
    discounted_cashflows: list[float] = field(default_factory=list)
    currency: str = "GBP"


@dataclass(frozen=True)
class MaccStep:
    """One marginal, additive increment on the abatement curve.

    Cost and abatement are *deltas* over everything to the left of this step,
    never a standalone scenario (CLAUDE.md, MACC section).
    """

    label: str
    delta_capex: float
    delta_tco2: float
    delta_opex_per_year: float = 0.0
    currency: str = "GBP"

    @property
    def cost_per_tco2(self) -> float | None:
        if isclose(self.delta_tco2, 0.0):
            return None
        return self.delta_capex / self.delta_tco2


@dataclass
class Scenario:
    """The app state. Mutable by design — screens write into it in place."""

    site: Site = field(default_factory=Site)
    array: ArraySpec = field(default_factory=ArraySpec)
    layout: Layout | None = None
    yield_result: YieldResult | None = None
    load: LoadProfile = field(default_factory=LoadProfile)
    battery: BatterySpec = field(default_factory=BatterySpec)
    dispatch: DispatchResult | None = None
    finance_inputs: FinanceInputs = field(default_factory=FinanceInputs)
    finance: FinanceResult | None = None
    macc: list[MaccStep] = field(default_factory=list)
    notes: str = ""

    # -- convenience -------------------------------------------------------

    @property
    def kwp(self) -> float:
        return self.layout.kwp if self.layout else 0.0

    def with_array(self, **changes: Any) -> "Scenario":
        self.array = replace(self.array, **changes)
        return self

    def invalidate_from(self, stage: str) -> None:
        """Drop downstream results when an upstream input changes.

        Stages, in order: site, array, yield, storage, finance.
        """
        order = ["site", "array", "yield", "storage", "finance"]
        if stage not in order:
            raise ValueError(f"unknown stage {stage!r}, expected one of {order}")
        idx = order.index(stage)
        if idx <= order.index("array"):
            self.layout = None
        if idx <= order.index("yield"):
            self.yield_result = None
        if idx <= order.index("storage"):
            self.dispatch = None
        self.finance = None
        self.macc = []

    # -- serialisation -----------------------------------------------------

    def to_dict(self, include_hourly: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data = _enums_to_values(data)
        if not include_hourly:
            if data.get("yield_result"):
                data["yield_result"]["hourly_kwh"] = []
            if data.get("dispatch"):
                data["dispatch"]["soc_hourly_kwh"] = []
            data["load"]["hourly_kwh"] = []
        return data

    def to_json(self, include_hourly: bool = True, indent: int = 2) -> str:
        return json.dumps(self.to_dict(include_hourly=include_hourly), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        site = data.get("site") or {}
        array = data.get("array") or {}
        module = array.get("module")
        scenario = cls(
            site=Site(
                name=site.get("name", "Untitled site"),
                market=Market(site.get("market", Market.UK.value)),
                boundary=[tuple(v) for v in site.get("boundary", [])],
                exclusions=[[tuple(v) for v in ring] for ring in site.get("exclusions", [])],
            ),
            array=ArraySpec(
                array_type=ArrayType(array.get("array_type", ArrayType.ROOFTOP.value)),
                mounting=Mounting(array.get("mounting", Mounting.BALLASTED.value)),
                module=ModuleSpec(**module) if module else None,
                orientation=Orientation(array.get("orientation", Orientation.PORTRAIT.value)),
                tilt_deg=array.get("tilt_deg", 10.0),
                azimuth_deg=array.get("azimuth_deg"),
                edge_setback_m=array.get("edge_setback_m", 0.5),
                module_gap_m=array.get("module_gap_m", 0.02),
                row_gap_m=array.get("row_gap_m"),
                min_solar_altitude_deg=array.get("min_solar_altitude_deg", 20.0),
                exclusion_buffer_m=array.get("exclusion_buffer_m", 0.0),
            ),
            load=LoadProfile(**(data.get("load") or {})),
            battery=BatterySpec(**(data.get("battery") or {})),
            finance_inputs=FinanceInputs(**(data.get("finance_inputs") or {})),
            notes=data.get("notes", ""),
        )
        if data.get("layout"):
            raw = dict(data["layout"])
            raw["placements"] = [ModulePlacement(**p) for p in raw.get("placements", [])]
            raw["polygon_m"] = [tuple(v) for v in raw.get("polygon_m", [])]
            raw["holes_m"] = [[tuple(v) for v in ring] for ring in raw.get("holes_m", [])]
            scenario.layout = Layout(**raw)
        if data.get("yield_result"):
            raw = dict(data["yield_result"])
            if raw.get("year_range"):
                raw["year_range"] = tuple(raw["year_range"])
            scenario.yield_result = YieldResult(**raw)
        if data.get("dispatch"):
            scenario.dispatch = DispatchResult(**data["dispatch"])
        if data.get("finance"):
            scenario.finance = FinanceResult(**data["finance"])
        scenario.macc = [MaccStep(**step) for step in data.get("macc", [])]
        return scenario

    @classmethod
    def from_json(cls, text: str) -> "Scenario":
        return cls.from_dict(json.loads(text))


def _enums_to_values(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _enums_to_values(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        seq: Iterable[Any] = obj
        return [_enums_to_values(v) for v in seq]
    return obj


def as_ring(points: Sequence[Sequence[float]]) -> Ring:
    """Coerce anything vaguely coordinate-shaped into a lon/lat ring."""
    return [(float(p[0]), float(p[1])) for p in points]
