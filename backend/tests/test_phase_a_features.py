from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h3

from backend.src.export_relative_suitability import SPECIES, export_relative_suitability
from backend.src.ingestion.dynamic.aemet_opendata import (
    calibrate_weather_records_with_aemet,
    parse_aemet_dms,
    parse_aemet_float,
)
from backend.src.ingestion.static.lithology_legal import classify_lithology
from backend.src.processing.duckdb_manager import DuckDBManager
from backend.src.scoring.engine import MushroomScoringEngine


class PhaseAFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.engine = MushroomScoringEngine()
        self.static = {
            "latitude": 40.85,
            "dominant_species": "Pinus sylvestris",
            "canopy_cover": 65.0,
            "elevation_m": 1250.0,
            "slope_deg": 12.0,
            "aspect_cardinal": "S",
            "soil_ph": 5.5,
            "soil_ph_q05": 4.7,
            "environmental_data_quality": "sourced_unvalidated",
        }
        self.weather = {
            "snapshot_date": "2026-10-15",
            "precip_accum_14d_mm": 55.0,
            "precip_daily_mm": [2.0, 5.0, 12.0, 14.0, 8.0, 4.0, 2.0, 2.0, 2.0, 2.0, 1.0, 1.0, 0.0, 0.0],
            "temp_mean_14d_celsius": 11.5,
            "temp_mean_recent_3d_celsius": 11.0,
            "temp_min_14d_celsius": 5.0,
            "temp_min_recent_3d_celsius": 5.0,
            "soil_moisture_percent": 31.0,
            "soil_temp_mean_14d_celsius": 11.5,
            "soil_temp_0_7cm_3d_celsius": 11.0,
        }

    def test_classify_lithology_distinguishes_spanish_igme_units(self):
        self.assertEqual(classify_lithology("Granitoides biotíticos", "Otros granitoides"), "siliceous")
        self.assertEqual(classify_lithology("Ortocuarcitas, areniscas y pizarras"), "siliceous")
        self.assertEqual(classify_lithology("Dolomías, calizas y calizas nodulosas"), "calcareous")
        self.assertEqual(classify_lithology("Arcillas versicolores y yesos"), "calcareous")
        self.assertEqual(
            classify_lithology("Conglomerados, areniscas, arenas arcósicas, arcillas, calizas y yesos"),
            "mixed",
        )

    def test_lithology_adjusts_edaphic_and_protects_limestone_ph(self):
        siliceous = {**self.static, "soil_ph": 6.4, "soil_ph_q05": 5.0, "lithology_class": "siliceous"}
        calcareous = {**self.static, "soil_ph": 7.0, "soil_ph_q05": 5.2, "lithology_class": "calcareous"}

        eff_sil = self.engine._effective_soil_ph(siliceous)
        eff_cal = self.engine._effective_soil_ph(calcareous)
        self.assertLess(eff_sil, 6.1)
        self.assertGreaterEqual(eff_cal, 6.65)

        ed_sil = self.engine.calculate_relative_suitability("boletus_edulis", siliceous, self.weather)
        ed_cal = self.engine.calculate_relative_suitability("boletus_edulis", calcareous, self.weather)
        lac_cal = self.engine.calculate_relative_suitability("lactarius_deliciosus", calcareous, self.weather)
        self.assertGreater(ed_sil["breakdown"]["edaphic"], ed_cal["breakdown"]["edaphic"])
        self.assertGreaterEqual(ed_sil["breakdown"]["lithology_adj"], 2.0)
        self.assertLess(ed_cal["breakdown"]["lithology_adj"], 0.0)
        self.assertGreaterEqual(lac_cal["breakdown"]["lithology_adj"], 0.0)

    def test_snowfall_penalizes_weather_stalls_flush_and_triggers_snow_risk(self):
        cold_wet = {
            **self.weather,
            "snapshot_date": "2026-11-23",
            "temp_mean_14d_celsius": 3.5,
            "temp_mean_recent_3d_celsius": 2.0,
            "soil_temp_mean_14d_celsius": 7.5,
            "soil_temp_0_7cm_3d_celsius": 6.5,
            "temp_min_recent_3d_celsius": 1.0,
            "snowfall_7d_cm": 0.0,
            "forecast_3d_precip_mm": 12.0,
            "forecast_3d_temp_min_celsius": 0.0,
            "forecast_3d_temp_mean_celsius": 3.0,
            "snowfall_3d_fc_cm": 0.0,
        }
        snowy = {
            **cold_wet,
            "snowfall_7d_cm": 10.0,
            "snowfall_3d_fc_cm": 5.5,
        }
        res_no_snow = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, cold_wet, season_phase="late"
        )
        res_snow = self.engine.calculate_relative_suitability(
            "boletus_edulis", self.static, snowy, season_phase="late"
        )
        self.assertLess(res_snow["weather_score"], res_no_snow["weather_score"])
        self.assertLess(res_snow["breakdown"]["snow_factor"], 0.7)
        self.assertEqual(res_snow["breakdown"]["flush_phase"], "cold_stalled")
        self.assertEqual(res_snow["breakdown"]["forecast_outlook"], "snow_risk")

    def test_topographic_solar_factor_prefers_south_in_cold_and_north_in_hot_dry(self):
        cold_meteo = {
            **self.weather,
            "snapshot_date": "2026-11-23",
            "temp_mean_14d_celsius": 6.8,
            "temp_mean_recent_3d_celsius": 6.5,
            "soil_temp_mean_14d_celsius": 6.8,
            "radiation_mean_7d_mj": 9.5,
        }
        south_cell = {**self.static, "slope_deg": 20.0, "aspect_cardinal": "S"}
        north_cell = {**self.static, "slope_deg": 20.0, "aspect_cardinal": "N"}

        s_cold = self.engine.calculate_relative_suitability(
            "boletus_edulis", south_cell, cold_meteo, season_phase="late"
        )
        n_cold = self.engine.calculate_relative_suitability(
            "boletus_edulis", north_cell, cold_meteo, season_phase="late"
        )
        self.assertGreater(s_cold["breakdown"]["solar_factor"], 1.0)
        self.assertLess(n_cold["breakdown"]["solar_factor"], 1.0)
        self.assertGreater(s_cold["breakdown"]["topography"], n_cold["breakdown"]["topography"])
        self.assertGreater(s_cold["weather_score"], n_cold["weather_score"])

        hot_dry_meteo = {
            **self.weather,
            "snapshot_date": "2026-09-05",
            "temp_mean_14d_celsius": 19.5,
            "temp_mean_recent_3d_celsius": 20.0,
            "soil_temp_mean_14d_celsius": 17.5,
            "water_balance_14d_mm": -18.0,
            "radiation_mean_7d_mj": 20.0,
        }
        s_hot = self.engine.calculate_relative_suitability(
            "boletus_edulis", south_cell, hot_dry_meteo, season_phase="early"
        )
        n_hot = self.engine.calculate_relative_suitability(
            "boletus_edulis", north_cell, hot_dry_meteo, season_phase="early"
        )
        self.assertGreater(n_hot["breakdown"]["topography"], s_hot["breakdown"]["topography"])
        self.assertGreater(n_hot["weather_score"], s_hot["weather_score"])

    def test_aemet_helpers_and_graceful_fallback_without_key(self):
        self.assertAlmostEqual(parse_aemet_dms("404735N"), 40.79306, places=4)
        self.assertAlmostEqual(parse_aemet_dms("040038W"), -4.01056, places=4)
        self.assertEqual(parse_aemet_float("12,4"), 12.4)
        self.assertEqual(parse_aemet_float("Ip", allow_trace_precip=True), 0.1)
        self.assertIsNone(parse_aemet_float("Acum"))

        rec = [{"h3_index": "88392b420bfffff", "precip_accum_14d_mm": 20.0, "temp_min_14d_celsius": 4.0}]
        with patch.dict(os.environ, {"AEMET_API_KEY": ""}, clear=False):
            with patch("backend.src.ingestion.dynamic.aemet_opendata.load_dotenv", return_value=False):
                out_recs, info = calibrate_weather_records_with_aemet(
                    rec, {"88392b420bfffff": (40.8, -3.8, 1200.0)}, "2026-11-23"
                )
        self.assertFalse(info["aemet_calibrated"])
        self.assertEqual(info["aemet_stations_used"], 0)
        self.assertEqual(out_recs[0]["precip_accum_14d_mm"], 20.0)

    def test_aemet_calibration_applies_bounded_station_residuals(self):
        snap_date = "2026-11-23"
        end_d = datetime.strptime(snap_date, "%Y-%m-%d").date()
        days_obs = {
            (end_d - timedelta(days=13 - k)).isoformat(): {
                "prec": 6.0,
                "tmin": 1.0,
                "tmed": 6.0,
            }
            for k in range(14)
        }

        class MockAemetClient:
            def fetch_recent_daily_observations(self, days=14, reference_date=None):
                return {
                    "stations": {"3100": {"indicativo": "3100", "lat": 40.80, "lon": -3.80, "elevation_m": 1200.0}},
                    "observations": {
                        "3100": {"indicativo": "3100", "lat": 40.80, "lon": -3.80, "elevation_m": 1200.0, "days": days_obs}
                    },
                }

        p28 = [2.0] * 28
        tmin28 = [4.0] * 28
        records = [
            {
                "h3_index": "88392b420bfffff",
                "precip_daily_mm": p28[-14:],
                "precip_daily_28d_mm": p28[:],
                "temp_min_daily_28d_c": tmin28[:],
                "precip_last_3d_mm": 6.0,
                "precip_accum_7d_mm": 14.0,
                "precip_accum_10d_mm": 20.0,
                "precip_accum_14d_mm": 28.0,
                "precip_accum_28d_mm": 56.0,
                "precip_15_28d_mm": 28.0,
                "temp_min_14d_celsius": 4.0,
                "temp_min_recent_3d_celsius": 4.0,
            }
        ]
        calibrated, info = calibrate_weather_records_with_aemet(
            records,
            {"88392b420bfffff": (40.80, -3.80, 1200.0)},
            snap_date,
            client=MockAemetClient(),
        )
        self.assertTrue(info["aemet_calibrated"])
        self.assertEqual(info["aemet_stations_used"], 1)
        # Lluvia corregida al alza dentro del clamp [0.65, 1.55]
        self.assertGreater(calibrated[0]["precip_accum_14d_mm"], 35.0)
        self.assertLessEqual(calibrated[0]["precip_accum_14d_mm"], 28.0 * 1.55)
        # Mínima enfriada hacia 1.0 °C (delta máximo -3.0 °C)
        self.assertLess(calibrated[0]["temp_min_14d_celsius"], 3.0)
        self.assertGreaterEqual(calibrated[0]["temp_min_14d_celsius"], 1.0)

    def test_export_relative_suitability_v2_splits_base_and_computes_trend_dcs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "test_export_v2.duckdb"
            out_dir = root / "out"
            db = DuckDBManager(db_path=db_path)

            h3_id = h3.latlng_to_cell(40.83, -3.83, 8)
            db.upsert_static_cells([{
                "h3_index": h3_id,
                "latitude": 40.83,
                "longitude": -3.83,
                "elevation_m": 1350.0,
                "slope_deg": 8.0,
                "aspect_cardinal": "S",
                "soil_ph": 5.5,
                "soil_sand_percent": 40.0,
                "soil_silt_percent": 40.0,
                "soil_clay_percent": 20.0,
                "dominant_species": "Pinus sylvestris",
                "canopy_cover": 65.0,
                "is_forest": True,
                "environmental_data_quality": "sourced_unvalidated",
            }])
            with db.get_connection() as con:
                con.execute(
                    """
                    UPDATE cells_static
                    SET lithology_class = 'siliceous',
                        lithology_desc = 'Granitoides',
                        legal_zone_type = 'national_park',
                        legal_zone_name = 'Parque Nacional de la Sierra de Guadarrama'
                    WHERE h3_index = ?
                    """,
                    [h3_id],
                )

            base_snap = {
                "h3_index": h3_id,
                "precip_accum_7d_mm": 12.0,
                "precip_accum_10d_mm": 22.0,
                "precip_accum_14d_mm": 30.0,
                "precip_last_3d_mm": 4.0,
                "precip_daily_mm": [0.0, 2.0, 4.0, 6.0, 8.0, 4.0, 2.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                "precip_daily_28d_mm": [0.0] * 14 + [0.0, 2.0, 4.0, 6.0, 8.0, 4.0, 2.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                "soil_temp_daily_28d_c": [11.0] * 28,
                "temp_min_daily_28d_c": [6.0] * 28,
                "temp_mean_14d_celsius": 11.5,
                "temp_mean_recent_3d_celsius": 11.0,
                "temp_min_14d_celsius": 6.0,
                "temp_min_recent_3d_celsius": 6.0,
                "soil_moisture_percent": 28.0,
                "soil_temp_mean_14d_celsius": 11.0,
                "soil_temp_0_7cm_3d_celsius": 10.5,
                "forecast_3d_precip_mm": 8.0,
                "forecast_3d_temp_min_celsius": 4.0,
                "forecast_3d_temp_mean_celsius": 8.0,
                "forecast_3d_precip_daily": [4.0, 3.0, 1.0],
                "forecast_3d_temp_min_daily": [5.0, 4.0, 4.0],
                "snowfall_7d_cm": 0.0,
                "snowfall_3d_fc_cm": 0.0,
                "radiation_mean_7d_mj": 10.0,
                "weather_source": "Open-Meteo test",
                "weather_sampling_step_deg": 0.1,
                "weather_interpolation": "idw_4_nearest",
                "weather_nearest_distance_km": 3.0,
            }
            # Snapshot 1: más frío/seco; Snapshot 2: más húmedo/templado -> dcs > 0
            snap1 = {
                **base_snap,
                "precip_accum_14d_mm": 12.0,
                "precip_daily_mm": [0.0] * 10 + [3.0] * 4,
                "precip_daily_28d_mm": [0.0] * 24 + [3.0] * 4,
                "soil_moisture_percent": 19.0,
            }
            db.upsert_meteo_snapshot([snap1], "2026-10-12")
            db.upsert_meteo_snapshot([base_snap], "2026-10-15")

            res = export_relative_suitability(db_path=db_path, output_dir=out_dir)
            base_data = json.loads((out_dir / "cells_base.json").read_text(encoding="utf-8"))
            self.assertEqual(base_data["schema_version"], 2)
            self.assertEqual(base_data["date"], "2026-10-15")
            self.assertEqual(base_data["prev_date"], "2026-10-12")
            bcell = base_data["cells"][0]
            self.assertEqual(bcell["lith"], "siliceous")
            self.assertEqual(bcell["leg"], "national_park")
            self.assertEqual(bcell["lhab"], "evergreen_conifer")
            self.assertEqual(bcell["soil_texture_status"], "scored")
            self.assertEqual(len(bcell["p28d"]), 28)
            self.assertEqual(len(bcell["fcr3d"]), 3)

            for sp in SPECIES:
                sp_data = json.loads((out_dir / f"{sp}.json").read_text(encoding="utf-8"))
                self.assertEqual(sp_data["schema_version"], 2)
                self.assertEqual(sp_data["base_file"], "cells_base.json")
                self.assertEqual(sp_data["prev_date"], "2026-10-12")
                self.assertTrue(isinstance(sp_data.get("reasons"), list) and len(sp_data["reasons"]) >= 1)
                scell = sp_data["cells"][0]
                self.assertNotIn("lat", scell)
                self.assertNotIn("s", scell)
                self.assertIn("ri", scell)
                self.assertIsNotNone(scell["dcs"])
            ed_data = json.loads((out_dir / "boletus_edulis.json").read_text(encoding="utf-8"))
            self.assertGreater(ed_data["cells"][0]["dcs"], 0.0)
            self.assertEqual(set(res["outputs"].keys()), set(SPECIES))

    def test_aemet_fetch_endpoint_decodes_latin1_accents(self):
        from backend.src.ingestion.dynamic.aemet_opendata import AemetOpenDataClient

        class FakeResp:
            def __init__(self, status_code, content_bytes, json_obj=None):
                self.status_code = status_code
                self.content = content_bytes
                self._json = json_obj
                self.encoding = "ISO-8859-1"

            def raise_for_status(self):
                return None

            def json(self):
                return self._json

        latin1_bytes = json.dumps(
            [{"indicativo": "2444", "nombre": "Ávila / Peñaranda"}],
            ensure_ascii=False,
        ).encode("latin-1")

        client = AemetOpenDataClient(api_key="dummy-key")
        with patch.object(
            client.session,
            "get",
            return_value=FakeResp(200, b"{}", {"estado": 200, "datos": "https://aemet.test/data"}),
        ), patch(
            "backend.src.ingestion.dynamic.aemet_opendata.requests.get",
            return_value=FakeResp(200, latin1_bytes),
        ):
            parsed = client._fetch_endpoint("/valores/test")

        self.assertEqual(parsed[0]["nombre"], "Ávila / Peñaranda")


if __name__ == "__main__":
    unittest.main()
