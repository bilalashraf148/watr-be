import pytest

from trips.risk import (
    RiskLevel,
    apply_load_rules,
    classify,
    rain_level,
    snow_level,
    wind_level,
)
from trips.weather import Weather

LOW, MODERATE, HIGH, SEVERE, NO_TRAVEL = RiskLevel


def weather(wind=0.0, rain=0.0, snow=0.0) -> Weather:
    return Weather(wind_mph=wind, gust_mph=wind + 10, rain_in_hr=rain, snow_in_hr=snow)


@pytest.mark.parametrize(
    ("mph", "level"),
    [
        (24.9, LOW),
        (25, MODERATE),
        (34.9, MODERATE),
        (35, HIGH),
        (44.9, HIGH),
        (45, SEVERE),
        (54.9, SEVERE),
        (55, NO_TRAVEL),
    ],
)
def test_wind_thresholds(mph, level):
    assert wind_level(mph) == level


@pytest.mark.parametrize(
    ("inches", "level"),
    [
        (0.0999, LOW),
        (0.10, MODERATE),
        (0.2499, MODERATE),
        (0.25, HIGH),
        (0.4999, HIGH),
        (0.50, SEVERE),
        (0.999, SEVERE),
        (1.00, NO_TRAVEL),
        (1.01, NO_TRAVEL),
    ],
)
def test_rain_thresholds(inches, level):
    assert rain_level(inches) == level


@pytest.mark.parametrize(
    ("inches", "level"),
    [
        (0.49, LOW),
        (0.5, MODERATE),
        (0.99, MODERATE),
        (1.0, HIGH),
        (1.99, HIGH),
        (2.0, SEVERE),
        (2.99, SEVERE),
        (3.0, NO_TRAVEL),
        (3.01, NO_TRAVEL),
    ],
)
def test_snow_thresholds(inches, level):
    assert snow_level(inches) == level


@pytest.mark.parametrize(
    ("mph", "load", "level"),
    [
        (45, 30_000, SEVERE),
        (45, 30_001, NO_TRAVEL),
        (54.9, 30_001, NO_TRAVEL),
        (35, 40_000, HIGH),
        (35, 40_001, SEVERE),
        (44.9, 40_001, SEVERE),
        (34.9, 80_000, MODERATE),  # no rule below 35 mph
        (55, 1, NO_TRAVEL),  # No Travel for any load
    ],
)
def test_load_rules(mph, load, level):
    assert apply_load_rules(mph, wind_level(mph), load) == level


def test_load_rules_never_lower_the_level():
    assert apply_load_rules(40, NO_TRAVEL, 40_001) == NO_TRAVEL


def test_calm_weather_is_low_without_drivers():
    assert classify(weather(wind=10), 80_000) == (LOW, [])


def test_level_is_the_max_of_the_factors():
    assert classify(weather(wind=10, rain=0.3, snow=0.6), 10_000) == (HIGH, ["rain"])


def test_every_factor_at_the_level_is_a_driver():
    assert classify(weather(wind=30, rain=0.1, snow=0.5), 10_000) == (
        MODERATE,
        ["wind", "rain", "snow"],
    )


def test_load_is_a_driver_when_it_raised_the_wind_level():
    assert classify(weather(wind=46), 30_001) == (NO_TRAVEL, ["wind", "load"])
    assert classify(weather(wind=40, rain=0.6), 45_000) == (SEVERE, ["wind", "rain", "load"])


def test_load_is_not_a_driver_when_no_rule_applied():
    assert classify(weather(wind=55), 1) == (NO_TRAVEL, ["wind"])
    assert classify(weather(wind=46), 30_000) == (SEVERE, ["wind"])


def test_api_names():
    assert [level.api_name for level in RiskLevel] == [
        "low",
        "moderate",
        "high",
        "severe",
        "no_travel",
    ]
