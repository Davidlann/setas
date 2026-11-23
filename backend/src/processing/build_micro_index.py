"""Genera fragmentos estáticos H3-9 agrupados por H3-5 para el micro-detalle bajo demanda."""
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import duckdb
import h3
import numpy as np
import rasterio
from rasterio.transform import rowcol
from rasterio.warp import transform as warp_transform

from backend.config.settings import DB_PATH, RAW_DATA_DIR, TARGET_REGION_BOUNDS
from backend.src.ingestion.static.lithology_legal import classify_lithology
from backend.src.processing.mfe_processor import normalize_forest_name
from backend.src.scoring.engine import MushroomScoringEngine

LITHO_CODES = {
    "siliceous": "S",
    "calcareous": "C",
    "mixed": "M",
    "unknown": "U",
}


def _sample_local_soil_ph_x10(coords_lat_lon, ph_dir: Path):
    """Muestrea pH×10 desde las teselas SoilGrids locales cacheadas (sin red)."""
    tifs = sorted(ph_dir.rglob("*.tif"))
    n = len(coords_lat_lon)
    out = [None] * n
    if not tifs or n == 0:
        return out

    lats = [lat for lat, _ in coords_lat_lon]
    lons = [lon for _, lon in coords_lat_lon]
    # Transformamos una vez al CRS de la primera tesela (todas comparten Homolosine)
    cached_crs = None
    xs = ys = None

    for tif in tifs:
        with rasterio.open(tif) as ds:
            if ds.crs is None:
                continue
            crs_wkt = ds.crs.to_wkt()
            if crs_wkt != cached_crs:
                cached_crs = crs_wkt
                xs, ys = warp_transform("EPSG:4326", ds.crs, lons, lats)
                xs = np.asarray(xs, dtype=np.float64)
                ys = np.asarray(ys, dtype=np.float64)

            bounds = ds.bounds
            mask = (
                (xs >= bounds.left)
                & (xs <= bounds.right)
                & (ys >= bounds.bottom)
                & (ys <= bounds.top)
            )
            idxs = np.flatnonzero(mask)
            if len(idxs) == 0:
                continue

            rows, cols = rowcol(ds.transform, xs[idxs], ys[idxs])
            rows = np.asarray(rows, dtype=np.int64)
            cols = np.asarray(cols, dtype=np.int64)
            valid = (rows >= 0) & (rows < ds.height) & (cols >= 0) & (cols < ds.width)
            idxs = idxs[valid]
            if len(idxs) == 0:
                continue
            rows = rows[valid]
            cols = cols[valid]

            band = ds.read(1)
            nodata = ds.nodata
            vals = band[rows, cols]
            for pos, raw_val in zip(idxs, vals):
                if out[int(pos)] is not None:
                    continue
                v = float(raw_val)
                if not np.isfinite(v) or (nodata is not None and raw_val == nodata) or v <= 0:
                    continue
                out[int(pos)] = int(round(v))
    return out


