import json
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .redis_client import redis_client
from .services import (
    build_cache_key,
    calculate_multiple_fuel_stops,
    calculate_route,
    find_stations_near_route,
)


logger = logging.getLogger(__name__)


class CalculateRouteAPIView(APIView):
    """
    Calculate a driving route and determine fuel stops.

    Request:
        POST /api/route/calculate/

    Responsibilities:
        1. Validate request data.
        2. Check API-level Redis cache.
        3. Calculate route.
        4. Find nearby fuel stations.
        5. Calculate fuel stops.
        6. Return the final result.
    """

    def post(self, request):
        # ====================================================
        # 1. Read request data
        # ====================================================

        start_address = request.data.get(
            "start_address"
        )

        destination_address = request.data.get(
            "destination_address"
        )

        fuel_efficiency_mpg = request.data.get(
            "fuel_efficiency_mpg",
            10,
        )

        tank_capacity_gallons = request.data.get(
            "tank_capacity_gallons",
            100,
        )

        starting_fuel_gallons = request.data.get(
            "starting_fuel_gallons"
        )

        # ====================================================
        # 2. Validate addresses
        # ====================================================

        if not isinstance(start_address, str) or not start_address.strip():
            return Response(
                {
                    "error": "start_address is required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            not isinstance(
                destination_address,
                str,
            )
            or not destination_address.strip()
        ):
            return Response(
                {
                    "error": (
                        "destination_address is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ====================================================
        # 3. Validate numeric parameters
        # ====================================================

        try:
            fuel_efficiency_mpg = float(
                fuel_efficiency_mpg
            )

            tank_capacity_gallons = float(
                tank_capacity_gallons
            )

            if starting_fuel_gallons is not None:
                starting_fuel_gallons = float(
                    starting_fuel_gallons
                )

        except (TypeError, ValueError):
            return Response(
                {
                    "error": (
                        "Fuel parameters must be "
                        "valid numbers."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ====================================================
        # 4. Validate fuel values
        # ====================================================

        if fuel_efficiency_mpg <= 0:
            return Response(
                {
                    "error": (
                        "fuel_efficiency_mpg must "
                        "be greater than zero."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if tank_capacity_gallons <= 0:
            return Response(
                {
                    "error": (
                        "tank_capacity_gallons must "
                        "be greater than zero."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if starting_fuel_gallons is not None:

            if starting_fuel_gallons < 0:
                return Response(
                    {
                        "error": (
                            "starting_fuel_gallons "
                            "cannot be negative."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if (
                starting_fuel_gallons
                > tank_capacity_gallons
            ):
                return Response(
                    {
                        "error": (
                            "starting_fuel_gallons "
                            "cannot exceed "
                            "tank_capacity_gallons."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ====================================================
        # 5. Normalize addresses
        # ====================================================

        start_address = " ".join(
            start_address.strip().split()
        )

        destination_address = " ".join(
            destination_address.strip().split()
        )

        # ====================================================
        # 6. Build API response cache key
        # ====================================================

        cache_key = build_cache_key(
            "api_route",
            {
                "start": start_address.lower(),
                "destination": destination_address.lower(),
                "fuel_efficiency_mpg": (
                    fuel_efficiency_mpg
                ),
                "tank_capacity_gallons": (
                    tank_capacity_gallons
                ),
                "starting_fuel_gallons": (
                    starting_fuel_gallons
                ),
            },
        )

        # ====================================================
        # 7. Check API-level Redis cache
        # ====================================================

        try:
            cached_response = redis_client.get(
                cache_key
            )

            if cached_response:

                response_data = json.loads(
                    cached_response
                )

                return Response(
                    response_data,
                    status=status.HTTP_200_OK,
                )

        except Exception:
            # Cache failure should not break the API.
            logger.exception(
                "Redis read failed for key %s",
                cache_key,
            )

        # ====================================================
        # 8. Calculate route
        # ====================================================

        try:

            route_result = calculate_route(
                start_address=start_address,
                destination_address=destination_address,
                fuel_efficiency_mpg=(
                    fuel_efficiency_mpg
                ),
                tank_capacity_gallons=(
                    tank_capacity_gallons
                ),
                starting_fuel_gallons=(
                    starting_fuel_gallons
                ),
            )

            # =================================================
            # 9. Find fuel stations near route
            # =================================================

            nearby_stations = find_stations_near_route(
                route_geometry=(
                    route_result["route_geometry"]
                ),
                max_distance_miles=10,
            )

            # =================================================
            # 10. Calculate fuel stops
            # =================================================

            fuel_result = calculate_multiple_fuel_stops(
                start_location=route_result["start"],
                destination_location=(
                    route_result["destination"]
                ),
                candidate_stations=nearby_stations,
                fuel_efficiency_mpg=(
                    fuel_efficiency_mpg
                ),
                tank_capacity_gallons=(
                    tank_capacity_gallons
                ),
                starting_fuel_gallons=(
                    starting_fuel_gallons
                ),
            )

            # =================================================
            # 11. Build response
            # =================================================

            response_data = {
                "start": {
                    "latitude": (
                        route_result["start"][
                            "latitude"
                        ]
                    ),
                    "longitude": (
                        route_result["start"][
                            "longitude"
                        ]
                    ),
                },
                "destination": {
                    "latitude": (
                        route_result["destination"][
                            "latitude"
                        ]
                    ),
                    "longitude": (
                        route_result["destination"][
                            "longitude"
                        ]
                    ),
                },
                "distance_miles": round(
                    route_result["distance_miles"],
                    2,
                ),
                "fuel": route_result[
                    "fuel_details"
                ],
                "fuel_stops": fuel_result[
                    "stops"
                ],
                "total_stops": fuel_result[
                    "total_stops"
                ],
                "total_fuel_cost": fuel_result[
                    "total_fuel_cost"
                ],
                "remaining_fuel_gallons": (
                    fuel_result[
                        "remaining_fuel_gallons"
                    ]
                ),
                "route_geometry": (
                    route_result[
                        "route_geometry"
                    ]
                ),
            }

        except ValueError as exc:

            return Response(
                {
                    "error": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception:

            logger.exception(
                "Unexpected error while calculating route."
            )

            return Response(
                {
                    "error": (
                        "Unable to calculate route "
                        "at this time."
                    )
                },
                status=(
                    status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
            )

        # ====================================================
        # 12. Cache final API response
        # ====================================================

        try:
            redis_client.set(
                cache_key,
                json.dumps(response_data),
                ex=1800,
            )

        except Exception:
            # Redis is an optimization.
            # API should still return the response
            # if Redis is temporarily unavailable.
            logger.exception(
                "Redis write failed for key %s",
                cache_key,
            )

        # ====================================================
        # 13. Return response
        # ====================================================

        return Response(
            response_data,
            status=status.HTTP_200_OK,
        )