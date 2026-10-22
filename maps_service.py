
import json
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


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
    interests: list[str],
    limit: int = 5,
) -> list[dict]:
    """Search and normalize real attractions for a destination."""
    if not isinstance(destination, str):
        raise ValueError("Destination must be text.")
    destination = destination.strip()
    if not destination or len(destination) > 200:
        raise ValueError("Destination must contain between 1 and 200 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("Attraction limit must be between 1 and 10.")
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

    interest_query = " ".join(normalized_interests)
    query = f"tourist attractions in {destination}"
    if interest_query:
        query += f" for {interest_query}"

    places = await search_places(query, limit)
    attractions = []
    seen_ids = set()
    seen_names = set()
    for place in places:
        name = place.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        place_id = place.get("place_id")
        place_id = place_id if isinstance(place_id, str) and place_id else None
        normalized_name = name.strip()
        name_key = normalized_name.casefold()
        if (place_id and place_id in seen_ids) or name_key in seen_names:
            continue
        if place_id:
            seen_ids.add(place_id)
        seen_names.add(name_key)
        attractions.append({
            "place_id": place_id,
            "name": normalized_name,
            "address": place.get("address"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "maps_url": place.get("maps_url"),
            "source": "Google Places API (New)",
        })

    return attractions


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
