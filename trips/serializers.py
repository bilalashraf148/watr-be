from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import serializers

from trips.geo import haversine_miles

DEPARTURE_GRACE = timedelta(minutes=10)
DEPARTURE_HORIZON = timedelta(days=7)
MIN_TRIP_MILES = 1.0


class AwareDateTimeField(serializers.DateTimeField):
    """ISO 8601 datetime that must carry a UTC offset (no silent server-timezone guess)."""

    default_error_messages = {
        "naive": "Include a UTC offset, e.g. 2026-11-26T08:00:00-06:00.",
    }

    def enforce_timezone(self, value: datetime) -> datetime:
        if timezone.is_naive(value):
            self.fail("naive")
        return super().enforce_timezone(value)


class PlaceQueryInputSerializer(serializers.Serializer):
    q = serializers.CharField(min_length=3, max_length=100)  # trimmed by CharField


class PlaceOutputSerializer(serializers.Serializer):
    label = serializers.CharField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()


class LocationInputSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=200)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class TripPlanInputSerializer(serializers.Serializer):
    origin = LocationInputSerializer()
    destination = LocationInputSerializer()
    departure_time = AwareDateTimeField()
    load_weight_lb = serializers.IntegerField(min_value=1, max_value=80_000)
    checkpoint_interval_miles = serializers.ChoiceField(choices=[10, 25, 50])

    def validate_departure_time(self, value: datetime) -> datetime:
        now = timezone.now()
        if value < now - DEPARTURE_GRACE:
            raise serializers.ValidationError("Departure cannot be in the past.")
        if value > now + DEPARTURE_HORIZON:
            raise serializers.ValidationError("Departure must be within the next 7 days.")
        return value

    def validate(self, attrs: dict) -> dict:
        origin, destination = attrs["origin"], attrs["destination"]
        distance = haversine_miles(
            (origin["lat"], origin["lon"]), (destination["lat"], destination["lon"])
        )
        if distance < MIN_TRIP_MILES:
            raise serializers.ValidationError(
                "Origin and destination must be at least 1 mile apart."
            )
        return attrs


class WeatherOutputSerializer(serializers.Serializer):
    wind_mph = serializers.FloatField()
    gust_mph = serializers.FloatField()
    rain_in_hr = serializers.FloatField()
    snow_in_hr = serializers.FloatField()


class RiskOutputSerializer(serializers.Serializer):
    level = serializers.CharField()
    score = serializers.IntegerField()
    drivers = serializers.ListField(child=serializers.CharField())


class CheckpointOutputSerializer(serializers.Serializer):
    index = serializers.IntegerField()
    distance_miles = serializers.FloatField()
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    eta = serializers.DateTimeField()
    weather = WeatherOutputSerializer()
    risk = RiskOutputSerializer()


class SegmentOutputSerializer(serializers.Serializer):
    level = serializers.CharField()
    coordinates = serializers.ListField(child=serializers.ListField(child=serializers.FloatField()))


class RiskSummaryOutputSerializer(serializers.Serializer):
    max_level = serializers.CharField()
    average_risk = serializers.FloatField()
    miles_by_level = serializers.DictField(child=serializers.FloatField())


class RouteOutputSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    rank = serializers.IntegerField()
    source = serializers.CharField()
    distance_miles = serializers.FloatField()
    duration_seconds = serializers.IntegerField()
    arrival_time = serializers.DateTimeField()
    bbox = serializers.ListField(child=serializers.FloatField())
    risk_summary = RiskSummaryOutputSerializer()
    segments = SegmentOutputSerializer(many=True)
    checkpoints = CheckpointOutputSerializer(many=True)


class HeatmapCellOutputSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    risk = serializers.ListField(child=serializers.IntegerField())


class HeatmapOutputSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    hours = serializers.IntegerField()
    cell_size_deg = serializers.FloatField()
    cells = HeatmapCellOutputSerializer(many=True)


class TripPlanOutputSerializer(serializers.Serializer):
    departure_time = serializers.DateTimeField()
    load_weight_lb = serializers.IntegerField()
    checkpoint_interval_miles = serializers.IntegerField()
    recommended_route_id = serializers.CharField(allow_null=True)
    recommendation_reason = serializers.CharField()
    no_safe_route = serializers.BooleanField()
    routes_note = serializers.CharField(allow_null=True)
    routes = RouteOutputSerializer(many=True)
    heatmap = HeatmapOutputSerializer()
