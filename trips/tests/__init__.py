"""Shared fakes for the trips tests. No test talks to the network."""

import json
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from trips import geo
from trips.clients.tomtom import ProviderRoute
from trips.weather import HourlySeries

FIXTURES = Path(__file__).parent / "fixtures"

ORIGIN = (40.0, -90.0)
DESTINATION = (40.0, -88.0)  # ~106 miles east of ORIGIN
NORTH_VIA = (40.5, -89.0)
SOUTH_VIA = (39.5, -89.0)
SPEED_MPH = 50


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def make_route(*stops: geo.Point, points_per_leg: int = 100, length_scale: float = 1.0):
    """A straight-legged route through `stops`, driven at SPEED_MPH."""
    points = [stops[0]]
    for (lat1, lon1), (lat2, lon2) in pairwise(stops):
        for k in range(1, points_per_leg + 1):
            t = k / points_per_leg
            points.append((lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)))
    miles = geo.cumulative_miles(points)[-1] * length_scale
    travel_s = round(miles / SPEED_MPH * 3600)
    length_m = miles * geo.METERS_PER_MILE
    departure = datetime(2026, 11, 26, 14, tzinfo=timezone.utc)
    return ProviderRoute(
        points=points,
        length_m=length_m,
        travel_time_s=travel_s,
        departure=departure,
        arrival=departure + timedelta(seconds=travel_s),
        time_profile=[(0.0, 0.0), (length_m, float(travel_s))],
    )


def make_series(
    start: datetime,
    hours: int,
    wind: float = 10.0,
    gust: float = 15.0,
    rain: float = 0.0,
    showers: float = 0.0,
    snow: float = 0.0,
) -> HourlySeries:
    return HourlySeries(
        start=start,
        wind_mph=(wind,) * hours,
        gust_mph=(gust,) * hours,
        rain_in=(rain,) * hours,
        showers_in=(showers,) * hours,
        snow_in=(snow,) * hours,
    )


def fake_routing(provider_routes, detour=None):
    """Stands in for tomtom.calculate_routes; `detour(via)` answers the waypoint calls."""
    calls = []

    def calculate_routes(origin, destination, depart_at, max_alternatives, via=None):
        calls.append({"depart_at": depart_at, "max_alternatives": max_alternatives, "via": via})
        if via is None:
            return provider_routes
        if detour is None:
            return [make_route(origin, via, destination)]
        return detour(via)

    calculate_routes.calls = calls
    return calculate_routes


def fake_forecast(weather_for=None):
    """Stands in for open_meteo.fetch_hourly; `weather_for(cell)` → make_series keyword values."""
    calls = []

    def fetch_hourly(cells, start_hour, end_hour):
        cells = list(cells)
        calls.append({"cells": cells, "start_hour": start_hour, "end_hour": end_hour})
        hours = int((end_hour - start_hour).total_seconds() // 3600) + 1
        return {
            cell: make_series(start_hour, hours, **(weather_for(cell) if weather_for else {}))
            for cell in cells
        }

    fetch_hourly.calls = calls
    return fetch_hourly
