"""Exporta índices ambientales experimentales sin etiquetarlos como probabilidades."""
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
)
GBIF_TAXON_KEYS = {
    "boletus_edulis": 5954958,
    "boletus_aereus": 8733688,
    "boletus_pinophilus": 5954949,
    "boletus_reticulatus": 5954691,
}


def export_relative_suitability(
    db_path: Path = DB_PATH,
    output_dir: Path | None = None,
    season_phase: str = "early",
):
    if season_phase not in {"early", "late"}:
        raise ValueError("season_phase debe ser 'early' o 'late'.")
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
        meteo_table_columns = {
            row[0] for row in con.execute("DESCRIBE meteo_snapshots").fetchall()
        }
        weather_metadata_columns = (
            "precip_daily_mm_json",
            "weather_source",
            "weather_sampling_step_deg",
            "weather_interpolation",
            "weather_nearest_distance_km",
        )
        metadata_select = ", ".join(
            f"m.{column}" if column in meteo_table_columns
            else f"NULL AS {column}"
            for column in weather_metadata_columns
        )
        rows = con.execute(f"""
            SELECT
                c.h3_index, c.latitude, c.longitude, c.elevation_m, c.slope_deg,
                c.aspect_cardinal, c.soil_ph, c.soil_ph_q05, c.soil_ph_q95,
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
    candidate_rows = []
    for row in rows:
        source = dict(zip(columns, row))
        static = {
            key: source[key]
            for key in (
                "elevation_m", "soil_ph", "dominant_species", "canopy_cover",
            )
        }
        if engine.has_potential_habitat(static):
            candidate_rows.append(row)
    if not candidate_rows:
        raise RuntimeError(
            "Ninguna celda supera las exclusiones estáticas de hábitat "
            "para las cuatro especies."
        )
    excluded_cell_count = len(rows) - len(candidate_rows)

    generated_at = datetime.now(timezone.utc).isoformat()
    first_source = dict(zip(columns, candidate_rows[0]))
    weather_source = first_source["weather_source"]
    weather_interpolation = first_source["weather_interpolation"]
    weather_sampling_step_deg = first_source["weather_sampling_step_deg"]
    weather_metadata_rows = [
        dict(zip(columns, row)) for row in candidate_rows
    ]
    weather_metadata_recorded = all(
        source[key] is not None
        for source in weather_metadata_rows
        for key in (
            "weather_source",
            "weather_interpolation",
            "weather_sampling_step_deg",
            "weather_nearest_distance_km",
        )
    )
    if weather_metadata_recorded:
        weather_metadata = {
            "source": weather_source,
            "sampling_step_degrees": weather_sampling_step_deg,
            "interpolation": weather_interpolation,
            "max_nearest_source_distance_km": round(
                max(source["weather_nearest_distance_km"] for source in weather_metadata_rows),
                2,
            ),
            "resolution_note": (
                "La malla de consulta y la interpolación suavizan la asignación, "
                "pero el modelo meteorológico sigue siendo regional y no resuelve "
                "el microclima de cada hexágono H3."
            ),
        }
    else:
        weather_metadata = {
            "source": "Open-Meteo; metadatos de interpolación no registrados",
            "sampling_step_degrees": None,
            "interpolation": "legacy_unknown",
            "max_nearest_source_distance_km": None,
            "resolution_note": (
                "Snapshot heredado: no se registró la resolución de consulta ni "
                "el método de asignación meteorológica. Actualiza la meteorología "
                "con el pipeline para regenerar estos datos."
            ),
        }
    outputs = {}
    for species_id in SPECIES:
        species_rules = engine.species_matrix[species_id]
        habitat_rules = species_rules["habitat"]
        edaphic_rules = species_rules["edaphic"]
        meteo_rules = species_rules["meteorology"]
        cells = []
        for row in candidate_rows:
            source = dict(zip(columns, row))
            static = {
                key: source[key]
                for key in (
                    "elevation_m", "slope_deg", "aspect_cardinal",
                    "soil_ph", "dominant_species", "canopy_cover",
                    "environmental_data_quality",
                )
            }
            meteo = {
                "precip_accum_14d_mm": source["precip_accum_14d_mm"],
                "temp_mean_14d_celsius": source["temp_mean_14d_celsius"],
                "temp_min_14d_celsius": source["temp_min_14d_celsius"],
                "soil_moisture_percent": source["soil_moisture_percent"],
            }
            if source["precip_daily_mm_json"]:
                meteo["precip_daily_mm"] = json.loads(
                    source["precip_daily_mm_json"]
                )
            result = engine.calculate_relative_suitability(
                species_id, static, meteo, season_phase=season_phase
            )
            breakdown = result["breakdown"]
            cells.append({
                "h": source["h3_index"],
                "s": result["score"],
                "hs": result["score"],
                "ws": result["weather_score"],
                "st": result["status"],
                "reason": result["reason"],
                "host": breakdown.get("host_assessment", "unknown"),
                "lat": source["latitude"],
                "lon": source["longitude"],
                "elev": source["elevation_m"],
                "slope": source["slope_deg"],
                "asp": source["aspect_cardinal"],
                "ph": source["soil_ph"],
                "ph05": source["soil_ph_q05"],
                "ph95": source["soil_ph_q95"],
                "tree": source["dominant_species"],
                "cc": source["canopy_cover"],
                "p14": meteo["precip_accum_14d_mm"],
                "pe": breakdown["effective_precip_14d_mm"],
                "t": meteo["temp_mean_14d_celsius"],
                "tmin": meteo["temp_min_14d_celsius"],
                "moist": meteo["soil_moisture_percent"],
                "weather_distance_km": source["weather_nearest_distance_km"],
                "met": breakdown.get("meteorology"),
                "edaphic": breakdown.get("edaphic"),
                "topo": breakdown.get("topography"),
            })

        payload = {
            "date": snapshot_date.isoformat(),
            "generated_at": generated_at,
            "res": 8,
            "data_quality": "sourced_unvalidated",
            "score_type": "experimental_separated_habitat_weather",
            "score_label": "Índices separados de hábitat potencial y meteorología reciente (0–100)",
            "is_probability": False,
            "season_phase": season_phase,
            "habitat_filter": {
                "scope": "shared_union_of_target_species",
                "excluded_cells": excluded_cell_count,
                "unknown_hosts_retained": True,
            },
            "weather_metadata": weather_metadata,
            "cells_count": len(cells),
            "species_id": species_id,
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
                "soil_moisture_min_percent": meteo_rules["soil_moisture_min_percent"],
            },
            "gbif_occurrences_with_uncertainty_le_250m": observation_counts.get(
                GBIF_TAXON_KEYS[species_id], 0
            ),
            "limitations": [
                "Índice heurístico, no probabilidad de encontrar fructificación.",
                "Las reglas ecológicas aún no se han calibrado con validación independiente.",
                "El índice de hábitat potencial excluye meteorología; la señal meteorológica "
                "reciente se muestra por separado y no es una predicción de presencia.",
                "La lluvia eficaz usa una ponderación heurística de 14 días: la lluvia "
                "intermedia pesa más que la de ayer; el retardo no se ha calibrado "
                "con fructificaciones locales.",
                "Solo se exportan celdas que pasan las exclusiones estáticas de hábitat "
                "para al menos una especie; hospedadores no identificados se conservan.",
                "El filtro común no elimina celdas por meteorología, que puede cambiar "
                "en la actualización diaria.",
                "Meteo de los últimos 14 días interpolada desde una malla regional.",
                "Relieve y suelo de 200/250 m; las celdas H3 son más pequeñas.",
                "Los registros oportunistas de GBIF no corrigen el esfuerzo de observación.",
            ],
            "cells": cells,
        }
        out_path = output_dir / f"{species_id}.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_dir,
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, separators=(",", ":"), ensure_ascii=False)
            temp_path = Path(temp_file.name)
        try:
            os.replace(temp_path, out_path)
        finally:
            temp_path.unlink(missing_ok=True)
        outputs[species_id] = {
            "path": out_path,
            "cells": len(cells),
            "favorability_50_plus": sum(cell["s"] >= 50 for cell in cells),
        }
    return {
        "snapshot_date": snapshot_date.isoformat(),
        "outputs": outputs,
    }


if __name__ == "__main__":
    result = export_relative_suitability()
    print(
        f"Índices ambientales experimentales exportados para "
        f"{result['snapshot_date']}."
    )
    for species_id, summary in result["outputs"].items():
        print(
            f"{species_id}: {summary['cells']} celdas; "
            f"{summary['favorability_50_plus']} con indice >=50. "
            f"{summary['path']}"
        )
    print("El índice no es una probabilidad ni una predicción validada.")
