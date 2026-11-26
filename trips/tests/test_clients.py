import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from django.core.cache import cache, caches

from trips import geo
from trips.clients import http_client, open_meteo, tomtom
from trips.exceptions import (
    ROUTING_UNAVAILABLE,
    SEARCH_UNAVAILABLE,
    WEATHER_RATE_LIMITED,
    WEATHER_UNAVAILABLE,
    ForecastUnavailable,
    NoRouteFound,
    UpstreamUnavailable,
)
from trips.tests import load_fixture

KEY = "secret-test-key-123"
START = datetime(2026, 11, 26, 14, tzinfo=timezone.utc)
END = START + timedelta(hours=2)


@pytest.fixture(autouse=True)
def _isolate(settings):
    settings.TOMTOM_API_KEY = KEY
    cache.clear()
    caches[open_meteo.CACHE_ALIAS].clear()


def mock_http(monkeypatch, module, handler):
    """Route the module's HTTP calls to `handler`; returns the list of requests made."""
    requests = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        module, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(recording))
    )
    return requests


def params(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


def assert_key_not_leaked(exc: Exception, caplog) -> None:
    assert KEY not in str(exc.detail)
    assert KEY not in caplog.text


def test_http_client_has_the_configured_timeout(settings):
    settings.HTTP_TIMEOUT_SECONDS = 7
    with http_client() as client:
        assert client.timeout.read == 7
        assert client.timeout.connect == 7


# --- TomTom routing ----------------------------------------------------------------------------


def test_calculate_routes_parses_routes(monkeypatch):
    mock_http(
        monkeypatch, tomtom, lambda r: httpx.Response(200, json=load_fixture("tomtom_routes.json"))
    )

    first, second = tomtom.calculate_routes((41.8781, -87.6298), (41.8, -87.75), None, 2)

    assert first.points == [(41.8781, -87.6298), (41.85, -87.65), (41.82, -87.7), (41.8, -87.75)]
    assert first.length_m == 20000
    assert first.travel_time_s == 1200
    assert first.departure == datetime(2026, 11, 26, 14, tzinfo=timezone.utc)
    assert first.arrival == datetime(2026, 11, 26, 14, 20, tzinfo=timezone.utc)
    assert first.time_profile == [
        (0.0, 0.0),
        (0.0, 0.0),
        (8000.0, 600.0),
        (20000.0, 1200.0),
        (20000.0, 1200.0),
    ]
    assert second.time_profile == [(0.0, 0.0), (23000.0, 1500.0)]  # no guidance


def test_calculate_routes_sends_truck_parameters(monkeypatch):
    requests = mock_http(
        monkeypatch, tomtom, lambda r: httpx.Response(200, json=load_fixture("tomtom_routes.json"))
    )
    depart = datetime(2026, 11, 26, 8, tzinfo=timezone(timedelta(hours=-6)))

    tomtom.calculate_routes((41.8781, -87.6298), (39.7392, -104.9903), None, 2)
    tomtom.calculate_routes((41.8781, -87.6298), (39.7392, -104.9903), depart, 0, via=(40.5, -95.0))

    assert requests[0].url.path == (
        "/routing/1/calculateRoute/41.878100,-87.629800:39.739200,-104.990300/json"
    )
    sent = params(requests[0])
    assert sent == {
        "key": KEY,
        "travelMode": "truck",
        "vehicleCommercial": "true",
        "traffic": "true",
        "routeType": "fastest",
        "departAt": "now",
        "maxAlternatives": "2",
        "instructionsType": "coded",
    }
    assert ":40.500000,-95.000000:" in requests[1].url.path
    assert params(requests[1])["departAt"] == "2026-11-26T14:00:00+00:00"
    assert params(requests[1])["maxAlternatives"] == "0"


@pytest.mark.parametrize("code", ["NO_ROUTE_FOUND", "MAP_MATCHING_FAILURE"])
def test_no_route_is_422(monkeypatch, code):
    body = {"formatVersion": "0.0.12", "detailedError": {"code": code, "message": "No route"}}
    mock_http(monkeypatch, tomtom, lambda r: httpx.Response(400, json=body))

    with pytest.raises(NoRouteFound) as exc:
        tomtom.calculate_routes((41.0, -87.0), (39.0, -104.0), None, 2)
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"detailedError": {"code": "INVALID_REQUEST"}}),
        httpx.Response(500, text="oops"),
        httpx.Response(429, text="Too Many Requests"),
        httpx.Response(200, json={"routes": [{"summary": {}}]}),
        httpx.Response(200, text="not json"),
    ],
)
def test_other_routing_errors_are_502(monkeypatch, caplog, response):
    mock_http(monkeypatch, tomtom, lambda r: response)

    with pytest.raises(UpstreamUnavailable) as exc:
        tomtom.calculate_routes((41.0, -87.0), (39.0, -104.0), None, 2)
    assert exc.value.status_code == 502
    assert exc.value.detail == ROUTING_UNAVAILABLE
    assert_key_not_leaked(exc.value, caplog)


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_502_and_logged_without_the_key(monkeypatch, caplog, status):
    mock_http(monkeypatch, tomtom, lambda r: httpx.Response(status, text=f"Invalid key {KEY}"))

    with caplog.at_level(logging.INFO), pytest.raises(UpstreamUnavailable) as exc:
        tomtom.calculate_routes((41.0, -87.0), (39.0, -104.0), None, 2)
    assert exc.value.detail == ROUTING_UNAVAILABLE
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert_key_not_leaked(exc.value, caplog)


