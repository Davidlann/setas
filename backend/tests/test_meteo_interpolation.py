from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from backend.src.ingestion.dynamic.meteo_openmeteo import (
    OpenMeteoClient,
    WEATHER_GRID_STEP_DEG,
    WEATHER_INTERPOLATION_METHOD,
)


def weather_node(latitude, longitude, value):
    return {
        "latitude": latitude,
        "longitude": longitude,
        "precip_accum_7d_mm": value,
        "precip_accum_10d_mm": value,
        "precip_accum_14d_mm": value,
        "precip_daily_mm": [value] * 14,
        "temp_mean_14d_celsius": value,
        "temp_min_14d_celsius": value,
        "soil_moisture_percent": value,
    }


class WeatherInterpolationTests(unittest.TestCase):
    def test_interpolates_four_nearest_nodes_smoothly(self):
        nodes = [
            weather_node(40.0, -4.0, 0.0),
            weather_node(40.0, -3.0, 10.0),
            weather_node(41.0, -4.0, 20.0),
            weather_node(41.0, -3.0, 30.0),
        ]

        result = OpenMeteoClient.batch_interpolate_weather(
            [(40.5, -3.5)], nodes
        )[0]

        self.assertEqual(result["precip_accum_14d_mm"], 15.0)
        self.assertEqual(result["precip_daily_mm"], [15.0] * 14)
        self.assertEqual(result["temp_mean_14d_celsius"], 15.0)
        self.assertEqual(result["weather_interpolation"], WEATHER_INTERPOLATION_METHOD)
        self.assertEqual(result["weather_sampling_step_deg"], WEATHER_GRID_STEP_DEG)
        self.assertGreater(result["weather_nearest_distance_km"], 0)

    def test_exact_grid_match_returns_source_value_without_smoothing(self):
        nodes = [
            weather_node(40.0, -4.0, 2.0),
            weather_node(40.0, -3.0, 8.0),
            weather_node(41.0, -4.0, 14.0),
            weather_node(41.0, -3.0, 20.0),
        ]

        result = OpenMeteoClient.batch_interpolate_weather(
            [(40.0, -4.0)], nodes
        )[0]

        self.assertEqual(result["precip_accum_14d_mm"], 2.0)
        self.assertEqual(result["weather_nearest_distance_km"], 0.0)

    def test_records_the_sampling_step_used_by_the_caller(self):
        result = OpenMeteoClient.batch_interpolate_weather(
            [(40.0, -4.0)],
            [weather_node(40.0, -4.0, 3.0)],
            grid_step_deg=0.2,
        )[0]

        self.assertEqual(result["weather_sampling_step_deg"], 0.2)

    def test_rejects_weather_nodes_without_coordinates(self):
        with self.assertRaisesRegex(ValueError, "coordenadas"):
            OpenMeteoClient.batch_interpolate_weather(
                [(40.0, -4.0)], [{"precip_accum_14d_mm": 1.0}]
            )

    def test_rejects_missing_weather_values_in_all_nearby_nodes(self):
        nodes = [
            {
                "latitude": 40.0,
                "longitude": -4.0,
                "precip_accum_7d_mm": 1.0,
            }
        ]

        with self.assertRaisesRegex(ValueError, "precip_accum_10d_mm"):
            OpenMeteoClient.batch_interpolate_weather(
                [(40.1, -4.1)], nodes
            )

    def test_interpolates_optional_physical_metrics_when_present(self):
        nodes = [
            {
                **weather_node(40.0, -4.0, 10.0),
                "soil_temp_mean_14d_celsius": 12.0,
                "water_balance_14d_mm": 5.0,
                "thermal_shock_celsius": -3.0,
            },
            {
                **weather_node(40.0, -3.0, 20.0),
                "soil_temp_mean_14d_celsius": 16.0,
                "water_balance_14d_mm": 15.0,
                "thermal_shock_celsius": -1.0,
            },
        ]

        result = OpenMeteoClient.batch_interpolate_weather(
            [(40.0, -3.5)], nodes
        )[0]

        self.assertEqual(result["soil_temp_mean_14d_celsius"], 14.0)
        self.assertEqual(result["water_balance_14d_mm"], 10.0)
        self.assertEqual(result["thermal_shock_celsius"], -2.0)
        self.assertIsNone(result["wind_max_14d_kmh"])

    def test_applies_altitudinal_lapse_rate_when_target_elevation_differs(self):
        nodes = [
            {
                **weather_node(40.0, -4.0, 12.0),
                "elevation": 1000.0,
                "soil_temp_mean_14d_celsius": 11.0,
                "temp_mean_recent_3d_celsius": 10.0,
                "temp_min_recent_3d_celsius": 4.0,
                "precip_last_3d_mm": 8.5,
                "humidity_mean_7d_percent": 78.0,
            }
        ]

        higher_cell = OpenMeteoClient.batch_interpolate_weather(
            [(40.0, -4.0)], nodes, target_elevations=[1500.0]
        )[0]

        # +500m elevation -> -3.0 C air temperature, -2.25 C soil temperature
        self.assertAlmostEqual(higher_cell["temp_mean_14d_celsius"], 9.0, places=2)
        self.assertAlmostEqual(higher_cell["temp_mean_recent_3d_celsius"], 7.0, places=2)
        self.assertAlmostEqual(higher_cell["temp_min_recent_3d_celsius"], 1.0, places=2)
        self.assertAlmostEqual(higher_cell["soil_temp_mean_14d_celsius"], 8.75, places=2)
        self.assertAlmostEqual(higher_cell["precip_last_3d_mm"], 8.5, places=2)
        self.assertAlmostEqual(higher_cell["humidity_mean_7d_percent"], 78.0, places=2)

    def test_build_forest_covering_grid_covers_cells_without_distant_nodes(self):
        coords = [(40.829, -3.833), (41.106, -3.129)]
        grid = OpenMeteoClient.build_forest_covering_grid(coords, grid_step_deg=0.1)
        # 2 isolated points -> at most 8 unique 0.1-deg corner nodes (versus huge bbox)
        self.assertEqual(len(grid), 8)
        lats = [pt[0] for pt in grid]
        lons = [pt[1] for pt in grid]
        for lat, lon in coords:
            self.assertTrue(any(abs(nlat - lat) <= 0.1 + 1e-6 and abs(nlon - lon) <= 0.1 + 1e-6 for nlat, nlon in grid))
        self.assertLessEqual(max(lats), 41.25)
        self.assertGreaterEqual(min(lons), -3.95)

    def test_429_rate_limit_retries_using_header_wait_and_succeeds(self):
        today = datetime.now(ZoneInfo("Europe/Madrid")).date()
        dates = [(today - timedelta(days=13 - k)).isoformat() for k in range(14)]
        ok_payload = [
            {
                "latitude": 40.8,
                "longitude": -3.8,
                "elevation": 1200.0,
                "daily": {
                    "time": dates,
                    "precipitation_sum": [2.0] * 14,
                    "temperature_2m_mean": [12.0] * 14,
                    "temperature_2m_min": [6.0] * 14,
                    "soil_moisture_0_to_7cm_mean": [0.3] * 14,
                    "soil_moisture_7_to_28cm_mean": [0.3] * 14,
                    "soil_temperature_0_to_7cm_mean": [11.0] * 14,
                    "soil_temperature_7_to_28cm_mean": [11.0] * 14,
                    "et0_fao_evapotranspiration": [1.5] * 14,
                    "wind_speed_10m_max": [12.0] * 14,
                    "relative_humidity_2m_mean": [75.0] * 14,
                },
            }
        ]
        resp_429 = MagicMock(status_code=429, headers={"Retry-After": "12"}, text="Too many requests")
        resp_200 = MagicMock(
            status_code=200,
            headers={"X-RateLimit-Cost": "2.5", "X-RateLimit-Remaining": "597.5"},
        )
        resp_200.json.return_value = ok_payload
        resp_200.raise_for_status.return_value = None

        client = OpenMeteoClient(request_delay_seconds=0.0)
        client.session.get = MagicMock(side_effect=[resp_429, resp_200])

        with patch("backend.src.ingestion.dynamic.meteo_openmeteo.time.sleep") as mock_sleep:
            results = client.fetch_weather_for_coords(
                [(40.8, -3.8)], past_days=14, forecast_days=1
            )

        self.assertEqual(len(results), 1)
        mock_sleep.assert_called_once_with(14.0)


if __name__ == "__main__":
    unittest.main()
