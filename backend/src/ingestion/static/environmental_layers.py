"""Ingesta de relieve IGN y datos edáficos SoilGrids para la malla H3 local."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import duckdb
import numpy as np
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.transform import Affine
from rasterio.warp import transform
from scipy.spatial import cKDTree

from backend.config.settings import DB_PATH, RAW_DATA_DIR, TARGET_REGION_BOUNDS
from backend.src.processing.h3_indexer import h3_to_lat_lng

DEM_URL = "https://servicios.idee.es/wcs-inspire/mdt"
DEM_COVERAGE = "Elevacion4258_200"
SOIL_BASE_URL = "https://files.isric.org/soilgrids/latest/data"
SOIL_LAYERS = {
    "mean": "phh2o/phh2o_0-5cm_mean",
    "q05": "phh2o/phh2o_0-5cm_Q0.05",
    "q95": "phh2o/phh2o_0-5cm_Q0.95",
}
SOIL_TEXTURE_LAYERS = {
    "sand": "sand/sand_5-15cm_mean",
    "silt": "silt/silt_5-15cm_mean",
    "clay": "clay/clay_5-15cm_mean",
}
SOIL_SCALE_FACTOR = 10


def _download_dem(session: requests.Session, path: Path, refresh: bool) -> None:
    if path.exists() and not refresh:
        with rasterio.open(path) as dataset:
            if dataset.count != 1 or not dataset.width or not dataset.height:
                raise RuntimeError(f"El MDT almacenado no es válido: {path}")
            if dataset.crs is None or dataset.crs.to_epsg() != 4326:
                raise RuntimeError(f"El MDT almacenado no usa EPSG:4326: {path}")
        return

    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    response = session.get(
        DEM_URL,
        params=[
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("request", "GetCoverage"),
            ("coverageId", DEM_COVERAGE),
            ("subset", f"Lat({min_lat},{max_lat})"),
            ("subset", f"Long({min_lon},{max_lon})"),
            ("format", "image/tiff"),
        ],
        timeout=180,
    )
    response.raise_for_status()
    with MemoryFile(response.content) as memory_file:
        with memory_file.open() as dataset:
            if dataset.count != 1 or dataset.crs is None:
                raise RuntimeError("IGN WCS devolvió un MDT no interpretable.")
            if dataset.crs.to_epsg() != 4326:
                raise RuntimeError("IGN WCS no devolvió el MDT en EPSG:4326.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, suffix=".tif", delete=False
    ) as temp_file:
        temp_file.write(response.content)
        temp_path = Path(temp_file.name)
    try:
        with rasterio.open(temp_path) as dataset:
            if dataset.count != 1 or dataset.crs.to_epsg() != 4326:
                raise RuntimeError("El MDT temporal descargado no es válido.")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _soil_vrt_metadata(session: requests.Session, layer: str):
    vrt_url = f"{SOIL_BASE_URL}/{layer}.vrt"
    response = session.get(vrt_url, timeout=90)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    srs = root.findtext("SRS")
    geotransform = root.findtext("GeoTransform")
    if not srs or not geotransform:
        raise RuntimeError(f"El VRT de SoilGrids está incompleto: {vrt_url}")
    affine = Affine.from_gdal(
        *(float(value) for value in geotransform.split(","))
    )

    rectangles = []
    for source in root.findall(".//SimpleSource") + root.findall(".//ComplexSource"):
        filename = source.find("SourceFilename")
        src_rect = source.find("SrcRect")
        dst_rect = source.find("DstRect")
        if filename is None or filename.text is None or src_rect is None or dst_rect is None:
            continue
        src = tuple(float(src_rect.attrib[key]) for key in ("xOff", "yOff", "xSize", "ySize"))
        dst = tuple(float(dst_rect.attrib[key]) for key in ("xOff", "yOff", "xSize", "ySize"))
        rectangles.append((filename.text, src, dst))
    if not rectangles:
        raise RuntimeError(f"El VRT de SoilGrids no contiene teselas: {vrt_url}")
    return vrt_url, srs, affine, rectangles


def _find_soil_sources(coords, srs, affine, rectangles):
    x_coords, y_coords = transform(
        "EPSG:4326",
        srs,
        [lon for _, lon in coords],
        [lat for lat, _ in coords],
    )
    pixels = np.array([(~affine) * (x, y) for x, y in zip(x_coords, y_coords)])
    centers = np.array([
        (dst[0] + dst[2] / 2, dst[1] + dst[3] / 2)
        for _, _, dst in rectangles
    ])
    _, candidates = cKDTree(centers).query(pixels, k=min(32, len(rectangles)))
    if candidates.ndim == 1:
        candidates = candidates[:, None]

    samples = []
    for (column, row), candidate_indices in zip(pixels, candidates):
        found = None
        for rect_index in candidate_indices:
            filename, src, dst = rectangles[int(rect_index)]
            if dst[0] <= column < dst[0] + dst[2] and dst[1] <= row < dst[1] + dst[3]:
                source_col = src[0] + (column - dst[0]) * src[2] / dst[2]
                source_row = src[1] + (row - dst[1]) * src[3] / dst[3]
                found = (filename, int(source_row), int(source_col))
                break
        samples.append(found)
    return samples


def _download_soil_tiles(session, vrt_url, samples, layer_cache_dir, refresh):
    needed_files = {sample[0] for sample in samples if sample is not None}
    downloaded = {}
    vrt_directory = vrt_url.rsplit("/", 1)[0] + "/"
    for relative_path in needed_files:
        source_path = relative_path.removeprefix("./")
        local_path = layer_cache_dir / Path(source_path)
        if refresh or not local_path.exists():
            response = session.get(vrt_directory + source_path, timeout=120)
            response.raise_for_status()
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=local_path.parent, suffix=".tif", delete=False
            ) as temp_file:
                temp_file.write(response.content)
                temp_path = Path(temp_file.name)
            try:
                with rasterio.open(temp_path) as dataset:
                    if (
                        dataset.count != 1
                        or dataset.width <= 0
                        or dataset.height <= 0
                        or dataset.crs is None
                    ):
                        raise RuntimeError(f"Tesela SoilGrids inesperada: {source_path}")
                os.replace(temp_path, local_path)
            finally:
                temp_path.unlink(missing_ok=True)
        downloaded[relative_path] = local_path
    return downloaded


def _sample_soil(session, coords, layer, cache_dir, refresh):
    vrt_url, srs, affine, rectangles = _soil_vrt_metadata(session, layer)
    samples = _find_soil_sources(coords, srs, affine, rectangles)
    layer_cache_dir = cache_dir / Path(layer)
    paths = _download_soil_tiles(
        session, vrt_url, samples, layer_cache_dir, refresh
    )
    values = [None] * len(coords)
    with ExitStack() as stack:
        datasets = {
            filename: stack.enter_context(rasterio.open(path))
            for filename, path in paths.items()
        }
        rasters = {
            filename: dataset.read(1)
            for filename, dataset in datasets.items()
        }
        for index, sample in enumerate(samples):
            if sample is None:
                continue
            filename, row, column = sample
            dataset = datasets[filename]
            if not (
                0 <= row < dataset.height
                and 0 <= column < dataset.width
            ):
                continue
            value = rasters[filename][row, column]
            if value != datasets[filename].nodata and np.isfinite(value):
                values[index] = float(value) / SOIL_SCALE_FACTOR
    return values, paths


def _terrain_values(dataset, lat, lon, data):
    row, column = dataset.index(lon, lat)
    if row < 1 or column < 1 or row + 1 >= data.shape[0] or column + 1 >= data.shape[1]:
        return None, None, None

    window = data[row - 1:row + 2, column - 1:column + 2]
    nodata = dataset.nodata
    if not np.all(np.isfinite(window)) or (
        nodata is not None and np.any(window == nodata)
    ):
        return None, None, None

    lat_cellsize_m = abs(dataset.transform.e) * 111320
    lon_cellsize_m = abs(dataset.transform.a) * 111320 * math.cos(math.radians(lat))
    dz_dx = (
        window[0, 2] + 2 * window[1, 2] + window[2, 2]
        - window[0, 0] - 2 * window[1, 0] - window[2, 0]
    ) / (8 * lon_cellsize_m)
    dz_dy = (
        window[0, 0] + 2 * window[0, 1] + window[0, 2]
        - window[2, 0] - 2 * window[2, 1] - window[2, 2]
    ) / (8 * lat_cellsize_m)
    slope = math.degrees(math.atan(math.hypot(dz_dx, dz_dy)))
    if slope < 0.5:
        aspect = None
    else:
        azimuth = (math.degrees(math.atan2(-dz_dx, -dz_dy)) + 360) % 360
        directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        aspect = directions[round(azimuth / 45) % 8]
    return float(window[1, 1]), slope, aspect


def ingest_environmental_layers(refresh=False, cell_limit=None):
    raw_dir = RAW_DATA_DIR / "environmental"
    dem_path = raw_dir / "ign" / "mdt_pnoa_200m_4326.tif"
    soil_cache = raw_dir / "soilgrids"
    session = requests.Session()
    session.headers["User-Agent"] = "MapaSetero/1.0 (environmental data ingestion)"

    with duckdb.connect(str(DB_PATH)) as con:
        con.execute("""
            ALTER TABLE cells_static
            ADD COLUMN IF NOT EXISTS soil_ph_q05 DOUBLE
        """)
        con.execute("""
            ALTER TABLE cells_static
            ADD COLUMN IF NOT EXISTS soil_ph_q95 DOUBLE
        """)
        for column in (
            "soil_sand_percent",
            "soil_silt_percent",
            "soil_clay_percent",
        ):
            con.execute(
                f"ALTER TABLE cells_static ADD COLUMN IF NOT EXISTS {column} DOUBLE"
            )
        rows = con.execute("""
            SELECT h3_index FROM cells_static WHERE is_forest = TRUE
        """).fetchall()

    min_lon, min_lat, max_lon, max_lat = TARGET_REGION_BOUNDS
    cells = []
    for (h3_index,) in rows:
        lat, lon = h3_to_lat_lng(h3_index)
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            cells.append({"h3_index": h3_index, "latitude": lat, "longitude": lon})
    if cell_limit is not None:
        if cell_limit < 1:
            raise ValueError("--limit debe ser un entero positivo.")
        cells = cells[:cell_limit]
    if not cells:
        raise RuntimeError("No hay celdas forestales en el área de estudio configurada.")
    coords = [(cell["latitude"], cell["longitude"]) for cell in cells]

    _download_dem(session, dem_path, refresh)
    with rasterio.open(dem_path) as dem:
        if dem.crs is None or dem.crs.to_epsg() != 4326:
            raise RuntimeError("El MDT del IGN no está en EPSG:4326 como se esperaba.")
        elevation_data = dem.read(1)
        terrain = [
            _terrain_values(dem, lat, lon, elevation_data)
            for lat, lon in coords
        ]

    soil_results = {}
    source_assets = []
    retrieved_at = datetime.now(timezone.utc).isoformat()
    source_assets.append((
        "elevation_m_slope_aspect",
        dem_path.name,
        f"{DEM_URL}?service=WCS&request=GetCoverage&coverageId={DEM_COVERAGE}",
        _sha256(dem_path),
        str(dem_path),
        retrieved_at,
    ))
    for name, layer in SOIL_LAYERS.items():
        soil_results[name], tile_paths = _sample_soil(
            session, coords, layer, soil_cache, refresh
        )
        print(f"SoilGrids {layer}: {len(tile_paths)} teselas consultadas.")
        source_assets.extend(
            (
                f"soil_ph_{name}",
                relative_path.removeprefix("./"),
                f"{SOIL_BASE_URL}/{layer.rsplit('/', 1)[0]}/{relative_path.removeprefix('./')}",
                _sha256(path),
                str(path),
                retrieved_at,
            )
            for relative_path, path in tile_paths.items()
        )
    for component, layer in SOIL_TEXTURE_LAYERS.items():
        soil_results[f"texture_{component}"], tile_paths = _sample_soil(
            session, coords, layer, soil_cache, refresh
        )
        print(f"SoilGrids {layer}: {len(tile_paths)} teselas consultadas.")
        source_assets.extend(
            (
                f"soil_{component}_percent",
                relative_path.removeprefix("./"),
                f"{SOIL_BASE_URL}/{layer.rsplit('/', 1)[0]}/{relative_path.removeprefix('./')}",
                _sha256(path),
                str(path),
                retrieved_at,
            )
            for relative_path, path in tile_paths.items()
        )

    records = []
    for index, cell in enumerate(cells):
        elevation, slope, aspect = terrain[index]
        soil_mean = soil_results["mean"][index]
        soil_q05 = soil_results["q05"][index]
        soil_q95 = soil_results["q95"][index]
        texture_values = tuple(
            soil_results[f"texture_{component}"][index]
            for component in ("sand", "silt", "clay")
        )
        soil_values = (soil_mean, soil_q05, soil_q95)
        if all(value is not None for value in soil_values) and not (
            0 <= soil_q05 <= soil_mean <= soil_q95 <= 14
        ):
            raise RuntimeError(
                f"Valores SoilGrids fuera de rango o cuantiles incoherentes "
                f"en la celda {cell['h3_index']}: {soil_values}"
            )
        if any(value is not None and not 0 <= value <= 100 for value in texture_values):
            raise RuntimeError(
                f"Textura SoilGrids fuera del rango 0-100 % "
                f"en la celda {cell['h3_index']}: {texture_values}"
            )
        has_values = (
            elevation is not None and slope is not None
            and soil_mean is not None and soil_q05 is not None and soil_q95 is not None
        )
        records.append((
            elevation, slope, aspect, soil_mean, soil_q05, soil_q95,
            *texture_values,
            "sourced_unvalidated" if has_values else "unverified",
            cell["h3_index"],
        ))

    with duckdb.connect(str(DB_PATH)) as con:
        con.executemany("""
            UPDATE cells_static SET
                elevation_m = ?,
                slope_deg = ?,
                aspect_cardinal = ?,
                soil_ph = ?,
                soil_ph_q05 = ?,
                soil_ph_q95 = ?,
                soil_sand_percent = ?,
                soil_silt_percent = ?,
                soil_clay_percent = ?,
                environmental_data_quality = ?
            WHERE h3_index = ?
        """, records)
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
        provenance = [
            (
                "elevation_m_slope_aspect",
                f"{DEM_URL}?service=WCS&request=GetCoverage&coverageId={DEM_COVERAGE}",
                "IGN PNOA-LiDAR Digital Terrain Model",
                "200 m",
                "elevation m; slope degrees; aspect cardinal",
                "CC BY 4.0 SCNE",
                retrieved_at,
                "Elevation sampled at H3 center; slope and aspect from 3x3 Horn gradient.",
            ),
            *[
                (
                    f"soil_ph_{key}",
                    f"{SOIL_BASE_URL}/{layer}.vrt",
                    f"ISRIC SoilGrids 2.0 pH in H2O, depth 0-5 cm ({key})",
                    "250 m",
                    "pH × 10 in source, divided by 10",
                    "CC BY 4.0",
                    retrieved_at,
                    "Nearest SoilGrids raster pixel sampled at H3 center.",
                )
                for key, layer in SOIL_LAYERS.items()
            ],
            *[
                (
                    f"soil_{component}_percent",
                    f"{SOIL_BASE_URL}/{layer}.vrt",
                    f"ISRIC SoilGrids 2.0 {component} content, depth 5-15 cm (mean)",
                    "250 m",
                    "g/kg divided by 10 to percent by mass",
                    "CC BY 4.0",
                    retrieved_at,
                    "Nearest SoilGrids raster pixel sampled at H3 center.",
                )
                for component, layer in SOIL_TEXTURE_LAYERS.items()
            ],
        ]
        con.executemany("""
            INSERT OR REPLACE INTO environmental_layer_provenance
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, provenance)
        con.execute("""
        CREATE TABLE IF NOT EXISTS environmental_source_assets (
            layer_name VARCHAR NOT NULL,
            asset_name VARCHAR NOT NULL,
            source_url VARCHAR NOT NULL,
            sha256 VARCHAR NOT NULL,
            local_path VARCHAR NOT NULL,
            retrieved_at TIMESTAMP NOT NULL,
            PRIMARY KEY (layer_name, asset_name)
        )
        """)
        con.executemany("""
        INSERT OR REPLACE INTO environmental_source_assets
        VALUES (?, ?, ?, ?, ?, ?)
        """, source_assets)

    sourced = sum(record[9] == "sourced_unvalidated" for record in records)
    print(
        f"Variables ambientales obtenidas para {sourced}/{len(records)} celdas. "
        "Calidad: sourced_unvalidated; no habilita puntuaciones predictivas."
    )
    print(
        "Valores ausentes: elevación "
        f"{sum(record[0] is None for record in records)}, pH medio "
        f"{sum(record[3] is None for record in records)}, cuantiles "
        f"q05 {sum(record[4] is None for record in records)} / "
        f"q95 {sum(record[5] is None for record in records)}; textura ausente "
        f"arena {sum(record[6] is None for record in records)}, "
        f"limo {sum(record[7] is None for record in records)}, "
        f"arcilla {sum(record[8] is None for record in records)}."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Descarga otra vez las fuentes.")
    parser.add_argument("--limit", type=int, help="Limita celdas (solo para pruebas).")
    args = parser.parse_args()
    ingest_environmental_layers(refresh=args.refresh, cell_limit=args.limit)
