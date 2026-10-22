import asyncio
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from maps_service import (
    API_KEY,
    OSM_SOURCE,
    get_curated_attractions,
    get_destination_attractions,
    get_route,
    search_places,
    suggest_destinations,
    SuggestionRateLimitError,
)

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="RouteWise AI MVP", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

class TripRequest(BaseModel):
    destination: str = Field(default="Rishikesh", min_length=1, max_length=200)
    origin: Optional[str] = Field(default=None, min_length=1, max_length=200)
    destination_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    destination_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    destination_context: Optional[str] = Field(default=None, max_length=250)
    origin_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    days: int = Field(default=3, ge=1, le=10)
    travelers: int = Field(default=2, ge=1, le=20)
    budget_per_person: int = Field(default=5000, ge=1)
    transport_rate_per_km: float = Field(default=15, gt=0, le=1000)
    interests: List[str] = Field(
        default_factory=lambda: ["adventure", "nature", "culture"],
        max_length=10,
    )
    rafting_closed: bool = False
    expensive_rafting: bool = False

    @model_validator(mode="after")
    def validate_location_coordinate_pairs(self):
        if (self.destination_latitude is None) != (self.destination_longitude is None):
            raise ValueError("Destination latitude and longitude must be provided together.")
        if (self.origin_latitude is None) != (self.origin_longitude is None):
            raise ValueError("Origin latitude and longitude must be provided together.")
        return self

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Destination cannot be blank.")
        return value

    @field_validator("interests")
    @classmethod
    def validate_interests(cls, values: List[str]) -> List[str]:
        interests = [value.strip() for value in values if value.strip()]
        if len(interests) > 10 or any(len(value) > 50 for value in interests):
            raise ValueError("Provide up to 10 interests, each no longer than 50 characters.")
        return interests

ACTIVITIES = {
    "beatles": {
        "id": "beatles", "name": "Beatles Ashram", "category": "culture",
        "duration_minutes": 150, "cost_per_person": 250, "source": "demo estimate",
        "time": "1:00 PM", "description": "Heritage walk and murals; verify current entry hours and fee."
    },
    "aarti": {
        "id": "aarti", "name": "Ganga Aarti at Triveni Ghat", "category": "culture",
        "duration_minutes": 75, "cost_per_person": 0, "source": "demo estimate",
        "time": "6:00 PM", "description": "Cultural visit; confirm local timing on the day."
    },
    "rafting": {
        "id": "rafting", "name": "Guided river rafting", "category": "adventure",
        "duration_minutes": 210, "cost_per_person": 1000, "source": "demo estimate",
        "time": "9:00 AM", "description": "Conditional on operator availability, official permission, weather and river safety."
    },
    "waterfall": {
        "id": "waterfall", "name": "Neer Garh Waterfall hike", "category": "nature",
        "duration_minutes": 180, "cost_per_person": 100, "source": "demo estimate",
        "time": "9:00 AM", "description": "Check trail access and weather; do not hike in unsafe conditions."
    },
    "yoga": {
        "id": "yoga", "name": "Budget yoga / local experience", "category": "culture",
        "duration_minutes": 90, "cost_per_person": 300, "source": "demo estimate",
        "time": "10:00 AM", "description": "Alternative experience; verify provider and current price."
    },
    "viewpoint": {
        "id": "viewpoint", "name": "Nearby river viewpoint and local walk", "category": "nature",
        "duration_minutes": 90, "cost_per_person": 0, "source": "demo estimate",
        "time": "2:00 PM", "description": "Low-cost flexible activity; route and access should be checked."
    }
}