def test_timeout_is_502_without_the_key(monkeypatch, caplog):
    def timeout(request):
        raise httpx.ReadTimeout(f"timed out: {request.url}", request=request)

    mock_http(monkeypatch, tomtom, timeout)

    with caplog.at_level(logging.DEBUG), pytest.raises(UpstreamUnavailable) as exc:
        tomtom.calculate_routes((41.0, -87.0), (39.0, -104.0), None, 2)
    assert exc.value.detail == ROUTING_UNAVAILABLE
    assert_key_not_leaked(exc.value, caplog)


def test_missing_key_is_502_and_makes_no_call(monkeypatch, settings, caplog):
    settings.TOMTOM_API_KEY = ""
    requests = mock_http(monkeypatch, tomtom, lambda r: httpx.Response(200, json={}))

    with pytest.raises(UpstreamUnavailable) as exc:
        tomtom.calculate_routes((41.0, -87.0), (39.0, -104.0), None, 2)
    assert exc.value.detail == ROUTING_UNAVAILABLE
    assert requests == []
    assert "TOMTOM_API_KEY is not set" in caplog.text


# --- TomTom search -----------------------------------------------------------------------------


def test_search_places_parses_labels(monkeypatch):
    requests = mock_http(
        monkeypatch, tomtom, lambda r: httpx.Response(200, json=load_fixture("tomtom_search.json"))
    )

    places = tomtom.search_places("chicago/il")

    assert places == [
        tomtom.Place("Chicago, IL", 41.8781, -87.6298),
        tomtom.Place("Pilot Travel Center, 1205 Houbolt Rd, Joliet, IL 60431", 41.5101, -88.1823),
    ]
    assert requests[0].url.raw_path.startswith(b"/search/2/search/chicago%2Fil.json?")
    assert params(requests[0]) == {
        "key": KEY,
        "typeahead": "true",
        "limit": "5",
        "language": "en-US",
    }


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, text="down"), httpx.Response(200, json={"results": [{"address": {}}]})],
)
def test_search_errors_are_502(monkeypatch, caplog, response):
    mock_http(monkeypatch, tomtom, lambda r: response)

    with pytest.raises(UpstreamUnavailable) as exc:
        tomtom.search_places("chicago")
    assert exc.value.detail == SEARCH_UNAVAILABLE
    assert_key_not_leaked(exc.value, caplog)


# --- Open-Meteo --------------------------------------------------------------------------------


def location_body(count: int) -> dict:
    return {
        "latitude": 40.0,
        "longitude": -90.0,
        "hourly": {
            "time": ["2026-11-26T14:00", "2026-11-26T15:00", "2026-11-26T16:00"],
            "wind_speed_10m": [float(count), 2.0, 3.0],
            "wind_gusts_10m": [4.0, 5.0, 6.0],
            "rain": [0.0, 0.1, 0.2],
            "showers": [0.0, 0.0, 0.01],
            "snowfall": [0.0, 0.0, 0.0],
        },
    }


def by_location_count(request: httpx.Request) -> httpx.Response:
    """A multi-location answer sized to the request; the first value tags the location's index."""
    count = len(params(request)["latitude"].split(","))
    if count == 1:
        return httpx.Response(200, json=location_body(0))
    return httpx.Response(200, json=[location_body(i) for i in range(count)])


def test_fetch_hourly_maps_a_list_by_request_order(monkeypatch):
    requests = mock_http(
        monkeypatch,
        open_meteo,
        lambda r: httpx.Response(200, json=load_fixture("open_meteo_multi.json")),
    )
    cells = [(41.9, -87.6), (41.8, -87.8)]

    result = open_meteo.fetch_hourly(cells, START, END)

    assert result[(41.9, -87.6)].wind_mph == (12.4, 26.0, 36.2)
    assert result[(41.8, -87.8)].snow_in == (0.4, 0.6, 1.1)
    assert result[(41.9, -87.6)].start == START
    assert params(requests[0]) == {
        "latitude": "41.9000,41.8000",
        "longitude": "-87.6000,-87.8000",
        "hourly": "wind_speed_10m,wind_gusts_10m,rain,showers,snowfall",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "GMT",
        "start_hour": "2026-11-26T14:00",
        "end_hour": "2026-11-26T16:00",
    }


def test_fetch_hourly_accepts_a_single_location_object(monkeypatch):
    mock_http(monkeypatch, open_meteo, by_location_count)

    result = open_meteo.fetch_hourly([(40.0, -90.0)], START, END)

    assert result[(40.0, -90.0)].rain_in == (0.0, 0.1, 0.2)


