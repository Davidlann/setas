import os
from pathlib import Path

# Directorio raíz del proyecto
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
STATIC_MESH_DIR = DATA_DIR / "static_mesh"

# Crear directorios clave si no existen
for d in [DATA_DIR, RAW_DATA_DIR, PROCESSED_DATA_DIR, STATIC_MESH_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Base de datos DuckDB espacial
DB_PATH = PROCESSED_DATA_DIR / "mycologist_spatial.duckdb"

# Resolución H3 predeterminada del prototipo
# Resolución 8: área media aproximada de 0,737 km² y arista media de ~531 m.
H3_RESOLUTION = 8

# Envelope for screening the four target provinces; not their exact boundaries.
TARGET_REGION_BOUNDS = (-5.5, 39.5, -1.0, 42.5)

# Bounding Box de España (Península, Baleares, Canarias)
# Formato: [min_lon, min_lat, max_lon, max_lat]
SPAIN_BBOX = {
    "peninsula_baleares": [-9.5, 35.8, 4.5, 43.8],
    "canarias": [-18.2, 27.5, -13.3, 29.5],
}

# Configuración Open-Meteo
OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# Fuentes oficiales
MFE50_WFS_OR_DOWNLOAD = "https://www.miteco.gob.es/es/biodiversidad/servicios/banco-datos-naturaleza/informacion-disponible/mfe50.html"
SOILGRIDS_REST_URL = "https://rest.isric.org/soilgrids/v2.0"
