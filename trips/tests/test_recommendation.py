import pytest

from trips.recommendation import (
    NO_SAFE_ROUTE_REASON,
    RankInput,
    RouteMetrics,
    coverage,
    format_duration,
    rank,
    summarize,
)
from trips.risk import RiskLevel

LOW, MODERATE, HIGH, SEVERE, NO_TRAVEL = RiskLevel


def route(
    route_id: str,
    *,
    no_travel: float = 0.0,
    severe: float = 0.0,
    high: float = 0.0,
    average: float = 0.0,
    duration: int = 3600,
) -> RankInput:
    miles = dict.fromkeys(RiskLevel, 0.0)
    miles.update({NO_TRAVEL: no_travel, SEVERE: severe, HIGH: high})
    max_level = max((level for level, m in miles.items() if m > 0), default=LOW)
    return RankInput(route_id, RouteMetrics(miles, average, max_level), duration)


def test_coverage_is_halfway_to_the_neighbours_clipped_to_the_ends():
    assert coverage([0, 25, 50, 60], 60) == [(0, 12.5), (12.5, 37.5), (37.5, 55), (55, 60)]
    assert coverage([0], 0.8) == [(0, 0.8)]


def test_summarize_sums_covered_miles_and_weights_the_average():
    metrics = summarize([0, 25, 50, 60], [LOW, HIGH, SEVERE, LOW], 60)
    assert metrics.miles_by_level == {
        LOW: 17.5,
        MODERATE: 0.0,
        HIGH: 25.0,
        SEVERE: 17.5,
        NO_TRAVEL: 0.0,
    }
    assert metrics.average_risk == pytest.approx((2 * 25 + 3 * 17.5) / 60)
    assert metrics.max_level == SEVERE


def test_summarize_last_checkpoint_covers_to_the_route_end():
    metrics = summarize([0, 25, 50], [LOW, LOW, NO_TRAVEL], 50.05)
    assert metrics.miles_by_level[NO_TRAVEL] == pytest.approx(12.55)


@pytest.mark.parametrize(
    ("worse", "better", "reason"),
    [
        (route("a", no_travel=12.5), route("b", severe=80), "No No Travel miles (0.0 vs 12.5)"),
        (route("a", severe=37.5), route("b", high=90), "Fewest Severe miles (0.0 vs 37.5)"),
        (route("a", high=20.04), route("b", high=3, average=3), "Fewest High miles (3.0 vs 20.0)"),
        (
            route("a", average=0.35),
            route("b", average=0.21, duration=9_000),
            "Lowest average risk (0.21 vs 0.35)",
        ),
        (
            route("a", duration=61_500),
            route("b", duration=60_000),
            "Shortest travel time (16 h 40 min vs 17 h 5 min)",
        ),
    ],
)
def test_each_tie_break_in_order(worse, better, reason):
    plan = rank([worse, better])
    assert plan.order == ["b", "a"]
    assert plan.recommended_route_id == "b"
    assert plan.no_safe_route is False
    assert plan.reason == reason


def test_values_are_rounded_before_comparing():
    plan = rank([route("a", severe=0.04, average=0.214), route("b", average=0.206)])
    # 0.04 Severe miles round to 0.0; average risks round to 0.21 each; then duration ties
    assert plan.order == ["a", "b"]
    assert plan.reason == "Equal on every criterion; first route returned"


def test_tie_keeps_generation_order():
    plan = rank([route("a"), route("b"), route("c")])
    assert plan.order == ["a", "b", "c"]
    assert plan.recommended_route_id == "a"
    assert plan.reason == "Equal on every criterion; first route returned"


def test_single_route():
    plan = rank([route("a", high=10)])
    assert (plan.order, plan.recommended_route_id, plan.reason) == (["a"], "a", "Only route found")


def test_no_travel_route_is_never_recommended():
    plan = rank([route("a", no_travel=1), route("b", no_travel=0.5)])
    assert plan.order == ["b", "a"]
    assert plan.recommended_route_id is None
    assert plan.no_safe_route is True
    assert plan.reason == NO_SAFE_ROUTE_REASON


def test_single_no_travel_route_is_no_safe_route():
    plan = rank([route("a", no_travel=3)])
    assert (plan.recommended_route_id, plan.no_safe_route) == (None, True)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(60_000, "16 h 40 min"), (61_500, "17 h 5 min"), (2_700, "0 h 45 min"), (3_599, "1 h 0 min")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
