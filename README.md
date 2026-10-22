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

The endpoint accepts `destination`, repeated `interests` parameters, and a `limit` from 1 to 30 (default 15). It returns the destination, count, source, and normalized attraction records with name, optional address, coordinates, category, source, and a map search link.

Destination and starting-city fields use the backend endpoint `/api/destinations/suggest?q=bang`. It uses Photon’s documented `https://photon.komoot.io/api/` search endpoint, shows the place context, and returns selected coordinates to the trip planner. Requests are debounced in the browser (350 ms), limited in result count and frequency on the server, and cached for 15 minutes. Photon describes its hosted demo as suitable for reasonable use but warns that extensive requests may be throttled or banned; for higher traffic, run a private Photon instance or choose a supported provider. Source and API terms: <https://github.com/komoot/photon> and <https://photon.komoot.io/>.

Run the mocked API, attraction, and budget tests:

```powershell
uv run python -m unittest -v test_app
```

## Configuration

Attraction search does not require a Google API key. Jaipur, Delhi, and Rishikesh (plus other common Indian destinations) have built-in coordinate centers. Add/override centers with a JSON object in `ROUTEWISE_DESTINATION_COORDINATES`, for example:

```text
ROUTEWISE_DESTINATION_COORDINATES={"pune":[18.5204,73.8567]}
```

Delhi/New Delhi, Bangalore/Bengaluru, and other configured names resolve to shared city centers when a suggestion is not selected. A selected Photon suggestion sends its actual latitude/longitude and region context to the attraction search, avoiding reliance on a city-center guess. Unknown free-text destinations are geocoded through Nominatim only when `NOMINATIM_USER_AGENT` is configured with the application name and a real contact URL or email, for example `RouteWiseAI/0.1 (https://your-project.example/contact)`. Nominatim calls are cached and throttled to at most one per second. If valid identification is not configured or geocoding fails, the app uses destination-specific curated records where available; it does not send a request on each keystroke.

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
- The JSON dataset contains 10 city-specific curated reference attractions for each of Delhi, Jaipur, Mumbai, Agra, Udaipur, Jodhpur, Varanasi, Rishikesh, Haridwar, Amritsar, Shimla, Manali, Bengaluru, Hyderabad, Chennai, Kolkata, Pune, and Goa. When the live list is shorter than requested, unique curated records may supplement it; each record is labeled by its source, and curated records are clearly marked **“Curated fallback (not live data)”**. Curated points are approximate and are never presented as live results.
- Display OSM data with the required attribution: **© OpenStreetMap contributors**. OSM provides map data, not routing, travel times, prices, opening hours, ratings, or live availability.
- Trip prices and activity allowances are illustrative estimates. The daily itinerary is not schedule-validated.
- Optional Google Routes estimates require Google Maps place lookup and route computation. Without Google configuration (or if those APIs fail), route functionality is unavailable and intercity travel is excluded from the plan.

## Demo flow

1. Type at least three characters into the destination or starting-city field, then choose a contextual autocomplete result with the keyboard or mouse.
2. Destination attraction discovery uses the chosen coordinates; fallback records are used only when live data is unavailable or insufficient.
3. Try the simulated activity constraints and review the labeled data sources and estimated costs.
4. To include optional Google round-trip route estimates, configure the Google key, APIs, and a starting city.
