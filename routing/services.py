import requests
from django.conf import settings
from .models import Location
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .redis_client import redis_client



def get_route(start_lon, start_lat, end_lon, end_lat):
    url = "https://api.geoapify.com/v1/routing"

    params = {
        "waypoints": f"{start_lat},{start_lon}|{end_lat},{end_lon}",
        "mode": "drive",
        "apiKey": settings.GEOAPIFY_API_KEY,
    }

    response = requests.get(url, params=params, timeout=10)

    response.raise_for_status()

    return response.json()

def get_route_distance_miles(route):
    distance_meters = route["features"][0]["properties"]["distance"]

    distance_miles = distance_meters / 1609.344

    return distance_miles

def geocode_address(address):
    url = "https://api.geoapify.com/v1/geocode/search"

    params = {
        "text": address,
        "filter": "countrycode:us",
        "limit": 5,
        "apiKey": settings.GEOAPIFY_API_KEY,
        "format": "json",
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    data = response.json()
    results = data.get("results", [])

    if not results:
        return None

    # Select the result with the highest confidence
    best_result = max(
        results,
        key=lambda result: result.get("rank", {}).get("confidence", 0)
    )

    confidence = best_result.get("rank", {}).get("confidence", 0)

    if confidence <= 0:
        return None

    return {
        "latitude": best_result.get("lat"),
        "longitude": best_result.get("lon"),
    }

def calculate_fuel_stops_required(distance_miles, max_range=500):
    if distance_miles <= max_range:
        return 0

    return int(distance_miles // max_range)


def normalize_location(address):
    return address.strip().lower()


def get_or_create_location(address):
    location_key = normalize_location(address)

    cache_key = f"location:{location_key}"
    lock_key = f"location_lock:{location_key}"

    # 1. Check Redis cache
    cached = redis_client.get(cache_key)

    if cached:
        latitude, longitude = cached.split(",")

        return {
            "latitude": float(latitude),
            "longitude": float(longitude),
        }

    # 2. Try to acquire lock
    lock_acquired = redis_client.set(
        lock_key,
        "1",
        nx=True,
        ex=30,
    )

    if lock_acquired:

        try:
            # 3. Check Redis AGAIN
            #
            # Another request may have populated
            # the cache just before we acquired the lock.
            cached = redis_client.get(cache_key)

            if cached:
                latitude, longitude = cached.split(",")

                return {
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                }

            # 4. Check PostgreSQL
            location = Location.objects.filter(
                location_key=location_key
            ).first()

            if location:

                redis_client.set(
                    cache_key,
                    f"{location.latitude},{location.longitude}",
                )

                return {
                    "latitude": float(location.latitude),
                    "longitude": float(location.longitude),
                }

            # 5. Finally call Geoapify
            coordinates = geocode_address(address)

            if not coordinates:
                return None

            # 6. Save to PostgreSQL
            location = Location.objects.create(
                location_key=location_key,
                latitude=coordinates["latitude"],
                longitude=coordinates["longitude"],
                formatted_name=address,
            )

            # 7. Save to Redis
            redis_client.set(
                cache_key,
                f"{coordinates['latitude']},{coordinates['longitude']}",
            )

            return {
                "latitude": float(location.latitude),
                "longitude": float(location.longitude),
            }

        finally:
            # 8. Release lock
            redis_client.delete(lock_key)

    # Another request currently has the lock.
    # Wait briefly and check Redis again.
    import time

    for _ in range(10):
        time.sleep(0.2)

        cached = redis_client.get(cache_key)

        if cached:
            latitude, longitude = cached.split(",")

            return {
                "latitude": float(latitude),
                "longitude": float(longitude),
            }

    return None   