# Illustrative per-person cost assumptions for RouteWise AI.
# Add or adjust destinations as needed. These are NOT live quotes.
DESTINATION_COSTS = {
    "rishikesh": {
        "hotel_per_night": 700,
        "food_per_day": 400,
        "local_transport_per_day": 170,
        "entry_fees": 250,
        "main_activity": 1000,
        "experience": 300,
    },
    "jaipur": {
        "hotel_per_night": 1000,
        "food_per_day": 500,
        "local_transport_per_day": 250,
        "entry_fees": 500,
        "main_activity": 400,
        "experience": 300,
    },
    "manali": {
        "hotel_per_night": 1200,
        "food_per_day": 500,
        "local_transport_per_day": 350,
        "entry_fees": 300,
        "main_activity": 1200,
        "experience": 400,
    },
    "goa": {
        "hotel_per_night": 1500,
        "food_per_day": 600,
        "local_transport_per_day": 350,
        "entry_fees": 300,
        "main_activity": 1000,
        "experience": 500,
    },
}

DEFAULT_COSTS = {
    "hotel_per_night": 900,
    "food_per_day": 450,
    "local_transport_per_day": 250,
    "entry_fees": 300,
    "main_activity": 700,
    "experience": 300,
}

def get_destination_costs(destination: str) -> dict:
    """Return the configured estimates for a destination."""
    key = destination.strip().casefold()
    profile = DESTINATION_COSTS.get(key, DEFAULT_COSTS)
    return dict(profile)


def make_expenses(
    req: TripRequest,
    rafting_cost: int,
    intercity_transport_cost: int = 0,
    include_local_experience: bool = True,
    include_activity_allowance: bool = True,
):
    costs = get_destination_costs(req.destination)
    travelers = req.travelers

    nights = max(0, req.days - 1)
    items = [
        {
            "name": "Accommodation",
            "per_person": costs["hotel_per_night"] * nights,
            "status": "estimated",
        },
        {
            "name": "Food and drinking water",
            "per_person": costs["food_per_day"] * req.days,
            "status": "estimated",
        },
        {
            "name": "Local transport",
            "per_person": costs["local_transport_per_day"] * req.days,
            "status": "estimated",
        },
        {"name": "Entry fees", "per_person": costs["entry_fees"], "status": "estimated"},
    ]
    if include_activity_allowance:
        items.append({
            "name": "Activity allowance",
            "per_person": rafting_cost,
            "status": "estimated",
        })
    if include_local_experience:
        items.append({
            "name": "Local experience",
            "per_person": costs["experience"],
            "status": "estimated",
        })
    result = [
        {
            **item,
            "group_cost": item["per_person"] * travelers,
        }
        for item in items
    ]
    if intercity_transport_cost:
        result.append({
            "name": "Intercity transport (round trip)",
            "per_person": round(intercity_transport_cost / travelers),
            "group_cost": intercity_transport_cost,
            "status": "route-based estimate",
        })

    subtotal_group = sum(item["group_cost"] for item in result)
    reserve_group = round(subtotal_group * 0.10)
    result.append({
        "name": "Contingency reserve (10%)",
        "per_person": round(reserve_group / travelers),
        "group_cost": reserve_group,
        "status": "reserve",
    })
    return result


