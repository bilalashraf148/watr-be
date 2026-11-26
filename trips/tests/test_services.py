import dataclasses
from datetime import timedelta
from itertools import pairwise

import pytest
from django.utils import timezone

from trips import geo, services
from trips.clients import open_meteo, tomtom
from trips.exceptions import ForecastUnavailable, NoRouteFound, TripTooLong
from trips.services import Location
from trips.tests import (
    DESTINATION,
    NORTH_VIA,
    ORIGIN,
    SOUTH_VIA,
    SPEED_MPH,
    fake_forecast,
    fake_routing,
    make_route,
)

START = Location("Start", *ORIGIN)
END = Location("End", *DESTINATION)


@pytest.fixture
def departure():
    return (timezone.now() + timedelta(days=1)).replace(minute=20, second=0, microsecond=0)


@pytest.fixture
def forecast(monkeypatch):
    fake = fake_forecast()
    monkeypatch.setattr(open_meteo, "fetch_hourly", fake)
    return fake


def use_routing(monkeypatch, provider_routes):
    fake = fake_routing(provider_routes)
    monkeypatch.setattr(tomtom, "calculate_routes", fake)
    return fake


def plan(departure, load=20_000, interval=25):
    return services.trip_plan(
        origin=START,
        destination=END,
        departure_time=departure,
        load_weight_lb=load,
        interval_miles=interval,
    )


def route_by_id(result, route_id):
    return next(r for r in result.routes if r.id == route_id)


def test_three_provider_routes(monkeypatch, forecast, departure):
    routing = use_routing(
        monkeypatch,
        [
            make_route(ORIGIN, DESTINATION),
            make_route(ORIGIN, NORTH_VIA, DESTINATION),
            make_route(ORIGIN, SOUTH_VIA, DESTINATION),
        ],
    )

    result = plan(departure)

    assert len(routing.calls) == 1
    assert routing.calls[0] == {"depart_at": departure, "max_alternatives": 2, "via": None}
    assert [r.id for r in result.routes] == ["route-a", "route-b", "route-c"]  # all equal: a first
    assert [r.name for r in result.routes] == ["Route A", "Route B", "Route C"]
    assert [r.rank for r in result.routes] == [1, 2, 3]
    assert {r.source for r in result.routes} == {"provider"}
    assert result.routes_note is None
    assert result.recommended_route_id == "route-a"
    assert result.recommendation_reason.startswith("Shortest travel time (2 h 7 min vs 2 h ")
    assert result.no_safe_route is False
    assert result.departure_time == departure
    assert len(forecast.calls) == 1  # one weather fetch serves every route and the heatmap


def test_two_provider_routes(monkeypatch, forecast, departure):
    routing = use_routing(
        monkeypatch, [make_route(ORIGIN, DESTINATION), make_route(ORIGIN, NORTH_VIA, DESTINATION)]
    )

    result = plan(departure)

    assert len(routing.calls) == 1
    assert [r.id for r in result.routes] == ["route-a", "route-b"]
    assert {r.source for r in result.routes} == {"provider"}
    assert result.routes_note == "Only 2 routes were found."


def test_one_provider_route(monkeypatch, forecast, departure):
    routing = use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    result = plan(departure)

    assert len(routing.calls) == 1
    assert [r.id for r in result.routes] == ["route-a"]
    assert result.routes_note == "Only 1 route was found."
    assert result.recommendation_reason == "Only route found"


def test_no_route_found(monkeypatch, forecast, departure):
    use_routing(monkeypatch, [])
    with pytest.raises(NoRouteFound):
        plan(departure)


def test_trips_over_3000_miles_are_rejected(monkeypatch, forecast, departure):
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION, length_scale=30)])  # ~3,180 miles

    with pytest.raises(TripTooLong):
        plan(departure)
    assert forecast.calls == []


@pytest.mark.parametrize("offset", [timedelta(minutes=-5), timedelta(seconds=30)])
def test_departure_in_the_past_or_imminent_departs_now(monkeypatch, forecast, offset):
    routing = use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])
    departure = timezone.now() + offset

    result = plan(departure)

    assert {call["depart_at"] for call in routing.calls} == {None}
    assert result.departure_time == departure.replace(microsecond=0)  # ETAs keep the departure


