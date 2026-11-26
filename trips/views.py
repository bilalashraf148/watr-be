from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from trips.serializers import (
    PlaceOutputSerializer,
    PlaceQueryInputSerializer,
    TripPlanInputSerializer,
    TripPlanOutputSerializer,
)
from trips.services import Location, place_search, trip_plan


class PlaceSearchView(APIView):
    # PUBLIC: place typeahead for the trip form; the app has no accounts. Rate-limited per client
    # IP to protect the TomTom API key.
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "places"

    def get(self, request: Request) -> Response:
        data = PlaceQueryInputSerializer(data=request.query_params)
        data.is_valid(raise_exception=True)
        places = place_search(query=data.validated_data["q"])
        return Response({"results": PlaceOutputSerializer(places, many=True).data})


class TripPlanView(APIView):
    # PUBLIC: stateless trip planning; the app has no accounts and stores nothing. Rate-limited per
    # client IP to protect the TomTom key and the Open-Meteo quota.
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "trip_plan"

    def post(self, request: Request) -> Response:
        data = TripPlanInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        plan = trip_plan(
            origin=Location(**values["origin"]),
            destination=Location(**values["destination"]),
            departure_time=values["departure_time"],
            load_weight_lb=values["load_weight_lb"],
            interval_miles=values["checkpoint_interval_miles"],
        )
        return Response(TripPlanOutputSerializer(plan).data)