async def _fetch_intercity_route(
    req: TripRequest,
) -> Optional[dict]:
    if not req.origin:
        return None
    if not API_KEY:
        return None

    origin_query = req.origin.strip()
    destination_query = req.destination.strip()
    if not origin_query or not destination_query:
        raise HTTPException(
            status_code=422,
            detail="Origin and destination are required for route-based transport estimates.",
        )

    try:
        origin_matches, destination_matches = await asyncio.gather(
            search_places(origin_query, 1)
            if req.origin_latitude is None or req.origin_longitude is None
            else asyncio.sleep(0, result=[]),
            search_places(destination_query, 1)
            if req.destination_latitude is None or req.destination_longitude is None
            else asyncio.sleep(0, result=[]),
        )
        origin_place = origin_matches[0] if origin_matches else {}
        destination_place = destination_matches[0] if destination_matches else {}
        origin_latitude = (
            req.origin_latitude
            if req.origin_latitude is not None
            else origin_place.get("latitude")
        )
        origin_longitude = (
            req.origin_longitude
            if req.origin_longitude is not None
            else origin_place.get("longitude")
        )
        destination_latitude = (
            req.destination_latitude
            if req.destination_latitude is not None
            else destination_place.get("latitude")
        )
        destination_longitude = (
            req.destination_longitude
            if req.destination_longitude is not None
            else destination_place.get("longitude")
        )
        if origin_latitude is None or origin_longitude is None:
            raise ValueError(f"Google Places could not find the origin: {origin_query}.")
        if destination_latitude is None or destination_longitude is None:
            raise ValueError(
                f"Google Places could not find the destination: {destination_query}."
            )
        coordinates = (
            origin_latitude,
            origin_longitude,
            destination_latitude,
            destination_longitude,
        )
        if any(value is None for value in coordinates):
            raise ValueError("Google Places did not return coordinates for the route.")

        outbound, return_route = await asyncio.gather(
            get_route(*coordinates, travel_mode="DRIVE"),
            get_route(
                coordinates[2],
                coordinates[3],
                coordinates[0],
                coordinates[1],
                travel_mode="DRIVE",
            ),
        )
        for route in (outbound, return_route):
            if not isinstance(route.get("distance_meters"), (int, float)):
                raise ValueError("Google Routes did not return a route distance.")
            if not isinstance(route.get("duration_seconds"), (int, float)):
                raise ValueError("Google Routes did not return a route duration.")
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Google Maps is not configured on the server.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Google Maps route lookup failed. Verify API enablement, billing, and quota.",
        ) from exc

    distance_meters = outbound["distance_meters"] + return_route["distance_meters"]
    duration_seconds = outbound["duration_seconds"] + return_route["duration_seconds"]
    distance_km = distance_meters / 1000
    estimated_cost = round(distance_km * req.transport_rate_per_km)

    return {
        "source": "Google Places + Google Routes",
        "travel_mode": "DRIVE",
        "origin": (
            origin_place.get("address") or origin_place.get("name") or req.origin
        ),
        "destination": (
            destination_place.get("address")
            or destination_place.get("name")
            or req.destination
        ),
        "distance_meters": distance_meters,
        "duration_seconds": duration_seconds,
        "rate_per_km": req.transport_rate_per_km,
        "estimated_cost": estimated_cost,
    }


def _fallback_attractions(destination: str) -> tuple[list[dict], str]:
    places = get_curated_attractions(destination, limit=10)
    if places:
        return places, "Curated fallback (not live data)"
    return [], "No live or curated attractions found"


def _attraction_source(attractions: list[dict]) -> str:
    sources = list(dict.fromkeys(
        item.get("source")
        for item in attractions
        if isinstance(item.get("source"), str) and item["source"]
    ))
    return " + ".join(sources) if sources else "No live or curated attraction results"


async def _load_plan_attractions(req: TripRequest) -> tuple[list[dict], str]:
    try:
        attractions = await get_destination_attractions(
            req.destination,
            req.interests,
            limit=min(30, req.days * 2),
            latitude=req.destination_latitude,
            longitude=req.destination_longitude,
            context=req.destination_context,
        )
    except RuntimeError:
        attractions = []
        source = "OpenStreetMap lookup unavailable"
    except httpx.HTTPError:
        attractions = []
        source = "OpenStreetMap lookup unavailable"
    else:
        source = _attraction_source(attractions) if attractions else "No live OSM results"

    usable = []
    seen_ids = set()
    seen_names = set()
    for attraction in attractions:
        name = attraction.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        place_id = attraction.get("place_id")
        name_key = name.strip().casefold()
        if (
            (isinstance(place_id, str) and place_id in seen_ids)
            or name_key in seen_names
        ):
            continue
        if isinstance(place_id, str) and place_id:
            seen_ids.add(place_id)
        seen_names.add(name_key)
        usable.append(attraction)

    if not usable:
        fallback, fallback_source = _fallback_attractions(req.destination)
        source = fallback_source
        usable = fallback

    return usable[: min(30, req.days * 2)], source


