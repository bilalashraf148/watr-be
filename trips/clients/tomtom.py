"""TomTom Routing and Search APIs. The API key never appears in logs, errors or responses."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from django.conf import settings
from django.utils.dateparse import parse_datetime

from trips.clients import http_client
from trips.exceptions import (
    ROUTING_UNAVAILABLE,
    SEARCH_UNAVAILABLE,
    NoRouteFound,
    UpstreamUnavailable,
)
from trips.geo import Point

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tomtom.com"
NO_ROUTE_CODES = {"NO_ROUTE_FOUND", "MAP_MATCHING_FAILURE"}


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lon: float


@dataclass(frozen=True)
class ProviderRoute:
    points: list[Point]
    length_m: float
    travel_time_s: int
    departure: datetime
    arrival: datetime
    time_profile: list[tuple[float, float]]  # (route offset m, travel time s), sorted, 0 → end


def _api_key(unavailable: str) -> str:
    key = settings.TOMTOM_API_KEY
    if not key:
        logger.error("TOMTOM_API_KEY is not set")
        raise UpstreamUnavailable(unavailable)
    return key


def _get(path: str, params: dict[str, Any], unavailable: str) -> httpx.Response:
    params = {"key": _api_key(unavailable), **params}
    try:
        with http_client() as client:
            return client.get(f"{BASE_URL}{path}", params=params)
    except httpx.HTTPError as exc:  # message would carry the URL (and key): log the type only
        logger.warning("TomTom request failed: %s", type(exc).__name__)
        raise UpstreamUnavailable(unavailable) from None


def _check_status(response: httpx.Response, unavailable: str) -> None:
    if response.status_code in (401, 403):
        logger.error("TomTom rejected the API key (HTTP %s)", response.status_code)
        raise UpstreamUnavailable(unavailable)
    if response.status_code != 200:
        logger.warning("TomTom returned HTTP %s", response.status_code)
        raise UpstreamUnavailable(unavailable)


def search_places(query: str) -> list[Place]:
    response = _get(
        f"/search/2/search/{quote(query, safe='')}.json",
        {"typeahead": "true", "limit": 5, "language": "en-US"},
        SEARCH_UNAVAILABLE,
    )
    _check_status(response, SEARCH_UNAVAILABLE)
    try:
        places = []
        for result in response.json()["results"]:
            address = result["address"]["freeformAddress"]
            poi_name = (result.get("poi") or {}).get("name")
            places.append(
                Place(
                    label=f"{poi_name}, {address}" if poi_name else address,
                    lat=float(result["position"]["lat"]),
                    lon=float(result["position"]["lon"]),
                )
            )
        return places
    except (ValueError, KeyError, TypeError):
        logger.warning("TomTom search returned an unexpected body")
        raise UpstreamUnavailable(SEARCH_UNAVAILABLE) from None


def calculate_routes(
    origin: Point,
    destination: Point,
    depart_at: datetime | None,
    max_alternatives: int,
    via: Point | None = None,
) -> list[ProviderRoute]:
    """Truck routes origin → (via →) destination, fastest first. `depart_at=None` departs now."""
    stops = [origin, via, destination] if via else [origin, destination]
    locations = ":".join(f"{lat:.6f},{lon:.6f}" for lat, lon in stops)
    response = _get(
        f"/routing/1/calculateRoute/{locations}/json",
        {
            "travelMode": "truck",
            "vehicleCommercial": "true",
            "traffic": "true",
            "routeType": "fastest",
            "departAt": (
                depart_at.astimezone(timezone.utc).isoformat(timespec="seconds")
                if depart_at
                else "now"
            ),
            "maxAlternatives": max_alternatives,
            "instructionsType": "coded",
        },
        ROUTING_UNAVAILABLE,
    )
    if response.status_code == 400 and _error_code(response) in NO_ROUTE_CODES:
        raise NoRouteFound()
    _check_status(response, ROUTING_UNAVAILABLE)
    try:
        return [_parse_route(route) for route in response.json()["routes"]]
    except (ValueError, KeyError, TypeError, IndexError):
        logger.warning("TomTom routing returned an unexpected body")
        raise UpstreamUnavailable(ROUTING_UNAVAILABLE) from None


def _error_code(response: httpx.Response) -> str | None:
    try:
        return response.json()["detailedError"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def _parse_time(value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"not a datetime: {value!r}")
    return parsed


def _parse_route(route: dict[str, Any]) -> ProviderRoute:
    summary = route["summary"]
    length_m = float(summary["lengthInMeters"])
    travel_time_s = int(summary["travelTimeInSeconds"])

    points: list[Point] = []
    for leg in route["legs"]:
        leg_points = [(float(p["latitude"]), float(p["longitude"])) for p in leg["points"]]
        if points and leg_points and leg_points[0] == points[-1]:
            leg_points = leg_points[1:]  # legs share the waypoint
        points.extend(leg_points)
    if len(points) < 2:
        raise ValueError("route has fewer than 2 points")

    instructions = (route.get("guidance") or {}).get("instructions") or []
    profile = [(0.0, 0.0), (length_m, float(travel_time_s))]
    profile += [
        (float(i["routeOffsetInMeters"]), float(i["travelTimeInSeconds"])) for i in instructions
    ]
    return ProviderRoute(
        points=points,
        length_m=length_m,
        travel_time_s=travel_time_s,
        departure=_parse_time(summary["departureTime"]),
        arrival=_parse_time(summary["arrivalTime"]),
        time_profile=sorted(profile),
    )
