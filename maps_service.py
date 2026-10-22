
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_SOURCE = "OpenStreetMap (Overpass API)"
CURATED_SOURCE = "Curated fallback (not live data)"
_CACHE_TTL_SECONDS = 30 * 60
_FALLBACK_CACHE_TTL_SECONDS = 5 * 60
_GEOCODE_CACHE_TTL_SECONDS = 24 * 60 * 60
_logger = logging.getLogger(__name__)
_attraction_cache: dict[str, tuple[float, list[dict]]] = {}
_coordinate_cache: dict[str, tuple[float, tuple[float, float] | None]] = {}
_geocode_lock = asyncio.Lock()
_last_geocode_request = 0.0

DESTINATION_COORDINATES = {
    "jaipur": (26.9124, 75.7873),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "rishikesh": (30.0869, 78.2676),
    "agra": (27.1767, 78.0081),
    "varanasi": (25.3176, 82.9739),
    "udaipur": (24.5854, 73.7125),
    "mumbai": (19.0760, 72.8777),
    "goa": (15.2993, 74.1240),
    "manali": (32.2432, 77.1892),
    "amritsar": (31.6340, 74.8723),
}

try:
    _configured_coordinates = json.loads(
        os.getenv("ROUTEWISE_DESTINATION_COORDINATES", "{}")
    )
    if not isinstance(_configured_coordinates, dict):
        raise ValueError("ROUTEWISE_DESTINATION_COORDINATES must be a JSON object.")
    for _name, _coordinates in _configured_coordinates.items():
        if (
            isinstance(_name, str)
            and isinstance(_coordinates, list)
            and len(_coordinates) == 2
            and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in _coordinates
            )
            and -90 <= _coordinates[0] <= 90
            and -180 <= _coordinates[1] <= 180
        ):
            DESTINATION_COORDINATES[_name.strip().casefold()] = (
                float(_coordinates[0]),
                float(_coordinates[1]),
            )
except (json.JSONDecodeError, ValueError) as exc:
    _logger.warning("Ignoring invalid destination coordinates configuration: %s", exc)

with (Path(__file__).resolve().parent / "attractions_fallback.json").open(
    encoding="utf-8"
) as _fallback_file:
    CURATED_ATTRACTIONS = json.load(_fallback_file)


def _headers(field_mask: str) -> dict:
    if not API_KEY:
        raise RuntimeError(
            "GOOGLE_MAPS_API_KEY is missing. Add it to your .env file."
        )

    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": field_mask,
    }


