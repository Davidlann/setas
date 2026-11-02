"""
Procesador Espacial de MFE50 oficial para Madrid, Segovia, Soria y Guadalajara.
Agrupa teselas forestales reales a la malla Uber H3 (Res 8) y las inserta en DuckDB.
"""
import sys
from pathlib import Path
from typing import Dict, List, Any
import unicodedata

# Raíz del proyecto
current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.config.settings import RAW_DATA_DIR, H3_RESOLUTION, TARGET_REGION_BOUNDS
from backend.src.processing.h3_indexer import lat_lng_to_h3
from backend.src.processing.duckdb_manager import DuckDBManager

# Mapeo de formaciones arboladas oficiales del MFE50 a nombres científicos
FOREST_SPECIES_MAPPING = {
    "pinares de pino albar": "Pinus sylvestris",
    "pinares de pino salgareno": "Pinus nigra",
    "pinares de pino laricio": "Pinus nigra",
    "pinares de pino pinaster en region mediterranea (p.pinaster ssp. mesogeensis)": "Pinus pinaster",
    "pinares de pino pinaster": "Pinus pinaster",
    "pinares de pino pinonero": "Pinus pinea",
    "pinares de pino carrasco": "Pinus halepensis",
    "pinares de pino negro": "Pinus uncinata",
    "hayedos": "Fagus sylvatica",
    "melojares": "Quercus pyrenaica",
    "robledales de roble albar (quercus petraea)": "Quercus petraea",
    "robledales de roble carballo (quercus robur)": "Quercus robur",
    "quejigares": "Quercus faginea",
    "encinares": "Quercus ilex",
    "dehesas": "Quercus ilex",
    "alcornocales": "Quercus suber",
    "castanares": "Castanea sativa"
}

def _normalize_forest_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(without_accents.split())


def normalize_forest_name(name: str) -> str:
    """Mapea solo formaciones MFE50 identificables a una especie concreta."""
    if not name:
        return ""
    name_clean = name.strip()
    mapped_species = FOREST_SPECIES_MAPPING.get(_normalize_forest_name(name_clean))
    if mapped_species:
        return mapped_species
    return name_clean

def process_target_provinces_to_h3(max_cells_per_province: int = 250):
    """
    Procesa las 4 provincias hacia la malla H3 Res 8.
    max_cells_per_province limita el número de celdas por provincia para mantener
    el volumen meteorológico dentro de los límites de consulta rápida de Open-Meteo.
    """
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

    all_h3_cells: Dict[str, Dict[str, Any]] = {}
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS

    for prov_name, shp_path in target_provinces.items():
        if not shp_path.exists():
            print(f"[!] Shapefile no encontrado para {prov_name}: {shp_path}")
            continue

        print(f"\n[MFE50] Procesando teselas forestales para {prov_name.upper()}...")
        # Consultamos las teselas forestales con masa arbórea significativa
        query = f"""
            SELECT 
                NOM_FORARB,
                TFCCARB,
                Shape_Area,
                ST_X(ST_Centroid(ST_Transform(geom, 'EPSG:25830', 'EPSG:4326'))) as lon,
                ST_Y(ST_Centroid(ST_Transform(geom, 'EPSG:25830', 'EPSG:4326'))) as lat
            FROM ST_Read('{shp_path.resolve().as_posix()}')
            WHERE USOS_SUELO NOT IN ('Artificial', 'Agua') 
              AND NOM_FORARB != 'Sin formación arbolada'
              AND TFCCARB >= 30.0
            ORDER BY Shape_Area DESC
        """
        rows = con.execute(query).fetchall()
        print(f"-> {len(rows)} masas forestales filtradas en {prov_name}.")

        count_added = 0
        for r in rows:
            formacion = r[0]
            canopy = float(r[1]) if r[1] is not None else 50.0
            lon = float(r[3])
            lat = float(r[4])

            # Filtro básico de coordenadas dentro del Bounding Box de la provincia
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                continue

            h3_cell = lat_lng_to_h3(lat, lon, H3_RESOLUTION)

            # Evitar duplicar celdas H3 ya procesadas
            if h3_cell not in all_h3_cells:
                botanical_species = normalize_forest_name(formacion)
                
                all_h3_cells[h3_cell] = {
                    "h3_index": h3_cell,
                    "latitude": round(lat, 5),
                    "longitude": round(lon, 5),
                    "elevation_m": None,
                    "slope_deg": None,
                    "aspect_cardinal": None,
                    "soil_ph": None,
                    "dominant_species": botanical_species,
                    "canopy_cover": canopy,
                    "is_forest": True,
                    "province": prov_name,
                    "environmental_data_quality": "unverified"
                }
                count_added += 1

                if count_added >= max_cells_per_province:
                    break

        print(f"-> {count_added} hexágonos H3 únicos consolidados para {prov_name}.")

    print(f"\n[Total] {len(all_h3_cells)} hexágonos H3 forestales generados para Madrid, Segovia, Soria y Guadalajara.")
    return list(all_h3_cells.values())

if __name__ == "__main__":
    cells = process_target_provinces_to_h3()
    db = DuckDBManager()
    db.upsert_static_cells(cells)
    print("Guardado en DuckDB con éxito.")
