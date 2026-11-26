import math
from itertools import pairwise

import pytest

from trips import geo

CHICAGO = (41.8781, -87.6298)
DENVER = (39.7392, -104.9903)


def meridian(miles: int) -> list[geo.Point]:
    """Points one mile apart going north from (40, -90)."""
    step = 1 / 69.09  # degrees of latitude per mile on a 3958.8-mile sphere
    return [(40.0 + i * step, -90.0) for i in range(miles + 1)]


def test_haversine_miles():
    assert geo.haversine_miles(CHICAGO, DENVER) == pytest.approx(920, abs=5)
    assert geo.haversine_miles((40.0, -90.0), (41.0, -90.0)) == pytest.approx(69.09, abs=0.01)
    assert geo.haversine_miles(CHICAGO, CHICAGO) == 0


def test_cumulative_distance_and_point_at():
    line = [(40.0, -90.0), (41.0, -90.0), (41.0, -89.0)]
    cum = geo.cumulative_miles(line)
    assert cum[0] == 0
    assert cum[1] == pytest.approx(69.09, abs=0.01)
    assert geo.point_at(line, cum, cum[1] / 2) == pytest.approx((40.5, -90.0))
    assert geo.point_at(line, cum, -5) == line[0]
    assert geo.point_at(line, cum, cum[-1] + 5) == line[-1]


@pytest.mark.parametrize(
    ("total", "interval", "expected"),
    [
        (106.3, 25, [0, 25, 50, 75, 100, 106.3]),  # destination added
        (100.05, 25, [0, 25, 50, 75, 100]),  # within 0.1 mile of the last checkpoint
        (100, 25, [0, 25, 50, 75, 100]),
        (5, 10, [0, 5]),
        (10, 10, [0, 10]),
    ],
)
def test_checkpoint_distances(total, interval, expected):
    assert geo.checkpoint_distances(total, interval) == pytest.approx(expected)


def test_sample_polyline_includes_the_end():
    line = meridian(10)
    samples = geo.sample_polyline(line, 3)
    assert len(samples) == 5  # 0, 3, 6, 9 and the end
    assert samples[0] == line[0]
    assert samples[-1] == line[-1]


def test_time_profile_interpolation():
    profile = [(0.0, 0.0), (1000.0, 100.0), (1000.0, 100.0), (3000.0, 500.0)]
    assert geo.interpolate(profile, 500) == 50
    assert geo.interpolate(profile, 1000) == 100
    assert geo.interpolate(profile, 2000) == 300
    assert geo.interpolate(profile, -1) == 0
    assert geo.interpolate(profile, 5000) == 500


def test_simplify_drops_collinear_points():
    line = [(40.0, -90.0 + i * 0.001) for i in range(1000)]
    assert geo.simplify(line) == [line[0], line[-1]]


def test_simplify_doubles_tolerance_until_it_fits():
    zigzag = [(40.0 + (0.002 if i % 2 else 0.0), -90.0 + i * 0.01) for i in range(5000)]
    assert len(geo.simplify(zigzag, max_points=10_000)) == 5000  # 0.002° survives 0.0005°
    simplified = geo.simplify(zigzag)  # at most 2,000 points by default
    assert len(simplified) <= 2000
    assert (simplified[0], simplified[-1]) == (zigzag[0], zigzag[-1])


def test_split_by_ranges_covers_the_line_end_to_end():
    line = meridian(10)
    cum = geo.cumulative_miles(line)
    pieces = geo.split_by_ranges(line, cum, [(0, 2.5), (2.5, 7), (7, cum[-1])])
    assert pieces[0][0] == line[0]
    assert pieces[-1][-1] == line[-1]
    for left, right in pairwise(pieces):
        assert left[-1] == right[0]
    assert len(pieces[0]) == 4  # start, the vertices at miles 1 and 2, the end at 2.5
    assert pieces[0][-1][0] == pytest.approx(40.0 + 2.5 / 69.09, abs=1e-3)


def test_bbox():
    assert geo.bbox([CHICAGO, DENVER]) == [-104.9903, 39.7392, -87.6298, 41.8781]


def test_lattice_snapping():
    assert geo.snap(41.87, 0.15) == 41.85
    assert geo.snap(-87.63, 0.15) == -87.6
    assert geo.snap_point((41.87, -87.63), 0.1) == (41.9, -87.6)


def test_corridor_cells_include_the_8_neighbours():
    cells = geo.corridor_cells([[(40.0, -90.0), (40.001, -90.0)]], 0.1)
    assert cells == {(lat, lon) for lat in (39.9, 40.0, 40.1) for lon in (-90.1, -90.0, -89.9)}


def test_corridor_cells_follow_the_line():
    line = [(40.0, -90.0), (40.0, -89.0)]
    cells = geo.corridor_cells([line], 0.1)
    assert {lon for _, lon in cells} >= {round(-90.0 + i * 0.1, 6) for i in range(11)}
    assert {lat for lat, _ in cells} == {39.9, 40.0, 40.1}


