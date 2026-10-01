import hashlib
import json
import math
import time

import requests
from django.conf import settings

from .models import FuelStation, Location
from .redis_client import redis_client


# ============================================================
# Cache Helpers
# ============================================================

def build_cache_key(prefix, data):
    """
    Build a deterministic Redis cache key from input data.
    """
    serialized_data = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    )

    data_hash = hashlib.sha256(
        serialized_data.encode("utf-8")
    ).hexdigest()

    return f"{prefix}:{data_hash}"


# ============================================================
# Geoapify API
# ============================================================

def get_route(start_lon, start_lat, end_lon, end_lat):
    """
    Get driving route from Geoapify.
    """

    url = "https://api.geoapify.com/v1/routing"

    params = {
        "waypoints": (
            f"{start_lat},{start_lon}|"
            f"{end_lat},{end_lon}"
        ),
        "mode": "drive",
        "apiKey": settings.GEOAPIFY_API_KEY,
    }

    response = requests.get(
        url,
        params=params,
        timeout=10,
    )

    response.raise_for_status()

    return response.json()


def get_route_distance_miles(route):
    """
    Extract route distance from Geoapify response.

    Geoapify returns distance in meters.
    """

    try:
        distance_meters = (
            route["features"][0]["properties"]["distance"]
        )
    except (KeyError, IndexError, TypeError):
        raise ValueError("Invalid route response.")

    return distance_meters / 1609.344


def get_route_geometry(route):
    """
    Extract route geometry from Geoapify response.
    """

    try:
        return route["features"][0]["geometry"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Route geometry not found.")


# ============================================================
# Geocoding
# ============================================================

def normalize_location(address):
    """
    Normalize an address before using it as a database/cache key.
    """

    return " ".join(
        address.strip().lower().split()
    )


def geocode_address(address):
    """
    Convert an address into latitude/longitude using Geoapify.
    """

    url = "https://api.geoapify.com/v1/geocode/search"

    params = {
        "text": address,
        "filter": "countrycode:us",
        "limit": 5,
        "format": "json",
        "apiKey": settings.GEOAPIFY_API_KEY,
    }

    response = requests.get(
        url,
        params=params,
        timeout=10,
    )

    response.raise_for_status()

    results = response.json().get("results", [])

    if not results:
        return None

    # Select the result with the highest confidence.
    best_result = max(
        results,
        key=lambda item: item.get(
            "rank", {}
        ).get(
            "confidence", 0
        ),
    )

    latitude = best_result.get("lat")
    longitude = best_result.get("lon")

    if latitude is None or longitude is None:
        return None

    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
    }


def get_or_create_location(address):
    """
    Resolve an address using:

        Redis cache
            ↓
        PostgreSQL
            ↓
        Geoapify

    A Redis lock prevents multiple simultaneous
    requests from geocoding the same address.
    """

    location_key = normalize_location(address)

    cache_key = f"location:{location_key}"
    lock_key = f"location_lock:{location_key}"

    # --------------------------------------------------------
    # 1. Redis cache
    # --------------------------------------------------------

    cached_location = redis_client.get(cache_key)

    if cached_location:
        return json.loads(cached_location)

    # --------------------------------------------------------
    # 2. Acquire distributed lock
    # --------------------------------------------------------

    lock_acquired = redis_client.set(
        lock_key,
        "1",
        nx=True,
        ex=30,
    )

    if lock_acquired:

        try:
            # ------------------------------------------------
            # Double-check Redis after acquiring the lock
            # ------------------------------------------------

            cached_location = redis_client.get(cache_key)

            if cached_location:
                return json.loads(cached_location)

            # ------------------------------------------------
            # 3. PostgreSQL
            # ------------------------------------------------

            existing_location = (
                Location.objects
                .filter(location_key=location_key)
                .first()
            )

            if existing_location:

                location_data = {
                    "latitude": float(
                        existing_location.latitude
                    ),
                    "longitude": float(
                        existing_location.longitude
                    ),
                }

                redis_client.set(
                    cache_key,
                    json.dumps(location_data),
                    ex=86400,
                )

                return location_data

            # ------------------------------------------------
            # 4. Geoapify
            # ------------------------------------------------

            location_data = geocode_address(address)

            if not location_data:
                return None

            # ------------------------------------------------
            # 5. PostgreSQL persistence
            # ------------------------------------------------

            Location.objects.create(
                location_key=location_key,
                latitude=location_data["latitude"],
                longitude=location_data["longitude"],
                formatted_name=address,
            )

            # ------------------------------------------------
            # 6. Redis cache
            # ------------------------------------------------

            redis_client.set(
                cache_key,
                json.dumps(location_data),
                ex=86400,
            )

            return location_data

        finally:
            redis_client.delete(lock_key)

    # --------------------------------------------------------
    # Another request is already resolving this location.
    # Wait briefly for its result.
    # --------------------------------------------------------

    for _ in range(10):

        time.sleep(0.2)

        cached_location = redis_client.get(cache_key)

        if cached_location:
            return json.loads(cached_location)

    return None


