import math
import unittest

from backend.src.scoring.engine import MushroomScoringEngine


class RelativeSuitabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = MushroomScoringEngine()
        self.static = {
            "dominant_species": "Pinares de pino albar",
            "canopy_cover": 65.0,
            "elevation_m": 1200.0,
            "slope_deg": 12.0,
            "aspect_cardinal": None,
            "soil_ph": 5.2,
            "environmental_data_quality": "sourced_unvalidated",
        }
        self.weather = {
            "precip_accum_14d_mm": 45.0,
            "temp_mean_14d_celsius": 15.0,
            "temp_min_14d_celsius": 6.0,
            "soil_moisture_percent": 30.0,
        }

    def test_relative_index_accepts_sourced_data_and_reports_nonprobability(self):
        result = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, self.weather
        )

        self.assertEqual(
            result["score_type"], "experimental_separated_habitat_weather"
        )
        self.assertEqual(result["data_quality"], "sourced_unvalidated")
        self.assertGreater(result["score"], 0)
        self.assertIn("heurístico", result["reason"])
        self.assertEqual(
            result["combined_score"],
            round(math.sqrt(result["score"] * result["weather_score"]), 1),
        )
        self.assertNotEqual(result["combined_score"], result["weather_score"])

    def test_missing_texture_keeps_the_existing_ph_score(self):
        baseline = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, self.weather
        )
        missing_texture = self.engine.calculate_relative_suitability(
            "boletus_edulis",
            {
                **self.static,
                "soil_sand_percent": 60.0,
                "soil_silt_percent": None,
                "soil_clay_percent": 15.0,
            },
            self.weather,
        )

        self.assertEqual(missing_texture["score"], baseline["score"])
        self.assertEqual(
            missing_texture["weather_score"], baseline["weather_score"]
        )
        self.assertEqual(
            missing_texture["breakdown"]["edaphic"],
            baseline["breakdown"]["edaphic"],
        )
        self.assertEqual(
            missing_texture["breakdown"]["soil_texture_status"], "missing"
        )

    def test_texture_extremes_reduce_habitat_score_and_are_explained(self):
        balanced = self.engine.calculate_relative_suitability(
            "boletus_edulis",
            {
                **self.static,
                "soil_sand_percent": 40.0,
                "soil_silt_percent": 40.0,
                "soil_clay_percent": 20.0,
            },
            self.weather,
        )
        sandy = self.engine.calculate_relative_suitability(
            "boletus_edulis",
            {
                **self.static,
                "soil_sand_percent": 100.0,
                "soil_silt_percent": 0.0,
                "soil_clay_percent": 0.0,
            },
            self.weather,
        )

        self.assertEqual(
            balanced["breakdown"]["soil_texture_score"], 7.0
        )
        self.assertLess(
            sandy["breakdown"]["soil_texture_score"],
            balanced["breakdown"]["soil_texture_score"],
        )
        self.assertLess(sandy["score"], balanced["score"])
        self.assertEqual(sandy["weather_score"], balanced["weather_score"])
        self.assertEqual(
            sandy["breakdown"]["soil_texture_status"], "scored"
        )

    def test_texture_fraction_outside_valid_percent_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "entre 0 y 100"):
            self.engine.calculate_relative_suitability(
                "boletus_edulis",
                {
                    **self.static,
                    "soil_sand_percent": 101.0,
                    "soil_silt_percent": 0.0,
                    "soil_clay_percent": 0.0,
                },
                self.weather,
            )

    def test_legacy_validated_scoring_still_rejects_sourced_data(self):
        with self.assertRaisesRegex(ValueError, "variables ambientales sin verificar"):
            self.engine.calculate_cell_score(
                "boletus_edulis", self.static, self.weather
            )

    def test_relative_index_rejects_unverified_data(self):
        static = {**self.static, "environmental_data_quality": "unverified"}
        with self.assertRaisesRegex(ValueError, "sourced_unvalidated"):
            self.engine.calculate_relative_suitability(
                "boletus_edulis", static, self.weather
            )

    def test_excluded_cell_keeps_host_assessment_for_explanations(self):
        static = {**self.static, "soil_ph": 6.9}

        result = self.engine.calculate_relative_suitability(
            "boletus_edulis", static, self.weather
        )

        self.assertEqual(result["score"], 0)
        self.assertEqual(result["combined_score"], 0)
        self.assertEqual(result["breakdown"]["host_assessment"], "compatible")
        self.assertEqual(result["breakdown"]["host_tree"], "Pinares de pino albar")

    def test_suitable_habitat_cannot_offset_recently_dry_weather(self):
        dry_weather = {
            **self.weather,
            "precip_accum_14d_mm": 5.0,
            "temp_mean_14d_celsius": 15.0,
        }
        wet_weather = {
            **self.weather,
            "precip_accum_14d_mm": 60.0,
            "temp_mean_14d_celsius": 15.0,
        }

        dry = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, dry_weather
        )
        wet = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, wet_weather
        )

        self.assertEqual(dry["score"], wet["score"])
        self.assertGreater(wet["weather_score"], dry["weather_score"])
        self.assertEqual(
            dry["breakdown"]["combination_method"],
            "separate_habitat_and_weather",
        )

    def test_recent_freezing_minimum_reduces_weather_score(self):
        mild_weather = {**self.weather, "temp_min_14d_celsius": 2.0}
        freezing_weather = {**self.weather, "temp_min_14d_celsius": -1.0}

        mild = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, mild_weather
        )
        freezing = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, freezing_weather
        )

        self.assertEqual(mild["score"], freezing["score"])
        self.assertLess(freezing["weather_score"], mild["weather_score"])

    def test_subthreshold_rain_limits_other_weather_contributions(self):
        encinar = {
            **self.static,
            "dominant_species": "Encinares",
            "canopy_cover": 65.0,
            "elevation_m": 600.0,
            "soil_ph": 6.8,
        }
        warm_and_dry = {
            **self.weather,
            "precip_accum_14d_mm": 3.17,
            "temp_mean_14d_celsius": 13.49,
            "soil_moisture_percent": 14.92,
        }
        rain_at_threshold = {
            **warm_and_dry,
            "precip_accum_14d_mm": 30.0,
        }

        dry_result = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, warm_and_dry
        )
        threshold_result = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, rain_at_threshold
        )

        self.assertLess(dry_result["weather_score"], 35.0)
        self.assertEqual(threshold_result["score"], dry_result["score"])
        self.assertGreater(
            threshold_result["weather_score"], dry_result["weather_score"]
        )

    def test_recent_rain_contributes_less_than_rain_one_week_ago(self):
        encinar = {
            **self.static,
            "dominant_species": "Encinares",
            "canopy_cover": 65.0,
            "elevation_m": 600.0,
            "soil_ph": 6.0,
        }
        recent_rain = [0.0] * 13 + [30.0]
        rain_one_week_ago = [0.0] * 7 + [30.0] + [0.0] * 6
        recent_weather = {**self.weather, "precip_daily_mm": recent_rain}
        delayed_weather = {**self.weather, "precip_daily_mm": rain_one_week_ago}

        recent = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, recent_weather
        )
        delayed = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, delayed_weather
        )

        self.assertLess(
            recent["breakdown"]["effective_precip_14d_mm"],
            delayed["breakdown"]["effective_precip_14d_mm"],
        )
        self.assertLess(recent["weather_score"], delayed["weather_score"])

    def test_known_mfe_formation_names_match_species_hosts(self):
        cases = (
            ("Pinares de pino albar", "boletus_edulis"),
            ("Melojares", "boletus_edulis"),
            ("Encinares", "boletus_aereus"),
            ("Pinares de pino pinaster", "boletus_pinophilus"),
            ("Pinares de pino salgareño", "boletus_pinophilus"),
            ("Castañares", "boletus_reticulatus"),
        )
        for formation, species_id in cases:
            with self.subTest(formation=formation, species_id=species_id):
                self.assertTrue(
                    self.engine._is_compatible_host(
                        formation,
                        self.engine.species_matrix[species_id]["habitat"][
                            "preferred_forest_types"
                        ],
                    )
                )

    def test_mixed_unidentified_formation_is_uncertain_not_impossible(self):
        static = {
            **self.static,
            "dominant_species": (
                "Mezclas de coníferas y frondosas autóctonas en región mediterránea"
            ),
        }

        result = self.engine.calculate_relative_suitability(
            "boletus_edulis", static, self.weather
        )

        self.assertGreater(result["score"], 0)
        self.assertEqual(result["breakdown"]["host_assessment"], "unknown")
        self.assertIn("no permite identificar", result["reason"])

    def test_shared_habitat_filter_keeps_cells_suitable_for_any_species(self):
        encinar = {
            **self.static,
            "dominant_species": "Encinares",
            "canopy_cover": 65.0,
            "elevation_m": 600.0,
            "soil_ph": 6.0,
        }

        self.assertTrue(self.engine.has_potential_habitat(encinar))

    def test_shared_habitat_filter_removes_cells_excluded_for_all_species(self):
        pine_carrasco = {
            **self.static,
            "dominant_species": "Pinus halepensis",
            "canopy_cover": 65.0,
            "elevation_m": 600.0,
            "soil_ph": 6.0,
        }

        self.assertFalse(self.engine.has_potential_habitat(pine_carrasco))

    def test_shared_habitat_filter_keeps_uncertain_mixed_forest(self):
        mixed_forest = {
            **self.static,
            "dominant_species": (
                "Mezclas de coníferas y frondosas autóctonas en región mediterránea"
            ),
            "canopy_cover": 65.0,
            "elevation_m": 600.0,
            "soil_ph": 6.0,
        }

        self.assertTrue(self.engine.has_potential_habitat(mixed_forest))

    def test_thermal_shock_boosts_autumn_species_fruiting_trigger(self):
        favorable_shock_weather = {
            **self.weather,
            "precip_accum_14d_mm": 50.0,
            "thermal_shock_celsius": -3.5,
        }
        stable_weather = {
            **self.weather,
            "precip_accum_14d_mm": 50.0,
            "thermal_shock_celsius": 0.0,
        }

        shock_result = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, favorable_shock_weather
        )
        stable_result = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, stable_weather
        )

        self.assertGreater(
            shock_result["weather_score"], stable_result["weather_score"]
        )
        self.assertEqual(
            shock_result["breakdown"]["thermal_shock_status"],
            "triggered_favorable",
        )
        self.assertEqual(
            stable_result["breakdown"]["thermal_shock_status"],
            "stable",
        )

    def test_cold_shock_suppresses_thermophilic_species(self):
        encinar = {
            **self.static,
            "dominant_species": "Encinares",
            "elevation_m": 600.0,
            "soil_ph": 6.5,
        }
        warm_weather = {
            **self.weather,
            "precip_accum_14d_mm": 40.0,
            "temp_mean_14d_celsius": 20.0,
            "thermal_shock_celsius": 0.5,
        }
        cold_shock_weather = {
            **self.weather,
            "precip_accum_14d_mm": 40.0,
            "temp_mean_14d_celsius": 20.0,
            "thermal_shock_celsius": -4.5,
        }

        warm_res = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, warm_weather
        )
        cold_res = self.engine.calculate_relative_suitability(
            "boletus_aereus", encinar, cold_shock_weather
        )

        self.assertGreater(
            warm_res["weather_score"], cold_res["weather_score"]
        )
        self.assertEqual(
            cold_res["breakdown"]["thermal_shock_status"],
            "cold_suppressed",
        )

    def test_negative_water_balance_reduces_weather_score(self):
        deficit_weather = {
            **self.weather,
            "precip_accum_14d_mm": 40.0,
            "water_balance_14d_mm": -30.0, # Evaporación mayor que lluvia
        }
        surplus_weather = {
            **self.weather,
            "precip_accum_14d_mm": 40.0,
            "water_balance_14d_mm": 20.0, # Superávit hídrico
        }

        deficit_res = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, deficit_weather
        )
        surplus_res = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, surplus_weather
        )

        self.assertLess(
            deficit_res["weather_score"], surplus_res["weather_score"]
        )

    def test_strong_wind_desiccation_reduces_weather_score(self):
        calm_weather = {
            **self.weather,
            "wind_max_14d_kmh": 20.0,
        }
        gale_weather = {
            **self.weather,
            "wind_max_14d_kmh": 65.0, # Viento desecante fuerte
        }

        calm_res = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, calm_weather
        )
        gale_res = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, gale_weather
        )

        self.assertGreater(
            calm_res["weather_score"], gale_res["weather_score"]
        )


if __name__ == "__main__":
    unittest.main()
