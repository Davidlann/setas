import tempfile
import unittest
from pathlib import Path

from backend.src.processing.duckdb_manager import DuckDBManager
from backend.src.scoring.engine import MushroomScoringEngine


class FieldCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = MushroomScoringEngine()

    def test_morcuera_and_alto_leon_nov14_show_tail_or_incubating_not_peak_flush(self):
        # Lluvias de hace ~26-28d (cola madura), sequía intermedia y lluvia muy reciente fría (hace 3-5d)
        p28_morcuera = (
            [12.0, 14.0, 5.0] + [0.0] * 19 + [11.2, 30.5, 55.6, 3.0, 1.2, 0.6]
        )
        st28_morcuera = (
            [9.5, 8.2, 7.5] + [6.5] * 16 + [5.0, 5.5, 6.8, 7.2, 7.1, 6.8, 6.2, 6.0, 5.8]
        )
        tmin28_morcuera = (
            [5.0, 4.0, 3.0] + [1.5] * 13 + [-1.4, -2.3, -2.0, -1.6, 2.4, 4.1, 6.8, 6.6, 5.9, 5.6, 3.9, 4.4]
        )
        morcuera_static = {
            "dominant_species": "Pinus sylvestris",
            "canopy_cover": 65.0,
            "elevation_m": 1679.0,
            "slope_deg": 9.0,
            "aspect_cardinal": "SW",
            "soil_ph": 5.9,
            "soil_ph_q05": 4.5,
            "environmental_data_quality": "sourced_unvalidated",
        }
        morcuera_meteo = {
            "snapshot_date": "2026-11-14",
            "precip_accum_14d_mm": sum(p28_morcuera[-14:]),
            "precip_daily_mm": p28_morcuera[-14:],
            "precip_daily_28d_mm": p28_morcuera,
            "soil_temp_daily_28d_c": st28_morcuera,
            "temp_min_daily_28d_c": tmin28_morcuera,
            "temp_mean_14d_celsius": 5.3,
            "temp_mean_recent_3d_celsius": 6.4,
            "temp_min_14d_celsius": -2.3,
            "temp_min_recent_3d_celsius": 3.9,
            "soil_moisture_percent": 36.3,
            "soil_temp_mean_14d_celsius": 6.4,
            "soil_temp_0_7cm_3d_celsius": 6.1,
        }
        res_pin = self.engine.calculate_relative_suitability(
            "boletus_pinophilus", morcuera_static, morcuera_meteo, season_phase="late"
        )
        res_ed = self.engine.calculate_relative_suitability(
            "boletus_edulis", morcuera_static, morcuera_meteo, season_phase="late"
        )
        self.assertIn(res_pin["breakdown"]["flush_phase"], ("mature_tail", "incubating", "cold_stalled"))
        self.assertNotEqual(res_pin["breakdown"]["flush_phase"], "peak_flush")
        self.assertNotEqual(res_ed["breakdown"]["flush_phase"], "peak_flush")

        # Alto del León 14-nov: 26mm a mitad de octubre, luego 20d secos y lluvia del 9-11 nov todavía incubando
        p28_leon = [10.0, 8.0, 4.0] + [0.0] * 19 + [8.2, 16.6, 27.2, 6.5, 1.6, 5.8]
        st28_leon = [11.0, 9.8, 8.8] + [7.8] * 16 + [6.5, 6.9, 8.5, 8.8, 8.6, 8.4, 8.2, 7.6, 7.2]
        leon_static = {
            **morcuera_static,
            "elevation_m": 1493.0,
            "soil_ph": 6.1,
            "soil_ph_q05": 4.7,
            "canopy_cover": 80.0,
        }
        leon_meteo = {
            **morcuera_meteo,
            "precip_accum_14d_mm": sum(p28_leon[-14:]),
            "precip_daily_mm": p28_leon[-14:],
            "precip_daily_28d_mm": p28_leon,
            "soil_temp_daily_28d_c": st28_leon,
            "temp_mean_14d_celsius": 6.8,
            "temp_mean_recent_3d_celsius": 7.9,
            "soil_temp_mean_14d_celsius": 7.9,
            "soil_temp_0_7cm_3d_celsius": 7.7,
        }
        res_leon_pin = self.engine.calculate_relative_suitability(
            "boletus_pinophilus", leon_static, leon_meteo, season_phase="late"
        )
        self.assertIn(res_leon_pin["breakdown"]["flush_phase"], ("mature_tail", "incubating"))
        self.assertNotEqual(res_leon_pin["breakdown"]["flush_phase"], "peak_flush")

    def test_arroyo_de_las_fraguas_nov21_is_low_and_dormant(self):
        arroyo_static = {
            "dominant_species": "Quercus pyrenaica",
            "canopy_cover": 80.0,
            "elevation_m": 1222.0,
            "slope_deg": 2.5,
            "aspect_cardinal": "E",
            "soil_ph": 7.0,
            "soil_ph_q05": 5.3,
            "environmental_data_quality": "sourced_unvalidated",
        }
        p28 = [0.0] * 14 + [0.7, 4.4, 25.4, 35.3, 8.5, 5.7, 11.8, 1.5, 0.0, 0.0, 10.3, 0.2, 0.0, 13.9]
        st28 = [11.0] * 6 + [8.0, 6.5, 6.6, 6.4, 8.0, 8.1, 10.2, 10.6, 10.6, 10.2, 9.8, 9.5, 9.2, 8.7, 8.9, 9.5, 11.0, 10.0, 9.2, 9.0, 8.8, 8.5]
        tmin28 = [5.0] * 6 + [0.1, -2.2, 0.6, 1.1, 3.2, 4.4, 8.7, 9.4, 8.8, 8.0, 7.9, 6.8, 5.4, 6.5, 7.7, 10.7, 5.8, 4.9, 6.4, 5.5, 5.2, 4.9]
        meteo = {
            "snapshot_date": "2026-11-21",
            "precip_accum_14d_mm": sum(p28[-14:]),
            "precip_daily_mm": p28[-14:],
            "precip_daily_28d_mm": p28,
            "soil_temp_daily_28d_c": st28,
            "temp_min_daily_28d_c": tmin28,
            "temp_mean_14d_celsius": 8.9,
            "temp_mean_recent_3d_celsius": 7.1,
            "temp_min_14d_celsius": 4.4,
            "temp_min_recent_3d_celsius": 4.9,
            "soil_moisture_percent": 37.8,
            "soil_temp_mean_14d_celsius": 9.5,
            "soil_temp_0_7cm_3d_celsius": 9.0,
            "hard_frost_28d": True,
        }
        edulis = self.engine.calculate_relative_suitability("boletus_edulis", arroyo_static, meteo, season_phase="late")
        pino = self.engine.calculate_relative_suitability("boletus_pinophilus", arroyo_static, meteo, season_phase="late")
        aereus = self.engine.calculate_relative_suitability("boletus_aereus", arroyo_static, meteo, season_phase="late")

        self.assertLess(edulis["combined_score"], 45.0)
        self.assertEqual(edulis["breakdown"]["flush_phase"], "dormant_host")
        self.assertEqual(pino["score"], 0.0)
        self.assertEqual(pino["combined_score"], 0.0)
        self.assertLess(aereus["combined_score"], 20.0)

    def test_morcuera_freezing_nov23_is_cold_stalled_without_favorable_shock(self):
        morcuera_static = {
            "dominant_species": "Pinus sylvestris",
            "canopy_cover": 65.0,
            "elevation_m": 1679.0,
            "slope_deg": 9.0,
            "aspect_cardinal": "SW",
            "soil_ph": 5.9,
            "soil_ph_q05": 4.5,
            "environmental_data_quality": "sourced_unvalidated",
        }
        meteo = {
            "snapshot_date": "2026-11-23",
            "precip_accum_14d_mm": 145.4,
            "precip_daily_mm": [11.2, 30.5, 55.6, 3.0, 1.2, 0.6, 3.2, 0.0, 0.0, 17.2, 4.9, 0.4, 27.6, 1.2],
            "temp_mean_14d_celsius": 5.9,
            "temp_mean_recent_3d_celsius": 3.4,
            "temp_min_14d_celsius": -1.3,
            "temp_min_recent_3d_celsius": -1.3,
            "soil_moisture_percent": 39.1,
            "soil_temp_mean_14d_celsius": 4.2,
            "soil_temp_0_7cm_3d_celsius": 3.3,
            "thermal_shock_celsius": -2.94,
        }
        edulis = self.engine.calculate_relative_suitability("boletus_edulis", morcuera_static, meteo, season_phase="late")
        self.assertEqual(edulis["breakdown"]["flush_phase"], "cold_stalled")
        self.assertLess(edulis["combined_score"], 35.0)
        self.assertNotEqual(edulis["breakdown"]["thermal_shock_status"], "triggered_favorable")

    def test_optimal_october_pine_flush_reaches_high_peak_flush(self):
        pine_static = {
            "dominant_species": "Pinus sylvestris",
            "canopy_cover": 65.0,
            "elevation_m": 1350.0,
            "slope_deg": 8.0,
            "aspect_cardinal": "N",
            "soil_ph": 5.6,
            "soil_ph_q05": 4.6,
            "environmental_data_quality": "sourced_unvalidated",
        }
        p28 = [0.0] * 8 + [12.0, 18.0, 5.0] + [0.0] * 5 + [18.0, 22.0, 15.0, 4.0, 0.0] + [0.0] * 7
        st28 = [13.5] * 14 + [13.2] * 7 + [12.0] * 7
        tmin28 = [7.0] * 28
        meteo = {
            "snapshot_date": "2026-10-12",
            "precip_accum_14d_mm": sum(p28[-14:]),
            "precip_daily_mm": p28[-14:],
            "precip_daily_28d_mm": p28,
            "soil_temp_daily_28d_c": st28,
            "temp_min_daily_28d_c": tmin28,
            "temp_mean_14d_celsius": 13.0,
            "temp_mean_recent_3d_celsius": 11.8,
            "temp_min_14d_celsius": 7.0,
            "temp_min_recent_3d_celsius": 7.0,
            "soil_moisture_percent": 31.0,
            "soil_temp_mean_14d_celsius": 12.2,
            "soil_temp_0_7cm_3d_celsius": 11.8,
            "thermal_shock_celsius": -2.5,
            "humidity_mean_7d_percent": 82.0,
        }
        res = self.engine.calculate_relative_suitability("boletus_edulis", pine_static, meteo, season_phase="early")
        self.assertGreaterEqual(res["combined_score"], 80.0)
        self.assertEqual(res["breakdown"]["flush_phase"], "peak_flush")

    def test_summer_june_reticulatus_flush_in_melojar_reaches_peak_flush(self):
        oak_static = {
            "dominant_species": "Quercus pyrenaica",
            "canopy_cover": 65.0,
            "elevation_m": 1100.0,
            "slope_deg": 6.0,
            "aspect_cardinal": "NE",
            "soil_ph": 5.8,
            "soil_ph_q05": 4.9,
            "environmental_data_quality": "sourced_unvalidated",
        }
        p28 = [0.0] * 9 + [15.0, 12.0] + [0.0] * 7 + [20.0, 18.0, 10.0, 5.0] + [0.0] * 6
        st28 = [18.5] * 28
        tmin28 = [11.0] * 28
        meteo = {
            "snapshot_date": "2026-06-15",
            "precip_accum_14d_mm": sum(p28[-14:]),
            "precip_daily_mm": p28[-14:],
            "precip_daily_28d_mm": p28,
            "soil_temp_daily_28d_c": st28,
            "temp_min_daily_28d_c": tmin28,
            "temp_mean_14d_celsius": 19.0,
            "temp_mean_recent_3d_celsius": 19.5,
            "temp_min_14d_celsius": 11.0,
            "temp_min_recent_3d_celsius": 12.0,
            "soil_moisture_percent": 29.0,
            "soil_temp_mean_14d_celsius": 18.5,
            "soil_temp_0_7cm_3d_celsius": 18.5,
            "humidity_mean_7d_percent": 75.0,
        }
        res = self.engine.calculate_relative_suitability("boletus_reticulatus", oak_static, meteo, season_phase="early")
        self.assertGreaterEqual(res["combined_score"], 75.0)
        self.assertEqual(res["breakdown"]["flush_phase"], "peak_flush")

    def test_multiplicative_liebig_law_prevents_compensation_of_zeros(self):
        pine_static = {
            "dominant_species": "Pinus sylvestris",
            "canopy_cover": 65.0,
            "elevation_m": 1200.0,
            "slope_deg": 10.0,
            "aspect_cardinal": "S",
            "soil_ph": 5.5,
            "environmental_data_quality": "sourced_unvalidated",
        }
        freezing_wet = {
            "precip_accum_14d_mm": 100.0,
            "temp_mean_14d_celsius": 2.0,
            "temp_min_14d_celsius": -2.0,
            "temp_min_recent_3d_celsius": -2.0,
            "soil_temp_mean_14d_celsius": 2.5,
            "soil_moisture_percent": 35.0,
        }
        warm_bone_dry = {
            "precip_accum_14d_mm": 0.0,
            "temp_mean_14d_celsius": 14.0,
            "temp_min_14d_celsius": 8.0,
            "temp_min_recent_3d_celsius": 8.0,
            "soil_temp_mean_14d_celsius": 13.0,
            "soil_moisture_percent": 10.0,
        }
        wet_cold_res = self.engine.calculate_relative_suitability("boletus_edulis", pine_static, freezing_wet)
        dry_warm_res = self.engine.calculate_relative_suitability("boletus_edulis", pine_static, warm_bone_dry)
        self.assertLess(wet_cold_res["weather_score"], 25.0)
        self.assertLess(dry_warm_res["weather_score"], 25.0)

    def test_duckdb_prune_and_compact_keep_latest_dates_and_shrink_file(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_prune.duckdb"
            db = DuckDBManager(db_path=db_path)
            base_rec = {
                "h3_index": "88392b420bfffff",
                "precip_accum_7d_mm": 10.0,
                "precip_accum_10d_mm": 15.0,
                "precip_accum_14d_mm": 20.0,
                "precip_daily_mm": [1.0] * 14,
                "temp_mean_14d_celsius": 12.0,
                "temp_min_14d_celsius": 5.0,
                "soil_moisture_percent": 30.0,
            }
            for day in range(1, 9):
                db.upsert_meteo_snapshot([base_rec], f"2026-11-{day:02d}")
            deleted = db.prune_old_meteo_snapshots(keep_dates=5)
            self.assertEqual(deleted, 3)
            db.compact_database()
            with db.get_connection() as con:
                dates = [
                    str(r[0])
                    for r in con.execute(
                        "SELECT snapshot_date FROM meteo_snapshots ORDER BY 1"
                    ).fetchall()
                ]
            self.assertEqual(
                dates,
                ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"],
            )

    def test_lactarius_deliciosus_november_pine_stays_active_in_mild_frost(self):
        pine_static = {
            "dominant_species": "Pinus pinaster",
            "canopy_cover": 55.0,
            "elevation_m": 1050.0,
            "slope_deg": 7.0,
            "aspect_cardinal": "SE",
            "soil_ph": 6.7,
            "soil_ph_q05": 5.5,
            "environmental_data_quality": "sourced_unvalidated",
        }
        p28 = [0.0] * 10 + [10.0, 14.0, 6.0] + [0.0] * 4 + [12.0, 15.0, 8.0, 4.0] + [0.0] * 7
        st28 = [10.5] * 14 + [8.5] * 7 + [7.8] * 7
        tmin28 = [4.0] * 21 + [-0.5, -1.0, 0.5, 1.5, 2.0, 1.0, -0.5]
        meteo = {
            "snapshot_date": "2026-11-23",
            "precip_accum_14d_mm": sum(p28[-14:]),
            "precip_daily_mm": p28[-14:],
            "precip_daily_28d_mm": p28,
            "soil_temp_daily_28d_c": st28,
            "temp_min_daily_28d_c": tmin28,
            "temp_mean_14d_celsius": 7.8,
            "temp_mean_recent_3d_celsius": 6.8,
            "temp_min_14d_celsius": -1.0,
            "temp_min_recent_3d_celsius": -0.5,
            "soil_moisture_percent": 29.0,
            "soil_temp_mean_14d_celsius": 8.1,
            "soil_temp_0_7cm_3d_celsius": 7.8,
            "humidity_mean_7d_percent": 78.0,
        }
        delic = self.engine.calculate_relative_suitability("lactarius_deliciosus", pine_static, meteo, season_phase="late")
        edulis = self.engine.calculate_relative_suitability("boletus_edulis", pine_static, meteo, season_phase="late")
        self.assertGreater(delic["combined_score"], 55.0)
        self.assertGreater(delic["combined_score"], edulis["combined_score"])
        self.assertIn(delic["breakdown"]["flush_phase"], ("peak_flush", "emerging_buttons", "moderate_active"))


if __name__ == "__main__":
    unittest.main()
