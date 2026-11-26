"""Risk classification of weather for a loaded truck. Pure functions: no Django, no HTTP."""

from enum import IntEnum

from trips.weather import Weather


class RiskLevel(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2
    SEVERE = 3
    NO_TRAVEL = 4

    @property
    def api_name(self) -> str:
        return self.name.lower()


def wind_level(wind_mph: float) -> RiskLevel:
    if wind_mph < 25:
        return RiskLevel.LOW
    if wind_mph < 35:
        return RiskLevel.MODERATE
    if wind_mph < 45:
        return RiskLevel.HIGH
    if wind_mph < 55:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def rain_level(rain_in_hr: float) -> RiskLevel:
    if rain_in_hr < 0.10:
        return RiskLevel.LOW
    if rain_in_hr < 0.25:
        return RiskLevel.MODERATE
    if rain_in_hr < 0.50:
        return RiskLevel.HIGH
    if rain_in_hr < 1.00:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def snow_level(snow_in_hr: float) -> RiskLevel:
    if snow_in_hr < 0.5:
        return RiskLevel.LOW
    if snow_in_hr < 1.0:
        return RiskLevel.MODERATE
    if snow_in_hr < 2.0:
        return RiskLevel.HIGH
    if snow_in_hr < 3.0:
        return RiskLevel.SEVERE
    return RiskLevel.NO_TRAVEL


def apply_load_rules(wind_mph: float, level: RiskLevel, load_weight_lb: int) -> RiskLevel:
    """Raise the wind level for heavy loads; never lower it. (Wind >= 55 is No Travel already.)"""
    if 45 <= wind_mph < 55 and load_weight_lb > 30_000:
        return RiskLevel.NO_TRAVEL
    if 35 <= wind_mph < 45 and load_weight_lb > 40_000:
        return max(level, RiskLevel.SEVERE)
    return level


def classify(weather: Weather, load_weight_lb: int) -> tuple[RiskLevel, list[str]]:
    """The checkpoint level (max of load-adjusted wind, rain, snow) and the factors driving it."""
    base_wind = wind_level(weather.wind_mph)
    wind = apply_load_rules(weather.wind_mph, base_wind, load_weight_lb)
    factors = (
        ("wind", wind),
        ("rain", rain_level(weather.rain_in_hr)),
        ("snow", snow_level(weather.snow_in_hr)),
    )
    level = max(factor_level for _, factor_level in factors)
    if level == RiskLevel.LOW:
        return level, []
    drivers = [name for name, factor_level in factors if factor_level == level]
    if wind > base_wind:
        drivers.append("load")
    return level, drivers
