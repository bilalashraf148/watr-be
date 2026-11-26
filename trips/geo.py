"""Geometry on (lat, lon) polylines. Pure functions: no Django, no HTTP."""

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from itertools import pairwise

Point = tuple[float, float]  # (lat, lon) in degrees

EARTH_RADIUS_MILES = 3958.8
METERS_PER_MILE = 1609.344
MILES_PER_DEGREE_LAT = 69.0

GRID_STEPS_DEG = (0.1, 0.15, 0.2, 0.25, 0.35, 0.5, 0.75, 1.0)
# Open-Meteo counts every location as one call (free tier: 600 a minute): ≤ 150 → 4 plans a minute
MAX_WEATHER_CELLS = 150


def haversine_miles(a: Point, b: Point) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, h)))


def cumulative_miles(points: Sequence[Point]) -> list[float]:
    cum = [0.0]
    for prev, cur in pairwise(points):
        cum.append(cum[-1] + haversine_miles(prev, cur))
    return cum


def point_at(points: Sequence[Point], cum: Sequence[float], distance: float) -> Point:
    """The point `distance` along the polyline (`cum` = its cumulative distances, same unit)."""
    if distance <= cum[0]:
        return points[0]
    if distance >= cum[-1]:
        return points[-1]
    i = bisect_right(cum, distance) - 1
    span = cum[i + 1] - cum[i]
    t = (distance - cum[i]) / span if span > 0 else 0.0
    (lat1, lon1), (lat2, lon2) = points[i], points[i + 1]
    return (lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1))


def bearing_degrees(a: Point, b: Point) -> float:
    """Initial great-circle bearing from a to b, 0–360 (0 = north, 90 = east)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def destination_point(origin: Point, bearing_deg: float, distance_miles: float) -> Point:
    lat1, lon1 = math.radians(origin[0]), math.radians(origin[1])
    brg = math.radians(bearing_deg)
    ang = distance_miles / EARTH_RADIUS_MILES
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)


def checkpoint_distances(total_miles: float, interval_miles: float) -> list[float]:
    """Mile 0, every interval along the route, and the end when it is > 0.1 mile past the last."""
    count = int(total_miles // interval_miles)
    distances = [k * interval_miles for k in range(count + 1)]
    if total_miles - distances[-1] > 0.1:
        distances.append(total_miles)
    return distances


def sample_polyline(points: Sequence[Point], step_miles: float) -> list[Point]:
    """Points every `step_miles` along the polyline, plus its last point."""
    cum = cumulative_miles(points)
    total = cum[-1]
    count = int(total // step_miles)
    samples = [point_at(points, cum, k * step_miles) for k in range(count + 1)]
    if total - count * step_miles > 1e-9:
        samples.append(points[-1])
    return samples


def interpolate(profile: Sequence[tuple[float, float]], x: float) -> float:
    """Linear interpolation over (x, y) pairs sorted by x; clamped at both ends."""
    xs = [p[0] for p in profile]
    if x <= xs[0]:
        return profile[0][1]
    if x >= xs[-1]:
        return profile[-1][1]
    i = bisect_right(xs, x) - 1
    (x1, y1), (x2, y2) = profile[i], profile[i + 1]
    return y1 if x2 == x1 else y1 + (x - x1) / (x2 - x1) * (y2 - y1)


def _segment_distance(p: Point, a: Point, b: Point) -> float:
    """Planar distance in degrees from p to segment a–b."""
    (py, px), (ay, ax), (by, bx) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _douglas_peucker(points: Sequence[Point], tolerance: float) -> list[Point]:
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        max_dist, index = 0.0, -1
        for i in range(start + 1, end):
            dist = _segment_distance(points[i], points[start], points[end])
            if dist > max_dist:
                max_dist, index = dist, i
        if index != -1 and max_dist > tolerance:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, kept in zip(points, keep, strict=True) if kept]


def simplify(
    points: Sequence[Point], max_points: int = 2000, tolerance: float = 0.0005
) -> list[Point]:
    """Douglas–Peucker at `tolerance` degrees, doubled until at most `max_points` remain."""
    result = list(points)
    if len(result) <= 2:
        return result
    result = _douglas_peucker(result, tolerance)
    while len(result) > max_points:
        tolerance *= 2
        result = _douglas_peucker(result, tolerance)
    return result


def split_by_ranges(
    points: Sequence[Point], cum: Sequence[float], ranges: Sequence[tuple[float, float]]
) -> list[list[Point]]:
    """One sub-polyline per (start, end) distance range, with interpolated end points."""
    pieces = []
    for start, end in ranges:
        piece = [point_at(points, cum, start)]
        i = bisect_right(cum, start)
        while i < len(points) and cum[i] < end:
            piece.append(points[i])
            i += 1
        piece.append(point_at(points, cum, end))
        pieces.append(piece)
    return pieces


def bbox(points: Iterable[Point]) -> list[float]:
    """[min_lon, min_lat, max_lon, max_lat]."""
    lats, lons = zip(*points, strict=True)
    return [min(lons), min(lats), max(lons), max(lats)]


def snap(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def snap_point(point: Point, step: float) -> Point:
    return (snap(point[0], step), snap(point[1], step))


def corridor_cells(
    polylines: Iterable[Sequence[Point]], step: float, *, ring: bool = True
) -> set[Point]:
    """Lattice cells along every polyline (walked every step × 34.5 miles), plus their 8
    neighbours when `ring`."""
    offsets = (-1, 0, 1) if ring else (0,)
    cells: set[Point] = set()
    for line in polylines:
        for lat, lon in sample_polyline(line, step * MILES_PER_DEGREE_LAT / 2):
            row, col = round(lat / step), round(lon / step)
            for dr in offsets:
                for dc in offsets:
                    cells.add((round((row + dr) * step, 6), round((col + dc) * step, 6)))
    return cells


def weather_grid(polylines: Sequence[Sequence[Point]]) -> tuple[float, set[Point]]:
    """The finest spacing whose corridor has at most MAX_WEATHER_CELLS cells.

    At each spacing the corridor with the 8-neighbour ring is tried first, then without it (only
    the cells the routes pass through). If nothing fits: the coarsest spacing without the ring.
    """
    for step in GRID_STEPS_DEG:
        for ring in (True, False):
            cells = corridor_cells(polylines, step, ring=ring)
            if len(cells) <= MAX_WEATHER_CELLS:
                return step, cells
    return step, cells


def grid_cell(point: Point, step: float, cells: Collection[Point]) -> Point:
    """The point's own lattice cell when it is in `cells`, else the nearest cell of `cells`."""
    cell = snap_point(point, step)
    if cell in cells:
        return cell
    return min(cells, key=lambda c: haversine_miles(c, point))


