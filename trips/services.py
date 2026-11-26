"""Trip planning: routes, checkpoints, weather, risk, ranking and the heatmap. Stateless."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone

from trips import geo
from trips.clients import open_meteo, tomtom
from trips.clients.tomtom import Place, ProviderRoute
from trips.exceptions import ForecastUnavailable, NoRouteFound, TripTooLong
from trips.recommendation import RankInput, coverage, rank, summarize
from trips.risk import RiskLevel, classify
from trips.weather import ForecastRangeError, HourlySeries, Weather, weather_at

logger = logging.getLogger(__name__)

MAX_TRIP_MILES = 3000
MAX_ROUTES = 3
HEATMAP_HOURS = 49  # frames 0–48 h from the departure hour
# TomTom refuses a departAt in the past; the routing calls take up to a minute, so departures
# closer than this are sent as "now" (ETAs still count from the requested departure).
DEPART_NOW_MARGIN = timedelta(minutes=2)


@dataclass(frozen=True)
class Location:
    label: str
    lat: float
    lon: float


@dataclass(frozen=True)
class CheckpointRisk:
    level: str
    score: int
    drivers: list[str]


@dataclass(frozen=True)
class Checkpoint:
    index: int
    distance_miles: float
    lat: float
    lon: float
    eta: datetime
    weather: Weather
    risk: CheckpointRisk


@dataclass(frozen=True)
class Segment:
    level: str
    coordinates: list[list[float]]  # [lon, lat] pairs


@dataclass(frozen=True)
class RiskSummary:
    max_level: str
    average_risk: float
    miles_by_level: dict[str, float]


@dataclass(frozen=True)
class PlannedRoute:
    id: str
    name: str
    rank: int
    source: str
    distance_miles: float
    duration_seconds: int
    arrival_time: datetime
    bbox: list[float]
    risk_summary: RiskSummary
    segments: list[Segment]
    checkpoints: list[Checkpoint]


@dataclass(frozen=True)
class HeatmapCell:
    lat: float
    lon: float
    risk: list[int]


@dataclass(frozen=True)
class Heatmap:
    start_time: datetime
    hours: int
    cell_size_deg: float
    cells: list[HeatmapCell]


@dataclass(frozen=True)
class TripPlan:
    departure_time: datetime
    load_weight_lb: int
    checkpoint_interval_miles: int
    recommended_route_id: str | None
    recommendation_reason: str
    no_safe_route: bool
    routes_note: str | None
    routes: list[PlannedRoute]
    heatmap: Heatmap


@dataclass(frozen=True)
class _Route:
    source: str  # "provider" | "detour"
    provider: ProviderRoute
    simplified: list[geo.Point]

    @property
    def miles(self) -> float:
        return self.provider.length_m / geo.METERS_PER_MILE


@dataclass(frozen=True)
class _Stop:
    distance_miles: float
    point: geo.Point
    eta: datetime


def place_search(*, query: str) -> list[Place]:
    return tomtom.search_places(query)


def trip_plan(
    *,
    origin: Location,
    destination: Location,
    departure_time: datetime,
    load_weight_lb: int,
    interval_miles: int,
) -> TripPlan:
    departure = departure_time.astimezone(dt_timezone.utc).replace(microsecond=0)
    depart_at = departure if departure > timezone.now() + DEPART_NOW_MARGIN else None
    routes, routes_note = _find_routes(
        (origin.lat, origin.lon), (destination.lat, destination.lon), depart_at
    )
    stops = [_stops(route, departure, interval_miles) for route in routes]

    step, cells = geo.weather_grid([route.simplified for route in routes])
    start_hour = departure.replace(minute=0, second=0)
    latest_eta = max(stop.eta for route_stops in stops for stop in route_stops)
    end_hour = max(start_hour + timedelta(hours=HEATMAP_HOURS - 1), _ceil_hour(latest_eta))
    forecast = open_meteo.fetch_hourly(cells, start_hour, end_hour)

    evaluated = {}  # route id → (route, checkpoints, metrics, segments), in generation order
    for i, (route, route_stops) in enumerate(zip(routes, stops, strict=True)):
        checkpoints = [
            _checkpoint(index, stop, forecast, step, load_weight_lb)
            for index, stop in enumerate(route_stops)
        ]
        levels = [RiskLevel(c.risk.score) for c in checkpoints]
        distances = [stop.distance_miles for stop in route_stops]
        metrics = summarize(distances, levels, route.miles)
        segments = _segments(route, distances, levels)
        evaluated[f"route-{chr(ord('a') + i)}"] = (route, checkpoints, metrics, segments)

    ranked = rank(
        [
            RankInput(route_id, metrics, route.provider.travel_time_s)
            for route_id, (route, _, metrics, _) in evaluated.items()
        ]
    )
    planned = []
    for position, route_id in enumerate(ranked.order, start=1):
        route, checkpoints, metrics, segments = evaluated[route_id]
        planned.append(
            PlannedRoute(
                id=route_id,
                name=f"Route {route_id[-1].upper()}",
                rank=position,
                source=route.source,
                distance_miles=round(route.miles, 1),
                duration_seconds=route.provider.travel_time_s,
                arrival_time=departure + timedelta(seconds=route.provider.travel_time_s),
                bbox=[round(v, 5) for v in geo.bbox(route.provider.points)],
                risk_summary=RiskSummary(
                    max_level=metrics.max_level.api_name,
                    average_risk=round(metrics.average_risk, 2),
                    miles_by_level={
                        level.api_name: round(miles, 1)
                        for level, miles in metrics.miles_by_level.items()
                    },
                ),
                segments=segments,
                checkpoints=checkpoints,
            )
        )

    logger.info(
        "trip planned",
        extra={"routes": len(planned), "grid_cells": len(cells), "grid_step": step},
    )
    return TripPlan(
        departure_time=departure,
        load_weight_lb=load_weight_lb,
        checkpoint_interval_miles=interval_miles,
        recommended_route_id=ranked.recommended_route_id,
        recommendation_reason=ranked.reason,
        no_safe_route=ranked.no_safe_route,
        routes_note=routes_note,
        routes=planned,
        heatmap=_heatmap(forecast, start_hour, step, load_weight_lb),
    )


def _find_routes(
    origin: geo.Point, destination: geo.Point, depart_at: datetime | None
) -> tuple[list[_Route], str | None]:
    """TomTom's up to 3 native truck routes."""
    provider = tomtom.calculate_routes(origin, destination, depart_at, MAX_ROUTES - 1)
    if not provider:
        raise NoRouteFound()
    if provider[0].length_m / geo.METERS_PER_MILE > MAX_TRIP_MILES:
        raise TripTooLong()
    routes = [
        _Route("provider", r, geo.simplify(r.points))
        for r in provider
        if r.length_m / geo.METERS_PER_MILE <= MAX_TRIP_MILES
    ]

    if len(routes) == MAX_ROUTES:
        return routes, None
    if len(routes) == 1:
        return routes, "Only 1 route was found."
    return routes, f"Only {len(routes)} routes were found."


