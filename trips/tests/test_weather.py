from datetime import datetime, timedelta, timezone

import pytest

from trips.weather import ForecastRangeError, HourlySeries, weather_at

START = datetime(2026, 11, 26, 14, tzinfo=timezone.utc)


@pytest.fixture
def series() -> HourlySeries:
    return HourlySeries(
        start=START,  # 14:00 … 18:00
        wind_mph=(10.0, 20.0, 30.0, 40.0, 50.0),
        gust_mph=(15.0, 25.0, 35.0, 45.0, 55.0),
        rain_in=(0.01, 0.07, 0.2, 0.3, 0.4),
        showers_in=(0.0, 0.03, 0.05, 0.0, 0.0),
        snow_in=(0.0, 0.5, 1.0, 1.5, 2.0),
    )


def at(hours: float) -> datetime:
    return START + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("moment", "wind", "gust"),
    [
        (at(0), 10.0, 15.0),
        (START + timedelta(minutes=29), 10.0, 15.0),
        (START + timedelta(minutes=30), 20.0, 25.0),  # halfway rounds to the later hour
        (START + timedelta(minutes=31), 20.0, 25.0),
        (START + timedelta(hours=3, minutes=50), 50.0, 55.0),
    ],
)
def test_wind_and_gust_come_from_the_nearest_hour(series, moment, wind, gust):
    result = weather_at(series, moment)
    assert (result.wind_mph, result.gust_mph) == (wind, gust)


@pytest.mark.parametrize(
    ("moment", "snow"),
    [
        (at(0), 0.0),  # 14:00 is in the bin (13:00, 14:00], reported at 14:00
        (START + timedelta(minutes=1), 0.5),  # (14:00, 15:00], reported at 15:00
        (at(1), 0.5),
        (START + timedelta(hours=1, minutes=59), 1.0),
    ],
)
def test_precipitation_comes_from_the_hour_bin_containing_the_moment(series, moment, snow):
    assert weather_at(series, moment).snow_in_hr == snow


def test_rain_is_rain_plus_showers(series):
    assert weather_at(series, START + timedelta(minutes=45)).rain_in_hr == 0.1  # 0.07 + 0.03
    assert weather_at(series, at(2)).rain_in_hr == 0.25


@pytest.mark.parametrize(
    "moment",
    [
        START - timedelta(minutes=31),  # nearest hour is 13:00
        at(4) + timedelta(minutes=1),  # its bin is reported at 19:00
        at(10),
    ],
)
def test_moment_outside_the_series_raises(series, moment):
    with pytest.raises(ForecastRangeError):
        weather_at(series, moment)
