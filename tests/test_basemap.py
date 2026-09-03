"""Web-Mercator tile maths for the report's satellite image. No network."""

from __future__ import annotations

import pytest

from arka import basemap


def test_zoom_zero_is_a_single_tile_world():
    assert basemap.lonlat_to_global_px(-180.0, 0.0, 0)[0] == pytest.approx(0.0)
    assert basemap.lonlat_to_global_px(180.0, 0.0, 0)[0] == pytest.approx(basemap.TILE_PX)
    assert basemap.lonlat_to_global_px(0.0, 0.0, 0) == pytest.approx((128.0, 128.0))


def test_the_mercator_limit_is_the_top_of_the_world():
    _, y = basemap.lonlat_to_global_px(0.0, 85.05112878, 0)
    assert y == pytest.approx(0.0, abs=1e-6)


def test_latitude_is_clamped_beyond_the_mercator_limit():
    at_limit = basemap.lonlat_to_global_px(0.0, 85.05112878, 3)
    beyond = basemap.lonlat_to_global_px(0.0, 89.9, 3)
    assert beyond == pytest.approx(at_limit)


def test_each_zoom_level_doubles_the_pixel_grid():
    a = basemap.lonlat_to_global_px(10.0, 50.0, 10)
    b = basemap.lonlat_to_global_px(10.0, 50.0, 11)
    assert b[0] == pytest.approx(a[0] * 2.0)
    assert b[1] == pytest.approx(a[1] * 2.0)


def test_north_is_up():
    _, north = basemap.lonlat_to_global_px(0.0, 60.0, 10)
    _, south = basemap.lonlat_to_global_px(0.0, 40.0, 10)
    assert north < south


def test_a_small_site_gets_a_deep_zoom():
    tiny = (-0.1201, 51.5000, -0.1197, 51.5003)     # a roof
    big = (-0.30, 51.30, 0.10, 51.70)               # most of a city
    assert basemap.choose_zoom(tiny) > basemap.choose_zoom(big)


def test_the_chosen_zoom_actually_fits_the_site():
    bounds = (-0.1201, 51.5000, -0.1197, 51.5003)
    zoom = basemap.choose_zoom(bounds, span_tiles=3)
    x0, y0 = basemap.lonlat_to_global_px(bounds[0], bounds[3], zoom)
    x1, y1 = basemap.lonlat_to_global_px(bounds[2], bounds[1], zoom)
    assert max(x1 - x0, y1 - y0) <= 3 * basemap.TILE_PX


def test_the_view_is_square_and_centred():
    view = basemap.view_for(-0.12, 51.5, 18, span=3)
    assert view.span == 3
    assert view.size_px == 3 * basemap.TILE_PX
    px, py = basemap.lonlat_to_global_px(-0.12, 51.5, 18)
    assert view.origin_px[0] <= px <= view.origin_px[0] + view.size_px
    assert view.origin_px[1] <= py <= view.origin_px[1] + view.size_px


def test_a_degenerate_boundary_yields_no_figure():
    assert basemap.satellite_figure([(0.0, 0.0), (1.0, 1.0)]) is None


def test_unreachable_imagery_degrades_to_none(monkeypatch):
    """A report with no photo is fine. A report with a broken image is not."""
    monkeypatch.setattr(basemap, "fetch_tiles", lambda *a, **k: None)
    assert basemap.satellite_figure(
        [(-0.12, 51.5), (-0.119, 51.5), (-0.119, 51.501), (-0.12, 51.501)]
    ) is None


def test_a_figure_embeds_its_tiles_and_overlay(monkeypatch):
    monkeypatch.setattr(
        basemap, "fetch_tiles",
        lambda view, **k: [["data:image/png;base64,AAAA"] * view.span for _ in range(view.span)],
    )
    ring = [(-0.12, 51.5), (-0.119, 51.5), (-0.119, 51.501), (-0.12, 51.501)]
    html = basemap.satellite_figure(ring, module_rings=[ring])
    assert "data:image/png;base64," in html
    assert "<svg" in html and "<polygon" in html
    # Self-contained: no external request when the report is opened.
    assert "http://" not in html and "https://" not in html
    assert "Esri" in html