def test_checkpoints_and_etas(monkeypatch, forecast, departure):
    route = make_route(ORIGIN, DESTINATION)
    use_routing(monkeypatch, [route])
    miles = route.length_m / geo.METERS_PER_MILE

    checkpoints = route_by_id(plan(departure, interval=25), "route-a").checkpoints

    assert [c.distance_miles for c in checkpoints] == [0, 25, 50, 75, 100, round(miles, 1)]
    assert [c.index for c in checkpoints] == list(range(6))
    assert (checkpoints[0].lat, checkpoints[0].lon) == ORIGIN
    assert (checkpoints[-1].lat, checkpoints[-1].lon) == pytest.approx(DESTINATION)
    for c, distance in zip(checkpoints, [0, 25, 50, 75, 100, miles], strict=True):
        expected = departure + timedelta(hours=distance / SPEED_MPH)
        assert abs((c.eta - expected).total_seconds()) <= 1
    assert checkpoints[2].weather.wind_mph == 10.0
    assert (checkpoints[2].risk.level, checkpoints[2].risk.score) == ("low", 0)


def test_etas_follow_the_time_profile(monkeypatch, forecast, departure):
    route = make_route(ORIGIN, DESTINATION)
    half = route.length_m / 2
    slow_start = dataclasses.replace(  # first half takes 3 h, second half 1 h
        route,
        travel_time_s=4 * 3600,
        time_profile=[(0.0, 0.0), (half, 3 * 3600.0), (route.length_m, 4 * 3600.0)],
    )
    use_routing(monkeypatch, [slow_start])

    route_plan = route_by_id(plan(departure, interval=10), "route-a")

    mid = next(c for c in route_plan.checkpoints if c.distance_miles == 50)
    miles = route.length_m / geo.METERS_PER_MILE
    assert mid.eta == departure + timedelta(seconds=round(50 / (miles / 2) * 3 * 3600))
    assert route_plan.arrival_time == departure + timedelta(hours=4)
    assert route_plan.checkpoints[-1].eta == route_plan.arrival_time


def test_weather_window_covers_48_hours_and_the_latest_eta(monkeypatch, forecast, departure):
    long_drive = dataclasses.replace(make_route(ORIGIN, DESTINATION), travel_time_s=60 * 3600)
    long_drive = dataclasses.replace(
        long_drive, time_profile=[(0.0, 0.0), (long_drive.length_m, 60 * 3600.0)]
    )
    use_routing(monkeypatch, [long_drive])

    plan(departure)

    start_hour = departure.replace(minute=0)
    assert forecast.calls[0]["start_hour"] == start_hour
    assert forecast.calls[0]["end_hour"] == start_hour + timedelta(hours=61)  # 60 h 20 min → 61


def test_short_trip_still_fetches_48_hours(monkeypatch, forecast, departure):
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    plan(departure)

    call = forecast.calls[0]
    assert call["end_hour"] - call["start_hour"] == timedelta(hours=48)


def test_forecast_ending_before_the_last_eta_is_422(monkeypatch, departure):
    full = fake_forecast()

    def one_hour_only(cells, start_hour, end_hour):  # the trip takes ~2 h 7 min
        return full(cells, start_hour, start_hour + timedelta(hours=1))

    monkeypatch.setattr(open_meteo, "fetch_hourly", one_hour_only)
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    with pytest.raises(ForecastUnavailable) as exc:
        plan(departure)
    assert exc.value.status_code == 422
    assert exc.value.detail == (
        "The weather forecast does not cover this trip. Choose an earlier departure."
    )


def test_heatmap_has_49_frames_per_cell(monkeypatch, forecast, departure):
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    heatmap = plan(departure).heatmap

    assert heatmap.start_time == departure.replace(minute=0)
    assert heatmap.hours == 49
    assert heatmap.cell_size_deg == 0.1
    assert {(c.lat, c.lon) for c in heatmap.cells} == set(forecast.calls[0]["cells"])
    assert all(len(c.risk) == 49 and set(c.risk) == {0} for c in heatmap.cells)


