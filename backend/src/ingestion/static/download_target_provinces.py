import sys
from pathlib import Path

# Buscar la raíz del proyecto que contiene 'backend'
current = Path(__file__).resolve()
while current.name != "backend" and current.parent != current:
    current = current.parent
project_root = current.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.src.ingestion.static.mfe_downloader import download_and_extract, PROVINCES

def download_all_target_provinces():
    print("Iniciando descarga oficial del MFE50 (MITECO) para las 4 provincias...")
    for prov in ["madrid", "segovia", "soria", "guadalajara"]:
        print(f"\n--- Procesando {prov.upper()} ---")
        extract_dir = download_and_extract(prov)
        print(f"Directorio listo: {extract_dir}")

if __name__ == "__main__":
    download_all_target_provinces()