def _make_itinerary(
    req: TripRequest,
    attractions: list[dict],
    reduced_scope: bool = False,
) -> list[dict]:
    itinerary = []
    places_per_day = 1 if reduced_scope else 2
    for day_number in range(1, req.days + 1):
        start = (day_number - 1) * places_per_day
        day_places = attractions[start:start + places_per_day]
        items = []
        for place in day_places:
            items.append({
                "id": place.get("place_id"),
                "name": place["name"],
                "category": place.get("category", "attraction"),
                "address": place.get("address"),
                "latitude": place.get("latitude"),
                "longitude": place.get("longitude"),
                "maps_url": place.get("maps_url"),
                "source": place.get("source", OSM_SOURCE),
                "description": place.get("description"),
                "time": "Flexible",
                "duration_minutes": None,
                "cost_per_person": None,
            })
        if not items:
            no_results_name = (
                "No additional attractions scheduled"
                if attractions
                else f"No attractions found for {req.destination}"
            )
            no_results_description = (
                "No more unique attractions were returned for this trip."
                if attractions
                else (
                    f"No attraction results are available for {req.destination}. "
                    "Try another supported Indian city or check the spelling."
                )
            )
            items.append({
                "id": None,
                "name": no_results_name,
                "category": "notice",
                "address": None,
                "latitude": None,
                "longitude": None,
                "maps_url": None,
                "source": "No live or curated attraction results",
                "description": no_results_description,
                "time": "Flexible",
                "duration_minutes": None,
                "cost_per_person": None,
            })
        itinerary.append({
            "day": day_number,
            "title": f"Explore {req.destination} · Day {day_number}",
            "items": items,
        })
    return itinerary


def build_plan(
    req: TripRequest,
    intercity_route: Optional[dict] = None,
    attractions: Optional[list[dict]] = None,
    attraction_source: Optional[str] = None,
):
    budget = req.budget_per_person * req.travelers
    destination_costs = get_destination_costs(req.destination)
    rafting_cost = destination_costs["main_activity"]
    intercity_transport_cost = (
        intercity_route["estimated_cost"] if intercity_route else 0
    )

    if req.expensive_rafting:
        rafting_cost = round(rafting_cost * 1.6)
    warnings = []
    if attractions is None:
        attractions, fallback_source = _fallback_attractions(req.destination)
        attraction_source = attraction_source or fallback_source
    if intercity_route:
        warnings.append(
            "Round-trip driving distance and time use Google Maps. Transport cost is "
            "an estimate based on the configured per-kilometer rate, not a live fare."
        )
    else:
        warnings.append(
            "Home-to-destination travel is excluded. Optional Google Places and "
            "Routes configuration is required for a route estimate."
        )
    if req.rafting_closed:
        warnings.append(
            "The main activity was marked unavailable in this demo scenario; "
            "its modeled allowance was removed."
        )
        rafting_cost = 0
    elif req.expensive_rafting:
        warnings.append("Activity allowance increased for the simulated higher-cost option.")

    if attraction_source:
        warnings.append(f"Attraction data: {attraction_source}.")
    if any(item.get("source") == "Curated fallback (not live data)" for item in attractions):
        warnings.append(
            "Curated fallback attraction names are not live-verified; coordinates and "
            "addresses are not provided."
        )
    warnings.append(
        "Accommodation, food, entry fees, local transport, and activity allowances "
        "are estimates; OpenStreetMap does not provide prices, opening hours, or availability."
    )

    expenses = make_expenses(req, rafting_cost, intercity_transport_cost)
    total = sum(x["group_cost"] for x in expenses)
    reduced_scope = False
    if total > budget:
        warnings.append(
            f"Initial plan exceeded budget by ₹{total-budget:,}. "
            "Reducing the modeled activity allowance and itinerary scope."
        )
        reduced_scope = True
        rafting_cost = 0
        expenses = make_expenses(
            req,
            rafting_cost,
            intercity_transport_cost,
            include_local_experience=False,
            include_activity_allowance=False,
        )
        total = sum(x["group_cost"] for x in expenses)
    if total > budget:
        warnings.append("No feasible plan found under the current modeled costs. Reduce scope or increase the budget.")
    else:
        warnings.append("Budget check passed for modeled costs. Estimates are not live quotes.")
    itinerary = _make_itinerary(req, attractions, reduced_scope)
    return {
        "destination": req.destination,
        "days": req.days,
        "travelers": req.travelers,
        "budget_per_person": req.budget_per_person,
        "budget_total": budget,
        "expenses": expenses,
        "total_cost": total,
        "remaining": budget-total,
        "valid": total <= budget,
        "itinerary": itinerary,
        "attractions": attractions,
        "attraction_source": attraction_source,
        "warnings": warnings,
        "data_mode": (
            f"{attraction_source or 'Attraction source unavailable'}; "
            f"{'Google route distance + modeled fare' if intercity_route else 'intercity travel excluded'}; "
            "costs are estimates, not live prices"
        ),
        "travel_route": intercity_route,
        "checks": {
            "budget": total <= budget,
            "schedule": intercity_route is not None,
            "availability": not req.rafting_closed,
            "note": (
                "Google Maps route time is included for the round trip; the daily "
                "itinerary is not schedule-validated."
                if intercity_route
                else "No intercity route was requested; daily itinerary timing is not validated."
            ),
        }
    }

