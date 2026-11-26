"""Weather at a moment from an hourly forecast series. Pure functions: no Django, no HTTP."""

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Weather:
    wind_mph: float
    gust_mph: float
    rain_in_hr: float
    snow_in_hr: float


@dataclass(frozen=True)
class HourlySeries:
    """Open-Meteo hourly values, one per UTC hour from `start` (aware, on the hour).

    Precipitation values (rain, showers, snow) are the totals of the hour *before* each timestamp.
    """

    start: datetime
    wind_mph: tuple[float, ...]
    gust_mph: tuple[float, ...]
    rain_in: tuple[float, ...]
    showers_in: tuple[float, ...]
    snow_in: tuple[float, ...]


class ForecastRangeError(ValueError):
    """The moment is outside the hours the series covers."""


def weather_at(series: HourlySeries, moment: datetime) -> Weather:
    """Wind and gust at the hour nearest `moment`; rain and snow of the hour bin containing it."""
    hours = (moment - series.start).total_seconds() / 3600
    nearest = math.floor(hours + 0.5)
    containing = math.ceil(hours)  # first full hour >= moment
    size = len(series.wind_mph)
    if not (0 <= nearest < size and 0 <= containing < size):
        raise ForecastRangeError(f"{moment.isoformat()} is outside the forecast series.")
    return Weather(
        wind_mph=series.wind_mph[nearest],
        gust_mph=series.gust_mph[nearest],
        # rounded so float addition cannot push a value just under a threshold (0.07 + 0.03)
        rain_in_hr=round(series.rain_in[containing] + series.showers_in[containing], 4),
        snow_in_hr=series.snow_in[containing],
    )