def _stops(route: _Route, departure: datetime, interval_miles: int) -> list[_Stop]:
    """Checkpoint positions and ETAs; polyline distances are scaled to TomTom's route length."""
    points = route.provider.points
    cum = geo.cumulative_miles(points)
    scale = route.miles / cum[-1] if cum[-1] > 0 else 1.0
    cum = [c * scale for c in cum]
    stops = []
    for distance in geo.checkpoint_distances(route.miles, interval_miles):
        seconds = geo.interpolate(route.provider.time_profile, distance * geo.METERS_PER_MILE)
        stops.append(
            _Stop(
                distance_miles=distance,
                point=geo.point_at(points, cum, distance),
                eta=departure + timedelta(seconds=round(seconds)),
            )
        )
    return stops


def _checkpoint(
    index: int,
    stop: _Stop,
    forecast: dict[geo.Point, HourlySeries],
    step: float,
    load_weight_lb: int,
) -> Checkpoint:
    weather = _weather(forecast[geo.grid_cell(stop.point, step, forecast)], stop.eta)
    level, drivers = classify(weather, load_weight_lb)
    return Checkpoint(
        index=index,
        distance_miles=round(stop.distance_miles, 1),
        lat=round(stop.point[0], 5),
        lon=round(stop.point[1], 5),
        eta=stop.eta,
        weather=Weather(
            wind_mph=round(weather.wind_mph, 1),
            gust_mph=round(weather.gust_mph, 1),
            rain_in_hr=round(weather.rain_in_hr, 3),
            snow_in_hr=round(weather.snow_in_hr, 3),
        ),
        risk=CheckpointRisk(level=level.api_name, score=int(level), drivers=drivers),
    )


def _segments(route: _Route, distances: list[float], levels: list[RiskLevel]) -> list[Segment]:
    """The simplified line split at checkpoint coverage boundaries, equal neighbours merged."""
    ranges: list[tuple[float, float, RiskLevel]] = []
    for (start, end), level in zip(coverage(distances, route.miles), levels, strict=True):
        if ranges and ranges[-1][2] == level:
            ranges[-1] = (ranges[-1][0], end, level)
        else:
            ranges.append((start, end, level))

    line = route.simplified
    cum = geo.cumulative_miles(line)
    scale = route.miles / cum[-1] if cum[-1] > 0 else 1.0
    pieces = geo.split_by_ranges(line, [c * scale for c in cum], [(s, e) for s, e, _ in ranges])
    return [
        Segment(level.api_name, [[round(lon, 5), round(lat, 5)] for lat, lon in piece])
        for piece, (_, _, level) in zip(pieces, ranges, strict=True)
    ]


def _heatmap(
    forecast: dict[geo.Point, HourlySeries], start_hour: datetime, step: float, load: int
) -> Heatmap:
    frames = [start_hour + timedelta(hours=h) for h in range(HEATMAP_HOURS)]
    cells = [
        HeatmapCell(
            lat=lat,
            lon=lon,
            risk=[int(classify(_weather(forecast[(lat, lon)], t), load)[0]) for t in frames],
        )
        for lat, lon in sorted(forecast)
    ]
    return Heatmap(start_time=start_hour, hours=HEATMAP_HOURS, cell_size_deg=step, cells=cells)


def _weather(series: HourlySeries, moment: datetime) -> Weather:
    try:
        return weather_at(series, moment)
    except ForecastRangeError:
        raise ForecastUnavailable() from None


def _ceil_hour(moment: datetime) -> datetime:
    floor = moment.replace(minute=0, second=0, microsecond=0)
    return floor if floor == moment else floor + timedelta(hours=1)
