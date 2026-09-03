"""Polygon handling, orientation and module packing.

Everything here is pure: shapely and pyproj in, dataclasses out. No I/O, no
Streamlit, no network.

Coordinate conventions
----------------------
* Site geometry arrives as lon/lat (EPSG:4326) from the map widget.
* All metric work happens in a site-local azimuthal equidistant frame whose
  origin is the site centre. x is east, y is north, both in metres. Over the
  couple of hundred metres an array spans, distortion is well under a
  millimetre, and unlike UTM there is no zone edge to fall off.
* Azimuths are degrees clockwise from true north (0 = N, 90 = E, 180 = S),
  matching pvlib. PVGIS uses a south-referenced convention; the conversion
  lives in `resource.py` and nowhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pyproj import CRS, Transformer
from shapely.affinity import rotate, scale
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .scenario import (
    ArraySpec,
    ArrayType,
    Layout,
    LonLat,
    ModulePlacement,
    ModuleSpec,
    Mounting,
    Orientation,
    Ring,
    Site,
)

WGS84 = CRS.from_epsg(4326)


class PackingError(ValueError):
    """The polygon and parameters cannot produce a layout."""


@dataclass(frozen=True)
class LocalFrame:
    """Site-local metric frame. Construct once per site, reuse everywhere."""

    origin_lon: float
    origin_lat: float

    @classmethod
    def for_site(cls, site: Site) -> "LocalFrame":
        centre = site.centre
        if centre is None:
            raise PackingError("site has no boundary, cannot build a local frame")
        return cls(origin_lon=centre[0], origin_lat=centre[1])

    @property
    def crs(self) -> CRS:
        return CRS.from_proj4(
            f"+proj=aeqd +lat_0={self.origin_lat} +lon_0={self.origin_lon} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )

    def _to_metric(self) -> Transformer:
        return Transformer.from_crs(WGS84, self.crs, always_xy=True)

    def _to_wgs84(self) -> Transformer:
        return Transformer.from_crs(self.crs, WGS84, always_xy=True)

    def to_metric(self, ring: Ring) -> list[tuple[float, float]]:
        t = self._to_metric()
        return [t.transform(lon, lat) for lon, lat in ring]

    def to_lonlat(self, points: list[tuple[float, float]]) -> list[LonLat]:
        t = self._to_wgs84()
        return [t.transform(x, y) for x, y in points]


# ---------------------------------------------------------------------------
# Polygon basics
# ---------------------------------------------------------------------------


def ring_to_polygon(ring: Ring, frame: LocalFrame) -> Polygon:
    """Project a lon/lat ring into the local metric frame as a valid polygon."""
    if len(ring) < 3:
        raise PackingError(f"a polygon needs at least 3 vertices, got {len(ring)}")
    poly = Polygon(frame.to_metric(ring))
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        raise PackingError("polygon is empty after cleaning; check for self-intersection")
    return poly


def area_m2(polygon: BaseGeometry) -> float:
    """Planimetric area in square metres. The frame is already metric."""
    return float(polygon.area)


def punch_exclusions(
    boundary: Polygon,
    exclusions: list[Polygon],
    buffer_m: float = 0.0,
) -> BaseGeometry:
    """Difference exclusion zones out of the boundary, optionally buffered out."""
    if not exclusions:
        return boundary
    holes = [e.buffer(buffer_m) if buffer_m > 0.0 else e for e in exclusions]
    return boundary.difference(unary_union(holes))


def usable_area(
    boundary: Polygon,
    exclusions: list[Polygon],
    edge_setback_m: float = 0.0,
    exclusion_buffer_m: float = 0.0,
) -> BaseGeometry:
    """Boundary minus its perimeter setback minus buffered exclusions."""
    area = boundary
    if edge_setback_m > 0.0:
        area = area.buffer(-edge_setback_m)
        if area.is_empty:
            raise PackingError(
                f"a {edge_setback_m} m setback consumes the whole polygon "
                f"({area_m2(boundary):.1f} m2) — reduce the setback or draw a larger area"
            )
    return punch_exclusions(area, exclusions, exclusion_buffer_m)


def edge_bearings(polygon: Polygon) -> list[tuple[float, float]]:
    """(length_m, bearing_deg) for each exterior edge, longest first."""
    coords = list(polygon.exterior.coords)
    out: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length <= 0.0:
            continue
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        out.append((length, bearing))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return out


def angular_difference(a_deg: float, b_deg: float) -> float:
    """Smallest absolute difference between two bearings, 0..180."""
    return abs((a_deg - b_deg + 180.0) % 360.0 - 180.0)


def azimuth_from_longest_edge(polygon: Polygon, latitude: float) -> float:
    """Array azimuth implied by the polygon's longest edge.

    A roof's longest edge is normally the ridge or the eaves, so the modules
    face along its normal. Of the two normals we take the one closer to
    equator-facing: south in the northern hemisphere, north in the southern.
    """
    bearings = edge_bearings(polygon)
    if not bearings:
        raise PackingError("polygon has no measurable edges")
    _, ridge = bearings[0]
    candidates = ((ridge + 90.0) % 360.0, (ridge - 90.0) % 360.0)
    equator_facing = 180.0 if latitude >= 0.0 else 0.0
    return min(candidates, key=lambda c: angular_difference(c, equator_facing))


# ---------------------------------------------------------------------------
# Row geometry
# ---------------------------------------------------------------------------


def row_pitch_m(
    module_length_m: float,
    tilt_deg: float,
    mounting: Mounting,
    min_solar_altitude_deg: float = 20.0,
    row_gap_m: float | None = None,
) -> float:
    """Centre-to-centre row spacing along the slope direction, in plan view.

    FLUSH modules sit in the roof plane, so rows abut with only the mechanical
    gap between them. BALLASTED rows on a flat plane are spaced so that the row
    in front does not shade the row behind while the sun is above
    `min_solar_altitude_deg`:

        pitch = L·cos(tilt) + L·sin(tilt) / tan(altitude_min)

    An explicit `row_gap_m` overrides the shading calculation.
    """
    if module_length_m <= 0.0:
        raise PackingError("module length must be positive")
    projected = module_length_m * math.cos(math.radians(tilt_deg))
    if mounting is Mounting.FLUSH:
        return module_length_m + (row_gap_m or 0.0)
    if row_gap_m is not None:
        return projected + row_gap_m
    if not 0.0 < min_solar_altitude_deg < 90.0:
        raise PackingError("min_solar_altitude_deg must be between 0 and 90 exclusive")
    collector_height = module_length_m * math.sin(math.radians(tilt_deg))
    shadow = collector_height / math.tan(math.radians(min_solar_altitude_deg))
    return projected + shadow


def module_plan_depth_m(module_length_m: float, tilt_deg: float, mounting: Mounting) -> float:
    """Depth the module occupies in the packing frame."""
    if mounting is Mounting.FLUSH:
        return module_length_m
    return module_length_m * math.cos(math.radians(tilt_deg))


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------


def pack(polygon: Polygon, array_type: ArrayType, params: ArraySpec, latitude: float = 0.0,
         exclusions: list[Polygon] | None = None) -> Layout:
    """Fill a polygon with modules and return the resulting layout.

    `polygon` and `exclusions` are already in the site-local metric frame.
    GROUND_MOUNT and FLOATING are part of the interface from day one but are
    not implemented yet — they raise rather than silently falling back to the
    rooftop routine.
    """
    if array_type is ArrayType.GROUND_MOUNT:
        raise NotImplementedError(
            "GROUND_MOUNT packing is not implemented. It needs row layout driven by "
            "ground coverage ratio and row-to-row shading; see CLAUDE.md scope."
        )
    if array_type is ArrayType.FLOATING:
        raise NotImplementedError(
            "FLOATING packing is not implemented. It needs coverage ratio limits, "
            "freeboard and anchoring corridors; see CLAUDE.md scope."
        )
    if array_type is not ArrayType.ROOFTOP:
        raise NotImplementedError(f"unknown array type {array_type!r}")

    module = params.module
    if module is None:
        raise PackingError("no module selected; pick one from the CEC database first")

    gross = area_m2(polygon)
    region = usable_area(
        polygon,
        exclusions or [],
        edge_setback_m=params.edge_setback_m,
        exclusion_buffer_m=params.exclusion_buffer_m,
    )
    if region.is_empty:
        raise PackingError("nothing left to pack after setback and exclusions")

    azimuth = params.azimuth_deg
    if azimuth is None:
        azimuth = azimuth_from_longest_edge(polygon, latitude)

    across_m, along_m = module.footprint(params.orientation)
    pitch = row_pitch_m(
        along_m,
        params.tilt_deg,
        params.mounting,
        params.min_solar_altitude_deg,
        params.row_gap_m,
    )
    depth = module_plan_depth_m(along_m, params.tilt_deg, params.mounting)
    col_step = across_m + params.module_gap_m

    # Work in a frame where the array faces due south, so rows run along x and
    # the pitch runs along y. Rotating counterclockwise by (azimuth - 180)
    # takes the facing direction onto -y.
    centroid = polygon.centroid
    origin = (centroid.x, centroid.y)
    working = rotate(region, azimuth - 180.0, origin=origin, use_radians=False)

    # A flush array is packed in the roof plane, not in plan view. Stretching
    # the slope axis by 1/cos(tilt) recovers the true plane length from the
    # satellite footprint; placements are squashed back at the end.
    slope_stretch = 1.0
    if params.mounting is Mounting.FLUSH:
        cos_tilt = math.cos(math.radians(params.tilt_deg))
        if cos_tilt <= 0.0:
            raise PackingError("flush mounting needs a tilt below 90 degrees")
        slope_stretch = 1.0 / cos_tilt
        working = scale(working, xfact=1.0, yfact=slope_stretch, origin=origin)

    placements: list[ModulePlacement] = []
    min_x, min_y, max_x, max_y = working.bounds
    n_cols = max(0, int(math.floor((max_x - min_x + params.module_gap_m) / col_step)))
    n_rows = max(0, int(math.floor((max_y - min_y + (pitch - depth)) / pitch)))

    for row in range(n_rows):
        y0 = min_y + row * pitch
        y1 = y0 + depth
        if y1 > max_y:
            break
        for col in range(n_cols):
            x0 = min_x + col * col_step
            x1 = x0 + across_m
            if x1 > max_x:
                break
            cell = box(x0, y0, x1, y1)
            if working.contains(cell):
                placements.append(
                    ModulePlacement(
                        x_m=(x0 + x1) / 2.0,
                        y_m=(y0 + y1) / 2.0,
                        row=row,
                        col=col,
                    )
                )

    # Back to the real frame: undo the slope stretch, then the rotation.
    unrotate = -(azimuth - 180.0)
    real_placements: list[ModulePlacement] = []
    for p in placements:
        x, y = p.x_m, origin[1] + (p.y_m - origin[1]) / slope_stretch
        angle = math.radians(unrotate)
        dx, dy = x - origin[0], y - origin[1]
        real_placements.append(
            ModulePlacement(
                x_m=origin[0] + dx * math.cos(angle) - dy * math.sin(angle),
                y_m=origin[1] + dx * math.sin(angle) + dy * math.cos(angle),
                row=p.row,
                col=p.col,
            )
        )

    count = len(real_placements)
    rows_used = len({p.row for p in real_placements})
    return Layout(
        module_count=count,
        kwp=count * module.stc_watts / 1000.0,
        rows=rows_used,
        gross_area_m2=gross,
        usable_area_m2=area_m2(region),
        module_area_m2=count * module.area_m2,
        row_pitch_m=pitch,
        azimuth_deg=azimuth,
        tilt_deg=params.tilt_deg,
        placements=real_placements,
        polygon_m=_exterior_coords(region),
        holes_m=_interior_coords(region),
    )


def module_outlines(layout: Layout, module: ModuleSpec, orientation: Orientation) -> list[list[tuple[float, float]]]:
    """Corner coordinates of each packed module, for drawing the layout."""
    across_m, along_m = module.footprint(orientation)
    depth = module_plan_depth_m(along_m, layout.tilt_deg, Mounting.BALLASTED)
    angle = math.radians(-(layout.azimuth_deg - 180.0))
    half_w, half_d = across_m / 2.0, depth / 2.0
    corners = [(-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d)]
    outlines = []
    for p in layout.placements:
        outlines.append(
            [
                (
                    p.x_m + dx * math.cos(angle) - dy * math.sin(angle),
                    p.y_m + dx * math.sin(angle) + dy * math.cos(angle),
                )
                for dx, dy in corners
            ]
        )
    return outlines


def _exterior_coords(geom: BaseGeometry) -> list[tuple[float, float]]:
    if isinstance(geom, MultiPolygon):
        largest = max(geom.geoms, key=lambda g: g.area)
        return [(float(x), float(y)) for x, y in largest.exterior.coords]
    if isinstance(geom, Polygon):
        return [(float(x), float(y)) for x, y in geom.exterior.coords]
    return []


def _interior_coords(geom: BaseGeometry) -> list[list[tuple[float, float]]]:
    polys = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    rings = []
    for poly in polys:
        if isinstance(poly, Polygon):
            for interior in poly.interiors:
                rings.append([(float(x), float(y)) for x, y in interior.coords])
    return rings
