"""
Gestor de persistencia columnar y espacial con DuckDB.
Maneja tablas de celdas estáticas, métricas meteorológicas y scores diarios sin servidores externos.
"""
from typing import List, Dict, Any, Optional
import json
import duckdb
from pathlib import Path
from backend.config.settings import DB_PATH, TARGET_REGION_BOUNDS
from backend.src.processing.h3_indexer import h3_to_geojson_polygon

class DuckDBManager:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = str(db_path or DB_PATH)
        self.init_schema()

    def get_connection(self):
        return duckdb.connect(self.db_path)

    def init_schema(self):
        """Inicializa las tablas y la extensión espacial de DuckDB."""
        with self.get_connection() as con:
            con.execute("INSTALL spatial; LOAD spatial;")

            # 1. Tabla de celdas base H3 con variables estáticas de España
            con.execute("""
                CREATE TABLE IF NOT EXISTS cells_static (
                    h3_index VARCHAR PRIMARY KEY,
                    latitude DOUBLE,
                    longitude DOUBLE,
                    elevation_m DOUBLE,
                    slope_deg DOUBLE,
                    aspect_cardinal VARCHAR,
                    soil_ph DOUBLE,
                    soil_sand_percent DOUBLE,
                    soil_silt_percent DOUBLE,
                    soil_clay_percent DOUBLE,
                    dominant_species VARCHAR,
                    canopy_cover DOUBLE,
                    is_forest BOOLEAN,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            con.execute("""
                ALTER TABLE cells_static
                ADD COLUMN IF NOT EXISTS environmental_data_quality
                VARCHAR DEFAULT 'unverified'
            """)
            for column in (
                "soil_sand_percent",
                "soil_silt_percent",
                "soil_clay_percent",
            ):
                con.execute(
                    f"ALTER TABLE cells_static ADD COLUMN IF NOT EXISTS {column} DOUBLE"
                )

            # 2. Histórico y snapshots de meteorología real diaria
            con.execute("""
                CREATE TABLE IF NOT EXISTS meteo_snapshots (
                    h3_index VARCHAR,
                    snapshot_date DATE,
                    precip_accum_7d_mm DOUBLE,
                    precip_accum_10d_mm DOUBLE,
                    precip_accum_14d_mm DOUBLE,
                    temp_mean_14d_celsius DOUBLE,
                    temp_min_14d_celsius DOUBLE,
                    soil_moisture_percent DOUBLE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (h3_index, snapshot_date)
                );
            """)
            for column, column_type in (
                ("precip_daily_mm_json", "VARCHAR"),
                ("weather_source", "VARCHAR"),
                ("weather_sampling_step_deg", "DOUBLE"),
                ("weather_interpolation", "VARCHAR"),
                ("weather_nearest_distance_km", "DOUBLE"),
                ("soil_temp_mean_14d_celsius", "DOUBLE"),
                ("et0_accum_14d_mm", "DOUBLE"),
                ("water_balance_14d_mm", "DOUBLE"),
                ("thermal_shock_celsius", "DOUBLE"),
                ("wind_max_14d_kmh", "DOUBLE"),
            ):
                con.execute(
                    f"ALTER TABLE meteo_snapshots ADD COLUMN IF NOT EXISTS "
                    f"{column} {column_type}"
                )

            # 3. Predicciones diarias del Índice de Fructificación
            con.execute("""
                CREATE TABLE IF NOT EXISTS fruiting_scores (
                    h3_index VARCHAR,
                    species_id VARCHAR,
                    calculation_date DATE,
                    score DOUBLE,
                    status VARCHAR,
                    breakdown_json VARCHAR,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (h3_index, species_id, calculation_date)
                );
            """)

    def upsert_static_cells(self, cells: List[Dict[str, Any]]):
        """Inserta o actualiza un lote de celdas estáticas."""
        if not cells:
            return

        with self.get_connection() as con:
            con.executemany("""
                INSERT OR REPLACE INTO cells_static (
                    h3_index, latitude, longitude, elevation_m, slope_deg,
                    aspect_cardinal, soil_ph, soil_sand_percent, soil_silt_percent,
                    soil_clay_percent, dominant_species, canopy_cover, is_forest,
                    environmental_data_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    c["h3_index"], c["latitude"], c["longitude"], c["elevation_m"],
                    c["slope_deg"], c["aspect_cardinal"], c["soil_ph"],
                    c.get("soil_sand_percent"), c.get("soil_silt_percent"),
                    c.get("soil_clay_percent"),
                    c["dominant_species"], c["canopy_cover"], c["is_forest"],
                    c.get("environmental_data_quality", "unverified")
                )
                for c in cells
            ])

    def upsert_meteo_snapshot(self, records: List[Dict[str, Any]], snapshot_date: str):
        """Inserta métricas meteorológicas reales para la fecha dada."""
        if not records:
            return

        with self.get_connection() as con:
            con.executemany("""
                INSERT OR REPLACE INTO meteo_snapshots (
                    h3_index, snapshot_date, precip_accum_7d_mm, precip_accum_10d_mm,
                    precip_accum_14d_mm, precip_daily_mm_json,
                    temp_mean_14d_celsius, temp_min_14d_celsius,
                    soil_moisture_percent, weather_source,
                    weather_sampling_step_deg, weather_interpolation,
                    weather_nearest_distance_km,
                    soil_temp_mean_14d_celsius, et0_accum_14d_mm,
                    water_balance_14d_mm, thermal_shock_celsius,
                    wind_max_14d_kmh
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    r["h3_index"], snapshot_date, r["precip_accum_7d_mm"], r["precip_accum_10d_mm"],
                    r["precip_accum_14d_mm"],
                    json.dumps(r["precip_daily_mm"], separators=(",", ":")),
                    r["temp_mean_14d_celsius"], r["temp_min_14d_celsius"],
                    r["soil_moisture_percent"], r.get("weather_source"),
                    r.get("weather_sampling_step_deg"), r.get("weather_interpolation"),
                    r.get("weather_nearest_distance_km"),
                    r.get("soil_temp_mean_14d_celsius"),
                    r.get("et0_accum_14d_mm"),
                    r.get("water_balance_14d_mm"),
                    r.get("thermal_shock_celsius"),
                    r.get("wind_max_14d_kmh")
                )
                for r in records
            ])

    def upsert_scores(self, scores: List[Dict[str, Any]], calculation_date: str):
        """Inserta scores diarios calculados por especie."""
        if not scores:
            return

        with self.get_connection() as con:
            con.executemany("""
                INSERT OR REPLACE INTO fruiting_scores (
                    h3_index, species_id, calculation_date, score, status, breakdown_json
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (
                    s["h3_index"], s["species_id"], calculation_date, s["score"],
                    s["status"], str(s.get("breakdown", {}))
                )
                for s in scores
            ])

    def query_geojson_layer(self, species_id: str, min_score: float = 0.0) -> Dict[str, Any]:
        """
        Retorna un GeoJSON FeatureCollection para la fecha verificada más reciente.
        """
        with self.get_connection() as con:
            min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
            latest_date = con.execute("""
                SELECT MAX(s.calculation_date)
                FROM fruiting_scores s
                JOIN cells_static c ON s.h3_index = c.h3_index
                WHERE s.species_id = ?
                  AND c.environmental_data_quality = 'verified'
                  AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
            """, [
                species_id, min_lon, max_lon, min_lat, max_lat
            ]).fetchone()[0]
            if latest_date is None:
                return {"type": "FeatureCollection", "features": []}

            rows = con.execute("""
                SELECT 
                    s.h3_index,
                    c.latitude,
                    c.longitude,
                    c.elevation_m,
                    c.dominant_species,
                    c.soil_ph,
                    c.soil_sand_percent,
                    c.soil_silt_percent,
                    c.soil_clay_percent,
                    s.score,
                    s.status,
                    s.calculation_date
                FROM fruiting_scores s
                JOIN cells_static c ON s.h3_index = c.h3_index
                WHERE s.species_id = ? AND s.calculation_date = ? AND s.score >= ?
                  AND c.environmental_data_quality = 'verified'
                  AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
                ORDER BY s.score DESC
            """, [
                species_id, latest_date, min_score,
                min_lon, max_lon, min_lat, max_lat
            ]).fetchall()
            columns = [description[0] for description in con.description]

        features = []
        for row in rows:
            data = dict(zip(columns, row))
            feature = h3_to_geojson_polygon(data["h3_index"])
            feature["properties"].update({
                key: (str(value) if key == "calculation_date" else value)
                for key, value in data.items()
            })
            features.append(feature)

        return {"type": "FeatureCollection", "features": features}
