"""
Exportador de Snapshots GeoJSON para el Frontend.
Genera archivos estáticos .geojson en la carpeta pública del frontend para visualización instantánea.
"""
import sys
import json
from pathlib import Path

root_path = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(root_path))

from backend.src.api.main import get_scores_geojson

def export_all_species():
    output_dir = root_path / "frontend" / "public" / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    species_list = ["boletus_edulis", "boletus_aereus", "boletus_pinophilus", "boletus_reticulatus"]
    for sp in species_list:
        geojson_data = get_scores_geojson(species_id=sp, min_score=0.0)
        out_file = output_dir / f"{sp}.geojson"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(geojson_data, f, ensure_ascii=False, indent=2)
        print(f"Exportado {sp} -> {out_file} ({len(geojson_data['features'])} polígonos)")

if __name__ == "__main__":
    export_all_species()
