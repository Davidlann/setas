"""
Generador experimental de celdas forestales desde MFE50 para cuatro provincias.
La rasterización H3 de centroides no representa una cobertura completa de bosque.
"""
import sys
import json
from pathlib import Path
from typing import Dict, List, Any
import duckdb
import h3

current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.config.settings import RAW_DATA_DIR, H3_RESOLUTION, DB_PATH, TARGET_REGION_BOUNDS
from backend.src.processing.h3_indexer import lat_lng_to_h3, IS_H3_V4
from backend.src.processing.duckdb_manager import DuckDBManager
from backend.src.processing.mfe_processor import FOREST_SPECIES_MAPPING, normalize_forest_name

def build_complete_forest_mesh():
    print("=" * 80)
    print("INICIANDO CONSTRUCCIÓN EXPERIMENTAL DE MALLA FORESTAL")
    print("Provincias: Madrid, Segovia, Soria y Guadalajara")
    print("=" * 80)

    db = DuckDBManager()
    con = db.get_connection()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("SET geometry_always_xy = true;")

    target_provinces = {
        "madrid": RAW_DATA_DIR / "mfe50" / "madrid" / "mfe50_28.shp",
        "segovia": RAW_DATA_DIR / "mfe50" / "segovia" / "mfe50_40.shp",
        "soria": RAW_DATA_DIR / "mfe50" / "soria" / "mfe50_42.shp",
        "guadalajara": RAW_DATA_DIR / "mfe50" / "guadalajara" / "mfe50_19.shp"
    }

    all_cells: Dict[str, Dict[str, Any]] = {}
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS

    for prov_name, shp_path in target_provinces.items():
        if not shp_path.exists():
            print(f"[!] No se encontró el shapefile para {prov_name}: {shp_path}")
            continue

        print(f"\n>>> Procesando masas forestales de {prov_name.upper()}...")
        # Extraemos centroide y también GeoJSON para polígonos grandes
        query = f"""
            SELECT 
                NOM_FORARB,
                TFCCARB,
                Shape_Area,
                ST_X(ST_Centroid(ST_Transform(geom, 'EPSG:25830', 'EPSG:4326'))) as lon,
                ST_Y(ST_Centroid(ST_Transform(geom, 'EPSG:25830', 'EPSG:4326'))) as lat,
                CASE 
                    WHEN Shape_Area >= 1500000 THEN ST_AsGeoJSON(ST_Transform(geom, 'EPSG:25830', 'EPSG:4326'))
                    ELSE NULL 
                END as geojson_geom
            FROM ST_Read('{shp_path.resolve().as_posix()}')
            WHERE USOS_SUELO NOT IN ('Artificial', 'Agua') 
              AND NOM_FORARB != 'Sin formación arbolada'
              AND TFCCARB >= 20.0
            ORDER BY Shape_Area DESC
        """
        rows = con.execute(query).fetchall()
        print(f"-> {len(rows)} masas forestales arboladas encontradas en {prov_name}.")

        prov_cells_count = 0
        for r in rows:
            formacion = r[0]
            canopy = float(r[1]) if r[1] is not None else 50.0
            area_m2 = float(r[2]) if r[2] is not None else 0.0
            lon = float(r[3])
            lat = float(r[4])
            geojson_str = r[5]

            botanical_species = normalize_forest_name(formacion)

            # Lista de celdas H3 a las que aplica este rodal
            cells_to_add = []

            # 1. Si es una gran masa boscosa continua (> 1.5 km² = 150 ha), hacemos polyfill para rellenar toda la superficie
            if geojson_str and area_m2 >= 1500000:
                try:
                    poly_dict = json.loads(geojson_str)
                    if IS_H3_V4:
                        if poly_dict.get("type") == "Polygon":
                            poly_cells = list(h3.polygon_to_cells(poly_dict, H3_RESOLUTION))
                            cells_to_add.extend(poly_cells)
                        elif poly_dict.get("type") == "MultiPolygon":
                            for sub_coords in poly_dict.get("coordinates", []):
                                sub_poly = {"type": "Polygon", "coordinates": sub_coords}
                                sub_cells = list(h3.polygon_to_cells(sub_poly, H3_RESOLUTION))
                                cells_to_add.extend(sub_cells)
                    else:
                        if poly_dict.get("type") == "Polygon":
                            poly_cells = list(h3.polyfill(poly_dict, H3_RESOLUTION, geo_json_conformant=True))
                            cells_to_add.extend(poly_cells)
                except (TypeError, ValueError, KeyError) as exc:
                    raise RuntimeError(
                        f"No se pudo rasterizar una geometría MFE50 de {prov_name}."
                    ) from exc

            # 2. Si no se generaron celdas de polyfill (o es un rodal menor), usamos el centroide
            if not cells_to_add:
                # Validar límites de la provincia
                if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
                    c_h3 = lat_lng_to_h3(lat, lon, H3_RESOLUTION)
                    cells_to_add.append(c_h3)

            for cell_id in cells_to_add:
                if IS_H3_V4:
                    c_lat, c_lon = h3.cell_to_latlng(cell_id)
                else:
                    c_lat, c_lon = h3.h3_to_geo(cell_id)
                if not (min_lon <= c_lon <= max_lon and min_lat <= c_lat <= max_lat):
                    continue
                if cell_id not in all_cells:
                    all_cells[cell_id] = {
                        "h3_index": cell_id,
                        "latitude": round(c_lat, 5),
                        "longitude": round(c_lon, 5),
                        "elevation_m": None,
                        "slope_deg": None,
                        "aspect_cardinal": None,
                        "soil_ph": None,
                        "dominant_species": botanical_species,
                        "canopy_cover": canopy,
                        "is_forest": True,
                        "environmental_data_quality": "unverified"
                    }
                    prov_cells_count += 1

        print(f"-> {prov_cells_count} hexágonos H3 únicos añadidos para {prov_name}.")

    print(f"\n[TOTAL] Malla forestal completa construida: {len(all_cells)} hexágonos H3 (Res 8).")
    print("Guardando en la base de datos DuckDB...")
    db.upsert_static_cells(list(all_cells.values()))
    print("Guardado completado exitosamente.")
    return len(all_cells)

if __name__ == "__main__":
    build_complete_forest_mesh()