@app.get("/")
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.post("/api/plan")
async def plan(req: TripRequest):
    (attractions, attraction_source), route = await asyncio.gather(
        _load_plan_attractions(req),
        _fetch_intercity_route(req),
    )
    return build_plan(req, route, attractions, attraction_source)

@app.post("/api/replan")
async def replan(req: TripRequest):
    (attractions, attraction_source), route = await asyncio.gather(
        _load_plan_attractions(req),
        _fetch_intercity_route(req),
    )
    result = build_plan(req, route, attractions, attraction_source)
    result["warnings"].insert(0, "Replanning executed from current constraints and demo tool data.")
    return result

@app.get("/api/health")
def health():
    return {"status": "ok", "message": "RouteWise AI MVP API is running"}


@app.get("/api/places")
async def api_places(
    query: str = Query(
        default="adventure activities in Rishikesh",
        min_length=3,
        max_length=200,
    ),
    limit: int = Query(default=5, ge=1, le=10),
):
    try:
        places = await search_places(query, limit)
        return {
            "source": "Google Places API (New)",
            "count": len(places),
            "places": places,
        }
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Google Places is not configured on the server.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Places lookup failed. Verify API enablement, billing, and quota.",
        ) from exc


@app.get("/api/destinations/suggest")
async def api_destination_suggestions(
    request: Request,
    q: str = Query(min_length=3, max_length=100),
    limit: int = Query(default=6, ge=1, le=8),
):
    client_key = request.client.host if request.client else "unknown"
    try:
        suggestions = await suggest_destinations(q, client_key, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SuggestionRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Destination search is temporarily unavailable. Try again shortly.",
        ) from exc
    return {"query": q.strip(), "count": len(suggestions), "suggestions": suggestions}


@app.get("/api/attractions")
async def api_attractions(
    destination: str = Query(min_length=1, max_length=200),
    interests: List[str] = Query(default=[]),
    limit: int = Query(default=15, ge=1, le=30),
    latitude: Optional[float] = Query(default=None, ge=-90, le=90),
    longitude: Optional[float] = Query(default=None, ge=-180, le=180),
    context: Optional[str] = Query(default=None, max_length=250),
):
    try:
        attractions = await get_destination_attractions(
            destination,
            interests,
            limit,
            latitude,
            longitude,
            context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Attraction lookup is temporarily unavailable.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenStreetMap attraction lookup failed.",
        ) from exc

    response = {
        "destination": destination.strip(),
        "count": len(attractions),
        "source": _attraction_source(attractions),
        "attractions": attractions,
    }
    if not attractions:
        response["message"] = (
            f"No live or curated attractions found for {destination.strip()}. "
            "Try another supported Indian city or check the spelling."
        )
    return response


@app.get("/api/route")
async def api_route(
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    travel_mode: str = "DRIVE",
):
    try:
        route = await get_route(
            origin_lat,
            origin_lng,
            destination_lat,
            destination_lng,
            travel_mode,
        )
        return {
            "source": "Google Routes API",
            **route,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Google Routes is not configured on the server.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Route lookup failed. Verify API enablement, billing, and quota.",
        ) from exc
