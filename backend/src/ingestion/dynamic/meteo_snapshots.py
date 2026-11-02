"""Actualiza snapshots meteorológicos regionales sin calcular scores validados."""
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.config.settings import TARGET_REGION_BOUNDS
from backend.src.ingestion.dynamic.meteo_openmeteo import (
    OpenMeteoClient,
    WEATHER_GRID_STEP_DEG,
)
from backend.src.processing.duckdb_manager import DuckDBManager


def refresh_weather_snapshots(db=None, meteo_client=None):
    """Descarga e interpola meteo para celdas forestales dentro del área objetivo."""
    db = db or DuckDBManager()
    meteo_client = meteo_client or OpenMeteoClient(request_delay_seconds=1.0)
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS

    with db.get_connection() as con:
        rows = con.execute("""
            SELECT h3_index, latitude, longitude
            FROM cells_static
            WHERE is_forest = TRUE
              AND latitude BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            ORDER BY h3_index
        """, [min_lat, max_lat, min_lon, max_lon]).fetchall()

    if not rows:
        raise RuntimeError(
            "No hay celdas forestales con coordenadas dentro del área objetivo."
        )

    cells = [
        {"h3_index": row[0], "latitude": row[1], "longitude": row[2]}
        for row in rows
    ]
    lats = [cell["latitude"] for cell in cells]
    lons = [cell["longitude"] for cell in cells]
    padding = WEATHER_GRID_STEP_DEG
    meteo_grid = meteo_client.fetch_weather_for_bbox_grid(
        min_lat=min(lats) - padding,
        max_lat=max(lats) + padding,
        min_lon=min(lons) - padding,
        max_lon=max(lons) + padding,
        grid_step_deg=WEATHER_GRID_STEP_DEG,
        past_days=14,
    )
    coordinates = [(cell["latitude"], cell["longitude"]) for cell in cells]
    interpolated = meteo_client.batch_interpolate_weather(
        coordinates,
        meteo_grid,
        grid_step_deg=WEATHER_GRID_STEP_DEG,
    )
    snapshot_date = datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()

    records = []
    for cell, weather in zip(cells, interpolated):
        records.append({
            "h3_index": cell["h3_index"],
            "precip_daily_mm": weather["precip_daily_mm"],
            "precip_accum_7d_mm": weather["precip_accum_7d_mm"],
            "precip_accum_10d_mm": weather["precip_accum_10d_mm"],
            "precip_accum_14d_mm": weather["precip_accum_14d_mm"],
            "temp_mean_14d_celsius": weather["temp_mean_14d_celsius"],
            "temp_min_14d_celsius": weather["temp_min_14d_celsius"],
            "soil_moisture_percent": weather["soil_moisture_percent"],
            "weather_source": weather["weather_source"],
            "weather_sampling_step_deg": weather["weather_sampling_step_deg"],
            "weather_interpolation": weather["weather_interpolation"],
            "weather_nearest_distance_km": weather["weather_nearest_distance_km"],
        })

    db.upsert_meteo_snapshot(records, snapshot_date)
    return {
        "snapshot_date": snapshot_date,
        "cells_count": len(records),
        "grid_nodes_count": len(meteo_grid),
        "max_nearest_source_distance_km": max(
            record["weather_nearest_distance_km"] for record in records
        ),
        "records": records,
    }


if __name__ == "__main__":
    result = refresh_weather_snapshots()
    print(
        f"Snapshot Open-Meteo para {result['snapshot_date']}: "
        f"{result['cells_count']} celdas, {result['grid_nodes_count']} nodos, "
        f"interpolación IDW-4; distancia máxima al nodo: "
        f"{result['max_nearest_source_distance_km']:.1f} km."
    )
    print(
        "Los valores siguen siendo meteorología de modelo regional; "
        "la interpolación no crea datos a escala de hexágono."
    )
