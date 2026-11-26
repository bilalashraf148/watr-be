from django.urls import path

from trips.views import PlaceSearchView, TripPlanView

urlpatterns = [
    path("places/", PlaceSearchView.as_view(), name="place-search"),
    path("trips/plan/", TripPlanView.as_view(), name="trip-plan"),
]