async def search_places(query: str, limit: int = 5) -> list[dict]:
    """Search real places and return basic details and opening hours."""
    headers = _headers(
        "places.id,places.displayName,places.formattedAddress,"
        "places.location,places.regularOpeningHours,"
        "places.currentOpeningHours,places.googleMapsUri"
    )

    payload = {
        "textQuery": query,
        "pageSize": max(1, min(limit, 10)),
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            PLACES_URL,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise httpx.DecodingError(
                "Google Places returned an invalid response."
            ) from exc

    results = []
    places = data.get("places", []) if isinstance(data, dict) else []
    if not isinstance(places, list):
        return results
    for place in places:
        if not isinstance(place, dict):
            continue
        display_name = place.get("displayName")
        display_name = display_name if isinstance(display_name, dict) else {}
        location = place.get("location")
        location = location if isinstance(location, dict) else {}
        name = display_name.get("text")
        results.append({
            "place_id": place.get("id"),
            "name": name if isinstance(name, str) else None,
            "address": place.get("formattedAddress"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "opening_hours": place.get("regularOpeningHours"),
            "current_opening_hours": place.get("currentOpeningHours"),
            "maps_url": place.get("googleMapsUri"),
        })

    return results


async def get_destination_attractions(
    destination: str,
    interests: list[str] | None = None,
    limit: int = 8,
) -> list[dict]:
    """Find nearby OSM attractions, using labeled curated data if needed."""
    if not isinstance(destination, str):
        raise ValueError("Destination must be text.")
    destination = destination.strip()
    if not destination or len(destination) > 200:
        raise ValueError("Destination must contain between 1 and 200 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("Attraction limit must be between 1 and 10.")
    if interests is None:
        interests = []
    if not isinstance(interests, list):
        raise ValueError("Interests must be provided as a list of text values.")

    normalized_interests = []
    for interest in interests:
        if not isinstance(interest, str):
            raise ValueError("Interests must be text values.")
        value = interest.strip()
        if not value or len(value) > 50:
            raise ValueError("Each interest must contain between 1 and 50 characters.")
        if value.casefold() not in {item.casefold() for item in normalized_interests}:
            normalized_interests.append(value)
    if len(normalized_interests) > 10:
        raise ValueError("No more than 10 interests may be requested.")

    center = await _destination_coordinates(destination)
    if center is None:
        _logger.info("No coordinates resolved for destination %r", destination)
        return get_curated_attractions(destination, limit)

    cache_key = f"{center[0]:.4f},{center[1]:.4f}"
    cached = _attraction_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        places = cached[1]
    else:
        try:
            elements = await _fetch_overpass(*center)
            places = _normalize_overpass_results(elements)
            if places:
                _attraction_cache[cache_key] = (
                    time.monotonic() + _CACHE_TTL_SECONDS,
                    places,
                )
            else:
                _logger.info("Overpass returned no attractions for %s", destination)
        except httpx.HTTPError as exc:
            _logger.warning(
                "Overpass lookup failed for %s; using curated data: %s",
                destination,
                exc,
            )
            places = []
        if not places:
            _attraction_cache[cache_key] = (
                time.monotonic() + _FALLBACK_CACHE_TTL_SECONDS,
                [],
            )

    if not places:
        return get_curated_attractions(destination, limit)

    places = [dict(place) for place in places]
    if normalized_interests:
        places.sort(key=lambda place: _interest_score(place, normalized_interests), reverse=True)
    return places[:limit]


def get_curated_attractions(destination: str, limit: int = 8) -> list[dict]:
    """Return explicitly labeled destination-specific fallback records."""
    records = CURATED_ATTRACTIONS.get(destination.strip().casefold(), [])
    return [
        {
            "place_id": None,
            "name": item["name"],
            "address": item.get("address"),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "category": item["category"],
            "maps_url": (
                item.get("maps_url")
                or "https://www.google.com/maps/search/?api=1&query="
                + quote(f"{item['name']} {destination}")
            ),
            "source": CURATED_SOURCE,
        }
        for item in records[:limit]
    ]


async def _destination_coordinates(destination: str) -> tuple[float, float] | None:
    global _last_geocode_request

    key = destination.casefold()
    if key in DESTINATION_COORDINATES:
        return DESTINATION_COORDINATES[key]

    now = time.monotonic()
    cached = _coordinate_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    user_agent = os.getenv("NOMINATIM_USER_AGENT", "").strip()
    if not user_agent or "RouteWise" not in user_agent or not (
        "@" in user_agent or "https://" in user_agent
    ):
        _logger.warning(
            "Unknown destination %r was not geocoded; set NOMINATIM_USER_AGENT "
            "with a RouteWise app identifier and contact URL or email.",
            destination,
        )
        _coordinate_cache[key] = (now + _GEOCODE_CACHE_TTL_SECONDS, None)
        return None

    async with _geocode_lock:
        delay = 1.0 - (time.monotonic() - _last_geocode_request)
        if delay > 0:
            await asyncio.sleep(delay)
        _last_geocode_request = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
                response = await client.get(
                    NOMINATIM_URL,
                    params={"q": destination, "format": "jsonv2", "limit": 1},
                    headers={"User-Agent": user_agent},
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise httpx.DecodingError("Overpass returned invalid JSON.") from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            _logger.warning("Nominatim lookup failed for %s: %s", destination, exc)
            _coordinate_cache[key] = (
                time.monotonic() + _FALLBACK_CACHE_TTL_SECONDS,
                None,
            )
            return None

    coordinates = None
    if isinstance(data, list) and data:
        result = data[0]
        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                coordinates = (latitude, longitude)
        except (KeyError, TypeError, ValueError):
            _logger.warning("Nominatim returned invalid coordinates for %s", destination)
    _coordinate_cache[key] = (
        time.monotonic() + _GEOCODE_CACHE_TTL_SECONDS,
        coordinates,
    )
    return coordinates


async def _fetch_overpass(latitude: float, longitude: float) -> list[dict]:
    radius_meters = 12_000
    query = (
        "[out:json][timeout:20];("
        f'nwr(around:{radius_meters},{latitude},{longitude})'
        '["tourism"~"^(attraction|museum|viewpoint|gallery|artwork)$"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})["historic"];'
        ");out center tags 100;"
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=5.0)) as client:
        response = await client.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "RouteWiseAI/0.1 (OpenStreetMap attraction search)"},
        )
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise httpx.DecodingError("Overpass returned an invalid response.")
    return data["elements"]


def _normalize_overpass_results(elements: list[dict]) -> list[dict]:
    results = []
    seen_ids = set()
    seen_names = set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        name = tags.get("name") or tags.get("name:en")
        if not isinstance(name, str) or not name.strip():
            continue
        element_type = element.get("type")
        element_id = element.get("id")
        place_id = (
            f"{element_type}/{element_id}"
            if element_type in {"node", "way", "relation"} and element_id is not None
            else None
        )
        name_key = name.strip().casefold()
        if (place_id and place_id in seen_ids) or name_key in seen_names:
            continue

        center = element.get("center")
        center = center if isinstance(center, dict) else {}
        latitude = element.get("lat", center.get("lat"))
        longitude = element.get("lon", center.get("lon"))
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            continue

        if place_id:
            seen_ids.add(place_id)
        seen_names.add(name_key)
        address = tags.get("addr:full")
        if not address:
            address_parts = [
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
                tags.get("addr:suburb"),
                tags.get("addr:city"),
                tags.get("addr:state"),
            ]
            address = ", ".join(part for part in address_parts if part)
        category = tags.get("tourism") or tags.get("historic") or "attraction"
        results.append({
            "place_id": place_id,
            "name": name.strip(),
            "address": address or None,
            "latitude": latitude,
            "longitude": longitude,
            "category": category,
            "maps_url": (
                f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}"
                f"#map=17/{latitude}/{longitude}"
            ),
            "source": OSM_SOURCE,
        })
    return results


