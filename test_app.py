import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

import app
from app import (
    TripRequest,
    _fetch_intercity_route,
    build_plan,
    make_expenses,
)
from maps_service import (
    CURATED_ATTRACTIONS,
    DESTINATION_COORDINATES,
    get_destination_attractions,
    get_route,
    suggest_destinations,
)


class ExpenseTests(unittest.TestCase):
    def test_destination_costs_use_trip_length_and_group_size(self):
        request = TripRequest(destination="Rishikesh", days=3, travelers=2)

        expenses = make_expenses(request, rafting_cost=1000)
        expenses_by_name = {expense["name"]: expense for expense in expenses}

        self.assertEqual(expenses_by_name["Accommodation"]["per_person"], 1400)
        self.assertEqual(expenses_by_name["Food and drinking water"]["per_person"], 1200)
        self.assertEqual(expenses_by_name["Local transport"]["per_person"], 510)
        self.assertEqual(expenses_by_name["Accommodation"]["group_cost"], 2800)

    def test_one_day_trip_has_no_accommodation_night(self):
        request = TripRequest(destination="Jaipur", days=1, travelers=1)

        expenses = make_expenses(request, rafting_cost=400)
        expenses_by_name = {expense["name"]: expense for expense in expenses}

        self.assertEqual(expenses_by_name["Accommodation"]["per_person"], 0)
        self.assertEqual(expenses_by_name["Food and drinking water"]["per_person"], 500)


class RouteEstimateTests(unittest.TestCase):
    def test_google_duration_is_converted_to_seconds_and_minutes(self):
        route_response = {
            "routes": [
                {
                    "distanceMeters": 12000,
                    "duration": "125.4s",
                    "polyline": {"encodedPolyline": "example"},
                }
            ]
        }

        with (
            patch("maps_service.API_KEY", "test-key"),
            patch(
                "maps_service.httpx.AsyncClient",
                return_value=MagicMock(),
            ) as async_client,
        ):
            response = MagicMock()
            response.json.return_value = route_response
            client = MagicMock()
            client.post = AsyncMock(return_value=response)
            async_client.return_value.__aenter__ = AsyncMock(
                return_value=client
            )

            result = asyncio.run(get_route(1, 2, 3, 4))

        self.assertEqual(result["distance_meters"], 12000)
        self.assertEqual(result["duration_seconds"], 125)
        self.assertEqual(result["duration_minutes"], 2)

    def test_fetches_both_route_directions_and_models_the_round_trip_fare(self):
        request = TripRequest(
            origin="New Delhi",
            destination="Rishikesh",
            travelers=2,
            budget_per_person=25000,
            transport_rate_per_km=15,
        )
        origin = {
            "latitude": 28.6,
            "longitude": 77.2,
            "address": "New Delhi, India",
        }
        destination = {
            "latitude": 30.1,
            "longitude": 78.3,
            "address": "Rishikesh, India",
        }
        routes = [
            {"distance_meters": 250000, "duration_seconds": 18000},
            {"distance_meters": 255000, "duration_seconds": 18600},
        ]

        with (
            patch("app.API_KEY", "test-key"),
            patch(
                "app.search_places",
                new_callable=AsyncMock,
                side_effect=[[origin], [destination]],
            ),
            patch(
                "app.get_route",
                new_callable=AsyncMock,
                side_effect=routes,
            ) as get_route_mock,
        ):
            route = asyncio.run(_fetch_intercity_route(request))

        self.assertEqual(route["distance_meters"], 505000)
        self.assertEqual(route["duration_seconds"], 36600)
        self.assertEqual(route["estimated_cost"], 7575)
        self.assertEqual(get_route_mock.await_count, 2)

        plan = build_plan(request, route)
        expenses_by_name = {expense["name"]: expense for expense in plan["expenses"]}
        self.assertEqual(
            expenses_by_name["Intercity transport (round trip)"]["group_cost"],
            7575,
        )
        self.assertEqual(plan["travel_route"]["source"], "Google Places + Google Routes")

    def test_route_lookup_is_skipped_when_google_is_not_configured(self):
        request = TripRequest(
            origin="Delhi",
            destination="Rishikesh",
            origin_latitude=28.6139,
            origin_longitude=77.2090,
            destination_latitude=30.0869,
            destination_longitude=78.2676,
        )
        with patch("app.API_KEY", None), patch("app.get_route", new_callable=AsyncMock) as route:
            result = asyncio.run(_fetch_intercity_route(request))

        self.assertIsNone(result)
        route.assert_not_awaited()


class AttractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import maps_service

        maps_service._attraction_cache.clear()

    async def test_overpass_request_uses_bounded_radius_and_attraction_tag_filters(self):
        import maps_service

        response = MagicMock()
        response.json.return_value = {"elements": []}
        response.raise_for_status = MagicMock()
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        async_client = MagicMock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=None)

        with patch("maps_service.httpx.AsyncClient", return_value=async_client):
            elements = await maps_service._fetch_overpass(28.6, 77.2)

        self.assertEqual(elements, [])
        request = client.post.await_args
        self.assertEqual(request.args[0], maps_service.OVERPASS_URL)
        query = request.kwargs["data"]["data"]
        self.assertIn("around:12000,28.6,77.2", query)
        self.assertIn("theme_park", query)
        self.assertIn('["historic"]', query)
        self.assertIn("out center tags 500", query)

    async def test_destination_attractions_queries_overpass_and_normalizes_osm(self):
        elements = [{
            "type": "way",
            "id": 123,
            "center": {"lat": 26.9855, "lon": 75.8513},
            "tags": {
                "name": "Amber Fort",
                "tourism": "attraction",
                "addr:city": "Jaipur",
            },
        }]
        with patch("maps_service._fetch_overpass", new_callable=AsyncMock) as query:
            query.return_value = elements
            result = await get_destination_attractions(
                "Jaipur",
                ["culture", "architecture"],
                3,
            )

        query.assert_awaited_once_with(26.9124, 75.7873)
        self.assertEqual(result[0]["name"], "Amber Fort")
        self.assertEqual(result[0]["place_id"], "way/123")
        self.assertEqual(result[0]["address"], "Jaipur")
        self.assertEqual(result[0]["category"], "attraction")
        self.assertEqual(result[0]["source"], "OpenStreetMap results")
        self.assertIn("openstreetmap.org", result[0]["maps_url"])

    async def test_selected_coordinates_are_used_for_overpass_search(self):
        with patch("maps_service._fetch_overpass", new_callable=AsyncMock, return_value=[]) as query:
            await get_destination_attractions(
                "Bang",
                limit=5,
                latitude=12.9716,
                longitude=77.5946,
            )
        query.assert_awaited_once_with(12.9716, 77.5946)

    async def test_photon_suggestions_normalize_place_context_and_cache_results(self):
        import maps_service

        maps_service._suggestion_cache.clear()
        maps_service._suggestion_rate.clear()
        response = MagicMock()
        response.json.return_value = {
            "features": [
                {
                    "geometry": {"coordinates": [77.5946, 12.9716]},
                    "properties": {
                        "name": "Bengaluru",
                        "city": "Bengaluru",
                        "state": "Karnataka",
                        "country": "India",
                        "type": "city",
                    },
                }
            ]
        }
        response.raise_for_status = MagicMock()
        client = MagicMock()
        client.get = AsyncMock(return_value=response)
        async_client = MagicMock()
        async_client.__aenter__ = AsyncMock(return_value=client)
        async_client.__aexit__ = AsyncMock(return_value=None)
        with patch("maps_service.httpx.AsyncClient", return_value=async_client) as factory:
            result = await suggest_destinations("bang", "test-client")
            cached_result = await suggest_destinations("bang", "test-client")

        self.assertEqual(result[0]["name"], "Bengaluru")
        self.assertEqual(result[0]["context"], ["Karnataka", "India"])
        self.assertEqual(result[0]["latitude"], 12.9716)
        self.assertEqual(result[0]["longitude"], 77.5946)
        self.assertEqual(cached_result, result)
        factory.assert_called_once()

    async def test_destination_attractions_skips_unusable_and_duplicate_results(self):
        with patch("maps_service._fetch_overpass", new_callable=AsyncMock) as query:
            query.return_value = [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 26.9,
                    "lon": 75.8,
                    "tags": {"name": "Hawa Mahal", "historic": "monument"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 26.9,
                    "lon": 75.8,
                    "tags": {"name": "Hawa Mahal"},
                },
                {"type": "node", "id": 3, "lat": 26.9, "lon": 75.8, "tags": {"name": "  "}},
            ]
            result = await get_destination_attractions("Jaipur", [], 5)

        self.assertEqual(result[0]["name"], "Hawa Mahal")
        self.assertEqual(len(result), 5)
        self.assertEqual(
            len({place["name"].casefold() for place in result}),
            len(result),
        )
        self.assertEqual(result[0]["latitude"], 26.9)

    async def test_short_live_result_list_is_filled_with_labeled_curated_records(self):
        live_results = [
            {
                "type": "node",
                "id": index,
                "lat": 28.6,
                "lon": 77.2,
                "tags": {
                    "name": "Red Fort" if index == 0 else f"Live Delhi attraction {index}",
                    "tourism": "museum",
                },
            }
            for index in range(8)
        ]
        with patch(
            "maps_service._fetch_overpass",
            new_callable=AsyncMock,
            return_value=live_results,
        ):
            attractions = await get_destination_attractions("Delhi", limit=15)

        self.assertEqual(len(attractions), 15)
        self.assertEqual(attractions[0]["source"], "OpenStreetMap results")
        self.assertEqual(attractions[-1]["source"], "Curated fallback (not live data)")
        self.assertEqual(len({item["name"].casefold() for item in attractions}), 15)

    async def test_overpass_failure_returns_curated_fallback_honestly(self):
        with patch(
            "maps_service._fetch_overpass",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectTimeout("upstream unavailable"),
        ) as query:
            result = await get_destination_attractions("Delhi", limit=2)
            cached_result = await get_destination_attractions("Delhi", limit=1)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "Red Fort")
        self.assertEqual(result[0]["source"], "Curated fallback (not live data)")
        self.assertIsNotNone(result[0]["latitude"])
        self.assertIn("openstreetmap.org", result[0]["maps_url"])
        self.assertEqual(len(cached_result), 1)
        query.assert_awaited_once()

    async def test_city_aliases_resolve_and_return_city_specific_curated_places(self):
        with patch("maps_service._fetch_overpass", new_callable=AsyncMock, return_value=[]):
            for city, expected in (
                ("Delhi", "Red Fort"),
                ("New Delhi", "Red Fort"),
                ("Jaipur", "Hawa Mahal"),
                ("Bangalore", "Bangalore Palace"),
            ):
                places = await get_destination_attractions(city, limit=15)
                self.assertTrue(places, city)
                self.assertEqual(places[0]["name"], expected)
                self.assertEqual(places[0]["source"], "Curated fallback (not live data)")
                self.assertIsNotNone(places[0]["latitude"])
                self.assertIsNotNone(places[0]["longitude"])
                self.assertIn("openstreetmap.org", places[0]["maps_url"])

    async def test_all_required_cities_have_ten_valid_destination_specific_records(self):
        expected_cities = {
            "delhi", "jaipur", "mumbai", "agra", "udaipur", "jodhpur",
            "varanasi", "rishikesh", "haridwar", "amritsar", "shimla",
            "manali", "bengaluru", "hyderabad", "chennai", "kolkata",
            "pune", "goa",
        }
        self.assertTrue(expected_cities.issubset(CURATED_ATTRACTIONS))
        for city in expected_cities:
            records = CURATED_ATTRACTIONS[city]
            self.assertGreaterEqual(len(records), 10, city)
            self.assertEqual(len({record["name"] for record in records}), len(records))
            for record in records:
                self.assertEqual(record["destination"].casefold(), city)
                self.assertIn("openstreetmap.org", record["maps_url"])
                self.assertTrue(record["category"])
                self.assertTrue(-90 <= record["latitude"] <= 90)
                self.assertTrue(-180 <= record["longitude"] <= 180)
            self.assertIn(city, DESTINATION_COORDINATES)

    async def test_every_supported_city_returns_curated_results_when_overpass_is_empty(self):
        cities = (
            "Delhi", "Jaipur", "Mumbai", "Agra", "Udaipur", "Jodhpur",
            "Varanasi", "Rishikesh", "Haridwar", "Amritsar", "Shimla",
            "Manali", "Bengaluru", "Hyderabad", "Chennai", "Kolkata",
            "Pune", "Goa",
        )
        with patch("maps_service._fetch_overpass", new_callable=AsyncMock, return_value=[]):
            for city in cities:
                attractions = await get_destination_attractions(city, limit=15)
                self.assertGreaterEqual(len(attractions), 10, city)
                self.assertTrue(all(place["source"] == "Curated fallback (not live data)" for place in attractions))
                self.assertEqual(attractions[0]["destination"].casefold(), city.casefold())

    async def test_unsupported_city_without_geocoding_has_no_fabricated_results(self):
        with patch.dict("os.environ", {"NOMINATIM_USER_AGENT": ""}):
            import maps_service

            maps_service._coordinate_cache.pop("unsupported town", None)
            result = await get_destination_attractions("Unsupported Town", limit=15)
        self.assertEqual(result, [])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)

    def test_health_endpoint_still_works(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_destination_suggestions_endpoint_returns_photon_places(self):
        suggestions = [{
            "display_name": "Bengaluru, Karnataka, India",
            "name": "Bengaluru",
            "context": ["Karnataka", "India"],
            "latitude": 12.9716,
            "longitude": 77.5946,
            "place_type": "city",
            "source": "Photon / OpenStreetMap",
        }]
        with patch(
            "app.suggest_destinations",
            new_callable=AsyncMock,
            return_value=suggestions,
        ) as suggest:
            response = self.client.get("/api/destinations/suggest?q=bang")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["suggestions"], suggestions)
        self.assertEqual(suggest.await_args.args[0], "bang")

    def test_destination_suggestions_validate_input_and_throttle(self):
        self.assertEqual(
            self.client.get("/api/destinations/suggest?q=ab").status_code,
            422,
        )
        with patch(
            "app.suggest_destinations",
            new_callable=AsyncMock,
            side_effect=app.SuggestionRateLimitError("wait"),
        ):
            response = self.client.get("/api/destinations/suggest?q=bang")
        self.assertEqual(response.status_code, 429)

    def test_plan_uses_selected_destination_coordinates(self):
        with (
            patch("app.get_destination_attractions", new_callable=AsyncMock, return_value=[]) as lookup,
            patch("app._fetch_intercity_route", new_callable=AsyncMock, return_value=None),
        ):
            response = self.client.post(
                "/api/plan",
                json={
                    "destination": "Bengaluru",
                    "destination_latitude": 12.9716,
                    "destination_longitude": 77.5946,
                    "days": 1,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(lookup.await_args.kwargs["latitude"], 12.9716)
        self.assertEqual(lookup.await_args.kwargs["longitude"], 77.5946)

    def test_existing_home_places_and_route_endpoints_still_work(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        with (
            patch(
                "app.search_places",
                new_callable=AsyncMock,
                return_value=[{"name": "Sample place"}],
            ),
            patch(
                "app.get_route",
                new_callable=AsyncMock,
                return_value={"distance_meters": 1000, "duration_seconds": 120},
            ),
        ):
            places = self.client.get("/api/places?query=Jaipur%20attractions")
            route = self.client.get(
                "/api/route?origin_lat=1&origin_lng=2"
                "&destination_lat=3&destination_lng=4"
            )

        self.assertEqual(places.status_code, 200)
        self.assertEqual(places.json()["count"], 1)
        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["distance_meters"], 1000)

    def test_attractions_endpoint_returns_normalized_destination_results(self):
        place = {
            "place_id": "jaipur-1",
            "name": "Hawa Mahal",
            "address": "Jaipur, Rajasthan",
            "latitude": 26.9239,
            "longitude": 75.8267,
            "maps_url": "https://maps.google.com/?cid=1",
            "source": "Google Places API (New)",
        }
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            return_value=[place],
        ) as get_attractions:
            response = self.client.get(
                "/api/attractions?destination=Jaipur&interests=culture&limit=1"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["destination"], "Jaipur")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["attractions"][0]["place_id"], "jaipur-1")
        self.assertEqual(get_attractions.await_args.args[:2], ("Jaipur", ["culture"]))

    def test_attractions_endpoint_defaults_to_fifteen_without_google_api_key(self):
        import maps_service

        maps_service._attraction_cache.clear()
        elements = [
            {
                "type": "node",
                "id": index,
                "lat": 26.9,
                "lon": 75.8,
                "tags": {"name": f"Jaipur Museum {index}", "tourism": "museum"},
            }
            for index in range(20)
        ]
        with (
            patch("maps_service.API_KEY", None),
            patch("maps_service._fetch_overpass", new_callable=AsyncMock, return_value=elements),
        ):
            response = self.client.get("/api/attractions?destination=Jaipur")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 15)
        self.assertEqual(response.json()["attractions"][0]["name"], "Jaipur Museum 0")
        self.assertEqual(
            response.json()["attractions"][0]["source"],
            "OpenStreetMap results",
        )

    def test_delhi_api_request_with_limit_15_uses_curated_data_if_overpass_has_no_results(self):
        import maps_service

        maps_service._attraction_cache.clear()
        with patch(
            "maps_service._fetch_overpass",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = self.client.get(
                "/api/attractions?destination=Delhi&limit=15"
            )
            plan_response = self.client.post(
                "/api/plan",
                json={"destination": "Delhi", "days": 2},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 10)
        self.assertEqual(response.json()["source"], "Curated fallback (not live data)")
        self.assertEqual(response.json()["attractions"][0]["name"], "Red Fort")
        self.assertEqual(response.json()["attractions"][-1]["name"], "Purana Qila")
        self.assertEqual(plan_response.status_code, 200)
        self.assertEqual(plan_response.json()["attractions"][0]["name"], "Red Fort")
        self.assertEqual(
            plan_response.json()["attraction_source"],
            "Curated fallback (not live data)",
        )

    def test_unsupported_destination_returns_a_friendly_empty_result(self):
        import maps_service

        maps_service._coordinate_cache.pop("unsupported island", None)
        with patch.dict("os.environ", {"NOMINATIM_USER_AGENT": ""}):
            response = self.client.get(
                "/api/attractions?destination=Unsupported%20Island&limit=15"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attractions"], [])
        self.assertIn("Try another supported Indian city", response.json()["message"])

    def test_rishikesh_attractions_endpoint_returns_normalized_record(self):
        place = {
            "place_id": "rishikesh-1",
            "name": "Triveni Ghat",
            "address": "Rishikesh, Uttarakhand",
            "latitude": 30.1,
            "longitude": 78.3,
            "maps_url": "https://maps.google.com/?cid=2",
            "source": "Google Places API (New)",
        }
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            return_value=[place],
        ) as get_attractions:
            response = self.client.get("/api/attractions?destination=Rishikesh")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["destination"], "Rishikesh")
        self.assertEqual(response.json()["attractions"][0], place)
        self.assertEqual(get_attractions.await_args.args[0], "Rishikesh")

    def test_attractions_rejects_invalid_limit_and_sanitizes_upstream_failures(self):
        self.assertEqual(
            self.client.get("/api/attractions?destination=Jaipur&limit=31").status_code,
            422,
        )
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectTimeout("secret upstream details"),
        ):
            response = self.client.get("/api/attractions?destination=Jaipur")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret upstream details", response.text)
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("private server configuration"),
        ):
            response = self.client.get("/api/attractions?destination=Jaipur")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private server configuration", response.text)

    def test_empty_attractions_return_empty_api_results_and_curated_plan_fallback(self):
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = self.client.get("/api/attractions?destination=Jaipur")
            plan_response = self.client.post(
                "/api/plan",
                json={"destination": "Jaipur", "days": 2},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attractions"], [])
        self.assertEqual(plan_response.status_code, 200)
        plan = plan_response.json()
        self.assertEqual(plan["attractions"][0]["name"], "Hawa Mahal")
        self.assertEqual(plan["attraction_source"], "Curated fallback (not live data)")
        self.assertEqual(
            plan["itinerary"][0]["items"][0]["name"],
            "Hawa Mahal",
        )
        self.assertNotIn("Beatles", str(plan["itinerary"]))

    def test_plan_and_replan_use_the_requested_destination_attractions(self):
        requests = []

        async def places_for(destination, interests, limit, **_kwargs):
            requests.append((
                destination,
                interests,
                limit,
                _kwargs.get("latitude"),
                _kwargs.get("longitude"),
            ))
            return [{
                "place_id": f"{destination}-place",
                "name": f"{destination} attraction",
                "address": f"{destination} address",
                "latitude": 1.0,
                "longitude": 2.0,
                "maps_url": "https://maps.google.com/",
                "source": "Google Places API (New)",
            }]

        with patch("app.get_destination_attractions", side_effect=places_for) as search:
            jaipur = self.client.post(
                "/api/plan",
                json={"destination": "Jaipur", "days": 1, "interests": ["culture"]},
            ).json()
            rishikesh = self.client.post(
                "/api/plan",
                json={"destination": "Rishikesh", "days": 1, "interests": ["nature"]},
            ).json()
            replan = self.client.post(
                "/api/replan",
                json={"destination": "Jaipur", "days": 1, "interests": ["culture"]},
            ).json()

        self.assertEqual(jaipur["itinerary"][0]["items"][0]["name"], "Jaipur attraction")
        self.assertEqual(rishikesh["itinerary"][0]["items"][0]["name"], "Rishikesh attraction")
        self.assertEqual(replan["itinerary"][0]["items"][0]["name"], "Jaipur attraction")
        self.assertEqual(search.await_count, 3)
        self.assertEqual(
            requests,
            [
                ("Jaipur", ["culture"], 2, None, None),
                ("Rishikesh", ["nature"], 2, None, None),
                ("Jaipur", ["culture"], 2, None, None),
            ],
        )
        self.assertIsNone(jaipur["itinerary"][0]["items"][0]["cost_per_person"])
        self.assertEqual(
            sum(
                expense["name"] == "Activity allowance"
                for expense in jaipur["expenses"]
            ),
            1,
        )

    def test_attraction_failure_uses_labeled_fallback_for_plan(self):
        with patch(
            "app.get_destination_attractions",
            new_callable=AsyncMock,
            side_effect=httpx.ReadTimeout("sensitive upstream detail"),
        ):
            response = self.client.post(
                "/api/plan",
                json={"destination": "Rishikesh", "days": 2},
            )

        self.assertEqual(response.status_code, 200)
        plan = response.json()
        self.assertEqual(
            plan["attraction_source"],
            "Curated fallback (not live data)",
        )
        self.assertTrue(
            all(
                item["source"] == "Curated fallback (not live data)"
                for day in plan["itinerary"]
                for item in day["items"]
            )
        )
        self.assertNotIn("sensitive upstream detail", response.text)

    def test_estimates_scale_by_destination_trip_duration_and_group_size(self):
        request = TripRequest(
            destination="Jaipur",
            days=4,
            travelers=3,
            budget_per_person=100000,
        )
        plan = build_plan(request, attractions=[])
        expenses = {item["name"]: item for item in plan["expenses"]}
        subtotal = sum(
            item["group_cost"]
            for name, item in expenses.items()
            if name != "Contingency reserve (10%)"
        )

        self.assertEqual(expenses["Accommodation"]["per_person"], 3000)
        self.assertEqual(expenses["Food and drinking water"]["per_person"], 2000)
        self.assertEqual(expenses["Local transport"]["group_cost"], 3000)
        self.assertEqual(expenses["Contingency reserve (10%)"]["group_cost"], round(subtotal * 0.1))
        self.assertEqual(plan["total_cost"], sum(item["group_cost"] for item in plan["expenses"]))

    def test_budget_repair_reduces_allowance_and_itinerary_scope(self):
        request = TripRequest(
            destination="Jaipur",
            days=3,
            travelers=1,
            budget_per_person=2000,
        )
        attractions = [
            {
                "place_id": f"place-{index}",
                "name": f"Attraction {index}",
                "source": "Google Places API (New)",
            }
            for index in range(6)
        ]

        plan = build_plan(
            request,
            attractions=attractions,
            attraction_source="Google Places API (New)",
        )

        expense_names = {item["name"] for item in plan["expenses"]}
        self.assertNotIn("Activity allowance", expense_names)
        self.assertNotIn("Local experience", expense_names)
        self.assertTrue(all(len(day["items"]) <= 1 for day in plan["itinerary"]))
        self.assertEqual(
            plan["total_cost"],
            sum(item["group_cost"] for item in plan["expenses"]),
        )


if __name__ == "__main__":
    unittest.main()
