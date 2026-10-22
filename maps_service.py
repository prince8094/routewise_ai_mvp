
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PHOTON_URL = "https://photon.komoot.io/api/"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_SOURCE = "OpenStreetMap results"
CURATED_SOURCE = "Curated fallback (not live data)"
_CACHE_TTL_SECONDS = 30 * 60
_FALLBACK_CACHE_TTL_SECONDS = 5 * 60
_GEOCODE_CACHE_TTL_SECONDS = 24 * 60 * 60
_logger = logging.getLogger(__name__)
_attraction_cache: dict[str, tuple[float, list[dict]]] = {}
_coordinate_cache: dict[str, tuple[float, tuple[float, float] | None]] = {}
_geocode_lock = asyncio.Lock()
_overpass_lock = asyncio.Lock()
_last_geocode_request = 0.0
_suggestion_cache: dict[str, tuple[float, list[dict]]] = {}
_suggestion_rate: dict[str, list[float]] = {}
_suggestion_cache_ttl = 15 * 60
_suggestion_client_min_interval = 0.25
_suggestion_client_max_per_minute = 30

DESTINATION_COORDINATES = {
    "jaipur": (26.9124, 75.7873),
    "delhi": (28.6139, 77.2090),
    "new delhi": (28.6139, 77.2090),
    "mumbai": (19.0760, 72.8777),
    "agra": (27.1767, 78.0081),
    "udaipur": (24.5854, 73.7125),
    "jodhpur": (26.2389, 73.0243),
    "varanasi": (25.3176, 82.9739),
    "rishikesh": (30.0869, 78.2676),
    "haridwar": (29.9457, 78.1642),
    "amritsar": (31.6340, 74.8723),
    "shimla": (31.1048, 77.1734),
    "manali": (32.2396, 77.1887),
    "bengaluru": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "chennai": (13.0827, 80.2707),
    "kolkata": (22.5726, 88.3639),
    "pune": (18.5204, 73.8567),
    "goa": (15.2993, 74.1240),
}
DESTINATION_ALIASES = {
    "new delhi": "delhi",
    "bangalore": "bengaluru",
}
for _alias, _canonical in DESTINATION_ALIASES.items():
    DESTINATION_COORDINATES[_alias] = DESTINATION_COORDINATES[_canonical]

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
    _curated_records = json.load(_fallback_file)

CURATED_ATTRACTIONS: dict[str, list[dict]] = {}
for _record in _curated_records:
    if not isinstance(_record, dict):
        continue
    _record_destination = _record.get("destination")
    _record_latitude = _record.get("latitude")
    _record_longitude = _record.get("longitude")
    if (
        not isinstance(_record_destination, str)
        or not isinstance(_record.get("name"), str)
        or not isinstance(_record.get("category"), str)
        or not isinstance(_record.get("maps_url"), str)
        or not _record["maps_url"].startswith("https://www.openstreetmap.org/")
        or not isinstance(_record_latitude, (int, float))
        or not isinstance(_record_longitude, (int, float))
        or not -90 <= _record_latitude <= 90
        or not -180 <= _record_longitude <= 180
    ):
        raise ValueError(f"Invalid curated attraction record: {_record!r}")
    CURATED_ATTRACTIONS.setdefault(_record_destination.casefold(), []).append(_record)


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
    limit: int = 15,
    latitude: float | None = None,
    longitude: float | None = None,
    context: str | None = None,
) -> list[dict]:
    """Find nearby OSM attractions, using labeled curated data if needed."""
    if not isinstance(destination, str):
        raise ValueError("Destination must be text.")
    destination = destination.strip()
    if not destination or len(destination) > 200:
        raise ValueError("Destination must contain between 1 and 200 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30:
        raise ValueError("Attraction limit must be between 1 and 30.")
    if context is not None and (not isinstance(context, str) or len(context) > 250):
        raise ValueError("Destination context must be no longer than 250 characters.")
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

    if (latitude is None) != (longitude is None):
        raise ValueError("Both destination latitude and longitude are required.")
    if latitude is not None and (
        not -90 <= latitude <= 90 or not -180 <= longitude <= 180
    ):
        raise ValueError("Destination coordinates are out of range.")
    center = (
        (latitude, longitude)
        if latitude is not None and longitude is not None
        else await _destination_coordinates(destination)
    )
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
    for place in places:
        place["destination"] = (
            f"{destination}, {context.strip()}"
            if context and context.strip()
            else destination
        )
    places.sort(
        key=lambda place: (
            -_interest_score(place, normalized_interests),
            _attraction_rank(place),
            _distance_squared(place, center),
            place["name"].casefold(),
        )
    )
    if len(places) < limit:
        seen_names = {place["name"].casefold() for place in places}
        for curated_place in get_curated_attractions(destination, limit):
            if curated_place["name"].casefold() not in seen_names:
                places.append(curated_place)
                seen_names.add(curated_place["name"].casefold())
                if len(places) >= limit:
                    break
    return places[:limit]


async def suggest_destinations(
    query: str,
    client_key: str,
    limit: int = 6,
) -> list[dict]:
    """Query Photon for destination suggestions with bounded usage and caching."""
    normalized_query = " ".join(query.strip().split())
    if len(normalized_query) < 3 or len(normalized_query) > 100:
        raise ValueError("Search text must contain 3 to 100 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError("Suggestion limit must be between 1 and 8.")

    cache_key = f"{normalized_query.casefold()}:{limit}"
    now = time.monotonic()
    cached = _suggestion_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1][:limit]

    recent = [
        request_time
        for request_time in _suggestion_rate.get(client_key, [])
        if now - request_time < 60
    ]
    if len(recent) >= _suggestion_client_max_per_minute:
        raise SuggestionRateLimitError("Too many destination searches. Please wait a minute.")
    if recent and now - recent[-1] < _suggestion_client_min_interval:
        raise SuggestionRateLimitError("Please wait briefly before searching again.")
    recent.append(now)
    _suggestion_rate[client_key] = recent

    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
        response = await client.get(
            PHOTON_URL,
            params={"q": normalized_query, "limit": limit, "lang": "en"},
            headers={"User-Agent": "RouteWiseAI/0.1 (destination autocomplete)"},
        )
        response.raise_for_status()
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise httpx.DecodingError("Photon returned invalid JSON.") from exc

    features = data.get("features", []) if isinstance(data, dict) else []
    if not isinstance(features, list):
        raise httpx.DecodingError("Photon returned an invalid feature list.")
    suggestions = []
    seen = set()
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
            or not isinstance(coordinates[0], (int, float))
            or not isinstance(coordinates[1], (int, float))
        ):
            continue
        longitude, latitude = coordinates[:2]
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        name = properties.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        context = [
            properties.get(field)
            for field in ("district", "city", "county", "state", "country")
            if isinstance(properties.get(field), str)
            and properties[field].strip()
            and properties[field].casefold() != name.casefold()
        ]
        context = list(dict.fromkeys(context))
        display_name = ", ".join([name.strip(), *context])
        key = (display_name.casefold(), round(latitude, 5), round(longitude, 5))
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({
            "display_name": display_name,
            "name": name.strip(),
            "context": context,
            "latitude": latitude,
            "longitude": longitude,
            "place_type": properties.get("type"),
            "source": "Photon / OpenStreetMap",
        })
    _suggestion_cache[cache_key] = (
        time.monotonic() + _suggestion_cache_ttl,
        suggestions,
    )
    return suggestions[:limit]


