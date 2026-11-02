"""API geoespacial experimental para servir celdas con datos verificados."""
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from pathlib import Path
import sys

root_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_path))

from backend.src.processing.duckdb_manager import DuckDBManager
from backend.src.processing.h3_indexer import h3_to_geojson_polygon
from backend.config.settings import TARGET_REGION_BOUNDS

app = FastAPI(
    title="Mapa setero API - España",
    description="API experimental de análisis micológico; solo sirve índices con variables ambientales verificadas.",
    version="1.0.0"
)

# Permitir CORS para el frontend (Next.js / MapLibre)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = DuckDBManager()

@app.get("/")
def health_check():
    return {
        "status": "online",
        "service": "Mapa setero Geospatial Engine",
        "coverage": (
            "objetivo: Madrid, Segovia, Soria y Guadalajara; "
            "alcance geográfico no validado"
        ),
        "supported_species": [
            "boletus_edulis",
            "boletus_aereus",
            "boletus_pinophilus",
            "boletus_reticulatus"
        ]
    }

@app.get("/api/scores/geojson")
def get_scores_geojson(
    species_id: str,
    min_score: float = 0.0,
    date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retorna polígonos H3 solo cuando las variables ambientales están verificadas.
    """
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    with db.get_connection() as con:
        verified_cells = con.execute("""
            SELECT COUNT(*) FROM cells_static
            WHERE is_forest = TRUE AND environmental_data_quality = 'verified'
              AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?
        """, [min_lon, max_lon, min_lat, max_lat]).fetchone()[0]
        if not verified_cells:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No hay celdas dentro del área de estudio con variables ambientales "
                    "verificadas. Los índices históricos no se publican como predicciones."
                )
            )

        # Obtener la fecha más reciente si no se pasa
        if not date:
            recent_date_row = con.execute("""
                SELECT MAX(s.calculation_date)
                FROM fruiting_scores s
                JOIN cells_static c ON s.h3_index = c.h3_index
                WHERE s.species_id = ?
                  AND c.environmental_data_quality = 'verified'
                  AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
            """, [species_id, min_lon, max_lon, min_lat, max_lat]).fetchone()
            if not recent_date_row or not recent_date_row[0]:
                raise HTTPException(status_code=404, detail=f"No hay registros calculados para {species_id}")
            calc_date = recent_date_row[0]
        else:
            calc_date = date

        query = """
            SELECT 
                s.h3_index,
                s.species_id,
                s.calculation_date,
                s.score,
                s.status,
                s.breakdown_json,
                c.latitude,
                c.longitude,
                c.elevation_m,
                c.slope_deg,
                c.aspect_cardinal,
                c.soil_ph,
                c.dominant_species,
                c.canopy_cover,
                m.precip_accum_14d_mm,
                m.temp_mean_14d_celsius,
                m.soil_moisture_percent,
                c.soil_sand_percent,
                c.soil_silt_percent,
                c.soil_clay_percent
            FROM fruiting_scores s
            JOIN cells_static c ON s.h3_index = c.h3_index
            LEFT JOIN meteo_snapshots m ON (s.h3_index = m.h3_index AND s.calculation_date = m.snapshot_date)
            WHERE s.species_id = ? AND s.calculation_date = ? AND s.score >= ?
              AND c.environmental_data_quality = 'verified'
              AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
            ORDER BY s.score DESC
        """
        rows = con.execute(query, [
            species_id, calc_date, min_score, min_lon, max_lon, min_lat, max_lat
        ]).fetchall()
        columns = [desc[0] for desc in con.description]

    features = []
    for r in rows:
        row_dict = dict(zip(columns, r))
        h3_idx = row_dict["h3_index"]
        feature = h3_to_geojson_polygon(h3_idx)
        feature["properties"].update({
            "species_id": row_dict["species_id"],
            "score": row_dict["score"],
            "status": row_dict["status"],
            "latitude": row_dict["latitude"],
            "longitude": row_dict["longitude"],
            "elevation_m": row_dict["elevation_m"],
            "slope_deg": row_dict["slope_deg"],
            "aspect": row_dict["aspect_cardinal"],
            "soil_ph": row_dict["soil_ph"],
            "soil_sand_percent": row_dict["soil_sand_percent"],
            "soil_silt_percent": row_dict["soil_silt_percent"],
            "soil_clay_percent": row_dict["soil_clay_percent"],
            "dominant_species": row_dict["dominant_species"],
            "precip_14d_mm": row_dict["precip_accum_14d_mm"],
            "temp_mean_c": row_dict["temp_mean_14d_celsius"],
            "soil_moisture_pct": row_dict["soil_moisture_percent"],
            "calculation_date": str(row_dict["calculation_date"])
        })
        features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features
    }
