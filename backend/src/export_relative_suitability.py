"""Exporta índices ambientales experimentales separados en base común + 5 especies."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import duckdb

from backend.config.settings import DB_PATH, TARGET_REGION_BOUNDS
from backend.src.scoring.engine import MushroomScoringEngine

SPECIES = (
    "boletus_edulis",
    "boletus_aereus",
    "boletus_pinophilus",
    "boletus_reticulatus",
    "lactarius_deliciosus",
)
GBIF_TAXON_KEYS = {
    "boletus_edulis": 5954958,
    "boletus_aereus": 8733688,
    "boletus_pinophilus": 5954949,
    "boletus_reticulatus": 5954691,
    "lactarius_deliciosus": 5248629,
}

_ASPECT_ES_MAP = {"SW": "SO", "W": "O", "NW": "NO"}


def _compact_num(value, decimals: int = 1):
    if value is None:
        return None
    val = round(float(value), decimals)
    return 0 if val == 0 else val


def _compact_series(values, decimals: int = 1):
    if not isinstance(values, list):
        return None
    return [_compact_num(v, decimals) if v is not None else None for v in values]


def _write_json_atomic(payload: dict, out_path: Path) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=out_path.parent,
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        json.dump(payload, temp_file, separators=(",", ":"), ensure_ascii=False)
        temp_path = Path(temp_file.name)
    try:
        os.replace(temp_path, out_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _row_to_static_and_meteo(source: dict, snapshot_date_iso: str):
    static = {
        key: source.get(key)
        for key in (
            "latitude", "elevation_m", "slope_deg", "aspect_cardinal",
            "soil_ph", "soil_ph_q05", "dominant_species", "canopy_cover",
            "soil_sand_percent", "soil_silt_percent", "soil_clay_percent",
            "tpi_m", "soil_soc_g_kg", "lithology_class", "lithology_desc",
            "environmental_data_quality",
        )
    }
    meteo = {
        "snapshot_date": snapshot_date_iso,
        "precip_accum_14d_mm": source["precip_accum_14d_mm"],
        "precip_accum_28d_mm": source.get("precip_accum_28d_mm"),
        "precip_15_28d_mm": source.get("precip_15_28d_mm"),
        "precip_last_3d_mm": source.get("precip_last_3d_mm"),
        "temp_mean_14d_celsius": source["temp_mean_14d_celsius"],
        "temp_mean_recent_3d_celsius": source.get("temp_mean_recent_3d_celsius"),
        "temp_min_14d_celsius": source["temp_min_14d_celsius"],
        "temp_min_recent_3d_celsius": source.get("temp_min_recent_3d_celsius"),
        "soil_moisture_percent": source["soil_moisture_percent"],
        "humidity_mean_7d_percent": source.get("humidity_mean_7d_percent"),
        "soil_temp_mean_14d_celsius": source.get("soil_temp_mean_14d_celsius"),
        "soil_temp_0_7cm_3d_celsius": source.get("soil_temp_0_7cm_3d_celsius"),
        "soil_temp_root_mean_14d_celsius": source.get("soil_temp_root_mean_14d_celsius"),
        "et0_accum_14d_mm": source.get("et0_accum_14d_mm"),
        "water_balance_14d_mm": source.get("water_balance_14d_mm"),
        "thermal_shock_celsius": source.get("thermal_shock_celsius"),
        "wind_max_14d_kmh": source.get("wind_max_14d_kmh"),
        "snowfall_7d_cm": source.get("snowfall_7d_cm"),
        "snowfall_3d_fc_cm": source.get("snowfall_3d_fc_cm"),
        "radiation_mean_7d_mj": source.get("radiation_mean_7d_mj"),
        "frost_days_7d": source.get("frost_days_7d"),
        "frost_days_14d": source.get("frost_days_14d"),
        "hard_frost_28d": source.get("hard_frost_28d"),
        "pre_rain_dry_streak_days": source.get("pre_rain_dry_streak_days"),
        "pre_rain_min_soil_moisture_pct": source.get("pre_rain_min_soil_moisture_pct"),
        "forecast_3d_precip_mm": source.get("forecast_3d_precip_mm"),
        "forecast_3d_temp_min_celsius": source.get("forecast_3d_temp_min_celsius"),
        "forecast_3d_temp_mean_celsius": source.get("forecast_3d_temp_mean_celsius"),
    }
    if source.get("precip_daily_mm_json"):
        meteo["precip_daily_mm"] = json.loads(source["precip_daily_mm_json"])
    if source.get("precip_daily_28d_mm_json"):
        meteo["precip_daily_28d_mm"] = json.loads(source["precip_daily_28d_mm_json"])
    if source.get("soil_temp_daily_28d_json"):
        meteo["soil_temp_daily_28d_c"] = json.loads(source["soil_temp_daily_28d_json"])
    if source.get("temp_min_daily_28d_json"):
        meteo["temp_min_daily_28d_c"] = json.loads(source["temp_min_daily_28d_json"])
    return static, meteo


def export_relative_suitability(
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
    season_phase: str | None = None,
):
    if season_phase is not None and season_phase not in {"early", "late", "auto"}:
        raise ValueError("season_phase debe ser 'early', 'late' o 'auto'.")
    if output_dir is None:
        project_root = Path(__file__).resolve().parents[2]
        output_dir = project_root / "frontend" / "public" / "data" / "experimental"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    with duckdb.connect(str(db_path), read_only=True) as con:
        snapshot_date = con.execute(
            "SELECT MAX(snapshot_date) FROM meteo_snapshots"
        ).fetchone()[0]
        if snapshot_date is None:
            raise RuntimeError("No hay snapshots meteorológicos para calcular el índice.")
        if season_phase in (None, "auto"):
            resolved_phase = MushroomScoringEngine.resolve_season_phase(snapshot_date)
        else:
            resolved_phase = season_phase

        prev_snapshot_row = con.execute(
            "SELECT MAX(snapshot_date) FROM meteo_snapshots WHERE snapshot_date < ?",
            [snapshot_date],
        ).fetchone()
        prev_snapshot_date = prev_snapshot_row[0] if prev_snapshot_row else None

        static_table_columns = {
            row[0] for row in con.execute("DESCRIBE cells_static").fetchall()
        }
        optional_static_columns = (
            "soil_ph_q05",
            "soil_ph_q95",
            "soil_sand_percent",
            "soil_silt_percent",
            "soil_clay_percent",
            "tpi_m",
            "soil_soc_g_kg",
            "lithology_class",
            "lithology_desc",
            "legal_zone_type",
            "legal_zone_name",
            "legal_zone_code",
        )
        optional_static_select = ", ".join(
            f"c.{column}" if column in static_table_columns
            else f"NULL AS {column}"
            for column in optional_static_columns
        )
        meteo_table_columns = {
            row[0] for row in con.execute("DESCRIBE meteo_snapshots").fetchall()
        }
        weather_metadata_columns = (
            "precip_daily_mm_json",
            "precip_daily_28d_mm_json",
            "soil_temp_daily_28d_json",
            "temp_min_daily_28d_json",
            "soil_moisture_daily_28d_json",
            "precip_last_3d_mm",
            "precip_accum_28d_mm",
            "precip_15_28d_mm",
            "weather_source",
            "weather_sampling_step_deg",
            "weather_interpolation",
            "weather_nearest_distance_km",
            "soil_temp_mean_14d_celsius",
            "soil_temp_0_7cm_3d_celsius",
            "soil_temp_root_mean_14d_celsius",
            "temp_mean_recent_3d_celsius",
            "temp_min_recent_3d_celsius",
            "humidity_mean_7d_percent",
            "et0_accum_14d_mm",
            "water_balance_14d_mm",
            "thermal_shock_celsius",
            "wind_max_14d_kmh",
            "snowfall_7d_cm",
            "snowfall_3d_fc_cm",
            "radiation_mean_7d_mj",
            "frost_days_7d",
            "frost_days_14d",
            "hard_frost_28d",
            "pre_rain_dry_streak_days",
            "pre_rain_min_soil_moisture_pct",
            "forecast_3d_precip_mm",
            "forecast_3d_temp_min_celsius",
            "forecast_3d_temp_mean_celsius",
            "forecast_3d_precip_daily_json",
            "forecast_3d_temp_min_daily_json",
            "aemet_calibrated",
            "aemet_stations_used",
        )
        metadata_select = ", ".join(
            f"m.{column}" if column in meteo_table_columns
            else f"NULL AS {column}"
            for column in weather_metadata_columns
        )
        rows = con.execute(f"""
            SELECT
                c.h3_index, c.latitude, c.longitude, c.elevation_m, c.slope_deg,
                c.aspect_cardinal, c.soil_ph,
                {optional_static_select},
                c.dominant_species, c.canopy_cover, c.environmental_data_quality,
                m.precip_accum_14d_mm, m.temp_mean_14d_celsius,
                m.temp_min_14d_celsius, m.soil_moisture_percent,
                {metadata_select}
            FROM cells_static c
            JOIN meteo_snapshots m
                ON m.h3_index = c.h3_index AND m.snapshot_date = ?
            WHERE c.is_forest = TRUE
              AND c.environmental_data_quality = 'sourced_unvalidated'
              AND c.longitude BETWEEN ? AND ?
              AND c.latitude BETWEEN ? AND ?
              AND c.elevation_m IS NOT NULL
              AND c.slope_deg IS NOT NULL
              AND c.soil_ph IS NOT NULL
              AND c.dominant_species IS NOT NULL
              AND c.canopy_cover IS NOT NULL
              AND m.precip_accum_14d_mm IS NOT NULL
              AND m.temp_mean_14d_celsius IS NOT NULL
              AND m.temp_min_14d_celsius IS NOT NULL
              AND m.soil_moisture_percent IS NOT NULL
            ORDER BY c.h3_index
        """, [
            snapshot_date, min_lon, max_lon, min_lat, max_lat
        ]).fetchall()
        columns = [description[0] for description in con.description]
        if not rows:
            raise RuntimeError(
                f"No hay celdas con datos ambientales y meteorológicos completos "
                f"para {snapshot_date}."
            )

        prev_meteo_by_h3 = {}
        if prev_snapshot_date is not None:
            prev_rows = con.execute(f"""
                SELECT
                    m.h3_index,
                    m.precip_accum_14d_mm, m.temp_mean_14d_celsius,
                    m.temp_min_14d_celsius, m.soil_moisture_percent,
                    {metadata_select}
                FROM meteo_snapshots m
                WHERE m.snapshot_date = ?
                  AND m.precip_accum_14d_mm IS NOT NULL
                  AND m.temp_mean_14d_celsius IS NOT NULL
                  AND m.temp_min_14d_celsius IS NOT NULL
                  AND m.soil_moisture_percent IS NOT NULL
            """, [prev_snapshot_date]).fetchall()
            prev_cols = [desc[0] for desc in con.description]
            prev_meteo_by_h3 = {
                row[0]: dict(zip(prev_cols, row)) for row in prev_rows
            }

        observation_counts = dict(con.execute("""
            SELECT taxon_key, COUNT(*)
            FROM fungal_occurrences
            WHERE coordinate_uncertainty_m <= 250
              AND TRY_CAST(event_date AS TIMESTAMP)::DATE <= ?
            GROUP BY taxon_key
        """, [snapshot_date]).fetchall()) if con.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'fungal_occurrences'
        """).fetchone()[0] else {}

    engine = MushroomScoringEngine()
    candidate_sources = []
    for row in rows:
        source = dict(zip(columns, row))
        static = {
            key: source.get(key)
            for key in (
                "elevation_m", "soil_ph", "soil_ph_q05", "dominant_species",
                "canopy_cover", "soil_sand_percent", "soil_silt_percent",
                "soil_clay_percent", "tpi_m", "soil_soc_g_kg", "lithology_class",
            )
        }
        if engine.has_potential_habitat(static):
            candidate_sources.append(source)
    if not candidate_sources:
        raise RuntimeError(
            "Ninguna celda supera las exclusiones estáticas de hábitat "
            "para las especies configuradas."
        )
    excluded_cell_count = len(rows) - len(candidate_sources)

    generated_at = datetime.now(timezone.utc).isoformat()
    first_source = candidate_sources[0]
    weather_metadata_recorded = all(
        source[key] is not None
        for source in candidate_sources
        for key in (
            "weather_source",
            "weather_interpolation",
            "weather_sampling_step_deg",
            "weather_nearest_distance_km",
        )
    )
    aemet_calibrated_any = any(bool(s.get("aemet_calibrated")) for s in candidate_sources)
    aemet_stations_max = max(
        (int(s.get("aemet_stations_used") or 0) for s in candidate_sources), default=0
    )
    if weather_metadata_recorded:
        weather_metadata = {
            "source": first_source["weather_source"],
            "sampling_step_degrees": first_source["weather_sampling_step_deg"],
            "interpolation": first_source["weather_interpolation"],
            "max_nearest_source_distance_km": round(
                max(source["weather_nearest_distance_km"] for source in candidate_sources),
                2,
            ),
            "aemet_calibrated": aemet_calibrated_any,
            "aemet_stations_used": aemet_stations_max,
            "resolution_note": (
                "Ventana meteorológica de 28 días + nieve/radiación + previsión de 3 días "
                "interpolada con IDW-4 y ajuste altitudinal (-0,6 °C/100 m aire; -0,45 °C/100 m suelo)."
            ),
        }
    else:
        weather_metadata = {
            "source": "Open-Meteo; metadatos de interpolación no registrados",
            "sampling_step_degrees": None,
            "interpolation": "legacy_unknown",
            "max_nearest_source_distance_km": None,
            "aemet_calibrated": aemet_calibrated_any,
            "aemet_stations_used": aemet_stations_max,
            "resolution_note": (
                "Snapshot heredado: no se registró la resolución de consulta ni "
                "el método de asignación meteorológica. Actualiza la meteorología "
                "con el pipeline para regenerar estos datos."
            ),
        }

    snapshot_date_iso = (
        snapshot_date.isoformat()
        if hasattr(snapshot_date, "isoformat")
        else str(snapshot_date)
    )
    prev_snapshot_iso = (
        prev_snapshot_date.isoformat()
        if prev_snapshot_date is not None and hasattr(prev_snapshot_date, "isoformat")
        else (str(prev_snapshot_date) if prev_snapshot_date is not None else None)
    )
    prev_season_phase = (
        MushroomScoringEngine.resolve_season_phase(prev_snapshot_iso)
        if prev_snapshot_iso
        else resolved_phase
    )

    soil_texture_metadata = {
        "source": "ISRIC SoilGrids 2.0",
        "depth_cm": "5-15",
        "units": "percent by mass",
        "note": (
            "Aporta hasta 7 de 35 puntos edáficos. Regla general experimental: "
            "penaliza de forma gradual extremos de arena (>70-90%), limo (>60-80%) y arcilla (>40-60%)."
        ),
        "scoring": engine.config["soil_texture_scoring"],
        "missing_data": (
            "Sin las tres fracciones, se conserva la puntuación de pH anterior."
        ),
    }
    habitat_filter = {
        "scope": "shared_union_of_target_species",
        "excluded_cells": excluded_cell_count,
        "unknown_hosts_retained": True,
    }

    # 1. Construir cells_base.json con geometría, suelo, litología, legal y serie meteo 28d+3d
    base_cells = []
    parsed_pairs = []
    for source in candidate_sources:
        static, meteo = _row_to_static_and_meteo(source, snapshot_date_iso)
        parsed_pairs.append((source, static, meteo))

        p14_list = meteo.get("precip_daily_mm")
        p28_list = meteo.get("precip_daily_28d_mm")
        st28_list = meteo.get("soil_temp_daily_28d_c")
        tn28_list = meteo.get("temp_min_daily_28d_c")
        fcr3d = (
            json.loads(source["forecast_3d_precip_daily_json"])
            if source.get("forecast_3d_precip_daily_json")
            else None
        )
        fctn3d = (
            json.loads(source["forecast_3d_temp_min_daily_json"])
            if source.get("forecast_3d_temp_min_daily_json")
            else None
        )
        pinc = round(sum(p14_list[:10]), 2) if isinstance(p14_list, list) and len(p14_list) == 14 else None
        prec = (
            round(sum(p14_list[10:]), 2)
            if isinstance(p14_list, list) and len(p14_list) == 14
            else meteo.get("precip_last_3d_mm")
        )
        texture_complete = all(
            source.get(k) is not None
            for k in ("soil_sand_percent", "soil_silt_percent", "soil_clay_percent")
        )
        if texture_complete:
            base_tex_score = round(
                engine._soil_texture_score(
                    source["soil_sand_percent"],
                    source["soil_silt_percent"],
                    source["soil_clay_percent"],
                ),
                1,
            )
            base_tex_status = "scored"
        else:
            base_tex_score = None
            base_tex_status = "missing"
        base_cells.append({
            "h": source["h3_index"],
            "lat": source["latitude"],
            "lon": source["longitude"],
            "elev": source["elevation_m"],
            "slope": source["slope_deg"],
            "tpi": source.get("tpi_m"),
            "asp": (
                _ASPECT_ES_MAP.get(
                    str(source["aspect_cardinal"]).upper(),
                    source["aspect_cardinal"],
                )
                if source["aspect_cardinal"]
                else None
            ),
            "ph": source["soil_ph"],
            "ph05": source["soil_ph_q05"],
            "ph95": source["soil_ph_q95"],
            "sand": source["soil_sand_percent"],
            "silt": source["soil_silt_percent"],
            "clay": source["soil_clay_percent"],
            "soil_texture_score": base_tex_score,
            "soil_texture_status": base_tex_status,
            "soc": source.get("soil_soc_g_kg"),
            "tree": source["dominant_species"],
            "lhab": MushroomScoringEngine._host_leaf_habit(source["dominant_species"]),
            "cc": source["canopy_cover"],
            "lith": source.get("lithology_class"),
            "lith_d": source.get("lithology_desc"),
            "leg": source.get("legal_zone_type"),
            "leg_n": source.get("legal_zone_name"),
            "leg_c": source.get("legal_zone_code"),
            "p14": meteo["precip_accum_14d_mm"],
            "p28": meteo.get("precip_accum_28d_mm"),
            "pold": meteo.get("precip_15_28d_mm"),
            "p3": meteo.get("precip_last_3d_mm"),
            "pinc": pinc,
            "prec": prec,
            "t": meteo["temp_mean_14d_celsius"],
            "t3": meteo.get("temp_mean_recent_3d_celsius"),
            "tmin": meteo["temp_min_14d_celsius"],
            "tmin3": meteo.get("temp_min_recent_3d_celsius"),
            "moist": meteo["soil_moisture_percent"],
            "rh": meteo.get("humidity_mean_7d_percent"),
            "stemp": meteo.get("soil_temp_mean_14d_celsius"),
            "st07": meteo.get("soil_temp_0_7cm_3d_celsius"),
            "wb": meteo.get("water_balance_14d_mm"),
            "tshock": meteo.get("thermal_shock_celsius"),
            "wind": meteo.get("wind_max_14d_kmh"),
            "snow7": meteo.get("snowfall_7d_cm"),
            "snowfc": meteo.get("snowfall_3d_fc_cm"),
            "rad7": meteo.get("radiation_mean_7d_mj"),
            "fc_r": meteo.get("forecast_3d_precip_mm"),
            "fc_tn": meteo.get("forecast_3d_temp_min_celsius"),
            "weather_distance_km": source.get("weather_nearest_distance_km"),
            "p28d": _compact_series(p28_list, 1),
            "st28d": _compact_series(st28_list, 1),
            "tn28d": _compact_series(tn28_list, 1),
            "fcr3d": _compact_series(fcr3d, 1),
            "fctn3d": _compact_series(fctn3d, 1),
        })

    base_payload = {
        "date": snapshot_date_iso,
        "prev_date": prev_snapshot_iso,
        "generated_at": generated_at,
        "res": 8,
        "schema_version": 2,
        "data_quality": "sourced_unvalidated",
        "weather_metadata": weather_metadata,
        "soil_texture_metadata": soil_texture_metadata,
        "habitat_filter": habitat_filter,
        "cells_count": len(base_cells),
        "cells": base_cells,
    }
    base_path = output_dir / "cells_base.json"
    _write_json_atomic(base_payload, base_path)

    # Preconstruir meteo del snapshot previo si existe para calcular tendencia dcs
    prev_pairs_by_h3 = {}
    if prev_meteo_by_h3 and prev_snapshot_iso:
        for source, static, _ in parsed_pairs:
            h3_id = source["h3_index"]
            prev_src = prev_meteo_by_h3.get(h3_id)
            if prev_src is None:
                continue
            _, prev_meteo = _row_to_static_and_meteo(prev_src, prev_snapshot_iso)
            prev_pairs_by_h3[h3_id] = (static, prev_meteo)

    # 2. Exportar los 5 JSON ligeros por especie
    outputs = {}
    for species_id in SPECIES:
        species_rules = engine.species_matrix[species_id]
        habitat_rules = species_rules["habitat"]
        edaphic_rules = species_rules["edaphic"]
        meteo_rules = species_rules["meteorology"]
        cells = []
        reasons_list = []
        reason_to_idx = {}
        for source, static, meteo in parsed_pairs:
            result = engine.calculate_relative_suitability(
                species_id, static, meteo, season_phase=resolved_phase
            )
            breakdown = result["breakdown"]
            h3_id = source["h3_index"]
            dcs = None
            prev_entry = prev_pairs_by_h3.get(h3_id)
            if prev_entry is not None:
                prev_static, prev_meteo = prev_entry
                prev_res = engine.calculate_relative_suitability(
                    species_id, prev_static, prev_meteo, season_phase=prev_season_phase
                )
                dcs = round(result["combined_score"] - prev_res["combined_score"], 1)

            reason_text = result["reason"]
            if reason_text not in reason_to_idx:
                reason_to_idx[reason_text] = len(reasons_list)
                reasons_list.append(reason_text)
            ri = reason_to_idx[reason_text]

            cells.append({
                "h": h3_id,
                "hs": result["score"],
                "ws": result["weather_score"],
                "cs": result["combined_score"],
                "dcs": dcs,
                "st": result["status"],
                "ri": ri,
                "host": breakdown.get("host_assessment", "unknown"),
                "htier": breakdown.get("host_tier"),
                "fp": breakdown.get("flush_phase"),
                "eph": breakdown.get("effective_soil_ph", source["soil_ph"]),
                "rtstat": breakdown.get("rain_timing_status"),
                "pe": breakdown["effective_precip_14d_mm"],
                "f7": breakdown.get("frost_days_7d"),
                "hf28": breakdown.get("hard_frost_28d"),
                "drg": breakdown.get("drought_factor"),
                "wl": breakdown.get("waterlog_factor"),
                "tshock_status": breakdown.get("thermal_shock_status"),
                "phen": breakdown.get("phenology_factor"),
                "sstat": breakdown.get("season_status"),
                "fc_out": breakdown.get("forecast_outlook"),
                "snow_f": breakdown.get("snow_factor"),
                "sol_f": breakdown.get("solar_factor"),
                "met": breakdown.get("meteorology"),
                "p_pts": breakdown.get("precip_score"),
                "t_pts": breakdown.get("temp_score"),
                "m_pts": breakdown.get("soil_moisture_score"),
                "edaphic": breakdown.get("edaphic"),
                "soil_ph_score": breakdown.get("soil_ph_score"),
                "topo": breakdown.get("topography"),
                "sl_pts": breakdown.get("slope_score"),
                "asp_pts": breakdown.get("aspect_score"),
            })

        payload = {
            "date": snapshot_date_iso,
            "prev_date": prev_snapshot_iso,
            "generated_at": generated_at,
            "res": 8,
            "schema_version": 2,
            "base_file": "cells_base.json",
            "data_quality": "sourced_unvalidated",
            "score_type": "experimental_separated_habitat_weather",
            "score_label": "Índices separados de hábitat potencial y meteorología reciente (0–100)",
            "is_probability": False,
            "season_phase": resolved_phase,
            "habitat_filter": habitat_filter,
            "weather_metadata": weather_metadata,
            "soil_texture_metadata": soil_texture_metadata,
            "cells_count": len(cells),
            "species_id": species_id,
            "reasons": reasons_list,
            "species_profile": {
                "common_name_es": species_rules["common_name_es"],
                "scientific_name": species_rules["scientific_name"],
                "preferred_forest_types": habitat_rules["preferred_forest_types"],
                "minimum_canopy_cover_percent": habitat_rules["minimum_canopy_cover_percent"],
                "altitude_range_meters": habitat_rules["altitude_range_meters"],
                "ph_tolerated": edaphic_rules["ph_tolerated"],
                "precipitation_threshold_14d_mm": (
                    meteo_rules["accumulated_precip_14d_threshold_mm"]
                ),
                "precipitation_optimal_14d_mm": (
                    meteo_rules["accumulated_precip_14d_optimal_mm"]
                ),
                "mean_temp_optimal_celsius": meteo_rules["mean_temp_optimal_celsius"],
                "min_temp_limit_celsius": meteo_rules["min_temp_limit_celsius"],
                "soil_moisture_min_percent": meteo_rules["soil_moisture_min_percent"],
            },
            "gbif_occurrences_with_uncertainty_le_250m": observation_counts.get(
                GBIF_TAXON_KEYS[species_id], 0
            ),
            "limitations": [
                "Índice heurístico, no probabilidad de encontrar fructificación.",
                "Las reglas ecológicas combinan hábitat potencial, litología, nieve, radiación, fenología estacional y meteorología diaria.",
                "El índice de hábitat potencial excluye meteorología; la señal meteorológica reciente se muestra por separado y no es una predicción de presencia.",
                "La textura edáfica aporta hasta 7 de 35 puntos; los umbrales generales son experimentales.",
                "La lluvia eficaz usa tiempo térmico acumulado sobre 28 días, temperatura del suelo radicular (0–28 cm), nieve y previsión a 3 días.",
                "Solo se exportan celdas que pasan las exclusiones estáticas de hábitat para al menos una especie; hospedadores mixtos no identificados se conservan.",
                "Relieve de 200 m, litología 1:1M y suelo de 250 m; usa 'Ver micro-detalle' para analizar vertientes y rodales H3-9 bajo demanda.",
                "Los registros oportunistas de GBIF no corrigen el esfuerzo de observación.",
            ],
            "cells": cells,
        }
        out_path = output_dir / f"{species_id}.json"
        _write_json_atomic(payload, out_path)
        outputs[species_id] = {
            "path": out_path,
            "cells": len(cells),
            "favorability_50_plus": sum(cell["hs"] >= 50 for cell in cells),
            "combined_50_plus": sum(cell["cs"] >= 50 for cell in cells),
        }
    return {
        "snapshot_date": snapshot_date_iso,
        "prev_snapshot_date": prev_snapshot_iso,
        "base_path": base_path,
        "outputs": outputs,
    }


if __name__ == "__main__":
    result = export_relative_suitability()
    print(
        f"Índices ambientales experimentales v2 exportados para {result['snapshot_date']} "
        f"(previo: {result['prev_snapshot_date'] or 'ninguno'})."
    )
    print(f"Base común: {result['base_path']}")
    for species_id, summary in result["outputs"].items():
        print(
            f"{species_id}: {summary['cells']} celdas; "
            f"{summary['favorability_50_plus']} con hábitat >=50; "
            f"{summary['combined_50_plus']} con índice combinado >=50. "
            f"{summary['path']}"
        )
    print("El índice no es una probabilidad ni una predicción validada.")
