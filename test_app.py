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
from maps_service import get_destination_attractions, get_route


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


class AttractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_destination_attractions_builds_destination_and_interest_query(self):
        places = [{
            "place_id": "place-1",
            "name": "Amber Fort",
            "address": "Devisinghpura, Jaipur",
            "latitude": 26.9855,
            "longitude": 75.8513,
            "maps_url": "https://maps.google.com/?cid=1",
        }]
        with patch("maps_service.search_places", new_callable=AsyncMock) as search:
            search.return_value = places
            result = await get_destination_attractions(
                "Jaipur",
                ["culture", "architecture"],
                3,
            )

        self.assertEqual(
            search.await_args.args[0],
            "tourist attractions in Jaipur for culture architecture",
        )
        self.assertEqual(search.await_args.args[1], 3)
        self.assertEqual(result[0]["name"], "Amber Fort")
        self.assertEqual(result[0]["source"], "Google Places API (New)")

    async def test_destination_attractions_skips_unusable_and_duplicate_results(self):
        with patch("maps_service.search_places", new_callable=AsyncMock) as search:
            search.return_value = [
                {"place_id": "1", "name": "Hawa Mahal"},
                {"place_id": "1", "name": "Hawa Mahal duplicate"},
                {"place_id": "2", "name": "  "},
            ]
            result = await get_destination_attractions("Jaipur", [], 5)

        self.assertEqual([place["name"] for place in result], ["Hawa Mahal"])
        self.assertIsNone(result[0]["maps_url"])
        self.assertIsNone(result[0]["latitude"])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)

    def test_health_endpoint_still_works(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

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
            self.client.get("/api/attractions?destination=Jaipur&limit=11").status_code,
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

    def test_empty_attractions_return_empty_api_results_and_labeled_plan_fallback(self):
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
        self.assertEqual(plan["attractions"], [])
        self.assertIn("No destination-specific demo data", plan["attraction_source"])
        self.assertEqual(
            plan["itinerary"][0]["items"][0]["name"],
            "No verified attractions available",
        )
        self.assertNotIn("Beatles", str(plan["itinerary"]))

    def test_plan_and_replan_use_the_requested_destination_attractions(self):
        requests = []

        async def places_for(destination, interests, limit):
            requests.append((destination, interests, limit))
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
                ("Jaipur", ["culture"], 2),
                ("Rishikesh", ["nature"], 2),
                ("Jaipur", ["culture"], 2),
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

    def test_places_failure_uses_labeled_fallback_for_plan(self):
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
        self.assertTrue(plan["attraction_source"].startswith("Fallback data"))
        self.assertTrue(
            all(
                item["source"].startswith("Rishikesh demo fallback")
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
