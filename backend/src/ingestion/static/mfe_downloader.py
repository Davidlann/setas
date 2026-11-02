"""
Descargador de coberturas oficiales MFE50 para Madrid, Segovia, Soria y Guadalajara.
"""
import os
import requests
import zipfile
import sys
from pathlib import Path
current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.config.settings import RAW_DATA_DIR

MFE_RAW_DIR = RAW_DATA_DIR / "mfe50"
MFE_RAW_DIR.mkdir(parents=True, exist_ok=True)

PROVINCES = {
    "madrid": {
        "code": "28",
        "url": "https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/servicios/banco-datos-naturaleza/MFE50_28_tcm30-200078.zip"
    },
    "segovia": {
        "code": "40",
        "url": "https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/servicios/banco-datos-naturaleza/MFE50_40_tcm30-200076.zip"
    },
    "soria": {
        "code": "42",
        "url": "https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/servicios/banco-datos-naturaleza/MFE50_42_tcm30-200072.zip"
    },
    "guadalajara": {
        "code": "19",
        "url": "https://www.miteco.gob.es/content/dam/miteco/es/biodiversidad/servicios/banco-datos-naturaleza/MFE50_19_tcm30-200104.zip"
    }
}

def download_and_extract(province_key: str):
    info = PROVINCES[province_key]
    zip_path = MFE_RAW_DIR / f"MFE50_{info['code']}.zip"
    extract_dir = MFE_RAW_DIR / province_key

    if not zip_path.exists():
        print(f"Descargando MFE50 para {province_key.upper()} ({info['url']})...")
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(info['url'], headers=headers, stream=True, timeout=60)
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        print(f"Descargado: {zip_path} ({zip_path.stat().st_size / (1024*1024):.2f} MB)")

    if not extract_dir.exists():
        print(f"Descomprimiendo en {extract_dir}...")
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_dir)
        print(f"Descomprimido con éxito.")

    return extract_dir

if __name__ == "__main__":
    import sys
    prov = sys.argv[1] if len(sys.argv) > 1 else "madrid"
    download_and_extract(prov)