def build_micro_index(db_path: Path = DB_PATH, output_dir: Path | None = None):
    """Construye fragmentos H3-5 con los 7 hijos H3-9 de cada celda activa."""
    project_root = Path(__file__).resolve().parents[3]
    if output_dir is None:
        output_dir = project_root / "frontend" / "public" / "data" / "micro"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    engine = MushroomScoringEngine()
    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS

    with duckdb.connect(str(db_path), read_only=True) as con:
        cols_in_db = {r[0] for r in con.execute("DESCRIBE cells_static").fetchall()}
        extra_cols = [
            c for c in ("soil_ph_q05", "soil_sand_percent", "soil_silt_percent",
                        "soil_clay_percent", "tpi_m", "soil_soc_g_kg", "lithology_class")
            if c in cols_in_db
        ]
        sel_cols = ["h3_index", "elevation_m", "soil_ph", "dominant_species", "canopy_cover"] + extra_cols
        rows = con.execute(f"""
            SELECT {", ".join(sel_cols)}
            FROM cells_static
            WHERE is_forest = TRUE
              AND environmental_data_quality = 'sourced_unvalidated'
              AND longitude BETWEEN ? AND ?
              AND latitude BETWEEN ? AND ?
              AND elevation_m IS NOT NULL
              AND soil_ph IS NOT NULL
              AND dominant_species IS NOT NULL
              AND canopy_cover IS NOT NULL
            ORDER BY h3_index
        """, [min_lon, max_lon, min_lat, max_lat]).fetchall()

    active_h8 = []
    for r in rows:
        cell_dict = dict(zip(sel_cols, r))
        if engine.has_potential_habitat(cell_dict):
            active_h8.append(cell_dict["h3_index"])

    print(f"[Micro H3-9] Celdas H3-8 activas: {len(active_h8)}")
    if not active_h8:
        raise RuntimeError("No hay celdas activas para construir el micro-índice H3-9.")

    subcells = []
    for h8 in active_h8:
        for h9 in sorted(h3.cell_to_children(h8, 9)):
            lat, lon = h3.cell_to_latlng(h9)
            h5 = h3.cell_to_parent(h9, 5)
            subcells.append((h9, h5, round(lat, 6), round(lon, 6)))

    print(f"[Micro H3-9] Subceldas H3-9 a cruzar: {len(subcells)}")

    # 1. Cruce espacial en memoria con MFE50 + IGME
    sp_con = duckdb.connect()
    sp_con.execute("INSTALL spatial; LOAD spatial;")
    sp_con.execute("SET geometry_always_xy = true;")
    sp_con.execute("CREATE TEMP TABLE pts (idx INTEGER, h9 VARCHAR, lat DOUBLE, lon DOUBLE)")
    sp_con.executemany(
        "INSERT INTO pts VALUES (?, ?, ?, ?)",
        [(idx, sc[0], sc[2], sc[3]) for idx, sc in enumerate(subcells)],
    )
    sp_con.execute("""
        CREATE TEMP TABLE pts_geom AS
        SELECT
            idx, h9,
            ST_Transform(ST_Point(lon, lat), 'EPSG:4326', 'EPSG:25830') AS g25830,
            ST_Transform(ST_Point(lon, lat), 'EPSG:4326', 'EPSG:23030') AS g23030
        FROM pts
    """)

    trees_by_idx = {}
    mfe_shps = [
        RAW_DATA_DIR / "mfe50" / "madrid" / "mfe50_28.shp",
        RAW_DATA_DIR / "mfe50" / "segovia" / "mfe50_40.shp",
        RAW_DATA_DIR / "mfe50" / "soria" / "mfe50_42.shp",
        RAW_DATA_DIR / "mfe50" / "guadalajara" / "mfe50_19.shp",
    ]
    for shp in mfe_shps:
        if not shp.exists():
            continue
        matched = sp_con.execute("""
            SELECT DISTINCT ON (p.idx)
                p.idx,
                m.NOM_FORARB,
                COALESCE(m.TFCCARB, 0.0) AS cc
            FROM pts_geom p
            JOIN ST_Read(?) m
              ON ST_Intersects(m.geom, p.g25830)
            WHERE m.USOS_SUELO NOT IN ('Artificial', 'Agua')
              AND m.NOM_FORARB IS NOT NULL
              AND m.NOM_FORARB != 'Sin formación arbolada'
            ORDER BY p.idx, COALESCE(m.TFCCARB, 0.0) DESC, m.Shape_Area DESC
        """, [shp.as_posix()]).fetchall()
        for idx, formacion, cc in matched:
            cc_int = int(round(max(0.0, min(100.0, float(cc)))))
            prev = trees_by_idx.get(idx)
            if prev is None or cc_int > prev[1]:
                trees_by_idx[idx] = (normalize_forest_name(formacion), cc_int)
        print(f"   [MFE50] {shp.name}: {len(matched)} subceldas con arbolado")

    litho_by_idx = {}
    geopb_shp = RAW_DATA_DIR / "environmental" / "igme" / "geologico_1000_shapes" / "geopb_1000.shp"
    if geopb_shp.exists():
        l_rows = sp_con.execute("""
            SELECT DISTINCT ON (p.idx)
                p.idx,
                COALESCE(NULLIF(TRIM(g.DLO), ''), TRIM(g.LITOLOGIA)) AS lith_desc,
                TRIM(g.LITOLOGIA) AS lith_broad
            FROM pts_geom p
            JOIN ST_Read(?) g
              ON ST_Intersects(g.geom, p.g23030)
            ORDER BY p.idx, g.AREA ASC NULLS LAST
        """, [geopb_shp.as_posix()]).fetchall()
        for idx, desc, broad in l_rows:
            litho_by_idx[idx] = LITHO_CODES.get(classify_lithology(desc, broad), "U")
        print(f"   [IGME] {len(litho_by_idx)} subceldas clasificadas")
    sp_con.close()

    # 2. Muestreo SoilGrids pH 0-5 cm offline
    ph_dir = RAW_DATA_DIR / "environmental" / "soilgrids" / "phh2o" / "phh2o_0-5cm_mean"
    ph_x10_list = _sample_local_soil_ph_x10([(sc[2], sc[3]) for sc in subcells], ph_dir)
    ph_valid_count = sum(v is not None for v in ph_x10_list)
    print(f"   [SoilGrids pH H3-9] {ph_valid_count}/{len(subcells)} subceldas muestreadas")

    # 3. Agrupar por H3-5 y escribir fragmentos compactos
    chunks = defaultdict(list)
    for idx, (h9, h5, _lat, _lon) in enumerate(subcells):
        tree_name, cc_int = trees_by_idx.get(idx, ("", 0))
        lith_code = litho_by_idx.get(idx, "U")
        ph_x10 = ph_x10_list[idx]
        chunks[h5].append((h9, tree_name, cc_int, ph_x10, lith_code))

    # Limpiar posibles chunks obsoletos previos
    written_names = {f"{h5}.json" for h5 in chunks} | {"index.json"}
    for old_file in output_dir.glob("*.json"):
        if old_file.name not in written_names:
            old_file.unlink()

    total_bytes = 0
    for h5, items in sorted(chunks.items()):
        unique_trees = [""] + sorted({t for _, t, _, _, _ in items if t})
        tree_to_idx = {name: i for i, name in enumerate(unique_trees)}
        cell_map = {
            h9: [tree_to_idx[tree_name], cc_int, ph_x10, lith_code]
            for h9, tree_name, cc_int, ph_x10, lith_code in items
        }
        payload = {
            "res": 9,
            "h5": h5,
            "trees": unique_trees,
            "c": cell_map,
        }
        out_path = output_dir / f"{h5}.json"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_dir, suffix=".tmp", delete=False
        ) as tf:
            json.dump(payload, tf, separators=(",", ":"), ensure_ascii=False)
            tmp_path = Path(tf.name)
        try:
            os.replace(tmp_path, out_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        total_bytes += out_path.stat().st_size

    index_payload = {
        "res": 9,
        "parent_res": 5,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_subcells": len(subcells),
        "tree_matched_subcells": len(trees_by_idx),
        "ph_matched_subcells": ph_valid_count,
        "chunks": sorted(chunks.keys()),
    }
    index_path = output_dir / "index.json"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_dir, suffix=".tmp", delete=False
    ) as tf:
        json.dump(index_payload, tf, separators=(",", ":"), ensure_ascii=False)
        tmp_path = Path(tf.name)
    try:
        os.replace(tmp_path, index_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    total_bytes += index_path.stat().st_size

    total_mb = total_bytes / (1024 * 1024)
    print(
        f"[Micro H3-9] {len(chunks)} fragmentos escritos en {output_dir} "
        f"(total: {total_mb:.2f} MB; cobertura arbolado: {len(trees_by_idx)}/{len(subcells)})"
    )
    return {
        "chunks": len(chunks),
        "total_subcells": len(subcells),
        "tree_matched_subcells": len(trees_by_idx),
        "ph_matched_subcells": ph_valid_count,
        "total_mb": round(total_mb, 3),
    }


if __name__ == "__main__":
    build_micro_index()
