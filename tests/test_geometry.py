"""Geometry and packing.

These tests pin the numbers that everything downstream depends on: if the
packed capacity is wrong, every kWh, pound and tonne after it is wrong too.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Polygon

from arka import geometry
from arka.scenario import ArrayType, Mounting, Orientation
from tests.conftest import MODULE_550, square_site


# -- frame and area ---------------------------------------------------------


def test_local_frame_round_trips_lonlat():
    site = square_site(side_m=40.0)
    frame = geometry.LocalFrame.for_site(site)
    metric = frame.to_metric(site.boundary)
    back = frame.to_lonlat(metric)
    for (lon0, lat0), (lon1, lat1) in zip(site.boundary, back):
        assert lon0 == pytest.approx(lon1, abs=1e-9)
        assert lat0 == pytest.approx(lat1, abs=1e-9)


def test_area_of_a_known_square():
    site = square_site(side_m=40.0)
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    # 0.5% tolerance covers the crude lon/lat construction in the fixture.
    assert geometry.area_m2(poly) == pytest.approx(1600.0, rel=0.005)


def test_area_is_scale_correct_at_different_latitudes():
    for lat in (-33.9, 0.0, 25.2, 51.5):
        site = square_site(side_m=30.0, lat=lat)
        frame = geometry.LocalFrame.for_site(site)
        poly = geometry.ring_to_polygon(site.boundary, frame)
        assert geometry.area_m2(poly) == pytest.approx(900.0, rel=0.01)


def test_ring_with_too_few_vertices_is_rejected():
    frame = geometry.LocalFrame(origin_lon=0.0, origin_lat=0.0)
    with pytest.raises(geometry.PackingError):
        geometry.ring_to_polygon([(0.0, 0.0), (0.001, 0.0)], frame)


# -- exclusions and setbacks ------------------------------------------------


def test_exclusions_are_differenced_out():
    boundary = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    hole = Polygon([(5, 5), (10, 5), (10, 10), (5, 10)])
    result = geometry.punch_exclusions(boundary, [hole])
    assert geometry.area_m2(result) == pytest.approx(400.0 - 25.0)


def test_exclusion_buffer_grows_the_hole():
    boundary = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    hole = Polygon([(8, 8), (12, 8), (12, 12), (8, 12)])
    plain = geometry.area_m2(geometry.punch_exclusions(boundary, [hole]))
    buffered = geometry.area_m2(geometry.punch_exclusions(boundary, [hole], buffer_m=1.0))
    assert buffered < plain


def test_setback_shrinks_the_usable_area():
    boundary = Polygon([(0, 0), (20, 0), (20, 20), (0, 20)])
    usable = geometry.usable_area(boundary, [], edge_setback_m=1.0)
    assert geometry.area_m2(usable) == pytest.approx(18.0 * 18.0)


def test_setback_larger_than_the_polygon_raises():
    boundary = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
    with pytest.raises(geometry.PackingError):
        geometry.usable_area(boundary, [], edge_setback_m=5.0)


# -- azimuth ----------------------------------------------------------------


def test_edge_bearings_are_sorted_longest_first():
    poly = Polygon([(0, 0), (30, 0), (30, 10), (0, 10)])
    bearings = geometry.edge_bearings(poly)
    assert bearings[0][0] == pytest.approx(30.0)
    assert bearings[0][1] == pytest.approx(90.0)


def test_azimuth_faces_south_in_the_northern_hemisphere():
    # Longest edge runs east-west, so the normals are north and south.
    poly = Polygon([(0, 0), (30, 0), (30, 10), (0, 10)])
    assert geometry.azimuth_from_longest_edge(poly, latitude=51.5) == pytest.approx(180.0)


def test_azimuth_faces_north_in_the_southern_hemisphere():
    poly = Polygon([(0, 0), (30, 0), (30, 10), (0, 10)])
    azimuth = geometry.azimuth_from_longest_edge(poly, latitude=-33.9)
    assert geometry.angular_difference(azimuth, 0.0) == pytest.approx(0.0)


def test_azimuth_follows_a_rotated_ridge():
    # Ridge running 30 degrees east of north; the south-facing normal is 120.
    angle = math.radians(30.0)
    length = 30.0
    ridge = (length * math.sin(angle), length * math.cos(angle))
    poly = Polygon([(0, 0), ridge, (ridge[0] + 5 * math.cos(angle), ridge[1] - 5 * math.sin(angle)),
                    (5 * math.cos(angle), -5 * math.sin(angle))])
    assert geometry.azimuth_from_longest_edge(poly, latitude=51.5) == pytest.approx(120.0, abs=0.5)


def test_angular_difference_wraps_around_north():
    assert geometry.angular_difference(350.0, 10.0) == pytest.approx(20.0)
    assert geometry.angular_difference(10.0, 350.0) == pytest.approx(20.0)


# -- row pitch --------------------------------------------------------------


def test_ballasted_row_pitch_matches_the_shading_formula():
    length, tilt, altitude = 2.0, 20.0, 20.0
    expected = (
        length * math.cos(math.radians(tilt))
        + length * math.sin(math.radians(tilt)) / math.tan(math.radians(altitude))
    )
    got = geometry.row_pitch_m(length, tilt, Mounting.BALLASTED, min_solar_altitude_deg=altitude)
    assert got == pytest.approx(expected)


def test_lower_sun_angle_forces_wider_rows():
    tight = geometry.row_pitch_m(2.0, 20.0, Mounting.BALLASTED, min_solar_altitude_deg=35.0)
    wide = geometry.row_pitch_m(2.0, 20.0, Mounting.BALLASTED, min_solar_altitude_deg=15.0)
    assert wide > tight


def test_flush_rows_abut():
    assert geometry.row_pitch_m(2.0, 30.0, Mounting.FLUSH) == pytest.approx(2.0)


def test_explicit_row_gap_overrides_the_shading_calculation():
    pitch = geometry.row_pitch_m(2.0, 20.0, Mounting.BALLASTED, row_gap_m=0.5)
    assert pitch == pytest.approx(2.0 * math.cos(math.radians(20.0)) + 0.5)


def test_zero_solar_altitude_is_rejected():
    with pytest.raises(geometry.PackingError):
        geometry.row_pitch_m(2.0, 20.0, Mounting.BALLASTED, min_solar_altitude_deg=0.0)


# -- packing ----------------------------------------------------------------


def test_pack_fills_a_square_roof(site, rooftop_spec):
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    layout = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)

    assert layout.module_count > 0
    assert layout.kwp == pytest.approx(layout.module_count * 0.550)
    assert layout.rows > 1
    # Modules cannot cover more ground than the usable area they sit in.
    assert layout.module_area_m2 <= layout.usable_area_m2
    # A ballasted 10-degree array lands near the 6-8 m2/kWp benchmark band.
    assert 5.0 < layout.area_per_kwp_m2 < 12.0


def test_pack_respects_exclusions(site, rooftop_spec):
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    clear = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)

    centre = poly.centroid
    plant = Polygon(
        [
            (centre.x - 8, centre.y - 8),
            (centre.x + 8, centre.y - 8),
            (centre.x + 8, centre.y + 8),
            (centre.x - 8, centre.y + 8),
        ]
    )
    obstructed = geometry.pack(
        poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat, exclusions=[plant]
    )
    assert obstructed.module_count < clear.module_count
    assert obstructed.usable_area_m2 < clear.usable_area_m2


def test_steeper_tilt_costs_capacity_when_ballasted(site, rooftop_spec):
    from dataclasses import replace

    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    shallow = geometry.pack(poly, ArrayType.ROOFTOP, replace(rooftop_spec, tilt_deg=5.0),
                            latitude=frame.origin_lat)
    steep = geometry.pack(poly, ArrayType.ROOFTOP, replace(rooftop_spec, tilt_deg=35.0),
                          latitude=frame.origin_lat)
    assert steep.module_count < shallow.module_count


def test_flush_mounting_beats_ballasted_on_the_same_footprint(site, rooftop_spec):
    from dataclasses import replace

    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    ballasted = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)
    flush = geometry.pack(
        poly, ArrayType.ROOFTOP, replace(rooftop_spec, mounting=Mounting.FLUSH),
        latitude=frame.origin_lat,
    )
    assert flush.module_count > ballasted.module_count


def test_landscape_and_portrait_pack_the_same_roof_comparably(site, rooftop_spec):
    from dataclasses import replace

    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    portrait = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)
    landscape = geometry.pack(
        poly, ArrayType.ROOFTOP, replace(rooftop_spec, orientation=Orientation.LANDSCAPE),
        latitude=frame.origin_lat,
    )
    assert portrait.module_count > 0 and landscape.module_count > 0
    assert 0.4 < landscape.module_count / portrait.module_count < 2.5


def test_placements_stay_inside_the_polygon(site, rooftop_spec):
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    layout = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)
    from shapely.geometry import Point

    for placement in layout.placements:
        assert poly.contains(Point(placement.x_m, placement.y_m))


def test_module_outlines_match_the_placement_count(site, rooftop_spec):
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    layout = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=frame.origin_lat)
    outlines = geometry.module_outlines(layout, MODULE_550, Orientation.PORTRAIT)
    assert len(outlines) == layout.module_count
    assert all(len(o) == 4 for o in outlines)


def test_tiny_polygon_packs_nothing(rooftop_spec):
    poly = Polygon([(0, 0), (3, 0), (3, 3), (0, 3)])
    layout = geometry.pack(poly, ArrayType.ROOFTOP, rooftop_spec, latitude=51.5)
    assert layout.module_count == 0
    assert layout.kwp == pytest.approx(0.0)


def test_packing_without_a_module_raises(site, rooftop_spec):
    from dataclasses import replace

    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    with pytest.raises(geometry.PackingError):
        geometry.pack(poly, ArrayType.ROOFTOP, replace(rooftop_spec, module=None),
                      latitude=frame.origin_lat)


# -- the interfaces that are not built yet ----------------------------------


@pytest.mark.parametrize("array_type", [ArrayType.GROUND_MOUNT, ArrayType.FLOATING])
def test_unbuilt_array_types_raise_rather_than_falling_back(site, rooftop_spec, array_type):
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(site.boundary, frame)
    with pytest.raises(NotImplementedError):
        geometry.pack(poly, array_type, rooftop_spec, latitude=frame.origin_lat)


# -- coordinates typed by hand ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "25.2048, 55.2708",
        "25.2048 55.2708",
        "25.2048; 55.2708",
        "lat 25.2048 lon 55.2708",
        "latitude: 25.2048, longitude: 55.2708",
        "25.2048° N, 55.2708° E",
        "  25.2048,55.2708  ",
    ],
)
def test_every_common_paste_form_parses(text):
    assert geometry.parse_latlon(text) == pytest.approx((25.2048, 55.2708))


def test_southern_and_western_hemispheres_go_negative():
    assert geometry.parse_latlon("33.8688 S, 151.2093 E") == pytest.approx((-33.8688, 151.2093))
    assert geometry.parse_latlon("51.5074 N, 0.1278 W") == pytest.approx((51.5074, -0.1278))


def test_longitude_first_is_swapped_when_unambiguous():
    # 151 cannot be a latitude, so this is lon, lat.
    assert geometry.parse_latlon("151.2093, -33.8688") == pytest.approx((-33.8688, 151.2093))


def test_a_single_number_is_rejected():
    with pytest.raises(ValueError):
        geometry.parse_latlon("25.2048")


def test_three_numbers_are_rejected():
    with pytest.raises(ValueError):
        geometry.parse_latlon("25.2, 55.2, 12")


def test_an_impossible_coordinate_is_rejected():
    with pytest.raises(ValueError):
        geometry.parse_latlon("95.0, 200.0")


# -- a rectangle dropped on a point -----------------------------------------


def test_rectangle_has_the_requested_area_on_the_ground():
    ring = geometry.rectangle_around(-0.12, 51.5, width_m=40.0, depth_m=25.0)
    frame = geometry.LocalFrame(origin_lon=-0.12, origin_lat=51.5)
    poly = geometry.ring_to_polygon(ring, frame)
    assert geometry.area_m2(poly) == pytest.approx(1000.0, rel=1e-6)


def test_rectangle_is_centred_on_the_point():
    ring = geometry.rectangle_around(55.2708, 25.2048, 30.0, 20.0)
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    assert sum(lons) / 4 == pytest.approx(55.2708, abs=1e-9)
    assert sum(lats) / 4 == pytest.approx(25.2048, abs=1e-9)


def test_rectangle_is_a_true_rectangle_far_from_the_equator():
    """Degrees of longitude shrink with latitude; the rectangle must not."""
    ring = geometry.rectangle_around(18.0, 68.0, width_m=40.0, depth_m=40.0)   # Arctic Norway
    frame = geometry.LocalFrame(origin_lon=18.0, origin_lat=68.0)
    xs = [p[0] for p in frame.to_metric(ring)]
    ys = [p[1] for p in frame.to_metric(ring)]
    assert max(xs) - min(xs) == pytest.approx(40.0, rel=1e-6)
    assert max(ys) - min(ys) == pytest.approx(40.0, rel=1e-6)


def test_bearing_rotates_the_long_axis():
    frame = geometry.LocalFrame(origin_lon=-0.12, origin_lat=51.5)
    north = frame.to_metric(geometry.rectangle_around(-0.12, 51.5, 10.0, 40.0, bearing_deg=0.0))
    east = frame.to_metric(geometry.rectangle_around(-0.12, 51.5, 10.0, 40.0, bearing_deg=90.0))
    span = lambda pts, i: max(p[i] for p in pts) - min(p[i] for p in pts)  # noqa: E731
    assert span(north, 1) == pytest.approx(40.0, rel=1e-6)     # depth runs north-south
    assert span(east, 0) == pytest.approx(40.0, rel=1e-6)      # now east-west


def test_rectangle_packs_like_any_other_boundary():
    """The whole point: a pasted coordinate should get someone to a kWp figure."""
    from arka.scenario import ArraySpec, Site
    from tests.conftest import MODULE_550

    ring = geometry.rectangle_around(-1.5197, 52.4068, 40.0, 25.0)
    site = Site(boundary=ring)
    frame = geometry.LocalFrame.for_site(site)
    poly = geometry.ring_to_polygon(ring, frame)
    spec = ArraySpec(module=MODULE_550, tilt_deg=10.0, azimuth_deg=180.0)
    layout = geometry.pack(poly, ArrayType.ROOFTOP, spec, latitude=52.4068)
    assert layout.module_count > 100


def test_a_zero_sized_rectangle_is_rejected():
    with pytest.raises(geometry.PackingError):
        geometry.rectangle_around(0.0, 0.0, 0.0, 10.0)