def get_station_location(station):
    """
    Return the location of a fuel station.

    If the station is already linked to a Location record,
    no external API call is made.

    Otherwise, geocode the station address once and
    permanently link the station to that Location.
    """

    if station.location:

        return {
            "latitude": float(
                station.location.latitude
            ),
            "longitude": float(
                station.location.longitude
            ),
        }

    address = (
        f"{station.address}, "
        f"{station.city}, "
        f"{station.state}, USA"
    )

    location_data = get_or_create_location(address)

    if not location_data:
        return None

    location_key = normalize_location(address)

    location = Location.objects.get(
        location_key=location_key
    )

    station.location = location

    station.save(
        update_fields=["location"]
    )

    return location_data


# ============================================================
# Route Calculation
# ============================================================

def calculate_route(
    start_address,
    destination_address,
    fuel_efficiency_mpg=10,
    tank_capacity_gallons=100,
    starting_fuel_gallons=None,
):
    """
    Calculate the route between two addresses.

    Route geometry/distance is cached independently
    from fuel parameters because fuel parameters do
    not change the actual route.
    """

    start_key = normalize_location(start_address)
    destination_key = normalize_location(
        destination_address
    )

    route_cache_key = build_cache_key(
        "route",
        {
            "start": start_key,
            "destination": destination_key,
        },
    )

    # --------------------------------------------------------
    # Route cache
    # --------------------------------------------------------

    cached_route = redis_client.get(
        route_cache_key
    )

    if cached_route:

        route_data = json.loads(cached_route)

        fuel_details = calculate_fuel_details(
            distance_miles=route_data["distance_miles"],
            fuel_efficiency_mpg=fuel_efficiency_mpg,
            tank_capacity_gallons=tank_capacity_gallons,
            starting_fuel_gallons=starting_fuel_gallons,
        )

        return {
            **route_data,
            "fuel_details": fuel_details,
        }

    # --------------------------------------------------------
    # Resolve locations
    # --------------------------------------------------------

    start_location = get_or_create_location(
        start_address
    )

    if not start_location:
        raise ValueError(
            "Unable to geocode start address."
        )

    destination_location = get_or_create_location(
        destination_address
    )

    if not destination_location:
        raise ValueError(
            "Unable to geocode destination address."
        )

    # --------------------------------------------------------
    # Get route from Geoapify
    # --------------------------------------------------------

    route = get_route(
        start_lon=start_location["longitude"],
        start_lat=start_location["latitude"],
        end_lon=destination_location["longitude"],
        end_lat=destination_location["latitude"],
    )

    distance_miles = get_route_distance_miles(
        route
    )

    route_geometry = get_route_geometry(
        route
    )

    # --------------------------------------------------------
    # Store only route-related data in cache
    # --------------------------------------------------------

    route_data = {
        "start": start_location,
        "destination": destination_location,
        "distance_miles": distance_miles,
        "route_geometry": route_geometry,
    }

    redis_client.set(
        route_cache_key,
        json.dumps(route_data),
        ex=3600,
    )

    # --------------------------------------------------------
    # Calculate fuel information separately
    # --------------------------------------------------------

    fuel_details = calculate_fuel_details(
        distance_miles=distance_miles,
        fuel_efficiency_mpg=fuel_efficiency_mpg,
        tank_capacity_gallons=tank_capacity_gallons,
        starting_fuel_gallons=starting_fuel_gallons,
    )

    return {
        **route_data,
        "fuel_details": fuel_details,
    }