class SuggestionRateLimitError(Exception):
    """Raised when a client exceeds autocomplete request limits."""


def get_curated_attractions(destination: str, limit: int = 15) -> list[dict]:
    """Return explicitly labeled destination-specific fallback records."""
    records = CURATED_ATTRACTIONS.get(_destination_key(destination), [])
    return [
        {
            "place_id": None,
            "destination": item["destination"],
            "name": item["name"],
            "address": item.get("address"),
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "category": item["category"],
            "maps_url": item["maps_url"],
            "source": CURATED_SOURCE,
        }
        for item in records[:limit]
    ]


def _destination_key(destination: str) -> str:
    key = " ".join(destination.strip().casefold().replace(",", " ").split())
    if key.endswith(" india"):
        key = key[:-6].strip()
    return DESTINATION_ALIASES.get(key, key)


def _attraction_rank(place: dict) -> int:
    category = place.get("category", "").casefold()
    if any(
        term in category
        for term in (
            "attraction", "museum", "historic", "monument", "temple", "fort",
            "castle", "memorial", "archaeological", "ruin", "palace",
        )
    ):
        return 0
    if any(term in category for term in ("viewpoint", "park", "garden", "beach", "waterfall")):
        return 1
    return 2


def _distance_squared(place: dict, center: tuple[float, float]) -> float:
    latitude_delta = place["latitude"] - center[0]
    longitude_delta = place["longitude"] - center[1]
    return latitude_delta * latitude_delta + longitude_delta * longitude_delta


async def _destination_coordinates(destination: str) -> tuple[float, float] | None:
    global _last_geocode_request

    key = _destination_key(destination)
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
                    raise httpx.DecodingError("Nominatim returned invalid JSON.") from exc
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
        '["tourism"~"^(attraction|museum|viewpoint|gallery|artwork|theme_park|zoo|aquarium|water_park|wilderness_hut)$"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})["historic"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})'
        '["leisure"~"^(park|nature_reserve|water_park)$"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})'
        '["amenity"~"^(arts_centre|theatre)$"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})'
        '["natural"~"^(cave_entrance|waterfall)$"];'
        ");out center tags 500;"
    )
    async with _overpass_lock:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0)
        ) as client:
            for attempt in range(2):
                try:
                    response = await client.post(
                        OVERPASS_URL,
                        data={"data": query},
                        headers={
                            "User-Agent": "RouteWiseAI/0.1 (OpenStreetMap attraction search)"
                        },
                    )
                    response.raise_for_status()
                    try:
                        data = response.json()
                    except json.JSONDecodeError as exc:
                        raise httpx.DecodingError(
                            "Overpass returned invalid JSON."
                        ) from exc
                    break
                except httpx.HTTPStatusError as exc:
                    if attempt or exc.response.status_code < 500 and exc.response.status_code != 429:
                        raise
                    await asyncio.sleep(0.5 if exc.response.status_code != 429 else 1.0)
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt:
                        raise
                    await asyncio.sleep(0.5)
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
        category = (
            tags.get("tourism")
            or tags.get("historic")
            or tags.get("leisure")
            or tags.get("amenity")
            or tags.get("natural")
            or "attraction"
        )
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
        "culture": {
            "museum", "gallery", "artwork", "historic", "attraction", "fort",
            "castle", "palace", "temple", "monument", "memorial",
        },
        "history": {
            "historic", "museum", "attraction", "fort", "castle", "palace",
            "monument", "memorial", "ruins",
        },
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
