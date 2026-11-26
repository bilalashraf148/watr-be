"""Open-Meteo hourly forecast for many lattice cells, batched and cached."""

import logging
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx
from django.core.cache import caches

from trips.clients import http_client
from trips.exceptions import (
    WEATHER_RATE_LIMITED,
    WEATHER_UNAVAILABLE,
    ForecastUnavailable,
    UpstreamUnavailable,
)
from trips.geo import Point
from trips.weather import HourlySeries

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = ("wind_speed_10m", "wind_gusts_10m", "rain", "showers", "snowfall")
BATCH_SIZE = 100
MAX_PARALLEL_REQUESTS = 4
CACHE_ALIAS = "forecast"  # never "default": it holds the rate-limit history (settings.CACHES)
CACHE_SECONDS = 15 * 60
HOUR_FORMAT = "%Y-%m-%dT%H:%M"


class _RateLimited(Exception):
    """HTTP 429 from Open-Meteo (it counts every location as a call); carries its reason text."""


def _cache_key(cell: Point, start_hour: datetime, end_hour: datetime) -> str:
    return (
        f"open-meteo:{cell[0]:.4f}:{cell[1]:.4f}:"
        f"{start_hour.strftime(HOUR_FORMAT)}:{end_hour.strftime(HOUR_FORMAT)}"
    )


def fetch_hourly(
    cells: Iterable[Point], start_hour: datetime, end_hour: datetime
) -> dict[Point, HourlySeries]:
    """Hourly series per cell for start_hour … end_hour (UTC, inclusive); cache misses fetched."""
    cache = caches[CACHE_ALIAS]
    keys = {cell: _cache_key(cell, start_hour, end_hour) for cell in cells}
    cached = cache.get_many(list(keys.values()))
    result = {cell: cached[key] for cell, key in keys.items() if key in cached}
    missing = [cell for cell in keys if cell not in result]
    if not missing:
        return result

    batches = [missing[i : i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]
    try:
        with (
            http_client() as client,
            ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REQUESTS, len(batches))) as pool,
        ):
            fetched = list(
                pool.map(lambda batch: _fetch_batch(client, batch, start_hour, end_hour), batches)
            )
    except _RateLimited as exc:  # only the first failed batch surfaces: one log line per plan
        logger.warning("Open-Meteo rate limit reached (HTTP 429): %s", exc)
        raise UpstreamUnavailable(WEATHER_RATE_LIMITED) from None

    fresh = {cell: series for batch in fetched for cell, series in batch.items()}
    cache.set_many({keys[cell]: series for cell, series in fresh.items()}, timeout=CACHE_SECONDS)
    return result | fresh


def _fetch_batch(
    client: httpx.Client, cells: Sequence[Point], start_hour: datetime, end_hour: datetime
) -> dict[Point, HourlySeries]:
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in cells),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in cells),
        "hourly": ",".join(HOURLY_FIELDS),
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "GMT",
        "start_hour": start_hour.strftime(HOUR_FORMAT),
        "end_hour": end_hour.strftime(HOUR_FORMAT),
    }
    try:
        response = client.get(FORECAST_URL, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Open-Meteo request failed: %s", type(exc).__name__)
        raise UpstreamUnavailable(WEATHER_UNAVAILABLE) from None
    if response.status_code == 429:
        raise _RateLimited(response.text[:200])
    if response.status_code != 200:
        logger.warning("Open-Meteo returned HTTP %s: %s", response.status_code, response.text[:200])
        raise UpstreamUnavailable(WEATHER_UNAVAILABLE)
    try:
        body = response.json()
        locations = body if isinstance(body, list) else [body]  # one location → an object
        if len(locations) != len(cells):
            raise ValueError(f"{len(locations)} locations returned for {len(cells)} requested")
        # mapped by order: the returned coordinates are model grid points, not the requested ones
        return {
            cell: _parse_series(loc["hourly"]) for cell, loc in zip(cells, locations, strict=True)
        }
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("Open-Meteo returned an unexpected body: %r", exc)
        raise UpstreamUnavailable(WEATHER_UNAVAILABLE) from None


def _values(hourly: dict[str, Any], field: str, size: int) -> tuple[float, ...]:
    values = hourly[field]
    if len(values) != size:
        raise ValueError(f"{field} has {len(values)} values for {size} hours")
    if any(v is None for v in values):  # no forecast for some hour: never read it as calm weather
        raise ForecastUnavailable()
    return tuple(float(v) for v in values)


def _parse_series(hourly: dict[str, Any]) -> HourlySeries:
    times = hourly["time"]
    size = len(times)
    start = datetime.strptime(times[0], HOUR_FORMAT).replace(tzinfo=timezone.utc)
    return HourlySeries(
        start=start,
        wind_mph=_values(hourly, "wind_speed_10m", size),
        gust_mph=_values(hourly, "wind_gusts_10m", size),
        rain_in=_values(hourly, "rain", size),
        showers_in=_values(hourly, "showers", size),
        snow_in=_values(hourly, "snowfall", size),
    )
