from django.urls import path

from .views import CalculateRouteAPIView


urlpatterns = [
    path(
        "route/calculate/",
        CalculateRouteAPIView.as_view(),
        name="calculate-route",
    ),
]