# ============================================================
# Distance Calculations
# ============================================================

def calculate_distance_miles(
    lat1,
    lon1,
    lat2,
    lon2,
):
    """
    Calculate straight-line distance using the Haversine formula.
    """

    earth_radius_miles = 3958.8

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a),
    )

    return earth_radius_miles * c


def get_driving_distance_miles(
    start_lat,
    start_lon,
    end_lat,
    end_lon,
):
    """
    Get actual driving distance using Geoapify.

    Result is cached because route-distance requests
    are relatively expensive.
    """

    cache_key = build_cache_key(
        "driving_distance",
        {
            "start_lat": round(start_lat, 6),
            "start_lon": round(start_lon, 6),
            "end_lat": round(end_lat, 6),
            "end_lon": round(end_lon, 6),
        },
    )

    cached_distance = redis_client.get(
        cache_key
    )

    if cached_distance:
        return float(cached_distance)

    route = get_route(
        start_lon=start_lon,
        start_lat=start_lat,
        end_lon=end_lon,
        end_lat=end_lat,
    )

    distance_miles = get_route_distance_miles(
        route
    )

    redis_client.set(
        cache_key,
        str(distance_miles),
        ex=3600,
    )

    return distance_miles


# ============================================================
# Route / Station Filtering
# ============================================================

def get_route_bounding_box(
    route_geometry,
    buffer_degrees=0.2,
):
    """
    Create a rough bounding box around the route.

    Used as a cheap pre-filter before calculating
    Haversine distances.
    """

    coordinates = route_geometry.get(
        "coordinates",
        [],
    )

    if not coordinates:
        return None

    longitudes = [
        point[0]
        for point in coordinates
    ]

    latitudes = [
        point[1]
        for point in coordinates
    ]

    return {
        "min_lon": min(longitudes) - buffer_degrees,
        "max_lon": max(longitudes) + buffer_degrees,
        "min_lat": min(latitudes) - buffer_degrees,
        "max_lat": max(latitudes) + buffer_degrees,
    }


def find_stations_near_route(
    route_geometry,
    max_distance_miles=10,
    route_sample_step=10,
):
    """
    Find fuel stations close to the route.

    Important:
    Station coordinates must already exist in Location.

    This function deliberately does NOT geocode stations
    during an API request.
    """

    coordinates = route_geometry.get(
        "coordinates",
        [],
    )

    if not coordinates:
        return []

    bounding_box = get_route_bounding_box(
        route_geometry,
        buffer_degrees=0.2,
    )

    if not bounding_box:
        return []

    # Sample route points instead of checking every
    # geometry point.
    route_points = list(
        coordinates[::route_sample_step]
    )

    if coordinates[-1] not in route_points:
        route_points.append(
            coordinates[-1]
        )

    stations = (
        FuelStation.objects
        .select_related("location")
        .filter(location__isnull=False)
    )

    nearby_stations = []

    for station in stations:

        location = station.location

        station_lat = float(
            location.latitude
        )

        station_lon = float(
            location.longitude
        )

        # ----------------------------------------------------
        # Bounding-box pre-filter
        # ----------------------------------------------------

        if not (
            bounding_box["min_lon"]
            <= station_lon
            <= bounding_box["max_lon"]
        ):
            continue

        if not (
            bounding_box["min_lat"]
            <= station_lat
            <= bounding_box["max_lat"]
        ):
            continue

        # ----------------------------------------------------
        # Calculate minimum distance to sampled route points
        # ----------------------------------------------------

        minimum_distance = None

        for lon, lat in route_points:

            distance = calculate_distance_miles(
                station_lat,
                station_lon,
                lat,
                lon,
            )

            if (
                minimum_distance is None
                or distance < minimum_distance
            ):
                minimum_distance = distance

            if minimum_distance <= max_distance_miles:
                break

        if (
            minimum_distance is not None
            and minimum_distance <= max_distance_miles
        ):
            nearby_stations.append(
                {
                    "station": station,
                    "latitude": station_lat,
                    "longitude": station_lon,
                    "distance_from_route": round(
                        minimum_distance,
                        2,
                    ),
                    "price": float(
                        station.retail_price
                    ),
                }
            )

    return nearby_stations


# ============================================================
# Fuel Calculations
# ============================================================