class _BucketIndex:
    """Points bucketed on a lat/lon grid for radius queries."""

    def __init__(self, points: Iterable[Point], bucket_deg: float = 0.05) -> None:
        self.bucket_deg = bucket_deg
        self.buckets: dict[tuple[int, int], list[Point]] = defaultdict(list)
        for p in points:
            self.buckets[self._key(p)].append(p)

    def _key(self, p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / self.bucket_deg), math.floor(p[1] / self.bucket_deg))

    def has_point_within(self, p: Point, radius_miles: float) -> bool:
        row, col = self._key(p)
        lat_span = math.ceil(radius_miles / MILES_PER_DEGREE_LAT / self.bucket_deg)
        miles_per_deg_lon = MILES_PER_DEGREE_LAT * max(math.cos(math.radians(p[0])), 0.01)
        lon_span = math.ceil(radius_miles / miles_per_deg_lon / self.bucket_deg)
        for dr in range(-lat_span, lat_span + 1):
            for dc in range(-lon_span, lon_span + 1):
                for q in self.buckets.get((row + dr, col + dc), ()):
                    if haversine_miles(p, q) <= radius_miles:
                        return True
        return False


def overlap_ratio(
    line: Sequence[Point], other: Sequence[Point], radius_miles: float = 1.0
) -> float:
    """Share of `line`'s points sampled every mile that lie within `radius_miles` of `other`."""
    samples = sample_polyline(line, 1.0)
    index = _BucketIndex(sample_polyline(other, radius_miles / 4))
    near = sum(1 for p in samples if index.has_point_within(p, radius_miles))
    return near / len(samples)


def detour_waypoints(points: Sequence[Point]) -> list[Point]:
    """Waypoints off the route's midpoint: left D, right D, left 1.6 D, right 1.6 D.

    D = clamp(15 % of the route length, 20, 150) miles, perpendicular to the bearing from the
    45 % point to the 55 % point.
    """
    cum = cumulative_miles(points)
    total = cum[-1]
    mid = point_at(points, cum, 0.5 * total)
    heading = bearing_degrees(
        point_at(points, cum, 0.45 * total), point_at(points, cum, 0.55 * total)
    )
    offset = min(max(0.15 * total, 20.0), 150.0)
    left, right = heading - 90, heading + 90
    return [
        destination_point(mid, left, offset),
        destination_point(mid, right, offset),
        destination_point(mid, left, 1.6 * offset),
        destination_point(mid, right, 1.6 * offset),
    ]
