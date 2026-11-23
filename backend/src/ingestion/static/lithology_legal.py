"""Ingesta estática de litología (IGME 1M) y zonas legales (OAPN + Micocyl)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unicodedata
import zipfile

import duckdb
import requests

from backend.config.settings import DB_PATH, RAW_DATA_DIR, TARGET_REGION_BOUNDS
from backend.src.processing.duckdb_manager import DuckDBManager

IGME_1M_URL = (
    "https://info.igme.es/cartografiadigital/datos/geologicos1M/"
    "Geologico1000_(1994)/shapes/geologico_1000_shapes.zip"
)
OAPN_PN_WFS_URL = (
    "https://sigred.oapn.es/geoserverOAPN/LimitesParquesNacionalesZPP/wfs"
    "?service=WFS&version=1.1.0&request=GetFeature"
    "&typename=LimitesParquesNacionalesZPP:view_red_oapn_limite_pn"
    "&srsName=EPSG:4326&outputFormat=application/json"
)
MICOCYL_WFS_URL = (
    "https://idecyl.jcyl.es/geoserver/micologia/wfs"
    "?service=WFS&version=1.1.0&request=GetFeature"
    "&typename=micologia:mico_cyl_zonas_reguladas_peri"
    "&srsName=EPSG:4326&outputFormat=application/json"
    "&BBOX=-4.7,40.0,-1.5,42.1,EPSG:4326"
)


def _norm(text: str | None) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def classify_lithology(dlo: str | None, litologia: str | None = None) -> str:
    """Clasifica una unidad geológica IGME en 'siliceous', 'calcareous', 'mixed' o 'unknown'."""
    primary = _norm(dlo)
    secondary = _norm(litologia)
    text = primary or secondary
    if not text:
        return "unknown"

    carbonate_tokens = (
        "caliza", "dolomia", "marga", "margocaliza", "calcarenita",
        "biocalcarenita", "yeso", "evaporit", "carbonat",
    )
    crystalline_siliceous_tokens = (
        "granitoid", "granito", "gneis", "migmatit", "ortocuarcit",
        "metavulcanit", "rocas volcanicas", "serpentinit", "peridotit",
    )
    clastic_siliceous_tokens = (
        "pizarr", "cuarcit", "esquist", "filita", "grauwac",
        "areniscas silicea", "ampelita", "lidita", "turbiditas silicea",
    )
    detrital_mixed_tokens = ("conglomerad", "grava", "limo", "aluvial", "terraza")

    if any(tok in text for tok in crystalline_siliceous_tokens):
        return "siliceous"
    if any(text.startswith(p) for p in ("pizarras", "cuarcitas", "ortocuarcitas", "esquistos", "areniscas siliceas")):
        return "siliceous"

    has_carb = any(tok in text for tok in carbonate_tokens)
    has_clastic_sil = any(tok in text for tok in clastic_siliceous_tokens)
    has_detrital = any(tok in text for tok in detrital_mixed_tokens)

    if any(text.startswith(p) for p in ("calizas", "dolomias", "margas", "turbiditas calcareas")):
        return "calcareous"
    if "arcillas versicolores y yesos" in text:
        return "calcareous"

    if has_carb and not has_detrital and not has_clastic_sil:
        return "calcareous"
    if has_carb and (has_detrital or "arenas" in text):
        return "mixed"
    if "aluvial" in text or "terrazas" in text or "limos" in text:
        return "mixed"
    if has_clastic_sil or ("arenisca" in text and not has_carb):
        return "siliceous"
    if has_detrital or "arcilla" in text:
        return "mixed"
    return "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_with_windows_tls_fallback(url: str, dest: Path, timeout_s: int = 60) -> None:
    """Descarga un recurso HTTPS usando requests o PowerShell si la CA solo está en Windows."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=dest.suffix, delete=False) as tf:
        tmp_path = Path(tf.name)
    try:
        try:
            resp = requests.get(
                url,
                timeout=timeout_s,
                headers={"User-Agent": "MapaSetero/1.0 (environmental ingestion)"},
            )
            resp.raise_for_status()
            tmp_path.write_bytes(resp.content)
        except requests.exceptions.SSLError:
            if os.name != "nt":
                raise
            ps_cmd = (
                f"$ProgressPreference='SilentlyContinue'; "
                f"Invoke-WebRequest -Uri '{url}' -OutFile '{tmp_path}' -TimeoutSec {timeout_s}"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True)
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError(f"Descarga vacía desde {url}")
        os.replace(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def _ensure_sources(refresh: bool = False):
    igme_dir = RAW_DATA_DIR / "environmental" / "igme"
    legal_dir = RAW_DATA_DIR / "environmental" / "legal"
    igme_dir.mkdir(parents=True, exist_ok=True)
    legal_dir.mkdir(parents=True, exist_ok=True)

    igme_zip = igme_dir / "geologico_1000_shapes.zip"
    geopb_shp = igme_dir / "geologico_1000_shapes" / "geopb_1000.shp"
    if refresh or not geopb_shp.exists():
        if refresh or not igme_zip.exists():
            print("[IGME] Descargando mapa geológico 1:1M...")
            _download_with_windows_tls_fallback(IGME_1M_URL, igme_zip, timeout_s=120)
        with zipfile.ZipFile(igme_zip) as zf:
            zf.extractall(igme_dir)
    if not geopb_shp.exists():
        raise RuntimeError(f"No se encontró el shapefile IGME esperado: {geopb_shp}")

    oapn_geojson = legal_dir / "oapn_parques_nacionales.geojson"
    if refresh or not oapn_geojson.exists():
        print("[OAPN] Descargando límites oficiales de Parques Nacionales...")
        _download_with_windows_tls_fallback(OAPN_PN_WFS_URL, oapn_geojson, timeout_s=45)

    mico_geojson = legal_dir / "micocyl_cyl.geojson"
    if refresh or not mico_geojson.exists():
        print("[IDECyL] Descargando zonas reguladas Micocyl...")
        _download_with_windows_tls_fallback(MICOCYL_WFS_URL, mico_geojson, timeout_s=45)

    return geopb_shp, igme_zip, oapn_geojson, mico_geojson


def ingest_lithology_and_legal(db_path: Path = DB_PATH, refresh: bool = False):
    """Cruza litología IGME y zonas legales (Parques Nacionales + Micocyl) sobre cells_static."""
    DuckDBManager(db_path=db_path)
    geopb_shp, igme_zip, oapn_geojson, mico_geojson = _ensure_sources(refresh=refresh)

    project_root = Path(__file__).resolve().parents[4]
    legal_out_geojson = project_root / "frontend" / "public" / "data" / "legal_zones.geojson"
    legal_out_geojson.parent.mkdir(parents=True, exist_ok=True)

    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    retrieved_at = datetime.now(timezone.utc).isoformat()

    with duckdb.connect(str(db_path)) as con:
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("SET geometry_always_xy = true;")

        # 1. Litología IGME sobre todas las celdas en TARGET_REGION_BOUNDS
        litho_rows = con.execute(f"""
            WITH distinct_cells AS (
                SELECT h3_index, latitude, longitude
                FROM cells_static
                WHERE is_forest = TRUE
                  AND latitude BETWEEN ? AND ?
                  AND longitude BETWEEN ? AND ?
            )
            SELECT DISTINCT ON (c.h3_index)
                c.h3_index,
                COALESCE(NULLIF(TRIM(g.DLO), ''), TRIM(g.LITOLOGIA)) AS lith_desc,
                TRIM(g.LITOLOGIA) AS lith_broad
            FROM distinct_cells c
            LEFT JOIN ST_Read(?) g
              ON ST_Intersects(
                    g.geom,
                    ST_Transform(ST_Point(c.longitude, c.latitude), 'EPSG:4326', 'EPSG:23030')
                 )
            ORDER BY c.h3_index, g.AREA ASC NULLS LAST
        """, [min_lat, max_lat, min_lon, max_lon, geopb_shp.as_posix()]).fetchall()

        litho_updates = [
            (
                classify_lithology(desc, broad),
                (desc or broad)[:180] if (desc or broad) else None,
                h3_id,
            )
            for h3_id, desc, broad in litho_rows
        ]
        con.executemany(
            "UPDATE cells_static SET lithology_class = ?, lithology_desc = ? WHERE h3_index = ?",
            litho_updates,
        )

        # Limpiar marcas legales previas dentro del área antes de recruzar
        con.execute("""
            UPDATE cells_static
            SET legal_zone_type = NULL,
                legal_zone_name = NULL,
                legal_zone_code = NULL
            WHERE latitude BETWEEN ? AND ?
              AND longitude BETWEEN ? AND ?
        """, [min_lat, max_lat, min_lon, max_lon])

        # 2. Zonas reguladas Micocyl (Castilla y León)
        mico_rows = con.execute("""
            SELECT DISTINCT ON (c.h3_index)
                c.h3_index,
                CASE
                    WHEN LOWER(COALESCE(m.n_tip_terreno, '')) LIKE '%parque%'
                    THEN 'Parque Micológico (' || COALESCE(m.n_tip_terreno, 'Micocyl') || ')'
                    ELSE 'Acotado micológico Micocyl'
                END AS zone_name,
                CAST(m.c_clave_id AS VARCHAR) AS zone_code
            FROM cells_static c
            JOIN ST_Read(?) m
              ON ST_Intersects(m.geom, ST_Point(c.longitude, c.latitude))
            WHERE c.is_forest = TRUE
              AND c.latitude BETWEEN ? AND ?
              AND c.longitude BETWEEN ? AND ?
            ORDER BY c.h3_index, m.fid ASC
        """, [mico_geojson.as_posix(), min_lat, max_lat, min_lon, max_lon]).fetchall()

        con.executemany("""
            UPDATE cells_static
            SET legal_zone_type = 'regulated_coto',
                legal_zone_name = ?,
                legal_zone_code = ?
            WHERE h3_index = ?
        """, [(name, code, h3_id) for h3_id, name, code in mico_rows])

        # 3. Parques Nacionales OAPN (prevalece sobre acotado si hubiese solape)
        pn_rows = con.execute("""
            SELECT DISTINCT ON (c.h3_index)
                c.h3_index,
                CAST(p.Nombre AS VARCHAR) AS zone_name
            FROM cells_static c
            JOIN ST_Read(?) p
              ON ST_Intersects(p.geom, ST_Point(c.longitude, c.latitude))
            WHERE c.is_forest = TRUE
              AND c.latitude BETWEEN ? AND ?
              AND c.longitude BETWEEN ? AND ?
            ORDER BY c.h3_index
        """, [oapn_geojson.as_posix(), min_lat, max_lat, min_lon, max_lon]).fetchall()

        con.executemany("""
            UPDATE cells_static
            SET legal_zone_type = 'national_park',
                legal_zone_name = ?,
                legal_zone_code = 'PN-OAPN'
            WHERE h3_index = ?
        """, [(name, h3_id) for h3_id, name in pn_rows])

        # 4. Exportar GeoJSON ligero simplificado para el visor
        pn_features = con.execute("""
            SELECT
                CAST(Nombre AS VARCHAR) AS name,
                CAST(Web AS VARCHAR) AS url,
                ST_AsGeoJSON(
                    ST_SimplifyPreserveTopology(
                        ST_Intersection(geom, ST_MakeEnvelope(?, ?, ?, ?)),
                        0.0012
                    )
                ) AS geom_json
            FROM ST_Read(?)
            WHERE ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
        """, [
            min_lon, min_lat, max_lon, max_lat,
            oapn_geojson.as_posix(),
            min_lon, min_lat, max_lon, max_lat,
        ]).fetchall()

        mico_features = con.execute("""
            SELECT
                CAST(n_tip_terreno AS VARCHAR) AS tip,
                CAST(c_clave_id AS VARCHAR) AS code,
                ST_AsGeoJSON(
                    ST_SimplifyPreserveTopology(
                        ST_Intersection(geom, ST_MakeEnvelope(?, ?, ?, ?)),
                        0.0015
                    )
                ) AS geom_json
            FROM ST_Read(?)
            WHERE ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
        """, [
            min_lon, min_lat, max_lon, max_lat,
            mico_geojson.as_posix(),
            min_lon, min_lat, max_lon, max_lat,
        ]).fetchall()

        features = []
        for name, url, gj_str in pn_features:
            if not gj_str:
                continue
            geom_obj = json.loads(gj_str)
            if not geom_obj or not geom_obj.get("coordinates"):
                continue
            features.append({
                "type": "Feature",
                "properties": {
                    "type": "national_park",
                    "name": name or "Parque Nacional",
                    "code": "PN-OAPN",
                    "url": url or "https://www.parquenacionalsierraguadarrama.es/",
                },
                "geometry": geom_obj,
            })

        for tip, code, gj_str in mico_features:
            if not gj_str:
                continue
            geom_obj = json.loads(gj_str)
            if not geom_obj or not geom_obj.get("coordinates"):
                continue
            features.append({
                "type": "Feature",
                "properties": {
                    "type": "regulated_coto",
                    "name": f"{tip or 'Acotado'} Micocyl",
                    "code": code or "",
                    "url": "https://www.micocyl.es/",
                },
                "geometry": geom_obj,
            })

        out_fc = {
            "type": "FeatureCollection",
            "generated_at": retrieved_at,
            "features": features,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=legal_out_geojson.parent, suffix=".tmp", delete=False
        ) as tf:
            json.dump(out_fc, tf, separators=(",", ":"), ensure_ascii=False)
            tmp_out = Path(tf.name)
        try:
            os.replace(tmp_out, legal_out_geojson)
        finally:
            tmp_out.unlink(missing_ok=True)

        # Registrar procedencia en DuckDB
        con.execute("""
            CREATE TABLE IF NOT EXISTS environmental_layer_provenance (
                layer_name VARCHAR PRIMARY KEY,
                source_url VARCHAR NOT NULL,
                source_product VARCHAR NOT NULL,
                spatial_resolution VARCHAR NOT NULL,
                units VARCHAR NOT NULL,
                license VARCHAR NOT NULL,
                retrieved_at TIMESTAMP NOT NULL,
                method VARCHAR NOT NULL
            )
        """)
        con.executemany(
            "INSERT OR REPLACE INTO environmental_layer_provenance VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "lithology_igme_1m",
                    IGME_1M_URL,
                    "IGME Mapa Geológico de España 1:1.000.000 (geopb_1000)",
                    "1:1.000.000",
                    "siliceous / calcareous / mixed",
                    "IGME Open Data",
                    retrieved_at,
                    "Point-in-polygon ST_Intersects on H3 centroids with deterministic DLO classification.",
                ),
                (
                    "legal_national_parks_oapn",
                    OAPN_PN_WFS_URL,
                    "MITECO / OAPN Límites Parques Nacionales WFS",
                    "vector",
                    "categorical",
                    "MITECO / OAPN Public Data",
                    retrieved_at,
                    "Point-in-polygon ST_Intersects on H3 centroids.",
                ),
                (
                    "legal_micocyl_cyl",
                    MICOCYL_WFS_URL,
                    "JCyL / IDECyL Zonas Reguladas Micología WFS",
                    "vector",
                    "categorical",
                    "Junta de Castilla y León Open Data",
                    retrieved_at,
                    "Point-in-polygon ST_Intersects on H3 centroids.",
                ),
            ],
        )

        litho_summary = con.execute("""
            SELECT COALESCE(lithology_class, 'NULL'), COUNT(*)
            FROM cells_static WHERE is_forest = TRUE
            GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()
        legal_summary = con.execute("""
            SELECT COALESCE(legal_zone_type, 'unregulated'), COUNT(*)
            FROM cells_static WHERE is_forest = TRUE
            GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()

    gj_mb = legal_out_geojson.stat().st_size / (1024 * 1024)
    print(f"[Litología IGME] Resumen en cells_static: {dict(litho_summary)}")
    print(f"[Capa Legal] Resumen en cells_static: {dict(legal_summary)}")
    print(
        f"[Legal GeoJSON] Exportado {legal_out_geojson} "
        f"({len(features)} polígonos, {gj_mb:.2f} MB)"
    )
    return {
        "lithology": dict(litho_summary),
        "legal": dict(legal_summary),
        "geojson_mb": round(gj_mb, 3),
        "features": len(features),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Vuelve a descargar las capas.")
    args = parser.parse_args()
    ingest_lithology_and_legal(refresh=args.refresh)