def calculate_fuel_required(
    distance_miles,
    fuel_efficiency_mpg=10,
):
    """
    Calculate fuel required for a given distance.
    """

    if distance_miles < 0:
        raise ValueError(
            "distance_miles cannot be negative."
        )

    if fuel_efficiency_mpg <= 0:
        raise ValueError(
            "fuel_efficiency_mpg must be greater than zero."
        )

    return distance_miles / fuel_efficiency_mpg


def calculate_max_range(
    tank_capacity_gallons=100,
    fuel_efficiency_mpg=10,
):
    """
    Calculate maximum driving range with a full tank.
    """

    if tank_capacity_gallons <= 0:
        raise ValueError(
            "tank_capacity_gallons must be greater than zero."
        )

    if fuel_efficiency_mpg <= 0:
        raise ValueError(
            "fuel_efficiency_mpg must be greater than zero."
        )

    return (
        tank_capacity_gallons
        * fuel_efficiency_mpg
    )


def calculate_refueling_needed(
    distance_miles,
    fuel_efficiency_mpg=10,
    tank_capacity_gallons=100,
    starting_fuel_gallons=None,
):
    """
    Calculate the minimum number of refueling stops
    required based only on vehicle range.

    This does not select actual stations.
    """

    if starting_fuel_gallons is None:
        starting_fuel_gallons = (
            tank_capacity_gallons
        )

    if starting_fuel_gallons < 0:
        raise ValueError(
            "starting_fuel_gallons cannot be negative."
        )

    if starting_fuel_gallons > tank_capacity_gallons:
        raise ValueError(
            "starting_fuel_gallons cannot exceed "
            "tank_capacity_gallons."
        )

    current_range = (
        starting_fuel_gallons
        * fuel_efficiency_mpg
    )

    if distance_miles <= current_range:
        return 0

    remaining_distance = (
        distance_miles - current_range
    )

    max_range = calculate_max_range(
        tank_capacity_gallons,
        fuel_efficiency_mpg,
    )

    return math.ceil(
        remaining_distance / max_range
    )


def calculate_fuel_cost(
    fuel_gallons,
    price_per_gallon,
):
    """
    Calculate fuel cost.
    """

    if fuel_gallons < 0:
        raise ValueError(
            "fuel_gallons cannot be negative."
        )

    if price_per_gallon < 0:
        raise ValueError(
            "price_per_gallon cannot be negative."
        )

    return (
        fuel_gallons
        * price_per_gallon
    )


def calculate_fuel_details(
    distance_miles,
    fuel_efficiency_mpg=10,
    tank_capacity_gallons=100,
    starting_fuel_gallons=None,
):
    """
    Return all basic fuel calculations for a route.
    """

    fuel_required = calculate_fuel_required(
        distance_miles,
        fuel_efficiency_mpg,
    )

    max_range = calculate_max_range(
        tank_capacity_gallons,
        fuel_efficiency_mpg,
    )

    refueling_stops = calculate_refueling_needed(
        distance_miles,
        fuel_efficiency_mpg,
        tank_capacity_gallons,
        starting_fuel_gallons,
    )

    return {
        "distance_miles": round(
            distance_miles,
            2,
        ),
        "fuel_required_gallons": round(
            fuel_required,
            2,
        ),
        "fuel_efficiency_mpg": fuel_efficiency_mpg,
        "tank_capacity_gallons": tank_capacity_gallons,
        "max_range_miles": round(
            max_range,
            2,
        ),
        "refueling_stops": refueling_stops,
    }


# ============================================================
# Fuel Stop Helpers
# ============================================================

def calculate_fuel_needed_for_distance(
    distance_miles,
    fuel_efficiency_mpg,
):
    """
    Fuel needed to travel a specific distance.
    """

    if distance_miles < 0:
        raise ValueError(
            "distance_miles cannot be negative."
        )

    if fuel_efficiency_mpg <= 0:
        raise ValueError(
            "fuel_efficiency_mpg must be greater than zero."
        )

    return (
        distance_miles
        / fuel_efficiency_mpg
    )


