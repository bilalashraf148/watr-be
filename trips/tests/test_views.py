from datetime import timedelta

import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from trips.clients import open_meteo, tomtom
from trips.exceptions import (
    ROUTING_UNAVAILABLE,
    SEARCH_UNAVAILABLE,
    WEATHER_UNAVAILABLE,
    NoRouteFound,
    TripTooLong,
    UpstreamUnavailable,
)
from trips.tests import DESTINATION, NORTH_VIA, ORIGIN, fake_forecast, fake_routing, make_route
from trips.views import PlaceSearchView, TripPlanView

PLACES_URL = "/api/v1/places/"
PLAN_URL = "/api/v1/trips/plan/"


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    cache.clear()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def routing(monkeypatch):
    fake = fake_routing(
        [
            make_route(ORIGIN, DESTINATION),
            make_route(ORIGIN, NORTH_VIA, DESTINATION),
            make_route(ORIGIN, (ORIGIN[0] + 0.5, ORIGIN[1] - 0.5), DESTINATION),
        ]
    )
    monkeypatch.setattr(tomtom, "calculate_routes", fake)
    monkeypatch.setattr(open_meteo, "fetch_hourly", fake_forecast())
    return fake


def body(**overrides):
    data = {
        "origin": {"label": "Start", "lat": ORIGIN[0], "lon": ORIGIN[1]},
        "destination": {"label": "End", "lat": DESTINATION[0], "lon": DESTINATION[1]},
        "departure_time": (timezone.now() + timedelta(hours=2)).isoformat(),
        "load_weight_lb": 42000,
        "checkpoint_interval_miles": 25,
    }
    data.update(overrides)
    return data


# --- GET /api/v1/places/ -----------------------------------------------------------------------


def test_place_search_returns_results(client, monkeypatch):
    queries = []

    def search(query):
        queries.append(query)
        return [tomtom.Place("Chicago, IL", 41.8781, -87.6298)]

    monkeypatch.setattr(tomtom, "search_places", search)

    response = client.get(PLACES_URL, {"q": "  Chicago  "})

    assert response.status_code == 200
    assert response.json() == {
        "results": [{"label": "Chicago, IL", "lat": 41.8781, "lon": -87.6298}]
    }
    assert queries == ["Chicago"]  # trimmed


@pytest.mark.parametrize("params", [{}, {"q": "ab"}, {"q": "  ab  "}, {"q": "x" * 101}])
def test_place_search_validates_q(client, params):
    response = client.get(PLACES_URL, params)

    assert response.status_code == 400
    assert "q" in response.json()


def test_place_search_upstream_failure_is_502(client, monkeypatch):
    def down(query):
        raise UpstreamUnavailable(SEARCH_UNAVAILABLE)

    monkeypatch.setattr(tomtom, "search_places", down)

    response = client.get(PLACES_URL, {"q": "Chicago"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Place search is unavailable. Try again."}


# --- POST /api/v1/trips/plan/ ------------------------------------------------------------------

TRIP_FIELDS = {
    "departure_time",
    "load_weight_lb",
    "checkpoint_interval_miles",
    "recommended_route_id",
    "recommendation_reason",
    "no_safe_route",
    "routes_note",
    "routes",
    "heatmap",
}
ROUTE_FIELDS = {
    "id",
    "name",
    "rank",
    "source",
    "distance_miles",
    "duration_seconds",
    "arrival_time",
    "bbox",
    "risk_summary",
    "segments",
    "checkpoints",
}
CHECKPOINT_FIELDS = {"index", "distance_miles", "lat", "lon", "eta", "weather", "risk"}


def test_trip_plan_returns_the_contract_shape(client, routing):
    departure = (timezone.now() + timedelta(days=1)).replace(hour=8, minute=0, second=0)
    response = client.post(
        PLAN_URL,
        body(departure_time=departure.astimezone(timezone.get_fixed_timezone(-360)).isoformat()),
        format="json",
    )

    assert response.status_code == 200
    data = response.json()
    assert set(data) == TRIP_FIELDS
    assert data["departure_time"] == departure.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    assert data["load_weight_lb"] == 42000
    assert data["checkpoint_interval_miles"] == 25
    assert data["recommended_route_id"] == "route-a"
    assert data["no_safe_route"] is False
    assert data["routes_note"] is None
    assert [r["rank"] for r in data["routes"]] == [1, 2, 3]

    route = data["routes"][0]
    assert set(route) == ROUTE_FIELDS
    assert route["source"] in {"provider", "detour"}
    assert route["arrival_time"].endswith("Z")
    assert len(route["bbox"]) == 4
    assert set(route["risk_summary"]) == {"max_level", "average_risk", "miles_by_level"}
    assert list(route["risk_summary"]["miles_by_level"]) == [
        "low",
        "moderate",
        "high",
        "severe",
        "no_travel",
    ]
    assert set(route["segments"][0]) == {"level", "coordinates"}
    assert route["segments"][0]["coordinates"][0] == [ORIGIN[1], ORIGIN[0]]  # [lon, lat]

    checkpoint = route["checkpoints"][0]
    assert set(checkpoint) == CHECKPOINT_FIELDS
    assert checkpoint["eta"] == data["departure_time"]
    assert set(checkpoint["weather"]) == {"wind_mph", "gust_mph", "rain_in_hr", "snow_in_hr"}
    assert checkpoint["risk"] == {"level": "low", "score": 0, "drivers": []}

    heatmap = data["heatmap"]
    assert set(heatmap) == {"start_time", "hours", "cell_size_deg", "cells"}
    assert heatmap["hours"] == 49
    assert heatmap["start_time"].endswith(":00:00Z")
    assert set(heatmap["cells"][0]) == {"lat", "lon", "risk"}
    assert all(len(cell["risk"]) == 49 for cell in heatmap["cells"])


def test_trip_plan_passes_validated_values_to_the_service(client, routing):
    response = client.post(PLAN_URL, body(checkpoint_interval_miles="10"), format="json")

    assert response.json()["checkpoint_interval_miles"] == 10
    distances = [c["distance_miles"] for c in response.json()["routes"][0]["checkpoints"]]
    assert distances[:3] == [0, 10, 20]
    assert routing.calls[0]["depart_at"] is not None


def future(**delta) -> str:
    return (timezone.now() + timedelta(**delta)).isoformat()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"departure_time": "2026-11-26T08:00:00"}, "departure_time"),  # no offset
        ({"departure_time": "tomorrow"}, "departure_time"),
        ({"departure_time": future(minutes=-11)}, "departure_time"),
        ({"departure_time": future(days=7, hours=1)}, "departure_time"),  # margin for slow runs
        ({"load_weight_lb": 0}, "load_weight_lb"),
        ({"load_weight_lb": 80_001}, "load_weight_lb"),
        ({"load_weight_lb": 1.5}, "load_weight_lb"),
        ({"checkpoint_interval_miles": 30}, "checkpoint_interval_miles"),
        ({"origin": {"label": "Start", "lat": 90.1, "lon": -90}}, "origin"),
        ({"origin": {"label": "Start", "lat": 40, "lon": -180.1}}, "origin"),
        ({"origin": {"label": "x" * 201, "lat": 40, "lon": -90}}, "origin"),
        ({"destination": {"lat": 40, "lon": -88}}, "destination"),
        ({"destination": None}, "destination"),
    ],
)
def test_trip_plan_validation(client, routing, overrides, field):
    response = client.post(PLAN_URL, body(**overrides), format="json")

    assert response.status_code == 400
    assert field in response.json()
    assert routing.calls == []


