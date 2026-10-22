# RouteWise AI — Hackathon MVP

A FastAPI travel-planning prototype with destination attraction discovery through OpenStreetMap (OSM), a small curated fallback, modeled trip costs, and optional Google Maps route estimates.

## Windows setup and local run

Install Python 3.10+ and [`uv`](https://docs.astral.sh/uv/getting-started/installation/). In PowerShell, from the project directory:

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
uv run uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. API health is at `/api/health`; interactive docs are at `/docs`.

Create a `.env` file manually only if configuring optional Google features or unknown-destination Nominatim geocoding.

Try attraction discovery:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/attractions?destination=Jaipur&limit=8"
```

The endpoint accepts `destination`, repeated `interests` parameters, and a `limit` from 1 to 10 (default 8). It returns the destination, count, source, and normalized attraction records with name, optional address, coordinates, category, source, and a map search link.

Run the mocked API, attraction, and budget tests:

```powershell
uv run python -m unittest -v test_app
```

## Configuration

Attraction search does not require a Google API key. Jaipur, Delhi, and Rishikesh (plus other common Indian destinations) have built-in coordinate centers. Add/override centers with a JSON object in `ROUTEWISE_DESTINATION_COORDINATES`, for example:

```text
ROUTEWISE_DESTINATION_COORDINATES={"pune":[18.5204,73.8567]}
```

Unknown destinations are geocoded through Nominatim only when `NOMINATIM_USER_AGENT` is configured with the application name and a real contact URL or email, for example `RouteWiseAI/0.1 (https://your-project.example/contact)`. Nominatim calls are cached and throttled to at most one per second. If valid identification is not configured or geocoding fails, the app uses destination-specific curated records where available; it does not send a request on each keystroke.

Google integration remains optional:

- `GOOGLE_MAPS_API_KEY` is only needed for the existing `/api/places` and Google Routes endpoints/route estimates.
- Without it, attraction search continues to use OSM. Route estimates may be unavailable; OSM attraction discovery does not provide routing.
- Google Places and Routes API enablement and billing are required only if you choose to use those optional Google features.

## Deployment to Google Cloud Run

Install and authenticate the Google Cloud CLI, select a project with Cloud Run and source-build APIs enabled, and ensure that project has the billing configuration required by Google Cloud Run. From PowerShell:

```powershell
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud run deploy routewise-ai `
  --source . `
  --region YOUR_REGION `
  --allow-unauthenticated `
  --set-build-env-vars "GOOGLE_ENTRYPOINT=uvicorn app:app --host 0.0.0.0 --port 8080" `
  --set-env-vars "NOMINATIM_USER_AGENT=RouteWiseAI/0.1 (https://your-project.example/contact)"
```

Use a real contact URL or email in the Nominatim user agent. Set `GOOGLE_MAPS_API_KEY` as a Cloud Run secret/environment variable only if enabling optional Google routing or Places. Do not commit API keys to source control.

## Data sources and limitations

- Attraction search queries the public Overpass API with a 12 km radius and a bounded query timeout; results are cached in-process to reduce public-server traffic. OSM records can be incomplete or outdated.
- The small JSON fallback for Jaipur, Delhi, and Rishikesh is curated reference data, clearly labeled **“Curated fallback (not live data)”**. It is used when Overpass has no results or is unavailable; it is not presented as live data. Fallback coordinates/addresses are omitted rather than guessed.
- Display OSM data with the required attribution: **© OpenStreetMap contributors**. OSM provides map data, not routing, travel times, prices, opening hours, ratings, or live availability.
- Trip prices and activity allowances are illustrative estimates. The daily itinerary is not schedule-validated.
- Optional Google Routes estimates require Google Maps place lookup and route computation. Without Google configuration (or if those APIs fail), route functionality is unavailable and intercity travel is excluded from the plan.

## Demo flow

1. Generate an itinerary for Rishikesh, Jaipur, or Delhi.
2. Change destination and move focus away to refresh the plan; attraction discovery is not triggered on each keystroke.
3. Try the simulated activity constraints and review the labeled data sources and estimated costs.
4. To include optional Google round-trip route estimates, configure the Google key, APIs, and a starting city.
