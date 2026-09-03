"""Satellite imagery for the report export.

The report needs a picture of the roof with the array drawn on it. Rather than
stitching tiles into one bitmap — which would mean a new imaging dependency —
this fetches the tiles around the site and lays them out as a CSS grid of
base64-embedded images, with the boundary and modules drawn over the top as
inline SVG. The result is a single self-contained HTML fragment with no external
requests, which is what an emailed one-page report needs.

Tile servers are fetched server-side, like PVGIS: never from the browser.

If the imagery cannot be fetched the functions return None and the caller falls
back to the plan view. A report without a photo is fine; a report with a broken
image is not.
"""

from __future__ import annotations

import base64
import logging
import math
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

TILE_PX = 256
ESRI_WORLD_IMAGERY = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
    "MapServer/tile/{z}/{y}/{x}"
)
OSM = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

ATTRIBUTION = {
    ESRI_WORLD_IMAGERY: "Imagery: Esri, Maxar, Earthstar Geographics",
    OSM: "© OpenStreetMap contributors",
}


@dataclass(frozen=True)
class TileView:
    """A square of tiles and the pixel frame they define."""

    zoom: int
    x0: int
    y0: int
    span: int
    origin_px: tuple[float, float]   # pixel coords of the view's top-left, global

    @property
    def size_px(self) -> int:
        return self.span * TILE_PX


def lonlat_to_global_px(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Web-Mercator pixel coordinates at a zoom level."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    scale = TILE_PX * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return (x, y)


def choose_zoom(bounds: tuple[float, float, float, float], span_tiles: int = 3,
                max_zoom: int = 20) -> int:
    """Deepest zoom at which the site still fits inside the tile square."""
    min_lon, min_lat, max_lon, max_lat = bounds
    for zoom in range(max_zoom, 0, -1):
        x0, y0 = lonlat_to_global_px(min_lon, max_lat, zoom)
        x1, y1 = lonlat_to_global_px(max_lon, min_lat, zoom)
        if (x1 - x0) <= span_tiles * TILE_PX * 0.85 and (y1 - y0) <= span_tiles * TILE_PX * 0.85:
            return zoom
    return 1


def view_for(lon: float, lat: float, zoom: int, span: int = 3) -> TileView:
    """A span x span tile square centred on a point."""
    px, py = lonlat_to_global_px(lon, lat, zoom)
    centre_x, centre_y = int(px // TILE_PX), int(py // TILE_PX)
    half = span // 2
    x0, y0 = centre_x - half, centre_y - half
    return TileView(zoom=zoom, x0=x0, y0=y0, span=span,
                    origin_px=(x0 * TILE_PX, y0 * TILE_PX))


def fetch_tiles(view: TileView, template: str = ESRI_WORLD_IMAGERY,
                timeout_s: float = 15.0,
                session: requests.Session | None = None) -> list[list[str | None]] | None:
    """Base64 data URIs for every tile in the view, or None if imagery is unavailable."""
    http = session or requests.Session()
    http.headers.setdefault("User-Agent", "Arka/0.1 (PV siting planner)")
    grid: list[list[str | None]] = []
    fetched = 0
    for row in range(view.span):
        line: list[str | None] = []
        for col in range(view.span):
            url = template.format(z=view.zoom, x=view.x0 + col, y=view.y0 + row)
            try:
                response = http.get(url, timeout=timeout_s)
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "image/png").split(";")[0]
                payload = base64.b64encode(response.content).decode("ascii")
                line.append(f"data:{mime};base64,{payload}")
                fetched += 1
            except requests.RequestException as exc:
                log.warning("tile %s unavailable: %s", url, exc)
                line.append(None)
        grid.append(line)
    if fetched == 0:
        return None
    return grid


def satellite_figure(
    boundary: list[tuple[float, float]],
    exclusions: list[list[tuple[float, float]]] | None = None,
    module_rings: list[list[tuple[float, float]]] | None = None,
    template: str = ESRI_WORLD_IMAGERY,
    span: int = 3,
    session: requests.Session | None = None,
) -> str | None:
    """A self-contained HTML fragment: satellite tiles with the array drawn on them.

    All geometry arrives as lon/lat. Returns None when imagery cannot be fetched,
    so the caller can fall back to the plan view rather than emit a broken image.
    """
    if len(boundary) < 3:
        return None
    lons = [p[0] for p in boundary]
    lats = [p[1] for p in boundary]
    bounds = (min(lons), min(lats), max(lons), max(lats))
    zoom = choose_zoom(bounds, span_tiles=span)
    centre = (sum(lons) / len(lons), sum(lats) / len(lats))
    view = view_for(centre[0], centre[1], zoom, span)

    grid = fetch_tiles(view, template=template, session=session)
    if grid is None:
        return None

    def to_view_px(lon: float, lat: float) -> tuple[float, float]:
        gx, gy = lonlat_to_global_px(lon, lat, zoom)
        return (gx - view.origin_px[0], gy - view.origin_px[1])

    def path(ring: list[tuple[float, float]]) -> str:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (to_view_px(*p) for p in ring))
        return f'<polygon points="{pts}" '

    size = view.size_px
    tiles_html = "".join(
        f'<img src="{cell}" style="position:absolute;left:{col * TILE_PX}px;'
        f'top:{row * TILE_PX}px;width:{TILE_PX}px;height:{TILE_PX}px" alt="">'
        for row, line in enumerate(grid)
        for col, cell in enumerate(line)
        if cell is not None
    )

    shapes = [path(boundary) + 'fill="none" stroke="#ffcc00" stroke-width="2"/>']
    for ring in exclusions or []:
        if len(ring) >= 3:
            shapes.append(path(ring) + 'fill="#ff4b4b" fill-opacity="0.35" stroke="#ff4b4b"/>')
    for ring in module_rings or []:
        shapes.append(path(ring) + 'fill="#2a4d7a" fill-opacity="0.85" stroke="none"/>')

    attribution = ATTRIBUTION.get(template, "")
    return (
        f'<div style="position:relative;width:{size}px;height:{size}px;max-width:100%;'
        f'overflow:hidden;border-radius:4px">{tiles_html}'
        f'<svg viewBox="0 0 {size} {size}" style="position:absolute;inset:0;'
        f'width:{size}px;height:{size}px">{"".join(shapes)}</svg></div>'
        f'<p style="font-size:0.75rem;color:#666;margin-top:0.25rem">{attribution}</p>'
    )