def test_departure_messages(client, routing):
    late = client.post(PLAN_URL, body(departure_time=future(days=8)), format="json")
    naive = client.post(PLAN_URL, body(departure_time="2026-11-26T08:00:00"), format="json")

    assert late.json() == {"departure_time": ["Departure must be within the next 7 days."]}
    assert naive.json() == {
        "departure_time": ["Include a UTC offset, e.g. 2026-11-26T08:00:00-06:00."]
    }


def test_departure_within_the_grace_window_is_accepted(client, routing):
    response = client.post(PLAN_URL, body(departure_time=future(minutes=-9)), format="json")

    assert response.status_code == 200
    assert routing.calls[0]["depart_at"] is None


def test_origin_and_destination_must_be_a_mile_apart(client, routing):
    near = {"label": "Next door", "lat": ORIGIN[0] + 0.01, "lon": ORIGIN[1]}  # ~0.7 mile
    response = client.post(PLAN_URL, body(destination=near), format="json")

    assert response.status_code == 400
    assert response.json() == {
        "non_field_errors": ["Origin and destination must be at least 1 mile apart."]
    }


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (NoRouteFound(), 422, "No truck route was found between these places."),
        (TripTooLong(), 422, "Trips longer than 3,000 miles are not supported."),
        (
            UpstreamUnavailable(ROUTING_UNAVAILABLE),
            502,
            "Routing service is unavailable. Try again.",
        ),
    ],
)
def test_routing_errors_map_to_status_codes(client, monkeypatch, error, status, detail):
    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(tomtom, "calculate_routes", failing)

    response = client.post(PLAN_URL, body(), format="json")

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_weather_failure_is_502(client, routing, monkeypatch):
    def failing(*args, **kwargs):
        raise UpstreamUnavailable(WEATHER_UNAVAILABLE)

    monkeypatch.setattr(open_meteo, "fetch_hourly", failing)

    response = client.post(PLAN_URL, body(), format="json")

    assert response.status_code == 502
    assert response.json() == {"detail": "Weather service is unavailable. Try again."}


def test_trip_plan_response_is_gzipped(client, routing):
    response = client.post(PLAN_URL, body(), format="json", HTTP_ACCEPT_ENCODING="gzip")

    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"


# --- Access and throttling ---------------------------------------------------------------------


@pytest.mark.parametrize("view", [PlaceSearchView, TripPlanView])
def test_endpoints_are_public_and_throttled(view):
    assert view.authentication_classes == []
    assert view.permission_classes == [AllowAny]
    assert view.throttle_classes == [ScopedRateThrottle]


def test_throttle_scopes_and_rates():
    assert PlaceSearchView.throttle_scope == "places"
    assert TripPlanView.throttle_scope == "trip_plan"
    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] == {
        "places": "60/min",
        "trip_plan": "10/min",
    }


def test_trip_plan_post_needs_no_csrf_token_or_login(routing):
    response = APIClient(enforce_csrf_checks=True).post(PLAN_URL, body(), format="json")

    assert response.status_code == 200


def test_trip_plan_is_throttled_after_10_requests(client):
    statuses = [client.post(PLAN_URL, {}, format="json").status_code for _ in range(11)]

    assert statuses == [400] * 10 + [429]


def test_spoofed_forwarded_for_does_not_reset_the_throttle(client):
    statuses = [
        client.post(PLAN_URL, {}, format="json", HTTP_X_FORWARDED_FOR=f"10.0.0.{i}").status_code
        for i in range(11)
    ]

    assert statuses == [400] * 10 + [429]
