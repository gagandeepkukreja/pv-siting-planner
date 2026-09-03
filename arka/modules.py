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

# Cell columns across the frame, by series-cell count. Almost every crystalline
# module is six cells wide; the 96-cell back-contact format is eight.
#
# Width is derived from cell *pitch*, not from a fixed table of frame widths. A
# fixed table cannot work: the CEC database reports both a classic 156 mm
# 72-cell module (992 mm wide) and a modern 182 mm half-cut one (1134 mm wide)
# as N_s = 72, so cell count alone does not identify the format. Cell pitch,
# recovered from cell area over cell count, does.
_COLUMNS_BY_CELLS: tuple[tuple[int, int], ...] = (
    (36, 4),
    (48, 4),
    (60, 6),
    (66, 6),
    (72, 6),
    (96, 8),
    (120, 6),
    (132, 6),
    (144, 6),
)
_DEFAULT_COLUMNS = 6
_DEFAULT_WIDTH_M = 1.05

# Above this count the cells are half-cut: twice as many, each half as tall, so
# the pitch calculation has to account for a 2:1 cell rather than a square one.
_HALF_CUT_THRESHOLD = 100

# Glass-to-frame overhead, used only where the cell grid is unknown: the CEC
# `A_c` is aperture cell area, a little under the outer frame footprint.
_FRAME_OVERHEAD = 1.06

# Inter-cell gaps plus the frame border, as a linear scale on each dimension
# once the cell grid is known. Calibrated against published datasheets for
# 60-, 72- and 96-cell modules.
_FRAME_BORDER = 1.005


class ModuleNotFound(LookupError):
    """No CEC module matched the query."""


@lru_cache(maxsize=1)
def _cec_table():
    """The raw CEC module DataFrame. Ships with pvlib; no network needed."""
    from pvlib.pvsystem import retrieve_sam

    return retrieve_sam("CECMod")


def cell_columns(cells_in_series: int | None) -> int:
    """How many cells sit across the frame, for a given series-cell count."""
    if not cells_in_series or cells_in_series <= 0:
        return _DEFAULT_COLUMNS
    return min(_COLUMNS_BY_CELLS, key=lambda pair: abs(pair[0] - cells_in_series))[1]


def frame_width_m(cells_in_series: int | None, area_m2: float | None = None) -> float:
    """Frame width, derived from cell pitch where cell area is known.

    For a full-cell module the cells are square, so pitch is the square root of
    cell area. For a half-cut module each cell is twice as wide as it is tall,
    so the pitch is the square root of twice the cell area. Multiplying by the
    column count gives the width across the glass.
    """
    if not cells_in_series or cells_in_series <= 0 or not area_m2 or area_m2 <= 0.0:
        return _DEFAULT_WIDTH_M
    cell_area = area_m2 / cells_in_series
    if cells_in_series >= _HALF_CUT_THRESHOLD:
        pitch = math.sqrt(2.0 * cell_area)
    else:
        pitch = math.sqrt(cell_area)
    return cell_columns(cells_in_series) * pitch


def dimensions_from_area(area_m2: float, cells_in_series: int | None) -> tuple[float, float]:
    """(width_m, height_m) for a module of the given cell area.

    Both dimensions come from the cell grid — columns across, rows down, at the
    pitch implied by cell area. Deriving only the width from the grid and then
    solving the height from total area compounds the frame overhead into one
    dimension, which overstated module length by around 5% and, through row
    pitch, understated how many rows fit on a roof.
    """
    if area_m2 <= 0.0:
        raise ValueError("module area must be positive")
    if not cells_in_series or cells_in_series <= 0:
        width = _DEFAULT_WIDTH_M
        return (width, (area_m2 * _FRAME_OVERHEAD) / width)

    columns = cell_columns(cells_in_series)
    cell_area = area_m2 / cells_in_series
    half_cut = cells_in_series >= _HALF_CUT_THRESHOLD
    pitch = math.sqrt(2.0 * cell_area) if half_cut else math.sqrt(cell_area)
    rows = cells_in_series / columns

    width = columns * pitch
    height = rows * (pitch / 2.0 if half_cut else pitch)
    # Inter-cell gaps and the frame border, spread across both dimensions.
    return (width * _FRAME_BORDER, height * _FRAME_BORDER)


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
