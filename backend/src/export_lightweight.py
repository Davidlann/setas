"""
Exportador optimizado: genera datos ligeros (sin geometría) para el frontend.
El cliente renderiza los polígonos H3 usando h3-js, sin peso de coordenadas redundantes.
"""
import sys
import json
import duckdb
from pathlib import Path

current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.config.settings import TARGET_REGION_BOUNDS

OUTPUT_DIR = project_root / "frontend" / "public" / "data"
DB_PATH = project_root / "backend" / "data" / "processed" / "mycologist_spatial.duckdb"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SPECIES = ["boletus_edulis", "boletus_aereus", "boletus_pinophilus", "boletus_reticulatus"]

def export_lightweight():
    con = duckdb.connect(str(DB_PATH))
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    static_table_columns = {
        row[0] for row in con.execute("DESCRIBE cells_static").fetchall()
    }
    texture_select = ", ".join(
        f"c.{column}" if column in static_table_columns
        else f"NULL AS {column}"
        for column in (
            "soil_sand_percent",
            "soil_silt_percent",
            "soil_clay_percent",
        )
    )
    verified_cells = con.execute("""
        SELECT COUNT(*) FROM cells_static
        WHERE is_forest = TRUE AND environmental_data_quality = 'verified'
          AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?
    """, [min_lon, max_lon, min_lat, max_lat]).fetchone()[0]
    if not verified_cells:
        con.close()
        raise RuntimeError(
            "No se exportan puntuaciones: faltan variables ambientales verificadas."
        )

    for sp in SPECIES:
        # Obtener la fecha más reciente
        calc_date = con.execute("""
            SELECT MAX(s.calculation_date)
            FROM fruiting_scores s
            JOIN cells_static c ON s.h3_index = c.h3_index
            WHERE s.species_id = ? AND c.environmental_data_quality = 'verified'
              AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
        """, [sp, min_lon, max_lon, min_lat, max_lat]).fetchone()[0]
        if calc_date is None:
            raise RuntimeError(f"No hay puntuaciones verificadas para {sp}.")

        rows = con.execute(f"""
            SELECT
                s.h3_index,
                s.score,
                s.status,
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
                m.temp_min_14d_celsius,
                m.soil_moisture_percent,
                {texture_select}
            FROM fruiting_scores s
            JOIN cells_static c ON s.h3_index = c.h3_index
            LEFT JOIN meteo_snapshots m
                ON s.h3_index = m.h3_index AND s.calculation_date = m.snapshot_date
            WHERE s.species_id = ? AND s.calculation_date = ?
              AND c.environmental_data_quality = 'verified'
              AND c.longitude BETWEEN ? AND ? AND c.latitude BETWEEN ? AND ?
            ORDER BY s.score DESC
        """, [sp, calc_date, min_lon, max_lon, min_lat, max_lat]).fetchall()
        if not rows:
            raise RuntimeError(
                f"No hay puntuaciones verificadas para {sp} en la fecha {calc_date}."
            )

        # Formato ultra-ligero: lista de objetos simples (sin GeoJSON)
        features = []
        for r in rows:
            features.append({
                "h": r[0],                # h3_index (clave de renderizado en cliente)
                "s": round(float(r[1]), 1) if r[1] else 0.0,   # score
                "st": r[2],               # status
                "lat": round(float(r[3]), 5) if r[3] else 0,
                "lon": round(float(r[4]), 5) if r[4] else 0,
                "elev": int(r[5]) if r[5] else 0,
                "slope": round(float(r[6]), 1) if r[6] else 0,
                "asp": r[7] or "N",
                "ph": round(float(r[8]), 1) if r[8] else 0,
                "tree": r[9] or "",
                "cc": round(float(r[10]), 0) if r[10] else 0,
                "p14": round(float(r[11]), 1) if r[11] else 0,  # precip 14d
                "t": round(float(r[12]), 1) if r[12] else 0,    # temp mean
                "tmin": round(float(r[13]), 1) if r[13] else 0, # temp min
                "moist": round(float(r[14]), 1) if r[14] else 0,
                "sand": round(float(r[15]), 1) if r[15] is not None else None,
                "silt": round(float(r[16]), 1) if r[16] is not None else None,
                "clay": round(float(r[17]), 1) if r[17] is not None else None,
            })

        out = {
            "date": str(calc_date),
            "res": 8,
            "data_quality": "verified",
            "cells": features
        }
        out_path = OUTPUT_DIR / f"{sp}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, separators=(",", ":"), ensure_ascii=False)

        size_kb = out_path.stat().st_size / 1024
        print(f"{sp}: {len(features)} celdas -> {size_kb:.0f} KB ({out_path.name})")

    con.close()
    print("Exportación ligera completada.")

if __name__ == "__main__":
    export_lightweight()
