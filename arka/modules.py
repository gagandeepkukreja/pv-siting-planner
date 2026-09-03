"""PV module lookup, backed by the CEC database that ships with pvlib.

The CEC database gives cell area (`A_c`) and STC power but not physical
dimensions, so the packing routine needs width and height derived from area.
We pick a plausible frame width from the cell count and solve for length. That
approximation is flagged on the module picker and can be overridden by the
user — a wrong module length shifts row pitch, which shifts capacity.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable

from .scenario import ModuleSpec

# Frame widths, in metres, by series-cell count. Standard glass sizes: 60/66-cell
# modules are ~1.0 m wide, 72-cell and larger half-cut formats ~1.13 m.
_WIDTH_BY_CELLS: tuple[tuple[int, float], ...] = (
    (36, 0.67),
    (48, 0.80),
    (60, 0.99),
    (66, 1.03),
    (72, 1.13),
    (96, 1.05),
    (120, 1.13),
    (132, 1.13),
    (144, 1.13),
)
_DEFAULT_WIDTH_M = 1.05

# Glass-to-frame overhead: the CEC `A_c` is aperture cell area, a little under
# the outer frame footprint.
_FRAME_OVERHEAD = 1.06


class ModuleNotFound(LookupError):
    """No CEC module matched the query."""


@lru_cache(maxsize=1)
def _cec_table():
    """The raw CEC module DataFrame. Ships with pvlib; no network needed."""
    from pvlib.pvsystem import retrieve_sam

    return retrieve_sam("CECMod")


def frame_width_m(cells_in_series: int | None) -> float:
    """Nominal frame width for a cell count, nearest bin."""
    if not cells_in_series or cells_in_series <= 0:
        return _DEFAULT_WIDTH_M
    return min(_WIDTH_BY_CELLS, key=lambda pair: abs(pair[0] - cells_in_series))[1]


def dimensions_from_area(area_m2: float, cells_in_series: int | None) -> tuple[float, float]:
    """(width_m, height_m) for a module of the given cell area."""
    if area_m2 <= 0.0:
        raise ValueError("module area must be positive")
    width = frame_width_m(cells_in_series)
    height = (area_m2 * _FRAME_OVERHEAD) / width
    return (width, height)


def technologies() -> list[str]:
    """Distinct technology labels present in the CEC database."""
    table = _cec_table()
    return sorted({str(v) for v in table.loc["Technology"].unique()})


def search(
    query: str = "",
    technology: str | None = None,
    min_watts: float | None = None,
    max_watts: float | None = None,
    limit: int = 50,
) -> list[str]:
    """CEC module names matching the filters, most powerful first."""
    table = _cec_table()
    names: Iterable[str] = table.columns
    tokens = [t for t in query.lower().split() if t]
    hits: list[tuple[float, str]] = []
    for name in names:
        haystack = name.lower().replace("_", " ")
        if tokens and not all(t in haystack for t in tokens):
            continue
        record = table[name]
        if technology and str(record.get("Technology", "")) != technology:
            continue
        watts = float(record.get("STC", 0.0) or 0.0)
        if min_watts is not None and watts < min_watts:
            continue
        if max_watts is not None and watts > max_watts:
            continue
        hits.append((watts, name))
    hits.sort(reverse=True)
    return [name for _, name in hits[:limit]]


def get(name: str) -> ModuleSpec:
    """Look up one CEC module by exact name and derive its footprint."""
    table = _cec_table()
    if name not in table.columns:
        raise ModuleNotFound(f"{name!r} is not in the CEC module database")
    record = table[name]
    area = float(record.get("A_c", 0.0) or 0.0)
    cells = int(record.get("N_s", 0) or 0)
    watts = float(record.get("STC", 0.0) or 0.0)
    if area <= 0.0 or watts <= 0.0:
        raise ModuleNotFound(f"{name!r} has no usable area or STC rating in the database")
    width, height = dimensions_from_area(area, cells)
    return ModuleSpec(
        name=name,
        width_m=round(width, 4),
        height_m=round(height, 4),
        stc_watts=watts,
        technology=str(record.get("Technology", "") or "unknown"),
        efficiency=watts / (width * height * 1000.0),
        source="pvlib CEC module database",
    )


def custom(name: str, width_m: float, height_m: float, stc_watts: float,
           technology: str = "user-specified") -> ModuleSpec:
    """A module the user typed in — dimensions from the datasheet, not derived."""
    if min(width_m, height_m, stc_watts) <= 0.0:
        raise ValueError("width, height and STC power must all be positive")
    return ModuleSpec(
        name=name,
        width_m=width_m,
        height_m=height_m,
        stc_watts=stc_watts,
        technology=technology,
        efficiency=stc_watts / (width_m * height_m * 1000.0),
        source="user-specified",
    )


def sanity_check(spec: ModuleSpec) -> list[str]:
    """Warnings about a module whose numbers look implausible."""
    warnings: list[str] = []
    if spec.efficiency is not None:
        if spec.efficiency > 0.26:
            warnings.append(
                f"implied efficiency {spec.efficiency:.1%} is above any commercial module; "
                "check the dimensions"
            )
        elif spec.efficiency < 0.10:
            warnings.append(f"implied efficiency {spec.efficiency:.1%} is very low for a modern module")
    aspect = max(spec.width_m, spec.height_m) / min(spec.width_m, spec.height_m)
    if not math.isfinite(aspect) or aspect > 3.0:
        warnings.append(f"aspect ratio {aspect:.1f}:1 is unusual for a PV module")
    return warnings