def test_corridor_cells_without_the_ring_are_only_the_cells_passed_through():
    cells = geo.corridor_cells([[(40.0, -90.0), (40.001, -90.0)]], 0.1, ring=False)
    assert cells == {(40.0, -90.0)}


WEST, EAST = (40.0, -110.0), (40.0, -85.5)
LONG_ROUTES = [[WEST, EAST], [WEST, (41.5, -97.75), EAST], [WEST, (38.5, -97.75), EAST]]


def over_budget(polylines, step, ring):
    return len(geo.corridor_cells(polylines, step, ring=ring)) > geo.MAX_WEATHER_CELLS


def test_short_trip_keeps_the_ring_at_the_finest_spacing():
    short = [[(40.0, -90.0), (40.0, -88.0)]]  # ~106 miles

    step, cells = geo.weather_grid(short)

    assert step == 0.1
    assert cells == geo.corridor_cells(short, 0.1, ring=True)


def test_long_corridor_fits_the_budget_by_dropping_the_ring_before_coarsening():
    assert all(1250 < geo.cumulative_miles(line)[-1] < 1350 for line in LONG_ROUTES)

    step, cells = geo.weather_grid(LONG_ROUTES)

    assert geo.MAX_WEATHER_CELLS == 150
    assert len(cells) <= 150
    assert step == 0.5
    assert cells == geo.corridor_cells(LONG_ROUTES, 0.5, ring=False)
    assert over_budget(LONG_ROUTES, 0.5, ring=True)  # the ring would only fit at 1.0°
    assert not over_budget(LONG_ROUTES, 1.0, ring=True)
    for finer in (s for s in geo.GRID_STEPS_DEG if s < step):
        assert over_budget(LONG_ROUTES, finer, ring=True)
        assert over_budget(LONG_ROUTES, finer, ring=False)


def test_grid_falls_back_to_the_coarsest_spacing_without_the_ring():
    sweep = [[(float(lat), -120.0), (float(lat), -75.0)] for lat in range(30, 46)]

    step, cells = geo.weather_grid(sweep)

    assert step == geo.GRID_STEPS_DEG[-1] == 1.0
    assert cells == geo.corridor_cells(sweep, 1.0, ring=False)
    assert len(cells) > geo.MAX_WEATHER_CELLS


def test_every_checkpoint_maps_to_a_cell_of_the_grid_next_to_it():
    step, cells = geo.weather_grid(LONG_ROUTES)

    for line in LONG_ROUTES:
        cum = geo.cumulative_miles(line)
        for distance in geo.checkpoint_distances(cum[-1], 10):
            point = geo.point_at(line, cum, distance)
            cell = geo.grid_cell(point, step, cells)
            assert cell in cells
            assert geo.haversine_miles(point, cell) <= step * geo.MILES_PER_DEGREE_LAT


def test_grid_cell_is_the_own_cell_else_the_nearest():
    cells = {(40.0, -90.0), (40.5, -90.0)}
    assert geo.grid_cell((40.1, -90.1), 0.5, cells) == (40.0, -90.0)  # its own cell
    assert geo.grid_cell((40.4, -90.8), 0.5, cells) == (40.5, -90.0)  # (40.5, -91.0) is missing


def test_overlap_ratio():
    line = [(40.0, -90.0), (40.0, -89.0)]
    assert geo.overlap_ratio(line, line) == 1.0
    assert geo.overlap_ratio(line, [(40.1, -90.0), (40.1, -89.0)]) == 0.0  # ~7 miles apart
    assert geo.overlap_ratio(line, [(40.005, -90.0), (40.005, -89.0)]) == 1.0  # ~0.35 mile
    half = geo.overlap_ratio(line, [(40.0, -90.0), (40.0, -89.5)])
    assert 0.45 < half < 0.6


def test_detour_waypoints_are_left_then_right_of_the_midpoint():
    eastward = [(40.0, -90.0), (40.0, -88.0)]  # ~106 miles → D = 20 miles (clamped)
    left, right, far_left, far_right = geo.detour_waypoints(eastward)
    mid = (40.0, -89.0)
    assert left[0] > 40 and far_left[0] > left[0]  # heading east, left is north
    assert right[0] < 40 and far_right[0] < right[0]
    assert geo.haversine_miles(mid, left) == pytest.approx(20, abs=0.5)
    assert geo.haversine_miles(mid, far_right) == pytest.approx(32, abs=0.5)


@pytest.mark.parametrize(("miles", "offset"), [(400, 60), (2000, 150)])
def test_detour_offset_is_15_percent_clamped(miles, offset):
    line = meridian(miles)
    left = geo.detour_waypoints(line)[0]
    mid = line[miles // 2]
    assert geo.haversine_miles(mid, left) == pytest.approx(offset, rel=0.01)
    assert left[1] < -90  # heading north, left is west
    assert math.isclose(left[0], mid[0], abs_tol=0.5)
