from rest_framework.exceptions import APIException

ROUTING_UNAVAILABLE = "Routing service is unavailable. Try again."
WEATHER_UNAVAILABLE = "Weather service is unavailable. Try again."
WEATHER_RATE_LIMITED = "Weather data limit reached. Try again in a minute."
SEARCH_UNAVAILABLE = "Place search is unavailable. Try again."


class NoRouteFound(APIException):
    status_code = 422
    default_detail = "No truck route was found between these places."
    default_code = "no_route"


class TripTooLong(APIException):
    status_code = 422
    default_detail = "Trips longer than 3,000 miles are not supported."
    default_code = "trip_too_long"


class ForecastUnavailable(APIException):
    status_code = 422
    default_detail = "The weather forecast does not cover this trip. Choose an earlier departure."
    default_code = "forecast_unavailable"


class UpstreamUnavailable(APIException):
    """A third-party service failed; the detail names which (ROUTING_ / WEATHER_ / SEARCH_).

    WEATHER_RATE_LIMITED: Open-Meteo answered HTTP 429 (its per-minute location limit).
    """

    status_code = 502
    default_detail = "An upstream service is unavailable. Try again."
    default_code = "upstream_unavailable"