def test_heatmap_uses_the_load_rules(monkeypatch, departure):
    monkeypatch.setattr(open_meteo, "fetch_hourly", fake_forecast(lambda cell: {"wind": 46}))
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    light = plan(departure, load=30_000).heatmap.cells[0].risk
    heavy = plan(departure, load=30_001).heatmap.cells[0].risk

    assert set(light) == {3}
    assert set(heavy) == {4}


def test_segments_cover_each_route_end_to_end(monkeypatch, departure):
    # severe snow east of -89°: the line turns severe halfway
    monkeypatch.setattr(
        open_meteo,
        "fetch_hourly",
        fake_forecast(lambda cell: {"snow": 2.5} if cell[1] > -89 else {}),
    )
    use_routing(
        monkeypatch, [make_route(ORIGIN, DESTINATION), make_route(ORIGIN, NORTH_VIA, DESTINATION)]
    )

    result = plan(departure, interval=10)

    for route in result.routes:
        segments = route.segments
        assert segments[0].coordinates[0] == [ORIGIN[1], ORIGIN[0]]
        assert segments[-1].coordinates[-1] == pytest.approx([DESTINATION[1], DESTINATION[0]])
        for left, right in pairwise(segments):
            assert left.coordinates[-1] == right.coordinates[0]
            assert left.level != right.level  # equal neighbours merged
        assert [s.level for s in segments] == ["low", "severe"]
        assert route.risk_summary.max_level == "severe"
        assert sum(route.risk_summary.miles_by_level.values()) == pytest.approx(
            route.distance_miles, abs=0.2
        )


def test_risk_drives_the_recommendation(monkeypatch, departure):
    # severe rain south of 39.95°: the direct route (lat 40) is clear, the southern one is not
    monkeypatch.setattr(
        open_meteo,
        "fetch_hourly",
        fake_forecast(lambda cell: {"rain": 0.6} if cell[0] < 39.95 else {}),
    )
    use_routing(
        monkeypatch,
        [make_route(ORIGIN, SOUTH_VIA, DESTINATION), make_route(ORIGIN, DESTINATION)],
    )

    result = plan(departure)

    assert result.recommended_route_id == "route-b"
    assert result.routes[0].id == "route-b"
    assert result.recommendation_reason.startswith("Fewest Severe miles (0.0 vs ")
    south = result.routes[1]
    assert south.risk_summary.miles_by_level["severe"] > 0
    assert any(c.risk.drivers == ["rain"] for c in south.checkpoints)


def test_all_routes_no_travel_means_no_safe_route(monkeypatch, departure):
    monkeypatch.setattr(open_meteo, "fetch_hourly", fake_forecast(lambda cell: {"wind": 60}))
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    result = plan(departure)

    assert result.no_safe_route is True
    assert result.recommended_route_id is None
    assert result.recommendation_reason == (
        "Every route has No Travel conditions at this departure time."
    )


def test_checkpoint_falls_back_to_the_nearest_cell(monkeypatch, departure):
    windy = fake_forecast(lambda cell: {"wind": 30})

    def only_north_row(cells, start_hour, end_hour):
        series = windy(cells, start_hour, end_hour)
        return {cell: s for cell, s in series.items() if cell[0] == 40.1}

    monkeypatch.setattr(open_meteo, "fetch_hourly", only_north_row)
    use_routing(monkeypatch, [make_route(ORIGIN, DESTINATION)])

    result = plan(departure)

    assert all(c.weather.wind_mph == 30 for c in result.routes[0].checkpoints)
    assert {c.lat for c in result.heatmap.cells} == {40.1}


def test_place_search_delegates_to_tomtom(monkeypatch):
    places = [tomtom.Place("Chicago, IL", 41.8781, -87.6298)]
    monkeypatch.setattr(tomtom, "search_places", lambda query: places if query == "chi" else [])

    assert services.place_search(query="chi") == places