def test_fetch_hourly_batches_at_most_100_locations(monkeypatch):
    requests = mock_http(monkeypatch, open_meteo, by_location_count)
    cells = [(40.0 + i * 0.1, -90.0) for i in range(250)]

    result = open_meteo.fetch_hourly(cells, START, END)

    sizes = sorted(len(params(r)["latitude"].split(",")) for r in requests)
    assert sizes == [50, 100, 100]
    assert len(result) == 250
    assert result[cells[0]].wind_mph[0] == 0.0  # each batch is mapped by its own order
    assert result[cells[149]].wind_mph[0] == 49.0


def test_cache_hit_skips_http(monkeypatch):
    requests = mock_http(monkeypatch, open_meteo, by_location_count)
    cells = [(40.0, -90.0), (40.1, -90.0)]

    first = open_meteo.fetch_hourly(cells, START, END)
    again = open_meteo.fetch_hourly(cells, START, END)
    assert len(requests) == 1
    assert again == first

    open_meteo.fetch_hourly([*cells, (40.2, -90.0)], START, END)
    assert len(requests) == 2
    assert params(requests[1])["latitude"] == "40.2000"  # only the miss is fetched

    open_meteo.fetch_hourly(cells, START, END + timedelta(hours=1))  # another window
    assert len(requests) == 3


def test_a_full_grid_stays_cached_without_evicting_the_rate_limit_history(monkeypatch):
    requests = mock_http(monkeypatch, open_meteo, by_location_count)
    cache.set("throttle_trip_plan_127.0.0.1", [1.0], 60)  # what ScopedRateThrottle stores
    cells = [(40.0 + i * 0.1, -90.0) for i in range(geo.MAX_WEATHER_CELLS)]  # the largest grid

    open_meteo.fetch_hourly(cells, START, END)
    open_meteo.fetch_hourly(cells, START, END)

    assert cache.get("throttle_trip_plan_127.0.0.1") == [1.0]
    assert len(requests) == 2  # 2 batches the first time; every cell cached the second time


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": True, "reason": "Parameter 'end_hour' is out of range"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json=[location_body(0)]),  # 1 location for 2 requested
        httpx.Response(200, json=[{"hourly": {"time": []}}, {}]),
    ],
)
def test_weather_errors_are_502(monkeypatch, response):
    mock_http(monkeypatch, open_meteo, lambda r: response)

    with pytest.raises(UpstreamUnavailable) as exc:
        open_meteo.fetch_hourly([(40.0, -90.0), (40.1, -90.0)], START, END)
    assert exc.value.status_code == 502
    assert exc.value.detail == WEATHER_UNAVAILABLE


RATE_LIMITED_BODY = {
    "error": True,
    "reason": "Minutely API request limit exceeded. Please try again in one minute.",
}


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (
            httpx.Response(429, json=RATE_LIMITED_BODY),
            "Weather data limit reached. Try again in a minute.",
        ),
        (httpx.Response(500, text="oops"), "Weather service is unavailable. Try again."),
    ],
)
def test_rate_limit_has_its_own_message_and_stays_502(monkeypatch, response, detail):
    mock_http(monkeypatch, open_meteo, lambda r: response)

    with pytest.raises(UpstreamUnavailable) as exc:
        open_meteo.fetch_hourly([(40.0, -90.0)], START, END)
    assert exc.value.status_code == 502
    assert exc.value.detail == detail


def test_rate_limited_batches_are_logged_once(monkeypatch, caplog):
    requests = mock_http(
        monkeypatch, open_meteo, lambda r: httpx.Response(429, json=RATE_LIMITED_BODY)
    )
    cells = [(40.0 + i * 0.1, -90.0) for i in range(250)]

    with caplog.at_level(logging.INFO), pytest.raises(UpstreamUnavailable) as exc:
        open_meteo.fetch_hourly(cells, START, END)

    assert len(requests) == 3  # every batch was answered 429
    assert exc.value.detail == WEATHER_RATE_LIMITED
    logged = [r for r in caplog.records if r.name == open_meteo.__name__]
    assert len(logged) == 1
    assert "HTTP 429" in logged[0].getMessage()
    assert "Minutely API request limit exceeded" in logged[0].getMessage()


def test_weather_timeout_is_502(monkeypatch):
    def timeout(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    mock_http(monkeypatch, open_meteo, timeout)

    with pytest.raises(UpstreamUnavailable):
        open_meteo.fetch_hourly([(40.0, -90.0)], START, END)


def test_missing_hourly_values_mean_no_forecast(monkeypatch):
    body = location_body(0)
    body["hourly"]["snowfall"][2] = None
    mock_http(monkeypatch, open_meteo, lambda r: httpx.Response(200, json=body))

    with pytest.raises(ForecastUnavailable) as exc:
        open_meteo.fetch_hourly([(40.0, -90.0)], START, END)
    assert exc.value.status_code == 422