def _interest_score(place: dict, interests: list[str]) -> int:
    category = place["category"].casefold()
    mappings = {
        "culture": {"museum", "gallery", "artwork", "historic", "attraction"},
        "history": {"historic", "museum", "attraction"},
        "nature": {"viewpoint", "attraction"},
        "art": {"artwork", "gallery"},
        "architecture": {"historic", "attraction"},
    }
    return sum(
        1
        for interest in interests
        if category in mappings.get(interest.casefold(), {interest.casefold()})
    )


async def get_route(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    travel_mode: str = "DRIVE",
) -> dict:
    """Get a real route between two coordinates."""
    allowed_modes = {"DRIVE", "WALK", "BICYCLE", "TWO_WHEELER"}

    if travel_mode not in allowed_modes:
        raise ValueError(f"Unsupported travel mode: {travel_mode}")

    headers = _headers(
        "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"
    )

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": origin_lat,
                    "longitude": origin_lng,
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": destination_lat,
                    "longitude": destination_lng,
                }
            }
        },
        "travelMode": travel_mode,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            ROUTES_URL,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    routes = data.get("routes", [])
    if not routes:
        raise ValueError("Google Routes returned no route for these locations.")

    route = routes[0]
    duration_text = route.get("duration", "0s")
    duration_seconds = round(float(duration_text.removesuffix("s")))

    return {
        "distance_meters": route.get("distanceMeters"),
        "duration_seconds": duration_seconds,
        "duration_minutes": round(duration_seconds / 60),
        "encoded_polyline": route.get("polyline", {}).get(
            "encodedPolyline"
        ),
    }
