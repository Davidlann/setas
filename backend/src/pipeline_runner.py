"""Orquestador experimental; requiere variables ambientales verificadas para puntuar."""
import sys
from pathlib import Path
from typing import List, Dict, Any

current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.src.processing.duckdb_manager import DuckDBManager
from backend.src.ingestion.dynamic.meteo_snapshots import refresh_weather_snapshots
from backend.src.scoring.engine import MushroomScoringEngine
from backend.src.export_geojson import export_all_species

def run_daily_pipeline(target_species: List[str] = None):
    if target_species is None:
        target_species = ["boletus_edulis", "boletus_aereus", "boletus_pinophilus", "boletus_reticulatus"]

    print("[1/5] Conectando con la base espacial DuckDB...")
    db = DuckDBManager()

    # Obtener todas las celdas estáticas indexadas
    with db.get_connection() as con:
        rows = con.execute("""
            SELECT 
                h3_index, latitude, longitude, elevation_m, slope_deg,
                aspect_cardinal, soil_ph, dominant_species, canopy_cover, is_forest,
                environmental_data_quality
            FROM cells_static
            WHERE is_forest = TRUE
        """).fetchall()
        cols = [desc[0] for desc in con.description]

    static_cells = [dict(zip(cols, r)) for r in rows]
    print(f"[2/5] {len(static_cells)} celdas marcadas como forestales cargadas desde la base local.")

    if not static_cells:
        print("[!] No hay celdas estáticas en la base. Ejecuta primero mfe_processor.py.")
        return

    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    outside_region = [
        cell["h3_index"] for cell in static_cells
        if not isinstance(cell["latitude"], (int, float))
        or not isinstance(cell["longitude"], (int, float))
        or not (
            min_lon <= cell["longitude"] <= max_lon
            and min_lat <= cell["latitude"] <= max_lat
        )
    ]
    if outside_region:
        raise RuntimeError(
            f"No se puede calcular el índice: {len(outside_region)} celdas forestales "
            "están fuera del área de estudio configurada o no tienen coordenadas válidas."
        )

    unverified_cells = [
        cell["h3_index"] for cell in static_cells
        if cell["environmental_data_quality"] != "verified"
        or any(cell[key] is None for key in (
            "elevation_m", "slope_deg", "aspect_cardinal", "soil_ph"
        ))
    ]
    if unverified_cells:
        raise RuntimeError(
            f"No se puede calcular el índice: {len(unverified_cells)} celdas forestales "
            "carecen de variables ambientales verificadas (altitud, pendiente, orientación "
            "y pH). Añade esas capas con procedencia documentada antes de ejecutar el pipeline."
        )

    print("[3/5] Actualizando la malla meteorológica regional...")
    weather_snapshot = refresh_weather_snapshots(db=db)
    today_str = weather_snapshot["snapshot_date"]
    weather_by_cell = {
        record["h3_index"]: record for record in weather_snapshot["records"]
    }
    meteo_upserts = [weather_by_cell[cell["h3_index"]] for cell in static_cells]
    print(
        f"-> Snapshot para {weather_snapshot['snapshot_date']}: "
        f"{weather_snapshot['grid_nodes_count']} nodos, "
        f"{weather_snapshot['cells_count']} celdas interpoladas con IDW-4. "
        "Los valores siguen siendo de resolución meteorológica regional."
    )

    print(f"[4/5] Ejecutando Motor Paramétrico de Scoring para las 4 especies de Boletus...")
    engine = MushroomScoringEngine()
    all_scores = []

    for cell, m_data in zip(static_cells, meteo_upserts):
        for sp in target_species:
            res = engine.calculate_cell_score(
                species_id=sp,
                cell_static_data=cell,
                cell_meteo_data=m_data,
                season_phase="early"
            )
            all_scores.append({
                "h3_index": cell["h3_index"],
                "species_id": sp,
                "score": res["score"],
                "status": res["status"],
                "breakdown": res["breakdown"]
            })

    db.upsert_scores(all_scores, today_str)
    print(f"-> {len(all_scores)} evaluaciones calculadas ({len(static_cells)} celdas x {len(target_species)} especies).")

    print("[5/5] Exportando Snapshots GeoJSON para el Visor Cartográfico...")
    export_all_species()
    print("=" * 80)
    print("Pipeline completado con éxito para Madrid, Segovia, Soria y Guadalajara.")

if __name__ == "__main__":
    run_daily_pipeline()