def calculate_remaining_fuel(
    available_fuel_gallons,
    distance_miles,
    fuel_efficiency_mpg,
):
    """
    Calculate remaining fuel after travelling a distance.
    """

    fuel_needed = calculate_fuel_needed_for_distance(
        distance_miles,
        fuel_efficiency_mpg,
    )

    remaining_fuel = (
        available_fuel_gallons
        - fuel_needed
    )

    return max(
        remaining_fuel,
        0,
    )


def calculate_refuel_amount(
    current_fuel_gallons,
    tank_capacity_gallons,
):
    """
    Calculate how much fuel is needed to fill the tank.
    """

    if current_fuel_gallons < 0:
        raise ValueError(
            "current_fuel_gallons cannot be negative."
        )

    if tank_capacity_gallons <= 0:
        raise ValueError(
            "tank_capacity_gallons must be greater than zero."
        )

    if current_fuel_gallons > tank_capacity_gallons:
        raise ValueError(
            "current_fuel_gallons cannot exceed "
            "tank_capacity_gallons."
        )

    return (
        tank_capacity_gallons
        - current_fuel_gallons
    )


def calculate_stop_cost(
    fuel_gallons,
    price_per_gallon,
):
    """
    Calculate cost of fuel purchased at a stop.
    """

    return calculate_fuel_cost(
        fuel_gallons,
        price_per_gallon,
    )


# ============================================================
# Station Selection
# ============================================================

def choose_cheapest_station(
    stations,
    current_location,
    fuel_efficiency_mpg=10,
    tank_capacity_gallons=100,
    available_fuel_gallons=None,
):
    """
    Select the cheapest reachable station.

    Process:

    1. Use straight-line distance as a cheap pre-filter.
    2. Calculate actual driving distance for candidates.
    3. Keep only reachable stations.
    4. Select the lowest-price station.

    Note:
    This is a greedy selection strategy and does not
    mathematically guarantee the globally cheapest
    multi-stop route.
    """

    if available_fuel_gallons is None:
        available_fuel_gallons = (
            tank_capacity_gallons
        )

    if available_fuel_gallons < 0:
        raise ValueError(
            "available_fuel_gallons cannot be negative."
        )

    if available_fuel_gallons > tank_capacity_gallons:
        raise ValueError(
            "available_fuel_gallons cannot exceed "
            "tank_capacity_gallons."
        )

    current_lat = current_location["latitude"]
    current_lon = current_location["longitude"]

    current_range = (
        available_fuel_gallons
        * fuel_efficiency_mpg
    )

    # --------------------------------------------------------
    # Straight-line pre-filter
    # --------------------------------------------------------

    possible_stations = []

    for item in stations:

        straight_distance = calculate_distance_miles(
            current_lat,
            current_lon,
            item["latitude"],
            item["longitude"],
        )

        if (
            straight_distance
            <= current_range * 0.8
        ):
            possible_stations.append(
                item
            )

    # --------------------------------------------------------
    # Actual driving-distance validation
    # --------------------------------------------------------

    reachable_stations = []

    for item in possible_stations:

        driving_distance = (
            get_driving_distance_miles(
                current_lat,
                current_lon,
                item["latitude"],
                item["longitude"],
            )
        )

        if driving_distance <= current_range:

            reachable_stations.append(
                {
                    "station": item["station"],
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                    "distance_from_current": round(
                        driving_distance,
                        2,
                    ),
                    "price": item["price"],
                }
            )

    if not reachable_stations:
        return None

    return min(
        reachable_stations,
        key=lambda item: item["price"],
    )


# ============================================================
# Multiple Fuel Stops
# ============================================================

