# RouteWise AI — Hackathon MVP

A small end-to-end travel-planning prototype. It demonstrates:
- Structured trip requirements
- Google Places attractions for the selected destination, with clearly labeled demo fallback data
- Budget calculation and deterministic budget repair
- Replanning when rafting is unavailable or more expensive
- UI for itinerary, expense breakdown, and validation status

## Run locally

Requires Python 3.10+.

```bash
uv venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

uv pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

API health: http://127.0.0.1:8000/api/health  
Interactive API docs: http://127.0.0.1:8000/docs
Attraction search example: http://127.0.0.1:8000/api/attractions?destination=Rishikesh&interests=nature&limit=5

Run the mocked API and budget regression tests:

```bash
python -m unittest -v test_app
```

## Demo flow
1. Click **Generate itinerary**.
2. Enable **Main activity allowance is higher** and click **Replan**.
3. Enable **Main activity unavailable** and click **Replan**.
4. Enter a starting city and adjust the estimated vehicle rate to include a Google Maps round-trip estimate.
5. Change trip length, travelers, or budget and verify that totals recalculate.

## Important limitations
- This is a mock-data prototype, not a live travel booking service.
- Google Places supplies attraction names, addresses, coordinates, and Maps links; it does not supply this app's accommodation, food, entry, or activity prices.
- If Places is unavailable or returns no usable results, Rishikesh uses explicitly labeled demo attractions; other destinations show an empty attraction list and an explanatory placeholder instead of Rishikesh data.
- Prices, times, and activities in fallback mode are illustrative estimates, not verified current quotes.
- `GET /api/attractions` accepts a destination, repeated `interests` query parameters, and a limit from 1 to 10. The itinerary API fetches a destination's attractions once per plan and reuses those results in its response.
- Enter a starting city to include Google Places-resolved origin/destination and Google Routes round-trip driving distance and duration in transport estimates. Set `GOOGLE_MAPS_API_KEY` in `.env`; enable Google Places API (New) and Routes API.
- The transport estimate multiplies the returned round-trip distance by the editable vehicle rate (default ₹15/km). Google Routes does not provide a fare quote, and vehicle rate is only an assumption.
- Without a starting city, home-to-destination travel remains excluded. Accommodation, food, activities, attraction hours, weather, permissions, and operator availability are not verified live.
- The schedule check is a placeholder; don't present it as real schedule validation.
- The current deterministic repair is intentionally simple and should be expanded before production.

## Next build step
Extend the existing Places and Routes integration to itinerary stops and add verified accommodation/activity pricing sources; preserve source/status labels and keep budget validation deterministic.
