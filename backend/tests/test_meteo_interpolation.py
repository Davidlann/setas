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


if __name__ == "__main__":
    unittest.main()