def calculate_multiple_fuel_stops(
    start_location,
    destination_location,
    candidate_stations,
    fuel_efficiency_mpg=10,
    tank_capacity_gallons=100,
    starting_fuel_gallons=None,
):
    """
    Calculate fuel stops using a greedy strategy.

    At each step:

        Current location
              ↓
        Can reach destination?
          ↓ yes        ↓ no
       Finish      Find reachable stations
                        ↓
                 Choose cheapest
                        ↓
                  Refuel fully
                        ↓
                  Continue

    The algorithm is practical for the assessment,
    but it is not a guaranteed global cost optimizer.
    """

    if fuel_efficiency_mpg <= 0:
        raise ValueError(
            "fuel_efficiency_mpg must be greater than zero."
        )

    if tank_capacity_gallons <= 0:
        raise ValueError(
            "tank_capacity_gallons must be greater than zero."
        )

    if starting_fuel_gallons is None:
        starting_fuel_gallons = (
            tank_capacity_gallons
        )

    if starting_fuel_gallons < 0:
        raise ValueError(
            "starting_fuel_gallons cannot be negative."
        )

    if starting_fuel_gallons > tank_capacity_gallons:
        raise ValueError(
            "starting_fuel_gallons cannot exceed "
            "tank_capacity_gallons."
        )

    current_location = {
        "latitude": start_location["latitude"],
        "longitude": start_location["longitude"],
    }

    current_fuel = starting_fuel_gallons

    remaining_stations = list(
        candidate_stations
    )

    visited_station_ids = set()

    stops = []

    total_distance = 0.0
    total_fuel_cost = 0.0

    while True:

        # ----------------------------------------------------
        # Can we directly reach the destination?
        # ----------------------------------------------------

        destination_distance = (
            get_driving_distance_miles(
                current_location["latitude"],
                current_location["longitude"],
                destination_location["latitude"],
                destination_location["longitude"],
            )
        )

        available_range = (
            current_fuel
            * fuel_efficiency_mpg
        )

        if destination_distance <= available_range:

            fuel_used = (
                destination_distance
                / fuel_efficiency_mpg
            )

            current_fuel = max(
                current_fuel - fuel_used,
                0,
            )

            total_distance += (
                destination_distance
            )

            break

        # ----------------------------------------------------
        # Find cheapest reachable station
        # ----------------------------------------------------

        available_stations = [
            station
            for station in remaining_stations
            if station["station"].id
            not in visited_station_ids
        ]

        selected_station = choose_cheapest_station(
            stations=available_stations,
            current_location=current_location,
            fuel_efficiency_mpg=fuel_efficiency_mpg,
            tank_capacity_gallons=tank_capacity_gallons,
            available_fuel_gallons=current_fuel,
        )

        if not selected_station:
            raise ValueError(
                "Destination cannot be reached with "
                "the available fuel and candidate "
                "fuel stations."
            )

        station = selected_station["station"]

        station_id = station.id

        # ----------------------------------------------------
        # Prevent repeated station selection
        # ----------------------------------------------------

        if station_id in visited_station_ids:
            raise ValueError(
                "Unable to find a valid sequence of "
                "fuel stations."
            )

        visited_station_ids.add(
            station_id
        )

        # ----------------------------------------------------
        # Travel to station
        # ----------------------------------------------------

        distance_to_station = (
            selected_station[
                "distance_from_current"
            ]
        )

        fuel_used = (
            distance_to_station
            / fuel_efficiency_mpg
        )

        current_fuel = max(
            current_fuel - fuel_used,
            0,
        )

        total_distance += (
            distance_to_station
        )

        # ----------------------------------------------------
        # Refuel to full tank
        # ----------------------------------------------------

        fuel_added = calculate_refuel_amount(
            current_fuel,
            tank_capacity_gallons,
        )

        stop_cost = calculate_stop_cost(
            fuel_added,
            selected_station["price"],
        )

        total_fuel_cost += stop_cost

        stops.append(
            {
                "station_id": station.id,
                "station_name": station.truckstop_name,
                "city": station.city,
                "state": station.state,
                "price_per_gallon": selected_station[
                    "price"
                ],
                "distance_from_previous_location": round(
                    distance_to_station,
                    2,
                ),
                "fuel_added_gallons": round(
                    fuel_added,
                    2,
                ),
                "stop_cost": round(
                    stop_cost,
                    2,
                ),
            }
        )

        # ----------------------------------------------------
        # Move to station and reset fuel
        # ----------------------------------------------------

        current_location = {
            "latitude": selected_station["latitude"],
            "longitude": selected_station["longitude"],
        }

        current_fuel = tank_capacity_gallons

        # Remove station from remaining candidates.
        remaining_stations = [
            item
            for item in remaining_stations
            if item["station"].id != station_id
        ]

    return {
        "stops": stops,
        "total_stops": len(stops),
        "total_distance_miles": round(
            total_distance,
            2,
        ),
        "total_fuel_cost": round(
            total_fuel_cost,
            2,
        ),
        "remaining_fuel_gallons": round(
            current_fuel,
            2,
        ),
    }