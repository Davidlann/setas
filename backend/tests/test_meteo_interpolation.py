import unittest

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


if __name__ == "__main__":
    unittest.main()
