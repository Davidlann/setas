"""Actualiza snapshots meteorológicos regionales sin calcular scores validados."""
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.config.settings import TARGET_REGION_BOUNDS
from backend.src.ingestion.dynamic.aemet_opendata import calibrate_weather_records_with_aemet
from backend.src.ingestion.dynamic.meteo_openmeteo import (
    OpenMeteoClient,
    WEATHER_GRID_STEP_DEG,
)
from backend.src.processing.duckdb_manager import DuckDBManager


def refresh_weather_snapshots(db=None, meteo_client=None):
    """Descarga e interpola meteo para celdas forestales dentro del área objetivo."""
    db = db or DuckDBManager()
    meteo_client = meteo_client or OpenMeteoClient(request_delay_seconds=0.8)
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS

    with db.get_connection() as con:
        rows = con.execute("""
            SELECT h3_index, latitude, longitude, elevation_m
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
        {
            "h3_index": row[0],
            "latitude": row[1],
            "longitude": row[2],
            "elevation_m": row[3],
        }
        for row in rows
    ]
    coordinates = [(cell["latitude"], cell["longitude"]) for cell in cells]
    elevations = [cell["elevation_m"] for cell in cells]
    grid_coords = OpenMeteoClient.build_forest_covering_grid(
        coordinates, grid_step_deg=WEATHER_GRID_STEP_DEG
    )
    meteo_grid = meteo_client.fetch_weather_for_coords(
        grid_coords,
        grid_step_deg=WEATHER_GRID_STEP_DEG,
        past_days=28,
        forecast_days=3,
    )
    interpolated = meteo_client.batch_interpolate_weather(
        coordinates,
        meteo_grid,
        grid_step_deg=WEATHER_GRID_STEP_DEG,
        target_elevations=elevations,
    )
    snapshot_date = datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()

    records = []
    for cell, weather in zip(cells, interpolated):
        records.append({
            "h3_index": cell["h3_index"],
            "precip_daily_mm": weather["precip_daily_mm"],
            "precip_daily_28d_mm": weather.get("precip_daily_28d_mm"),
            "soil_temp_daily_28d_c": weather.get("soil_temp_daily_28d_c"),
            "temp_min_daily_28d_c": weather.get("temp_min_daily_28d_c"),
            "soil_moisture_daily_28d_pct": weather.get("soil_moisture_daily_28d_pct"),
            "precip_last_3d_mm": weather.get("precip_last_3d_mm"),
            "precip_accum_7d_mm": weather["precip_accum_7d_mm"],
            "precip_accum_10d_mm": weather["precip_accum_10d_mm"],
            "precip_accum_14d_mm": weather["precip_accum_14d_mm"],
            "precip_accum_28d_mm": weather.get("precip_accum_28d_mm"),
            "precip_15_28d_mm": weather.get("precip_15_28d_mm"),
            "temp_mean_14d_celsius": weather["temp_mean_14d_celsius"],
            "temp_mean_recent_3d_celsius": weather.get("temp_mean_recent_3d_celsius"),
            "temp_min_14d_celsius": weather["temp_min_14d_celsius"],
            "temp_min_recent_3d_celsius": weather.get("temp_min_recent_3d_celsius"),
            "soil_moisture_percent": weather["soil_moisture_percent"],
            "humidity_mean_7d_percent": weather.get("humidity_mean_7d_percent"),
            "soil_temp_mean_14d_celsius": weather.get("soil_temp_mean_14d_celsius"),
            "soil_temp_0_7cm_3d_celsius": weather.get("soil_temp_0_7cm_3d_celsius"),
            "soil_temp_root_mean_14d_celsius": weather.get("soil_temp_root_mean_14d_celsius"),
            "et0_accum_14d_mm": weather.get("et0_accum_14d_mm"),
            "water_balance_14d_mm": weather.get("water_balance_14d_mm"),
            "thermal_shock_celsius": weather.get("thermal_shock_celsius"),
            "wind_max_14d_kmh": weather.get("wind_max_14d_kmh"),
            "snowfall_7d_cm": weather.get("snowfall_7d_cm"),
            "snowfall_3d_fc_cm": weather.get("snowfall_3d_fc_cm"),
            "radiation_mean_7d_mj": weather.get("radiation_mean_7d_mj"),
            "forecast_3d_precip_daily": weather.get("forecast_3d_precip_daily"),
            "forecast_3d_temp_min_daily": weather.get("forecast_3d_temp_min_daily"),
            "frost_days_7d": weather.get("frost_days_7d"),
            "frost_days_14d": weather.get("frost_days_14d"),
            "hard_frost_28d": weather.get("hard_frost_28d"),
            "pre_rain_dry_streak_days": weather.get("pre_rain_dry_streak_days"),
            "pre_rain_min_soil_moisture_pct": weather.get("pre_rain_min_soil_moisture_pct"),
            "forecast_3d_precip_mm": weather.get("forecast_3d_precip_mm"),
            "forecast_3d_temp_min_celsius": weather.get("forecast_3d_temp_min_celsius"),
            "forecast_3d_temp_mean_celsius": weather.get("forecast_3d_temp_mean_celsius"),
            "aemet_calibrated": False,
            "aemet_stations_used": 0,
            "weather_source": weather["weather_source"],
            "weather_sampling_step_deg": weather["weather_sampling_step_deg"],
            "weather_interpolation": weather["weather_interpolation"],
            "weather_nearest_distance_km": weather["weather_nearest_distance_km"],
        })

    cell_meta_by_h3 = {
        cell["h3_index"]: (cell["latitude"], cell["longitude"], cell["elevation_m"])
        for cell in cells
    }
    records, aemet_info = calibrate_weather_records_with_aemet(
        records, cell_meta_by_h3, snapshot_date
    )

    db.upsert_meteo_snapshot(records, snapshot_date)
    if hasattr(db, "prune_old_meteo_snapshots"):
        pruned = db.prune_old_meteo_snapshots(keep_dates=2)
        if pruned:
            print(f"[DuckDB] Podadas {pruned} filas de snapshots antiguos.")
    if hasattr(db, "compact_database"):
        db.compact_database()
        print("[DuckDB] Base compactada correctamente.")
    return {
        "snapshot_date": snapshot_date,
        "cells_count": len(records),
        "grid_nodes_count": len(meteo_grid),
        "max_nearest_source_distance_km": max(
            record["weather_nearest_distance_km"] for record in records
        ),
        "aemet_calibrated": bool(aemet_info.get("aemet_calibrated")),
        "aemet_stations_used": int(aemet_info.get("aemet_stations_used", 0)),
        "records": records,
    }


if __name__ == "__main__":
    result = refresh_weather_snapshots()
    print(
        f"Snapshot Open-Meteo para {result['snapshot_date']}: "
        f"{result['cells_count']} celdas, {result['grid_nodes_count']} nodos, "
        f"interpolación IDW-4 con ajuste altitudinal; distancia máxima al nodo: "
        f"{result['max_nearest_source_distance_km']:.1f} km."
    )
    print(
        "Los valores siguen siendo meteorología de modelo regional; "
        "la interpolación no crea datos a escala de hexágono."
    )
